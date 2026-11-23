"""上游「响应不符合协议」异常的分类与留痕（P0-1）。

`UpstreamProtocolViolation` / `SSEFrameTooLarge` / JSON 解析失败 / 会话票据
失效这几类此前不在 `_classify` 的识别范围内，返回 None 后调用点直接 `raise`，
最终被 `stream_guarded` 兜成 500 `internal_error`：不冷却、不写
`usage_events`、已出帧的 token 全部漏记。

本文件锁住修复后的口径：

- 归 `UPSTREAM_PROTOCOL`：零冷却、不罚号（`NO_PENALTY_KINDS`），但**必须留痕**。
- 已出帧后抛错：一帧错误收尾、不换号；`usage_events` 记下已出帧的 token，
  凭证不写冷却。
- `_classify` 仍不认识的异常（`kind is None`）：同样**先落一行再上抛**。
- 一次请求只记一行（已由 DEAD 尝试行覆盖且未出帧时不重复记账）。
"""

from __future__ import annotations

import json
import time

import pytest

from src.compat.openai.request import parse_chat_request
from src.engine.executor import (
    Executor,
    ExecutorDeps,
    _classify,
    _error_type_for,
)
from src.engine.scheduler import Candidate, Scheduler
from src.engine.sse import SSEFrameTooLarge
from src.provider.base import (
    NO_PENALTY_KINDS,
    ErrKind,
    Event,
    EventKind,
    UpstreamProtocolViolation,
    UpstreamReloginRequired,
    Usage,
)
from src.provider.codearts.client import _WebCookieExpired
from src.provider.lingxi.client import RunExpired
from src.stats.collector import StatsCollector
from tests.helpers.events import TRAE_GOOD


class _ProtocolProvider:
    """按调用次数回放脚本的 trae 替身：列表元素是异常实例时抛出。

    与 `tests/engine/test_executor_transport.py` 的 `TransportProvider` 同构，
    多记一份 token 是为了断言**是哪一张凭证**被（或没被）冷却。
    """

    id = "trae"

    def __init__(self, script: list) -> None:
        self.script = script
        self.calls = 0
        self.tokens: list[str] = []

    async def stream_chat(self, credential_data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        self.tokens.append(credential_data.get("accessToken", ""))
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    async def list_models(self, _credential_data):
        return []


class _UnknownError(RuntimeError):
    """`_classify` 完全不认识的异常（无 `kind()`），走 `kind is None` 分支。"""


class _DeadError(Exception):
    """分类为 DEAD 的异常：用来构造「已落过尝试行」的 `attributed=True`。"""

    def kind(self) -> ErrKind:
        return ErrKind.DEAD


def _build(credentials, script, **deps) -> tuple[Executor, _ProtocolProvider]:
    provider = _ProtocolProvider(script)
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", **deps))
    return executor, provider


def _add(credentials, token: str) -> str:
    return credentials.add(provider="trae", credential_data={"accessToken": token})


def _stream_request():
    return parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True})


def _complete_request():
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}]})


def _credential(credentials, credential_id: str):
    return next(c for c in credentials.candidates()
                if c.credential_id == credential_id)


def _rows(db) -> list:
    return db.connect().execute(
        "SELECT ok, error_type, input_tokens, output_tokens, reasoning_tokens "
        "FROM usage_events ORDER BY ts").fetchall()


# --------------------------------------------------------- 分类口径（纯函数）


@pytest.mark.parametrize("error", [
    UpstreamProtocolViolation("non-json upstream body"),
    UpstreamReloginRequired("refresh token has been used"),
    SSEFrameTooLarge("SSE line exceeds limit"),
    json.JSONDecodeError("bad json", "{", 0),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    RunExpired("SQLNoRows"),
    _WebCookieExpired("login/HTML response"),
])
def test_classify_protocol_violations(error):
    """漏网的协议类异常统一归 `UPSTREAM_PROTOCOL`，不再返回 None。"""
    assert _classify(error) is ErrKind.UPSTREAM_PROTOCOL


def test_protocol_kind_maps_to_controlled_error_type():
    """统计口径独立成「渠道响应异常」，不混进 `upstream_error`。"""
    assert _error_type_for(ErrKind.UPSTREAM_PROTOCOL) == "upstream_protocol"


def test_protocol_kind_is_zero_penalty():
    """不罚号：即使此前已累计错误数，本次也不冷却、不累计。"""
    assert ErrKind.UPSTREAM_PROTOCOL in NO_PENALTY_KINDS

    outcome = Scheduler().note_error(
        Candidate(credential_id="c", provider="trae", err_count=2),
        ErrKind.UPSTREAM_PROTOCOL, int(time.time()))

    assert outcome.cooling_until is None
    assert outcome.err_count == 2


# ------------------------------------- 已出帧后抛协议异常：留痕 + 不罚号


