"""单实例锁：同一 DATA_DIR 只能有一个进程。

背景：SQLite 允许多进程并发写（WAL + 忙等），所以「起了两个实例」不会报错，
只会安静地互相覆盖——两个进程各自跑后台任务（额度探测、签到、模型目录刷新），
同一凭证被探两次、同一小时汇总写两遍。更常见的形态是 launchd 崩溃循环：
端口被占 → 绑定失败 → 退出 → KeepAlive 每 10s 重拉（实测刷了 2377 次
`[Errno 48]`，见 2026-09-19~10-17 的 logs/launchd.err.log）。

锁用 `flock` 而不是端口探测：端口探测有 TOCTOU 窗口，且只能保护「同端口」
的实例；两个实例配不同 PORT 时照样双写同一个库。flock 由内核在进程退出
（含 SIGKILL）时自动释放，不留需要人工清理的陈旧锁。
"""

from __future__ import annotations

import os

import pytest

from src.instance_lock import LOCK_NAME, AlreadyRunning, InstanceLock, hold_instance_lock


def test_acquire_creates_lock_file_with_pid(tmp_path):
    """拿到锁后锁文件里写着本进程 PID（仅用于报错文案）。"""
    lock = InstanceLock(tmp_path)
    lock.acquire()
    try:
        assert (tmp_path / LOCK_NAME).read_text(encoding="utf-8").strip() == str(
            os.getpid())
    finally:
        lock.release()


def test_second_acquire_in_same_process_is_rejected(tmp_path):
    """同进程内第二次取锁也失败：flock 是进程级的，重入不递增计数。"""
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            InstanceLock(tmp_path).acquire()
    finally:
        first.release()


def test_second_process_is_rejected(tmp_path):
    """真正的并发场景：子进程持锁期间，父进程取不到。"""
    import subprocess
    import sys
    import textwrap

    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(os.getcwd())!r})
            from src.instance_lock import InstanceLock
            lock = InstanceLock({str(tmp_path)!r})
            lock.acquire()
            print("locked", flush=True)
            time.sleep(30)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(AlreadyRunning) as excinfo:
            InstanceLock(tmp_path).acquire()
        # 报错文案必须带持有者 PID：崩溃循环里这行是唯一线索
        assert "PID" in str(excinfo.value)
        assert excinfo.value.holder.strip() != str(os.getpid())
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_release_allows_reacquire(tmp_path):
    """释放后可以再取（重启场景）。"""
    lock = InstanceLock(tmp_path)
    lock.acquire()
    lock.release()
    again = InstanceLock(tmp_path)
    again.acquire()
    again.release()


def test_release_is_idempotent(tmp_path):
    """重复 release 不抛：finally 里可能被调用两次。"""
    lock = InstanceLock(tmp_path)
    lock.acquire()
    lock.release()
    lock.release()


def test_context_manager_releases_on_exception(tmp_path):
    """异常路径也要释放，否则下一个实例永远起不来。"""
    with pytest.raises(ValueError), InstanceLock(tmp_path):
        raise ValueError("boom")
    InstanceLock(tmp_path).acquire()


def test_missing_lock_file_reports_unknown_holder(tmp_path):
    """锁文件读不到时 holder 回落 unknown，不让报错路径自己崩掉。"""
    lock = InstanceLock(tmp_path)
    assert lock._read_holder() == "unknown"


def test_empty_lock_file_reports_unknown_holder(tmp_path):
    """空内容同样回落 unknown（被 truncate 但还没写 PID 的瞬间）。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / LOCK_NAME).write_text("   \n", encoding="utf-8")
    assert InstanceLock(tmp_path)._read_holder() == "unknown"


def test_hold_instance_lock_creates_data_dir(tmp_path):
    """DATA_DIR 还不存在时也能取锁（首次启动）。"""
    target = tmp_path / "fresh"
    lock = hold_instance_lock(target)
    try:
        assert (target / LOCK_NAME).is_file()
    finally:
        lock.release()


def test_already_running_message_is_actionable(tmp_path):
    """报错文案必须说清「同目录只能一个实例」与怎么解除。"""
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning) as excinfo:
            InstanceLock(tmp_path).acquire()
        text = str(excinfo.value)
        assert "另一个实例" in text
        assert "只能起一个进程" in text
        assert "删掉" in text
    finally:
        first.release()
