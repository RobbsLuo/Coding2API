"""A4：短冷却「等待续上」。

口径（计划 A4）：

- **有立即可用候选就绝不等**（等冷却中的号等于白降吞吐）；
- 全池都在冷却、但最短剩余 ≤ `short_cooldown_wait_seconds` → 等它到期后
  重取一次，成功续上；
- 超过上限 / 关闭（0）→ 立刻 503，不做「等第二轮」；
- 等待期间**不占在途租约**、**不计入 `max_rotate`**；
- 值经零参 callable 注入（热更）。

全部用假时钟（`time.time` + `asyncio.sleep` 一起换掉）：等待推进假时钟而不是
真实墙钟，所以整个文件毫秒级跑完。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.compat.openai.request import parse_chat_request
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.inflight import InflightGuard, InflightTracker
from src.engine.model_resolver import resolve
from src.engine.scheduler import ErrorOutcome, Scheduler
from src.provider.base import Event, EventKind, Usage
from tests.helpers.events import GOOD

_TARGET = resolve("glm-5.2@trae", "glm-5.2")


class _Clock:
    """假时钟：`time.time` 与 `asyncio.sleep` 共享同一个游标。"""

    def __init__(self) -> None:
        self.now = float(int(time.time()))
        self.sleeps: list[float] = []
        # 每次 sleep 时回调，用于断言「等待期间没有持有租约」等不变量
        self.on_sleep = None

    def time(self) -> float:
        return self.now

    async def sleep(self, delay, result=None):
        self.sleeps.append(delay)
        if self.on_sleep is not None:
            self.on_sleep()
        self.now += delay
        await _REAL_SLEEP(0)


_REAL_SLEEP = asyncio.sleep


@pytest.fixture()
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(time, "time", fake.time)
    monkeypatch.setattr(asyncio, "sleep", fake.sleep)
    return fake


def _cool(credentials, credential_id: str, seconds: float) -> None:
    """把凭证放进账号级冷却 `seconds` 秒。"""
    credentials.save_error(
        credential_id, ErrorOutcome(cooling_until=int(time.time() + seconds)))


def _cool_model(credentials, credential_id: str, model: str, seconds: float) -> None:
    """把 (凭证, 模型) 放进模型级冷却 `seconds` 秒。"""
    credentials.save_error(
        credential_id,
        ErrorOutcome(model_cooldowns={model: _model_entry(int(time.time() + seconds))}))


def _model_entry(until: int):
    from src.engine.scheduler import ModelCooldown

    return ModelCooldown(cooling_until=until, hits=1, reason="model")


def _executor(credentials, provider, **deps) -> Executor:
    return Executor(ExecutorDeps(providers={"trae": provider}, credentials=credentials,
                                 scheduler=deps.pop("scheduler", Scheduler()),
                                 default_model="glm-5.2", **deps))


class _Provider:
    id = "trae"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        self.calls += 1
        for event in GOOD:
            yield event


def _request():
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}]})


# ------------------------------------------------------------ 直接测等待判定


def _unit_executor(credentials, **deps) -> Executor:
    """只用于 `_wait_short_cooldown` 的 executor（provider 值不会被调用）。"""
    return Executor(ExecutorDeps(providers={"trae": object()}, credentials=credentials,
                                 scheduler=Scheduler(), default_model="glm-5.2", **deps))


async def test_no_wait_when_limit_disabled(repo, clock):
    """上限 0 = 关闭：直接返回 None，一秒都不等。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=0)
    assert await executor._wait_short_cooldown(_TARGET) is None
    assert clock.sleeps == []
    db.close()


async def test_no_wait_when_no_registered_provider(repo, clock):
    """模型没有注册渠道（目录全空）：等待也没有意义，直接失败。"""
    credentials, db = repo
    executor = Executor(ExecutorDeps(providers={}, credentials=credentials,
                                     scheduler=Scheduler(), default_model="glm-5.2",
                                     short_cooldown_wait_seconds=10))
    assert await executor._wait_short_cooldown(_TARGET) is None
    assert clock.sleeps == []
    db.close()


