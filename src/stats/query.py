"""用量统计查询（TECHNICAL §2 query.py）：overview / by-provider / timeline / events。"""

from __future__ import annotations

import time
from typing import Any

from ..sorting import parse_sort_order, sql_order

# 图表指标 → 取值语义。小时汇总只存 sum / count，因此**派生比率必须在聚合后
# 相除**：先按小时求比率再平均是另一种口径（会被低量小时等权带偏），不能混用。
METRIC_KINDS: dict[str, str] = {
    "requests": "sum_requests",
    "tokens": "sum_tokens",
    # 成本默认口径是人民币（与本模块展示一致）；仅含可定价明细的小时才有值。
    "cost": "sum_cost",
    "latency": "mean_latency",     # 均值由 ok_count 归一
    "ttfb": "mean_ttfb",           # 同上
    # 派生指标（P1）：成功率 / 缓存命中率 / 单请求成本。
    "success_rate": "ratio_ok",
    "cache_rate": "ratio_cache",
    "cost_per_request": "per_request_cost",
}

# 三个图表查询（timeline / model_timeline）共用的聚合列。
#
# 为什么一次取全：派生指标要的是**多个 sum 相除**，只选 `SUM(expr)` 一个值就
# 得为每个 metric 拼不同的 SQL，三处（timeline、model_timeline、将来新增的）
# 迟早会分叉。这里固定取齐 10 列，取值语义留给 `_metric_value` 决定。
_METRIC_AGGREGATES = """
                   SUM(requests) AS requests,
                   SUM(ok_count) AS ok_count,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(cached_known) AS cached_known,
                   SUM(cost_cny_sum) AS cost_cny_sum,
                   SUM(cost_known) AS cost_known,
                   SUM(latency_sum) AS latency_sum,
                   SUM(ttfb_sum) AS ttfb_sum"""

# 分位档：图表时序用的分位（延迟图要「中位数线 + P95 带」）。
# **不加进 METRIC_KINDS**：分位读明细表（小时汇总只存 sum/count，数学上无法
# 还原分布），见 `percentile_timeline`。
PERCENTILES: tuple[int, ...] = (50, 90, 95, 99)

# 稠密时间轴上限：`timeline` 按小时补桶，超过这个跨度就不再补（「全部」范围
# 可能横跨几个月，几万个空点只会拖垮图表）。约 92 天，覆盖最长的 90 天窗口。
MAX_DENSE_HOURS = 92 * 24

# 明细聚合子查询（分位 + 输出速度）在主查询里的 JOIN 别名：`_percentile_join`
# 与两个分组路径共用，别名固定。
_DETAIL_AGG_ALIAS = "agg"

# 输出速度（tok/s）口径：**token 总量 ÷ 生成时间总和**，不是逐条平均——
# 短请求的逐条比值噪声大，逐条平均会把整体数字带歪；总量相除才是池级吞吐。
# 只统计经本服务路由的成功请求，且**至少产出了 1 个 token**：输出为 0 / 未上报
# 的请求没有可测的产出（比值恒为 0），不是「慢」，不该混进分母。**不设输出量
# 下限**——短输出照样计入，量少时噪声由「总量相除」和下面两道物理下限兜住。
# 生成窗口下限（ms）：`latency - ttfb` 极小时分母趋近 0，比值会被网络抖动 /
# 毫秒取整放大成天文数字。真实成因是上游把整段内容攒成一批吐（首帧到达时
# 内容其实早已生成完），此时「生成时长」根本不可测——这类样本没有信息量，
# 属于无样本而不是「很快」，一律剔除（实测 901 token / 3ms = 30 万 tok/s）。
SPEED_MIN_GEN_MS = 200
# 单条样本的物理上限（tok/s）：单流生成不可能到这个量级。与上面的窗口下限
# 互补——窗口够长但仍远快于硬件极限的样本同样是「批到达」的畸形数据
# （如 901 token / 300ms），一并剔除，不拿它冒充速度。
SPEED_MAX_TPS = 1000


# 速度口径的样本筛选收在上面那两个 `_*_guard` 函数里：三处判定必须逐字一致，
# 各写一份迟早会分叉——
#   1. 分组 / 总览的 SQL 条件聚合（`_speed_aggregates`，见 `_percentile_join`）
#   2. 逐请求明细的 SQL 列（`_row_speed_sql`，`GET /api/stats/events`）
#   3. 实时流广播行的 Python 计算（`row_speed`，SSE 不查库，只能自己算）
def _routed_guard(ref: str) -> str:
    """路由口径的样本筛选（生成时间可测：有首字，分母 = 耗时 − 首字）。

    要求**至少产出 1 个 token**（`output_tokens > 0`，NULL 参与比较得 NULL 而
    排除）、生成窗口 ≥ `SPEED_MIN_GEN_MS` 且不超过单流物理上限。**不设输出量
    下限**：短输出也是真实产出，剔除它会让「短问答为主」的渠道永远没有速度；
    分母趋近 0 的畸形样本已由窗口下限与 `SPEED_MAX_TPS` 拦掉。
    `latency_ms > ttfb_ms` 同时承担「`ttfb_ms` 为 NULL 则排除」。
    `ref` 是表/子查询前缀，可为空串。
    """
    gen = f"({ref}latency_ms - {ref}ttfb_ms)"
    return (f"{ref}output_tokens > 0 "
            f"AND {ref}latency_ms > {ref}ttfb_ms "
            f"AND {gen} >= {SPEED_MIN_GEN_MS} "
            f"AND {ref}output_tokens * 1000.0 <= {SPEED_MAX_TPS} * {gen}")


def _estimate_guard(ref: str) -> str:
    """估算口径的样本筛选（无首字数据，分母 = 端到端耗时，含首字等待）。

    样本门槛与路由口径一致（同样是「至少 1 个 token」+ 两道物理下限）：两口径
    只在**分母**上不同，样本集合对齐后「换口径」不会顺带换掉样本。端到端耗时
    含首字等待，本就比生成窗口大，比值只会更保守。
    """
    return (f"{ref}output_tokens > 0 "
            f"AND {ref}latency_ms >= {SPEED_MIN_GEN_MS} "
            f"AND {ref}output_tokens * 1000.0 <= {SPEED_MAX_TPS} * {ref}latency_ms")


def _visible_tokens(ref: str) -> str:
    """正文 token 的 SQL 表达式：输出 − 思考，负数折 0（上游偶有 reasoning > output）。"""
    return (f"MAX(COALESCE({ref}output_tokens, 0) "
            f"- COALESCE({ref}reasoning_tokens, 0), 0)")


def _row_speed_sql(ref: str = "e.") -> tuple[str, str]:
    """单条明细的速度列 SQL `(正文, 总产出)`；样本不合格的行给 NULL（显示 —）。

    逐行只按**路由口径**算（`_routed_guard`），**不回落估算口径**：估算的分母
    含首字等待，聚合口径靠成百上千条样本摊平还能看，单条上就是拿「等我打完
    首字的时间」当生成时间，逐行展示只会误导。没有生成窗口的行给 — 更诚实。

    正文为 0（整段输出都是思考）时给 NULL 而不是 0：「算不出正文速度」与
    「正文速度是 0」在界面上不是一回事（前者显示 —）。
    """
    gen = f"({ref}latency_ms - {ref}ttfb_ms)"
    sample = f"{ref}ok = 1 AND {_routed_guard(ref)}"
    visible = (f"NULLIF(ROUND(CASE WHEN {sample} "
               f"THEN {_visible_tokens(ref)} * 1000.0 / {gen} END, 1), 0)")
    total = f"ROUND(CASE WHEN {sample} THEN {ref}output_tokens * 1000.0 / {gen} END, 1)"
    return visible, total


# 逐请求明细的速度表达式（`(正文, 总产出)`）：排序白名单与 `events()` 的
# SELECT 列共用同一片段，避免「排序按 A 算、展示按 B 算」两套口径。
_ROW_SPEED_VISIBLE, _ROW_SPEED_TOTAL = _row_speed_sql("e.")


