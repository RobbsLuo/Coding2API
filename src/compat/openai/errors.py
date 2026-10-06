"""OpenAI 兼容错误形状（error_payload）与流内错误异常（TECHNICAL §2）。"""

from __future__ import annotations

import json
from typing import Any

from ...engine.sse import SSE_DONE, format_openai_frame
from ...provider.base import Event


class UpstreamStreamError(Exception):
    """流内业务错误（聚合路径抛出，由 executor 转成冷却 + 轮换）。"""

    def __init__(self, event: Event) -> None:
        super().__init__(event.error_message or "upstream stream error")
        self.event = event


def error_payload(message: str, code: str, status: int) -> dict[str, Any]:
    return {"error": {"message": message, "type": "api_error", "code": code,
                      "status": status}}


def stream_error_frame(message: str, code: str) -> bytes:
    """OpenAI 兼容的流内错误帧：`data: {"error": ...}` + `[DONE]`。

    chat 出口（`compat/openai/response.py`）与执行层（`engine/executor.py`
    的兜底路径）共用同一形状，放这里避免两处各写一份 JSON 拼装而漂移。
    """
    payload = {"error": {"message": message, "type": "api_error", "code": code}}
    return format_openai_frame(json.dumps(payload, ensure_ascii=False)) + SSE_DONE
