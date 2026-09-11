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
    "authentication_error", "client_disconnect", "credential_unavailable",
    "invalid_request", "no_healthy_credential", "rate_limit", "upstream_error",
    "upstream_protocol", "internal_error",
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
            credit=_float_or_none(credit),
            latency_ms=_int_or_none(latency_ms),
            ttfb_ms=_int_or_none(ttfb_ms),
        )
        conn = self._db.connect()
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, ok, "
            "error_type, input_tokens, output_tokens, reasoning_tokens, credit, latency_ms, "
            "ttfb_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event.id, event.ts, event.username, event.provider, event.credential_id,
             event.model, int(event.ok), event.error_type, event.input_tokens,
             event.output_tokens, event.reasoning_tokens, event.credit, event.latency_ms,
             event.ttfb_ms),
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


class StatsQuery:
    def __init__(self, db) -> None:
        self._db = db

    def overview(self, *, username: str | None = None, provider: str | None = None,
                 since: int | None = None) -> dict[str, Any]:
        """总览。admin 传 username=None 看全局；普通用户只看自己。"""
        clauses: list[str] = []
        params: list[Any] = []
        if username is not None:
            clauses.append("username = ?")
            params.append(username)
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        row = self._db.connect().execute(
            f"""
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(ok), 0) AS ok_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                   SUM(credit) AS credit_sum,
                   SUM(CASE WHEN credit IS NULL THEN 0 ELSE 1 END) AS credit_known,
                   COALESCE(AVG(latency_ms), 0) AS avg_latency,
                   COALESCE(AVG(ttfb_ms), 0) AS avg_ttfb
            FROM usage_events {where}
            """, params).fetchone()
        requests = row["requests"] or 0
        return {
            "requests": requests,
            "ok_count": row["ok_count"],
            "success_rate": (row["ok_count"] / requests) if requests else None,
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
            "credit": row["credit_sum"] if row["credit_known"] else None,
            "avg_latency_ms": round(row["avg_latency"]) if requests else None,
            "avg_ttfb_ms": round(row["avg_ttfb"]) if requests else None,
        }

    def by_provider(self, *, username: str | None = None,
                    since: int | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if username is not None:
            clauses.append("username = ?")
            params.append(username)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.connect().execute(
            f"""
            SELECT provider, COUNT(*) AS requests, COALESCE(SUM(ok), 0) AS ok_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens, SUM(credit) AS credit_sum
            FROM usage_events {where} GROUP BY provider ORDER BY provider
            """, params).fetchall()
        return [
            {"provider": row["provider"], "requests": row["requests"],
             "ok_count": row["ok_count"],
             "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
             "credit": row["credit_sum"]}
            for row in rows
        ]

    def timeline(self, *, username: str | None = None,
                 since: int | None = None) -> list[dict[str, Any]]:
        """按小时的请求量时间序列（usage_hourly 聚合，跨 model 汇总）。

        每点包含 codebuddy / trae 两个上游的请求数，供前端绘制曲线。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if username is not None:
            clauses.append("username = ?")
            params.append(username)
        if since is not None:
            clauses.append("hour_utc >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.connect().execute(
            f"""
            SELECT hour_utc,
                   COALESCE(SUM(CASE WHEN provider = 'codebuddy' THEN requests ELSE 0 END), 0) AS codebuddy,
                   COALESCE(SUM(CASE WHEN provider = 'trae' THEN requests ELSE 0 END), 0) AS trae
            FROM usage_hourly {where}
            GROUP BY hour_utc
            ORDER BY hour_utc
            """, params).fetchall()
        return [
            {"hour": row["hour_utc"], "codebuddy": row["codebuddy"], "trae": row["trae"]}
            for row in rows
        ]