def row_speed(*, ok: bool | int, output_tokens: int | None, reasoning_tokens: int | None,
              latency_ms: int | None, ttfb_ms: int | None) -> tuple[float | None, float | None]:
    """单条明细的 `(正文 tok/s, 总产出 tok/s)`；不可测给 None（= `_row_speed_sql`）。

    实时流（`StatsCollector.record` 广播）不查库，只能在广播前用 Python 算，
    故这里保留一份与 SQL 同规则的实现（规则本体在 `_routed_guard`）。
    """
    if not ok or output_tokens is None or latency_ms is None or ttfb_ms is None:
        return None, None
    gen = latency_ms - ttfb_ms
    # 与 `_routed_guard` 同规则：至少 1 个 token、窗口 ≥ 下限、不超过物理上限
    if output_tokens <= 0 or gen < SPEED_MIN_GEN_MS:
        return None, None
    if output_tokens * 1000.0 > SPEED_MAX_TPS * gen:
        return None, None
    visible = max(output_tokens - (reasoning_tokens or 0), 0)
    visible_tps = round(visible * 1000.0 / gen, 1) if visible > 0 else None
    return visible_tps, round(output_tokens * 1000.0 / gen, 1)


# 分组统计排序白名单：API 键 → 聚合列别名（`group` 指分组键列 `group_key`）。
# 默认按请求数降序（「哪家/哪个模型用得最多」才是分组表的用途），并列时按
# 分组键升序——次序稳定且与旧行为一致（旧代码就是按分组键升序）。
#
# 延迟类键的两种来源：
# - 均值（avg_*）：小时汇总上的 `*_sum ÷ ok_count`，SQLite 允许 ORDER BY 引用
#   SELECT 别名并做表达式；`ok_count = 0` 时除零得 NULL，降序自然排最后（与
#   其它可空列同语义）。
# - 分位（p50_* / p90_* / p95_* / p99_*）：由 `_percentile_join` 的 LEFT JOIN
#   子查询算出，排序直接走该列别名。
GROUP_SORT_COLUMNS: dict[str, str] = {
    "group": "group_key",
    "requests": "requests",
    "ok_count": "ok_count",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cached_tokens": "cached_tokens",
    "credit": "credit_sum",
    "cost_cny": "cost_cny_sum",
    "avg_latency_ms": "CAST(latency_sum AS REAL) / ok_count",
    "avg_ttfb_ms": "CAST(ttfb_sum AS REAL) / ok_count",
    # 输出速度：读明细聚合（与分位同源，只覆盖 90 天）。路由口径（生成时间总和）
    # 优先；该组无路由样本时回落估算口径（端到端耗时含首字等待）。两口径都是
    # 总量相除，tokens/ms 与 tok/s 单调同序，排序直接按该表达式。
    # `tps` 是总产出（含思考 token），`tps_visible` 是正文（不含思考）；
    # 同分母，故两者排序互不干扰，各自按自己的分子排。
    "tps": (f"CASE WHEN {_DETAIL_AGG_ALIAS}.speed_gen_ms > 0 THEN "
            f"CAST({_DETAIL_AGG_ALIAS}.speed_tokens AS REAL) "
            f"/ {_DETAIL_AGG_ALIAS}.speed_gen_ms "
            f"WHEN {_DETAIL_AGG_ALIAS}.est_ms > 0 THEN "
            f"CAST({_DETAIL_AGG_ALIAS}.est_tokens AS REAL) "
            f"/ {_DETAIL_AGG_ALIAS}.est_ms ELSE NULL END"),
    "tps_visible": (f"CASE WHEN {_DETAIL_AGG_ALIAS}.speed_gen_ms > 0 THEN "
                    f"CAST({_DETAIL_AGG_ALIAS}.speed_visible_tokens AS REAL) "
                    f"/ {_DETAIL_AGG_ALIAS}.speed_gen_ms "
                    f"WHEN {_DETAIL_AGG_ALIAS}.est_ms > 0 THEN "
                    f"CAST({_DETAIL_AGG_ALIAS}.est_visible_tokens AS REAL) "
                    f"/ {_DETAIL_AGG_ALIAS}.est_ms ELSE NULL END"),
    "p50_latency_ms": "p50_latency_ms",
    "p50_ttfb_ms": "p50_ttfb_ms",
    "p90_latency_ms": "p90_latency_ms",
    "p90_ttfb_ms": "p90_ttfb_ms",
    "p95_latency_ms": "p95_latency_ms",
    "p95_ttfb_ms": "p95_ttfb_ms",
    "p99_latency_ms": "p99_latency_ms",
    "p99_ttfb_ms": "p99_ttfb_ms",
}


# 逐请求明细排序白名单：API 键 → 列（带 `e.` 前缀，因查询有 JOIN）。
# `time` 是 rowid（插入序）；它保持降序时走游标分页，其余情况走 offset 分页。
EVENT_SORT_COLUMNS: dict[str, str] = {
    "time": "e.rowid",
    "ts": "e.ts",
    "username": "e.username",
    "provider": "e.provider",
    "credential": "e.credential_id",
    "model": "e.model",
    "ok": "e.ok",
    "input_tokens": "e.input_tokens",
    "output_tokens": "e.output_tokens",
    "cached_tokens": "e.cached_tokens",
    # 命中率是派生列：命中 ÷ 输入。输入为 0 / 未上报（NULL）时给 NULL——与其它
    # 可空列同例（降序时 NULL 自然排最后），不拿「未上报」冒充 0%。CAST 成 REAL
    # 走浮点除法（整数除法会截断成 0）。
    "cache_rate": ("CASE WHEN e.input_tokens > 0 "
                   "THEN CAST(e.cached_tokens AS REAL) / e.input_tokens ELSE NULL END"),
    "credit": "e.credit",
    "cost_cny": "e.cost_cny",
    "ttfb_ms": "e.ttfb_ms",
    "latency_ms": "e.latency_ms",
    # 逐请求速度（路由口径，见 `_row_speed_sql`）：无合格样本的行该列为 NULL，
    # 降序时自然排最后。`tps` 总产出（含思考）/+ `tps_visible` 正文，各按自己排。
    "tps_visible": _ROW_SPEED_VISIBLE,
    "tps": _ROW_SPEED_TOTAL,
}


# 分组键白名单：`_group_hourly` / `_group_events` 把 key 直接拼进 SQL（列名无法
# 参数绑定），调用点全是本模块内的字面量。留一份显式白名单并在入口断言，未来
# 若有人把外部输入当 key 传进来，会当场炸而不是拼出注入语句。
_GROUP_KEYS = frozenset({"provider", "model", "username", "credential_id",
                        "api_key_id"})

# CSV 导出单次上限：明细只留 90 天，但全量导出（几万行）会一次性拉进内存并
# 撑爆响应。超上限时明确告知「被截断、请收窄条件」，而不是静默给半份文件。
EXPORT_MAX_ROWS = 10000

# 失败归因（`/api/stats/errors`）：默认窗口 24 小时、单次最多 5000 组。
# 窗口是硬要求——没有 `ts` 下界就是全表扫（明细留 90 天，几十万行）。
ERROR_WINDOW_SECONDS = 86400
ERROR_MAX_GROUPS = 5000

# 分位值的 nearest-rank 名次：第 p 百分位 = 第 ⌈p·n/100⌉ 小的值。用整数运算写
# `(p * n + 99) / 100`（SQLite 整数除法即向下取整，加分母−1 后等价于对分子
# 向上取整），避免浮点误差把名次算偏。与原 P95 的 `(95*n+99)/100` 逐字节一致。
_PERCENT_DENOM = 100

# 每档分位在结果行里下发的列名（latency 与 ttfb 各一列）。`_group_row` 按此
# 生成返回 dict——加档只需改 `PERCENTILES`，不用再改三处 SQL/映射。
_PERCENTILE_FIELDS: tuple[str, ...] = tuple(
    f"p{pct}_{kind}" for pct in PERCENTILES for kind in ("latency_ms", "ttfb_ms"))

# 明细聚合子查询下发的速度求和列名（`_group_row` / `_speed_value` 共用）：
# speed_* = 路由口径（生成时间总和），est_* = 无首字数据时的估算口径；
# *_tokens 是总产出（含思考），*_visible_tokens 是正文（不含思考）。
_SPEED_FIELDS: tuple[str, ...] = ("speed_tokens", "speed_visible_tokens", "speed_gen_ms",
                                  "est_tokens", "est_visible_tokens", "est_ms")


