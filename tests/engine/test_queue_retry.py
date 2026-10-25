"""F3：排队语义（上游回 `isQueued` / `RESOURCE_LIMIT` + `retryAfterSeconds`）。

口径：

- **不冷却**：排队不是故障，`scheduler.note_error` 对 `ErrKind.QUEUE` 零动作；
- **不换号**：等 `retryAfterSeconds` 后在**原号**上重开；
- **不占轮换预算**：排队轮次不计入 `attempts`，否则三次排队就把请求打成 503；
- **有界**：轮次上限 `QUEUE_MAX_ROUNDS`、单轮封顶 `QUEUE_MAX_WAIT_SECONDS`；
- **已出帧不重开**：半截输出不可回滚，降级为 SOFT 收尾。

全部用假时钟（`asyncio.sleep` 换掉），文件毫秒级跑完。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.compat.openai.request import parse_chat_request
from src.engine.executor import (
    QUEUE_MAX_ROUNDS,
    QUEUE_MAX_WAIT_SECONDS,
    Executor,
    ExecutorDeps,
    NoHealthyCredential,
)
from src.engine.model_resolver import resolve
from src.engine.scheduler import ErrorOutcome, Scheduler
from src.provider.base import (
    QUEUE_DEFAULT_WAIT_SECONDS,
    ErrKind,
    Event,
    EventKind,
    parse_queue_hint,
)
from tests.helpers.events import GOOD

_TARGET = resolve("glm-5.2@trae", "glm-5.2")

_REAL_SLEEP = asyncio.sleep


@pytest.fixture()
def sleeps(monkeypatch):
    """把 `asyncio.sleep` 换成记账版本（不真等）。"""
    recorded: list[float] = []

    async def fake(delay, result=None):
        recorded.append(delay)
        await _REAL_SLEEP(0)
        return result

    monkeypatch.setattr(asyncio, "sleep", fake)
    return recorded


def _request(stream: bool = True):
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                               "stream": stream})


def _executor(credentials, provider, **deps) -> Executor:
    return Executor(ExecutorDeps(providers={"trae": provider}, credentials=credentials,
                                 scheduler=deps.pop("scheduler", Scheduler()),
                                 default_model="glm-5.2", **deps))


class _QueuedProvider:
    """前 N 次上游回排队事件，之后正常吐 GOOD。"""

    id = "trae"

    def __init__(self, queue_times: int, retry_after: float | None = 0) -> None:
        self.queue_times = queue_times
        self.retry_after = retry_after
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        self.calls += 1
        if self.calls <= self.queue_times:
            yield Event(kind=EventKind.ERROR, error_code=403,
                        error_message='{"isQueued":true}',
                        error_kind=ErrKind.QUEUE,
                        retry_after_seconds=self.retry_after)
            return
        for event in GOOD:
            yield event


# --------------------------------------------------------------- 协议解析


def test_parse_queue_hint_is_queued_flag():
    assert parse_queue_hint(json.dumps({"isQueued": True,
                                        "retryAfterSeconds": 7})) == 7.0


def test_parse_queue_hint_sub_code():
    assert parse_queue_hint(json.dumps({"subCode": "RESOURCE_LIMIT",
                                        "retryAfterSeconds": 3})) == 3.0


def test_parse_queue_hint_nested_envelope():
    body = json.dumps({"data": {"error": {"isQueued": True,
                                          "retryAfterSeconds": 11}}})
    assert parse_queue_hint(body) == 11.0


def test_parse_queue_hint_list_node():
    body = json.dumps([{"ok": 1}, {"isQueued": True, "retryAfterSeconds": 2}])
    assert parse_queue_hint(body) == 2.0


def test_parse_queue_hint_list_without_queue():
    assert parse_queue_hint(json.dumps([1, "x", {"ok": 1}])) is None


def test_parse_queue_hint_list_without_flag():
    """列表节点整体没有排队标记：遍历完返回 None（而不是把列表本身当队列）。"""
    assert parse_queue_hint(json.dumps([{"ok": 1}, {"code": 0}])) is None


def test_parse_queue_hint_depth_limit():
    """超过 3 层就不再下钻：再深是业务正文，继续递归只白烧 CPU。"""
    node: dict = {"isQueued": True}
    for _ in range(6):
        node = {"wrap": node}
    assert parse_queue_hint(json.dumps(node)) is None


def test_parse_queue_hint_defaults_when_no_seconds():
    assert parse_queue_hint('{"isQueued": true}') == QUEUE_DEFAULT_WAIT_SECONDS


def test_parse_queue_hint_ignores_bool_wait():
    """`retryAfterSeconds: true` 不是秒数，回落到兜底值。"""
    assert parse_queue_hint('{"isQueued": true, "retryAfterSeconds": true}') == (
        QUEUE_DEFAULT_WAIT_SECONDS)


@pytest.mark.parametrize("body", [
    b"",
    b"   ",
    b"not json",
    b'"just a string"',
    b'{"isQueued": false}',
    b'{"code": 500}',
])
def test_parse_queue_hint_not_queued(body):
    assert parse_queue_hint(body) is None


def test_parse_queue_hint_accepts_bytes():
    assert parse_queue_hint(b'{"isQueued": true, "retryAfterSeconds": 4}') == 4.0


# ------------------------------------------------------- 调度器零动作


def test_scheduler_queue_writes_nothing(repo):
    """排队绝不惩罚凭证：err_count 不变、不写冷却。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    scheduler = Scheduler()
    candidate = next(c for c in credentials.candidates(["trae"])
                     if c.credential_id == cid)
    outcome = scheduler.note_error(candidate, ErrKind.QUEUE, 1_700_000_000)
    assert outcome == ErrorOutcome(err_count=candidate.err_count)
    assert outcome.cooling_until is None
    db.close()