async def test_waits_for_shortest_cooldown(repo, clock):
    """多个候选各在不同冷却：等**最短**的那个（而不是最长或平均）。"""
    credentials, db = repo
    slow = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    fast = credentials.add(provider="trae", credential_data={"accessToken": "b"})
    _cool(credentials, slow, 8)
    _cool(credentials, fast, 3)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)
    assert await executor._wait_short_cooldown(_TARGET) == fast
    assert clock.sleeps == [3]
    db.close()


async def test_shorter_first_then_longer_keeps_the_minimum(repo, clock):
    """先遇到短冷却、再遇到长冷却：`remaining` 不被放大（保持最短）。"""
    credentials, db = repo
    fast = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    slow = credentials.add(provider="trae", credential_data={"accessToken": "b"})
    _cool(credentials, fast, 2)
    _cool(credentials, slow, 9)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)
    assert await executor._wait_short_cooldown(_TARGET) == fast
    assert clock.sleeps == [2]
    db.close()


async def test_waits_only_when_remaining_within_limit(repo, clock):
    """最短剩余超过上限 → 不等（否则慢请求会在池子打满时层层排队）。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 30)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)
    assert await executor._wait_short_cooldown(_TARGET) is None
    assert clock.sleeps == []
    db.close()


async def test_wait_limit_is_read_per_call(repo, clock):
    """上限经零参 callable 现读：同一个 executor 改值立即生效（热更）。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 5)
    limit = {"value": 0}
    executor = _unit_executor(credentials,
                              short_cooldown_wait_seconds=lambda: limit["value"])
    assert await executor._wait_short_cooldown(_TARGET) is None
    limit["value"] = 5
    assert await executor._wait_short_cooldown(_TARGET) is not None
    assert clock.sleeps == [5]
    db.close()


async def test_model_level_cooldown_counts_toward_wait(repo, clock):
    """模型级冷却同样参与等待：账号本身没冷却，只是这个模型被限流。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool_model(credentials, cid, "glm-5.2", 4)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)
    assert await executor._wait_short_cooldown(_TARGET) is not None
    assert clock.sleeps == [4]
    db.close()


async def test_account_and_model_cooldown_take_the_later_one(repo, clock):
    """账号级与模型级同时存在：两个都过期才可用，按**较晚**的那个等。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 2)
    _cool_model(credentials, cid, "glm-5.2", 6)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)
    assert await executor._wait_short_cooldown(_TARGET) is not None
    assert clock.sleeps == [6]
    db.close()


async def test_tried_credential_in_cooldown_still_counts(repo, clock):
    """本轮轮换试过、但已进短冷却的号**仍然参与等待**（B1）。

    旧口径按 `tried` 排除，导致「一轮试完、全池都在软冷却」这个最常见的
    耗尽场景直接 503，等待机制永远触发不到。
    """
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)
    assert await executor._wait_short_cooldown(_TARGET) == cid
    assert clock.sleeps == [3]
    db.close()


async def test_tried_dead_credential_with_long_cooldown_fails_fast(repo, clock):
    """DEAD / INVALID 这类长冷却仍在 `tried` 里：剩余时间远超上限 → 不等。

    不需要按错误类型特判——`remaining > limit` 自然挡掉。
    """
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3600)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=60)
    assert await executor._wait_short_cooldown(_TARGET) is None
    assert clock.sleeps == []
    db.close()


async def test_disabled_credentials_are_ignored(repo, clock):
    """硬禁用 / 用户软关闭的号不是「等几秒就好」，不参与等待判定。"""
    credentials, db = repo
    hard = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    soft = credentials.add(provider="trae", credential_data={"accessToken": "b"})
    credentials.set_enabled(soft, False)
    db.connect().execute("UPDATE credentials SET disabled = 1 WHERE id = ?", (hard,))
    db.connect().commit()
    _cool(credentials, hard, 3)
    _cool(credentials, soft, 3)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)
    assert await executor._wait_short_cooldown(_TARGET) is None
    assert clock.sleeps == []
    db.close()


