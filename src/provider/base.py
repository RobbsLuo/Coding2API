"""Provider 协议与中立类型（Q16=A 细接口，Q13=B 中立事件层预留）。

Provider 承担上游协议私有部分：发请求、解析事件、分类错误、凭证生命周期与额度探测。
调度、冷却、重试、统计在共享引擎（engine/）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class EventKind(StrEnum):
    CONTENT = "content"
    REASONING = "reasoning"
    TOOL_CALLS = "tool_calls"
    USAGE = "usage"
    FINISH = "finish"
    ERROR = "error"


class ErrKind(StrEnum):
    """错误分类 → 冷却时长（Q12=B）。"""

    PLAN = "plan"      # 权益耗尽 → 12h
    SOFT = "soft"      # 限流/404 → 60s，不累计错误数
    DEAD = "dead"      # session 失效 → 硬禁用
    OTHER = "other"    # 其他 4xx/5xx → 累计，连续 3 次 → 10m
    INVALID = "invalid"  # 请求无效（如模型不存在）→ 不冷却凭证，直接 400 回客户端


@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    credit: float | None = None   # 上游可选字段，两边都经常为 None


@dataclass(slots=True)
class Event:
    kind: EventKind
    content: str | None = None
    tool_calls: list[dict] | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error_code: int | None = None
    error_message: str | None = None


@dataclass(slots=True)
class Quota:
    """额度探测结果。probe_failed 与「探测到 0」必须区分（Q26 三态）。"""

    remaining: float | None = None
    total: float | None = None
    cycle_end: int | None = None      # CB 有周期；TRAE 为 None
    probed_at: int | None = None
    probe_failed: bool = False


UNKNOWN: int | None = None
EXHAUSTED = -1
"""HealthScore：0-100 已知；None 未知；-1 已耗尽。"""


def health_score(quota: Quota | None) -> int | None:
    """三态健康度：known(0-100) / unknown(None) / exhausted(-1)。"""
    if quota is None or quota.probe_failed:
        return UNKNOWN
    if quota.total is None or quota.total <= 0:
        return EXHAUSTED
    remaining = quota.remaining or 0.0
    return max(0, min(100, round(remaining / quota.total * 100)))


@dataclass(slots=True)
class CheckinResult:
    """签到结果。already_checked_in 表示当日已签（不算错误）。"""

    ok: bool
    credit: float | None = None
    code: int | None = None
    message: str = ""
    already_checked_in: bool = False


@dataclass(slots=True)
class Model:
    id: str
    name: str = ""


@dataclass(slots=True)
class AuthSession:
    """flow=poll 用 auth_url/interval；flow=callback 用 callback_url。Q17=C 双轨。"""

    flow: str                       # "poll" | "callback"
    state: str
    auth_url: str | None = None
    interval: int | None = None
    callback_url: str | None = None


@dataclass(slots=True)
class AuthResult:
    credential_data: dict
    nickname: str = ""


@runtime_checkable
class Provider(Protocol):
    id: str

    def start_auth(self) -> AuthSession: ...
    def poll_auth(self, state: str) -> AuthResult | None: ...
    def complete_callback(self, url: str) -> AuthResult: ...
    def import_credential(self, raw: dict) -> dict: ...
    def refresh(self, credential_data: dict) -> dict: ...
    async def probe_quota(self, credential_data: dict) -> Quota: ...
    def classify(self, status: int, body: bytes) -> ErrKind: ...
    def list_models(self, credential_data: dict) -> list[Model]: ...
