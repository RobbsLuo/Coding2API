"""内容拒绝 → 归因发起者 + 阈值冻结（不换号、不冷却）。"""

from __future__ import annotations

import pytest

from src.compat.openai.request import parse_chat_request
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import AccessFreezeRepository, CredentialRepository
from src.engine.executor import (
    ContentRejected,
    Executor,
    ExecutorDeps,
    ProviderAccessFrozen,
    _business_code,
    _content_rejection_reason,
    _event_content_rejection,
)
from src.engine.scheduler import Scheduler
from src.provider.base import ErrKind, Event, EventKind
from src.provider.codebuddy.client import UpstreamHTTPError
from src.stats.collector import StatsCollector
from tests.helpers.constants import SECRET

CONTENT = Event(kind=EventKind.ERROR, error_code=11128,
                error_message="request illegal", error_kind=ErrKind.REQUEST)


class _Scripted:
    def __init__(self, provider_id: str, script) -> None:
        self.id = provider_id
        self.script = script
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        self.calls += 1
        for item in self.script:
            if isinstance(item, Exception):
                raise item
            yield item

    def list_models(self, _credential_data):
        return []


def _open(tmp_path):
    db = Database(tmp_path / "f.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"token": "b"})
    return db, credentials, AccessFreezeRepository(db)


def _executor(credentials, freezes, providers, threshold, db):
    return Executor(ExecutorDeps(
        providers=providers, credentials=credentials, scheduler=Scheduler(),
        default_model="glm-5.2", stats=StatsCollector(db),
        access_freezes=freezes, access_freeze_threshold=threshold))


def _request(stream: bool = False):
    return parse_chat_request({"model": "glm-5.2",
                               "stream": stream,
                               "messages": [{"role": "user", "content": "hi"}]})


def _usage_row(db):
    return db.connect().execute(
        "SELECT ok, error_type, provider FROM usage_events").fetchone()


async def test_threshold_zero_only_records_and_does_not_rotate(tmp_path):
    """默认阈值 0：只落命中记录 + usage，不换号、不冷却。"""
    db, credentials, freezes = _open(tmp_path)
    provider = _Scripted("trae", [CONTENT])
    executor = _executor(credentials, freezes, {"trae": provider}, 0, db)

    with pytest.raises(ContentRejected):
        await executor.complete(_request(), username="alice")

    assert provider.calls == 1                       # 没有换号重试
    assert freezes.count(subject="alice", provider="trae") == 1
    assert freezes.is_frozen(subject="alice", provider="trae", threshold=0) is False
    assert tuple(_usage_row(db)) == (0, "invalid_request", "trae")
    candidate = credentials.candidates(providers=["trae"])[0]
    assert candidate.cooling_until is None and candidate.err_count == 0
    db.close()


async def test_threshold_one_freezes_only_that_subject_and_provider(tmp_path):
    """阈值 1：命中后只拦该发起者 × 该渠道；其他人/其他渠道不受影响。"""
    db, credentials, freezes = _open(tmp_path)
    provider = _Scripted("trae", [CONTENT])
    executor = _executor(credentials, freezes, {"trae": provider}, lambda: 1, db)

    with pytest.raises(ContentRejected):
        await executor.complete(_request(), username="alice")

    # preflight 只关心注册表，换一个同时注册 codebuddy 的实例验证「其他渠道」
    executor = _executor(credentials, freezes,
                         {"trae": provider, "codebuddy": _Scripted("codebuddy", [])},
                         lambda: 1, db)
    with pytest.raises(ProviderAccessFrozen):
        executor.preflight(_request(), "trae", username="alice")
    # 同渠道的其他用户照常放行
    assert executor.preflight(_request(), "trae", username="bob").model == "glm-5.2"
    # 同一发起者换渠道照常放行（codebuddy 无冻结记录）
    assert executor.preflight(_request(), "codebuddy", username="alice").model == "glm-5.2"

    # 解冻后恢复
    assert freezes.unfreeze(subject="alice", provider="trae") == 1
    assert executor.preflight(_request(), "trae", username="alice").model == "glm-5.2"
    db.close()


async def test_unrecognized_http_content_shapes_stay_on_old_path():
    """识别不了的内容拒绝不改语义：400+11128 仍按请求级错误处理。"""
    assert _content_rejection_reason(
        UpstreamHTTPError(400, b'{"code":11128}')) is None
    assert _content_rejection_reason(
        UpstreamHTTPError(403, b'{"code":999}')) is None
    assert _content_rejection_reason(RuntimeError("boom")) is None
    assert _business_code("11-128") == 11128
    assert _business_code(11128) == 11128
    assert _business_code("n/a") is None


async def test_http_403_content_rejection_is_recorded(tmp_path):
    """HTTP 403 + 11128（body 形态）同样识别为内容拒绝。"""
    db, credentials, freezes = _open(tmp_path)
    provider = _Scripted("codebuddy", [UpstreamHTTPError(403, b'{"code":"11-128"}')])
    executor = _executor(credentials, freezes, {"codebuddy": provider}, 0, db)
    with pytest.raises(ContentRejected):
        await executor.complete(parse_chat_request(
            {"model": "glm-5.2@codebuddy",
             "messages": [{"role": "user", "content": "hi"}]}), username="alice")
    assert provider.calls == 1
    assert freezes.count(subject="alice", provider="codebuddy") == 1
    db.close()


async def test_stream_content_rejection_before_and_after_emit(tmp_path):
    """流式两处收尾：未出帧 → 首帧错误；已出帧 → 错误帧收尾，均不换号。"""
    db, credentials, freezes = _open(tmp_path)
    provider = _Scripted("trae", [CONTENT])
    executor = _executor(credentials, freezes, {"trae": provider}, 0, db)
    chunks = [chunk async for chunk in executor.stream(_request(stream=True),
                                                       username="alice")]
    assert b"invalid_request" in chunks[-1]
    assert provider.calls == 1
    assert freezes.count(subject="alice", provider="trae") == 1

    provider2 = _Scripted("trae", [Event(kind=EventKind.CONTENT, content="hi"),
                                   CONTENT])
    executor2 = _executor(credentials, freezes, {"trae": provider2}, 0, db)
    chunks2 = [chunk async for chunk in executor2.stream(_request(stream=True),
                                                          username="alice")]
    assert b'"hi"' in chunks2[0]
    assert b"invalid_request" in chunks2[-1]
    assert provider2.calls == 1
    assert freezes.count(subject="alice", provider="trae") == 2
    db.close()


async def test_freeze_record_failure_does_not_break_response(tmp_path, monkeypatch):
    """冻结命中写库失败只告警：内容拒绝的 400 仍必须回给客户端。"""
    db, credentials, freezes = _open(tmp_path)
    provider = _Scripted("trae", [CONTENT])
    executor = _executor(credentials, freezes, {"trae": provider}, 0, db)

    def _boom(**_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(freezes, "record_hit", _boom)
    with pytest.raises(ContentRejected):
        await executor.complete(_request(), username="alice")
    assert tuple(_usage_row(db)) == (0, "invalid_request", "trae")
    db.close()


async def test_content_rejection_without_freeze_repo(tmp_path):
    """未装配冻结仓储（旧装配/测试替身）：按旧行为只记 usage，不崩。"""
    db, credentials, _freezes = _open(tmp_path)
    provider = _Scripted("trae", [CONTENT])
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2",
        stats=StatsCollector(db)))
    with pytest.raises(ContentRejected):
        await executor.complete(_request(), username="alice")
    assert tuple(_usage_row(db)) == (0, "invalid_request", "trae")
    db.close()


