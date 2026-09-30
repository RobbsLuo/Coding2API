#!/usr/bin/env python3
"""一次性补齐 TRAE 历史明细的推算积分，并重算小时汇总。

背景：TRAE 上游 `token_usage` 帧不含单请求积分，`credit` 一直是 NULL；本服务
改为按官方单价推算后（`src/provider/trae/pricing.py`），**新**请求落库前会补上
推算值，但**改动前**已落库的历史明细仍是 NULL。本脚本把历史明细一次性补齐，
让统计页的 `≈` 覆盖全部时间范围。

判定（保守）：
  - 处理 `provider='trae'` 且 `credit IS NULL`（历史明细，补齐）或
    `credit_estimated=1`（本服务旧推算值，单价表调整后重算）的行；
  - 模型在单价表内才推算，未收录的保持原样（展示层仍显示 `—`）；
  - 上游真值（`credit_estimated=0`）不覆盖；值未变化的行也不重写。

补完明细后全量重算 `usage_hourly`（幂等 upsert），使汇总的 `credit_sum` /
`credit_estimated_known` 与明细一致。汇总永久保留、明细只留 90 天，所以更早
的小时汇总无法再推算（那时明细已不在）。

用法：
    python3 scripts/backfill_trae_credit.py                 # 预览，不写库
    python3 scripts/backfill_trae_credit.py --apply         # 备份后写库
    python3 scripts/backfill_trae_credit.py --db PATH ...   # 指定数据库
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.conn import Database  # noqa: E402
from src.provider.trae import pricing  # noqa: E402
from src.provider.trae.backfill import backfill_estimated_credit  # noqa: E402
from src.stats.collector import StatsCollector  # noqa: E402


def pending(conn) -> list[tuple[str, int, float, int]]:
    """待处理行列表：(model, 条数, 合计推算积分, 其中重算条数)，只含模型已收录的行。

    「重算」指 `credit_estimated = 1` 的旧推算值（单价表调整后需刷新）；
    其余为 `credit IS NULL` 的历史明细。
    """
    counts: dict[str, list] = {}
    for model, input_tokens, output_tokens, cached_tokens, credit, estimated in conn.execute(
            "SELECT model, input_tokens, output_tokens, cached_tokens, credit, "
            "credit_estimated FROM usage_events WHERE provider = 'trae' "
            "AND (credit IS NULL OR credit_estimated = 1)"):
        new = pricing.estimate_credit(
            model, input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=cached_tokens)
        if new is None:
            continue
        if credit is not None and credit == new:
            continue  # 已是最新，无需处理
        entry = counts.setdefault(model, [0, 0.0, 0])
        entry[0] += 1
        entry[1] += new
        if estimated:
            entry[2] += 1
    return [(model, c, t, r) for model, (c, t, r) in sorted(counts.items())]


class _DbAdapter:
    """把裸连接包装成 Database 的形态（rollup 只用 connect/transaction）。"""

    def __init__(self, conn) -> None:
        self._conn = conn

    def connect(self):
        return self._conn

    @contextmanager
    def transaction(self):
        """与 Database.transaction 同语义：正常提交、异常回滚。"""
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()


def backup(db_path: Path) -> Path:
    """写库前备份主库文件到同目录（.bak-<时间戳>），失败即中止。"""
    target = db_path.with_name(f"{db_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    source = sqlite3.connect(str(db_path))
    try:
        source.backup(sqlite3.connect(str(target)))
    finally:
        source.close()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补齐 TRAE 历史明细的推算积分")
    parser.add_argument("--db", help="SQLite 路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--apply", action="store_true",
                        help="实际写库（默认只预览）")
    args = parser.parse_args(argv)

    from src.config import load_settings

    db_path = Path(args.db) if args.db else Path(load_settings().db_path)
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")

    database = Database(db_path)
    conn = database.connect()
    todo = pending(conn)
    if not todo:
        print("没有需要处理的 TRAE 历史明细")
        return 0
    for model, count, total, recompute in todo:
        note = f"（其中重算 {recompute}）" if recompute else ""
        print(f"  {model:<28} x{count:<5} ≈{total:.4f}{note}")
    total_rows = sum(count for _, count, _, _ in todo)
    total_credit = sum(credit for _, _, credit, _ in todo)
    print(f"共 {total_rows} 条明细待处理，合计 ≈{total_credit:.4f} 积分")

    if not args.apply:
        print("预览模式，未修改数据库；确认无误后加 --apply 执行")
        return 0

    target = backup(db_path)
    updated = backfill_estimated_credit(_DbAdapter(conn))
    StatsCollector(_DbAdapter(conn)).rollup_hourly()
    print(f"已处理 {updated} 条明细，小时汇总已重算（备份: {target}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())