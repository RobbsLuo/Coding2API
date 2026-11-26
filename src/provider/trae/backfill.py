"""TRAE 历史明细的推算积分回填 / 重算。

上游 `token_usage` 帧不含单请求积分，`credit` 一直是 NULL；本服务改为按官方
单价推算后（见 `pricing`），**新**请求落库前会补上推算值，但**改动前**已落库的
历史明细仍是 NULL。`scripts/backfill_trae_credit.py` 用本函数补齐历史明细，
让统计页的 `≈` 覆盖全部时间范围；之后新请求走正常路径，不再需要回填。

处理 `provider='trae'` 且满足其一的行：
- `credit IS NULL`：从未推算过（历史明细）→ 补上；
- `credit_estimated = 1`：本服务推算的值 → **重算**。单价表 / 折扣调整后，
  这些旧推算值会过期，重跑即刷新到当前价（上游真值 `credit_estimated = 0`
  的行不动，保留上游给的数）。

模型未收录（`estimate_credit` 返回 None）的行保持原样，展示层仍显示 `—`。
**幂等**：重算后值不再变化，再次执行不产生更新（返回 0）。
"""

from __future__ import annotations

from . import pricing


def _effective_cached(cached_tokens, input_tokens, assumed_cache_ratio: float | None):
    """行内缓存命中数 → 定价用有效命中数。

    `cached_tokens is None` 表示上游未上报（TRAE 2026-10 起）。给了假定命中率
    就按 `input × ratio` 推算，与实时链路（`TraeClient`）同口径；否则保持
    None（`pricing.estimate_credit` 按 0 处理，即全价——宁可高估不低估）。
    """
    if cached_tokens is not None or assumed_cache_ratio is None:
        return cached_tokens
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool):
        return None
    if input_tokens < 0 or not (0 < assumed_cache_ratio <= 1):
        return None
    return min(int(input_tokens * assumed_cache_ratio), input_tokens)


def backfill_estimated_credit(db, *, assumed_cache_ratio: float | None = None) -> int:
    """补齐 / 重算 trae 明细的推算积分，返回更新条数（0=无需处理）。

    处理「credit 为空」或「credit_estimated=1」的行；后者在单价表调整后重算。
    `assumed_cache_ratio` 用于 `cached_tokens IS NULL`（上游未上报）的行：
    与实时链路一致地按假定命中率折算，避免历史行按全价高估。传 None 保持
    旧行为（按 0 命中）。
    `db` 只用到 `Database` 的 `transaction()` 形态（鸭子类型），便于测试与脚本
    注入裸连接适配器。调用方在返回值 > 0 时应重算小时汇总
    （`StatsCollector.rollup_hourly`），本函数只管明细，不碰汇总。
    """
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT rowid, model, input_tokens, output_tokens, cached_tokens, credit "
            "FROM usage_events WHERE provider = 'trae' "
            "AND (credit IS NULL OR credit_estimated = 1)"
        ).fetchall()
        updated = 0
        for row in rows:
            credit = pricing.estimate_credit(
                row["model"], input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cached_tokens=_effective_cached(
                    row["cached_tokens"], row["input_tokens"], assumed_cache_ratio))
            if credit is None:
                continue
            if row["credit"] is not None and row["credit"] == credit:
                continue  # 推算值已是最新，跳过（保证幂等、不虚增 updated）
            conn.execute(
                "UPDATE usage_events SET credit = ?, credit_estimated = 1 "
                "WHERE rowid = ?", (credit, row["rowid"]))
            updated += 1
    return updated