async def test_preflight_skips_freeze_check_when_no_provider_registered(tmp_path):
    """链项没有已注册渠道时直接走原 NoProviderForModel（冻结检查不抢答）。"""
    db, credentials, freezes = _open(tmp_path)
    executor = _executor(credentials, freezes, {}, 1, db)
    freezes.record_hit(subject="alice", provider="trae", reason="code=11128")
    from src.engine.executor import NoProviderForModel
    with pytest.raises(NoProviderForModel):
        executor.preflight(_request(), "codebuddy", username="alice")
    db.close()


def test_event_content_rejection_code_variants():
    assert _event_content_rejection(
        Event(kind=EventKind.ERROR, error_code="11-128"))
    assert not _event_content_rejection(
        Event(kind=EventKind.ERROR, error_code=None))
    assert not _event_content_rejection(
        Event(kind=EventKind.ERROR, error_code=11102))

async def test_stream_http_403_content_rejection(tmp_path):
    """流式 HTTP 403 + 11128 同样在出帧前识别为内容拒绝，不换号。"""
    db, credentials, freezes = _open(tmp_path)
    provider = _Scripted("codebuddy",
                         [UpstreamHTTPError(403, b'{"code":11128,"msg":"illegal"}')])
    executor = _executor(credentials, freezes, {"codebuddy": provider}, 0, db)
    chunks = [chunk async for chunk in executor.stream(parse_chat_request(
        {"model": "glm-5.2@codebuddy", "stream": True,
         "messages": [{"role": "user", "content": "hi"}]}), username="alice")]
    assert b"invalid_request" in chunks[-1]
    assert provider.calls == 1
    assert freezes.count(subject="alice", provider="codebuddy") == 1
    db.close()


def test_freeze_subject_prefers_username_then_context_key_id():
    """无用户名时冻结归属回落到请求上下文的 API Key id（绝不读入站头）。"""
    from src.engine.executor import Executor as _Executor
    from src.request_context import set_api_key_id

    executor = object.__new__(_Executor)
    assert executor._freeze_subject("alice") == "alice"
    set_api_key_id("key_ctx")
    try:
        assert executor._freeze_subject("unknown") == "key_ctx"
        assert executor._freeze_subject("") == "key_ctx"
    finally:
        set_api_key_id(None)
    assert executor._freeze_subject("unknown") == "unknown"


def test_content_rejection_reason_string_body_and_bad_event_code():
    """自定义错误体为字符串也解析；不可解析的事件码不参与命中。"""
    class _StringBody(Exception):
        status = 403
        body = '{"code":"11-128"}'

    assert _content_rejection_reason(_StringBody()) == "code=11128"

    weird = Event(kind=EventKind.ERROR, error_code="n/a")
    assert not _event_content_rejection(weird)


def test_degraded_trace_helper_marks_shared_object():
    """fallback 组命中时标记共享轨迹（响应头 X-Upstream-Degraded=true）。"""
    from src.engine.executor import (
        _note_upstream_degraded,
        begin_upstream_trace,
        clear_upstream_trace,
    )

    trace = begin_upstream_trace()
    try:
        _note_upstream_degraded()
        assert trace.degraded is True
    finally:
        clear_upstream_trace()


def test_content_rejection_reason_bad_event_code_is_ignored():
    """事件码无法归一（非数字）时不算命中；避免把普通错误冻结成内容拒绝。"""
    class _BadEvent:
        event = Event(kind=EventKind.ERROR, error_code="n/a")

    assert _content_rejection_reason(_BadEvent()) is None
