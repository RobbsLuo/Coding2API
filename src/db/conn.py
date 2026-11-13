"""SQLite 连接管理（标准库 sqlite3 + WAL，同步调用）。

本地 SQLite 读写为微秒级，asyncio 封装开销大于收益，因此走同步调用 + 线程本地连接。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..security.private_files import secure_dir

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
)


class Database:
    def __init__(self, path: str | Path, *, strict_permissions: bool = True) -> None:
        # 线程本地必须按实例隔离：模块级变量会让不同 Database 共用同一连接
        self._local = threading.local()
        # 所有线程建过的连接都登记在这里：`close()` 要能一次收干净。只关「当前
        # 线程」的连接会在多线程（如 TestClient 的线程池、后台任务线程）下漏掉
        # 其余连接，SQLite 文件句柄与 WAL 就永远不释放。
        self._conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        # 连接「代」：`close()` 递增。各线程缓存的连接带着建立时的代号，代号
        # 落后即视为已失效并重连——这样 `close()` 关掉**所有**线程的连接之后，
        # 任何线程下次 `connect()` 都会自动拿到新连接，而不是踩到已关闭的旧句柄。
        self._generation = 0
        self.path = str(path)
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
            # 库内是凭证密文 + 明文用量/审计/用户名：目录不对同机其他用户开放。
            # 校验失败默认**阻断启动**（issue #45 的口径）：这是运维能修的确定
            # 问题，不是需要降级的不确定状态。旁路场景（测试替身、只读运维脚本
            # 打开别人的库）可显式传 `strict_permissions=False` 放宽。
            secure_dir(parent, strict=strict_permissions)

    def connect(self) -> sqlite3.Connection:
        """返回当前线程的专属连接（同线程复用）。"""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "generation", -1) != self._generation:
            conn = None  # 上一代连接已被 close() 关闭
            self._local.tx_depth = 0
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            for pragma in PRAGMAS:
                conn.execute(pragma)
            self._local.conn = conn
            self._local.generation = self._generation
            with self._lock:
                self._conns.append(conn)
        return conn

    def close(self) -> None:
        """关闭**所有**线程连接（测试与停机用）。

        登记表先清空、代号递增，再逐个 `close()`：某个连接关失败不阻止其余
        连接被关，也不阻止后续 `connect()` 重连。
        """
        with self._lock:
            conns, self._conns = self._conns, []
            self._generation += 1
        for conn in conns:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写操作事务：正常退出提交，异常回滚。

        多语句写入必须走这个上下文（如「先清 pin 再置 pin」），
        否则中途抛错会留下半完成状态；单语句写入也可用，
        让「connect → execute → commit」的样板只出现一次。

        显式 `BEGIN IMMEDIATE`：sqlite3 默认的 deferred 事务要到**第一条
        写语句**才取写锁，于是 `with transaction(): SELECT ...; UPDATE ...` 里的
        SELECT 在锁外读到旧快照，两个写者都基于同一旧值更新 → 丢更新
        （`save_quota` 的 before/after、pin 切换都依赖读改写的一致性）。
        立即取写锁把整段纳入同一事务；`connect` 是线程本地连接，同一时刻只有
        一个协程在用。**嵌套调用只有最外层提交/回滚**（`tx_depth` 计数）：
        否则内层退出时的无条件 `commit()` 会把外层尚未写完的工作提前提交，
        内层异常时的 `rollback()` 又会丢弃外层的未提交写入。
        """
        conn = self.connect()
        depth = getattr(self._local, "tx_depth", 0)
        if depth == 0 and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        self._local.tx_depth = depth + 1
        try:
            yield conn
        except BaseException:
            if depth == 0:
                conn.rollback()
            raise
        finally:
            self._local.tx_depth = depth
        if depth == 0:
            conn.commit()
