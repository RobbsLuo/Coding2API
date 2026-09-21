"""CodeBuddy SSE → 中立事件映射。

上游是 OpenAI 风格 SSE（data: {...choices[].delta...}），以 data: [DONE] 结束。
宽松解析：不在事件层做完整协议校验（AGENTS.md 约束），
但结构非法（JSON 坏、非对象、choices 非数组）必须显式失败而非静默。
"""

from __future__ import annotations

import json
from typing import Any

from ...engine.sse import SSEFrame
from ...provider.base import ErrKind, Event, EventKind, Usage, body_hint, business_codes


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


_MODEL_RATE_LIMIT_CODE = 6004       # 该模型用量超限 → 模型级软冷却
_MODEL_BLOCKED_CODE = 11102         # 该后端无此模型 → (账号, 模型) 负缓存
_CREDITS_EXHAUSTED_CODE = 14018     # 积分耗尽 → 冷却到次日签到
_PLAN_EXHAUSTED_CODE = 1005         # 权益/套餐耗尽
# 请求级错误（不是账号的问题）：请求体坏 / 上下文超限 / 图片无效
_REQUEST_LEVEL_CODES = frozenset({11101, 11115, 11135})
_BAD_PARAMS_PHRASE = "unmarshal chat params failed"


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码 + body 业务码分类。

    判定顺序即优先级，改动前先确认没把更具体的一类遮住（6004 与 429
    的先后就是一处：6004 只冷却触发模型，落进 SOFT 会误伤整个账号）：

    * 402 / 14018 → 余额不足（冷却到次日 04:00，等签到恢复）
    * 1005 → 权益耗尽（12h 长冷却）
    * 11102「该后端无此模型」→ (账号, 模型) 负缓存（400/404 形态）
    * 401/403 → session 失效（硬禁用）
    * 404 → 软冷却，不累计错误数
    * 429 + 6004 → 模型级限流；429 其他 → 账号级软限流
    * 400 + 11101/11115/11135 → 请求级错误：不罚号，但仍换号
    * 400 其他 → 请求无效（模型不存在等），不冷却凭证
    """
    codes = business_codes(body.decode("utf-8", errors="replace"))
    if status == 402 or _CREDITS_EXHAUSTED_CODE in codes:
        return ErrKind.CREDIT
    if _PLAN_EXHAUSTED_CODE in codes:
        return ErrKind.PLAN
    if status in (400, 404) and _MODEL_BLOCKED_CODE in codes:
        return ErrKind.BLOCKED
    if status in (401, 403):
        return ErrKind.DEAD
    if status == 404:
        return ErrKind.SOFT
    if status == 429:
        return ErrKind.MODEL if _MODEL_RATE_LIMIT_CODE in codes else ErrKind.SOFT
    if status == 400:
        if (codes & _REQUEST_LEVEL_CODES
                or _BAD_PARAMS_PHRASE in body_hint(body).lower()):
            # 发给上游的 body 有问题：换号也大概率一样，但不罚号（仍轮转试一次）
            return ErrKind.REQUEST
        # 请求无效（如模型不存在）是客户端错误，冷却凭证只会误伤健康凭证
        return ErrKind.INVALID
    return ErrKind.OTHER


def classify_error_code(code: int | None) -> ErrKind:
    """流内 error 事件的业务码（与 classify_status 同一套语义）。"""
    if code == _PLAN_EXHAUSTED_CODE:
        return ErrKind.PLAN
    if code == _CREDITS_EXHAUSTED_CODE:
        return ErrKind.CREDIT
    if code == _MODEL_RATE_LIMIT_CODE:
        return ErrKind.MODEL
    if code == _MODEL_BLOCKED_CODE:
        return ErrKind.BLOCKED
    if code in _REQUEST_LEVEL_CODES:
        return ErrKind.REQUEST
    return ErrKind.OTHER
