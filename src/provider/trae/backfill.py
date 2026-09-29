"""TRAE 历史明细的推算积分回填。

上游 `token_usage` 帧不含单请求积分，`credit` 一直是 NULL；本服务改为按官方
单价推算后（见 `pricing`），**新**请求落库前会补上推算值，但**改动前**已落库的
历史明细仍是 NULL。`scripts/backfill_trae_credit.py` 用本函数一次性补齐历史明细，
让统计页的 `≈` 覆盖全部时间范围；之后新请求走正常路径，不再需要回填。

只动 `provider='trae' AND credit IS NULL` 且模型在单价表里的行：
- 已有 credit 的行不碰（保留上游真值）；
- 模型未收录（`estimate_credit` 返回 None）的保持 NULL，展示层仍显示 `—`。

幂等：补过的行 credit 不再为 NULL，重复执行自然跳过。
"""

from __future__ import annotations

from . import pricing


def backfill_estimated_credit(db) -> int:
    """给历史 trae 明细补推算积分，返回更新条数（0=无需回填）。

    `db` 只用到 `Database` 的 `transaction()` 形态（鸭子类型），便于测试与脚本
    注入裸连接适配器。调用方在返回值 > 0 时应重算小时汇总
    （`StatsCollector.rollup_hourly`），本函数只管明细，不碰汇总。
    """
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT rowid, model, input_tokens, output_tokens, cached_tokens "
            "FROM usage_events WHERE provider = 'trae' AND credit IS NULL"
        ).fetchall()
        updated = 0
        for row in rows:
            credit = pricing.estimate_credit(
                row["model"], input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"], cached_tokens=row["cached_tokens"])
            if credit is None:
                continue
            conn.execute(
                "UPDATE usage_events SET credit = ?, credit_estimated = 1 "
                "WHERE rowid = ?", (credit, row["rowid"]))
            updated += 1
    return updated