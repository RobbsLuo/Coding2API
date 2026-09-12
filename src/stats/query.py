"""用量统计查询（TECHNICAL §2 query.py）：overview / by-provider / timeline。"""

from __future__ import annotations

from typing import Any


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
                   COALESCE(SUM(CASE WHEN provider = 'codebuddy'
                               THEN requests ELSE 0 END), 0) AS codebuddy,
                   COALESCE(SUM(CASE WHEN provider = 'trae'
                               THEN requests ELSE 0 END), 0) AS trae
            FROM usage_hourly {where}
            GROUP BY hour_utc
            ORDER BY hour_utc
            """, params).fetchall()
        return [
            {"hour": row["hour_utc"], "codebuddy": row["codebuddy"], "trae": row["trae"]}
            for row in rows
        ]
