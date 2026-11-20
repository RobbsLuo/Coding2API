"""日志环形表的落库侧（E3 持久化）。

与 `webapp/log_buffer.py` 的分工：

- `log_buffer` 管**进程内**环形缓冲 + SSE 推送（零 IO，页面实时性靠它）；
- 这里管**持久化**：同样的行再写一份到 `log_events`，进程重启后仍能回看。

**为什么用环形表而不是日志文件**：单进程 SQLite 部署下，文件方案要处理
「读哪个文件 / 怎么 tail / 轮转后 inode 变了 / 跨平台路径」一整套差异，而且
`request_id` 检索要另做索引；一张表 + 一个索引就够了，与 `audit_events` /
`alert_events` 同一范式。

**容量与保留**：`LOG_PERSIST_LINES`（默认 2000，0 = 不落库）。每批写入后按
`seq` 从旧到新删除超额行——保留**最近 N 行**而不是按时间，与内存环口径一致，
重启前后看到的都是「最近的 N 行」。

**写入必须不阻塞业务**：`append` 在日志处理器里被调用，异常会让一条畸形日志
把请求打成 500。这里全部吞掉异常（与 `LogBufferHandler` 同一纪律），失败只
记一次告警到 stderr，不影响业务路径。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from typing import Any

from ..db.conn import Database

# 单批删除上限：一次 DELETE 太多行会长时间持写锁，而日志写入在业务线程上。
_PRUNE_BATCH = 500


class LogRepository:
    """`log_events` 的读写（写：环形裁剪；读：按 request_id / 时间窗）。"""

    def __init__(self, db: Database,
                 *, capacity: Callable[[], int]) -> None:
        self._db = db
        self._capacity = capacity
        # 写入来自任意线程（后台任务、OAuth 轮询）：SQLite 连接本身是线程本地的，
        # 这里只需保证「裁剪判断 + 删除」不被并发交错导致删多。
        self._lock = threading.Lock()
        self._failed = False

    def capacity(self) -> int:
        return max(int(self._capacity()), 0)

    @property
    def enabled(self) -> bool:
        return self.capacity() > 0

    def append(self, *, ts: float, level: str, logger_name: str, message: str,
               request_id: str | None = None) -> None:
        """写一行；关闭状态下静默跳过。任何异常都不上抛（见模块文档）。"""
        if not self.enabled:
            return
        try:
            with self._lock, self._db.transaction() as conn:
                # 必须走 `Database.transaction()` 而不是裸 connect + commit：后者
                # 会绕过 `tx_depth` 记账，在同一线程已持有外层事务时把它提前提交
                # （外层随后的异常回滚只剩后半段，留下半完成写入）。嵌套时
                # `transaction()` 不 BEGIN 也不 COMMIT，交由最外层统一收口。
                conn.execute(
                    "INSERT INTO log_events (ts, level, logger, message, request_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (int(ts), level, logger_name, message, request_id))
                self._prune(conn)
        except Exception:  # noqa: BLE001 - 日志落库故障不得影响业务
            # 只报一次：日志表写不进去通常意味着磁盘满 / 库锁死，重复告警没有
            # 新信息，反而会淹没真问题。
            if not self._failed:
                self._failed = True
                import sys

                print("日志持久化失败（后续不再重复告警）", file=sys.stderr)
        else:
            self._failed = False

    def _prune(self, conn: sqlite3.Connection) -> None:
        """删掉超出容量的最旧行（保留最近 N 行，与内存环口径一致）。"""
        capacity = self.capacity()
        excess = conn.execute("SELECT COUNT(*) FROM log_events").fetchone()[0] - capacity
        if excess <= 0:
            return
        # 分批删：一次删太多会长时间持写锁，而这条路径在业务线程上。
        while excess > 0:
            batch = min(excess, _PRUNE_BATCH)
            conn.execute(
                "DELETE FROM log_events WHERE seq IN "
                "(SELECT seq FROM log_events ORDER BY seq LIMIT ?)", (batch,))
            excess -= batch

    def query(self, *, limit: int = 200, request_id: str | None = None,
              since: int | None = None, level: str | None = None,
              ) -> list[dict[str, Any]]:
        """按条件取最近 N 行（旧→新）。

        `request_id` 是排障主入口：一次请求的全部日志（含轮换到别的渠道的
        尝试）都带同一个 id，比按时间翻页快得多。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if request_id:
            clauses.append("request_id = ?")
            params.append(request_id)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(int(since))
        if level:
            clauses.append("level = ?")
            params.append(level.upper())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.connect().execute(
            f"SELECT seq, ts, level, logger, message, request_id FROM log_events"
            f"{where} ORDER BY seq DESC LIMIT ?", (*params, max(1, int(limit)))).fetchall()
        return [dict(row) for row in reversed(rows)]

    def purge(self, *, keep_days: int, now: int | None = None) -> int:
        """按保留期清理（由 `RetentionTask` 周期调用，与其余表同口径）。

        环形表本身已按容量裁剪，这里兜的是「长期不写入 → 旧行永远留着」：
        容量只在写入时触发裁剪，一个不再产生日志的部署（如全部凭证暂停）
        会一直留着很久以前的行。
        """
        import time

        cutoff = int(now if now is not None else time.time()) - keep_days * 86400
        # 同 `append`：走 `transaction()` 才能与同线程外层事务正确嵌套，
        # 裸 connect + commit 会把外层未提交写入提前提交。
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM log_events WHERE ts < ?", (cutoff,))
        return cursor.rowcount
