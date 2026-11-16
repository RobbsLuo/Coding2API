"""执行引擎：成功路径、错误轮换、自动续写与双上游路由。"""

from __future__ import annotations

import time

import pytest

from src.compat.openai.request import parse_chat_request
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import ApiKeyRepository, CredentialRepository
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential, NoProviderForModel
from src.engine.scheduler import Scheduler
from src.engine.sse import SSE_DONE
from src.provider.base import ErrKind, Event, EventKind, Usage
from tests.helpers.chat import chat_request as _request
from tests.helpers.constants import SECRET
from tests.helpers.events import GOOD
from tests.helpers.events import TRAE_GOOD as GOOD_trae
from tests.helpers.executor import DualProvider
from tests.helpers.executor import dual_executor as _dual_executor
from tests.helpers.providers import ScriptedProvider as FakeProvider


class Boom(Exception):
    def __init__(self, kind: ErrKind, message: str = "upstream error") -> None:
        super().__init__(message)
        self._kind = kind

    def kind(self) -> ErrKind:
        return self._kind

@pytest.fixture()
def repo_trae(tmp_path):
    db = Database(tmp_path / "e.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), ApiKeyRepository(db), db
    db.close()

class UpstreamError(Exception):
    def __init__(self, kind: ErrKind) -> None:
        super().__init__(f"upstream {kind}")
        self._kind = kind

    def kind(self) -> ErrKind:
        return self._kind

def build_executor(repo_tuple, script, **kwargs):
    credentials, _keys, _db = repo_tuple
    provider = FakeProvider(script)
    executor = Executor(ExecutorDeps(providers={"trae": provider}, credentials=credentials,
                                     scheduler=Scheduler(**kwargs), default_model="glm-5.2"))
    return executor, provider, credentials

def add_credential(credentials, **kw):
    return credentials.add(provider="trae", credential_data={"accessToken": "a"}, **kw)

async def test_executor_limits_guard_filters_candidates(repo_trae):
    """限额护栏装配后：被停用的凭证不参与选号，只剩它时 503。"""
    credentials, _keys, _db = repo_trae
    blocked_id = add_credential(credentials, nickname="limited")
    ok_id = add_credential(credentials, nickname="healthy")

    class Guard:
        def __init__(self):
            self.calls = []

        def blocked(self, candidate, model):
            self.calls.append((candidate.credential_id, model))
            return candidate.credential_id == blocked_id

    guard = Guard()
    provider = FakeProvider([GOOD_trae])
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", limits=guard))
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"
    # 护栏对每个候选都被问到（含被停用的那条），且被停用者未被选中
    assert {cid for cid, _model in guard.calls} == {blocked_id, ok_id}

    # 只剩被限额的凭证 → 无可用凭证
    credentials.set_enabled(ok_id, False)

    class AllBlocked:
        def blocked(self, candidate, model):
            return True

    executor = Executor(ExecutorDeps(
        providers={"trae": FakeProvider([GOOD_trae])}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", limits=AllBlocked()))
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))


async def test_executor_complete_success(repo_trae, tmp_path):
    add_credential(repo_trae[0])
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"

async def test_executor_stream_success_frames(repo_trae):
    add_credential(repo_trae[0])
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert chunks[-1] == SSE_DONE
    assert b'"hi"' in chunks[0]                      # 首帧即带 role + content

async def test_executor_records_estimated_credit_flag(repo_trae):
    """执行引擎把 provider 的 credit_estimated 标记带进统计明细。"""
    from src.stats.collector import StatsCollector

    credentials, _keys, db = repo_trae
    add_credential(credentials)
    collector = StatsCollector(db)
    usage = Usage(1000, 200, 0, credit=0.05, credit_estimated=True)
    script = [[Event(kind=EventKind.CONTENT, content="hi"),
               Event(kind=EventKind.USAGE, usage=usage),
               Event(kind=EventKind.FINISH, finish_reason="stop")]]
    executor = Executor(ExecutorDeps(
        providers={"trae": FakeProvider(script)}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=collector))
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert chunks[-1] == SSE_DONE
    row = db.connect().execute(
        "SELECT credit, credit_estimated FROM usage_events").fetchone()
    assert row["credit"] == 0.05 and row["credit_estimated"] == 1

async def test_executor_complete_records_credit_and_flag(repo_trae):
    """非流式路径同样把 USAGE 事件的 credit / 推算标记写进统计（否则整条漏掉）。"""
    from src.stats.collector import StatsCollector

    credentials, _keys, db = repo_trae
    add_credential(credentials)
    collector = StatsCollector(db)
    usage = Usage(1000, 200, 0, credit=0.07, credit_estimated=True)
    script = [[Event(kind=EventKind.CONTENT, content="ok"),
               Event(kind=EventKind.USAGE, usage=usage),
               Event(kind=EventKind.FINISH, finish_reason="stop")]]
    executor = Executor(ExecutorDeps(
        providers={"trae": FakeProvider(script)}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=collector))
    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    row = db.connect().execute(
        "SELECT credit, credit_estimated FROM usage_events").fetchone()
    assert row["credit"] == 0.07 and row["credit_estimated"] == 1

async def test_executor_rotates_on_http_error(repo_trae):
    add_credential(repo_trae[0], nickname="first")
    add_credential(repo_trae[0], nickname="second")
    script = [[UpstreamError(ErrKind.SOFT)], GOOD_trae]
    executor, provider, _credentials = build_executor(repo_trae, script)
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"
    assert provider.calls == 2

async def test_executor_cools_plan_error_then_fails_when_exhausted(repo_trae):
    add_credential(repo_trae[0])
    executor, _provider, credentials = build_executor(repo_trae, [[UpstreamError(ErrKind.PLAN)]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert credentials.candidates()[0].cooling_until is not None

async def test_executor_stream_reports_no_credential(repo_trae):
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"no_healthy_credential" in chunks[0]

async def test_executor_stream_inline_error_triggers_rotation(repo_trae):
    add_credential(repo_trae[0])
    add_credential(repo_trae[0])
    script = [[Event(kind=EventKind.ERROR, error_code=1005, error_message="quota")], GOOD_trae]
    executor, provider, _credentials = build_executor(repo_trae, script)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert provider.calls == 2 and chunks[-1] == SSE_DONE

async def test_executor_unknown_error_propagates(repo_trae):
    add_credential(repo_trae[0])
    executor, _provider, _credentials = build_executor(repo_trae, [[RuntimeError("boom")]])
    with pytest.raises(RuntimeError):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))

async def test_executor_dead_error_disables_credential(repo_trae):
    add_credential(repo_trae[0])
    executor, _provider, credentials = build_executor(repo_trae, [[UpstreamError(ErrKind.DEAD)]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert credentials.candidates()[0].disabled is True

async def test_executor_reports_no_credential_as_service_unavailable(repo_trae):
    """provider 已注册但无凭证 → NoHealthyCredential（503），不是 400。"""
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))

async def test_no_healthy_credential_carries_earliest_recovery(repo_trae):
    """全池冷却 → 503 带 `Retry-After`：取**所有候选里最早**的恢复点。

    客户端（OpenAI SDK / Claude Code）拿不到这个头会立刻重试，把一次限流
    放大成请求风暴；取「最后一次尝试的提示」则会多等——另一个号可能已经
    快到期了。
    """
    credentials, _keys, _db = repo_trae
    add_credential(credentials)
    add_credential(credentials)
    executor, _provider, _credentials = build_executor(
        repo_trae, [[UpstreamError(ErrKind.PLAN)]])
    with pytest.raises(NoHealthyCredential) as info:
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    retry_after = info.value.retry_after
    assert retry_after is not None
    # PLAN 冷却 12h；取最早恢复点，允许几秒的执行漂移
    assert 12 * 3600 - 5 <= retry_after <= 12 * 3600

async def test_no_healthy_credential_without_recovery_has_none(repo_trae):
    """行还在但不启用（人工停用）→ 没有自动恢复点，不给 `Retry-After`。"""
    credentials, _keys, _db = repo_trae
    credential_id = add_credential(credentials)
    credentials.set_enabled(credential_id, False)
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    with pytest.raises(NoHealthyCredential) as info:
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert info.value.retry_after is None

async def test_recovery_seconds_skips_limit_blocked_and_model_scope(repo_trae):
    """`Retry-After` 跳过限额停用的号；模型级与账号级冷却取较晚者。"""
    from src.engine.model_resolver import resolve
    from src.engine.scheduler import ErrorOutcome, ModelCooldown

    credentials, _keys, _db = repo_trae
    blocked = add_credential(credentials)
    cooling = add_credential(credentials)
    model_cooled = add_credential(credentials)
    busy = add_credential(credentials)

    class Guard:
        def blocked(self, candidate, model):
            return candidate.credential_id == blocked

    class Inflight:
        def blocked(self, credential_id):
            return credential_id == busy

    now = int(time.time())
    credentials.save_error(blocked, ErrorOutcome(
        cooling_until=now + 9999, cooling_kind=ErrKind.SOFT.value))
    credentials.save_error(cooling, ErrorOutcome(
        cooling_until=now + 60, cooling_kind=ErrKind.SOFT.value))
    credentials.save_error(model_cooled, ErrorOutcome(
        model_cooldowns={"glm-5.2": ModelCooldown(
            cooling_until=now + 180, hits=1, reason="model")}))

    executor = Executor(ExecutorDeps(
        providers={"trae": FakeProvider([GOOD_trae])}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", limits=Guard(),
        inflight=Inflight(), short_cooldown_wait_seconds=0))
    target = resolve("glm-5.2@trae", "glm-5.2")
    assert executor._recovery_seconds(target) == pytest.approx(60, abs=5)


async def test_recovery_seconds_none_when_no_provider_registered(repo_trae):
    """一个上游都没注册时没有恢复点可言（executor 侧 400 语义，不是 503）。"""
    from src.engine.model_resolver import resolve

    credentials, _keys, _db = repo_trae
    executor = Executor(ExecutorDeps(
        providers={}, credentials=credentials, scheduler=Scheduler(),
        default_model="glm-5.2"))
    assert executor._recovery_seconds(resolve("glm-5.2@trae", "glm-5.2")) is None

async def test_privacy_rules_mask_outbound_copy_only(repo_trae):
    """出站脱敏只改发往上游的副本：`request.raw` 保持原样（粘性/续写读它）。"""
    credentials, _keys, _db = repo_trae
    add_credential(credentials)
    seen: list[dict] = []

    class Provider:
        id = "trae"

        async def stream_chat(self, _credential_data, payload, _model):
            seen.append(payload)
            for event in GOOD_trae:
                yield event

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2",
        privacy_rules=lambda: "email,cn_mobile"))
    request = parse_chat_request({"messages": [
        {"role": "user", "content": "mail a@b.com or 13800138000"}]})
    await executor.complete(request)
    assert seen[0]["messages"][0]["content"] == "mail [email] or [phone]"
    assert request.raw["messages"][0]["content"] == "mail a@b.com or 13800138000"

    # 规则开着但没有命中：同样不动（且不该产生副本副作用）
    seen.clear()
    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hello"}]}))
    assert seen[0]["messages"][0]["content"] == "hello"


