"""单请求成本估算（刊例价 × token，USD / 百万 token）。

**为什么单独一个模块**：成本不等于上游真实扣费，而是「按 token × 公开刊例价」
的估算，供管理台横向对比渠道成本。价表本身与抓取 / 快照逻辑在
`src/benchmarks.py`（OpenRouter 单一来源，价格与能力分一次抓完），这里只留
纯计算：`estimate_cost_usd` + `to_cny`。

**匹配口径**：与能力排行共用 `model_match`——**原样小写 id 先精确命中**
（既有行为不变），未命中才走候选键等值匹配（唯一命中才采用）。这样
OpenRouter 的 `z-ai/glm-5.3` 能对上本项目归一键 `glm-5.3`，而 `glm-5.3` 不会
误配到 `glm-5.3-flash`。

**成本写入时定值**：`estimate_cost_usd` + `to_cny` 在写明细那一刻算好落库，
历史行不随价表或汇率变化而重算（与 credit 推算同一心智模型）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 刊例价：`(input, output, cache_read)`，单位 USD / 百万 token。cache_read 上游
# 未声明时按 input 原价（成本就是这么算的：宁可不打折也不凭空把缓存 token 记成
# 免费）。价表本身由 `src/benchmarks.py` 从 OpenRouter 抓取构建，这里只留类型与
# 纯计算——统计侧（collector / backfill）只依赖本模块，不必知道数据来源。
PriceRecord = tuple[float, float, float]
PriceTable = dict[str, PriceRecord]


def estimate_cost_usd(
    table: Mapping[str, tuple[float, float, float]] | None,
    model: Any,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
) -> float | None:
    """按刊例价估算一次请求的美元成本；无法定价时返回 None。

    返回 None 的两种情形：价表里没有该模型，或没有输入 token 数（拿不到用量
    就无法估算——不能拿 0 冒充「免费」）。缓存 token 会被夹到 `[0, input]`，
    多报的缓存不会把未命中部分算成负数。
    """
    from .model_match import lookup

    if not isinstance(model, str) or not model:
        return None
    price = (table or {}).get(model.strip().lower())
    if price is None:
        price = lookup(table or {}, model)
    if price is None:
        return None
    if input_tokens is None:
        return None
    price_in, price_out, price_cached = price
    input_count = max(0, int(input_tokens))
    cached_count = 0 if cached_tokens is None else max(0, int(cached_tokens))
    cached_count = min(cached_count, input_count)
    uncached = input_count - cached_count
    output_count = 0 if output_tokens is None else max(0, int(output_tokens))
    return (uncached * price_in + cached_count * price_cached
            + output_count * price_out) / 1_000_000


def to_cny(cost_usd: float, rate: float) -> float:
    """美元成本按汇率折人民币；rate 非法（NaN / 非正）时按 1:1 兜底。

    汇率校验放这里是为了让落库值永远有限：坏汇率若产生 NaN 会污染整段聚合，
    且一进 SQLite 再也回不来（与 credit 拒绝 NaN 同理）。
    """
    numeric = float(cost_usd)
    try:
        exchange = float(rate)
    except (TypeError, ValueError):
        exchange = 0.0
    if not (exchange > 0) or exchange != exchange:  # 非正 / NaN
        exchange = 1.0
    return numeric * exchange
