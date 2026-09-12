"""脱敏统计采集与查询（Q24=A）。

纪律（PROPOSAL §8）：不存提示词、回答、请求头、Token、工具参数、原始错误体、会话 ID。
credit 为上游可选字段，两边都经常为 None。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

CONTROLLED_ERROR_TYPES = frozenset({
    # 实际会写入的取值（与 executor / api 层的 error_type 一一对应）：
    #   client_disconnect  — 客户端中途断开（executor.stream）
    #   credential_unavailable — session 失效（_error_type_for(DEAD)）
    #   invalid_request    — 请求无效（_record_invalid）
    #   no_healthy_credential — 无可用凭证（executor 耗尽轮换）
    #   rate_limit         — 权益耗尽（_error_type_for(PLAN)）
    #   upstream_error / upstream_protocol — 其它上游失败
    "client_disconnect", "credential_unavailable",
    "invalid_request", "no_healthy_credential", "rate_limit", "upstream_error",
    "upstream_protocol",
})
MAX_MODEL_LENGTH = 64


def _safe_model(model: Any) -> str:
    if not isinstance(model, str) or not model:
        return "unknown"
    if len(model) > MAX_MODEL_LENGTH:
        return "unknown"
    if any(character.isspace() or ord(character) < 0x20 for character in model):
        return "unknown"
    return model


def _normalize_error_type(value: Any) -> str | None:
    return value if isinstance(value, str) and value in CONTROLLED_ERROR_TYPES else None


@dataclass(slots=True)
class UsageEvent:
    id: str
    ts: int
    username: str
    provider: str
    credential_id: str | None
    model: str
    ok: bool
    error_type: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cached_tokens: int | None
    credit: float | None
    latency_ms: int | None
    ttfb_ms: int | None


class StatsCollector:
    def __init__(self, db) -> None:
        self._db = db

    def record(
        self,
        *,
        username: str,
        provider: str,
        model: str,
        ok: bool,
        credential_id: str | None = None,
        error_type: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cached_tokens: int | None = None,
        credit: float | None = None,
        latency_ms: int | None = None,
        ttfb_ms: int | None = None,
        now: int | None = None,
    ) -> None:
        """写入单条脱敏明细。失败不应影响聊天响应（调用方捕获）。"""
        event = UsageEvent(
            id=f"evt_{uuid.uuid4().hex[:16]}",
            ts=int(now if now is not None else time.time()),
            username=username or "unknown",
            provider=provider,
            credential_id=credential_id,
            model=_safe_model(model),
            ok=bool(ok),
            error_type=_normalize_error_type(error_type),
            input_tokens=_int_or_none(input_tokens),
            output_tokens=_int_or_none(output_tokens),
            reasoning_tokens=_int_or_none(reasoning_tokens),
            cached_tokens=_int_or_none(cached_tokens),
            credit=_float_or_none(credit),
            latency_ms=_int_or_none(latency_ms),
            ttfb_ms=_int_or_none(ttfb_ms),
        )
        conn = self._db.connect()
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, ok, "
            "error_type, input_tokens, output_tokens, reasoning_tokens, cached_tokens, credit, "
            "latency_ms, ttfb_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event.id, event.ts, event.username, event.provider, event.credential_id,
             event.model, int(event.ok), event.error_type, event.input_tokens,
             event.output_tokens, event.reasoning_tokens, event.cached_tokens, event.credit,
             event.latency_ms, event.ttfb_ms),
        )
        conn.commit()

    def purge_expired(self, retention_days: int = 90, now: int | None = None) -> int:
        """明细保留 90 天；小时汇总永久（PROPOSAL §8）。"""
        cutoff = int(now if now is not None else time.time()) - retention_days * 86400
        conn = self._db.connect()
        cursor = conn.execute("DELETE FROM usage_events WHERE ts < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount

    def rollup_hourly(self, since: int | None = None) -> int:
        """把明细汇总进小时表（幂等 upsert）。

        默认汇总全部明细：小时汇总要永久保留，不能因为「只看最近 N 小时」
        而丢掉历史数据。需要增量时由调用方显式传 since。
        """
        conn = self._db.connect()
        cursor = conn.execute(
            """
            INSERT INTO usage_hourly (hour_utc, username, provider, model, requests, ok_count,
                                      input_tokens, output_tokens, credit_sum, credit_known,
                                      latency_sum)
            SELECT (ts / 3600) * 3600 AS hour_utc, username, provider, model,
                   COUNT(*), SUM(ok),
                   COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                   SUM(credit), SUM(CASE WHEN credit IS NULL THEN 0 ELSE 1 END),
                   COALESCE(SUM(latency_ms), 0)
            FROM usage_events WHERE (? IS NULL OR ts >= ?)
            GROUP BY hour_utc, username, provider, model
            ON CONFLICT(hour_utc, username, provider, model) DO UPDATE SET
                requests = excluded.requests,
                ok_count = excluded.ok_count,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                credit_sum = excluded.credit_sum,
                credit_known = excluded.credit_known,
                latency_sum = excluded.latency_sum
            """, (since, since))
        conn.commit()
        return cursor.rowcount


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if numeric >= 0 else None

