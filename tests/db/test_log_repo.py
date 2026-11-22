"""日志环形表（`log_events`）：入队 / 批量落库 / 容量裁剪 / 查询 / 保留期清理 / 接线。"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from src.config import Settings
from src.db.conn import Database
from src.db.log_repo import LogRepository
from src.webapp.log_buffer import LogBuffer
from tests.helpers.constants import SECRET

# 本模块创建的仓库统一在用例结束后 close：消费者是 daemon 线程，泄漏会让
# 同一进程里堆积大量空转线程（每个还持一个 SQLite 连接）。
_OPEN: list[LogRepository] = []


@pytest.fixture(autouse=True)
def _close_repos():
    yield
    for repo in _OPEN:
        repo.close(timeout=2)
    _OPEN.clear()


def _repo(db, capacity: int = 100, *, value: list[int] | None = None,
          **kwargs) -> LogRepository:
    """`capacity` 用可变列表装：热更调小容量的场景直接改 `value[0]`。

    攒批窗口压到 10ms：默认 200ms 会让每个「写一行再看结果」的用例都空等
    一个攒批周期，而攒批本身有专门的用例覆盖。
    """
    box = value if value is not None else [capacity]
    kwargs.setdefault("flush_interval", 0.01)
    repo = LogRepository(db, capacity=lambda: box[0], **kwargs)
    _OPEN.append(repo)
    return repo


def _rows(db) -> list[tuple]:
    return [tuple(row) for row in db.connect().execute(
        "SELECT seq, level, logger, message, request_id FROM log_events ORDER BY seq")]


def test_append_writes_row_and_ignores_when_disabled(db):
    repo = _repo(db, capacity=0)
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="a")
    assert repo.flush(1)
    assert _rows(db) == []                       # 关闭时不落库
    box = [0]
    repo = _repo(db, value=box)
    box[0] = 5                                   # 热更打开
    repo.append(ts=2.5, level="WARNING", logger_name="src.y", message="b",
                request_id="req-1")
    assert repo.flush(1)
    rows = _rows(db)
    assert rows == [(1, "WARNING", "src.y", "b", "req-1")]
    # ts 落整数秒（排障够用，省空间）
    assert db.connect().execute("SELECT ts FROM log_events").fetchone()[0] == 2


def test_ring_prunes_oldest_rows_beyond_capacity(db):
    """保留**最近 N 行**（不是按时间）：重启前后口径一致。"""
    repo = _repo(db, capacity=3)
    for index in range(5):
        repo.append(ts=float(index), level="INFO", logger_name="src.x",
                    message=f"m{index}")
    assert repo.flush(2)
    assert [row[3] for row in _rows(db)] == ["m2", "m3", "m4"]


def test_ring_prunes_in_batches(db):
    """超额很多时分批删除（单次 DELETE 过大会长时间持写锁）。"""
    from src.db import log_repo as module

    repo = _repo(db, capacity=2)
    for index in range(5):
        repo.append(ts=float(index), level="INFO", logger_name="src.x",
                    message=f"m{index}")
    assert repo.flush(2)
    assert len(_rows(db)) == 2

    # 把批大小压到 1，验证循环删到容量为止
    original = module._PRUNE_BATCH
    module._PRUNE_BATCH = 1
    try:
        for index in range(5, 9):
            repo.append(ts=float(index), level="INFO", logger_name="src.x",
                        message=f"m{index}")
        assert repo.flush(2)
    finally:
        module._PRUNE_BATCH = original
    assert [row[3] for row in _rows(db)] == ["m7", "m8"]


def test_append_swallows_errors_once(db, capsys):
    """落库失败不上抛（日志处理器里不能炸业务），且只告警一次。"""

    class Flaky:
        """先坏后好再坏的库：验证告警「每次新故障只报一次」。"""

        def __init__(self, good) -> None:
            self._good = good
            self.broken = True

        def connect(self):
            if self.broken:
                raise sqlite3.OperationalError("disk I/O error")
            return self._good.connect()

        def transaction(self):
            if self.broken:
                raise sqlite3.OperationalError("disk I/O error")
            return self._good.transaction()

    logs = Flaky(db)
    repo = LogRepository(logs, capacity=lambda: 10, flush_interval=0.01)
    _OPEN.append(repo)
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="a")
    assert repo.flush(2)
    repo.append(ts=2.0, level="INFO", logger_name="src.x", message="b")
    assert repo.flush(2)
    assert capsys.readouterr().err.count("日志持久化失败") == 1

    # 恢复成功会重置告警位：再次故障时重新告警（不是永久静音）
    logs.broken = False
    repo.append(ts=3.0, level="INFO", logger_name="src.x", message="c")
    assert repo.flush(2)
    assert capsys.readouterr().err == ""
    logs.broken = True
    repo.append(ts=4.0, level="INFO", logger_name="src.x", message="d")
    assert repo.flush(2)
    assert capsys.readouterr().err.count("日志持久化失败") == 1


def test_queue_full_drops_rows_without_blocking(db, capsys):
    """队列满即丢：绝不阻塞调用方，丢弃计数只告警一次。"""
    repo = _repo(db, capacity=100, maxsize=1, autostart=False)
    started = time.monotonic()
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="留下")
    repo.append(ts=2.0, level="INFO", logger_name="src.x", message="丢弃")
    repo.append(ts=3.0, level="INFO", logger_name="src.x", message="再丢弃")
    assert time.monotonic() - started < 1.0        # 没有消费者也不许等
    assert repo.dropped == 2
    assert repo.flush(0.01) is False               # 没人消费 → 排不空
    assert _rows(db) == []
    assert repo._worker is None                    # noqa: SLF001 - 未启动消费者
    assert capsys.readouterr().err.count("日志落库队列已满") == 1
    repo.close(timeout=0.01)                       # 队里那一行没人消费，别让夹具空等


def test_flush_after_lazy_worker_start_drains_queue(db):
    """`flush` 超时说明队列还没人消费；启动消费者后排空。"""
    repo = _repo(db, capacity=10, autostart=False)
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="晚点写")
    assert repo.flush(0.01) is False
    repo._autostart = True                         # noqa: SLF001 - 模拟懒启动
    repo.append(ts=2.0, level="INFO", logger_name="src.x", message="第二条")
    assert repo.flush(2) is True
    assert [row[3] for row in _rows(db)] == ["晚点写", "第二条"]


def test_idle_consumer_exits_and_is_restarted_by_next_append(db):
    """空闲超时后消费者自行退出（不留空转线程），下次写入自动拉起。"""
    repo = _repo(db, capacity=10, idle_exit=0.01)
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="一")
    assert repo.flush(2)
    worker = repo._worker                          # noqa: SLF001 - 等它自己退出
    assert worker is not None
    worker.join(2)
    assert not worker.is_alive()
    assert repo._worker is None                    # noqa: SLF001 - 槽位已交还

    repo.append(ts=2.0, level="INFO", logger_name="src.x", message="二")
    assert repo.flush(2)
    assert [row[3] for row in _rows(db)] == ["一", "二"]


def test_idle_exit_skipped_when_queue_got_new_rows(db):
    """空闲判定与入队在同一把锁里：刚入队的行不会被「正在退出」的消费者漏掉。"""
    repo = _repo(db, capacity=10, autostart=False, idle_exit=0.0)
    repo._autostart = True                         # noqa: SLF001
    repo._start_worker_locked()                    # noqa: SLF001 - 手动拉起
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="抢在退出前")
    assert repo.flush(2)
    assert [row[3] for row in _rows(db)] == ["抢在退出前"]


def test_idle_exit_due_ignores_slot_taken_by_new_worker(db):
    """槽位已被新线程接管：旧线程只退出，不能把别人的槽清掉。"""
    repo = _repo(db, capacity=10, autostart=False, idle_exit=0.0)
    other = threading.Thread(target=lambda: None)
    other.start()
    other.join()
    repo._worker = other                           # noqa: SLF001 - 模拟已换人
    assert repo._idle_exit_due(0.0) is False       # noqa: SLF001
    assert repo._worker is other                   # noqa: SLF001


def test_idle_exit_due_keeps_worker_when_queue_not_empty(db):
    """空闲超时但队列里还有行：不能退出（否则这行永远不落库）。"""
    repo = _repo(db, capacity=10, autostart=False, idle_exit=0.0)
    repo._worker = threading.current_thread()      # noqa: SLF001 - 假装自己就是消费者
    repo._queue.put_nowait((1.0, "INFO", "src.x", "排队中", None))  # noqa: SLF001
    try:
        assert repo._idle_exit_due(0.0) is False   # noqa: SLF001
        assert repo._worker is threading.current_thread()  # noqa: SLF001
    finally:
        repo._worker = None                        # noqa: SLF001 - 夹具 close 时别 join 自己


def test_idle_exit_due_releases_slot_when_idle(db):
    repo = _repo(db, capacity=10, autostart=False, idle_exit=0.0)
    repo._worker = threading.current_thread()      # noqa: SLF001
    try:
        assert repo._idle_exit_due(0.0) is True    # noqa: SLF001
        assert repo._worker is None                # noqa: SLF001
    finally:
        repo._worker = None                        # noqa: SLF001


def test_idle_exit_due_waits_until_timeout(db):
    """未到空闲阈值直接返回 False（不碰锁、不看队列）。"""
    repo = _repo(db, capacity=10, autostart=False, idle_exit=60.0)
    assert repo._idle_exit_due(time.monotonic()) is False  # noqa: SLF001


def test_close_is_idempotent_and_flushes_pending_rows(db):
    repo = _repo(db, capacity=10)
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="关前")
    repo.close()
    assert [row[3] for row in _rows(db)] == ["关前"]
    repo.close()                                   # 幂等：重复调用无副作用
    repo.append(ts=2.0, level="INFO", logger_name="src.x", message="关后")
    assert repo.flush(0.01) is True                # 关闭后不再入队
    assert len(_rows(db)) == 1


def test_close_without_worker_is_noop(db):
    """从未写入过 / 落库关闭的仓库没有消费者线程，close 直接返回。"""
    repo = _repo(db, capacity=0)
    repo.close()
    repo.close()
    assert repo._worker is None                    # noqa: SLF001 - 未启动消费者


def test_small_batch_size_still_persists_every_row(db):
    """每批 ≤ batch_size 行：批量小只是事务多几次，一行都不能丢。"""
    repo = _repo(db, capacity=10, batch_size=1)
    for index in range(3):
        repo.append(ts=float(index), level="INFO", logger_name="src.x",
                    message=f"m{index}")
    assert repo.flush(2)
    assert [row[3] for row in _rows(db)] == ["m0", "m1", "m2"]


def test_batch_collection_stops_when_window_expires(db, monkeypatch):
    """攒批窗口到点必须落库，而不是一直等满 `batch_size`。

    用假时钟把 `monotonic` 每次调用推进 10s：窗口立即过期 → 每批只写当前
    已入队的那一行。
    """
    from src.db import log_repo as module

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            self.now += 10.0
            return self.now

    monkeypatch.setattr(module.time, "monotonic", Clock())
    repo = _repo(db, capacity=10, batch_size=50, flush_interval=0.2)
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="一")
    repo.append(ts=2.0, level="INFO", logger_name="src.x", message="二")
    assert repo.flush(2)
    assert [row[3] for row in _rows(db)] == ["一", "二"]


def test_append_dropped_when_close_races_after_enqueue_check(db):
    """`close()` 在「已通过开启检查」之后抢到锁：该行直接丢，不留在队列里。

    留着的话没人消费，`flush` 永远等不到归零（停机时会把关闭流程卡到超时）。
    """

    class ClosingLock:
        """进入临界区时把仓库标成已关闭，模拟 `close()` 抢先拿到锁。"""

        def __init__(self, repo) -> None:
            self._repo = repo
            self._real = repo._worker_lock      # noqa: SLF001

        def __enter__(self):
            self._real.acquire()
            self._repo._closed = True           # noqa: SLF001
            return self

        def __exit__(self, *exc):
            self._real.release()
            return False

    repo = _repo(db, capacity=10)
    repo._worker_lock = ClosingLock(repo)       # noqa: SLF001
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="关门时来的")
    assert repo.flush(0.01) is True             # 没留下永远等不到的行
    assert _rows(db) == []

    """`close()` 之后消费者线程真的退出（停机不留下空转线程）。"""
    repo = LogRepository(db, capacity=lambda: 10, flush_interval=0.01)
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="残局")
    assert repo.flush(2)
    worker = repo._worker                          # noqa: SLF001 - 断言线程被停掉
    assert worker is not None and worker.is_alive()
    repo.close()
    assert not worker.is_alive()


def test_query_filters_by_request_id_since_and_level(db):
    repo = _repo(db, capacity=100)
    repo.append(ts=10.0, level="INFO", logger_name="src.x", message="a", request_id="r1")
    repo.append(ts=20.0, level="ERROR", logger_name="src.x", message="b", request_id="r1")
    repo.append(ts=30.0, level="INFO", logger_name="src.x", message="c", request_id="r2")
    assert repo.flush(2)

    by_request = repo.query(request_id="r1")
    assert [row["message"] for row in by_request] == ["a", "b"]
    assert all(row["request_id"] == "r1" for row in by_request)

    since = repo.query(since=15)
    assert [row["message"] for row in since] == ["b", "c"]

    level = repo.query(level="error")
    assert [row["message"] for row in level] == ["b"]

    assert [row["message"] for row in repo.query(limit=1)] == ["c"]


def test_query_limit_is_at_least_one(db):
    repo = _repo(db, capacity=10)
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="a")
    assert repo.flush(2)
    assert len(repo.query(limit=0)) == 1


def test_purge_removes_rows_older_than_retention(db):
    repo = _repo(db, capacity=100)
    repo.append(ts=1_000_000.0, level="INFO", logger_name="src.x", message="old")
    repo.append(ts=2_000_000.0, level="INFO", logger_name="src.x", message="new")
    assert repo.flush(2)
    removed = repo.purge(keep_days=1, now=2_000_000)
    assert removed == 1
    assert [row[3] for row in _rows(db)] == ["new"]


def test_buffer_persists_lines_before_publishing(db):
    """LogBuffer 接线：落库调用在推送之前（否则「看到了却没留痕」）。"""
    calls: list[dict] = []

    class Recorder:
        def append(self, **kwargs):
            calls.append(kwargs)

    buffer = LogBuffer(capacity=lambda: 10, persist=Recorder(), clock=lambda: 5.0)
    buffer.append(level="INFO", logger_name="src.x", message="hello",
                  request_id="req-9")
    assert calls == [{"ts": 5.0, "level": "INFO", "logger_name": "src.x",
                      "message": "hello", "request_id": "req-9"}]


def test_endpoint_reads_persisted_rows_and_filters_request_id(tmp_path):
    """`GET /api/logs`：落库开启时读 `log_events`，并支持按 `request_id` 回溯。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", LOG_PERSIST_LINES="20")
    app = build_app(settings)
    repo = app.state.services.log_repo
    with TestClient(app) as client:
        # 写在自己启动之后：启动期日志也进同一张表，容量 20 会把先写的挤掉
        repo.append(ts=1.0, level="INFO", logger_name="src.x", message="一号",
                    request_id="req-a")
        repo.append(ts=2.0, level="ERROR", logger_name="src.x", message="二号",
                    request_id="req-b")
        assert repo.flush(2)                       # 落库是后台线程：先排空再读
        client.cookies.set("coding2api_session",
                           create_session_token("root", SECRET))
        body = client.get("/api/logs").json()
        filtered = client.get("/api/logs", params={"request_id": "req-a"}).json()
    assert body["persisted"] is True
    assert body["capacity"] == 20
    messages = [line["message"] for line in body["lines"]]
    # 启动期日志也在同一张表里：只断言本用例写入的两条存在且保持顺序
    assert [m for m in messages if m in ("一号", "二号")] == ["一号", "二号"]
    assert [line["message"] for line in filtered["lines"]] == ["一号"]