# ------------------------------------------------------------ 流式排队重开


async def test_stream_requeues_on_same_credential(repo, sleeps):
    """排队 1 次后成功：等 retryAfter 秒、用同一个号、只打 2 次上游。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    provider = _QueuedProvider(queue_times=1, retry_after=2)
    executor = _executor(credentials, provider)
    frames = [f async for f in executor.stream(_request())]
    assert b"ok" in b"".join(frames)
    assert provider.calls == 2
    assert sleeps == [2.0]
    # 不写冷却：凭证仍然可立即选用
    candidate = next(c for c in credentials.candidates(["trae"])
                     if c.credential_id == cid)
    assert candidate.cooling_until is None
    db.close()


async def test_stream_queue_rounds_are_bounded(repo, sleeps):
    """上游一直排队：最多重开 `QUEUE_MAX_ROUNDS` 轮，然后 503。"""
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    provider = _QueuedProvider(queue_times=99, retry_after=0)
    executor = _executor(credentials, provider)
    frames = [f async for f in executor.stream(_request())]
    assert b"no_healthy_credential" in b"".join(frames)
    # 首次 + 3 轮重开 = 4 次上游调用
    assert provider.calls == QUEUE_MAX_ROUNDS + 1
    assert len(sleeps) == QUEUE_MAX_ROUNDS
    db.close()


async def test_stream_queue_wait_is_capped(repo, sleeps):
    """上游点名 2 小时：单轮等待封顶到 `QUEUE_MAX_WAIT_SECONDS`。"""
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    provider = _QueuedProvider(queue_times=1, retry_after=7200)
    executor = _executor(credentials, provider)
    frames = [f async for f in executor.stream(_request())]
    assert b"ok" in b"".join(frames)
    assert sleeps == [QUEUE_MAX_WAIT_SECONDS]
    db.close()


async def test_stream_queue_without_seconds_uses_default(repo, sleeps):
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    provider = _QueuedProvider(queue_times=1, retry_after=None)
    executor = _executor(credentials, provider)
    frames = [f async for f in executor.stream(_request())]
    assert b"ok" in b"".join(frames)
    assert sleeps == [QUEUE_DEFAULT_WAIT_SECONDS]
    db.close()


async def test_stream_queue_after_emit_degrades_to_soft(repo, sleeps):
    """已出帧后再排队：不能重开（半截输出），按 SOFT 收尾。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})

    class Provider:
        id = "trae"

        async def stream_chat(self, _credential_data, _payload, _model):
            yield Event(kind=EventKind.CONTENT, content="half")
            yield Event(kind=EventKind.ERROR, error_code=403,
                        error_kind=ErrKind.QUEUE, retry_after_seconds=1)

    executor = _executor(credentials, Provider())
    frames = [f async for f in executor.stream(_request())]
    body = b"".join(frames)
    assert b"half" in body
    assert b"no_healthy_credential" in body
    assert sleeps == []
    candidate = next(c for c in credentials.candidates(["trae"])
                     if c.credential_id == cid)
    assert candidate.cooling_until is not None      # SOFT 冷却已写
    db.close()


