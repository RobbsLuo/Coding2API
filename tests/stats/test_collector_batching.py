"""P2-15：统计写批量合并、flush 与写失败计数。

要点：
1. 并发请求必须合并成**一次**事务（不是一条一行一提交）。
2. `flush()` 之后队列不残留、库里不缺行。
3. 写失败计数递增且行**不丢**（回填队列，后续写者重试）。
4. `synchronous = NORMAL` 真的生效。
5. 进程退出（`atexit` 钩子）把残留队列写掉。

时序全部用确定性手段（`threading.Barrier` / 直接调用钩子函数），不 sleep。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from src.stats import collector as collector_module
from src.stats.collector import BATCH_MAX_EVENTS, BATCH_MAX_PENDING, StatsCollector


def _rows(db) -> int:
    return db.connect().execute(
        "SELECT COUNT(*) AS c FROM usage_events").fetchone()["c"]


def _hourly(db) -> list[sqlite3.Row]:
    return db.connect().execute(
        "SELECT requests, ok_count, input_tokens FROM usage_hourly").fetchall()


# ------------------------------------------------------------------ synchronous


def test_synchronous_is_normal(repo):
    """WAL + NORMAL：提交不再每事务 fsync（P2-15 的一半收益在这里）。"""
    _credentials, db = repo
    assert db.connect().execute("PRAGMA synchronous").fetchone()[0] == 1  # 1 = NORMAL


def test_read_only_connection_keeps_default_sync(repo):
    """只读连接不设 synchronous：预览/诊断不改任何连接级行为。"""
    from src.db.conn import Database

    _credentials, db = repo
    target = db.path
    db.close()
    read_only = Database(target, read_only=True)
    try:
        # SQLite 默认 FULL = 2；只读连接不执行 PRAGMAS 里的 synchronous
        assert read_only.connect().execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        read_only.close()


# ------------------------------------------------------------------ 批量合并


def test_batch_coalesces_transactions(repo, monkeypatch):
    """N 个并发请求 → 事务数远少于 N，且每行都在库里。

    用 `Database.transaction` 的调用次数当「事务数」：批量合并的直接可观测
    证据（也是这条优化唯一的机制）。

    确定性做法：主线程先持写锁，让所有 worker 都**完成入队**后才放锁，
    于是第一个抢到锁的写者一定能看到整段积压 → 事务数恰为
    `ceil(N / BATCH_MAX_EVENTS)`。不靠线程调度碰运气。
    """
    _credentials, db = repo
    transactions = 0
    original = db.transaction

    def counting_transaction():
        nonlocal transactions
        transactions += 1
        return original()

    monkeypatch.setattr(db, "transaction", counting_transaction)
    collector = StatsCollector(db)
    count = BATCH_MAX_EVENTS * 3 + 1
    enqueued = [threading.Event() for _ in range(count)]
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            event = collector._build_event(
                username=f"u{index}", provider="trae", model="m", ok=True)
            collector._pending.append(event)
            enqueued[index].set()
            collector._settle()
        except BaseException as error:  # noqa: BLE001 - 收集后由主线程断言
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    with collector._write_lock:          # 锁住写者，直到全部入队
        for thread in threads:
            thread.start()
        for signal in enqueued:
            signal.wait()
        assert len(collector._pending) == count
    collector.flush()
    for thread in threads:
        thread.join()

    assert errors == []
    assert _rows(db) == count
    # 旧行为是每条一次提交 → count 次；合批后恰好 ceil(count / N)
    assert transactions == -(-count // BATCH_MAX_EVENTS) < count
    # 小时汇总也与明细一致（合并事务里逐行 upsert）
    hourly = _hourly(db)
    assert sum(row["requests"] for row in hourly) == count


def test_batch_returns_only_after_row_is_committed(repo):
    """read-after-write：`record()` 返回时该行必须已可读。"""
    _credentials, db = repo
    collector = StatsCollector(db)
    collector.record(username="u", provider="trae", model="m", ok=True)
    assert _rows(db) == 1


def test_batch_caps_rows_per_transaction(repo, monkeypatch):
    """单事务最多 `BATCH_MAX_EVENTS` 行：一批的行数有界（不无限攒）。"""
    _credentials, db = repo
    sizes: list[int] = []
    original = collector_module.StatsCollector._write_batch

    def spy(self, batch):
        sizes.append(len(batch))
        return original(self, batch)

    monkeypatch.setattr(collector_module.StatsCollector, "_write_batch", spy)
    collector = StatsCollector(db)
    # 直接造积压：先把队列塞满再 flush，绕开「每个 record 自己写空」的路径
    for _index in range(BATCH_MAX_EVENTS * 2 + 1):
        collector._pending.append(collector._build_event(
            username="u", provider="trae", model="m", ok=True))
    written = collector.flush()
    assert written == BATCH_MAX_EVENTS * 2 + 1
    assert sizes == [BATCH_MAX_EVENTS, BATCH_MAX_EVENTS, 1]
    assert _rows(db) == BATCH_MAX_EVENTS * 2 + 1


def test_queue_overflow_writes_inline_without_dropping(repo):
    """队列到达硬上限时不丢行：新来的行自己开事务写掉。"""
    _credentials, db = repo
    collector = StatsCollector(db)
    # 造一段积压（直接塞队列，模拟所有写者都卡在忙等）
    collector._pending.extend(
        collector._build_event(username="u", provider="trae", model="m", ok=True)
        for _ in range(BATCH_MAX_PENDING))
    collector.record(username="overflow", provider="trae", model="m", ok=True)
    # 溢出行直接落库
    overflow = db.connect().execute(
        "SELECT COUNT(*) AS c FROM usage_events WHERE username = 'overflow'").fetchone()
    assert overflow["c"] == 1
    # 积压仍在，flush 后不丢
    assert collector.flush() == BATCH_MAX_PENDING
    assert _rows(db) == BATCH_MAX_PENDING + 1


def test_queue_overflow_write_failure_is_counted(repo, monkeypatch):
    """兜底写失败同样计数、同样上抛（不因为「不走队列」而静默）。"""
    _credentials, db = repo
    collector = StatsCollector(db)
    collector._pending.extend(
        collector._build_event(username="u", provider="trae", model="m", ok=True)
        for _ in range(BATCH_MAX_PENDING))

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "transaction", boom)
    with pytest.raises(sqlite3.OperationalError):
        collector.record(username="overflow", provider="trae", model="m", ok=True)
    assert collector.write_failures() == 1


# ------------------------------------------------------------------ flush


def test_flush_writes_pending_rows(repo):
    """flush 后队列空、库里全在（进程退出/测试收尾的语义）。"""
    _credentials, db = repo
    collector = StatsCollector(db)
    for index in range(5):
        collector._pending.append(collector._build_event(
            username=f"u{index}", provider="trae", model="m", ok=True))
    assert _rows(db) == 0
    assert collector.flush() == 5
    assert _rows(db) == 5
    # 幂等：再 flush 一次没东西可写
    assert collector.flush() == 0


def test_flush_is_empty_in_steady_state(repo):
    """正常 `record()` 之后队列本来就是空的（等提交语义）。"""
    _credentials, db = repo
    collector = StatsCollector(db)
    collector.record(username="u", provider="trae", model="m", ok=True)
    assert collector.flush() == 0


def test_exit_hook_flushes_every_live_collector(repo):
    """进程退出钩子把各采集器残留队列写完（不依赖 main.py 的钩子）。"""
    _credentials, db = repo
    first, second = StatsCollector(db), StatsCollector(db)
    for collector in (first, second):
        collector._pending.append(collector._build_event(
            username="u", provider="trae", model="m", ok=True))
    collector_module._flush_all_collectors()
    assert _rows(db) == 2
    assert first.flush() == second.flush() == 0


def test_exit_hook_swallows_failures(repo, caplog):
    """退出钩子绝不向外抛（否则解释器关闭时会打 traceback）。"""
    _credentials, db = repo
    collector = StatsCollector(db)
    collector._pending.append(collector._build_event(
        username="u", provider="trae", model="m", ok=True))

    def boom():
        raise RuntimeError("disk gone")

    collector.flush = boom   # type: ignore[method-assign]
    with caplog.at_level("WARNING"):
        collector_module._flush_all_collectors()      # 不抛
    assert "退出时统计落库失败" in caplog.text


# ------------------------------------------------------------------ 写失败


def test_write_failure_is_counted_and_not_swallowed(repo, monkeypatch):
    """写失败要能被看见：计数递增 + 异常向上抛（调用方不再「静默吞掉」）。"""
    _credentials, db = repo
    collector = StatsCollector(db)

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "transaction", boom)
    assert collector.write_failures() == 0
    with pytest.raises(sqlite3.OperationalError):
        collector.record(username="u", provider="trae", model="m", ok=True)
    assert collector.write_failures() == 1
    # 失败的行回填队列，没有被丢掉
    assert _rows(db) == 0
    assert len(collector._pending) == 1


def test_failed_rows_are_retried_by_the_next_writer(repo, monkeypatch):
    """失败回填后，下一个写者把积压一起写掉——行不丢。"""
    _credentials, db = repo
    collector = StatsCollector(db)
    original = db.transaction
    failures = {"left": 1}

    def flaky_transaction():
        if failures["left"] > 0:
            failures["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return original()

    monkeypatch.setattr(db, "transaction", flaky_transaction)
    with pytest.raises(sqlite3.OperationalError):
        collector.record(username="u", provider="trae", model="m", ok=True)
    assert collector.write_failures() == 1
    # 第二个写者（使用同一条路）把回填的行和自己的行一起提交
    collector.record(username="u2", provider="trae", model="m", ok=True)
    assert _rows(db) == 2
    assert collector.write_failures() == 1     # 成功不递增


def test_write_failures_counts_rows_of_the_failed_batch(repo, monkeypatch):
    """计数单位是**行**：一批 3 行全失败就是 +3。"""
    _credentials, db = repo
    collector = StatsCollector(db)
    for index in range(3):
        collector._pending.append(collector._build_event(
            username=f"u{index}", provider="trae", model="m", ok=True))

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "transaction", boom)
    with pytest.raises(sqlite3.OperationalError):
        collector.flush()
    assert collector.write_failures() == 3
    assert len(collector._pending) == 3        # 全部回填


def test_db_error_counters_stay_per_instance(repo):
    """计数是进程内每实例的：一个采集器失败不影响另一个的读数。"""
    _credentials, db = repo
    first, second = StatsCollector(db), StatsCollector(db)
    assert first.write_failures() == 0 and second.write_failures() == 0


# ------------------------------------------------------------------ 回调时机


def test_callbacks_fire_once_per_row_after_commit(repo):
    """合并事务不改变回调契约：每行一次，且都在提交之后。"""
    _credentials, db = repo
    seen: list[int] = []
    writes: list[int] = []
    collector = StatsCollector(
        db,
        on_record=lambda row: seen.append(_rows(db)),
        on_write=lambda: writes.append(1))
    for _ in range(3):
        collector._pending.append(collector._build_event(
            username="u", provider="trae", model="m", ok=True))
    collector.flush()
    # 每行广播时该行都已可读（计数在递增）
    assert seen == [3, 3, 3]
    # 快照失效每批一次即可（不是每行一次）
    assert writes == [1]


def test_batch_write_rolls_back_all_rows_together(repo, monkeypatch):
    """一批里任何一行失败 → 整批回滚，不回填半批（避免重复计数）。"""
    _credentials, db = repo
    collector = StatsCollector(db)
    for index in range(BATCH_MAX_EVENTS):
        collector._pending.append(collector._build_event(
            username=f"u{index}", provider="trae", model="m", ok=True))

    # 第 BATCH_MAX_EVENTS 行的 upsert 失败 → 事务回滚，前面已执行的行一起回滚
    original = collector_module.StatsCollector._bump_hourly
    calls = {"n": 0}

    def fail_last(conn, event):
        calls["n"] += 1
        if calls["n"] == BATCH_MAX_EVENTS:
            raise sqlite3.OperationalError("disk I/O error")
        return original(conn, event)

    monkeypatch.setattr(collector_module.StatsCollector, "_bump_hourly",
                        staticmethod(fail_last))
    with pytest.raises(sqlite3.OperationalError):
        collector.flush()
    assert _rows(db) == 0
    assert collector.write_failures() == BATCH_MAX_EVENTS
    assert len(collector._pending) == BATCH_MAX_EVENTS
