"""Pacer 节流与并发名额（固定间隔 / 按凭证分桶 / 在途上限 / 滑动窗口）。"""

from __future__ import annotations

import asyncio

import pytest

from src.tasks import TaskReport
from src.tasks.pacer import JITTER_RATIO, Pacer, paced, stable_key


async def test_pacer_disabled_is_noop():
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    pacer = Pacer(0, 0, sleep=fake_sleep)
    assert pacer.disabled is True
    await pacer.wait_turn()
    await pacer.wait_turn()
    assert slept == []

async def test_pacer_fixed_interval_sleeps_remaining():
    slept: list[float] = []
    clock = {"t": 100.0}

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    pacer = Pacer(5, 5, sleep=fake_sleep, now=lambda: clock["t"])
    await pacer.wait_turn()                 # 首次不睡
    await pacer.wait_turn()                 # 第二次补足 5s（含抖动）
    assert len(slept) == 1
    assert 5.0 <= slept[0] <= 5.0 * (1 + JITTER_RATIO)

async def test_pacer_random_interval_within_bounds():
    pacer = Pacer(5, 20)
    intervals = [pacer.next_interval() for _ in range(20)]
    assert all(5 <= value <= 20 for value in intervals)
    assert len(set(intervals)) > 1          # 确实是随机而非定值

@pytest.mark.parametrize(("low", "high"), [(-1, 5), (10, 5)])
def test_pacer_rejects_invalid_bounds(low, high):
    with pytest.raises(ValueError):
        Pacer(low, high)

async def test_pacer_skips_sleep_when_elapsed_exceeds_interval():
    slept: list[float] = []
    clock = {"t": 0.0}

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    pacer = Pacer(5, 5, sleep=fake_sleep, now=lambda: clock["t"])
    await pacer.wait_turn()
    clock["t"] = 100.0
    await pacer.wait_turn()
    assert slept == []

async def test_pacer_concurrent_allows_same_bucket_parallelism():
    """同桶已有在途请求时直接放行：这是「同渠道同模型并发不并行」的修复点。"""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    clock = {"t": 0.0}
    pacer = Pacer(5, 5, allow_concurrent=True, sleep=fake_sleep,
                  now=lambda: clock["t"])
    assert pacer.allow_concurrent is True
    await pacer.wait_turn("codebuddy:acct")      # 首请求：不睡
    await pacer.wait_turn("codebuddy:acct")      # 在途 → 直接放行
    await pacer.wait_turn("codebuddy:acct")
    assert slept == []
    pacer.release("codebuddy:acct")
    pacer.release("codebuddy:acct")
    pacer.release("codebuddy:acct")

async def test_pacer_concurrent_spaces_sequential_starts():
    """桶空闲后「紧接着又来一个」才补足最小间隔。"""
    slept: list[float] = []
    clock = {"t": 0.0}

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    pacer = Pacer(5, 5, allow_concurrent=True, sleep=fake_sleep,
                  now=lambda: clock["t"])
    await pacer.wait_turn("k")
    pacer.release("k")                           # 上一请求结束
    await pacer.wait_turn("k")                   # 紧接又来一个 → 补 5s（含抖动）
    assert len(slept) == 1
    assert 5.0 <= slept[0] <= 5.0 * (1 + JITTER_RATIO)

async def test_pacer_concurrent_buckets_are_independent():
    """不同凭证分属不同桶：一个账号在途不拖住另一个账号。"""
    slept: list[float] = []
    clock = {"t": 0.0}

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    pacer = Pacer(5, 5, allow_concurrent=True, sleep=fake_sleep,
                  now=lambda: clock["t"])
    await pacer.wait_turn("a")
    pacer.release("a")
    await pacer.wait_turn("b")                   # 独立桶：不睡
    assert slept == []

async def test_pacer_concurrent_disabled_and_missing_key_are_noops():
    """0 关闭时不占名额；空 key 与 release 多余调用都不炸。"""
    pacer = Pacer(0, 0, allow_concurrent=True)
    assert pacer.disabled is True
    await pacer.wait_turn("k")
    pacer.release("k")
    pacer.release()                              # 多余 release（默认空桶）
    # 关闭态下再严格模式校准一次：严格模式 release 是空操作
    strict = Pacer(5, 5, allow_concurrent=False)
    assert strict.allow_concurrent is False
    strict.release("k")
    await strict.wait_turn("k")