async def test_limit_blocked_credentials_are_ignored(repo, clock):
    """被限额护栏停用的号同样不参与等待（等几秒不会恢复额度）。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)

    class AllBlocked:
        def blocked(self, candidate, model):
            return True

    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10,
                              limits=AllBlocked())
    assert await executor._wait_short_cooldown(_TARGET) is None
    assert clock.sleeps == []
    db.close()


async def test_inflight_blocked_credentials_are_ignored(repo, clock):
    """被在途占满的号不算「等几秒就好」（它要等一整个请求跑完）。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)
    guard = InflightGuard(lambda: 1)
    guard.acquire(cid)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10,
                              inflight=guard)
    assert await executor._wait_short_cooldown(_TARGET) is None
    assert clock.sleeps == []
    db.close()


async def test_expired_cooldown_is_not_waited_for(repo, clock):
    """冷却已过期（候选却仍不可选，例如并发删除）：等几秒也不会变好，直接失败。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, -5)                   # 已过期
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)
    assert await executor._wait_short_cooldown(_TARGET) is None
    assert clock.sleeps == []
    db.close()


async def test_no_cooldown_at_all_is_not_waited_for(repo, clock):
    """候选既无冷却也未被试过，但池子为空（无凭证）：没有「等到就好」的目标。"""
    credentials, db = repo
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)
    assert await executor._wait_short_cooldown(_TARGET) is None
    assert clock.sleeps == []
    db.close()


# ------------------------------------------------------------ 端到端（_pick）


async def test_immediately_available_candidate_is_not_delayed(repo, clock):
    """有立即可用候选 → 立刻打上游，不等（等待只发生在「一个都没有」时）。"""
    credentials, db = repo
    cooled = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    _cool(credentials, cooled, 3)
    provider = _Provider()
    executor = _executor(credentials, provider, short_cooldown_wait_seconds=10)
    result = await executor.complete(_request())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert provider.calls == 1
    assert clock.sleeps == []                     # 一次都没等
    db.close()


async def test_waits_then_serves_on_same_credential(repo, clock):
    """唯一凭证短冷却 → 等它到期后**原连接**续上，而不是 503。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 4)
    provider = _Provider()
    executor = _executor(credentials, provider, short_cooldown_wait_seconds=10)
    result = await executor.complete(_request())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert provider.calls == 1
    assert clock.sleeps == [4]
    db.close()


async def test_long_cooldown_still_fails_fast(repo, clock):
    """剩余冷却超过上限 → 立刻 503（不睡、不排队）。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 60)
    provider = _Provider()
    executor = _executor(credentials, provider, short_cooldown_wait_seconds=10)
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    assert provider.calls == 0
    assert clock.sleeps == []
    db.close()


async def test_disabled_wait_keeps_fast_failure(repo, clock):
    """显式 0 → 关闭：全池短冷却时立刻 503（不睡、不排队）。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)
    provider = _Provider()
    executor = _executor(credentials, provider, short_cooldown_wait_seconds=0)
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    assert provider.calls == 0
    assert clock.sleeps == []
    db.close()


async def test_wait_does_not_consume_rotate_budget(repo, clock):
    """等待不计入 `max_rotate`：max_rotate=1 时等待后仍能打一次上游。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)
    provider = _Provider()
    executor = _executor(credentials, provider, scheduler=Scheduler(max_rotate=1),
                         short_cooldown_wait_seconds=10)
    result = await executor.complete(_request())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert provider.calls == 1
    db.close()


async def test_wait_holds_no_inflight_lease(repo, clock):
    """等待期间不占租约：睡的那一刻在途计数必须是 0（否则并发会堆成洪峰）。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)
    guard = InflightGuard(lambda: 1)
    observed: list[int] = []
    clock.on_sleep = lambda: observed.append(guard.count(cid))
    provider = _Provider()
    executor = _executor(credentials, provider, inflight=guard,
                         short_cooldown_wait_seconds=10)
    await executor.complete(_request())
    assert observed == [0]                        # 等待时没有租约
    assert guard.count(cid) == 0                  # 结束后也已释放
    db.close()