async def test_stream_queue_pinned_credential_deleted(repo, sleeps):
    """排队等待期间凭证被并发删除：退回正常选号，不崩。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})

    class Provider:
        id = "trae"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _credential_data, _payload, _model):
            self.calls += 1
            if self.calls == 1:
                yield Event(kind=EventKind.ERROR, error_code=403,
                            error_kind=ErrKind.QUEUE, retry_after_seconds=0)
                return
            for event in GOOD:
                yield event

    provider = Provider()
    executor = _executor(credentials, provider)

    async def _drop_then_run():
        frames = []
        async for frame in executor.stream(_request()):
            frames.append(frame)
        return frames

    # 在第一次 sleep 里删掉凭证（假 sleep 会走到这里）
    original = asyncio.sleep
    fired = False

    async def deleting_sleep(delay, result=None):
        nonlocal fired
        if not fired:
            fired = True
            credentials.delete(cid)
        await original(0)

    import src.engine.executor as executor_module

    real = executor_module.asyncio.sleep
    executor_module.asyncio.sleep = deleting_sleep
    try:
        frames = await _drop_then_run()
    finally:
        executor_module.asyncio.sleep = real
    assert b"no_healthy_credential" in b"".join(frames)
    db.close()


async def test_stream_http_queue_error_requeues(repo, sleeps):
    """HTTP 级排队（403 + 排队 body 抛出的 UpstreamHTTPError）同样原号重开。"""
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})

    from src.provider.base import UpstreamHTTPError

    class QueuedHTTPError(UpstreamHTTPError):
        classify_status = staticmethod(lambda status, body: ErrKind.QUEUE)

    class Provider:
        id = "trae"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _credential_data, _payload, _model):
            self.calls += 1
            if self.calls == 1:
                raise QueuedHTTPError(403, b'{"isQueued": true}',
                                      retry_after_seconds=4.0)
            for event in GOOD:
                yield event

    provider = Provider()
    executor = _executor(credentials, provider)
    frames = [f async for f in executor.stream(_request())]
    assert b"ok" in b"".join(frames)
    assert provider.calls == 2
    assert sleeps == [4.0]
    db.close()


async def test_stream_http_queue_exhausted_degrades_to_soft(repo, sleeps):
    """HTTP 级排队但轮次已用尽：按 SOFT 冷却换号，不无限等。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})

    from src.provider.base import UpstreamHTTPError

    class QueuedHTTPError(UpstreamHTTPError):
        classify_status = staticmethod(lambda status, body: ErrKind.QUEUE)

    class Provider:
        id = "trae"

        async def stream_chat(self, _credential_data, _payload, _model):
            raise QueuedHTTPError(403, b'{"isQueued": true}')
            yield  # pragma: no cover - 让它成为 async generator

    executor = _executor(credentials, Provider())
    frames = [f async for f in executor.stream(_request())]
    assert b"no_healthy_credential" in b"".join(frames)
    assert len(sleeps) == QUEUE_MAX_ROUNDS
    candidate = next(c for c in credentials.candidates(["trae"])
                     if c.credential_id == cid)
    assert candidate.cooling_until is not None
    db.close()


# --------------------------------------------------------- 非流式排队重开


async def test_complete_requeues_on_same_credential(repo, sleeps):
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    provider = _QueuedProvider(queue_times=1, retry_after=1)
    executor = _executor(credentials, provider)
    result = await executor.complete(_request(stream=False))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert provider.calls == 2
    assert sleeps == [1.0]
    db.close()


async def test_complete_queue_rounds_bounded(repo, sleeps):
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    provider = _QueuedProvider(queue_times=99, retry_after=0)
    executor = _executor(credentials, provider)
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request(stream=False))
    assert provider.calls == QUEUE_MAX_ROUNDS + 1
    db.close()


async def test_complete_queue_pinned_credential_deleted(repo, sleeps):
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})

    class Provider:
        id = "trae"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _credential_data, _payload, _model):
            self.calls += 1
            if self.calls == 1:
                yield Event(kind=EventKind.ERROR, error_code=403,
                            error_kind=ErrKind.QUEUE, retry_after_seconds=0)
                return
            for event in GOOD:
                yield event

    provider = Provider()
    executor = _executor(credentials, provider)

    import src.engine.executor as executor_module

    original = executor_module.asyncio.sleep
    fired = False

    async def deleting_sleep(delay, result=None):
        nonlocal fired
        if not fired:
            fired = True
            credentials.delete(cid)
        await _REAL_SLEEP(0)

    executor_module.asyncio.sleep = deleting_sleep
    try:
        with pytest.raises(NoHealthyCredential):
            await executor.complete(_request(stream=False))
    finally:
        executor_module.asyncio.sleep = original
    db.close()


