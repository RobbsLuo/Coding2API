"""Responses 出站：中立 Event → Responses SSE 帧 / 非流式对象（B2.1）。

字段形状全部取自官方 openai-python 3.x 的类型定义（由 OpenAI OpenAPI
生成，见 `openai/types/responses/*.py`），不凭记忆写。本模块只发 Codex
CLI 会话需要的子集：

    response.created
    response.output_item.added        （message / reasoning / function_call）
    response.content_part.added       （output_text）
    response.output_text.delta × N
    response.output_text.done
    response.content_part.done
    response.output_item.done
    response.function_call_arguments.delta / .done
    response.reasoning_summary_text.delta / .done
    response.completed | response.incomplete

Responses 协议**没有 `[DONE]` 哨兵**，终止事件本身就是结束；`done_sent`
在发出终止事件时置位，供 executor 区分「正常收尾」与「中途断开」。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

from ...provider.base import Event, EventKind, Usage


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _frame(event_type: str, payload: dict[str, Any]) -> bytes:
    """Responses SSE 帧：`event:` 行 + `data:` 行（与官方格式一致）。"""
    body = {"type": event_type, **payload}
    return (f"event: {event_type}\n"
            f"data: {json.dumps(body, ensure_ascii=False)}\n\n").encode()


def _usage_payload(usage: Usage | None) -> dict[str, Any]:
    """Responses 的 usage 形状（字段名与 chat 不同，且全为必填 int）。"""
    if usage is None:
        return {"input_tokens": 0, "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 0}
    prompt = usage.input_tokens or 0
    completion = usage.output_tokens or 0
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": usage.cached_tokens or 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": usage.reasoning_tokens or 0},
        "total_tokens": prompt + completion,
    }


def _empty_response(model: str, response_id: str, created_at: int, *,
                    status: str = "in_progress") -> dict[str, Any]:
    """Response 对象的公共骨架；`output` 由调用方填充。

    必填字段（官方类型）：id / created_at / model / object / output /
    parallel_tool_calls / tool_choice / tools。
    """
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "metadata": {},
        "previous_response_id": None,
        "reasoning": None,
        "text": None,
        "temperature": None,
        "top_p": None,
        "truncation": "disabled",
        "user": None,
    }


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "output_text", "text": text, "annotations": []}


def _error_code_for(code: str) -> str:
    """本服务的错误码 → Responses/Codex 认识的错误码。

    Codex CLI 按 code 分类（见 codex-rs/codex-api/src/sse/responses.rs）：
    `context_length_exceeded` / `rate_limit_exceeded` / `server_is_overloaded`
    / `insufficient_quota` 等，未识别的一律按「可重试」处理。
    """
    return {
        "invalid_request": "invalid_request",
        "no_healthy_credential": "server_is_overloaded",
        "internal_error": "internal_error",
    }.get(code, code)


class ResponsesStreamTranslator:
    """把 Event 序列翻译为 Responses SSE 帧序列。

    接口与 `compat.openai.response.StreamTranslator` 对齐（translate /
    finish / keepalive / usage / done_sent），由 executor 注入使用，
    因此引擎侧的轮换、冷却、统计逻辑完全无感。
    """

    def __init__(self, model: str, *, response_id: str | None = None,
                 created_at: int | None = None) -> None:
        self.model = model
        self.response_id = response_id or _id("resp")
        self.created_at = created_at if created_at is not None else int(time.time())
        self.usage: Usage | None = None
        self.done_sent = False
        self._sequence = 0
        self._created = False
        self._order: list[int] = []                 # output_index 顺序
        self._items: dict[int, dict[str, Any]] = {}
        self._text_index: int | None = None
        self._text: list[str] = []
        self._reasoning_index: int | None = None
        self._reasoning: list[str] = []
        # chat 的 tool_calls index → 本出口的 output_index 与其参数累积
        self._tool_slots: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------ 内部工具

    def _next_sequence(self) -> int:
        value = self._sequence
        self._sequence += 1
        return value

    def _emit(self, event_type: str, payload: dict[str, Any]) -> bytes:
        return _frame(event_type, {"sequence_number": self._next_sequence(), **payload})

    def _add_item(self, item: dict[str, Any]) -> int:
        index = len(self._order)
        self._order.append(index)
        self._items[index] = item
        return index

    def _response_snapshot(self, *, status: str,
                           incomplete_reason: str | None = None) -> dict[str, Any]:
        response = _empty_response(self.model, self.response_id, self.created_at,
                                   status=status)
        response["output"] = [self._items[i] for i in self._order]
        response["usage"] = _usage_payload(self.usage)
        if incomplete_reason is not None:
            response["incomplete_details"] = {"reason": incomplete_reason}
        return response

    # ------------------------------------------------------------ 事件翻译

    def translate(self, event: Event) -> Iterator[bytes]:
        if event.kind is EventKind.USAGE:
            # 与 chat 出口一致：usage 不单独成帧，留给终止事件承载
            self.usage = event.usage
            return
        if event.kind is EventKind.ERROR:
            # executor 在调用 translate 前已拦截流内错误事件；这里只做兜底
            yield from self._fail(event.error_message or "upstream error")
            return
        if event.kind is EventKind.CONTENT and event.content:
            yield from self._content(event.content)
            return
        if event.kind is EventKind.REASONING and event.content:
            yield from self._reasoning_event(event.content)
            return
        if event.kind is EventKind.TOOL_CALLS and event.tool_calls:
            yield from self._tool_calls(event.tool_calls)
            return
        if event.kind is EventKind.FINISH:
            yield from self._close(event.finish_reason or "stop")

    def finish(self) -> Iterator[bytes]:
        """上游未发终止事件就断流：补一个终止事件，保证客户端不会挂住。"""
        if self.done_sent:
            return
        yield from self._close("stop")

    def keepalive(self) -> bytes:
        """SSE 注释帧心跳（与 chat 出口同一约定）。"""
        from ...engine.sse import SSE_COMMENT

        return SSE_COMMENT

    def error_frame(self, message: str, code: str) -> bytes:
        """出口错误帧：Responses 用 `response.failed` 事件（无 [DONE] 哨兵）。"""
        return b"".join(self._fail(message, code=_error_code_for(code)))

    # ------------------------------------------------------------ 分块细节

    def _content(self, chunk: str) -> Iterator[bytes]:
        if not self._created:
            yield from self._created_event()
        if self._text_index is None:
            index = self._add_item({"id": _id("msg"), "type": "message",
                                    "role": "assistant", "status": "in_progress",
                                    "content": [_text_part("")]})
            self._text_index = index
            item = self._items[index]
            yield self._emit("response.output_item.added", {
                "output_index": index, "item": item})
            yield self._emit("response.content_part.added", {
                "output_index": index, "item_id": item["id"], "content_index": 0,
                "part": _text_part("")})
        self._text.append(chunk)
        item = self._items[self._text_index]
        yield self._emit("response.output_text.delta", {
            "output_index": self._text_index, "item_id": item["id"],
            "content_index": 0, "delta": chunk, "logprobs": []})

    def _reasoning_event(self, chunk: str) -> Iterator[bytes]:
        if not self._created:
            yield from self._created_event()
        if self._reasoning_index is None:
            index = self._add_item({"id": _id("rs"), "type": "reasoning",
                                    "summary": [], "status": "in_progress"})
            self._reasoning_index = index
            item = self._items[index]
            item["summary"] = [{"type": "summary_text", "text": ""}]
            yield self._emit("response.output_item.added", {
                "output_index": index, "item": item})
            yield self._emit("response.reasoning_summary_part.added", {
                "output_index": index, "item_id": item["id"], "summary_index": 0,
                "part": {"type": "summary_text", "text": ""}})
        self._reasoning.append(chunk)
        item = self._items[self._reasoning_index]
        yield self._emit("response.reasoning_summary_text.delta", {
            "output_index": self._reasoning_index, "item_id": item["id"],
            "summary_index": 0, "delta": chunk})

    def _tool_calls(self, deltas: list[dict[str, Any]]) -> Iterator[bytes]:
        if not self._created:
            yield from self._created_event()
        for delta in deltas:
            index = delta.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                index = max(self._tool_slots, default=-1) + 1
            function = delta.get("function") or {}
            slot = self._tool_slots.get(index)
            if slot is None:
                name = function.get("name") or ""
                item_id = _id("fc")
                call_id = delta.get("id") or _id("call")
                item = {"id": item_id, "type": "function_call", "call_id": call_id,
                        "name": name, "arguments": "", "status": "in_progress"}
                output_index = self._add_item(item)
                slot = {"output_index": output_index, "item": item, "args": []}
                self._tool_slots[index] = slot
                yield self._emit("response.output_item.added", {
                    "output_index": output_index, "item": item})
            elif isinstance(function.get("name"), str) and function["name"]:
                # 少数上游把函数名放在后续分片；补齐以免 done 事件里是空名
                slot["item"]["name"] = function["name"]
            fragment = function.get("arguments")
            if isinstance(fragment, str) and fragment:
                slot["args"].append(fragment)
                yield self._emit("response.function_call_arguments.delta", {
                    "output_index": slot["output_index"],
                    "item_id": slot["item"]["id"], "delta": fragment})

    def _created_event(self) -> Iterator[bytes]:
        self._created = True
        yield self._emit("response.created", {
            "response": self._response_snapshot(status="in_progress")})

    def _close_text(self) -> Iterator[bytes]:
        if self._text_index is None:
            return
        item = self._items[self._text_index]
        text = "".join(self._text)
        item["content"] = [_text_part(text)]
        item["status"] = "completed"
        yield self._emit("response.output_text.done", {
            "output_index": self._text_index, "item_id": item["id"],
            "content_index": 0, "text": text, "logprobs": []})
        yield self._emit("response.content_part.done", {
            "output_index": self._text_index, "item_id": item["id"],
            "content_index": 0, "part": _text_part(text)})

    def _close_reasoning(self) -> Iterator[bytes]:
        if self._reasoning_index is None:
            return
        item = self._items[self._reasoning_index]
        text = "".join(self._reasoning)
        item["summary"] = [{"type": "summary_text", "text": text}]
        item["status"] = "completed"
        yield self._emit("response.reasoning_summary_text.done", {
            "output_index": self._reasoning_index, "item_id": item["id"],
            "summary_index": 0, "text": text})
        yield self._emit("response.reasoning_summary_part.done", {
            "output_index": self._reasoning_index, "item_id": item["id"],
            "summary_index": 0, "part": {"type": "summary_text", "text": text}})

    def _close_tools(self) -> Iterator[bytes]:
        for slot in self._tool_slots.values():
            item = slot["item"]
            arguments = "".join(slot["args"])
            item["arguments"] = arguments
            item["status"] = "completed"
            yield self._emit("response.function_call_arguments.done", {
                "output_index": slot["output_index"], "item_id": item["id"],
                "arguments": arguments})

    def _close_items(self) -> Iterator[bytes]:
        for index in self._order:
            item = self._items[index]
            item["status"] = "completed"
            yield self._emit("response.output_item.done", {
                "output_index": index, "item": item})

    def _close(self, finish_reason: str) -> Iterator[bytes]:
        if self.done_sent:
            return
        if not self._created:
            yield from self._created_event()
        yield from self._close_text()
        yield from self._close_reasoning()
        yield from self._close_tools()
        yield from self._close_items()
        # length / content_filter 不是「正常完成」，如实回 incomplete
        if finish_reason == "length":
            status, reason = "incomplete", "max_output_tokens"
        elif finish_reason == "content_filter":
            status, reason = "incomplete", "content_filter"
        else:
            status, reason = "completed", None
        self.done_sent = True
        if status == "incomplete":
            yield self._emit("response.incomplete", {
                "response": self._response_snapshot(status=status,
                                                    incomplete_reason=reason)})
        else:
            yield self._emit("response.completed", {
                "response": self._response_snapshot(status=status)})

    def _fail(self, message: str, *, code: str = "upstream_error") -> Iterator[bytes]:
        if self.done_sent:
            return
        if not self._created:
            yield from self._created_event()
        self.done_sent = True
        response = self._response_snapshot(status="failed")
        # Codex CLI 按 error.code 分类（context_length_exceeded / rate_limit_exceeded
        # / server_is_overloaded …），必须给字符串码；未知码它按 Retryable 处理
        response["error"] = {"code": code, "message": message}
        yield self._emit("response.failed", {"response": response})


def _output_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """chat 的 assistant message → Responses 的 output item 列表。"""
    output: list[dict[str, Any]] = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        output.append({"id": _id("rs"), "type": "reasoning", "status": "completed",
                       "summary": [{"type": "summary_text", "text": reasoning}]})
    content = message.get("content")
    if isinstance(content, str) and content:
        output.append({"id": _id("msg"), "type": "message", "role": "assistant",
                       "status": "completed", "content": [_text_part(content)]})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        output.append({
            "id": _id("fc"), "type": "function_call",
            "call_id": call.get("id") or _id("call"),
            "name": function.get("name") or "",
            "arguments": function.get("arguments") or "",
            "status": "completed",
        })
    return output


def completion_to_response(completion: dict[str, Any], *,
                           response_id: str | None = None,
                           created_at: int | None = None) -> dict[str, Any]:
    """聚合好的 chat.completion → Responses 的 `response` 对象（非流式出口）。

    复用 `executor.complete`（同一套选号/重试/统计），只做出口形状转换。
    """
    choice = (completion.get("choices") or [])[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"
    if finish_reason == "length":
        status, reason = "incomplete", "max_output_tokens"
    elif finish_reason == "content_filter":
        status, reason = "incomplete", "content_filter"
    else:
        status, reason = "completed", None

    usage = completion.get("usage") or {}
    prompt = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    payload = _empty_response(completion.get("model") or "",
                              response_id or _id("resp"),
                              created_at if created_at is not None else int(time.time()),
                              status=status)
    payload["output"] = _output_from_message(message)
    payload["usage"] = {
        "input_tokens": prompt if isinstance(prompt, int) else 0,
        "input_tokens_details": {
            "cached_tokens": _nested_int(usage, "prompt_tokens_details", "cached_tokens")},
        "output_tokens": completion_tokens if isinstance(completion_tokens, int) else 0,
        "output_tokens_details": {
            "reasoning_tokens": _nested_int(usage, "completion_tokens_details",
                                            "reasoning_tokens")},
        "total_tokens": usage.get("total_tokens")
        if isinstance(usage.get("total_tokens"), int)
        else (prompt or 0) + (completion_tokens or 0)
        if isinstance(prompt, int) or isinstance(completion_tokens, int) else 0,
    }
    if reason is not None:
        payload["incomplete_details"] = {"reason": reason}
    return payload


def _nested_int(source: dict[str, Any], key: str, inner: str) -> int:
    value = source.get(key)
    if isinstance(value, dict) and isinstance(value.get(inner), int):
        return value[inner]
    return 0