def _speed_aggregates(indent: str = "                   ", ref: str = "ranked.") -> str:
    """速度求和列（路由口径 + 估算口径；各含总产出与正文两个分子）。

    `ref`：取值行的表/子查询前缀——分组路径取窗口子查询（`ranked.`），
    总览路径直接在 `usage_events` 上聚合（空串）。

    样本筛选复用 `_routed_guard` / `_estimate_guard`（逐请求明细与实时流用同一
    套，见那两处注释），这里只负责把符合样本条件的分母与分子各自求和：
    - 路由：`output_tokens > 0` 且 `latency > ttfb`（`ttfb_ms` 为 NULL 时比较得
      NULL，天然排除非流式请求），分母是生成时间（耗时 − 首字）；另加物理合理性
      下限（见 `SPEED_MIN_GEN_MS` / `SPEED_MAX_TPS`）：窗口过短或单条快得不像是
      真生成的样本按「无法测量」剔除，不当作速度。**不设输出量下限**：短输出也
      是真实产出，剔掉会让短问答为主的渠道永远没有速度。
    - 估算：同样 `output_tokens > 0`，分母是端到端耗时（含首字等待）。仅在路由
      口径无样本时使用，展示标 ≈。物理下限同样要加：分母换成端到端耗时只是把
      比值压小，攒批到达的样本照样能算出成百上千 tok/s，不能因为「换了分母」就
      当成有效样本。样本门槛与路由口径一致，两口径只差在分母。

    正文分子用 `output_tokens - reasoning_tokens`：思考 token 也是模型产出的，
    但它是成批快速吐出的（实测 13071 思考 token 62s 流完，正文随后 5.9s 吐完
    1966 帧），混在一起算会得到远高于「屏幕上看到的正文刷新速度」的数字。
    两个分子同分母，故样本筛选只需按总产出的窗口判定（正文 ⊆ 总产出，总产出
    不超 `SPEED_MAX_TPS` 时正文也不会超）。
    """
    gen = f"({ref}latency_ms - {ref}ttfb_ms)"
    routed = _routed_guard(ref)
    est = _estimate_guard(ref)
    visible = _visible_tokens(ref)
    return (
        f"{indent}COALESCE(SUM(CASE WHEN {routed}\n"
        f"{indent}         THEN {ref}output_tokens END), 0) AS speed_tokens,\n"
        f"{indent}COALESCE(SUM(CASE WHEN {routed}\n"
        f"{indent}         THEN {visible} END), 0) AS speed_visible_tokens,\n"
        f"{indent}COALESCE(SUM(CASE WHEN {routed}\n"
        f"{indent}         THEN {gen} END), 0) AS speed_gen_ms,\n"
        f"{indent}COALESCE(SUM(CASE WHEN {est}\n"
        f"{indent}         THEN {ref}output_tokens END), 0) AS est_tokens,\n"
        f"{indent}COALESCE(SUM(CASE WHEN {est}\n"
        f"{indent}         THEN {visible} END), 0) AS est_visible_tokens,\n"
        f"{indent}COALESCE(SUM(CASE WHEN {est}\n"
        f"{indent}         THEN {ref}latency_ms END), 0) AS est_ms"
    )


def _speed_value(tokens: int | None, visible_tokens: int | None, gen_ms: int | None,
                 est_tokens: int | None, est_visible_tokens: int | None,
                 est_ms: int | None) -> tuple[float | None, float | None, bool]:
    """`(总产出 tok/s, 正文 tok/s, 是否估算)`；某口径无样本时该值为 None。

    路由口径有样本就用它，否则回落估算口径，都没有给 `(None, None, False)`。
    分子分母都要求 > 0：`SUM(CASE …)` 无匹配行时为 NULL，`COALESCE` 归 0；
    有耗时却没上报 token（或反之）的行不该产出「0 tok/s」这种假数据。正文分子
    还可能为 0（整段输出都是思考），此时正文列给 None 而不是假的 0。
    """
    def ratio(num: int | None, den: int | None) -> float | None:
        if den and den > 0 and num and num > 0:
            return round(num * 1000 / den, 1)
        return None

    routed = ratio(tokens, gen_ms)
    if routed is not None:
        return routed, ratio(visible_tokens, gen_ms), False
    estimated = ratio(est_tokens, est_ms)
    if estimated is not None:
        return estimated, ratio(est_visible_tokens, est_ms), True
    return None, None, False


def _percentile_rank(n_expr: str, pct: int) -> str:
    """`⌈pct/100·n⌉` 的 SQL 表达式（整数运算，避免浮点误差）。"""
    return f"({pct} * {n_expr} + {_PERCENT_DENOM - 1}) / {_PERCENT_DENOM}"


def _percentile_select(pct: int, *, indent: str = "                   ") -> str:
    """子查询里某一档分位的两列（latency / ttfb）。"""
    return (
        f"{indent}MAX(CASE WHEN ranked.lat_rn = {_percentile_rank('ranked.lat_n', pct)}\n"
        f"{indent}         THEN ranked.latency_ms END) AS p{pct}_latency_ms,\n"
        f"{indent}MAX(CASE WHEN ranked.ttfb_rn = {_percentile_rank('ranked.ttfb_n', pct)}\n"
        f"{indent}         THEN ranked.ttfb_ms END) AS p{pct}_ttfb_ms"
    )


def _percentile_columns(outer: str = _DETAIL_AGG_ALIAS) -> str:
    """主查询里取各档分名列的 SELECT 片段（逗号分隔，不含前导逗号）。"""
    return ",\n                   ".join(
        f"{outer}.{name} AS {name}" for name in _PERCENTILE_FIELDS)


def _speed_columns(outer: str = _DETAIL_AGG_ALIAS) -> str:
    """主查询里取速度求和列的 SELECT 片段（供 `_group_row` 折算 tok/s）。"""
    return ",\n                   ".join(
        f"{outer}.{name} AS {name}" for name in _SPEED_FIELDS)


def _percentile_join(key: str, *, where: str = "", params: list[Any] | None = None,
                     suffix: str = "e2", outer: str = "") -> tuple[str, list[Any]]:
    """按 `key` 分组算 P50/P90/P95/P99 延迟 + 输出速度求和的 LEFT JOIN 子查询。

    四档分位在**同一次**窗口函数扫描里算完：明细表按分组排一次序、按 latency 与
    ttfb 各生成一套 row_number，再用条件聚合按名次挑出各档的值。拆成四个 JOIN
    会把同一批明细扫四遍。输出速度的两套求和（路由 / 估算口径）也在这次扫描里
    用条件聚合顺带算完，不再单独扫一遍明细。

    分位与速度**只能从明细表算**：小时汇总只存 `latency_sum` / `ttfb_sum`（和与
    成功计数），既无法还原分布，也无法按「单请求输出量 / 生成窗口」筛选样本。
    代价是只覆盖明细保留期（90 天），均值列仍读小时汇总（永久）——表尾文案把这个
    差异写明。

    口径与均值一致：**只算成功请求**（`ok = 1`，TECHNICAL §9.7）。`ttfb_ms`
    对非流式 / 未产出首字的成功请求为 NULL，故 latency 与 ttfb 各自独立排名
    （`COUNT(col)` 忽略 NULL，天然只数有值样本）。`ORDER BY (col IS NULL), col`
    把 NULL 排在有值样本之后，配合 `COUNT(col)` 的名次不会错位。速度的路由口径
    同样要求 `latency > ttfb`（`ttfb_ms` 为 NULL 的比较得 NULL 而被排除），估算
    口径则只要耗时可算。

    `where` / `params`：子查询**独立**应用作用域与时间窗（取值列用 `ts`，
    与主查询的 `hour_utc` 口径区分）。参数由调用方按子查询内的占位符顺序给出，
    返回时原样带回，主查询拼在自己的参数之后。

    `suffix`：子查询内部表别名，避免与主查询的别名撞名（明细路径主查询用
    `e`，故子查询用 `e2`）。
    `outer`：**主查询**那侧的分组列前缀（小时路径无别名 = ""，明细路径 = "e."），
    JOIN 的 ON 必须指向主查询的列而不是子查询里的同名列。
    """
    if key not in _GROUP_KEYS:
        raise ValueError(f"非法分组键: {key!r}")
    inner = f"{suffix}.{key}"
    pct_selects = ",\n".join(_percentile_select(pct) for pct in PERCENTILES)
    return f"""
        LEFT JOIN (
            SELECT ranked.group_key AS group_key,
{pct_selects},
{_speed_aggregates()}
            FROM (
                SELECT {inner} AS group_key, {suffix}.latency_ms AS latency_ms,
                       {suffix}.ttfb_ms AS ttfb_ms,
                       {suffix}.output_tokens AS output_tokens,
                       {suffix}.reasoning_tokens AS reasoning_tokens,
                       ROW_NUMBER() OVER (
                           PARTITION BY {inner}
                           ORDER BY ({suffix}.latency_ms IS NULL), {suffix}.latency_ms
                       ) AS lat_rn,
                       COUNT({suffix}.latency_ms) OVER (PARTITION BY {inner}) AS lat_n,
                       ROW_NUMBER() OVER (
                           PARTITION BY {inner}
                           ORDER BY ({suffix}.ttfb_ms IS NULL), {suffix}.ttfb_ms
                       ) AS ttfb_rn,
                       COUNT({suffix}.ttfb_ms) OVER (PARTITION BY {inner}) AS ttfb_n
                FROM usage_events {suffix}
                WHERE {suffix}.ok = 1 AND {inner} IS NOT NULL {where}
            ) AS ranked
            GROUP BY ranked.group_key
        ) AS {_DETAIL_AGG_ALIAS} ON {_DETAIL_AGG_ALIAS}.group_key = {outer}{key}
    """, list(params or [])


