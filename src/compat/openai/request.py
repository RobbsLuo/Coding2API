"""OpenAI 请求校验（宽松：只校验本服务依赖的字段）。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


class InvalidRequest(ValueError):
    pass


# 采样参数与输出上限的宽松但有限的边界：本服务只做透传，上界只为拦住
# 明显畸形的输入（NaN/Inf、负数、超大值），真正语义由上游裁决。
MAX_TEMPERATURE = 1000.0
MAX_OUTPUT_TOKENS = 4_000_000


def require_finite_number(value: Any, name: str) -> float:
    """数值字段校验：拒绝 bool、非数、NaN/Inf（M4）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequest(f"{name} must be a number")
    if not math.isfinite(value):
        raise InvalidRequest(f"{name} must be a finite number")
    return float(value)


def require_positive_int(value: Any, name: str, *, maximum: int) -> int:
    """正整数校验：拒绝 bool、非整数、≤0、超过上界（M4）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequest(f"{name} must be an integer")
    if value <= 0 or value > maximum:
        raise InvalidRequest(f"{name} must be between 1 and {maximum}")
    return value


@dataclass(slots=True)
class ChatRequest:
    model: str
    messages: list[dict[str, Any]]
    stream: bool
    raw: dict[str, Any]


def parse_chat_request(body: Any) -> ChatRequest:
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be a JSON object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidRequest("messages must be a non-empty array")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise InvalidRequest(f"messages[{index}] must be an object")
        if not isinstance(message.get("role"), str):
            raise InvalidRequest(f"messages[{index}].role must be a string")
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        raise InvalidRequest("model must be a string")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise InvalidRequest("stream must be a boolean")
    # 数值字段校验（M4）：NaN/Inf 由 read_json_body 拦在 JSON 层，但 `-1`/`true`
    # 这类合法 JSON 仍会透传到上游、污染凭证健康度与统计；与 Anthropic /
    # Responses 出口保持同一套边界。在 raw 副本上归一，不改动调用方 body。
    raw = dict(body)
    for key in ("temperature", "top_p"):
        if key in raw:
            value = require_finite_number(raw[key], key)
            if key == "temperature" and value > MAX_TEMPERATURE:
                raise InvalidRequest(f"temperature must be <= {MAX_TEMPERATURE}")
            raw[key] = value
    for key in ("max_tokens", "max_completion_tokens"):
        if key in raw and raw[key] is not None:
            require_positive_int(raw[key], key, maximum=MAX_OUTPUT_TOKENS)
    return ChatRequest(model=model or "", messages=messages, stream=stream, raw=raw)
