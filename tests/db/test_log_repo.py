"""日志环形表（`log_events`）：写入 / 容量裁剪 / 查询 / 保留期清理 / 落库接线。"""

from __future__ import annotations

import sqlite3

import pytest

from src.config import Settings
from src.db.conn import Database
from src.db.log_repo import LogRepository
from src.webapp.log_buffer import LogBuffer
from tests.helpers.constants import SECRET


def _repo(db, capacity: int = 100, *, value: list[int] | None = None) -> LogRepository:
    """`capacity` 用可变列表装：热更调小容量的场景直接改 `value[0]`。"""
    box = value if value is not None else [capacity]
    return LogRepository(db, capacity=lambda: box[0])


def _rows(db) -> list[tuple]:
    return [tuple(row) for row in db.connect().execute(
        "SELECT seq, level, logger, message, request_id FROM log_events ORDER BY seq")]


def test_append_writes_row_and_ignores_when_disabled(db):
    repo = _repo(db, capacity=0)
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="a")
    assert _rows(db) == []                       # 关闭时不落库
    box = [0]
    repo = _repo(db, value=box)
    box[0] = 5                                   # 热更打开
    repo.append(ts=2.5, level="WARNING", logger_name="src.y", message="b",
                request_id="req-1")
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
    assert [row[3] for row in _rows(db)] == ["m2", "m3", "m4"]


def test_ring_prunes_in_batches(db):
    """超额很多时分批删除（单次 DELETE 过大会长时间持写锁）。"""
    from src.db import log_repo as module

    repo = _repo(db, capacity=2)
    for index in range(5):
        repo.append(ts=float(index), level="INFO", logger_name="src.x",
                    message=f"m{index}")
    assert len(_rows(db)) == 2

    # 把批大小压到 1，验证循环删到容量为止
    original = module._PRUNE_BATCH
    module._PRUNE_BATCH = 1
    try:
        for index in range(5, 9):
            repo.append(ts=float(index), level="INFO", logger_name="src.x",
                        message=f"m{index}")
    finally:
        module._PRUNE_BATCH = original
    assert [row[3] for row in _rows(db)] == ["m7", "m8"]


def test_append_swallows_errors_once(db, capsys):
    """落库失败不上抛（日志处理器里不能炸业务），且只告警一次。"""

    class Broken:
        def connect(self):
            raise sqlite3.OperationalError("disk I/O error")

    repo = LogRepository(Broken(), capacity=lambda: 10)
    repo.append(ts=1.0, level="INFO", logger_name="src.x", message="a")
    repo.append(ts=2.0, level="INFO", logger_name="src.x", message="b")
    stderr = capsys.readouterr().err
    assert stderr.count("日志持久化失败") == 1

    # 恢复后再次失败 → 重新告警（不是永久静音）
    repo.append(ts=3.0, level="INFO", logger_name="src.x", message="c")
    assert capsys.readouterr().err.count("日志持久化失败") == 0


def test_query_filters_by_request_id_since_and_level(db):
    repo = _repo(db, capacity=100)
    repo.append(ts=10.0, level="INFO", logger_name="src.x", message="a", request_id="r1")
    repo.append(ts=20.0, level="ERROR", logger_name="src.x", message="b", request_id="r1")
    repo.append(ts=30.0, level="INFO", logger_name="src.x", message="c", request_id="r2")

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
    assert len(repo.query(limit=0)) == 1


def test_purge_removes_rows_older_than_retention(db):
    repo = _repo(db, capacity=100)
    repo.append(ts=1_000_000.0, level="INFO", logger_name="src.x", message="old")
    repo.append(ts=2_000_000.0, level="INFO", logger_name="src.x", message="new")
    removed = repo.purge(keep_days=1, now=2_000_000)
    assert removed == 1
    assert [row[3] for row in _rows(db)] == ["new"]


def test_buffer_persists_lines_before_publishing(db):
    """LogBuffer 接线：落库在推送之前（否则「看到了却没留痕」）。"""
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

    旧实现走裸 `connect()` + `commit()`，绕过 `tx_depth` 记账：同一线程在
    `with db.transaction()` 里产生一条日志，就把外层未提交写入提前提交了，
    后来的异常只能回滚后半段，留下半完成状态。
    """
    db = Database(tmp_path / "t.sqlite3", strict_permissions=False)
    try:
        conn = db.connect()
        conn.executescript(
            "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);"
            "CREATE TABLE log_events(seq INTEGER PRIMARY KEY, ts INTEGER, level TEXT,"
            " logger TEXT, message TEXT, request_id TEXT);")
        logs = LogRepository(db, capacity=lambda: 100)
        conn.execute("INSERT INTO t(id, v) VALUES (1, 'first')")
        conn.commit()

        with pytest.raises(RuntimeError), db.transaction() as tx:
            tx.execute("INSERT INTO t(id, v) VALUES (2, 'second')")
            logs.append(ts=1, level="WARNING", logger_name="x", message="inner")
            raise RuntimeError("boom")

        remaining = [row["id"] for row in
                     db.connect().execute("SELECT id FROM t ORDER BY id")]
        assert remaining == [1]                       # 外层回滚完整
        assert db.connect().execute(
            "SELECT COUNT(*) FROM log_events").fetchone()[0] == 0
    finally:
        db.close()


def test_purge_inside_outer_transaction_does_not_commit_it(tmp_path):
    """`purge` 同样不得提前提交外层事务（与 append 同一根因）。"""
    db = Database(tmp_path / "t.sqlite3", strict_permissions=False)
    try:
        conn = db.connect()
        conn.executescript(
            "CREATE TABLE t(id INTEGER PRIMARY KEY);"
            "CREATE TABLE log_events(seq INTEGER PRIMARY KEY, ts INTEGER, level TEXT,"
            " logger TEXT, message TEXT, request_id TEXT);")
        logs = LogRepository(db, capacity=lambda: 100)
        conn.execute("INSERT INTO t(id) VALUES (1)")
        conn.commit()

        with pytest.raises(RuntimeError), db.transaction() as tx:
            tx.execute("INSERT INTO t(id) VALUES (2)")
            logs.purge(keep_days=0, now=10 ** 9)
            raise RuntimeError("boom")

        assert [row["id"] for row in
                db.connect().execute("SELECT id FROM t ORDER BY id")] == [1]
    finally:
        db.close()