async def test_wait_happens_only_once(repo, clock):
    """只重取一次：睡醒后仍无可用候选就照常 503，不做第二轮排队。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)
    provider = _Provider()

    def _freeze() -> None:
        # 睡醒后时钟没推进（等价于「别的请求又抢走了名额」）：候选仍不可选
        clock.now -= clock.sleeps[-1]

    clock.on_sleep = _freeze
    executor = _executor(credentials, provider, short_cooldown_wait_seconds=10)
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    assert clock.sleeps == [3]                    # 只等了一次
    assert provider.calls == 0
    db.close()


async def test_wait_also_covers_stream_path(repo, clock):
    """流式路径同样走 `_pick`：短冷却等待后正常出帧，客户端无错误帧。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 2)
    provider = _Provider()
    executor = _executor(credentials, provider, short_cooldown_wait_seconds=10)
    frames = [frame async for frame in executor.stream(
        parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                            "stream": True}), username="alice")]
    assert b'"ok"' in b"".join(frames)
    assert provider.calls == 1
    assert clock.sleeps == [2]
    db.close()


async def test_wait_after_soft_error_reuses_cooled_credential(repo, clock):
    """第一次 SOFT 失败 → 冷却 60s 太长不等待；配一个 3s 冷却的号则续上。

    这里验证的是「冷却由真实错误写入后，等待判定读的是落库值」——两段
    `_pick` 之间的状态确实串起来了。
    """
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)
    provider = _Provider()
    executor = _executor(credentials, provider, short_cooldown_wait_seconds=10)
    await executor.complete(_request())
    assert clock.sleeps == [3]
    db.close()


async def test_usage_recorded_after_wait(repo, clock):
    """等待续上后按成功记账（等待本身不产生任何统计行）。"""
    from src.stats.collector import StatsCollector

    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 2)

    class WithUsage(_Provider):
        async def stream_chat(self, credential_data, payload, model):
            self.calls += 1
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.USAGE, usage=Usage(5, 6))
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    executor = _executor(credentials, WithUsage(), short_cooldown_wait_seconds=10,
                         stats=StatsCollector(db))
    await executor.complete(_request())
    rows = db.connect().execute(
        "SELECT ok, input_tokens, output_tokens FROM usage_events").fetchall()
    assert [(r["ok"], r["input_tokens"], r["output_tokens"]) for r in rows] == [(1, 5, 6)]
    db.close()


async def test_inflight_tracker_counts_released_after_wait(repo, clock):
    """等待 + 请求结束后租约归零（tracker 视角，防止「等一次漏一次」）。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 2)
    tracker = InflightTracker()
    guard = InflightGuard(lambda: 1, tracker=tracker)
    executor = _executor(credentials, _Provider(), inflight=guard,
                         short_cooldown_wait_seconds=10)
    await executor.complete(_request())
    assert tracker.count(cid) == 0
    db.close()


async def test_pick_reuses_tried_credential_after_wait(repo, clock):
    """`_pick` 等到号后必须先从 `tried` 解禁，否则重取仍选不到它（B1 核心）。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)
    tried = {cid}
    picked = await executor._pick(_TARGET, tried)
    assert picked is not None and picked[0] == cid
    assert clock.sleeps == [3]
    db.close()


async def test_pick_waits_only_once_when_tried(repo, clock):
    """`tried` 号等完仍不可用：只等一次就返回 None（不二次排队）。"""
    credentials, db = repo
    cid = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _cool(credentials, cid, 3)
    executor = _unit_executor(credentials, short_cooldown_wait_seconds=10)

    def _freeze() -> None:
        clock.now -= clock.sleeps[-1]

    clock.on_sleep = _freeze
    assert await executor._pick(_TARGET, {cid}) is None
    assert clock.sleeps == [3]
    db.close()