async def test_privacy_rules_apply_on_stream_path(repo_trae):
    """流式路径同一落点：脱敏在压缩/档位映射之后、发上游之前。"""
    credentials, _keys, _db = repo_trae
    add_credential(credentials)
    seen: list[dict] = []

    class Provider:
        id = "trae"

        async def stream_chat(self, _credential_data, payload, _model):
            seen.append(payload)
            for event in GOOD_trae:
                yield event

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2",
        privacy_rules=lambda: "private_ip"))
    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "host 10.1.2.3"}],
             "stream": True})):
        pass
    assert seen[0]["messages"][0]["content"] == "host [private-ip]"


async def test_privacy_rules_empty_keeps_payload_untouched(repo_trae):
    """热更值是空串（默认）时零拷贝：payload 对象与 `request.raw` 同引用。"""
    credentials, _keys, _db = repo_trae
    add_credential(credentials)
    seen: list[dict] = []

    class Provider:
        id = "trae"

        async def stream_chat(self, _credential_data, payload, _model):
            seen.append(payload)
            for event in GOOD_trae:
                yield event

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2",
        privacy_rules=lambda: ""))
    request = parse_chat_request({"messages": [{"role": "user", "content": "a@b.com"}]})
    await executor.complete(request)
    assert seen[0]["messages"] == request.raw["messages"]
    assert seen[0]["messages"][0]["content"] == "a@b.com"