def _group_order(sort: str | None, order: str | None) -> str:
    """分组统计的 `ORDER BY`：主列随 sort，并列时分组键升序（稳定次序）。"""
    key, desc = parse_sort_order(sort, order, GROUP_SORT_COLUMNS,
                                 default_key="requests", default_desc=True)
    column = GROUP_SORT_COLUMNS[key]
    if column == "group_key":
        return "group_key ASC"
    direction = "DESC" if desc else "ASC"
    return f"{column} {direction}, group_key ASC"


class StatsQuery:
    def __init__(self, db) -> None:
        self._db = db

    def _metric_value(self, metric: str, row: Any) -> Any:
        """把一行聚合结果按 metric 归一成图表值；无有效样本给 `None`。

        入参是**整行**（`_METRIC_AGGREGATES` 的 10 列），不再只给单个
        `SUM(expr)`：成功率、缓存命中率、单请求成本都是两个 sum 相除，先聚合
        后相除才是池级口径（先逐小时相除再平均会被低量小时等权带偏）。

        「无样本」一律给 `None` 而不是 0——前端据此断线（不画成 0 值），
        否则一个没上报缓存的渠道会显示成「命中率 0%」。

        - `requests` / `tokens`：sum，恒有值
        - `latency` / `ttfb`：成功请求均值，无成功 → None（原实现给 0，会画成
          「0 ms」的假低谷，与「该小时没有成功请求」是两件事）
        - `cost`：仅含可定价明细的小时才有值（部分可定价时按已定价部分求和）
        - `success_rate`：ok_count / requests，requests 为 0 → None
        - `cache_rate`：cached / input，无上报（cached_known=0）或输入为 0 → None
        - `cost_per_request`：cost / requests，无定价或请求为 0 → None
        """
        kind = METRIC_KINDS.get(metric, "sum_requests")
        if kind == "sum_requests":
            return row["requests"]
        if kind == "sum_tokens":
            return row["input_tokens"] + row["output_tokens"]
        if kind == "sum_cost":
            return row["cost_cny_sum"] if row["cost_known"] else None
        if kind == "mean_latency":
            return round(row["latency_sum"] / row["ok_count"]) if row["ok_count"] else None
        if kind == "mean_ttfb":
            return round(row["ttfb_sum"] / row["ok_count"]) if row["ok_count"] else None
        if kind == "ratio_ok":
            return round(row["ok_count"] / row["requests"], 4) if row["requests"] else None
        if kind == "ratio_cache":
            if not row["cached_known"] or not row["input_tokens"]:
                return None
            return round(row["cached_tokens"] / row["input_tokens"], 4)
        if kind == "per_request_cost":
            if not row["cost_known"] or not row["requests"]:
                return None
            return round(row["cost_cny_sum"] / row["requests"], 6)
        raise ValueError(f"未知指标语义: {kind!r}")  # pragma: no cover - 词表封闭

    @staticmethod
    def _where(*, username: str | None = None, provider: str | None = None,
               model: str | None = None, credential_id: str | None = None,
               ok: int | None = None,
               since: int | None = None, since_col: str = "ts",
               until: int | None = None, until_col: str = "ts",
               before: int | None = None, before_col: str = "rowid",
               alias: str = "") -> tuple[str, list[Any]]:
        """拼 WHERE 子句与参数。alias 为 JOIN 时的表前缀（如 "e."）。

        `since` 与 `until` 是**同一个时间窗的两端**（半开区间 `[since, until)`），
        区别只在取值列：明细表用 `ts`，小时汇总用 `hour_utc`。两者可单独给
        （只给一端 = 单边不限），也可都不给（全部，与历史行为一致）。

        `before` 是**游标**不是时间：它按 `rowid`（插入序）取「更小的一页」，
        与 90 天时间窗无关，故与 `until` 分开、各用一列。

        `ok`：只看成功（1）或只看失败（0）的明细。`None` = 不限制（历史行为）。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if username is not None:
            clauses.append(f"{alias}username = ?")
            params.append(username)
        if provider is not None:
            clauses.append(f"{alias}provider = ?")
            params.append(provider)
        if model is not None:
            clauses.append(f"{alias}model = ?")
            params.append(model)
        if credential_id is not None:
            clauses.append(f"{alias}credential_id = ?")
            params.append(credential_id)
        if ok is not None:
            clauses.append(f"{alias}ok = ?")
            params.append(ok)
        if since is not None:
            clauses.append(f"{alias}{since_col} >= ?")
            params.append(since)
        if until is not None:
            # 半开区间上界：`until` 本身不计入（与 since 的闭下界区分），
            # 前端按此把「结束时间」当作瞬时右端点，前后两次查询不重叠
            clauses.append(f"{alias}{until_col} < ?")
            params.append(until)
        if before is not None:
            clauses.append(f"{alias}{before_col} < ?")
            params.append(before)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def overview(self, *, username: str | None = None, provider: str | None = None,
                 since: int | None = None, until: int | None = None) -> dict[str, Any]:
        """总览。admin 传 username=None 看全局；普通用户只看自己。

        数据源是 `usage_hourly`（与 timeline/model-timeline 同源）：
        明细只留 90 天，读明细会让选「全部」时总览小于图表；小时汇总永久保留。
        代价：最近 ≤5 分钟未进汇总的请求不计入（retention 每 5 分钟 rollup），
        刷新一次即可。延迟类均值统一按 `ok_count` 归一（与图表同口径）。

        **例外：输出速度**（`tps` / `tps_estimated`）读明细表——小时汇总没有
        单请求输出量与逐请求耗时，无法按「单请求输出量 / 生成窗口」筛样本；
        代价是只覆盖明细保留期（90 天），选「全部」时它只反映最近 90 天（前端
        卡片文案写明）。路由口径优先（生成时间总和），无路由样本时回落估算口径
        （含首字等待）并标 `tps_estimated=True`。

        `since` / `until`：自定义时间窗两端（半开 `[since, until)`），按小时桶
        过滤，与 `timeline` 同口径。
        """
        where, params = self._where(username=username, provider=provider,
                                    since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")

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
                   SUM(credit_estimated_known) AS credit_estimated_known,
                   SUM(cost_usd_sum) AS cost_usd_sum,
                   SUM(cost_cny_sum) AS cost_cny_sum,
                   SUM(cost_known) AS cost_known,
                   SUM(latency_sum) AS latency_sum,
                   SUM(ttfb_sum) AS ttfb_sum
            FROM usage_hourly {where}
            """, params).fetchone()
        # 输出速度单独读明细（口径见 docstring）：作用域与时间窗与上面一致，
        # 但取值列是明细的 `ts`（半开区间两端同 `_where` 的 ts 语义）。
        speed_where, speed_params = self._where(
            username=username, provider=provider, ok=1,
            since=since, since_col="ts", until=until, until_col="ts")
        speed = self._db.connect().execute(
            f"""
            SELECT
                {_speed_aggregates(indent="                ", ref="")}
            FROM usage_events {speed_where}
            """, speed_params).fetchone()
        tps, tps_visible, tps_estimated = _speed_value(
            speed["speed_tokens"], speed["speed_visible_tokens"], speed["speed_gen_ms"],
            speed["est_tokens"], speed["est_visible_tokens"], speed["est_ms"])
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
            # 汇总 credit 中含推算值时标 ≈（全部渠道任一为推算即标，保守）
            "credit_estimated": bool(row["credit_estimated_known"]),
            # 成本：只统计匹配到定价的明细（cost_known 条数>0 才可信）
            "cost_usd": row["cost_usd_sum"] if row["cost_known"] else None,
            "cost_cny": row["cost_cny_sum"] if row["cost_known"] else None,
            "avg_latency_ms": round(row["latency_sum"] / ok_count) if ok_count else None,
            "avg_ttfb_ms": round(row["ttfb_sum"] / ok_count) if ok_count else None,
            "tps": tps,
            "tps_visible": tps_visible,
            "tps_estimated": tps_estimated,
        }

    def metrics_provider_model(self) -> list[tuple[str, str, int, int]]:
        """按 (渠道, 模型) 汇总请求数 / 成功数（/metrics 的 requests_total）。

        读**永久保留**的 `usage_hourly`（与总览同源）：明细只留 90 天，
        用它算累计计数器会随裁剪下降。返回 (provider, model, requests, ok_count)，
        由调用方拆成 status=ok / error 两条样本。排序稳定（provider, model）。
        """
        rows = self._db.connect().execute(
            "SELECT provider, model, COALESCE(SUM(requests), 0) AS requests, "
            "COALESCE(SUM(ok_count), 0) AS ok_count "
            "FROM usage_hourly GROUP BY provider, model "
            "ORDER BY provider, model").fetchall()
        return [(row["provider"], row["model"], int(row["requests"]),
                 int(row["ok_count"])) for row in rows]

    def metrics_provider_tokens(self) -> list[tuple[str, int, int, int, int]]:
        """按渠道汇总各 token 类别（/metrics 的 tokens_total）。

        返回 (provider, input, output, reasoning, cached)，排序稳定（provider）。
        与 requests 同为 `usage_hourly`（永久）上的累计值。
        """
        rows = self._db.connect().execute(
            "SELECT provider, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens, "
            "COALESCE(SUM(cached_tokens), 0) AS cached_tokens "
            "FROM usage_hourly GROUP BY provider ORDER BY provider").fetchall()
        return [(row["provider"], int(row["input_tokens"]), int(row["output_tokens"]),
                 int(row["reasoning_tokens"]), int(row["cached_tokens"]))
                for row in rows]

    def model_failures(self, *, since: int, limit: int = 200) -> list[dict[str, Any]]:
        """按 (provider, model) 聚合窗口内的模型级失败（可用性视图的数据源）。

        只认三类错误：`model_not_found`（executor 对「上游明确说没有这个模型」
        `ErrKind.BLOCKED` 的归类，2026-10-26 从 `invalid_request` 里独立出来）、
        `invalid_request`（请求无效：`ErrKind.INVALID` / `ErrKind.REQUEST`，上游
        400 拒绝也落这里）与 `upstream_blocked`（协议预留值，未来 provider
        直接上报时自动纳入）。窗口由调用方给绝对时刻 `since`，SQL 走
        `idx_usage_ts`；`limit` 由调用方收敛，多取一条供判断截断。

        `last_failed_at` 取该组内最大 `ts`；排序按失败次数降序，同次数按
        (provider, model) 升序，保证分页 / 快照稳定。
        """
        rows = self._db.connect().execute(
            """
            SELECT provider, model, COUNT(*) AS failures, MAX(ts) AS last_failed_at
            FROM usage_events
            WHERE ts >= ? AND error_type IN
                  ('upstream_blocked', 'model_not_found', 'invalid_request')
            GROUP BY provider, model
            ORDER BY failures DESC, provider ASC, model ASC
            LIMIT ?
            """, (since, max(1, limit))).fetchall()
        return [{"provider": row["provider"], "model": row["model"],
                 "failures": int(row["failures"]),
                 "last_failed_at": int(row["last_failed_at"])} for row in rows]

    @staticmethod
    def _group_row(row: Any, key: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
        """把一行聚合结果转成 {分组键: ...} + 公共指标的 dict（各分组维度共用）。

        `extra`：维度特有的展示列（如凭证维度的 provider / credential_name），
        SQL 已算好，这里原样带出。

        延迟指标两种来源、两套口径（表尾文案对用户说明）：
        - 均值读小时汇总（永久保留），与 `overview()` 逐字节同口径：
          `round(sum / ok_count)`，无成功请求给 None（前端显示 —）；
        - 分位读明细表（只覆盖 90 天），SQL 里已按成功请求算好，NULL =
          该组无成功样本（全失败 / 未上报延迟）。

        输出速度（`tps` / `tps_visible` / `tps_estimated`）也读明细表：路由口径
        优先（生成时间总和），该组无路由样本时回落估算口径（含首字等待）并标
        `tps_estimated=True`，两者都无样本给 None。`tps` 是总产出（含思考 token），
        `tps_visible` 是正文（`output_tokens - reasoning_tokens`），两者同分母。
        """
        ok_count = row["ok_count"] or 0
        tps, tps_visible, tps_estimated = _speed_value(
            row["speed_tokens"], row["speed_visible_tokens"], row["speed_gen_ms"],
            row["est_tokens"], row["est_visible_tokens"], row["est_ms"])
        return {
            key: row["group_key"],
            "requests": row["requests"],
            "ok_count": ok_count,
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            # 缓存命中：任一明细上报过才可信，否则 None（前端显示 —）
            "cached_tokens": row["cached_tokens"] if row["cached_known"] else None,
            "credit": row["credit_sum"] if row["credit_known"] else None,
            "credit_estimated": bool(row["credit_estimated_known"]),
            "cost_usd": row["cost_usd_sum"] if row["cost_known"] else None,
            "cost_cny": row["cost_cny_sum"] if row["cost_known"] else None,
            "avg_latency_ms": round(row["latency_sum"] / ok_count) if ok_count else None,
            "avg_ttfb_ms": round(row["ttfb_sum"] / ok_count) if ok_count else None,
            "tps": tps,
            "tps_visible": tps_visible,
            "tps_estimated": tps_estimated,
            **{name: row[name] for name in _PERCENTILE_FIELDS},
            **{name: row[name] for name in extra},
        }

    def _group_hourly(self, key: str, *, username: str | None = None,
                      since: int | None = None, until: int | None = None,
                      sort: str | None = None,
                      order: str | None = None) -> list[dict[str, Any]]:
        """在小时汇总上按 `key` 列聚合（渠道 / 模型 / 用户三个维度共用）。"""
        if key not in _GROUP_KEYS:
            raise ValueError(f"非法分组键: {key!r}")
        where, params = self._where(username=username, since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")
        # 分位值另起一条明细过滤：同一作用域与时间窗，但取值列用 `ts`（明细表），
        # 与主查询的 `hour_utc`（小时表）区分——否则时间窗对不上，分位会算错范围
        pct_where, pct_params = self._where(username=username, since=since, until=until,
                                            alias="e2.")
        # 分位子查询的 WHERE 由 _where 生成（无条件时是空串），这里补 AND 接后续条件
        pct_clause = f"AND {pct_where[6:]}" if pct_where else ""
        join, join_params = _percentile_join(key, where=pct_clause, params=pct_params)
        rows = self._db.connect().execute(
            f"""
            SELECT {key} AS group_key, COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(ok_count), 0) AS ok_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(cached_known) AS cached_known,
                   SUM(credit_sum) AS credit_sum,
                   SUM(credit_known) AS credit_known,
                   SUM(credit_estimated_known) AS credit_estimated_known,
                   SUM(cost_usd_sum) AS cost_usd_sum,
                   SUM(cost_cny_sum) AS cost_cny_sum,
                   SUM(cost_known) AS cost_known,
                   COALESCE(SUM(latency_sum), 0) AS latency_sum,
                   COALESCE(SUM(ttfb_sum), 0) AS ttfb_sum,
                   {_percentile_columns()},
                   {_speed_columns()}
            FROM usage_hourly
            {join}
            {where} GROUP BY {key} ORDER BY {_group_order(sort, order)}
            """, [*join_params, *params]).fetchall()
        return [self._group_row(row, key) for row in rows]

    def _group_events(self, key: str, *, username: str | None = None,
                      since: int | None = None, until: int | None = None,
                      sort: str | None = None,
                      order: str | None = None, include_credential_name: bool = False,
                      ) -> list[dict[str, Any]]:
        """在明细上按 `key` 列聚合。

        仅凭证维度需要：小时汇总的主键里没有 credential_id，无法还原。代价是
        数据源只保留 90 天（选「全部」时凭证分组比其它维度少），且无凭证的
        预热失败等请求被排除（它们没有可归属的凭证）。

        `include_credential_name`：随行下发凭证昵称与渠道，前端据此在凭证名前
        画渠道 icon 并与请求明细显示同一个名字。昵称取自**全局共享池**（常含
        邮箱/手机），只对 admin/operator 返回（与 `events()` 的 约束一致）。
        """
        if key not in _GROUP_KEYS:
            raise ValueError(f"非法分组键: {key!r}")
        where, params = self._where(username=username, since=since, since_col="ts",
                                    until=until, until_col="ts", alias="e.")
        # 分组键为空的行（无凭证的预热失败等）没有可归属对象，不参与分组；
        # _where 无条件时返回空串，这里补 WHERE 关键词
        clause = f"{where} AND e.{key} IS NOT NULL" if where else f"WHERE e.{key} IS NOT NULL"
        # 分位子查询：与主查询同表同过滤（明细路径两条路都读 usage_events），
        # 内部别名用 e2 避免与主查询的 e 相撞
        pct_where, pct_params = self._where(username=username, since=since, until=until,
                                            alias="e2.")
        pct_clause = f"AND {pct_where[6:]}" if pct_where else ""
        join, join_params = _percentile_join(key, where=pct_clause, params=pct_params,
                                             suffix="e2", outer="e.")
        # 渠道优先取凭证当前所属渠道；凭证已被硬删除则回退该组明细里的渠道。
        # 同组跨渠道（凭证改过 provider）时 MAX 取字典序最大的那个，仅用于画
        # icon，不参与统计口径。
        name_expr = "NULLIF(c.nickname, '')" if include_credential_name else "NULL"
        rows = self._db.connect().execute(
            f"""
            SELECT e.{key} AS group_key, COUNT(*) AS requests,
                   SUM(e.ok) AS ok_count,
                   COALESCE(SUM(e.input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(e.output_tokens), 0) AS output_tokens,
                   SUM(e.cached_tokens) AS cached_tokens,
                   SUM(CASE WHEN e.cached_tokens IS NULL THEN 0 ELSE 1 END) AS cached_known,
                   COALESCE(SUM(e.credit), 0) AS credit_sum,
                   SUM(CASE WHEN e.credit IS NULL THEN 0 ELSE 1 END) AS credit_known,
                   SUM(CASE WHEN e.credit_estimated = 1 AND e.credit IS NOT NULL
                            THEN 1 ELSE 0 END) AS credit_estimated_known,
                   COALESCE(SUM(e.cost_usd), 0) AS cost_usd_sum,
                   COALESCE(SUM(e.cost_cny), 0) AS cost_cny_sum,
                   SUM(CASE WHEN e.cost_usd IS NULL THEN 0 ELSE 1 END) AS cost_known,
                   COALESCE(SUM(CASE WHEN e.ok = 1 THEN e.latency_ms END), 0) AS latency_sum,
                   COALESCE(SUM(CASE WHEN e.ok = 1 THEN e.ttfb_ms END), 0) AS ttfb_sum,
                   COALESCE(c.provider, MAX(e.provider)) AS provider,
                   {name_expr} AS credential_name,
                   {_percentile_columns()},
                   {_speed_columns()}
            FROM usage_events e
            LEFT JOIN credentials c ON c.id = e.credential_id
            {join}
            {clause}
            GROUP BY e.{key} ORDER BY {_group_order(sort, order)}
            """, [*join_params, *params]).fetchall()
        return [self._group_row(row, key, ("provider", "credential_name")) for row in rows]

    def by_provider(self, *, username: str | None = None,
                    since: int | None = None, until: int | None = None,
                    sort: str | None = None,
                    order: str | None = None) -> list[dict[str, Any]]:
        """按渠道聚合（读小时汇总，与总览/图表同源）。"""
        return self._group_hourly("provider", username=username, since=since, until=until,
                                  sort=sort, order=order)

    def by_model(self, *, username: str | None = None,
                 since: int | None = None, until: int | None = None,
                 sort: str | None = None,
                 order: str | None = None) -> list[dict[str, Any]]:
        """按模型聚合（读小时汇总，与总览/图表同源）。"""
        return self._group_hourly("model", username=username, since=since, until=until,
                                  sort=sort, order=order)

    def by_user(self, *, username: str | None = None,
                since: int | None = None, until: int | None = None,
                sort: str | None = None,
                order: str | None = None) -> list[dict[str, Any]]:
        """按用户聚合（读小时汇总，与总览/图表同源）。"""
        return self._group_hourly("username", username=username, since=since, until=until,
                                  sort=sort, order=order)

    def billing(self, *, start: int, end: int) -> dict[str, Any]:
        """月度账单：按人 + 按 Key 两套聚合 + 总计（时间窗 `[start, end)`）。

        - 按人读小时汇总（永久保留，与统计页同源）；
        - 按 Key 只能读明细（小时汇总主键没有 api_key_id，明细保留 90 天——
          跨月账单超过 90 天前会变空，这是保留策略的直接后果，不是查询 bug）；
          行里补 `key_name`（api_keys.name，空串保持空串）与本 Key 的归属用户，
          便于对账时不需要再查出 Key 是谁的。
        """
        users = self._group_hourly("username", since=start, until=end)
        keys = self._group_events("api_key_id", since=start, until=end)
        ownership = {
            row["id"]: (row["username"], row["name"])
            for row in self._db.connect().execute(
                "SELECT id, username, name FROM api_keys").fetchall()
        }
        for row in keys:
            owner, name = ownership.get(row["api_key_id"], (None, None))
            row["username"] = owner
            row["key_name"] = name
        totals = {
            "requests": sum(row["requests"] for row in users),
            "input_tokens": sum(row["input_tokens"] for row in users),
            "output_tokens": sum(row["output_tokens"] for row in users),
            "cost_cny": round(sum(row.get("cost_cny") or 0 for row in users), 6),
            "cost_usd": round(sum(row.get("cost_usd") or 0 for row in users), 6),
        }
        ok_count = sum(row["ok_count"] for row in users)
        totals["success_rate"] = (
            round(ok_count / totals["requests"], 4) if totals["requests"] else None)
        return {"users": users, "keys": keys, "totals": totals,
                "start": start, "end": end}

    def by_credential(self, *, username: str | None = None,
                      since: int | None = None, until: int | None = None,
                      sort: str | None = None,
                      order: str | None = None,
                      include_credential_name: bool = False) -> list[dict[str, Any]]:
        """按凭证聚合（读明细表：小时汇总主键里没有 credential_id）。

        行里带 `provider`（画渠道 icon）与 `credential_name`（与请求明细同口径
        显示凭证名；未授权/无昵称/凭证已删除时为 None，前端回退显示 ID 前 12 位）。
        """
        return self._group_events("credential_id", username=username, since=since,
                                  until=until, sort=sort, order=order,
                                  include_credential_name=include_credential_name)

    def timeline(self, *, username: str | None = None,
                 since: int | None = None, until: int | None = None,
                 metric: str = "requests") -> list[dict[str, Any]]:
        """按小时的指标时间序列（usage_hourly 聚合，跨 model 汇总）。

        每个点含该小时各渠道的指标值（按 metric 变化，语义见 `METRIC_KINDS`），
        供前端绘制曲线。渠道集合动态取自数据（新增渠道无需改 SQL），返回结构
        形如 `{hour, codebuddy, trae, qoder}`。

        **时间轴是稠密的**：`usage_hourly` 只在有请求时才有行，稀疏下发会让前端
        把「空档 5 小时」和「相邻 1 小时」画成一样宽（分类轴压缩时间）。这里按
        小时补齐缺失的桶，让每个点等距：

        - 给了 `since` / `until` 就按窗口补齐（含两端）；
        - 都没给（「全部」）就补在首个到末个观测小时之间；
        - 跨度超过 `MAX_DENSE_HOURS` 时不补（几万个空点只会拖垮图表），
          前端仍按时间戳定位，只是空档不插点。

        缺失桶的取值按指标语义区分（`_empty_value`）：请求数 / token 补 0（没流量
        就是 0），均值与比率补 `None`（「没有样本」不是 0，前端据此断线）。

        每个点还带一个 `__total` 键 = **全渠道合计**（单独按小时聚合一次）。为什么
        不在前端把各渠道值相加：均值（耗时 / 首字）与比率（成功率 / 命中率）相加
        没有意义，必须用同一小时的 sum/count 重新相除。前端「总量」视图读这个键。
        """
        where, params = self._where(username=username, since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")
        rows = self._db.connect().execute(
            f"""
            SELECT hour_utc, provider,{_METRIC_AGGREGATES}
            FROM usage_hourly {where}
            GROUP BY hour_utc, provider
            ORDER BY hour_utc
            """, params).fetchall()
        # 全渠道合计：均值/比率必须重新相除，不能把各渠道的值相加
        total_rows = self._db.connect().execute(
            f"""
            SELECT hour_utc,{_METRIC_AGGREGATES}
            FROM usage_hourly {where}
            GROUP BY hour_utc
            ORDER BY hour_utc
            """, params).fetchall()
        raw: dict[int, dict[str, Any]] = {}
        totals: dict[int, Any] = {}
        providers: set[str] = set()
        for row in rows:
            hour = row["hour_utc"]
            provider = row["provider"]
            providers.add(provider)
            raw.setdefault(hour, {})[provider] = self._metric_value(metric, row)
        for row in total_rows:
            totals[row["hour_utc"]] = self._metric_value(metric, row)
        # 渠道按名排序：同一批点的键顺序稳定（与渠道注册顺序无关）
        ordered = sorted(providers)
        empty = self._empty_value(metric)
        return [
            {"hour": hour,
             "__total": totals.get(hour, empty),
             **{p: raw.get(hour, {}).get(p, empty) for p in ordered}}
            for hour in self._dense_hours(raw, since=since, until=until)
        ]

    @staticmethod
    def _dense_hours(observed: dict[int, Any], *, since: int | None,
                     until: int | None) -> list[int]:
        """补齐小时桶（见 `timeline` 文档）。跨度超限时退回已观测的小时。"""
        if not observed:
            return []
        hours = sorted(observed)
        start = (since // 3600) * 3600 if since is not None else hours[0]
        if until is not None:
            # 半开区间：until 整点本身不算（与 `_where` 的 `< until` 一致）
            end = ((until - 1) // 3600) * 3600 if until > start else start
        else:
            end = hours[-1]
        # 走到这里必有 end ≥ start：观测到的小时都落在 [since, until) 内，
        # 首末观测小时天然被两端夹住，无需再判空
        count = (end - start) // 3600 + 1
        if count > MAX_DENSE_HOURS:
            return hours
        return [start + index * 3600 for index in range(count)]

    @staticmethod
    def _empty_value(metric: str) -> Any:
        """缺失小时桶的取值：可加量补 0，均值 / 比率 / 成本补 None（断线）。"""
        return 0 if METRIC_KINDS.get(metric) in ("sum_requests", "sum_tokens") else None


    def events(self, *, username: str | None = None, since: int | None = None,
               until: int | None = None,
               before: int | None = None, limit: int = 50,
               include_credential_name: bool = False, sort: str | None = None,
               order: str | None = None, offset: int = 0,
               provider: str | None = None, model: str | None = None,
               credential_id: str | None = None,
               ok: int | None = None) -> dict[str, Any]:
        """逐请求明细（默认新→旧，rowid 游标分页）。明细仅保留 90 天。

        **两种分页模式**：

        - 默认（`sort=time` 降序、`offset=0`）：rowid 即插入序，稳定且可比大小，
          游标翻页不漏不重；返回 `next_before` 供下一页取「rowid 更小」的记录，
          null 表示到底。
        - 按其它列排序、或时间升序、或显式给 `offset`：改用 `LIMIT/OFFSET` 分页，
          额外返回 `total`（总条数）供前端算页数；`next_before` 恒为 null。

        之所以分两套：任意列排序与「rowid 游标」不兼容（游标只在按 rowid 走时
        成立）。明细只留 90 天，offset 的深翻页成本可接受。

        `provider` / `model` / `credential_id`：钻取过滤（分组表点行 → 只看该
        渠道/模型/凭证的明细）。三者独立、可叠加，`AND` 语义。

        `ok`：只看成功（1）/ 只看失败（0）的明细。失败归因卡片点行即带 `ok=0`
        下钻——否则「失败 12 次」点进去会看到同一组的成功记录，对不上账。

        `tps` / `tps_visible`：逐请求速度（tok/s，总产出 / 正文）。规则与分组表
        同一套（`_routed_guard`），但**只在路由口径成立时给值**：逐行没有相邻
        样本摊平首字等待，不回落估算口径。不合格的行给 None（前端显示 —）。

        `include_credential_name`：凭证昵称取自**全局共享池**（常含邮箱/手机），
        只对 admin/operator 返回；viewer 即便只看自己的记录也不该看到池里他人
        凭证的昵称。
        """
        key, desc = parse_sort_order(sort, order, EVENT_SORT_COLUMNS,
                                     default_key="time", default_desc=True)
        cursor_mode = key == "time" and desc and offset == 0
        clause = sql_order(sort, order, EVENT_SORT_COLUMNS, default_key="time",
                           default_desc=True, tiebreak="e.rowid")
        name_expr = ("NULLIF(c.nickname, '')" if include_credential_name else "NULL")
        columns = (
            "e.rowid, e.ts, e.username, e.provider, e.credential_id, e.model, "
            "e.ok, e.error_type, e.input_tokens, e.output_tokens, "
            "e.reasoning_tokens, e.cached_tokens, e.credit, e.credit_estimated, "
            "e.cost_usd, e.cost_cny, e.latency_ms, "
            f"e.ttfb_ms, e.request_id, {_ROW_SPEED_VISIBLE} AS tps_visible, "
            f"{_ROW_SPEED_TOTAL} AS tps, {name_expr} AS credential_name"
        )
        filters = {"provider": provider, "model": model, "credential_id": credential_id,
                   "ok": ok}
        if cursor_mode:
            where, params = self._where(username=username, since=since, until=until,
                                        before=before, alias="e.", **filters)
            rows = self._db.connect().execute(
                f"""
                SELECT {columns}
                FROM usage_events e
                LEFT JOIN credentials c ON c.id = e.credential_id
                {where}
                ORDER BY {clause} LIMIT ?
                """, [*params, limit + 1]).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            return {
                "events": [dict(row) for row in page],
                "next_before": page[-1]["rowid"] if has_more and page else None,
                "total": None,
            }
        # offset 模式：before 不参与（游标语义仅对 rowid 降序成立）
        where, params = self._where(username=username, since=since, until=until,
                                    alias="e.", **filters)
        total = self._db.connect().execute(
            f"SELECT COUNT(*) AS n FROM usage_events e {where}", params).fetchone()["n"]
        rows = self._db.connect().execute(
            f"""
            SELECT {columns}
            FROM usage_events e
            LEFT JOIN credentials c ON c.id = e.credential_id
            {where}
            ORDER BY {clause} LIMIT ? OFFSET ?
            """, [*params, limit, max(0, offset)]).fetchall()
        return {
            "events": [dict(row) for row in rows],
            "next_before": None,
            "total": int(total),
        }

    def export_events(self, *, username: str | None = None, since: int | None = None,
                      until: int | None = None,
                      include_credential_name: bool = False,
                      provider: str | None = None, model: str | None = None,
                      credential_id: str | None = None,
                      ok: int | None = None,
                      limit: int = EXPORT_MAX_ROWS) -> list[dict[str, Any]]:
        """导出用明细（旧→新，按 rowid 升序），最多 `limit + 1` 条。

        与 `events()` 的差异：固定**升序**（导出文件从头读起更符合直觉）、不做
        分页、不带 `rowid` 语义。多取一条供调用方判断是否被截断（超上限时明确
        告知用户「收窄条件」，而不是静默给一份不全的文件）。

        `ok`：与 `events()` 同义（失败归因下钻后导出，导出的就该是同一批失败）。
        """
        where, params = self._where(
            username=username, since=since, until=until, alias="e.",
            provider=provider, model=model, credential_id=credential_id, ok=ok)
        name_expr = ("NULLIF(c.nickname, '')" if include_credential_name else "NULL")
        rows = self._db.connect().execute(
            f"""
            SELECT e.ts, e.username, e.provider, e.credential_id, e.model,
                   e.ok, e.error_type, e.input_tokens, e.output_tokens,
                   e.reasoning_tokens, e.cached_tokens, e.credit, e.credit_estimated,
                   e.cost_usd, e.cost_cny, e.ttfb_ms, e.latency_ms, e.request_id,
                   {_ROW_SPEED_VISIBLE} AS tps_visible, {_ROW_SPEED_TOTAL} AS tps,
                   {name_expr} AS credential_name
            FROM usage_events e
            LEFT JOIN credentials c ON c.id = e.credential_id
            {where}
            ORDER BY e.rowid ASC LIMIT ?
            """, [*params, limit + 1]).fetchall()
        return [dict(row) for row in rows]

    def errors(self, *, username: str | None = None, since: int | None = None,
               until: int | None = None, limit: int = ERROR_MAX_GROUPS,
               now: int | None = None) -> list[dict[str, Any]]:
        """失败归因：按 `(provider, model, error_type)` 分组计数 + 最近失败时刻。

        数据源是**明细表**（`usage_hourly` 没有 `error_type` 维度），故只覆盖明细
        保留期（90 天）；调用方必须给窗口，未给 `since` 时默认「最近 24 小时」，
        避免一次扫全表。

        `error_type` 为 NULL 的历史行（老库 / 未上报）单独成组：它们也是失败，
        藏起来会让「失败总数」对不上明细——前端把它显示成「失败」。

        `limit` 按失败数降序取前 N 组：归因卡片是「谁最该修」的排行榜，长尾
        组合（同渠道同模型的不同错误类型各算一组）容易把响应撑大。

        **`rescued`（换号救回数）**：同一用户同一 `request_id` 另有成功行的
        失败**行**数。换号重试是常态——`invalid_request` 在每次上游拒绝时都落
        一行，即使随后换了另一个渠道成功（实测 2026-10-23：qoder 53 次失败里
        36 次最终成功）。只看 `failures` 会把「已救回的尝试」读成「用户受损」，
        故单列下发：`failures - rescued` 是**未被救回的失败尝试数**（同一请求多
        次失败会计多次，不等于受损请求数）。救援判定**不叠加时间窗**：成功行
        可能落在窗口右界之后（同一请求的重试），按 `request_id` 全表关联，走
        `idx_usage_request`。

        **必须加 `s.username = e.username`**：`request_id` 取自入站
        `X-Request-ID`，客户端可伪造；只按 ID 关联会让别的用户的成功行把本用户
        的失败标成「已救回」，也会让传了 `username=` 的查询读到跨租户数据。

        `now` 可注入（测试确定性）：只在 `since` 缺省时用于推算默认窗口。
        """
        if since is None:
            since = (now if now is not None else int(time.time())) - ERROR_WINDOW_SECONDS
        where, params = self._where(username=username, since=since, since_col="ts",
                                    until=until, until_col="ts", ok=0, alias="e.")
        rows = self._db.connect().execute(
            f"""
            SELECT e.provider, e.model, e.error_type, COUNT(*) AS failures,
                   SUM(CASE WHEN EXISTS (
                       SELECT 1 FROM usage_events s
                       WHERE s.request_id = e.request_id
                         AND s.username = e.username AND s.ok = 1)
                   THEN 1 ELSE 0 END) AS rescued,
                   MAX(e.ts) AS last_ts
            FROM usage_events e {where}
            GROUP BY e.provider, e.model, e.error_type
            ORDER BY failures DESC, e.provider, e.model, e.error_type
            LIMIT ?
            """, [*params, max(1, limit)]).fetchall()
        return [{"provider": row["provider"], "model": row["model"],
                 "error_type": row["error_type"], "failures": int(row["failures"]),
                 "rescued": int(row["rescued"]),
                 "last_ts": int(row["last_ts"])} for row in rows]

    def model_timeline(self, *, username: str | None = None,
                       since: int | None = None, until: int | None = None,
                       top: int = 6,
                       metric: str = "requests") -> dict[str, Any]:
        """按小时的各模型指标趋势（usage_hourly 聚合）。

        Top N 模型按「请求数」排序（排序口径固定，与图表指标解耦）；
        曲线值按 metric 变化（语义见 `METRIC_KINDS`）。返回结构 `{models, points}`
        不变。时间轴与 `timeline` 同样按小时补齐（见其文档）。
        """
        where, params = self._where(username=username, since=since, since_col="hour_utc",
                                    until=until, until_col="hour_utc")
        rows = self._db.connect().execute(
            f"""
            SELECT hour_utc, model,{_METRIC_AGGREGATES}
            FROM usage_hourly {where}
            GROUP BY hour_utc, model
            ORDER BY hour_utc
            """, params).fetchall()
        totals: dict[str, int] = {}
        series: dict[str, dict[int, Any]] = {}
        observed: dict[int, dict[str, Any]] = {}
        for row in rows:
            hour, model = row["hour_utc"], row["model"]
            totals[model] = totals.get(model, 0) + row["requests"]
            series.setdefault(model, {})[hour] = self._metric_value(metric, row)
            observed.setdefault(hour, {})[model] = True
        # Top N 模型：按总量降序；总量相同按名字稳定排序
        top_models = sorted(totals, key=lambda m: (-totals[m], m))[:top]
        empty = self._empty_value(metric)
        points = [
            {"hour": hour, **{m: series.get(m, {}).get(hour, empty) for m in top_models}}
            for hour in self._dense_hours(observed, since=since, until=until)
        ]
        return {"models": top_models, "points": points}

    def percentile_timeline(self, *, username: str | None = None,
                            since: int | None = None, until: int | None = None,
                            metric: str = "latency",
                            now: int | None = None) -> dict[str, Any]:
        """按小时的分位时序（P50 中位数 + P95 上界），读明细表。

        延迟图只画均值会盖住长尾：池级均值 10 s 时 P95 可能 40 s，而「少数请求
        卡住」正是最该看见的故障形态。故一张图给「中位数线 + P95 上界带」。

        **只能读明细表**（`usage_events`，仅 90 天）：小时汇总只存 sum/count，
        数学上无法还原分布。`metric` 只接受 `latency` / `ttfb`（其余回退
        `latency`）；口径与分组表分位一致——只统计成功请求、nearest-rank
        （`⌈pct·n/100⌉`）、NULL 不参与排名。

        `since` 缺省时强制「最近 24 小时」（与 `errors` 同口径）：这条 SQL 带
        `ROW_NUMBER() OVER (PARTITION BY 小时)`，无窗口时会对 90 天明细全表做
        窗口函数排序；**任意登录用户（含 viewer）**都能反复触发，是现成的 DB
        CPU DoS。`now` 可注入（测试确定性），只在缺省时用于推算窗口。

        返回 `{"points": [{hour, p50, p95, samples}], "metric": ..., "window_days": 90}`；
        缺失小时补 None（前端断线，不画成 0）。`samples` 是该小时的样本数，
        前端可据此说明「该点样本很少」。
        """
        if since is None:
            since = (now if now is not None else int(time.time())) - ERROR_WINDOW_SECONDS
        column = "ttfb_ms" if metric == "ttfb" else "latency_ms"
        where, params = self._where(username=username, ok=1, since=since, until=until)
        clause = f"AND {where[6:]}" if where else ""
        rows = self._db.connect().execute(
            f"""
            SELECT hour_utc,
                   MAX(CASE WHEN rn = (50 * n + 99) / 100 THEN val END) AS p50,
                   MAX(CASE WHEN rn = (95 * n + 99) / 100 THEN val END) AS p95,
                   MAX(n) AS samples
            FROM (
                SELECT (ts / 3600) * 3600 AS hour_utc, {column} AS val,
                       ROW_NUMBER() OVER (
                           PARTITION BY (ts / 3600) * 3600 ORDER BY {column}
                       ) AS rn,
                       COUNT({column}) OVER (
                           PARTITION BY (ts / 3600) * 3600
                       ) AS n
                FROM usage_events
                WHERE {column} IS NOT NULL {clause}
            ) AS ranked
            GROUP BY hour_utc
            ORDER BY hour_utc
            """, params).fetchall()
        observed = {row["hour_utc"]: True for row in rows}
        by_hour = {row["hour_utc"]: row for row in rows}
        points = []
        for hour in self._dense_hours(observed, since=since, until=until):
            row = by_hour.get(hour)
            points.append({
                "hour": hour,
                "p50": row["p50"] if row else None,
                "p95": row["p95"] if row else None,
                "samples": row["samples"] if row else 0,
            })
        return {"points": points, "metric": column.removesuffix("_ms"),
                "window_days": 90}
