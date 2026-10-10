#!/usr/bin/env python3
"""按明细一次性重算历史小时汇总（`usage_hourly`），补齐/修正历史分组数据。

背景：统计页的**按渠道 / 按模型 / 按用户**三个分组维度都读 `usage_hourly`
（永久保留），逐请求明细只留 90 天。汇总缺失或过时刻意「重算一次历史」时，
分组数据就会偏小——例如老库升级前落库的明细没被 rollup 覆盖，或补明细
（成本 / credit 回填）后忘了重算汇总。**明细一旦被清理就不可逆**，所以本脚本
的价值在于「趁明细还在，赶紧补」。

行为：
  - **默认预览**：报告明细与汇总的缺口（缺行 / 数值不符）与两侧时间跨度，
    并提示「明细已过期、无法还原」的区间；不写库；
  - `--apply`：先自动备份主库文件，再全量重算（`rollup_hourly` 整行 REPLACE
    语义、幂等，重复执行无副作用）；
  - 重算是**全量**的：诊断只回答「有没有行需要重建」，credit / cost 等列一律
    按当前明细重算，不做增量。

注意：早于明细保留期（默认 90 天）且汇总也没有的小时**无法还原**——明细已删。
这类缺口本脚本如实报告，不猜数。

用法：
    python3 scripts/rollup_hourly.py                  # 预览，不写库
    python3 scripts/rollup_hourly.py --apply          # 备份后写库
    python3 scripts/rollup_hourly.py --db PATH        # 指定数据库
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db.conn import Database  # noqa: E402
from src.stats.backfill import pending_hourly  # noqa: E402
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


def _stamp(value: int | None) -> str:
    """epoch 秒 → 可读时间；None 显示为 —。"""
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _span_text(span: tuple[int, int] | None) -> str:
    return "—" if span is None else f"{_stamp(span[0])} ~ {_stamp(span[1])}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按明细重算历史小时汇总（补齐分组统计）")
    parser.add_argument("--db", help="SQLite 路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认只预览）")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else Path(load_settings().db_path)
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")

    database = Database(db_path)
    report = pending_hourly(database.connect())
    print(f"明细 {report['events']} 条，时间 {_span_text(report['events_span'])}")
    print(f"汇总 {report['hourly']} 行，时间 {_span_text(report['hourly_span'])}")
    gaps = report["missing"] + report["mismatched"]
    if gaps == 0:
        print("小时汇总与明细一致，无需重算")
        return 0
    print(f"待重算：缺汇总行 {report['missing']} 个分组键，数值不符 {report['mismatched']} 个")
    for hour, username, provider, model, requests, missing in report["samples"]:
        kind = "缺汇总行" if missing else "数值不符"
        print(f"  {_stamp(hour)}  {username:<12} {provider:<12} {model:<24} "
              f"{requests:>6} 条（{kind}）")

    events_span, hourly_span = report["events_span"], report["hourly_span"]
    if events_span and hourly_span and hourly_span[0] > events_span[0]:
        # 明细起点早于汇总起点：这段既没汇总、也可能已过保留期被清理
        print(f"⚠️  汇总起点（{_stamp(hourly_span[0])}）晚于明细起点"
              f"（{_stamp(events_span[0])}）：这段明细可能已被清理，无法还原")

    if not args.apply:
        print("预览模式，未修改数据库；确认无误后加 --apply 执行")
        return 0

    target = backup(db_path)
    rolled = StatsCollector(database).rollup_hourly()
    print(f"已重算 {rolled} 行小时汇总（备份: {target}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())