def test_endpoint_falls_back_to_memory_ring_when_persist_off(tmp_path):
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", LOG_PERSIST_LINES="0")
    app = build_app(settings)
    app.state.log_buffer.append(level="INFO", logger_name="src.x",
                               message="只读内存", request_id="req-m")
    with TestClient(app) as client:
        client.cookies.set("coding2api_session",
                           create_session_token("root", SECRET))
        body = client.get("/api/logs").json()
        filtered = client.get("/api/logs",
                              params={"request_id": "req-m"}).json()
        missing = client.get("/api/logs",
                             params={"request_id": "nope"}).json()
    assert body["persisted"] is False
    assert any(line["message"] == "只读内存" for line in body["lines"])
    assert [line["message"] for line in filtered["lines"]] == ["只读内存"]
    assert missing["lines"] == []


def test_retention_task_purges_logs():
    from src.tasks.retention import RetentionTask

    class Collector:
        def rollup_hourly(self, since=None):
            return 0

        def purge_expired(self, days, now=None):
            return 0

    class Logs:
        def __init__(self) -> None:
            self.calls: list[tuple[int, object]] = []

        def purge(self, *, keep_days, now=None):
            self.calls.append((keep_days, now))
            return 7

    logs = Logs()
    task = RetentionTask(Collector(), retention_days=9, logs=logs)
    report = task.run_once(now=1234)
    assert logs.calls == [(9, 1234)]
    assert report["purged_logs"] == 7

    # 未装配时不上报该键之外的行为：值为 0 且不崩
    assert RetentionTask(Collector()).run_once(now=1)["purged_logs"] == 0


