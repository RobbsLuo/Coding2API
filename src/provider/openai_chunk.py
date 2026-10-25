"""标准 OpenAI chunk 的共享解析原语（多 provider 复用）。

Zen / Kilo 是标准 OpenAI SSE；CodeBuddy 与 Qoder 的内层 chunk 也是 OpenAI
形状。这几家对 `choices` 数组的取值校验与「空 tool_call 噪声」的判定完全
一致，抽到这里避免 4 份实现各自漂移——上游把 `choices` 换成非数组时，
四处都要同时报 `UpstreamProtocolViolation` 而非静默返回空回复。
"""

from __future__ import annotations

from typing import Any

from .base import UpstreamProtocolViolation, Usage

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
