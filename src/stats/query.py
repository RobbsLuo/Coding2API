"""用量统计查询（TECHNICAL §2 query.py）：overview / by-provider / timeline / events。"""

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
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(CASE WHEN cached_tokens IS NULL THEN 0 ELSE 1 END) AS cached_known,
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
            # 缓存命中：任一明细上报过才可信，否则 None（前端显示 —）
            "cached_tokens": row["cached_tokens"] if row["cached_known"] else None,
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

    def events(self, *, username: str | None = None, since: int | None = None,
               before: int | None = None, limit: int = 50) -> dict[str, Any]:
        """逐请求明细（新→旧，rowid 游标分页）。明细仅保留 90 天。

        rowid 即插入序，稳定且可比大小，游标翻页不漏不重；
        返回 next_before 供下一页取「rowid 更小」的记录，null 表示到底。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if username is not None:
            clauses.append("username = ?")
            params.append(username)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if before is not None:
            clauses.append("rowid < ?")
            params.append(before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.connect().execute(
            f"""
            SELECT rowid, ts, username, provider, model, ok, error_type,
                   input_tokens, output_tokens, reasoning_tokens, cached_tokens,
                   credit, latency_ms
            FROM usage_events {where}
            ORDER BY rowid DESC LIMIT ?
            """, [*params, limit + 1]).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        return {
            "events": [dict(row) for row in page],
            "next_before": page[-1]["rowid"] if has_more and page else None,
        }

    def model_timeline(self, *, username: str | None = None,
                       since: int | None = None, top: int = 6) -> dict[str, Any]:
        """按小时的各模型请求数趋势（usage_hourly 聚合）。

        取时间范围内请求量 Top N 的模型，返回宽表点列（每点含各模型键），
        供前端绘制多曲线。模型过多时曲线不可读，非 Top N 不单独出线。
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
            SELECT hour_utc, model, SUM(requests) AS requests
            FROM usage_hourly {where}
            GROUP BY hour_utc, model
            ORDER BY hour_utc
            """, params).fetchall()
        totals: dict[str, int] = {}
        series: dict[str, dict[int, int]] = {}
        hours: list[int] = []
        for row in rows:
            hour, model, requests = row["hour_utc"], row["model"], row["requests"]
            if not hours or hours[-1] != hour:
                hours.append(hour)
            totals[model] = totals.get(model, 0) + requests
            series.setdefault(model, {})[hour] = requests
        # Top N 模型：按总量降序；总量相同按名字稳定排序
        top_models = sorted(totals, key=lambda m: (-totals[m], m))[:top]
        points = [
            {"hour": hour, **{m: series.get(m, {}).get(hour, 0) for m in top_models}}
            for hour in hours
        ]
        return {"models": top_models, "points": points}
