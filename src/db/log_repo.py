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

**写入必须不阻塞业务**：`append` 在日志处理器里被调用，而日志处理器常在
**事件循环线程**上跑（`access_log` / 业务 `logger.info`）。早先的实现直接在该
线程里 `BEGIN IMMEDIATE` + INSERT + 裁剪，后台任务持写锁时会把整个事件循环
卡到 `busy_timeout`（5s）；异常被吞后还只在库忙时丢掉排障日志。现在的口径：

- `append` **只入队**：`queue.Queue(maxsize=QUEUE_MAXSIZE)` + `put_nowait`，
  队列满就丢掉该行并计数（日志是尽力而为，绝不能反压业务）；
- 独立 **daemon 线程**消费：最多等 `FLUSH_INTERVAL` 攒一批、每批 ≤ `BATCH_SIZE`
  行，一次事务写完整个批次（复用 `Database.transaction()` 与 `_prune`）；
- 任何异常都不上抛（与 `LogBufferHandler` 同一纪律），失败只记一次告警；
- 消费者空闲超过 `IDLE_EXIT` 自行退出、下次写入再拉起：长期无日志的部署
  （以及大量建 app 却不进 lifespan 的测试）不该攒空转线程 + 空闲连接；
- `flush(timeout)` / `close()` 供测试与 lifespan 停机排空。

