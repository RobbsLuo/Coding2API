"""Gemini 出站：中立 `Event` → `streamGenerateContent` SSE / 非流式响应。

形状取自 Google Generative Language API（`GenerateContentResponse`）：

    {
      "candidates": [
        {"content": {"parts": [{"text": "..."}], "role": "model"},
         "finishReason": "STOP", "index": 0}
      ],
      "usageMetadata": {"promptTokenCount", "candidatesTokenCount",
                        "totalTokenCount", "cachedContentTokenCount"},
      "modelVersion": "...",
      "responseId": "..."
    }

流式（`?alt=sse`）每帧一个完整 `GenerateContentResponse`，增量只在 parts 里；
Gemini SDK 不认 OpenAI 的 `[DONE]` 哨兵，终止帧（带 `finishReason`）即结束，
`done_sent` 在发出终止帧时置位。

思考内容用 Gemini 的 `{"text": ..., "thought": true}` 表示；工具调用用
`{"functionCall": {"name", "args"}}`。Gemini 没有调用 id，与本服务入站映射
同口径（靠函数名配对）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

from ...provider.base import Event, EventKind, Usage

# chat finish_reason → Gemini finishReason（枚举见官方 GenerateContentResponse）。
_FINISH_REASON = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",
    "content_filter": "SAFETY",
}


def _frame(payload: dict[str, Any]) -> bytes:
    """Gemini SSE 帧：只有 `data:` 行（官方 `alt=sse` 形状，无 `event:` 行）。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _usage_metadata(usage: Usage | None) -> dict[str, int]:
    """内部 Usage → `usageMetadata`（字段名按官方类型，缺失不补 0 以外的值）。"""
    if usage is None:
        return {}
    prompt = usage.input_tokens or 0
    completion = usage.output_tokens or 0
    metadata = {
        "promptTokenCount": prompt,
        "candidatesTokenCount": completion,
        "totalTokenCount": prompt + completion,
    }
    if usage.cached_tokens is not None:
        metadata["cachedContentTokenCount"] = usage.cached_tokens
    if usage.reasoning_tokens:
        metadata["thoughtsTokenCount"] = usage.reasoning_tokens
    return metadata


def _text_part(text: str) -> dict[str, Any]:
    return {"text": text}


def _candidate(parts: list[dict[str, Any]], *, finish_reason: str | None = None
               ) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "content": {"parts": parts, "role": "model"},
        "index": 0,
    }
    if finish_reason is not None:
        candidate["finishReason"] = finish_reason
    return candidate


