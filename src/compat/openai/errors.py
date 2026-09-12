"""OpenAI 兼容错误形状（error_payload）与流内错误异常（TECHNICAL §2）。"""

from __future__ import annotations

from typing import Any

from ...provider.base import Event


class UpstreamStreamError(Exception):
    """流内业务错误（聚合路径抛出，由 executor 转成冷却 + 轮换）。"""

    def __init__(self, event: Event) -> None:
        super().__init__(event.error_message or "upstream stream error")
        self.event = event


def error_payload(message: str, code: str, status: int) -> dict[str, Any]:
    return {"error": {"message": message, "type": "api_error", "code": code,
                      "status": status}}
