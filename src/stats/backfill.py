"""历史明细的成本回填 / 重算（一次性，见 `scripts/backfill_cost.py`）。

成本在**写入明细那一刻**按当时的价表与汇率定值落库（`usage_events.cost_usd` /
`cost_cny`，见 `pricing` / `collector`）；改动前已落库的历史明细两列是 NULL，
统计页显示 `—`。本模块用**当前**价表与汇率把历史明细一次性补齐 / 重算，让成本
覆盖全部时间范围；之后新请求走正常写入路径，不再需要回填。

与 credit 回填（`provider.trae.backfill`）同一心智：

- 只覆盖「能定价」的行（模型已收录且上报了输入 token）。不能定价的行**保持
  原样**（历史 NULL 仍是 NULL，展示层显示 `—`），绝不拿 0 冒充免费；
- 值未变化的行不重写，**幂等**——重复执行不产生更新（返回 0）；
- 本模块只管明细，不碰 `usage_hourly`；调用方在返回值 > 0 时重算小时汇总
  （`StatsCollector.rollup_hourly`）。

**口径提醒**：历史汇率与当时的价表已不可得，故这是「按今天口径重估」，不是
还原每笔请求发生时的真实花费。
"""

from __future__ import annotations

from typing import Any

from ..pricing import PriceTable, estimate_cost_usd, to_cny


def _row_cost(table: PriceTable, rate: float, row: Any) -> tuple[float | None, float | None]:
    """单行 (cost_usd, cost_cny)；无法定价时 (None, None)。"""
    usd = estimate_cost_usd(
        table, row["model"], input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"], cached_tokens=row["cached_tokens"])
    if usd is None:
        return None, None
    return usd, to_cny(usd, rate)


def _changes(conn, table: PriceTable, rate: float):
    """产出 (rowid, model, cost_usd, cost_cny)，只含会被改写的行（生成器）。

    不能定价的行跳过（保持原样）；已经是当前口径的行也跳过（保证幂等）。
    """
    for row in conn.execute(
            "SELECT rowid, model, input_tokens, output_tokens, cached_tokens, "
            "cost_usd, cost_cny FROM usage_events"):
        usd, cny = _row_cost(table, rate, row)
        if usd is None:
            continue
        if row["cost_usd"] == usd and row["cost_cny"] == cny:
            continue
        yield row["rowid"], row["model"], usd, cny


def pending_cost(conn, table: PriceTable, rate: float) -> list[tuple[str, int, float]]:
    """预览：[(model, 条数, 合计人民币)]，只含会被改写的行。"""
    counts: dict[str, list[Any]] = {}
    for _rowid, model, _usd, cny in _changes(conn, table, rate):
        entry = counts.setdefault(model, [0, 0.0])
        entry[0] += 1
        entry[1] += cny
    return [(model, count, total)
            for model, (count, total) in sorted(counts.items())]


def recompute_costs(db, table: PriceTable, rate: float) -> int:
    """按当前价表 / 汇率重算全部明细成本，返回改写条数（0 = 无需处理）。

    `db` 只用到 `Database` 的 `transaction()` 形态（鸭子类型），便于脚本注入
    裸连接适配器与测试。先整批物化再 `executemany`：不在遍历游标的同时改表。
    """
    with db.transaction() as conn:
        updates = [(usd, cny, rowid)
                   for rowid, _model, usd, cny in _changes(conn, table, rate)]
        if updates:
            conn.executemany(
                "UPDATE usage_events SET cost_usd = ?, cost_cny = ? WHERE rowid = ?",
                updates)
    return len(updates)


# ---------------------------------------------------------------- 小时汇总重算

# 判定「汇总行与明细不符」的列：请求数 / 成功数 / 四个 token 列。
# 不含 credit / cost：它们的 NULL 语义要按 known 计数展开比对，而重算走整行
# REPLACE（宁可多算），诊断只需回答「有没有行需要被重建」。
_GAP_MISMATCH_SQL = """
    h.requests IS NULL OR h.requests != ev.requests
    OR h.ok_count != ev.ok_count
    OR h.input_tokens != ev.input_tokens
    OR h.output_tokens != ev.output_tokens
    OR h.cached_tokens != ev.cached_tokens
"""

# 明细按 (小时, 用户, 渠道, 模型) 聚合——与 usage_hourly 的主键同粒度
_EVENT_HOURLY_SQL = """
    SELECT (ts / 3600) * 3600 AS hour_utc, username, provider, model,
           COUNT(*) AS requests, SUM(ok) AS ok_count,
           COALESCE(SUM(input_tokens), 0) AS input_tokens,
           COALESCE(SUM(output_tokens), 0) AS output_tokens,
           COALESCE(SUM(cached_tokens), 0) AS cached_tokens
    FROM usage_events GROUP BY 1, 2, 3, 4
"""


def _span(conn, table: str, column: str) -> tuple[int, int] | None:
    """表内 `column` 的 (最小, 最大)；空表返回 None。"""
    row = conn.execute(
        f"SELECT MIN({column}) AS lo, MAX({column}) AS hi FROM {table}").fetchone()
    return (row["lo"], row["hi"]) if row["lo"] is not None else None


def pending_hourly(conn, limit: int = 10) -> dict[str, Any]:
    """诊断明细与小时汇总的缺口，供「历史重新汇总」脚本预览。

    返回：

    - `missing`：明细有、汇总里**没有该行**的分组键数；
    - `mismatched`：汇总行在，但请求数 / 令牌数与明细对不上的分组键数；
    - `samples`：前 `limit` 个缺口样例（便于人工确认是不是真缺）；
    - `events` / `hourly`：明细条数与汇总行数；
    - `events_span` / `hourly_span`：两侧各自的时间跨度。

    **口径**：明细只保留 90 天，故「汇总起点晚于明细起点」意味着更早的小时既无
    明细也无汇总，无法还原（由调用方提示，不是本函数的缺口）。
    """
    gaps: list[tuple[int, str, str, str, int, bool]] = []
    for row in conn.execute(
            f"""
            SELECT ev.hour_utc, ev.username, ev.provider, ev.model, ev.requests,
                   (h.hour_utc IS NULL) AS missing
            FROM ({_EVENT_HOURLY_SQL}) ev
            LEFT JOIN usage_hourly h
              ON h.hour_utc = ev.hour_utc AND h.username = ev.username
             AND h.provider = ev.provider AND h.model = ev.model
            WHERE {_GAP_MISMATCH_SQL}
            ORDER BY ev.hour_utc, ev.username, ev.provider, ev.model
            """):
        gaps.append((row["hour_utc"], row["username"], row["provider"],
                     row["model"], row["requests"], bool(row["missing"])))

    return {
        "missing": sum(1 for gap in gaps if gap[5]),
        "mismatched": sum(1 for gap in gaps if not gap[5]),
        "samples": gaps[:limit],
        "events": conn.execute("SELECT COUNT(*) c FROM usage_events").fetchone()["c"],
        "hourly": conn.execute("SELECT COUNT(*) c FROM usage_hourly").fetchone()["c"],
        "events_span": _span(conn, "usage_events", "ts"),
        "hourly_span": _span(conn, "usage_hourly", "hour_utc"),
    }
