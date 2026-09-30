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


def backfill_estimated_credit(db) -> int:
    """补齐 / 重算 trae 明细的推算积分，返回更新条数（0=无需处理）。

    处理「credit 为空」或「credit_estimated=1」的行；后者在单价表调整后重算。
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
                output_tokens=row["output_tokens"], cached_tokens=row["cached_tokens"])
            if credit is None:
                continue
            if row["credit"] is not None and row["credit"] == credit:
                continue  # 推算值已是最新，跳过（保证幂等、不虚增 updated）
            conn.execute(
                "UPDATE usage_events SET credit = ?, credit_estimated = 1 "
                "WHERE rowid = ?", (credit, row["rowid"]))
            updated += 1
    return updated