"""小浣熊 SSE → 中立事件映射 + 错误分类。

协议是标准 OpenAI 流式（`data: {chat.completion.chunk}`、`data: [DONE]`），
共享 `provider/openai_chunk.py` 的解析原语即可。实测补充（2026-11-04）：

* 正文 `delta.content`，思考 `delta.reasoning_content`（`sn-glm-5-3` 实测），
  同一帧可能同时带正文与 `finish_reason` → 用 `openai_events`；
* `usage` 只在带了 `stream_options.include_usage` 的末帧出现（没有就完全没有）；
* 每个 chunk 都带 `model` 回显——本渠道用它防「未知模型静默回落默认模型」；
* 错误信封是 `{"error": {"code": "...", "message": "..."}}`，`code` 是**字符串**
  （`user_model_rate_limited`），不是数字。

分类口径（都有真机证据）：

| 现象 | 分类 | 理由 |
|---|---|---|
| 401 / 403 + 身份校验失败 | `DEAD` | 会话 JWT 过期或被撤销，只能重新登录后重贴 |
| 402 / 余额不足（points、积分、insufficient、balance） | `CREDIT` | 等次日积分发放，不是凭证坏了 |
| 429 + `user_model_rate_limited` | `CONCURRENCY` | 请求频率超限（并发约 5），排空即恢复 |
| 429 其他 | `SOFT` | 账号级短限流 |
| 400 / 404 / 422 | `INVALID` | 请求或模型名不对，换号无用 |
| 5xx | `OTHER` | 上游故障，累计 3 次冷却 |
"""

from __future__ import annotations

from typing import Any

from ...engine.sse import SSEFrame
from ...provider.base import (
    ErrKind,
    Event,
    EventKind,
    UpstreamProtocolViolation,  # noqa: F401 - 测试经本模块访问
    Usage,
)
from ..openai_chunk import openai_events, usage_from_openai

#: 429 的并发限流标记（实测 `{"error":{"code":"user_model_rate_limited"}}`）
_RATE_LIMIT_MARKERS = ("user_model_rate_limited", "请求频率超限", "rate_limit")
#: 身份校验失败标记：会话 JWT 不可用
_IDENTITY_MARKERS = ("identity could not be verified", "invalid token",
                     "unauthorized", "token authentication failed")
#: 余额/积分不足标记
_CREDIT_MARKERS = ("insufficient", "balance", "points", "积分", "余额")


def parse_frame(frame: SSEFrame) -> list[Event]:
    """一帧 → 若干中立事件（空帧 / `[DONE]` → 空表）。"""
    return openai_events(frame, "Raccoon",
                         reasoning_keys=("reasoning_content", "reasoning"),
                         error_event=_error_event, usage=_usage)


def _error_event(error: dict[str, Any]) -> Event:
    code = error.get("code")
    message = error.get("message")
    return Event(
        kind=EventKind.ERROR,
        error_message=str(message) if message is not None else "",
        error_kind=classify_error(body=str(code or "") + " " + str(message or "")),
    )


def _usage(raw: dict[str, Any]) -> Usage:
    return usage_from_openai(raw)


def _lower(body: bytes | str) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace").lower()
    return body.lower()


def classify_error(body: bytes | str) -> ErrKind:
    """流内错误信封 → 分类（按文本判据，`code` 是字符串）。"""
    text = _lower(body)
    if any(marker in text for marker in _IDENTITY_MARKERS):
        return ErrKind.DEAD
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return ErrKind.CONCURRENCY
    if any(marker in text for marker in _CREDIT_MARKERS):
        return ErrKind.CREDIT
    return ErrKind.OTHER


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码 + 响应体 → 分类。

    先按语义标记判（429 的并发限流、403 的身份失败、余额不足三类都在正常
    状态码里出现），再按裸状态码兜底。
    """
    if status in (401, 403):
        # 401 一定是凭据问题；403 只有点到「身份校验」才算凭据问题（否则可能是
        # 风控/权限，按请求级处理，不硬禁用一条好凭证）
        if status == 401 or any(m in _lower(body) for m in _IDENTITY_MARKERS):
            return ErrKind.DEAD
        return ErrKind.REQUEST
    if status == 402:
        return ErrKind.CREDIT
    if status == 429:
        if any(marker in _lower(body) for marker in _RATE_LIMIT_MARKERS):
            return ErrKind.CONCURRENCY
        return ErrKind.SOFT
    if status in (400, 404, 422):
        return ErrKind.INVALID
    if status in (502, 503, 504):
        return ErrKind.OTHER
    return ErrKind.OTHER