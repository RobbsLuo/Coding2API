"""标准 OpenAI chunk 的共享解析原语（多 provider 复用）。

Zen / Kilo 是标准 OpenAI SSE；CodeBuddy 与 Qoder 的内层 chunk 也是 OpenAI
形状。这几家对 `choices` 数组的取值校验与「空 tool_call 噪声」的判定完全
一致，抽到这里避免 4 份实现各自漂移——上游把 `choices` 换成非数组时，
四处都要同时报 `UpstreamProtocolViolation` 而非静默返回空回复。
"""

from __future__ import annotations

from typing import Any

from .base import UpstreamProtocolViolation

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