async def test_pacer_concurrent_skips_when_interval_already_elapsed():
    """桶空闲且距上次开始已超过间隔：不睡。"""
    slept: list[float] = []
    clock = {"t": 0.0}

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    pacer = Pacer(5, 5, allow_concurrent=True, sleep=fake_sleep,
                  now=lambda: clock["t"])
    await pacer.wait_turn("k")
    pacer.release("k")
    clock["t"] = 100.0
    await pacer.wait_turn("k")
    assert slept == []

async def test_pacer_max_concurrency_blocks_until_release():
    """名额满时新请求挂起，直到有请求 release 让位（CodeArts 每账号 3 并发）。"""
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=2)
    await pacer.wait_turn("k")
    await pacer.wait_turn("k")
    assert pacer.max_concurrency == 2
    third = asyncio.create_task(pacer.wait_turn("k"))
    await asyncio.sleep(0)                # 让 third 跑到「等名额」处
    assert not third.done()               # 名额满 → 挂起
    pacer.release("k")                    # 让出一个名额
    await asyncio.wait_for(third, timeout=1)
    pacer.release("k")
    pacer.release("k")
    assert pacer._inflight == {}

async def test_pacer_max_concurrency_zero_is_unlimited():
    """0 = 关闭上限，保持旧行为（桶内并发放行）。"""
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=0)
    for _ in range(10):
        await pacer.wait_turn("k")
    assert pacer.max_concurrency == 0
    for _ in range(10):
        pacer.release("k")

async def test_pacer_max_concurrency_reads_live_limit():
    """上限存取值器：运行中放宽对后续让位立即生效（热更）。"""
    limit = {"n": 1}
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=lambda: limit["n"])
    await pacer.wait_turn("k")
    assert pacer.max_concurrency == 1
    blocked = asyncio.create_task(pacer.wait_turn("k"))
    await asyncio.sleep(0)
    assert not blocked.done()
    limit["n"] = 3                         # 热更放宽
    pacer.release("k")                     # 现有请求结束 → 唤醒等待者
    await asyncio.wait_for(blocked, timeout=1)
    pacer.release("k")

async def test_pacer_max_concurrency_cancelled_waiter_does_not_leak():
    """等待名额时被取消：不占用名额，桶回到干净状态。"""
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1)
    await pacer.wait_turn("k")
    waiter = asyncio.create_task(pacer.wait_turn("k"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    pacer.release("k")
    assert pacer._inflight == {}
    await pacer.wait_turn("k")             # 名额未泄漏，可再次取得
    pacer.release("k")

async def test_pacer_max_concurrency_cancel_during_interval_sleep_rolls_back():
    """预留名额后、补间隔睡眠中被取消：名额必须归还，否则该桶永久少一个。"""
    gate = asyncio.Event()
    clock = {"t": 0.0}

    async def blocking_sleep(_seconds: float) -> None:
        await gate.wait()

    pacer = Pacer(5, 5, allow_concurrent=True, max_concurrency=1,
                  sleep=blocking_sleep, now=lambda: clock["t"])
    await pacer.wait_turn("k")             # 首次：不睡
    pacer.release("k")                     # 桶空闲，但距上次开始 0s
    task = asyncio.create_task(pacer.wait_turn("k"))
    await asyncio.sleep(0)                 # 跑到补间隔睡眠处（已预留名额）
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pacer._inflight == {}           # 名额已回滚

def test_pacer_rejects_negative_max_concurrency():
    with pytest.raises(ValueError):
        Pacer(0, 0, allow_concurrent=True, max_concurrency=-1)

def test_stable_key_degrades_to_provider_when_identity_missing():
    assert stable_key("codebuddy", None) == "codebuddy"
    assert stable_key("trae", "") == "trae"
    assert stable_key("codebuddy", "acct-1") == stable_key("codebuddy", "acct-1")
    assert stable_key("codebuddy", "acct-1") != stable_key("trae", "acct-1")

@pytest.mark.parametrize("bad", [-1, -0.5])
def test_pacer_rejects_negative_window(bad):
    with pytest.raises(ValueError):
        Pacer(0, 0, allow_concurrent=True, max_concurrency=3,
              window_seconds=bad)

async def test_pacer_window_blocks_after_release_until_slot_expires():
    """核心回归：打满窗口后，即使 release 让出在途名额，也不能立刻再发。

    这正是 CodeArts「用了三次就限制」的上游口径——会话在流结束后仍滞留，
    纯在途上限挡不住「3 个刚结束就再发 3 个」。
    """
    slept: list[float] = []
    clock = {"t": 0.0}
    gate = asyncio.Event()

    async def gated_sleep(seconds: float) -> None:
        slept.append(seconds)
        await gate.wait()                        # 真正挂起，便于断言「仍在等」
        clock["t"] += seconds

    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=3,
                  window_seconds=60, sleep=gated_sleep, now=lambda: clock["t"])
    assert pacer.window_seconds == 60

    await pacer.wait_turn("k")                   # t=0，窗口 [0,3/60]
    await pacer.wait_turn("k")
    await pacer.wait_turn("k")
    # 三个都结束、名额全归还——纯在途模式下第 4 个本该放行
    pacer.release("k")
    pacer.release("k")
    pacer.release("k")

    fourth = asyncio.create_task(pacer.wait_turn("k"))
    await asyncio.sleep(0)
    assert not fourth.done()                     # 窗口满 → 仍挂起
    assert slept == [60.0]                       # 要等到最早一次启动滑出窗口
    # 恢复由「最早一次启动滑出窗口」驱动：t=0 的启动在 t=60 过期。
    gate.set()
    await asyncio.wait_for(fourth, timeout=1)
    assert clock["t"] == 60.0

async def test_pacer_window_is_per_bucket():
    """窗口按桶独立：一个账号窗口打满不影响另一个账号。"""
    clock = {"t": 0.0}

    async def fake_sleep(seconds: float) -> None:
        clock["t"] += seconds

    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1,
                  window_seconds=60, sleep=fake_sleep, now=lambda: clock["t"])
    await pacer.wait_turn("a")
    pacer.release("a")
    await pacer.wait_turn("b")                   # 独立桶：不睡
    assert clock["t"] == 0.0

