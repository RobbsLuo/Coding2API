"""OpenAI 请求校验（宽松：只校验本服务依赖的字段）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class InvalidRequest(ValueError):
    pass


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
    return ChatRequest(model=model or "", messages=messages, stream=stream, raw=dict(body))
