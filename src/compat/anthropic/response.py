"""Anthropic Messages 出站：中立 Event → Anthropic SSE 帧 / 非流式对象（P0-1）。

接口与 `compat.responses.response.ResponsesStreamTranslator` 对齐
（translate / finish / keepalive / usage / done_sent / error_frame），由
executor 注入使用，因此引擎侧的轮换、冷却、统计逻辑完全无感。

Anthropic 事件序列：
    message_start
    content_block_start × N
    content_block_delta × N（text_delta / input_json_delta / thinking_delta）
    content_block_stop × N
    message_delta（stop_reason + 最终 usage）
    message_stop

终止事件是 `message_stop`（没有 OpenAI 的 `[DONE]` 哨兵）；`done_sent`
在发出 `message_stop` 时置位，供 executor 区分「正常收尾」与「中途断开」。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

from ...provider.base import Event, EventKind, Usage

# 思考块在 Anthropic 里要求 stop 前带一个 signature_delta。上游 chat 不产生
# 签名，这里给一个固定的不透明占位；本网关入站又把 thinking 块只读 thinking
# 字段、忽略 signature，因此占位串在网关内部往返不会出错。
_THINKING_SIGNATURE = "coding2api"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _frame(event_type: str, payload: dict[str, Any]) -> bytes:
    body = {"type": event_type, **payload}
    return (f"event: {event_type}\n"
            f"data: {json.dumps(body, ensure_ascii=False)}\n\n").encode()


def _stop_reason(finish_reason: str) -> str:
    """chat finish_reason → Anthropic stop_reason。"""
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }.get(finish_reason, "end_turn")


def _error_type_for(code: str) -> str:
    """本服务错误码 → Anthropic 错误类型（客户端按类型分类）。"""
    return {
        "invalid_request": "invalid_request_error",
        "no_healthy_credential": "overloaded_error",
        "internal_error": "api_error",
    }.get(code, "api_error")


def _usage_payload(usage: Usage | None) -> dict[str, int]:
    """Anthropic usage 形状：input_tokens / output_tokens 必填，缓存命中另计。

    Anthropic 语义里 input / cache_read / cache_creation 三者互斥（`input_tokens`
    **不含**缓存命中）；本网关内部 `Usage.input_tokens` 取 OpenAI 口径的
    `prompt_tokens`（**含**命中，见各 provider 的 events 解析）。故命中时先从
    `input_tokens` 扣除、再另记 `cache_read_input_tokens`——否则 Claude Code 会
    把同一批 token 既按全价输入、又按缓存读各计一次。上游没报命中（None）时
    不补 0 占位（与 OpenAI 出口「上游没报就不冒充已上报」同一纪律）。
    """
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0}
    input_tokens = usage.input_tokens or 0
    payload: dict[str, int] = {
        "input_tokens": input_tokens,
        "output_tokens": usage.output_tokens or 0}
    cached = usage.cached_tokens
    if cached:
        payload["input_tokens"] = max(0, input_tokens - cached)
        payload["cache_read_input_tokens"] = cached
    return payload


class AnthropicStreamTranslator:
    """把 Event 序列翻译为 Anthropic SSE 帧序列。"""

    def __init__(self, model: str, *, message_id: str | None = None) -> None:
        self.model = model
        self.message_id = message_id or _id("msg")
        self.usage: Usage | None = None
        self.done_sent = False
        self._started = False
        self._next_index = 0
        self._open_index: int | None = None
        self._open_kind: str | None = None
        # chat 的 tool_calls index → 本出口已分配的 content block index
        self._tool_blocks: dict[int, int] = {}

    # ------------------------------------------------------------ 内部工具

    def _start(self) -> Iterator[bytes]:
        self._started = True
        yield _frame("message_start", {"message": {
            "id": self.message_id, "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def _close_open(self) -> Iterator[bytes]:
        if self._open_kind == "thinking":
            yield _frame("content_block_delta", {
                "index": self._open_index,
                "delta": {"type": "signature_delta", "signature": _THINKING_SIGNATURE}})
        if self._open_index is not None:
            yield _frame("content_block_stop", {"index": self._open_index})
        self._open_index = None
        self._open_kind = None

    def _open_block(self, kind: str, block: dict[str, Any]) -> Iterator[bytes]:
        self._open_index = self._next_index
        self._next_index += 1
        self._open_kind = kind
        yield _frame("content_block_start",
                     {"index": self._open_index, "content_block": block})

    # ------------------------------------------------------------ 事件翻译

    def translate(self, event: Event) -> Iterator[bytes]:
        if event.kind is EventKind.USAGE:
            self.usage = event.usage
            return
        if event.kind is EventKind.ERROR:
            yield from self._fail(event.error_message or "upstream error")
            return
        if event.kind is EventKind.CONTENT and event.content:
            yield from self._text(event.content)
            return
        if event.kind is EventKind.REASONING and event.content:
            yield from self._thinking(event.content)
            return
        if event.kind is EventKind.TOOL_CALLS and event.tool_calls:
            yield from self._tool_calls(event.tool_calls)
            return
        if event.kind is EventKind.FINISH:
            yield from self._close(event.finish_reason or "stop")

    def finish(self) -> Iterator[bytes]:
        """上游未发终止事件就断流：补终止帧，保证客户端不会挂住。"""
        if self.done_sent:
            return
        yield from self._close("stop")

    def keepalive(self) -> bytes:
        """SSE 注释帧心跳（与 chat / Responses 出口同一约定）。"""
        from ...engine.sse import SSE_COMMENT

        return SSE_COMMENT

    def error_frame(self, message: str, code: str) -> bytes:
        """出口错误帧：Anthropic 用 `event: error`（无 [DONE] 哨兵）。"""
        return b"".join(self._fail(message, code=code))

    # ------------------------------------------------------------ 分块细节

    def _text(self, chunk: str) -> Iterator[bytes]:
        if not self._started:
            yield from self._start()
        if self._open_kind != "text":
            yield from self._close_open()
            yield from self._open_block("text", {"type": "text", "text": ""})
        yield _frame("content_block_delta", {
            "index": self._open_index,
            "delta": {"type": "text_delta", "text": chunk}})

    def _thinking(self, chunk: str) -> Iterator[bytes]:
        if not self._started:
            yield from self._start()
        if self._open_kind != "thinking":
            yield from self._close_open()
            yield from self._open_block("thinking",
                                        {"type": "thinking", "thinking": ""})
        yield _frame("content_block_delta", {
            "index": self._open_index,
            "delta": {"type": "thinking_delta", "thinking": chunk}})

    def _tool_calls(self, deltas: list[dict[str, Any]]) -> Iterator[bytes]:
        if not self._started:
            yield from self._start()
        for delta in deltas:
            key = delta.get("index")
            if not isinstance(key, int) or isinstance(key, bool):
                key = max(self._tool_blocks, default=-1) + 1
            function = delta.get("function") or {}
            block_index = self._tool_blocks.get(key)
            if block_index is None:
                # 新工具调用：先关掉当前块，再开一个新的 tool_use 块
                yield from self._close_open()
                name = function.get("name") or ""
                yield from self._open_block("tool_use", {
                    "type": "tool_use", "id": delta.get("id") or _id("toolu"),
                    "name": name, "input": {}})
                block_index = self._open_index
                self._tool_blocks[key] = block_index
            fragment = function.get("arguments")
            if isinstance(fragment, str) and fragment:
                yield _frame("content_block_delta", {
                    "index": block_index,
                    "delta": {"type": "input_json_delta",
                              "partial_json": fragment}})

    def _close(self, finish_reason: str) -> Iterator[bytes]:
        if self.done_sent:
            return
        if not self._started:
            yield from self._start()
        yield from self._close_open()
        self.done_sent = True
        yield _frame("message_delta", {
            "delta": {"stop_reason": _stop_reason(finish_reason),
                      "stop_sequence": None},
            "usage": _usage_payload(self.usage)})
        yield _frame("message_stop", {})

    def _fail(self, message: str, *, code: str = "upstream_error") -> Iterator[bytes]:
        if self.done_sent:
            return
        self.done_sent = True
        yield _frame("error", {"error": {"type": _error_type_for(code),
                                         "message": message}})


def _content_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """chat 的 assistant message → Anthropic content 块列表。"""
    blocks: list[dict[str, Any]] = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        blocks.append({"type": "thinking", "thinking": reasoning,
                       "signature": _THINKING_SIGNATURE})
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        blocks.append({
            "type": "tool_use",
            "id": call.get("id") or _id("toolu"),
            "name": function.get("name") or "",
            "input": _parse_arguments(function.get("arguments")),
        })
    return blocks


def _parse_arguments(value: Any) -> Any:
    """chat 的 arguments JSON 串 → Anthropic 的 input 对象；解析失败回落空对象。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_or_none(value: Any) -> int | None:
    """chat usage 里的计数只认非 bool 的 int，其余（缺失/串/浮点）当未知。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _completion_usage(usage: dict[str, Any]) -> Usage:
    """chat.completion 的 OpenAI 形状 usage → 内部 Usage（含缓存命中）。"""
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return Usage(input_tokens=_int_or_none(usage.get("prompt_tokens")),
                 output_tokens=_int_or_none(usage.get("completion_tokens")),
                 cached_tokens=_int_or_none(cached))


def completion_to_message(completion: dict[str, Any], *,
                          message_id: str | None = None) -> dict[str, Any]:
    """聚合好的 chat.completion → Anthropic `message` 对象（非流式出口）。"""
    choice = (completion.get("choices") or [])[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"
    usage = completion.get("usage")
    usage_obj = _completion_usage(usage) if isinstance(usage, dict) else None
    return {
        "id": message_id or _id("msg"),
        "type": "message",
        "role": "assistant",
        "model": completion.get("model") or "",
        "content": _content_from_message(message),
        "stop_reason": _stop_reason(finish_reason),
        "stop_sequence": None,
        "usage": _usage_payload(usage_obj),
    }