async def test_pacer_window_cancel_rolls_back_start():
    """窗口模式下、补最小间隔睡眠中被取消：不白占窗口配额。"""
    gate = asyncio.Event()
    clock = {"t": 0.0}

    async def blocking_sleep(_seconds: float) -> None:
        await gate.wait()

    pacer = Pacer(5, 5, allow_concurrent=True, max_concurrency=3,
                  window_seconds=60, sleep=blocking_sleep,
                  now=lambda: clock["t"])
    await pacer.wait_turn("k")                   # 首次不睡，登记窗口启动（t=0）
    pacer.release("k")
    clock["t"] = 1.0
    task = asyncio.create_task(pacer.wait_turn("k"))  # 桶空闲 → 补 5s 睡眠
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pacer._inflight == {}
    # t=1.0 那次未真正发起的登记已抹掉，只剩 t=0 的合法登记
    assert pacer._starts == {"k": [0.0]}

async def test_pacer_window_rollback_skips_pruned_registration():
    """窗口登记在睡眠期间被别的等待者 prune 掉后取消：不因找不到而报错。"""
    gate = asyncio.Event()
    clock = {"t": 0.0}

    async def blocking_sleep(_seconds: float) -> None:
        await gate.wait()

    pacer = Pacer(5, 5, allow_concurrent=True, max_concurrency=3,
                  window_seconds=1, sleep=blocking_sleep,
                  now=lambda: clock["t"])
    await pacer.wait_turn("k")                   # t=0，登记 [0]
    pacer.release("k")
    clock["t"] = 0.5
    task = asyncio.create_task(pacer.wait_turn("k"))  # 登记 [0, 0.5] 后补 4.5s 睡眠
    await asyncio.sleep(0)
    clock["t"] = 3.0                             # 窗口滑过 → 另一个等待者 prune 掉 0/0.5
    await pacer.wait_turn("k")                   # 重新登记 [3]
    pacer.release("k")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pacer._starts == {"k": [3.0]}         # 只抹掉自己的登记，不动别人的

async def test_pacer_window_inflight_guard_blocks_when_stream_outlives_window():
    """长响应超过窗口时，在途闸门仍须挡住第 4 个（否则再撞 400）。

    窗口只按「启动时刻」计数：若 N 个流存活时间 > window，它们的启动时刻会
    滑出窗口，但 `_inflight` 仍为 N —— 此时必须靠在途上限兜底，等到有流
    `release` 才放行。
    """
    clock = {"t": 0.0}
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=2,
                  window_seconds=1, now=lambda: clock["t"])

    await pacer.wait_turn("k")                   # t=0，窗口/在途都占 1
    await pacer.wait_turn("k")                   # t=0，占满（在途 2）
    clock["t"] = 2.0                             # 两个流仍在途，但启动时刻已滑出窗口

    fourth = asyncio.create_task(pacer.wait_turn("k"))
    await asyncio.sleep(0)
    assert not fourth.done()                     # 窗口空、在途满 → 仍挂起等 release
    pacer.release("k")                           # 一个流结束
    await asyncio.wait_for(fourth, timeout=1)    # 让位后立即放行
    assert len(pacer._starts["k"]) == 1          # 新请求只登记自己的启动
    pacer.release("k")
    pacer.release("k")

