"""SQLite 连接管理（标准库 sqlite3 + WAL，同步调用）。

本地 SQLite 读写为微秒级，asyncio 封装开销大于收益，因此走同步调用 + 线程本地连接。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..security.private_files import secure_dir, secure_file

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    # WAL 下的官方推荐档位（SQLite 文档：WAL 模式下 NORMAL 不会损坏库）。
    # 默认 FULL 每次提交都要 fsync 一次：`record()` 是每请求一次提交的高频
    # 路径，一次 fsync 就是毫秒级延迟 + 写盘放大。NORMAL 只保证「事务原子、
    # 库不损坏」，**崩溃/断电最多丢最后几条已提交事务**（OS 缓冲未落盘）。
    # 统计是旁路数据，这个取舍是划算的；凭证/配额等强一致写入走的是同样的
    # 连接，但它们本身也是「提交即认为成功」的语义，不会读到半写状态。
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
)

#: 只读连接的 pragma 子集：不设 `journal_mode`（那会**持久改写库头**并建
#: `-wal`/`-shm`），只保证等锁与查询语义与读写连接一致。
READ_ONLY_PRAGMAS = (
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
)


class Database:
    def __init__(self, path: str | Path, *, strict_permissions: bool = True,
                 read_only: bool = False) -> None:
        """SQLite 连接工厂。

        `read_only=True` 用于「预览 / 诊断」类入口（`cleanup_*`、
        `rollup_hourly` 的 dry-run）：以 `file:...?mode=ro` URI 打开，
        **不**执行 `PRAGMA journal_mode=WAL`（它会持久改写库头）、不建目录、
        不 chmod 库与边车。库不存在时第一次 `connect()` 抛 `sqlite3.Error`
        （而不是顺手建一个空库）；写操作由 SQLite 自身拒绝。
        """
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
        self._strict_permissions = strict_permissions
        self.read_only = read_only
        if read_only:
            # 只读入口不碰文件系统：不建目录、不收紧权限。预览不该有副作用。
            return
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
            # 库内是凭证密文 + 明文用量/审计/用户名：目录不对同机其他用户开放。
            # 校验失败默认**阻断启动**（issue #45 的口径）：这是运维能修的确定
            # 问题，不是需要降级的不确定状态。旁路场景（测试替身、只读运维脚本
            # 打开别人的库）可显式传 `strict_permissions=False` 放宽。
            secure_dir(parent, strict=strict_permissions)
        # 库文件本身也要收紧：父目录 0700 只挡住「遍历」，而文件由 sqlite3 按
        # umask 创建（实测默认 0644），同机其他用户仍可直接读整库。
        self._secure_files()

    def _connect_uri(self) -> str:
        """建连目标：只读走 `mode=ro` URI，读写走普通路径。"""
        if not self.read_only:
            return self.path
        # 绝对化后拼 URI：相对路径会在 sqlite3 的 URI 解析里被当成相对主目录，
        # 而且 `?` / `#` 出现在文件名里会被误当参数分隔符。
        return f"file:{Path(self.path).resolve()}?mode=ro"

    def _secure_files(self) -> None:
        """把库文件（含 WAL/SHM 边车）收成 0600；不存在则跳过。

        每次建连都调一次：WAL/SHM 由 SQLite 在首次写入时创建，那一刻才出现，
        只有建连后收紧才能覆盖到。chmod 幂等，重复调用无副作用。
        只读连接跳过：预览/诊断不该改任何文件属性。
        """
        if self.read_only:
            return
        for suffix in ("", "-wal", "-shm"):
            target = Path(f"{self.path}{suffix}")
            if target.exists():
                secure_file(target, strict=self._strict_permissions)

    def connect(self) -> sqlite3.Connection:
        """返回当前线程的专属连接（同线程复用）。"""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "generation", -1) != self._generation:
            conn = None  # 上一代连接已被 close() 关闭
            self._local.tx_depth = 0
        if conn is None:
            conn = sqlite3.connect(self._connect_uri(), check_same_thread=False,
                                   uri=self.read_only)
            conn.row_factory = sqlite3.Row
            for pragma in (READ_ONLY_PRAGMAS if self.read_only else PRAGMAS):
                conn.execute(pragma)
            self._local.conn = conn
            self._local.generation = self._generation
            with self._lock:
                self._conns.append(conn)
            if not self.read_only:
                # 新库就是在这里被 sqlite3 建出来的（权限随 umask，实测 0644），
                # WAL/SHM 也在首个写事务里出现，所以建连后再收紧一次。
                self._secure_files()
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
