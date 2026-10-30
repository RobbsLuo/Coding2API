"""单实例保护：同一 `DATA_DIR` 只允许一个进程写库。

**为什么需要**：SQLite 允许多进程并发写（WAL + 忙等），所以「起了两个实例」
不会报错，只会安静地互相覆盖——两个进程各自跑后台任务（额度探测、签到、
模型目录刷新），同一凭证被探两次、同一小时汇总被写两遍。更常见的形态是
launchd 的崩溃循环：端口被占 → uvicorn 绑定失败 → 退出 → KeepAlive 每 10s
重拉，2026-09-19~10-17 实测刷了 2377 次 `[Errno 48]`。

**为什么用文件锁而不是端口探测**：
- 端口探测有 TOCTOU 窗口（探测时空闲、绑定前被抢），而且只能保护「同端口」
  的实例——两个实例配不同 PORT 时照样双写同一个库。
- `flock` 由内核在进程退出（含 SIGKILL）时自动释放，不会留下需要人工清理
  的陈旧锁文件。
- 锁的是**数据目录**而不是库文件：后台任务还会写 `data/backups/`、
  `model_catalog.json` 等旁路文件，只锁 `.sqlite3` 挡不住。

**为什么锁文件不删**：unlink 一个正被 flock 的文件会让第二个进程创建一个新
inode 并成功加锁——经典的 flock 陷阱。所以锁文件常驻，只锁内容不锁存在性。
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: 锁文件名（落在 DATA_DIR 下，与库、备份同目录，便于一起备份/迁移）。
LOCK_NAME = ".instance.lock"


class AlreadyRunning(RuntimeError):
    """同一 DATA_DIR 已有实例在运行。"""

    def __init__(self, lock_path: Path, holder: str) -> None:
        self.lock_path = lock_path
        self.holder = holder
        super().__init__(
            f"另一个实例已在使用 {lock_path.parent}（PID {holder}）。"
            f"同一数据目录只能起一个进程：两个实例会各自跑后台任务、"
            f"重复探测凭证并互相覆盖统计。若确认没有实例在跑，删掉 "
            f"{lock_path} 后重试。")


class InstanceLock:
    """持有 `DATA_DIR/.instance.lock` 的独占 flock。

    用 `LOCK_EX | LOCK_NB`：拿不到立即失败，绝不等待——启动路径上等待只会
    让 launchd 的 10s 重拉堆成一排等锁的进程。

    用法：

        lock = InstanceLock(data_dir)     # 拿不到抛 AlreadyRunning
        try:
            ...                          # 跑服务
        finally:
            lock.release()
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> None:
        """取独占锁；已被占用时抛 `AlreadyRunning`（含持有者 PID）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_CLOEXEC：不把锁 fd 漏给子进程（uvicorn 的 reload/worker 模型下
        # 子进程继承 fd 会让锁在主进程退出后仍被持有）。
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = self._read_holder()
            os.close(fd)
            raise AlreadyRunning(self.path, holder) from None
        # 写入自己的 PID：锁文件内容只用于报错文案，权威判据永远是 flock。
        # 截断后写，避免上次更长的 PID 留下尾巴。
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        """释放锁（幂等）。"""
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def _read_holder(self) -> str:
        """读锁文件里的 PID；读不到给 `unknown`（内容仅供参考，不参与判定）。"""
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"
        return text or "unknown"

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def hold_instance_lock(data_dir: str | Path) -> InstanceLock:
    """取锁并记日志；失败抛 `AlreadyRunning`（调用方决定退出码）。"""
    lock = InstanceLock(data_dir)
    lock.acquire()
    logger.info("已获取实例锁 %s（PID %d）", lock.path, os.getpid())
    return lock
