"""CodeBuddy SSE → 中立事件映射。

上游是 OpenAI 风格 SSE（data: {...choices[].delta...}），以 data: [DONE] 结束。
宽松解析：不在事件层做完整协议校验（AGENTS.md 约束），
但结构非法（JSON 坏、非对象、choices 非数组）必须显式失败而非静默。
"""

from __future__ import annotations

import json
from typing import Any

from ...engine.sse import SSEFrame
from ...provider.base import ErrKind, Event, EventKind, Usage


class UpstreamProtocolViolation(ValueError):
    """上游事件违反可映射的结构约束。"""


def _is_blank_tool_call(tc: dict) -> bool:
    """无 name 且 arguments 为空/{} 的噪声 tool_call（模型输出的空调用）。"""
    fn = tc.get("function")
    fn = fn if isinstance(fn, dict) else {}
    if str(fn.get("name") or "").strip():
        return False
    args = fn.get("arguments")
    return args in (None, "", "{}", {})


def parse_frame(frame: SSEFrame) -> Event | None:
    """单帧 → 中立事件；[DONE] 与无 delta 的帧返回 None。"""
    if not frame.data:
        return None
    if frame.data.strip() == "[DONE]":
        return None
    try:
        payload = json.loads(frame.data)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation("unparsable CodeBuddy SSE data") from error
    if not isinstance(payload, dict):
        raise UpstreamProtocolViolation("CodeBuddy SSE data is not an object")

    choice = _first_choice(payload)
    delta = choice.get("delta") if choice else None
    delta = delta if isinstance(delta, dict) else {}
    finish_reason = choice.get("finish_reason") if choice else None

    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list):
        # 上游偶发噪声调用：无 name 且 arguments 为空/{}（客户端聚合后
        # 显示 "Tool not found"）。正常分片块无 name 但带实际 arguments，
        # 必须保留。空名噪声整条丢弃。
        kept = [tc for tc in tool_calls if isinstance(tc, dict) and not _is_blank_tool_call(tc)]
        if kept:
            return Event(kind=EventKind.TOOL_CALLS, tool_calls=kept)

    content = delta.get("content")
    if isinstance(content, str) and content:
        return Event(kind=EventKind.CONTENT, content=content)

    reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return Event(kind=EventKind.REASONING, content=reasoning)

    usage = payload.get("usage")
    if isinstance(usage, dict):
        return Event(kind=EventKind.USAGE, usage=_usage(usage))

    if isinstance(finish_reason, str) and finish_reason:
        return Event(kind=EventKind.FINISH, finish_reason=finish_reason)
    return None


def parse_all_events(frame: SSEFrame) -> list[Event]:
    """一帧拆成多个事件：带 usage 的收尾帧同时给出 finish_reason 与 usage。"""
    if not frame.data:
        return []
    if frame.data.strip() == "[DONE]":
        return []
    try:
        payload = json.loads(frame.data)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation("unparsable CodeBuddy SSE data") from error
    if not isinstance(payload, dict):
        raise UpstreamProtocolViolation("CodeBuddy SSE data is not an object")

    events: list[Event] = [e for e in (parse_frame(frame),) if e is not None]
    has_usage = any(e.kind is EventKind.USAGE for e in events)
    has_finish = any(e.kind is EventKind.FINISH for e in events)
    usage = payload.get("usage")
    if isinstance(usage, dict) and not has_usage:
        events.append(Event(kind=EventKind.USAGE, usage=_usage(usage)))
    choice = _first_choice(payload)
    finish_reason = choice.get("finish_reason") if choice else None
    if isinstance(finish_reason, str) and finish_reason and not has_finish:
        events.append(Event(kind=EventKind.FINISH, finish_reason=finish_reason))
    return events


def _first_choice(payload: dict[str, Any]) -> dict[str, Any] | None:
    choices = payload.get("choices")
    if choices is None:
        return None
    if not isinstance(choices, list):
        raise UpstreamProtocolViolation("choices is not an array")
    if not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        raise UpstreamProtocolViolation("choices[0] is not an object")
    return first


def _usage(raw: dict[str, Any]) -> Usage:
    def as_int(key: str) -> int | None:
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    credit = raw.get("credit")
    # 缓存命中：OpenAI 惯例在 prompt_tokens_details.cached_tokens，顶层 cached_tokens 兜底
    details = raw.get("prompt_tokens_details")
    cached = (details or {}).get("cached_tokens") if isinstance(details, dict) else None
    if not isinstance(cached, int) or isinstance(cached, bool):
        cached = as_int("cached_tokens")

    return Usage(
        input_tokens=as_int("prompt_tokens"),
        output_tokens=as_int("completion_tokens"),
        reasoning_tokens=as_int("reasoning_tokens"),
        cached_tokens=cached,
        credit=float(credit) if isinstance(credit, (int, float)) and not isinstance(credit, bool)
        else None,
    )


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码 + body 分类（1005 = 权益不足）。"""
    compact = body.decode("utf-8", errors="replace").replace(" ", "").lower()
    if '"code":1005' in compact or ("1005" in compact and "plan" in compact):
        return ErrKind.PLAN
    if status == 400:
        # 请求无效（如模型不存在）是客户端错误，冷却凭证只会误伤健康凭证
        return ErrKind.INVALID
    if status in (401, 403):
        return ErrKind.DEAD
    if status in (404, 429):
        return ErrKind.SOFT
    return ErrKind.OTHER


def classify_error_code(code: int | None) -> ErrKind:
    return ErrKind.PLAN if code == 1005 else ErrKind.OTHER
