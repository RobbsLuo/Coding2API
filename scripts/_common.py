#!/usr/bin/env python3
"""scripts/ 下一次性运维脚本的共享基建。

此前每个脚本各自复制一份 `backup()`（6 份）与 `_DbAdapter`（4 份），副本已经
分叉：适配器的 `transaction()` 丢了 `Database` 的 `BEGIN IMMEDIATE`，写库前
不取写锁 → 读改写之间存在丢更新窗口。集中在这里，只有一份实现。

这些脚本不是包内模块，靠 `sys.path` 插入仓库根后被 `import scripts._common`
（隐式命名空间包）或测试的 `load_script` 加载。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def backup(db_path: Path) -> Path:
    """写库前备份主库文件到同目录（`.bak-<时间戳>`），失败即中止。

    用 SQLite 在线备份 API 而不是复制文件：WAL 模式下直接 copy 主库文件会
    丢掉尚未 checkpoint 的已提交事务。
    """
    target = db_path.with_name(f"{db_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    source = sqlite3.connect(str(db_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


class DbAdapter:
    """把裸连接包装成 `Database` 的形态（回填 / rollup 只用 connect、transaction）。

    与 `Database.transaction()` 保持同一语义，尤其 `BEGIN IMMEDIATE`：脚本的
    读改写（如 `save_quota` 的 before/after）必须立即取写锁，否则两个写者会
    基于同一旧快照更新（丢更新）。此前 4 份副本都漏了这一步。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._depth = 0

    def connect(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def transaction(self):
        """嵌套只有最外层提交/回滚，与 `Database.transaction` 同语义。"""
        depth = self._depth
        if depth == 0 and not self._conn.in_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
        self._depth = depth + 1
        try:
            yield self._conn
        except BaseException:
            if depth == 0:
                self._conn.rollback()
            raise
        finally:
            self._depth = depth
        if depth == 0:
            self._conn.commit()


def hour_range(conn, where: str, params: list[object]) -> tuple[int, int] | None:
    """`where` 命中行覆盖的小时区间（整点对齐、闭区间）；无行返回 None。

    删除失败明细前先取这个区间，`sync_hourly` 只在该区间清孤儿行：区间外的
    小时**本来就没有明细**（`purge_expired` 删 90 天前明细，小时汇总永久保留），
    无条件清会把历史汇总一起删掉。
    """
    row = conn.execute(
        f"SELECT MIN(ts), MAX(ts) FROM usage_events WHERE {where}", params).fetchone()
    if row[0] is None:
        return None
    return (int(row[0]) // 3600) * 3600, (int(row[1]) // 3600) * 3600


def sync_hourly(conn, hours: tuple[int, int] | None) -> None:
    """全量重算小时汇总，并清掉 `hours` 区间内已无明细对应的孤儿行。

    `hours` 必须是**本次删除覆盖**的小时区间（见 `hour_range`）；None = 没有
    待删行，只重算不清孤儿。
    """
    from src.stats.collector import StatsCollector

    StatsCollector(DbAdapter(conn)).rollup_hourly()
    if hours is None:
        return
    conn.execute(
        "DELETE FROM usage_hourly WHERE hour_utc BETWEEN ? AND ? AND NOT EXISTS ("
        "  SELECT 1 FROM usage_events e"
        "  WHERE (e.ts / 3600) * 3600 = usage_hourly.hour_utc"
        "    AND e.username = usage_hourly.username"
        "    AND e.provider = usage_hourly.provider"
        "    AND e.model = usage_hourly.model)", hours)
