#!/usr/bin/env python3
"""一次性补齐 CodeArts 福利模型历史明细的推算积分，并重算小时汇总。

背景：CodeArts v2 的 SSE **每个 chunk 都带 usage**，前置帧是 0/0 占位、只有收尾
帧是真值。`executor.complete` 曾取**第一个** USAGE 事件记账，于是福利模型
（每日 token 池按 1:1 折积分）的历史明细大量落了空 `credit`，统计页显示 `—`
而同一批请求明明消耗了池。该取末尾帧的 bug 已修（见 executor），**新**请求不再
复现；本脚本把改动前已落库的历史明细补齐。

判定（保守）：
  - 只处理 `provider='codearts'`、`ok=1`、`credit IS NULL` 的行；
  - 只处理能证明走过福利路由的模型（`backfill.benefit_models`：曾被记过推算积分
    的模型 ∪ `BENEFIT_SEED`）——内置模型走付费倍率、不扣每日池，不碰；
  - 两个 token 都为空的行不猜；
  - 已有 `credit` 的行（上游真值或旧推算值）一律不覆盖。

补完明细后全量重算 `usage_hourly`（幂等 upsert），使汇总的 `credit_sum` /
`credit_known` / `credit_estimated_known` 与明细一致。汇总永久保留、明细只留有限
天数，更早的小时汇总无法再推算（那时明细已不在）。

用法：
    python3 scripts/backfill_codearts_credit.py                 # 预览，不写库
    python3 scripts/backfill_codearts_credit.py --apply         # 备份后写库
    python3 scripts/backfill_codearts_credit.py --db PATH ...   # 指定数据库
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
from src.provider.codearts.backfill import (  # noqa: E402
    backfill_estimated_credit,
    benefit_models,
)
from src.provider.codearts.units import tokens_to_credits  # noqa: E402
from src.stats.collector import StatsCollector  # noqa: E402


def pending(conn) -> list[tuple[str, int, float]]:
    """待处理行列表：(model, 条数, 合计推算积分)，只含福利模型。

    与 `backfill_estimated_credit` 的跳过条件保持一致——预览说什么、--apply 就
    做什么，不会出现「预览 0 条、实际改了 N 条」。
    """
    known = benefit_models(conn)
    counts: dict[str, list] = {}
    for model, input_tokens, output_tokens in conn.execute(
            "SELECT model, input_tokens, output_tokens FROM usage_events "
            "WHERE provider = 'codearts' AND credit IS NULL AND ok = 1"):
        if (model or "").lower() not in known:
            continue
        if input_tokens is None and output_tokens is None:
            continue
        entry = counts.setdefault(model, [0, 0.0])
        entry[0] += 1
        entry[1] += (input_tokens or 0) + (output_tokens or 0)
    return [(model, count, tokens_to_credits(float(tokens)))
            for model, (count, tokens) in sorted(counts.items())]


class _DbAdapter:
    """把裸连接包装成 Database 的形态（回填 / rollup 只用 connect、transaction）。"""

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
    parser = argparse.ArgumentParser(description="补齐 CodeArts 福利模型历史明细的推算积分")
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
        print("没有需要处理的 CodeArts 历史明细")
        return 0
    for model, count, total in todo:
        print(f"  {model:<28} x{count:<5} ≈{total:.4f}")
    print(f"共 {sum(count for _, count, _ in todo)} 条明细待处理，"
          f"合计 ≈{sum(total for _, _, total in todo):.4f} 积分")

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