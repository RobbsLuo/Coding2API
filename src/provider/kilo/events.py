"""Kilo Gateway SSE → 中立事件映射。

Kilo 是标准 OpenAI 流式协议（`data: {chat.completion.chunk}`，`data: [DONE]`
结束），无需私有协议解析。实测要点（2026-09-29）：

* 正文在 `choices[0].delta.content`；**思考在 `delta.reasoning`**（注意不是
  Zen 的 `reasoning_content`），另带 `reasoning_details` 结构，忽略即可；
* `finish_reason` 可能与正文/思考同帧 → 必须用 `parse_all_events` 才能同时
  拿到两者；
* `usage` 在收尾帧给出（常与 `choices` 同帧），含
  `prompt_tokens_details.cached_tokens`；
* 上游在正文帧里塞了大量非标准字段（`provider`、`provider_metadata`、
  `native_finish_reason`、`cost` 等）——宽松解析，全部忽略；
* 错误信封两种形态：HTTP 非 2xx 的 `{"error": {...}}` / `{"error":"...",
  "error_type":"model_not_found"}`，以及流内的 `{"error": {...}}` 帧。

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
    payload = payload_of(frame, "Kilo")
    return None if payload is None else openai_event(
        payload, reasoning_keys=("reasoning",), error_event=_error_event,
        usage=_usage)


def parse_all_events(frame: SSEFrame) -> list[Event]:
    """一帧拆成多个事件：同一 chunk 可同时携带正文/思考与 finish_reason。"""
    return openai_events(frame, "Kilo", reasoning_keys=("reasoning",),
                         error_event=_error_event, usage=_usage)


def _error_event(error: dict[str, Any]) -> Event:
    """流内错误信封（OpenAI 兼容网关可能返回 `{"error": {...}}`）。

    `error.code` 可能是 int 也可能是字符串（如 `invalid_request_error`）；
    只有 int 才进 error_code，其余按消息分类。
    """
    raw_code = error.get("code")
    code = (raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool)
            else None)
    message = error.get("message")
    return Event(
        kind=EventKind.ERROR,
        error_code=code,
        error_message=str(message) if message is not None else "",
        error_kind=classify_error_code(code),
    )


def _usage(raw: dict[str, Any]) -> Usage:
    return usage_from_openai(raw)


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码分类。

    Kilo 是**无凭证的免费渠道**，拒绝多来自请求本身而非凭证：

    * 404 `model_not_found`（模型不存在 / 空模型名）与 400/422 → INVALID，
      跳过该渠道而不是冷却虚拟凭证；
    * 401 → INVALID：Kilo 匿名即可用免费模型，401 只说明该模型需要付费
      key（或 BYOK），归 DEAD 会因一次强制付费模型请求把整条渠道硬禁用；
    * 429 → MODEL：实测（2026-09-30）免费池是 OpenRouter 共享池转发，
      429 报错原文点名具体模型（`<model> is temporarily rate-limited
      upstream`，`limit_source: upstream_provider_shared_pool`），且同一
      时刻其他免费模型仍可用——是**模型级**限流而非渠道级。归账号级 SOFT
      会因单模型拥塞把整条 kilo 渠道冷却 60s（单虚拟凭证下即「all
      credentials unavailable」），违背「模型级限流不连累同账号其他模型」
      的两层冷却原则（对齐 CB/TRAE 的 `429+6004 → MODEL`）；
    * 502/503/504 → MODEL：上游 provider 瞬时不可用（实测 2026-09-30 同一
      模型 429 消退后转 503 `no endpoints available`，**同一时刻其他免费
      模型仍 200**，仍是模型级）。归 OTHER 会累计 3 次后冷却整条渠道 10m，
      与 429 同样的单凭证连累问题；故按模型级处理。
    * 403 → REQUEST（请求级，不罚号）。
    """
    if status == 401:
        return ErrKind.INVALID
    if status in (429, 502, 503, 504):
        return ErrKind.MODEL
    if status in (400, 404, 422):
        return ErrKind.INVALID
    if status == 403:
        return ErrKind.REQUEST
    return ErrKind.OTHER


def classify_error_code(code: int | None) -> ErrKind:
    """流内错误码 → 错误分类（仅 int 码；字符串码走 OTHER）。

    429 / 502/503/504 同 `classify_status`：模型级（限流 / 上游 provider
    瞬时不可用，实测都点名模型且不连累其他免费模型）。
    """
    if code == 401:
        return ErrKind.INVALID
    if code in (429, 502, 503, 504):
        return ErrKind.MODEL
    if code in (400, 404, 422):
        return ErrKind.INVALID
    if code == 403:
        return ErrKind.REQUEST
    return ErrKind.OTHER
