#!/usr/bin/env python3
"""把 TRAE「上游未上报」的缓存 0 归一为 NULL（历史明细一次性修正）。

背景：TRAE 自 2026-10-24 前后起不再上报缓存命中（`cache_read_input_tokens`
恒 0、`prompt_cache_pool_record` 恒 null，但计费仍按缓存折扣）。修复前落库的
明细把这些 0 当成了「真实未命中」，于是：缓存命中率被稀释成 0%、输入 token
全部进命中率分母、积分 / 成本按全价高估。

本脚本按**数据自身**确定分界：最后一条 `cached_tokens > 0` 的 TRAE 明细之后，
所有 `cached_tokens = 0` 的行都是「上游未上报」，统一置为 NULL。NULL 的语义
与实时链路一致（见 `src/provider/trae/client.py`），报表会显示 `—` 而不是
假的 0%。

行为：
  - 默认预览：报出待修正条数 / 时间跨度 / 输入 token，不写库；
  - `--apply`：先备份主库，再置 NULL 并全量重算 `usage_hourly`（幂等）；
  - `--since`：库里没有可推断的分界时显式指定（epoch 秒或 ISO 日期）。

**不重算 credit / cost**：它们是由历史 token 推算的估算值，旧行按全价算高了。
需要修正时在跑完本脚本后依次执行（两者都认 `TRAE_ASSUMED_CACHE_RATIO`）：
    python3 scripts/backfill_trae_credit.py --apply
    python3 scripts/backfill_cost.py --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._common import backup  # noqa: E402
from src.config import load_settings  # noqa: E402
from src.db.conn import Database  # noqa: E402
from src.stats.collector import StatsCollector  # noqa: E402


def _parse_since(raw: str) -> int:
    """`--since` 接受 epoch 秒或 ISO 日期（按 UTC 解析）。"""
    text = raw.strip()
    if text.isdigit():
        return int(text)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _stamp(value: int | None) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d %H:%M")


def cutoff(conn) -> int | None:
    """最后一条 TRAE 真实命中（cached_tokens > 0）的时刻；没有则 None。"""
    row = conn.execute(
        "SELECT MAX(ts) AS hi FROM usage_events "
        "WHERE provider = 'trae' AND cached_tokens > 0").fetchone()
    return int(row["hi"]) if row["hi"] is not None else None


def pending(conn, since: int) -> dict[str, int]:
    """待修正范围：`(provider=trae, ts > since, cached_tokens = 0)` 的汇总。"""
    row = conn.execute(
        "SELECT COUNT(*) AS rows, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "MIN(ts) AS lo, MAX(ts) AS hi "
        "FROM usage_events WHERE provider = 'trae' "
        "AND ts > ? AND cached_tokens = 0", (since,)).fetchone()
    return {"rows": int(row["rows"]), "input_tokens": int(row["input_tokens"]),
            "lo": row["lo"], "hi": row["hi"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 TRAE 上游未上报的缓存 0 归一为 NULL（历史明细）")
    parser.add_argument("--db", help="SQLite 路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--since",
                        help="分界时刻（epoch 秒或 ISO 日期）；默认取最后一条真实命中之后")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认只预览）")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else Path(load_settings().db_path)
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")

    database = Database(db_path)
    conn = database.connect()
    inferred = cutoff(conn)
    if args.since is not None:
        since = _parse_since(args.since)
    elif inferred is not None:
        since = inferred
    else:
        print("库里没有 TRAE 真实缓存命中记录，无法推断分界；请用 --since 显式指定")
        return 1
    print(f"分界 {_stamp(since)}（{since}）"
          + ("" if args.since is not None else " · 最后一条真实命中"))

    report = pending(conn, since)
    if report["rows"] == 0:
        print("没有需要修正的 TRAE 未上报缓存行")
        return 0
    print(f"待置 NULL：{report['rows']} 条明细，输入 {report['input_tokens']} tokens，"
          f"跨度 {_stamp(report['lo'])} ~ {_stamp(report['hi'])}")

    if not args.apply:
        print("预览模式，未修改数据库；确认无误后加 --apply 执行")
        print("之后建议依次重算：backfill_trae_credit.py --apply、backfill_cost.py --apply")
        return 0

    target = backup(db_path)
    with database.transaction() as write:
        write.execute(
            "UPDATE usage_events SET cached_tokens = NULL "
            "WHERE provider = 'trae' AND ts > ? AND cached_tokens = 0", (since,))
    StatsCollector(database).rollup_hourly()
    print(f"已置 NULL 并重算小时汇总（备份: {target}）")
    print("下一步建议：backfill_trae_credit.py --apply、backfill_cost.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
