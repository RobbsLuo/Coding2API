"""用量统计查询（TECHNICAL §2 query.py）：overview / by-provider / timeline / events。"""

from __future__ import annotations

from typing import Any

METRIC_COLUMNS: dict[str, str] = {
    # metric 参数 → usage_hourly 上的取值表达式
    "requests": "requests",
    "tokens": "input_tokens + output_tokens",
    "latency": "latency_sum",      # 均值由调用方除以 ok_count
    "ttfb": "ttfb_sum",            # 同上
}


class StatsQuery:
    def __init__(self, db) -> None:
        self._db = db

    def _metric_sql(self, metric: str) -> tuple[str, bool]:
        """返回 (取值表达式, 是否均值语义)。非法 metric 回退 requests。"""
        expr = METRIC_COLUMNS.get(metric, "requests")
        return expr, metric in ("latency", "ttfb")

    def _metric_value(self, metric: str, expr_value, ok_count) -> Any:
        """把 SQL 原始值按 metric 归一：均值类除以 ok_count，无成功则 0。"""
        if metric in ("latency", "ttfb"):
            return round(expr_value / ok_count) if ok_count else 0
        return expr_value

    @staticmethod
    def _where(*, username: str | None = None, provider: str | None = None,
               since: int | None = None, since_col: str = "ts",
               before: int | None = None, before_col: str = "rowid",
               alias: str = "") -> tuple[str, list[Any]]:
        """拼 WHERE 子句与参数。alias 为 JOIN 时的表前缀（如 "e."）。"""
        clauses: list[str] = []
        params: list[Any] = []
        if username is not None:
            clauses.append(f"{alias}username = ?")
            params.append(username)
        if provider is not None:
            clauses.append(f"{alias}provider = ?")
            params.append(provider)
        if since is not None:
            clauses.append(f"{alias}{since_col} >= ?")
            params.append(since)
        if before is not None:
            clauses.append(f"{alias}{before_col} < ?")
            params.append(before)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def overview(self, *, username: str | None = None, provider: str | None = None,
                 since: int | None = None) -> dict[str, Any]:
        """总览。admin 传 username=None 看全局；普通用户只看自己。

        数据源是 `usage_hourly`（与 timeline/model-timeline 同源）：
        明细只留 90 天，读明细会让选「全部」时总览小于图表；小时汇总永久保留。
        代价：最近 ≤5 分钟未进汇总的请求不计入（retention 每 5 分钟 rollup），
        刷新一次即可。延迟类均值统一按 `ok_count` 归一（与图表同口径）。
        """
        where, params = self._where(username=username, provider=provider,
                                    since=since, since_col="hour_utc")

        row = self._db.connect().execute(
            f"""
            SELECT COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(ok_count), 0) AS ok_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(cached_known) AS cached_known,
                   SUM(credit_sum) AS credit_sum,
                   SUM(credit_known) AS credit_known,
                   SUM(latency_sum) AS latency_sum,
                   SUM(ttfb_sum) AS ttfb_sum
            FROM usage_hourly {where}
            """, params).fetchone()
        requests = row["requests"] or 0
        ok_count = row["ok_count"] or 0
        # 均值口径：只除成功请求（失败请求的 latency 会拉偏"典型耗时"，
        # 且与图表 latency_sum/ok_count 同一致），无成功则 None
        return {
            "requests": requests,
            "ok_count": ok_count,
            "success_rate": (ok_count / requests) if requests else None,
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
            # 缓存命中：任一明细上报过才可信，否则 None（前端显示 —）
            "cached_tokens": row["cached_tokens"] if row["cached_known"] else None,
            "credit": row["credit_sum"] if row["credit_known"] else None,
            "avg_latency_ms": round(row["latency_sum"] / ok_count) if ok_count else None,
            "avg_ttfb_ms": round(row["ttfb_sum"] / ok_count) if ok_count else None,
        }

    def by_provider(self, *, username: str | None = None,
                    since: int | None = None) -> list[dict[str, Any]]:
        """按渠道聚合（同样读小时汇总，与总览/图表同源）。"""
        where, params = self._where(username=username, since=since, since_col="hour_utc")
        rows = self._db.connect().execute(
            f"""
            SELECT provider, COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(ok_count), 0) AS ok_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   SUM(credit_sum) AS credit_sum
            FROM usage_hourly {where} GROUP BY provider ORDER BY provider
            """, params).fetchall()
        return [
            {"provider": row["provider"], "requests": row["requests"],
             "ok_count": row["ok_count"],
             "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
             "credit": row["credit_sum"]}
            for row in rows
        ]

    def timeline(self, *, username: str | None = None,
                 since: int | None = None, metric: str = "requests") -> list[dict[str, Any]]:
        """按小时的指标时间序列（usage_hourly 聚合，跨 model 汇总）。

        每点包含 codebuddy / trae 两个渠道的指标值（按 metric 变化：
        请求数 / 总 token / 平均耗时 ms / 平均首字延迟 ms），供前端绘制曲线。
        返回结构 {hour, codebuddy, trae} 恒定，指标语义随 metric 参数切换。
        """
        expr, as_mean = self._metric_sql(metric)
        where, params = self._where(username=username, since=since, since_col="hour_utc")
        rows = self._db.connect().execute(
            f"""
            SELECT hour_utc,
                   COALESCE(SUM(CASE WHEN provider = 'codebuddy' THEN {expr} ELSE 0 END), 0)
                   AS codebuddy,
                   COALESCE(SUM(CASE WHEN provider = 'trae' THEN {expr} ELSE 0 END), 0)
                   AS trae,
                   COALESCE(SUM(CASE WHEN provider = 'codebuddy' THEN ok_count ELSE 0 END), 0)
                   AS codebuddy_ok,
                   COALESCE(SUM(CASE WHEN provider = 'trae' THEN ok_count ELSE 0 END), 0)
                   AS trae_ok
            FROM usage_hourly {where}
            GROUP BY hour_utc
            ORDER BY hour_utc
            """, params).fetchall()
        return [
            {"hour": row["hour_utc"],
             "codebuddy": self._metric_value(metric, row["codebuddy"], row["codebuddy_ok"]),
             "trae": self._metric_value(metric, row["trae"], row["trae_ok"])}
            for row in rows
        ]

    def events(self, *, username: str | None = None, since: int | None = None,
               before: int | None = None, limit: int = 50) -> dict[str, Any]:
        """逐请求明细（新→旧，rowid 游标分页）。明细仅保留 90 天。

        rowid 即插入序，稳定且可比大小，游标翻页不漏不重；
        返回 next_before 供下一页取「rowid 更小」的记录，null 表示到底。
        """
        where, params = self._where(username=username, since=since, before=before,
                                    alias="e.")
        rows = self._db.connect().execute(
            f"""
            SELECT e.rowid, e.ts, e.username, e.provider, e.credential_id, e.model,
                   e.ok, e.error_type, e.input_tokens, e.output_tokens,
                   e.reasoning_tokens, e.cached_tokens, e.credit, e.latency_ms,
                   e.ttfb_ms, c.nickname AS credential_name
            FROM usage_events e
            LEFT JOIN credentials c ON c.id = e.credential_id
            {where}
            ORDER BY e.rowid DESC LIMIT ?
            """, [*params, limit + 1]).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        return {
            "events": [dict(row) for row in page],
            "next_before": page[-1]["rowid"] if has_more and page else None,
        }

    def model_timeline(self, *, username: str | None = None,
                       since: int | None = None, top: int = 6,
                       metric: str = "requests") -> dict[str, Any]:
        """按小时的各模型指标趋势（usage_hourly 聚合）。

        Top N 模型按「请求数」排序（排序口径固定，与图表指标解耦）；
        曲线值按 metric 变化（请求数 / 总 token / 平均耗时 / 平均首字延迟）。
        返回结构 {models, points} 不变。
        """
        expr, as_mean = self._metric_sql(metric)
        where, params = self._where(username=username, since=since, since_col="hour_utc")
        rows = self._db.connect().execute(
            f"""
            SELECT hour_utc, model, SUM(requests) AS requests,
                   SUM({expr}) AS value, SUM(ok_count) AS ok_count
            FROM usage_hourly {where}
            GROUP BY hour_utc, model
            ORDER BY hour_utc
            """, params).fetchall()
        totals: dict[str, int] = {}
        series: dict[str, dict[int, int]] = {}
        hours: list[int] = []
        for row in rows:
            hour, model = row["hour_utc"], row["model"]
            if not hours or hours[-1] != hour:
                hours.append(hour)
            totals[model] = totals.get(model, 0) + row["requests"]
            series.setdefault(model, {})[hour] = (
                self._metric_value(metric, row["value"], row["ok_count"]))
        # Top N 模型：按总量降序；总量相同按名字稳定排序
        top_models = sorted(totals, key=lambda m: (-totals[m], m))[:top]
        points = [
            {"hour": hour, **{m: series.get(m, {}).get(hour, 0) for m in top_models}}
            for hour in hours
        ]
        return {"models": top_models, "points": points}
