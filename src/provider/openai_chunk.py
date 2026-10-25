"""标准 OpenAI chunk 的共享解析原语（多 provider 复用）。

Zen / Kilo 是标准 OpenAI SSE；CodeBuddy 与 Qoder 的内层 chunk 也是 OpenAI
形状。这几家对 `choices` 数组的取值校验与「空 tool_call 噪声」的判定完全
一致，抽到这里避免 4 份实现各自漂移——上游把 `choices` 换成非数组时，
四处都要同时报 `UpstreamProtocolViolation` 而非静默返回空回复。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..engine.sse import SSEFrame
from .base import Event, EventKind, UpstreamProtocolViolation, Usage

# 空 JSON 对象串（`arguments` 有时是未解析的原始字符串）；用拼接构造以避开
# 编辑期对 `` 字面量的意外改写。
_EMPTY_ARGUMENTS_JSON = "{" + "}"


def first_choice(payload: dict[str, Any]) -> dict[str, Any] | None:
    """取 `choices[0]`；无 choices 返回 None，结构非法显式失败。

    `choices` 缺失与 `choices: []` 都合法（usage / `[DONE]` 后帧常见），
    但非数组或首元素不是对象属于协议损坏，必须显式失败。
    """
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


def is_blank_tool_call(tc: dict[str, Any]) -> bool:
    """无 name 且 arguments 为空的噪声 tool_call。

    空 arguments 含 None、空串、空 JSON 对象串与已解析的空 dict。正常的分片
    tool_call 首块无 name 但带实际 arguments，必须保留；只有名与参都空
    （客户端聚合后显示 "Tool not found"）才是噪声。
    """
    function = tc.get("function")
    function = function if isinstance(function, dict) else {}
    if str(function.get("name") or "").strip():
        return False
    arguments = function.get("arguments")
    return arguments in (None, "", _EMPTY_ARGUMENTS_JSON) or arguments == {}


def _opt_int(value: Any) -> int | None:
    """整数取值：非 int / bool 一律 None（绝不把「未上报」量化成 0）。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def details_int(raw: dict[str, Any], details_key: str, key: str) -> int | None:
    """details 子对象优先，顶层同名字段兜底。

    OpenAI 惯例把 `cached_tokens` / `reasoning_tokens` 放在 details 里（未命中时
    报 0，是有效值而非缺失），顶层只是兼容形状。details 值非法（非数值 /
    bool）时回落顶层，仍缺则 None。
    """
    details = raw.get(details_key)
    value = details.get(key) if isinstance(details, dict) else None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _opt_int(raw.get(key))


def usage_from_openai(
    raw: dict[str, Any],
    *,
    credit: bool = False,
    cached_fallbacks: tuple[str, ...] = (),
) -> Usage:
    """标准 OpenAI `usage` 对象 → 中立 `Usage`。

    各渠道差异只有两处，参数化即可，不猜键：
    * `credit`：上游是否真给 credit 字段（Zen / CodeBuddy 有）；
    * `cached_fallbacks`：details 与顶层都缺时的额外候选键（TRAE 的
      `cache_read_input_tokens`）。

    `reasoning_tokens` / `cached_tokens` 一律 details 优先、顶层兜底：这是
    7 份原实现的**并集**——details 缺失时回落顶层，绝不丢顶层值。
    """
    cached = details_int(raw, "prompt_tokens_details", "cached_tokens")
    for key in cached_fallbacks:
        if cached is None:
            cached = _opt_int(raw.get(key))
    credit_value = raw.get("credit") if credit else None
    return Usage(
        input_tokens=_opt_int(raw.get("prompt_tokens")),
        output_tokens=_opt_int(raw.get("completion_tokens")),
        reasoning_tokens=details_int(raw, "completion_tokens_details", "reasoning_tokens"),
        cached_tokens=cached,
        credit=(float(credit_value)
                if isinstance(credit_value, (int, float))
                and not isinstance(credit_value, bool) else None),
    )


def payload_of(frame: SSEFrame, label: str) -> dict[str, Any] | None:
    """SSE 帧 → JSON 对象；空帧 / `[DONE]` 返回 None，坏帧显式失败。

    `label` 只进报错文案（便于定位是哪条渠道的帧坏了）。
    """
    if not frame.data:
        return None
    if frame.data.strip() == "[DONE]":
        return None
    try:
        payload = json.loads(frame.data)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation(f"unparsable {label} SSE data") from error
    if not isinstance(payload, dict):
        raise UpstreamProtocolViolation(f"{label} SSE data is not an object")
    return payload


def openai_event(
    payload: dict[str, Any],
    *,
    reasoning_keys: tuple[str, ...] = ("reasoning_content",),
    error_event: Callable[[dict[str, Any]], Event] | None = None,
    error_as_string: bool = False,
    usage: Callable[[dict[str, Any]], Usage] = usage_from_openai,
) -> Event | None:
    """已解析 OpenAI chunk → 代表性事件。

    优先级：tool_calls > 正文 > 思考 > 错误 > usage > finish。

    差异参数化，不猜键：
    * `reasoning_keys`：Zen / CodeBuddy / OfficeAce 用 `reasoning_content`，
      Kilo 用 `reasoning`。按顺序取第一个非空串。
    * `error_event`：错误信封的分类是各渠道私有码表，只注入回调。
    * `error_as_string`：OfficeAce 实测有 `{"error": "文本"}` 形态。
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

    for key in reasoning_keys:
        reasoning = delta.get(key)
        if isinstance(reasoning, str) and reasoning:
            return Event(kind=EventKind.REASONING, content=reasoning)

    if error_event is not None:
        error = payload.get("error")
        if isinstance(error, dict):
            return error_event(error)
        if error_as_string and isinstance(error, str) and error:
            return error_event({"message": error})

    usage_raw = payload.get("usage")
    if isinstance(usage_raw, dict):
        return Event(kind=EventKind.USAGE, usage=usage(usage_raw))

    if isinstance(finish_reason, str) and finish_reason:
        return Event(kind=EventKind.FINISH, finish_reason=finish_reason)
    return None


def openai_events(
    frame: SSEFrame,
    label: str,
    *,
    reasoning_keys: tuple[str, ...] = ("reasoning_content",),
    error_event: Callable[[dict[str, Any]], Event] | None = None,
    error_as_string: bool = False,
    usage: Callable[[dict[str, Any]], Usage] = usage_from_openai,
) -> list[Event]:
    """一帧拆成多个事件：同一 chunk 可同时携带正文与 finish_reason。

    必须基于代表性事件再补 usage/finish，否则「最后一段正文 + finish」
    这种合并帧会丢掉 finish（客户端永远收不到结束信号）。
    """
    payload = payload_of(frame, label)
    if payload is None:
        return []
    representative = openai_event(
        payload, reasoning_keys=reasoning_keys, error_event=error_event,
        error_as_string=error_as_string, usage=usage)
    events: list[Event] = [e for e in (representative,) if e is not None]
    has_usage = any(e.kind is EventKind.USAGE for e in events)
    has_finish = any(e.kind is EventKind.FINISH for e in events)

    usage_raw = payload.get("usage")
    if isinstance(usage_raw, dict) and not has_usage:
        events.append(Event(kind=EventKind.USAGE, usage=usage(usage_raw)))
    choice = first_choice(payload)
    finish_reason = choice.get("finish_reason") if choice else None
    if isinstance(finish_reason, str) and finish_reason and not has_finish:
        events.append(Event(kind=EventKind.FINISH, finish_reason=finish_reason))
    return events
