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

汇总同步：`usage_hourly` 由明细全量重算（`StatsCollector.rollup_hourly` 幂等），
删除后再清掉**本次删除覆盖的小时**里不再有明细对应的孤儿行，保证明细与汇总
严格一致。只清这些小时：小时汇总永久保留，90 天前的明细已被 `purge_expired`
删掉，无条件清孤儿会把历史汇总一起删掉。

用法：
    python3 scripts/cleanup_invalid_stats.py                 # 预览，不写库
    python3 scripts/cleanup_invalid_stats.py --apply         # 备份后删除
    python3 scripts/cleanup_invalid_stats.py --db PATH ...   # 指定数据库
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._common import backup, sync_hourly  # noqa: E402
from src.db.conn import Database  # noqa: E402


def stale_groups(conn) -> list[tuple[str, str, int, int, int]]:
    """白打组列表：(provider, model 原样, 待删条数, 最早 ts, 最晚 ts)。"""
    success: dict[tuple[str, str], int] = {}
    for provider, model, count in conn.execute(
            "SELECT provider, LOWER(model), COUNT(*) FROM usage_events "
            "WHERE ok = 1 GROUP BY provider, LOWER(model)"):
        success[(provider, model)] = count
    stale: list[tuple[str, str, int, int, int]] = []
    for provider, model, count, low, high in conn.execute(
            "SELECT provider, model, COUNT(*), MIN(ts), MAX(ts) FROM usage_events "
            "WHERE ok = 0 AND error_type = 'invalid_request' "
            "GROUP BY provider, model"):
        if success.get((provider, model.lower()), 0) == 0 and any(
                ok_count for (pid, lower), ok_count in success.items()
                if pid != provider and lower == model.lower()):
            stale.append((provider, model, count, low, high))
    return stale


def delete_stale(conn, stale: list[tuple[str, str, int, int, int]]) -> int:
    """删除白打明细，返回总条数。"""
    total = 0
    for provider, model, _count, _low, _high in stale:
        cursor = conn.execute(
            "DELETE FROM usage_events WHERE provider = ? AND model = ? "
            "AND ok = 0 AND error_type = 'invalid_request'", (provider, model))
        total += cursor.rowcount
    return total


def _deleted_hours(stale: list[tuple[str, str, int, int, int]]) -> tuple[int, int]:
    """待删白打行覆盖的小时区间（整点对齐）；只让这些小时参与孤儿清理。"""
    return (min(group[3] for group in stale) // 3600 * 3600,
            max(group[4] for group in stale) // 3600 * 3600)


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
    for provider, model, count, _low, _high in sorted(stale):
        print(f"  {provider:<10} {model:<24} x{count}")
    total = sum(group[2] for group in stale)
    print(f"共 {total} 条白打明细（{len(stale)} 组）")

    if not args.apply:
        print("预览模式，未修改数据库；确认无误后加 --apply 执行")
        return 0

    target = backup(db_path)
    hours = _deleted_hours(stale)
    delete_stale(conn, stale)
    sync_hourly(conn, hours)
    conn.commit()
    print(f"已删除 {total} 条明细，小时汇总已同步（备份: {target}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
