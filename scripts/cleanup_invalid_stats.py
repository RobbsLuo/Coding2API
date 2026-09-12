#!/usr/bin/env python3
"""清理跨上游白打产生的 invalid_request 统计记录。

背景：executor 旧版对扁平模型名（不带 @provider）会让双上游都真实打一次
请求；请求独有模型时非归属上游会拒绝，留下 ok=0 / error_type=invalid_request
的脏明细（上游侧留请求记录、统计多一条无效行）。

判定（保守）：一组 (provider, model) 的 invalid_request 明细，仅当
  - 该上游对该模型（按小写归一）从无成功记录，且
  - 另一上游对该模型有成功记录
时认定为白打，整组删除。两边都没成功过的模型名（如手滑输错的请求）保留；
本上游有成功记录的（偶发拒绝）保留；非 invalid_request 的错误保留。

汇总同步：usage_hourly 由明细全量重算（StatsCollector.rollup_hourly 幂等），
删除后重跑 rollup，再清掉不再有明细对应的小时行，保证明细与汇总严格一致。

用法：
    python3 scripts/cleanup_invalid_stats.py                 # 预览，不写库
    python3 scripts/cleanup_invalid_stats.py --apply         # 备份后删除
    python3 scripts/cleanup_invalid_stats.py --db PATH ...   # 指定数据库
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.conn import Database  # noqa: E402
from src.stats.collector import StatsCollector  # noqa: E402


def stale_groups(conn) -> list[tuple[str, str, int]]:
    """白打组列表：(provider, model 原样, 待删条数)。"""
    success: dict[tuple[str, str], int] = {}
    for provider, model, count in conn.execute(
            "SELECT provider, LOWER(model), COUNT(*) FROM usage_events "
            "WHERE ok = 1 GROUP BY provider, LOWER(model)"):
        success[(provider, model)] = count
    stale: list[tuple[str, str, int]] = []
    for provider, model, count in conn.execute(
            "SELECT provider, model, COUNT(*) FROM usage_events "
            "WHERE ok = 0 AND error_type = 'invalid_request' "
            "GROUP BY provider, model"):
        if success.get((provider, model.lower()), 0) == 0 and any(
                ok_count for (pid, lower), ok_count in success.items()
                if pid != provider and lower == model.lower()):
            stale.append((provider, model, count))
    return stale


def delete_stale(conn, stale: list[tuple[str, str, int]]) -> int:
    """删除白打明细，返回总条数。"""
    total = 0
    for provider, model, _count in stale:
        cursor = conn.execute(
            "DELETE FROM usage_events WHERE provider = ? AND model = ? "
            "AND ok = 0 AND error_type = 'invalid_request'", (provider, model))
        total += cursor.rowcount
    return total


def sync_hourly(conn) -> None:
    """小时汇总与明细对齐：全量重算 + 清孤儿行。"""
    StatsCollector(_DbAdapter(conn)).rollup_hourly()
    conn.execute(
        "DELETE FROM usage_hourly WHERE NOT EXISTS ("
        "  SELECT 1 FROM usage_events e"
        "  WHERE (e.ts / 3600) * 3600 = usage_hourly.hour_utc"
        "    AND e.username = usage_hourly.username"
        "    AND e.provider = usage_hourly.provider"
        "    AND e.model = usage_hourly.model)")


class _DbAdapter:
    """把裸连接包装成 Database.connect() 的形态（rollup 只用这一个方法）。"""

    def __init__(self, conn) -> None:
        self._conn = conn

    def connect(self):
        return self._conn


def backup(db_path: Path) -> Path:
    """删除前备份主库文件到同目录（.bak-<时间戳>），失败即中止。"""
    target = db_path.with_name(f"{db_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    source = sqlite3.connect(str(db_path))
    try:
        source.backup(sqlite3.connect(str(target)))
    finally:
        source.close()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理跨上游白打的 invalid_request 统计")
    parser.add_argument("--db", help="SQLite 路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--apply", action="store_true",
                        help="实际删除（默认只预览）")
    args = parser.parse_args(argv)

    from src.config import load_settings

    db_path = Path(args.db) if args.db else Path(load_settings().db_path)
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")

    database = Database(db_path)
    conn = database.connect()
    stale = stale_groups(conn)
    if not stale:
        print("没有需要清理的记录")
        return 0
    for provider, model, count in sorted(stale):
        print(f"  {provider:<10} {model:<24} x{count}")
    total = sum(count for _, _, count in stale)
    print(f"共 {total} 条白打明细（{len(stale)} 组）")

    if not args.apply:
        print("预览模式，未修改数据库；确认无误后加 --apply 执行")
        return 0

    target = backup(db_path)
    delete_stale(conn, stale)
    sync_hourly(conn)
    conn.commit()
    print(f"已删除 {total} 条明细，小时汇总已同步（备份: {target}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
