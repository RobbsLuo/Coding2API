"""请求作用域候选快照（P2-17）：一次请求只读一次候选表。

三件事必须同时成立，快照才不改变语义：

* **读放大被收掉**：选号 → 短冷却等待 → 成因拆解 / `Retry-After` 不再各读
  一遍 credentials + credential_model_cooldowns。
* **本请求自己写的惩罚必须立刻生效**：库里刚写下的冷却 / 禁用由排除集合
  镜像，同请求的下一次重试不会再选中它。
* **跨请求不共享**：别的请求写的新冷却要能被下一个请求看到（快照按请求取，
  不是进程级缓存）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.compat.openai.request import parse_chat_request
from src.engine.executor import (
    Executor,
    ExecutorDeps,
    NoHealthyCredential,
)
from src.engine.scheduler import Scheduler
from src.provider.base import ErrKind, Event, EventKind, Usage
from src.request_context import get_credential_id
from tests.helpers.chat import chat_request


class _Counter:
    """`CredentialRepository` 的计数替身：其余调用原样转给真仓储。

    只覆盖选号相关口径（`candidates` / `credential_data` / `provider_of` /
    `save_error` / `save_success`），其余方法由 `__getattr__` 透传——本文件
    只关心「一次请求读几次候选表」，不替身整个仓储接口。
    """

    def __init__(self, repo) -> None:
        self._repo = repo
        self.candidate_reads: list[tuple[bool, ...]] = []

    def candidates(self, providers=None, *, selectable_only=False, **kwargs):
        self.candidate_reads.append((selectable_only, tuple(providers or ())))
        return self._repo.candidates(providers, selectable_only=selectable_only, **kwargs)

    def __getattr__(self, name):
        return getattr(self._repo, name)


class _KindError(Exception):
    def __init__(self, kind: ErrKind, message: str = "boom") -> None:
        super().__init__(message)
        self._kind = kind

    def kind(self) -> ErrKind:
        return self._kind


def _good(text: str = "ok") -> list[Event]:
    return [Event(kind=EventKind.CONTENT, content=text),
            Event(kind=EventKind.USAGE, usage=Usage(1, 2, 0)),
            Event(kind=EventKind.FINISH, finish_reason="stop")]


class _Provider:
    """按调用次数回放的单渠道替身；`script` 里可以放异常。"""

    id = "trae"

    def __init__(self, script) -> None:
        self.script = script
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item


def _executor(credentials, script, **deps) -> Executor:
    return Executor(ExecutorDeps(
        providers={"trae": _Provider(script)}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", **deps))


def _add(credentials, token: str) -> str:
    return credentials.add(provider="trae", credential_data={"accessToken": token})


def test_one_request_reads_candidates_once_across_rotations(repo):
    """两次轮换 + 耗尽归因：整条请求只读一次候选表（此前是 4–5 次）。"""
    credentials, _db = repo
    _add(credentials, "a")
    _add(credentials, "b")
    _add(credentials, "c")
    counting = _Counter(credentials)
    # REQUEST 类零冷却（只换号），池子不被本用例打空
    executor = _executor(counting, [
        [_KindError(ErrKind.REQUEST)],
        [_KindError(ErrKind.REQUEST)],
        _good(),
    ])

    result = asyncio.run(executor.complete(chat_request()))

    assert result["choices"][0]["message"]["content"] == "ok"
    assert len(counting.candidate_reads) == 1


def test_stream_request_reads_candidates_once(repo):
    """流式路径同样只读一次（含选号 / 轮换 / 收尾归因）。"""
    credentials, _db = repo
    _add(credentials, "a")
    _add(credentials, "b")
    counting = _Counter(credentials)
    executor = _executor(counting, [
        [_KindError(ErrKind.REQUEST)],
        _good(),
    ])

    async def run() -> list[bytes]:
        return [frame async for frame in executor.stream(
            parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                                "stream": True}))]

    asyncio.run(run())
    assert len(counting.candidate_reads) == 1


def test_local_penalty_excludes_credential_for_the_rest_of_the_request(repo):
    """本请求写下的冷却立刻进排除集合：快照行还是旧值，重试不得再选它。"""
    credentials, db = repo
    _add(credentials, "a")
    _add(credentials, "b")
    _add(credentials, "c")
    used: list[str | None] = []

    class RecordingProvider(_Provider):
        async def stream_chat(self, credential_data, payload, model):
            used.append(get_credential_id())
            async for event in super().stream_chat(credential_data, payload, model):
                yield event

    executor = Executor(ExecutorDeps(
        providers={"trae": RecordingProvider([
            [_KindError(ErrKind.SOFT, "first")], _good("second")])},
        credentials=credentials, scheduler=Scheduler(), default_model="glm-5.2"))

    result = asyncio.run(executor.complete(chat_request()))

    assert result["choices"][0]["message"]["content"] == "second"
    # 两次尝试落在**不同**凭证上：本请求刚冷却的号没有被再选一次
    assert len(used) == 2 and used[0] != used[1]
    # 库里也确实写下了冷却（排除集合是镜像，不是替代）
    row = db.connect().execute(
        "SELECT cooling_until FROM credentials WHERE id = ?", (used[0],)).fetchone()
    assert row["cooling_until"] is not None


def test_local_penalty_is_visible_to_pool_breakdown(repo):
    """同一请求内「全池都被本请求打冷却」时，成因拆解仍看得见它们（cooling）。"""
    credentials, _db = repo
    _add(credentials, "a")
    executor = _executor(credentials, [[_KindError(ErrKind.SOFT)]])

    with pytest.raises(NoHealthyCredential) as caught:
        asyncio.run(executor.complete(chat_request()))

    assert "total=1" in str(caught.value)
    assert "cooling=1" in str(caught.value)


def test_local_model_penalty_only_covers_that_model(repo):
    """模型级惩罚按上游原始模型名登记：别的模型不受牵连（回退链不能少候选）。"""
    credentials, _db = repo
    credential_id = _add(credentials, "a")
    executor = _executor(credentials, [_good()])
    scope = executor._new_scope()          # noqa: SLF001 - 直接验证数据结构

    from src.engine.scheduler import ErrorOutcome, ModelCooldown

    scope.exclude(credential_id, ErrorOutcome(model_cooldowns={
        "glm-5.2": ModelCooldown(cooling_until=int(time.time()) + 600, hits=1,
                                 reason="model_rate_limited")}))

    now = int(time.time())
    assert scope.penalized(credential_id, "glm-5.2", now)
    assert not scope.penalized(credential_id, "kimi-k3", now)


def test_local_penalty_account_cooldown_clears_model_entries(repo):
    """账号级冷却在库里会清空模型级条目：本地镜像同口径（不留豁免泄漏）。"""
    credentials, _db = repo
    credential_id = _add(credentials, "a")
    executor = _executor(credentials, [_good()])
    scope = executor._new_scope()          # noqa: SLF001

    from src.engine.scheduler import ErrorOutcome, ModelCooldown

    until = int(time.time()) + 600
    scope.exclude(credential_id, ErrorOutcome(model_cooldowns={
        "glm-5.2": ModelCooldown(cooling_until=until, hits=1, reason="soft")}))
    scope.exclude(credential_id, ErrorOutcome(cooling_until=until, cooling_kind="soft"))

    assert scope.penalty(credential_id).model_cooling == {}
    assert scope.penalized(credential_id, "glm-5.2", int(time.time()))


def test_zero_penalty_outcome_is_not_registered(repo):
    """零动作分类（REQUEST/CONTEXT/QUEUE 等）不登记：否则成因拆解会误报「冷却中」。"""
    credentials, _db = repo
    credential_id = _add(credentials, "a")
    executor = _executor(credentials, [_good()])
    scope = executor._new_scope()          # noqa: SLF001

    from src.engine.scheduler import ErrorOutcome

    scope.exclude(credential_id, ErrorOutcome(err_count=1))

    assert scope.penalty(credential_id) is None
    assert not scope.penalized(credential_id, "glm-5.2", int(time.time()))


def test_provider_pause_excludes_whole_channel_locally(repo):
    """渠道级暂停（`cool_provider`）也镜像进排除集合：同请求不再从该渠道挑号。"""
    credentials, _db = repo
    first = _add(credentials, "a")
    second = _add(credentials, "b")
    executor = _executor(credentials, [_good()])
    scope = executor._new_scope()          # noqa: SLF001
    until = int(time.time()) + 3600

    scope.exclude_provider("trae", until=until)

    now = int(time.time())
    assert scope.penalized(first, "glm-5.2", now)
    assert scope.penalized(second, "glm-5.2", now)
    # 其它渠道不受影响
    assert not scope.penalized("other", "glm-5.2", now)


def test_penalized_without_model_scope_ignores_model_entries(repo):
    """无模型作用域时只看账号级/禁用：模型级条目不能连带排除（无模型可归因）。"""
    credentials, _db = repo
    credential_id = _add(credentials, "a")
    executor = _executor(credentials, [_good()])
    scope = executor._new_scope()          # noqa: SLF001

    from src.engine.scheduler import ErrorOutcome, ModelCooldown

    scope.exclude(credential_id, ErrorOutcome(model_cooldowns={
        "glm-5.2": ModelCooldown(cooling_until=int(time.time()) + 600, hits=1, reason="soft")}))

    assert scope.penalized(credential_id, None, int(time.time())) is False
    assert scope.penalized(credential_id, "glm-5.2", int(time.time())) is True


def test_provider_pause_ignores_other_channels(repo):
    """渠道级暂停只覆盖该渠道的行：别的渠道凭证不受影响。"""
    credentials, _db = repo
    other = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    _add(credentials, "a")
    executor = _executor(credentials, [_good()])
    scope = executor._new_scope()          # noqa: SLF001

    scope.exclude_provider("trae", until=int(time.time()) + 3600)

    assert scope.penalized(other, "glm-5.2", int(time.time())) is False
    assert scope.penalty(other) is None


def test_all_rows_filters_by_provider(repo):
    """`all_rows(providers)` 只回该渠道的行，`selectable_only` 再剔禁用/暂停。"""
    credentials, _db = repo
    trae = _add(credentials, "a")
    paused = _add(credentials, "b")
    other = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.set_enabled(paused, False)
    executor = _executor(credentials, [_good()])
    scope = executor._new_scope()          # noqa: SLF001

    assert {c.credential_id for c in scope.all_rows(["trae"])} == {trae, paused}
    assert {c.credential_id for c in scope.all_rows(["codebuddy"])} == {other}
    assert [c.credential_id for c in scope.all_rows(["trae"], selectable_only=True)] == [trae]


def test_scope_clear_releases_local_penalty(repo):
    """短冷却等到期后本地排除必须解除，否则「等到期再重取」还是选不到它。"""
    credentials, _db = repo
    credential_id = _add(credentials, "a")
    executor = _executor(credentials, [_good()])
    scope = executor._new_scope()          # noqa: SLF001

    from src.engine.scheduler import ErrorOutcome

    scope.exclude(credential_id, ErrorOutcome(cooling_until=int(time.time()) + 5))
    scope.clear(credential_id)

    assert scope.penalty(credential_id) is None
    assert not scope.penalized(credential_id, "glm-5.2", int(time.time()))


def test_concurrent_requests_get_independent_snapshots(repo):
    """并发请求各自一份快照：一个请求的本地排除不会串到另一个。"""
    credentials, _db = repo
    credential_id = _add(credentials, "a")
    executor = _executor(credentials, [_good()])

    from src.engine.scheduler import ErrorOutcome

    seen: list[bool] = []

    async def request() -> None:
        with executor._request_scope():    # noqa: SLF001
            scope = executor._scope()      # noqa: SLF001
            seen.append(scope.penalized(credential_id, "glm-5.2", int(time.time())))
            scope.exclude(credential_id, ErrorOutcome(
                cooling_until=int(time.time()) + 600))

    async def run() -> None:
        await asyncio.gather(request(), request())

    asyncio.run(run())

    # 两个请求都是「开始时未被排除」——另一方的写入没有串进来
    assert seen == [False, False]


def test_later_request_sees_cooldown_written_by_previous_request(repo):
    """跨请求不共享快照：第二个请求能看到第一个请求写下的冷却。"""
    credentials, _db = repo
    cooled = _add(credentials, "a")
    healthy = _add(credentials, "b")
    executor = _executor(credentials, [
        [_KindError(ErrKind.SOFT)],
        _good("second"),
    ])

    asyncio.run(executor.complete(chat_request()))

    # 第二个全新请求：取到的是新快照，冷却中的号已在行里（选中的那条进冷却，
    # 另一条不受影响——这里不假定调度器先选谁）
    scope = executor._new_scope()          # noqa: SLF001
    cooled_rows = [c for c in scope.rows if c.cooling_until is not None]
    assert len(cooled_rows) == 1
    assert cooled_rows[0].credential_id in {cooled, healthy}


def test_request_scope_restores_outer_scope(repo):
    """嵌套 / 退出后还原：作用域不会泄漏到作用域外（无作用域时现取最新库）。"""
    credentials, _db = repo
    _add(credentials, "a")
    executor = _executor(credentials, [_good()])

    with executor._request_scope():        # noqa: SLF001
        assert executor._scope() is not None    # noqa: SLF001

    assert executor._mutable_scope() is None    # noqa: SLF001 - 已还原


def test_scope_without_context_reads_live_state(repo):
    """无作用域（自检 / 单测直接调用）时现取一份：仍看到最新库状态。"""
    credentials, db = repo
    credential_id = _add(credentials, "a")
    executor = _executor(credentials, [_good()])

    first = executor._scope()              # noqa: SLF001
    assert first.rows[0].cooling_until is None

    db.connect().execute("UPDATE credentials SET cooling_until = ? WHERE id = ?",
                         (int(time.time()) + 600, credential_id))
    second = executor._scope()             # noqa: SLF001
    assert second.rows[0].cooling_until is not None


def test_local_disabled_penalty_blocks_selection(repo):
    """DEAD 的本地镜像：同请求重试不再选它（快照行仍显示可用）。"""
    credentials, _db = repo
    credential_id = _add(credentials, "a")
    executor = _executor(credentials, [_good()])
    scope = executor._new_scope()          # noqa: SLF001

    from src.engine.scheduler import ErrorOutcome

    scope.exclude(credential_id, ErrorOutcome(disabled=True))

    assert scope.penalized(credential_id, "glm-5.2", int(time.time()))


def test_wait_short_cooldown_ignores_locally_disabled_credential(repo):
    """本请求刚判 DEAD（本地禁用镜像）的号不参与短冷却等待：等几秒不会恢复。"""
    from src.engine.model_resolver import resolve
    from src.engine.scheduler import ErrorOutcome

    credentials, _db = repo
    credential_id = _add(credentials, "a")
    executor = _executor(credentials, [_good()], short_cooldown_wait_seconds=10)
    target = resolve("glm-5.2@trae", "glm-5.2")

    with executor._request_scope():        # noqa: SLF001
        scope = executor._scope()          # noqa: SLF001
        scope.exclude(credential_id, ErrorOutcome(disabled=True))
        assert asyncio.run(
            executor._wait_short_cooldown(target)) is None   # noqa: SLF001


def test_recovery_seconds_sees_local_penalty(repo):
    """`Retry-After` 也按「本地刚写的冷却」算：否则 503 会给出错的最早恢复点。"""
    from src.engine.model_resolver import resolve
    from src.engine.scheduler import ErrorOutcome

    credentials, _db = repo
    credential_id = _add(credentials, "a")
    executor = _executor(credentials, [_good()])
    target = resolve("glm-5.2@trae", "glm-5.2")

    with executor._request_scope():        # noqa: SLF001
        executor._scope().exclude(         # noqa: SLF001
            credential_id, ErrorOutcome(cooling_until=int(time.time()) + 120))
        assert executor._recovery_seconds(target) == pytest.approx(120, abs=5)  # noqa: SLF001
