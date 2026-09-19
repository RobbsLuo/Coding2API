"""SQLite 连接管理（T-Q2：标准库 sqlite3 + WAL，同步调用）。

本地 SQLite 读写为微秒级，asyncio 封装开销大于收益，因此走同步调用 + 线程本地连接。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
)


class Database:
    def __init__(self, path: str | Path) -> None:
        # 线程本地必须按实例隔离：模块级变量会让不同 Database 共用同一连接
        self._local = threading.local()
        self.path = str(path)
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """返回当前线程的专属连接（同线程复用）。"""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            for pragma in PRAGMAS:
                conn.execute(pragma)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """关闭当前线程连接（测试用）。"""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写操作事务：正常退出提交，异常回滚。

        多语句写入必须走这个上下文（如「先清 pin 再置 pin」），
        否则中途抛错会留下半完成状态；单语句写入也可用，
        让「connect → execute → commit」的样板只出现一次。
        """
        conn = self.connect()
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