async def test_complete_http_queue_requeues(repo, sleeps):
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})

    from src.provider.base import UpstreamHTTPError

    class QueuedHTTPError(UpstreamHTTPError):
        classify_status = staticmethod(lambda status, body: ErrKind.QUEUE)

    class Provider:
        id = "trae"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _credential_data, _payload, _model):
            self.calls += 1
            if self.calls == 1:
                raise QueuedHTTPError(403, b'{"isQueued": true}',
                                      retry_after_seconds=2.0)
            for event in GOOD:
                yield event

    provider = Provider()
    executor = _executor(credentials, provider)
    result = await executor.complete(_request(stream=False))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert sleeps == [2.0]
    db.close()


async def test_complete_http_queue_exhausted_degrades_to_soft(repo, sleeps):
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})

    from src.provider.base import UpstreamHTTPError

    class QueuedHTTPError(UpstreamHTTPError):
        classify_status = staticmethod(lambda status, body: ErrKind.QUEUE)

    class Provider:
        id = "trae"

        async def stream_chat(self, _credential_data, _payload, _model):
            raise QueuedHTTPError(403, b'{"isQueued": true}')
            yield  # pragma: no cover - 让它成为 async generator

    executor = _executor(credentials, Provider())
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request(stream=False))
    assert len(sleeps) == QUEUE_MAX_ROUNDS
    candidate = next(c for c in credentials.candidates(["trae"])
                     if c.credential_id == cid)
    assert candidate.cooling_until is not None
    db.close()


# ------------------------------------------------------------- _queue_wait


def test_queue_wait_disabled_when_emitted(repo):
    credentials, db = repo
    executor = _executor(credentials, object())
    assert executor._queue_wait(1.0, True, 0) is None
    db.close()


def test_queue_wait_disabled_when_rounds_exhausted(repo):
    credentials, db = repo
    executor = _executor(credentials, object())
    assert executor._queue_wait(1.0, False, QUEUE_MAX_ROUNDS) is None
    db.close()


def test_queue_wait_clamps_negative(repo):
    credentials, db = repo
    executor = _executor(credentials, object())
    assert executor._queue_wait(-5.0, False, 0) == 0.0
    db.close()


def test_queue_wait_uses_value(repo):
    credentials, db = repo
    executor = _executor(credentials, object())
    assert executor._queue_wait(12.5, False, 0) == 12.5
    db.close()


def test_plan_queue_restart_returns_budget_and_pin(repo):
    """四个入口共用同一份预算口径：轮次 +1、attempts -1、pin 原号。"""
    credentials, db = repo
    executor = _executor(credentials, object())
    data = {"access_token": "t"}

    plan = executor._plan_queue_restart(
        7.0, emitted=False, rounds=0, attempts=3,
        provider_id="trae", credential_id="cid-1", credential_data=data)

    assert plan is not None
    assert plan.wait == 7.0
    assert plan.rounds == 1
    # 排队不是换号尝试：不归还预算的话三次排队就把 max_rotate 吃满
    assert plan.attempts == 2
    # pin 原号：下一轮跳过 _pick，排队语义才不会丢
    assert plan.pinned == ("cid-1", data)
    db.close()


def test_plan_queue_restart_none_when_emitted_or_exhausted(repo):
    """不该重开的两种情形都返回 None，由调用方降级为 SOFT。"""
    credentials, db = repo
    executor = _executor(credentials, object())
    kwargs = dict(attempts=3, provider_id="trae", credential_id="cid-1",
                  credential_data={})

    assert executor._plan_queue_restart(1.0, emitted=True, rounds=0, **kwargs) is None
    assert executor._plan_queue_restart(
        1.0, emitted=False, rounds=QUEUE_MAX_ROUNDS, **kwargs) is None
    db.close()


def test_error_type_for_queue_is_rate_limit():
    from src.engine.executor import _error_type_for

    assert _error_type_for(ErrKind.QUEUE) == "rate_limit"