class GeminiStreamTranslator:
    """把 Event 序列翻译为 Gemini SSE 帧序列（`StreamSink` 实现）。"""

    def __init__(self, model: str, *, response_id: str | None = None) -> None:
        self._model = model
        self._response_id = response_id or f"resp_{uuid.uuid4().hex}"
        self.usage: Usage | None = None
        self.done_sent = False
        self._started = False

    def _prefix(self) -> dict[str, Any]:
        return {"modelVersion": self._model, "responseId": self._response_id}

    def _chunk(self, parts: list[dict[str, Any]]) -> bytes:
        """增量帧：首个帧带 `modelVersion` / `responseId`，后续帧只带 candidates。"""
        payload: dict[str, Any] = {"candidates": [_candidate(parts)]}
        if not self._started:
            payload.update(self._prefix())
            self._started = True
        return _frame(payload)

    def translate(self, event: Event) -> Iterator[bytes]:
        if event.kind is EventKind.USAGE:
            self.usage = event.usage
            return
        if event.kind is EventKind.ERROR:
            yield self.error_frame(event.error_message or "upstream error",
                                   "internal_error")
            return
        if event.kind is EventKind.CONTENT and event.content:
            yield self._chunk([_text_part(event.content)])
            return
        if event.kind is EventKind.REASONING and event.content:
            yield self._chunk([{"text": event.content, "thought": True}])
            return
        if event.kind is EventKind.TOOL_CALLS and event.tool_calls:
            yield from self._tool_parts(event.tool_calls)
            return
        if event.kind is EventKind.FINISH:
            yield from self._close(event.finish_reason or "stop")

    def _tool_parts(self, deltas: list[dict[str, Any]]) -> Iterator[bytes]:
        """工具增量：Gemini 没有分片参数协议，能解析出完整对象时整块发出。

        上游先给 name、后给 arguments 分片，因此这里**只发已能解析成对象**的
        参数；解析不出就跳过该分片，等下一片（Gemini 客户端拿不到半截 JSON，
        比发一个 `` 让模型执行空参更安全）。
        """
        for delta in deltas:
            function = delta.get("function") or {}
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            arguments = function.get("arguments")
            if arguments in (None, ""):
                args: dict[str, Any] = {}
            else:
                try:
                    parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                args = parsed
            yield self._chunk([{"functionCall": {"name": name, "args": args}}])

    def _close(self, finish_reason: str) -> Iterator[bytes]:
        if self.done_sent:
            return
        self.done_sent = True
        payload: dict[str, Any] = {
            "candidates": [_candidate([], finish_reason=_FINISH_REASON.get(
                finish_reason, "STOP"))],
            **self._prefix(),
        }
        metadata = _usage_metadata(self.usage)
        if metadata:
            payload["usageMetadata"] = metadata
        yield _frame(payload)

    def finish(self) -> Iterator[bytes]:
        """上游未发终止事件就断流：补终止帧，保证客户端不会挂住。"""
        if self.done_sent:
            return
        yield from self._close("stop")

    def keepalive(self) -> bytes:
        """SSE 注释帧：Gemini SDK 按 SSE 规范忽略注释，长静默下保住连接。"""
        return b": keepalive\n\n"

    def error_frame(self, message: str, code: str) -> bytes:
        """Gemini 错误形状（Google API 错误信封），终止流。"""
        if self.done_sent:
            return b""
        self.done_sent = True
        status = {"invalid_request": "INVALID_ARGUMENT",
                  "no_healthy_credential": "UNAVAILABLE"}.get(code, "INTERNAL")
        return _frame({"error": {"code": 400 if status == "INVALID_ARGUMENT" else 500,
                                 "message": message, "status": status}})


def completion_to_gemini(completion: dict[str, Any], *,
                         response_id: str | None = None) -> dict[str, Any]:
    """聚合好的 chat.completion → Gemini `GenerateContentResponse`（非流式）。"""
    choice = (completion.get("choices") or [])[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"
    parts: list[dict[str, Any]] = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        parts.append({"text": reasoning, "thought": True})
    content = message.get("content")
    if isinstance(content, str) and content:
        parts.append(_text_part(content))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        raw_arguments = function.get("arguments")
        try:
            parsed = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            # 坏的 arguments 已由上游 fail-closed 拦过；这里再兜一层，避免
            # 把非对象塞进 Gemini 的 `args`（官方类型要求 object）
            continue
        parts.append({"functionCall": {"name": name, "args": parsed}})
    if not parts:
        parts.append(_text_part(""))
    usage = completion.get("usage")
    payload: dict[str, Any] = {
        "candidates": [{"content": {"parts": parts, "role": "model"},
                        "finishReason": _FINISH_REASON.get(finish_reason, "STOP"),
                        "index": 0}],
        "modelVersion": completion.get("model") or "",
        "responseId": response_id or f"resp_{uuid.uuid4().hex}",
    }
    if isinstance(usage, dict):
        metadata: dict[str, int] = {}
        prompt = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if isinstance(prompt, int) and not isinstance(prompt, bool):
            metadata["promptTokenCount"] = prompt
        if isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool):
            metadata["candidatesTokenCount"] = completion_tokens
        if metadata:
            metadata["totalTokenCount"] = (metadata.get("promptTokenCount", 0)
                                           + metadata.get("candidatesTokenCount", 0))
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if isinstance(cached, int) and not isinstance(cached, bool) and cached:
            metadata["cachedContentTokenCount"] = cached
        if metadata:
            payload["usageMetadata"] = metadata
    return payload