async def test_executor_rejects_model_without_provider(repo_trae):
    add_credential(repo_trae[0])
    executor, _provider, _credentials = build_executor(repo_trae, [GOOD_trae])
    with pytest.raises(NoProviderForModel):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "model": "x@codebuddy"}))

@pytest.mark.asyncio
async def test_executor_auto_continues_on_length_same_credential(dual_repo):
    """finish_reason=length → 同凭证续写；executor 只看到一条更长的事件流。"""
    credentials, db = dual_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    truncated = [Event(kind=EventKind.CONTENT, content="part1"),
                 Event(kind=EventKind.USAGE, usage=Usage(input_tokens=7,
                                                         output_tokens=3)),
                 Event(kind=EventKind.FINISH, finish_reason="length")]
    completed = [Event(kind=EventKind.CONTENT, content="part2"),
                 Event(kind=EventKind.USAGE, usage=Usage(input_tokens=2,
                                                         output_tokens=4)),
                 Event(kind=EventKind.FINISH, finish_reason="stop")]
    provider = DualProvider("codebuddy", [truncated, completed])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", max_auto_continues=10))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "part1part2"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert provider.calls == 2                       # 同凭证两次请求
    # 累计用量：input 7+2、output 3+4
    assert result["usage"]["prompt_tokens"] == 9
    assert result["usage"]["completion_tokens"] == 7
    # 只用一个凭证，未发生轮换
    assert len(credentials.candidates(["codebuddy"])) == 1
    db.close()

