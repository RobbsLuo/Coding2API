"""非流式客户端早断：与流式同口径记 client_disconnect，且不凭证冷却。"""

from __future__ import annotations

import asyncio

import pytest
from starlette.requests import ClientDisconnect

from src.compat.openai.request import parse_chat_request
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.executor import Executor, ExecutorDeps
from src.engine.scheduler import Scheduler
from src.provider.base import Event, EventKind, Usage
from src.stats.collector import StatsCollector
from tests.helpers.constants import SECRET


class _Scripted:
    id = "trae"

    def __init__(self, script) -> None:
        self.script = script
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        self.calls += 1
        for event in self.script:
            yield event

    def list_models(self, _credential_data):
        return []


class _Blocking:
    id = "trae"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        self.calls += 1
        yield Event(kind=EventKind.USAGE, usage=Usage(3, 4, 1))
        self.started.set()
        await asyncio.Event().wait()      # 模拟上游还在吐、客户端已走

    def list_models(self, _credential_data):
        return []


def _open(tmp_path):
    db = Database(tmp_path / "d.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    return db, credentials


def _executor(credentials, provider, db):
    return Executor(ExecutorDeps(providers={"trae": provider},
                                 credentials=credentials,
                                 scheduler=Scheduler(),
                                 default_model="glm-5.2",
                                 stats=StatsCollector(db)))


def _request():
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}]})


async def _true():
    return True


async def test_nonstream_disconnect_records_partial_usage(tmp_path):
    """断开前拿到的 usage 照记，ok=False + client_disconnect，不冷却凭证。"""
    db, credentials = _open(tmp_path)
    provider = _Scripted([Event(kind=EventKind.USAGE, usage=Usage(5, 7, 2)),
                          Event(kind=EventKind.CONTENT, content="ignored")])
    executor = _executor(credentials, provider, db)

    calls = 0

    async def probe():
        nonlocal calls
        calls += 1
        return calls > 1              # 进聚合前 false；拿到第一个事件后 true

    with pytest.raises(ClientDisconnect):
        await executor.complete(_request(), username="u", is_disconnected=probe)

    row = db.connect().execute(
        "SELECT ok, error_type, input_tokens, output_tokens, reasoning_tokens "
        "FROM usage_events").fetchone()
    assert tuple(row) == (0, "client_disconnect", 5, 7, 2)
    candidate = credentials.candidates(providers=["trae"])[0]
    assert candidate.cooling_until is None
    assert candidate.err_count == 0
    db.close()


async def test_nonstream_disconnect_before_first_event_still_records_request(tmp_path):
    """一个 token 都没拿到也记一行（口径：拿不到就记请求）。"""
    db, credentials = _open(tmp_path)
    provider = _Scripted([Event(kind=EventKind.CONTENT, content="never")])
    executor = _executor(credentials, provider, db)

    with pytest.raises(ClientDisconnect):
        await executor.complete(_request(), username="u",
                                is_disconnected=_true)

    row = db.connect().execute(
        "SELECT ok, error_type, input_tokens FROM usage_events").fetchone()
    assert tuple(row) == (0, "client_disconnect", None)
    assert provider.calls == 0          # 断开在打开上游流之前，白打都不该发生
    db.close()


async def test_nonstream_cancellation_records_disconnect(tmp_path):
    """非流式保活/连接超时取消工作协程：同样记 client_disconnect。"""
    db, credentials = _open(tmp_path)
    provider = _Blocking()
    executor = _executor(credentials, provider, db)

    task = asyncio.create_task(executor.complete(_request(), username="u"))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = db.connect().execute(
        "SELECT ok, error_type, input_tokens, output_tokens FROM usage_events"
    ).fetchone()
    assert tuple(row) == (0, "client_disconnect", 3, 4)
    db.close()