async def test_pacer_window_disabled_falls_back_to_inflight():
    """window_seconds=0：退回纯在途口径，release 立即放行（旧行为）。"""
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1)
    assert pacer.window_seconds == 0.0
    await pacer.wait_turn("k")
    releaser = asyncio.create_task(asyncio.sleep(0))
    await releaser
    pacer.release("k")
    await asyncio.wait_for(pacer.wait_turn("k"), timeout=1)  # 立刻放行
    pacer.release("k")

async def test_pacer_window_reads_live_length():
    """窗口长度存取值器：热更后立即影响过期判定。"""
    clock = {"t": 0.0}
    win = {"n": 60.0}

    async def fake_sleep(seconds: float) -> None:
        clock["t"] += seconds

    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1,
                  window_seconds=lambda: win["n"], sleep=fake_sleep,
                  now=lambda: clock["t"])
    await pacer.wait_turn("k")
    pacer.release("k")
    win["n"] = 5.0                               # 热更：缩短窗口
    clock["t"] = 10.0                            # 原 60s 窗口下仍未过期
    await asyncio.wait_for(pacer.wait_turn("k"), timeout=1)
    pacer.release("k")

def test_task_report_dict():
    report = TaskReport(attempted=3, succeeded=2, failed=1, skipped=4)
    assert report.as_dict() == {"attempted": 3, "succeeded": 2, "failed": 1, "skipped": 4}

def test_pacer_random_single_value_path():
    pacer = Pacer(3, 3)
    intervals = [pacer.next_interval() for _ in range(20)]
    assert all(3 <= value <= 3 * (1 + JITTER_RATIO) for value in intervals)
    assert len(set(intervals)) > 1           # 定值间隔也抖动，非同拍
    assert pacer.disabled is False

def test_pacer_fixed_interval_jitter_is_upward_only():
    """定值间隔的抖动**只向上**：`min` 是频率风控护栏，不可击穿。"""
    assert all(Pacer(7, 7).next_interval() >= 7 for _ in range(20))

def test_pacer_range_interval_is_not_jittered_further():
    """`min < max` 的区间本就均匀随机，抖动不叠加（否则越出 `[min, max]`）。"""
    intervals = [Pacer(5, 20).next_interval() for _ in range(50)]
    assert all(5 <= value <= 20 for value in intervals)

def test_pacer_disabled_next_interval_is_zero():
    assert Pacer(0, 0).next_interval() == 0.0


# ------------------------------------------------------------------ paced()


class _RecordingPacer:
    """只实现 provider 真正用到的两个接口（paced 不要求更多）。"""

    def __init__(self) -> None:
        self.waited: list[str | None] = []
        self.released: list[str | None] = []

    async def wait_turn(self, key: str | None = None) -> None:
        self.waited.append(key)

    def release(self, key: str | None = None) -> None:
        self.released.append(key)


async def test_paced_takes_and_releases_turn():
    pacer = _RecordingPacer()
    async with paced(pacer, "bucket"):
        assert pacer.waited == ["bucket"]
        assert pacer.released == []          # 还没出块

    assert pacer.released == ["bucket"]


async def test_paced_releases_even_when_body_raises():
    """`finally` 归还名额：漏了会让该桶永久满载（「用了三次就限制」）。"""
    pacer = _RecordingPacer()
    with pytest.raises(RuntimeError):
        async with paced(pacer, "b"):
            raise RuntimeError("上游炸了")

    assert pacer.released == ["b"]


async def test_paced_releases_on_generator_early_close():
    """`async for ... break` 不耗尽生成器：出块时必须已归还。"""
    pacer = _RecordingPacer()

    async def consume() -> None:
        async with paced(pacer, "b"):
            yield 1                          # noqa: B901 - 故意只产一个

    gen = consume()
    assert await gen.__anext__() == 1
    await gen.aclose()                       # 模拟 break 后的显式关闭

    assert pacer.released == ["b"]


async def test_paced_without_pacer_is_passthrough():
    """无凭证渠道 / 测试：pacer=None 直接放行，不报错。"""
    ran = False
    async with paced(None, "whatever"):
        ran = True

    assert ran is True


async def test_paced_passes_key_through():
    pacer = _RecordingPacer()
    async with paced(pacer, None):
        pass

    assert pacer.waited == [None] and pacer.released == [None]