@pytest.mark.asyncio
async def test_executor_auto_continue_disabled(dual_repo):
    """max_auto_continues=0（默认）：length 原样透传，不续写。"""
    credentials, db = dual_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    truncated = [Event(kind=EventKind.CONTENT, content="part1"),
                 Event(kind=EventKind.FINISH, finish_reason="length")]
    provider = DualProvider("codebuddy", [truncated])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2"))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "part1"
    assert result["choices"][0]["finish_reason"] == "length"
    assert provider.calls == 1
    db.close()

@pytest.mark.asyncio
async def test_executor_stream_auto_continue_emits_single_finish(dual_repo):
    """流式续写：客户端只看到一条流、一个 finish_reason 与一条 [DONE]。"""
    credentials, db = dual_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    truncated = [Event(kind=EventKind.CONTENT, content="a"),
                 Event(kind=EventKind.FINISH, finish_reason="length")]
    completed = [Event(kind=EventKind.CONTENT, content="b"),
                 Event(kind=EventKind.FINISH, finish_reason="stop")]
    provider = DualProvider("codebuddy", [truncated, completed])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", max_auto_continues=10))
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert joined.count("data: [DONE]") == 1
    assert '"finish_reason": "stop"' in joined
    assert '"finish_reason": "length"' not in joined
    assert provider.calls == 2
    db.close()

async def test_dual_provider_same_model_routes_and_fails_over(dual_repo):
    """同一扁平模型名下，一个上游失败 → 自动落到另一个（的核心价值）。

    用 pin 固定首个上游，避免依赖同分时的字母序挑选。
    """
    credentials, db = dual_repo
    trae_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    credentials.set_pinned(trae_id)          # 固定先走 trae
    executor, trae, codebuddy = _dual_executor(
        credentials, [[Boom(ErrKind.SOFT)]], [GOOD])
    result = await executor.complete(_request())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and codebuddy.calls == 1
    assert credentials.candidates(["trae"])[0].cooling_until is not None
    assert credentials.candidates(["codebuddy"])[0].cooling_until is None
    db.close()

async def test_dual_provider_forced_suffix_targets_single_upstream(dual_repo):
    """model@codebuddy 强制只走 codebuddy，不落到 trae。"""
    credentials, db = dual_repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    executor, trae, codebuddy = _dual_executor(credentials, [GOOD], [GOOD])
    await executor.complete(_request("glm-5.2@codebuddy"))
    assert codebuddy.calls == 1 and trae.calls == 0
    db.close()

async def test_dual_provider_error_classification_is_consistent(dual_repo):
    """两个 provider 的错误分类都落到同一冷却语义（验证点）。"""
    credentials, db = dual_repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    executor, _trae, _codebuddy = _dual_executor(
        credentials, [[Boom(ErrKind.PLAN)]], [[Boom(ErrKind.PLAN)]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    for candidate in credentials.candidates():
        assert candidate.cooling_until is not None, candidate
    db.close()

async def test_dual_provider_no_credential_when_both_registered(dual_repo):
    credentials, db = dual_repo
    executor, _trae, _codebuddy = _dual_executor(credentials, [GOOD], [GOOD])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    db.close()
