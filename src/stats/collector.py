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
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, "
                "ok, error_type, input_tokens, output_tokens, reasoning_tokens, cached_tokens, "
                "credit, latency_ms, ttfb_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event.id, event.ts, event.username, event.provider, event.credential_id,
                 event.model, int(event.ok), event.error_type, event.input_tokens,
                 event.output_tokens, event.reasoning_tokens, event.cached_tokens, event.credit,
                 event.latency_ms, event.ttfb_ms),
            )
            # 当前小时增量累加：总览/图表都读小时表，不能等 5 分钟一轮的
            # retention rollup 才可见（否则刚发生的请求统计页面显示 0）。
            # 增量累加与全量重算等价：rollup 后续会把这一小时算成同样的值。
            self._bump_hourly(conn, event)

    @staticmethod
    def _bump_hourly(conn, event: UsageEvent) -> None:
        """把一条明细增量累加进所属小时行（同 key 则累加，不存在则插入）。"""
        conn.execute(
            """
            INSERT INTO usage_hourly (hour_utc, username, provider, model, requests, ok_count,
                                      input_tokens, output_tokens, reasoning_tokens,
                                      cached_tokens, cached_known,
                                      credit_sum, credit_known, latency_sum, ttfb_sum)
            VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(hour_utc, username, provider, model) DO UPDATE SET
                requests = requests + 1,
                ok_count = ok_count + excluded.ok_count,
                input_tokens = input_tokens + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
                cached_tokens = cached_tokens + excluded.cached_tokens,
                cached_known = cached_known + excluded.cached_known,
                credit_sum = COALESCE(credit_sum, 0) + excluded.credit_sum,
                credit_known = credit_known + excluded.credit_known,
                latency_sum = latency_sum + excluded.latency_sum,
                ttfb_sum = ttfb_sum + excluded.ttfb_sum
            """,
            # NULL 一律折成 0 参与累加：列定义是 NOT NULL DEFAULT 0，
            # `x + NULL` 会交出 NULL，把整行污染掉
            ((event.ts // 3600) * 3600, event.username, event.provider, event.model,
             int(event.ok),
             event.input_tokens or 0, event.output_tokens or 0,
             event.reasoning_tokens or 0,
             event.cached_tokens or 0,
             0 if event.cached_tokens is None else 1,
             event.credit or 0.0,
             0 if event.credit is None else 1,
             (event.latency_ms or 0) if event.ok else 0,
             (event.ttfb_ms or 0) if event.ok else 0),
        )

    def purge_expired(self, retention_days: int = 90, now: int | None = None) -> int:
        """明细保留 90 天；小时汇总永久（PROPOSAL §8）。

        切点向上对齐到小时边界：只删**整个小时都已过期**的明细。
        这不是保守取值而是正确性要求——`rollup_hourly` 对整行用 REPLACE
        语义，只有保证「仍有明细的小时保有全部明细」，重算才精确；
        若把边界小时只删一半，下一轮 rollup 会把汇总行覆盖为剩下的那一半，
        永久丢掉被删部分（已过期明细无法再从明细找回）。
        代价：明细最多多留 1 小时。
        """
        raw_cutoff = int(now if now is not None else time.time()) - retention_days * 86400
        cutoff = (raw_cutoff // 3600 + 1) * 3600
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM usage_events WHERE ts < ?", (cutoff,))
        return cursor.rowcount

    def rollup_hourly(self, since: int | None = None) -> int:
        """把明细汇总进小时表（幂等 upsert）。

        默认汇总全部明细：小时汇总要永久保留，不能因为「只看最近 N 小时」
        而丢掉历史数据。需要增量时由调用方显式传 since。

        整行 REPLACE 语义要求「有明细的小时保有全部明细」——
        `purge_expired` 按整小时删除来保这个前提（见其 docstring）。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO usage_hourly (hour_utc, username, provider, model, requests, ok_count,
                                          input_tokens, output_tokens, reasoning_tokens,
                                          cached_tokens, cached_known,
                                          credit_sum, credit_known, latency_sum, ttfb_sum)
                SELECT (ts / 3600) * 3600 AS hour_utc, username, provider, model,
                       COUNT(*), SUM(ok),
                       COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(reasoning_tokens), 0),
                       COALESCE(SUM(cached_tokens), 0),
                       SUM(CASE WHEN cached_tokens IS NULL THEN 0 ELSE 1 END),
                       COALESCE(SUM(credit), 0), SUM(CASE WHEN credit IS NULL THEN 0 ELSE 1 END),
                       COALESCE(SUM(CASE WHEN ok = 1 THEN latency_ms END), 0),
                       COALESCE(SUM(CASE WHEN ok = 1 THEN ttfb_ms END), 0)
                FROM usage_events WHERE (? IS NULL OR ts >= ?)
                GROUP BY hour_utc, username, provider, model
                ON CONFLICT(hour_utc, username, provider, model) DO UPDATE SET
                    requests = excluded.requests,
                    ok_count = excluded.ok_count,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    reasoning_tokens = excluded.reasoning_tokens,
                    cached_tokens = excluded.cached_tokens,
                    cached_known = excluded.cached_known,
                    credit_sum = excluded.credit_sum,
                    credit_known = excluded.credit_known,
                    latency_sum = excluded.latency_sum,
                    ttfb_sum = excluded.ttfb_sum
                """, (since, since))
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

