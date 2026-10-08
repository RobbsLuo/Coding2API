#!/usr/bin/env python3
"""按**当前**价表与汇率一次性重算历史明细的费用，并重算小时汇总。

背景：费用在**写入时**定值落库（`usage_events.cost_usd` / `cost_cny`，见
`src/pricing.py` 与 `src/stats/collector.py`）；改动前已落库的历史明细两列是
NULL，统计页显示 `—`。本脚本用**当前**落盘价表（`DATA_DIR/model_prices.json`）
与**当前生效**汇率（热更覆盖 > env 默认）补齐 / 重算全部明细，让费用覆盖全部
时间范围。

口径提醒：历史汇率与当时的价表已不可得，所以这是「按今天口径重估」，不是还原
每笔请求发生时的真实花费。执行后新请求仍按各自写入时的汇率/价表定值。

行为与 `backfill_trae_credit.py` 一致：
  - 只覆盖「能定价」的行（模型已收录且上报了输入 token）；未收录的保持原样（显示 —）；
  - 值未变化的行不重写；**幂等**，重复执行无副作用；
  - 补完明细后全量重算 `usage_hourly`（幂等 upsert）；
  - 写库前自动备份主库文件。

用法：
    python3 scripts/backfill_cost.py                  # 预览，不写库
    python3 scripts/backfill_cost.py --apply          # 备份后写库
    python3 scripts/backfill_cost.py --db PATH        # 指定数据库（价表默认取同目录）
    python3 scripts/backfill_cost.py --data-dir DIR   # 指定价表快照目录
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Settings, load_settings  # noqa: E402
from src.db.conn import Database  # noqa: E402
from src.db.repo import RuntimeSettingsRepository  # noqa: E402
from src.pricing import load_prices  # noqa: E402
from src.runtime_settings import load_runtime_settings  # noqa: E402
from src.stats.backfill import pending_cost, recompute_costs  # noqa: E402
from src.stats.collector import StatsCollector  # noqa: E402


def backup(db_path: Path) -> Path:
    """写库前备份主库文件到同目录（.bak-<时间戳>），失败即中止。"""
    target = db_path.with_name(f"{db_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    source = sqlite3.connect(str(db_path))
    try:
        source.backup(sqlite3.connect(str(target)))
    finally:
        source.close()
    return target


def effective_rate(database: Database) -> float:
    """当前生效汇率：DB 热更覆盖 > env/.env 默认（与运行时同口径）。

    `--db` 显式指定时可能不在有 `.env` 的环境里跑；`load_settings()` 失败就退回
    最小底座读默认值，不让脚本因为一个非必填配置而整体失败。
    """
    try:
        base = load_settings()
    except Exception:  # noqa: BLE001 - 脚本容错：拿不到 env 就用默认汇率
        base = Settings(_env_file=None, app_secret="backfill-cost-script")
    return load_runtime_settings(base, RuntimeSettingsRepository(database)).usd_cny_rate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按当前价表与汇率重算历史明细费用")
    parser.add_argument("--db", help="SQLite 路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--data-dir", help="价表快照目录（默认数据库所在目录）")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认只预览）")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else Path(load_settings().db_path)
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")

    data_dir = Path(args.data_dir) if args.data_dir else db_path.parent
    table = load_prices(str(data_dir))
    if not table:
        print(f"价表为空（{data_dir}/model_prices.json 缺失 / 过期 / 损坏）")
        print("先让服务刷新价表（或检查 --data-dir），再重跑本脚本")
        return 1

    database = Database(db_path)
    rate = effective_rate(database)
    print(f"价表 {len(table)} 个模型，汇率 1 USD = {rate} CNY")

    todo = pending_cost(database.connect(), table, rate)
    if not todo:
        print("没有需要处理的费用明细")
        return 0
    for model, count, total in todo:
        print(f"  {model:<28} x{count:<5} ≈¥{total:.4f}")
    total_rows = sum(count for _, count, _ in todo)
    total_cny = sum(total for _, _, total in todo)
    print(f"共 {total_rows} 条明细待处理，合计 ≈¥{total_cny:.4f}")

    if not args.apply:
        print("预览模式，未修改数据库；确认无误后加 --apply 执行")
        return 0

    target = backup(db_path)
    updated = recompute_costs(database, table, rate)
    StatsCollector(database).rollup_hourly()
    print(f"已重算 {updated} 条明细，小时汇总已重算（备份: {target}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