def test_append_inside_outer_transaction_does_not_commit_it(tmp_path):
    """事务内写日志不得提前提交外层事务。

    `append` 只入队、由消费者线程用自己的连接写库，因此**结构上**不可能把
    调用线程的外层事务提前提交；外层异常回滚后只剩自己的行被撤销，日志行
    由消费者线程独立落库（这正是「日志不随业务事务回滚」的期望口径）。
    """
    db = Database(tmp_path / "t.sqlite3", strict_permissions=False)
    try:
        conn = db.connect()
        conn.executescript(
            "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);"
            "CREATE TABLE log_events(seq INTEGER PRIMARY KEY, ts INTEGER, level TEXT,"
            " logger TEXT, message TEXT, request_id TEXT);")
        logs = LogRepository(db, capacity=lambda: 100, flush_interval=0.01)
        conn.execute("INSERT INTO t(id, v) VALUES (1, 'first')")
        conn.commit()

        with pytest.raises(RuntimeError), db.transaction() as tx:
            tx.execute("INSERT INTO t(id, v) VALUES (2, 'second')")
            logs.append(ts=1, level="WARNING", logger_name="x", message="inner")
            raise RuntimeError("boom")

        remaining = [row["id"] for row in
                     db.connect().execute("SELECT id FROM t ORDER BY id")]
        assert remaining == [1]                       # 外层回滚完整
        assert logs.flush(2)
        assert db.connect().execute(
            "SELECT COUNT(*) FROM log_events").fetchone()[0] == 1
        logs.close()
    finally:
        db.close()


def test_purge_inside_outer_transaction_does_not_commit_it(tmp_path):
    """`purge` 同样不得提前提交外层事务（与 `_write` 同一根因）。"""
    db = Database(tmp_path / "t.sqlite3", strict_permissions=False)
    try:
        conn = db.connect()
        conn.executescript(
            "CREATE TABLE t(id INTEGER PRIMARY KEY);"
            "CREATE TABLE log_events(seq INTEGER PRIMARY KEY, ts INTEGER, level TEXT,"
            " logger TEXT, message TEXT, request_id TEXT);")
        logs = LogRepository(db, capacity=lambda: 100, flush_interval=0.01)
        conn.execute("INSERT INTO t(id) VALUES (1)")
        conn.commit()

        with pytest.raises(RuntimeError), db.transaction() as tx:
            tx.execute("INSERT INTO t(id) VALUES (2)")
            logs.purge(keep_days=0, now=10 ** 9)
            raise RuntimeError("boom")

        assert [row["id"] for row in
                db.connect().execute("SELECT id FROM t ORDER BY id")] == [1]
        logs.close()
    finally:
        db.close()