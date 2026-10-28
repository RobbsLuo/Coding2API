"""按 API Key 聚合用量（Key 级配额与自助用量的唯一数据源）。

数据源是明细表 `usage_events` 的 `api_key_id` 列（B1 采集），按
`(api_key_id, ts)` 索引走范围扫描——这也是为什么不读 `usage_hourly`：
小时汇总的主键里没有 Key 维度，无法还原到单把 Key。

两个纪律：

- **只按 `api_key_id` 过滤**，不掺用户名：同一用户名下多把 Key 的配额互相
  独立；`api_key_id IS NULL` 的历史行（补列前/内部请求）不归属任何 Key。
- **失败请求也计入**：它们同样消耗了上游 token 与成本（与 `day_usage` 同口径）。
  另给 `ok` / `failed` 拆分供自助用量展示成功率。
"""

from __future__ import annotations

from ..key_quota import KeyUsage


class KeyUsageRepository:
    """`usage_events` 按 Key 的窗口聚合。"""

    def __init__(self, db) -> None:
        self._db = db

    def for_key(self, key_id: str, *, since: int,
                until: int | None = None) -> KeyUsage:
        """聚合某把 Key 在 `[since, until)` 内的用量；无记录返回全零。

        `until=None` 表示不设上界（「至今」）。`cost_usd` 为 NULL 的行按 0
        累加——不猜成本，也不让 NULL 把整行求和污染掉。
        """
        row = self._db.connect().execute(
            "SELECT COUNT(*) AS requests, "
            "COALESCE(SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END), 0) AS ok, "
            "COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failed, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd "
            "FROM usage_events WHERE api_key_id = ? AND ts >= ? "
            "AND (? IS NULL OR ts < ?)",
            (key_id, int(since), until, until)).fetchone()
        return _row_usage(row)

    def for_user(self, username: str, *, since: int,
                 until: int | None = None) -> KeyUsage:
        """聚合某个**用户**在 `[since, until)` 内的用量（用户级配额用）。

        与 `for_key` 的口径差别就是要按人而不是按 Key：该用户经由任何入口产生
        的行都算（自己 Key 的请求 + 管理台 Playground 的会话直连请求——后者的
        `api_key_id` 为 NULL）。所以这里不按 `api_key_id IS NOT NULL` 过滤，
        否则 Playground 自己的流量不吃自己那份配额，配额形同虚设。
        """
        row = self._db.connect().execute(
            "SELECT COUNT(*) AS requests, "
            "COALESCE(SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END), 0) AS ok, "
            "COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failed, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd "
            "FROM usage_events WHERE username = ? AND ts >= ? "
            "AND (? IS NULL OR ts < ?)",
            (username, int(since), until, until)).fetchone()
        return _row_usage(row)

    def map_for(self, key_ids: list[str], *, since: int) -> dict[str, KeyUsage]:
        """批量聚合（管理台列表「当日已用」一列）：一次查询，避免逐行 N+1。"""
        ids = [key_id for key_id in key_ids if key_id]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self._db.connect().execute(
            "SELECT api_key_id, COUNT(*) AS requests, "
            "COALESCE(SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END), 0) AS ok, "
            "COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failed, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd "
            f"FROM usage_events WHERE api_key_id IN ({placeholders}) AND ts >= ? "
            "GROUP BY api_key_id", (*ids, int(since))).fetchall()
        return {row["api_key_id"]: _row_usage(row) for row in rows}


def _row_usage(row) -> KeyUsage:
    return KeyUsage(
        requests=int(row["requests"] or 0),
        ok=int(row["ok"] or 0),
        failed=int(row["failed"] or 0),
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        cost_usd=float(row["cost_usd"] or 0.0),
    )
