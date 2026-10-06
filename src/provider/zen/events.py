"""OpenCode Zen SSE → 中立事件映射。

Zen 是标准 OpenAI 流式协议（`data: {chat.completion.chunk}`，`data: [DONE]`
结束），无需私有协议解析。实测要点（2026-09-29）：

* 正文在 `choices[0].delta.content`，思考在 `delta.reasoning_content`；
* `finish_reason` 可能与最后一段正文同帧（同一 chunk 里既有 content 又有
  finish_reason）→ 必须用 `parse_all_events` 才能同时拿到两者；
* `usage` 在收尾的独立帧里给出（`choices: []`），含
  `prompt_tokens_details.cached_tokens`；
* `[DONE]` 之后还有一帧 `{"choices": [], "cost": "0"}`，无下游语义，跳过。

解析宽松：不做完整协议校验，但结构非法（JSON 坏 / 非对象 / choices 非数组）
必须显式失败而非静默，否则会变成「上游坏了但客户端收到空回复」。
"""

from __future__ import annotations

import json
from typing import Any

from ...engine.sse import SSEFrame
from ...provider.base import (
    ErrKind,
    Event,
    EventKind,
    UpstreamProtocolViolation,
    Usage,
)
from ..openai_chunk import first_choice, is_blank_tool_call


def parse_frame(frame: SSEFrame) -> Event | None:
    """单帧 → 单个代表性事件；`[DONE]` 与无内容帧返回 None。"""
    payload = _payload(frame)
    if payload is None:
        return None
    return _event_from_payload(payload)


def _event_from_payload(payload: dict[str, Any]) -> Event | None:
    """已解析 JSON → 代表性事件。

    优先级：tool_calls > 正文 > 思考 > 错误 > usage > finish。
    """
    choice = first_choice(payload)
    delta = choice.get("delta") if choice else None
    delta = delta if isinstance(delta, dict) else {}
    finish_reason = choice.get("finish_reason") if choice else None

    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list):
        # 无 name 且 arguments 为空的噪声调用（客户端聚合后显示
        # "Tool not found"）：正常分片块无 name 但带实际 arguments，必须保留。
        kept = [tc for tc in tool_calls
                if isinstance(tc, dict) and not is_blank_tool_call(tc)]
        if kept:
            return Event(kind=EventKind.TOOL_CALLS, tool_calls=kept)

    content = delta.get("content")
    if isinstance(content, str) and content:
        return Event(kind=EventKind.CONTENT, content=content)

    reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return Event(kind=EventKind.REASONING, content=reasoning)

    error = payload.get("error")
    if isinstance(error, dict):
        return _error_event(error)

    usage = payload.get("usage")
    if isinstance(usage, dict):
        return Event(kind=EventKind.USAGE, usage=_usage(usage))

    if isinstance(finish_reason, str) and finish_reason:
        return Event(kind=EventKind.FINISH, finish_reason=finish_reason)
    return None


def parse_all_events(frame: SSEFrame) -> list[Event]:
    """一帧拆成多个事件：同一 chunk 可同时携带正文与 finish_reason。

    必须基于代表性事件再补 usage/finish，否则「最后一段正文 + finish」
    这种合并帧会丢掉 finish（客户端永远收不到结束信号）。
    """
    payload = _payload(frame)
    if payload is None:
        return []
    events: list[Event] = [e for e in (_event_from_payload(payload),) if e is not None]
    has_usage = any(e.kind is EventKind.USAGE for e in events)
    has_finish = any(e.kind is EventKind.FINISH for e in events)

    usage = payload.get("usage")
    if isinstance(usage, dict) and not has_usage:
        events.append(Event(kind=EventKind.USAGE, usage=_usage(usage)))
    choice = first_choice(payload)
    finish_reason = choice.get("finish_reason") if choice else None
    if isinstance(finish_reason, str) and finish_reason and not has_finish:
        events.append(Event(kind=EventKind.FINISH, finish_reason=finish_reason))
    return events


def _payload(frame: SSEFrame) -> dict[str, Any] | None:
    """帧 → JSON 对象；空帧 / `[DONE]` 返回 None，坏帧显式失败。"""
    if not frame.data:
        return None
    if frame.data.strip() == "[DONE]":
        return None
    try:
        payload = json.loads(frame.data)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation("unparsable Zen SSE data") from error
    if not isinstance(payload, dict):
        raise UpstreamProtocolViolation("Zen SSE data is not an object")
    return payload


def _error_event(error: dict[str, Any]) -> Event:
    """流内错误信封（OpenAI 兼容网关可能返回 `{"error": {...}}`）。

    `error.code` 可能是 int 也可能是字符串（如 `invalid_request_error`）；
    只有 int 才进 error_code，其余按消息分类。
    """
    raw_code = error.get("code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    message = error.get("message")
    return Event(
        kind=EventKind.ERROR,
        error_code=code,
        error_message=str(message) if message is not None else "",
        error_kind=classify_error_code(code),
    )


def _usage(raw: dict[str, Any]) -> Usage:
    def as_int(key: str) -> int | None:
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    # 缓存命中：OpenAI 惯例在 prompt_tokens_details.cached_tokens，顶层兜底
    details = raw.get("prompt_tokens_details")
    cached = (details or {}).get("cached_tokens") if isinstance(details, dict) else None
    if not isinstance(cached, int) or isinstance(cached, bool):
        cached = as_int("cached_tokens")

    credit = raw.get("credit")
    return Usage(
        input_tokens=as_int("prompt_tokens"),
        output_tokens=as_int("completion_tokens"),
        reasoning_tokens=as_int("reasoning_tokens"),
        cached_tokens=cached,
        credit=float(credit) if isinstance(credit, (int, float)) and not isinstance(credit, bool)
        else None,
    )


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码分类。

    Zen 免费层的拒绝来自**请求形状**（UA 版本过低 426 / 门禁 403），不是
    凭证问题；把它归成 DEAD 会把无凭证渠道直接打停，归成 REQUEST 则只
    轮换不惩罚。400 表示模型不可用（如已下线的 free 模型）→ INVALID，
    跳过该渠道而不是冷却虚拟凭证。
    401（`Missing API key.`）同理：Zen 无凭证，401 只说明**这个模型需要
    付费 key**，不是虚拟凭证失效——归 DEAD 会因一次付费模型请求把整条
    zen 渠道硬禁用，故同样按 INVALID 跳过。
    """
    if status == 401:
        return ErrKind.INVALID
    if status == 429:
        return ErrKind.SOFT
    if status in (400, 404, 422):
        return ErrKind.INVALID
    if status == 403:
        return ErrKind.REQUEST
    return ErrKind.OTHER


def classify_error_code(code: int | None) -> ErrKind:
    """流内错误码 → 错误分类（仅 int 码；字符串码走 OTHER）。"""
    if code == 401:
        return ErrKind.INVALID
    if code == 429:
        return ErrKind.SOFT
    if code in (400, 404, 422):
        return ErrKind.INVALID
    if code == 403:
        return ErrKind.REQUEST
    return ErrKind.OTHER
