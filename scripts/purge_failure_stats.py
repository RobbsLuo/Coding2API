#!/usr/bin/env python3
"""清理失败明细（`usage_events.ok = 0`），即「失败归因」面板的数据源。

背景：失败归因（`GET /api/stats/errors`）直接聚合**明细表**里 `ok=0` 的行，
没有独立表可清。想让面板归零，只能删明细。本脚本与
`cleanup_invalid_stats.py` 的区别：后者只删「跨上游白打」这一种保守判定，
本脚本按**显式过滤条件**删，用于「自己压测/联调打出来的一堆失败」这类
确认过没有诊断价值的数据。

判定（必须显式给条件之一，避免误删全部历史）：
  - `--provider`     只删该渠道
  - `--error-type`   只删该错误类型
  - `--days N`       只删最近 N 天
  - `--all`          无条件删全部失败明细（最危险，需 `--apply --yes` 同时确认，
                     且不能再叠加 `--provider` / `--error-type` / `--days`）
成功行（`ok=1`）一律不动。

汇总同步：`usage_hourly` 由明细全量重算（`StatsCollector.rollup_hourly`
幂等），删除后再清掉**本次删除覆盖的小时**里不再有明细对应的孤儿行，保证明细
与汇总严格一致。只清这些小时是关键：小时汇总永久保留，而 90 天前的明细已被
`purge_expired` 删掉，无条件清孤儿会把历史汇总一起删掉。注意副作用：失败明细
删掉后，该小时的 `requests` 会下降、成功率会「变好」——这是删数据的必然结果，
不是统计口径变了。

用法：
    python3 scripts/purge_failure_stats.py --provider qoder       # 预览
    python3 scripts/purge_failure_stats.py --provider qoder --apply
    python3 scripts/purge_failure_stats.py --all --apply --yes   # 全部失败明细
    python3 scripts/purge_failure_stats.py --db PATH ...
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._common import backup, hour_range, sync_hourly  # noqa: E402
from src.db.conn import Database  # noqa: E402


def _filters(*, provider: str | None, error_type: str | None,
             days: int | None, now: int | None) -> tuple[str, list[object]]:
    """构造 `ok=0` 之外的过滤条件（只允许 AND 收窄）。"""
    clauses = ["ok = 0"]
    params: list[object] = []
    if provider is not None:
        clauses.append("provider = ?")
        params.append(provider)
    if error_type is not None:
        clauses.append("error_type = ?")
        params.append(error_type)
    if days is not None:
        clauses.append("ts >= ?")
        params.append((now if now is not None else int(time.time())) - days * 86400)
    return " AND ".join(clauses), params


def failure_groups(conn, *, provider: str | None = None,
                   error_type: str | None = None, days: int | None = None,
                   now: int | None = None) -> list[tuple[str, str, str | None, int, int]]:
    """待删分组：`(provider, model, error_type, 条数, 最近时刻)`，按条数降序。"""
    where, params = _filters(provider=provider, error_type=error_type,
                             days=days, now=now)
    rows = conn.execute(
        f"SELECT provider, model, error_type, COUNT(*) AS n, MAX(ts) AS last_ts "
        f"FROM usage_events WHERE {where} "
        f"GROUP BY provider, model, error_type ORDER BY n DESC, provider, model",
        params).fetchall()
    return [(row[0], row[1], row[2], int(row[3]), int(row[4])) for row in rows]


def delete_groups(conn, *, provider: str | None = None,
                  error_type: str | None = None, days: int | None = None,
                  now: int | None = None) -> int:
    """按同一组条件删除失败明细，返回删除条数。"""
    where, params = _filters(provider=provider, error_type=error_type,
                             days=days, now=now)
    return conn.execute(f"DELETE FROM usage_events WHERE {where}", params).rowcount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理失败明细（失败归因数据源）")
    parser.add_argument("--db", help="SQLite 路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--provider", help="只删该渠道的失败明细")
    parser.add_argument("--error-type", dest="error_type",
                        help="只删该错误类型的失败明细")
    parser.add_argument("--days", type=int, help="只删最近 N 天的失败明细")
    parser.add_argument("--all", action="store_true",
                        help="删除全部失败明细（必须再叠加 --yes）")
    parser.add_argument("--yes", action="store_true",
                        help="确认执行 --all（没有它只预览/拒绝）")
    parser.add_argument("--apply", action="store_true", help="实际删除（默认只预览）")
    args = parser.parse_args(argv)

    if not (args.all or args.provider or args.error_type or args.days):
        parser.error("必须给出 --provider / --error-type / --days / --all 之一")
    if args.days is not None and args.days <= 0:
        parser.error("--days 必须为正整数")
    if args.all and (args.provider or args.error_type or args.days):
        # --all 写着「无条件删全部」，叠加收窄条件会静默只删一部分
        parser.error("--all 不能与 --provider / --error-type / --days 叠加")
    if args.all and args.apply and not args.yes:
        parser.error("--all --apply 需要 --yes 二次确认")

    from src.config import load_settings

    db_path = Path(args.db) if args.db else Path(load_settings().db_path)
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")

    database = Database(db_path)
    conn = database.connect()
    groups = failure_groups(conn, provider=args.provider,
                            error_type=args.error_type, days=args.days)
    if not groups:
        print("没有需要清理的记录")
        return 0
    for provider, model, error_type, count, last_ts in groups:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_ts))
        print(f"  {provider:<14} {model:<24} {error_type or '(未上报)':<22} "
              f"x{count:<4} 最近 {stamp}")
    total = sum(group[3] for group in groups)
    print(f"共 {total} 条失败明细（{len(groups)} 组）")

    if not args.apply:
        print("预览模式，未修改数据库；确认无误后加 --apply 执行")
        return 0

    target = backup(db_path)
    # 先取区间再删：删完就查不出自己动过哪些小时了
    hours = hour_range(conn, *_filters(provider=args.provider,
                                       error_type=args.error_type, days=args.days,
                                       now=None))
    deleted = delete_groups(conn, provider=args.provider,
                            error_type=args.error_type, days=args.days)
    sync_hourly(conn, hours)
    conn.commit()
    print(f"已删除 {deleted} 条明细，小时汇总已同步（备份: {target}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