async def test_stream_protocol_violation_after_frames_records_usage(repo):
    """已出帧后抛 `UpstreamProtocolViolation`：错误帧收尾、记账、不冷却。"""
    credentials, db = repo
    ids = {"a": _add(credentials, "a"), "b": _add(credentials, "b")}
    executor, provider = _build(credentials, [[
        Event(kind=EventKind.CONTENT, content="half"),
        Event(kind=EventKind.USAGE, usage=Usage(7, 3, 2)),
        UpstreamProtocolViolation("non-json upstream body"),
    ]], stats=StatsCollector(db))

    frames = [frame async for frame in executor.stream(
        _stream_request(), username="alice")]
    joined = b"".join(frames)

    # 已出帧：半截输出不可回滚，不换号
    assert provider.calls == 1
    assert b"internal_error" not in joined
    assert b"no_healthy_credential" in joined

    # ① 有 usage_events 行 ② error_type 正确 ④ 已出帧的 token 被记
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["ok"] == 0
    assert row["error_type"] == "upstream_protocol"
    assert (row["input_tokens"], row["output_tokens"],
            row["reasoning_tokens"]) == (7, 3, 2)

    # ③ 凭证未写冷却、未累计错误数
    failed = _credential(credentials, ids[provider.tokens[0]])
    assert failed.cooling_until is None
    assert failed.err_count == 0
    db.close()


async def test_complete_rotates_on_protocol_violation(repo):
    """非流式：协议异常换号重试（不冷却失败凭证），成功后按成功记账。"""
    credentials, db = repo
    ids = {"a": _add(credentials, "a"), "b": _add(credentials, "b")}
    executor, provider = _build(
        credentials,
        [[UpstreamProtocolViolation("bad shape")], TRAE_GOOD],
        stats=StatsCollector(db))

    result = await executor.complete(_complete_request(), username="alice")

    assert provider.calls == 2
    assert result["choices"][0]["message"]["content"] == "hi"
    failed = _credential(credentials, ids[provider.tokens[0]])
    assert failed.cooling_until is None
    assert failed.err_count == 0
    # 请求最终成功 → 只有成功行
    rows = _rows(db)
    assert [(row["ok"], row["error_type"]) for row in rows] == [(1, None)]
    db.close()


# ------------------------------------- 未归类异常（kind is None）：先留痕再上抛


async def test_stream_unknown_error_after_frames_records_row(repo):
    """`_classify` 返回 None 时也先落一行 `usage_events` 再上抛。"""
    credentials, db = repo
    ids = {"a": _add(credentials, "a"), "b": _add(credentials, "b")}
    executor, provider = _build(credentials, [[
        Event(kind=EventKind.CONTENT, content="half"),
        Event(kind=EventKind.USAGE, usage=Usage(5, 2, 1)),
        _UnknownError("boom"),
    ]], stats=StatsCollector(db))

    frames: list[bytes] = []
    with pytest.raises(_UnknownError):
        async for frame in executor.stream(_stream_request(), username="alice"):
            frames.append(frame)

    assert provider.calls == 1
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["ok"] == 0
    assert row["error_type"] == "upstream_protocol"
    assert (row["input_tokens"], row["output_tokens"],
            row["reasoning_tokens"]) == (5, 2, 1)

    failed = _credential(credentials, ids[provider.tokens[0]])
    assert failed.cooling_until is None
    assert failed.err_count == 0
    db.close()


async def test_stream_unknown_error_after_dead_does_not_double_record(repo):
    """已由 DEAD 尝试行覆盖且未出帧 → 不再补第二行（requests 不虚高）。"""
    credentials, db = repo
    _add(credentials, "a")
    _add(credentials, "b")
    executor, provider = _build(
        credentials,
        [[_DeadError("session dead")], [_UnknownError("boom")]],
        stats=StatsCollector(db))

    with pytest.raises(_UnknownError):
        async for _ in executor.stream(_stream_request(), username="alice"):
            pass

    assert provider.calls == 2
    rows = _rows(db)
    assert [(row["ok"], row["error_type"]) for row in rows] == [
        (0, "credential_unavailable")]
    db.close()


async def test_complete_unknown_error_records_row_before_raise(repo):
    """非流式：未归类异常同样先留痕再上抛。"""
    credentials, db = repo
    _add(credentials, "a")
    _add(credentials, "b")
    executor, _provider = _build(
        credentials, [[_UnknownError("boom")]], stats=StatsCollector(db))

    with pytest.raises(_UnknownError):
        await executor.complete(_complete_request(), username="alice")

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
    assert rows[0]["error_type"] == "upstream_protocol"
    db.close()


async def test_complete_unknown_error_after_dead_does_not_double_record(repo):
    """非流式：已落 DEAD 尝试行 → 未归类异常不再补第二行。"""
    credentials, db = repo
    _add(credentials, "a")
    _add(credentials, "b")
    executor, provider = _build(
        credentials,
        [[_DeadError("session dead")], [_UnknownError("boom")]],
        stats=StatsCollector(db))

    with pytest.raises(_UnknownError):
        await executor.complete(_complete_request(), username="alice")

    assert provider.calls == 2
    rows = _rows(db)
    assert [(row["ok"], row["error_type"]) for row in rows] == [
        (0, "credential_unavailable")]
    db.close()