**线程安全**：队列自身线程安全；`_prune` 的锁保留。SQLite 连接是线程本地的
（`Database.transaction()` 按当前线程取连接），消费者线程自建连接，与业务
线程的写入互不串行——这正是本改动的目的。
"""

from __future__ import annotations

import queue
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from ..db.conn import Database

# 单批删除上限：一次 DELETE 太多行会长时间持写锁，而写锁是全局的——持久了
# 其它线程（含业务线程的统计写入）都得等 `busy_timeout`。
_PRUNE_BATCH = 500

# 落库队列上限：512 行≈半页日志，进程被上游刷屏时也不至于吃光内存。
QUEUE_MAXSIZE = 512

# 单批行数上限：一个事务写 50 行，比逐行事务少 50 倍的取锁/提交开销。
BATCH_SIZE = 50

# 攒批等待上限：有行待写时最多再等这么久收同行，然后一次性落库。
FLUSH_INTERVAL = 0.2

# 消费者空闲多久后自行退出：没人写日志时不该白占一个线程 + 一个 SQLite 连接。
# 下次 `append` 会自动拉起新线程；30s 是「一阵日志打完就收工」的量级。
IDLE_EXIT = 30.0

# 待写行的五元组（与 INSERT 的列顺序一致，消费者线程直接 executemany）
_Row = tuple[float, str, str, str, str | None]


class LogRepository:
    """`log_events` 的读写（写：队列 + 后台线程批量落库；读：按 request_id / 时间窗）。"""

    def __init__(self, db: Database,
                 *, capacity: Callable[[], int],
                 maxsize: int = QUEUE_MAXSIZE,
                 batch_size: int = BATCH_SIZE,
                 flush_interval: float = FLUSH_INTERVAL,
                 idle_exit: float = IDLE_EXIT,
                 autostart: bool = True) -> None:
        self._db = db
        self._capacity = capacity
        # 裁剪判断 + 删除仍在锁内（消费者线程与 `purge` 可能并发）。
        self._lock = threading.Lock()
        self._failed = False
        self._queue: queue.Queue[_Row] = queue.Queue(maxsize=max(int(maxsize), 1))
        self._batch_size = max(int(batch_size), 1)
        self._flush_interval = max(float(flush_interval), 0.0)
        # 空闲超时后消费者自行退出（见 `_run`）：长期没日志就不该占着线程。
        self._idle_exit = max(float(idle_exit), 0.0)
        # `autostart=False` 供测试造「只入队不消费」的场景（验证不阻塞、会丢弃）。
        self._autostart = autostart
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._closed = False
        self._dropped = 0
        # 待写行数 + 条件变量：`flush` 只等它归零，不依赖 sqlite 事务何时提交。
        self._pending_cond = threading.Condition()
        self._pending = 0

    def capacity(self) -> int:
        return max(int(self._capacity()), 0)

    @property
    def enabled(self) -> bool:
        return self.capacity() > 0

    @property
    def dropped(self) -> int:
        """因队列满而被丢弃的行数（只增不减，供告警与测试观察）。"""
        with self._pending_cond:
            return self._dropped

    # ---------------------------------------------------------------- 写入

    def append(self, *, ts: float, level: str, logger_name: str, message: str,
               request_id: str | None = None) -> None:
        """入队一行；关闭状态下静默跳过。任何异常都不上抛（见模块文档）。"""
        if self._closed or not self.enabled:
            return
        # 先加计数再入队：反过来的话消费者可能先减（pending 变负），
        # `flush` 的「归零」判定就失真了。
        with self._pending_cond:
            self._pending += 1
        # 入队与「确保消费者在跑」必须在同一把锁里：消费者空闲退出时也在锁内
        # 确认队列为空再清线程槽，两者交错就会出现「行进了没人消费」。
        with self._worker_lock:
            if self._closed:
                # 与 `close()` 交错：关库后不再收行，直接丢。留下「已入队但没
                # 人消费」的行会让 `flush` 永远等不到归零。
                with self._pending_cond:
                    self._pending -= 1
                return
            self._start_worker_locked()
            try:
                self._queue.put_nowait((ts, level, logger_name, message, request_id))
            except queue.Full:
                # 满即丢：日志是尽力而为，反压会把一条慢库变成业务不可用。
                with self._pending_cond:
                    self._pending -= 1
                    self._dropped += 1
                    first = self._dropped == 1
                if first:
                    print("日志落库队列已满，丢弃新行（不阻塞业务，后续不再重复告警）",
                          file=sys.stderr)

    def flush(self, timeout: float = 5.0) -> bool:
        """等待队列里的行全部落库（最多 `timeout` 秒），返回是否已排空。"""
        with self._pending_cond:
            return self._pending_cond.wait_for(lambda: self._pending == 0,
                                               timeout=timeout)

    def close(self, timeout: float = 5.0) -> None:
        """排空队列并结束消费者线程（幂等；停机与测试用）。"""
        with self._worker_lock:
            if self._closed:
                return
            self._closed = True
        self.flush(timeout)
        self._stop.set()
        worker = self._worker
        if worker is None:
            return
        worker.join(timeout)

    # ------------------------------------------------------------ 消费者线程

    def _start_worker_locked(self) -> None:
        """确保消费者线程在跑（调用方必须持有 `_worker_lock`）。

        在锁内「检查 + 启动」：消费者空闲退出时也在同一把锁里清槽，两者交错
        就会出现「行已入队却没人消费」。
        """
        if not self._autostart or self._closed or self._worker is not None:
            return
        worker = threading.Thread(target=self._run, name="log-persist", daemon=True)
        self._worker = worker
        worker.start()

    def _idle_exit_due(self, idle_since: float) -> bool:
        """空闲超时且队列为空 → 交还线程槽，返回 True 表示消费者可以退出。"""
        if time.monotonic() - idle_since < self._idle_exit:
            return False
        with self._worker_lock:
            if self._worker is not threading.current_thread():
                return False        # 槽已被新线程接管：自己退出即可，别清别人的
            if not self._queue.empty():
                return False        # 刚有新行进来（`append` 已在锁内入队）
            self._worker = None
            return True

    def _run(self) -> None:
        """消费循环：取一行 → 尽量攒满一批 → 一次事务写完 → 通知 flush。

        空闲超过 `idle_exit` 就退出（把线程槽清空，下次写入再拉起）：大量
        `build_app` 但不进 lifespan 的场景（测试、只读运维脚本）否则每次都会
        留一个永不停转的线程 + 一个 SQLite 连接。
        """
        idle_since = time.monotonic()
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                if self._idle_exit_due(idle_since):
                    return
                continue
            idle_since = time.monotonic()
            batch = [item]
            deadline = time.monotonic() + self._flush_interval
            while len(batch) < self._batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self._queue.get(timeout=remaining))
                except queue.Empty:
                    break
            self._write(batch)
            with self._pending_cond:
                self._pending -= len(batch)
                self._pending_cond.notify_all()

    def _write(self, batch: list[_Row]) -> None:
        """一次事务写完一批；任何异常都不上抛（见模块文档）。"""
        try:
            with self._lock, self._db.transaction() as conn:
                # 必须走 `Database.transaction()` 而不是裸 connect + commit：后者
                # 会绕过 `tx_depth` 记账，在同一线程已持有外层事务时把它提前提交
                # （外层随后的异常回滚只剩后半段，留下半完成写入）。
                conn.executemany(
                    "INSERT INTO log_events (ts, level, logger, message, request_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(int(ts), level, logger, message, request_id)
                     for ts, level, logger, message, request_id in batch])
                self._prune(conn)
        except Exception:  # noqa: BLE001 - 日志落库故障不得影响业务
            # 只报一次：日志表写不进去通常意味着磁盘满 / 库锁死，重复告警没有
            # 新信息，反而会淹没真问题。
            if not self._failed:
                self._failed = True
                print("日志持久化失败（后续不再重复告警）", file=sys.stderr)
        else:
            self._failed = False

    def _prune(self, conn: sqlite3.Connection) -> None:
        """删掉超出容量的最旧行（保留最近 N 行，与内存环口径一致）。"""
        capacity = self.capacity()
        excess = conn.execute("SELECT COUNT(*) FROM log_events").fetchone()[0] - capacity
        if excess <= 0:
            return
        # 分批删：一次删太多会长时间持写锁。
        while excess > 0:
            batch = min(excess, _PRUNE_BATCH)
            conn.execute(
                "DELETE FROM log_events WHERE seq IN "
                "(SELECT seq FROM log_events ORDER BY seq LIMIT ?)", (batch,))
            excess -= batch

    # ---------------------------------------------------------------- 读取

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
        cutoff = int(now if now is not None else time.time()) - keep_days * 86400
        # 同 `_write`：走 `transaction()` 才能与同线程外层事务正确嵌套，
        # 裸 connect + commit 会把外层未提交写入提前提交。
        with self._lock, self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM log_events WHERE ts < ?", (cutoff,))
        return cursor.rowcount
