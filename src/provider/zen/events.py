"""OpenCode Zen SSE → 中立事件映射。

Zen 是标准 OpenAI 流式协议（`data: {chat.completion.chunk}`，`data: [DONE]`
结束），无需私有协议解析。实测要点（2026-09-29）：

* 正文在 `choices[0].delta.content`，思考在 `delta.reasoning_content`；
* `finish_reason` 可能与最后一段正文同帧（同一 chunk 里既有 content 又有
  finish_reason）→ 必须用 `parse_all_events` 才能同时拿到两者；
* `usage` 在收尾的独立帧里给出（`choices: []`），含
  `prompt_tokens_details.cached_tokens`；
* `[DONE]` 之后还有一帧 `{"choices": [], "cost": "0"}`，无下游语义，跳过。

解析宽松：不做完整协议校验，但结构非法（JSON 坏 / 非对象 / choices 非数组）
必须显式失败而非静默，否则会变成「上游坏了但客户端收到空回复」。
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
from ..openai_chunk import openai_event, openai_events, payload_of, usage_from_openai


def parse_frame(frame: SSEFrame) -> Event | None:
    """单帧 → 单个代表性事件；`[DONE]` 与无内容帧返回 None。"""
    payload = payload_of(frame, "Zen")
    return None if payload is None else openai_event(
        payload, error_event=_error_event, usage=_usage)


def parse_all_events(frame: SSEFrame) -> list[Event]:
    """一帧拆成多个事件：同一 chunk 可同时携带正文与 finish_reason。"""
    return openai_events(frame, "Zen", error_event=_error_event, usage=_usage)


def _error_event(error: dict[str, Any]) -> Event:
    """流内错误信封（OpenAI 兼容网关可能返回 `{"error": {...}}`）。

    `error.code` 可能是 int 也可能是字符串（如 `invalid_request_error`）；
    只有 int 才进 error_code，其余按消息分类。
    """
    raw_code = error.get("code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    message = error.get("message")
    return Event(
        kind=EventKind.ERROR,
        error_code=code,
        error_message=str(message) if message is not None else "",
        error_kind=classify_error_code(code),
    )


def _usage(raw: dict[str, Any]) -> Usage:
    return usage_from_openai(raw, credit=True)


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码分类。

    Zen 免费层的拒绝来自**请求形状**（UA 版本过低 426 / 门禁 403），不是
    凭证问题；把它归成 DEAD 会把无凭证渠道直接打停，归成 REQUEST 则只
    轮换不惩罚。400 表示模型不可用（如已下线的 free 模型）→ INVALID，
    跳过该渠道而不是冷却虚拟凭证。
    401（`Missing API key.`）同理：Zen 无凭证，401 只说明**这个模型需要
    付费 key**，不是虚拟凭证失效——归 DEAD 会因一次付费模型请求把整条
    zen 渠道硬禁用，故同样按 INVALID 跳过。
    """
    if status == 401:
        return ErrKind.INVALID
    if status == 429:
        return ErrKind.SOFT
    if status in (400, 404, 422):
        return ErrKind.INVALID
    if status == 403:
        return ErrKind.REQUEST
    return ErrKind.OTHER


def classify_error_code(code: int | None) -> ErrKind:
    """流内错误码 → 错误分类（仅 int 码；字符串码走 OTHER）。"""
    if code == 401:
        return ErrKind.INVALID
    if code == 429:
        return ErrKind.SOFT
    if code in (400, 404, 422):
        return ErrKind.INVALID
    if code == 403:
        return ErrKind.REQUEST
    return ErrKind.OTHER
