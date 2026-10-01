#!/usr/bin/env python3
"""一次性把 CodeArts 历史数据由 token 口径折算成「积分」口径。

背景：CodeArts 上游余额是每日免费 **token** 池（千万量级），本服务原先直接按
token 落库；改为统一「积分」（1 积分 = 10000 token，每日池满额 = 1000 积分）后，
`parse_balance` / `_fill_estimated_credit` 会折算新数据，但**改动前**已落库的
历史额度与用量仍是 token，需一次性折算，否则统计与额度排序口径分叉。

折算范围（仅 provider='codearts'）：
  - credentials.quota_remaining / quota_total / quota_expiry_ladder / quota_packages
  - usage_events.credit
  - usage_hourly.credit_sum
  - credit_events.before / after / delta（属于 codearts 凭证的行）

**只能执行一次**：折算即除以 10000，重复执行会把已折算的值再除一遍。
脚本默认只预览、需 `--apply` 才写库，并在写库前自动备份。

用法：
    python3 scripts/convert_codearts_credit_unit.py                 # 预览，不写库
    python3 scripts/convert_codearts_credit_unit.py --apply         # 备份后写库
    python3 scripts/convert_codearts_credit_unit.py --db PATH ...   # 指定数据库
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
    convert_codearts_credit_unit,
    pending,
)

_LABELS = {
    "credentials": "凭证额度",
    "usage_events": "用量明细（usage_events.credit）",
    "usage_hourly": "小时汇总（usage_hourly.credit_sum）",
    "credit_events": "积分变动流水（credit_events）",
}


class _DbAdapter:
    """把裸连接包装成 Database 的形态（折算只用 transaction）。"""

    def __init__(self, conn) -> None:
        self._conn = conn

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
    parser = argparse.ArgumentParser(description="把 CodeArts 历史数据折算为积分")
    parser.add_argument("--db", help="SQLite 路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认只预览）")
    args = parser.parse_args(argv)

    from src.config import load_settings

    db_path = Path(args.db) if args.db else Path(load_settings().db_path)
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")

    database = Database(db_path)
    conn = database.connect()
    todo = pending(conn)
    if not any(todo.values()):
        print("没有需要折算的 CodeArts 历史数据")
        return 0
    for key, count in todo.items():
        print(f"  {_LABELS[key]:<32} x{count}")
    print("将按 1 积分 = 10000 token 折算以上数据")

    if not args.apply:
        print("预览模式，未修改数据库；确认无误后加 --apply 执行")
        return 0

    target = backup(db_path)
    updated = convert_codearts_credit_unit(_DbAdapter(conn))
    for key, count in updated.items():
        print(f"  {_LABELS[key]:<32} 已折算 {count}")
    print(f"完成（备份: {target}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
