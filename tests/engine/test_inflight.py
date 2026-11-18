"""凭证在途租约：进程内计数 + TTL 兜底 + 选号前过滤。"""

from __future__ import annotations

import pytest

from src.compat.openai.request import parse_chat_request
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.inflight import InflightGuard, InflightTracker
from src.engine.scheduler import Scheduler
from src.provider.base import ErrKind, Model
from src.provider.trae.events import UpstreamProtocolViolation
from tests.helpers.chat import chat_request as _request
from tests.helpers.constants import SECRET
from tests.helpers.events import TRAE_GOOD as GOOD_trae

# ------------------------------------------------------------- 纯逻辑：tracker


def test_tracker_acquire_release_roundtrip():
    tracker = InflightTracker()
    assert tracker.count("c1") == 0
    tracker.acquire("c1")
    tracker.acquire("c1")
    assert tracker.count("c1") == 2
    tracker.release("c1")
    assert tracker.count("c1") == 1
    # 释放到空后键被清掉（不留无界增长的尾部空列表）
    tracker.release("c1")
    assert tracker.count("c1") == 0
    # 没有租约时释放是幂等空操作，不抛
    tracker.release("c1")
    tracker.release("never-seen")
    assert tracker.count("never-seen") == 0


def test_tracker_ttl_reclaims_leaked_lease():
    clock = {"now": 100.0}
    tracker = InflightTracker(ttl=10.0, now=lambda: clock["now"])
    tracker.acquire("c1")
    tracker.acquire("c1")
    clock["now"] += 9.0
    assert tracker.count("c1") == 2          # TTL 内
    clock["now"] += 2.0                      # 两个租约都过期
    assert tracker.count("c1") == 0
    # 过期回收是「计数时」顺带做的：再来一个租约，旧的不会复活
    tracker.acquire("c1")
    assert tracker.count("c1") == 1


def test_tracker_ttl_reclaims_only_expired_leases():
    """部分过期：只回收超时的那些，未过期的仍计入。"""
    clock = {"now": 0.0}
    tracker = InflightTracker(ttl=10.0, now=lambda: clock["now"])
    tracker.acquire("c1")                    # t=0
    clock["now"] = 5.0
    tracker.acquire("c1")                    # t=5
    clock["now"] = 12.0                      # 第一个过期（12>10），第二个没过期
    assert tracker.count("c1") == 1
    # 释放的是存活列表里的一个（先入先出），随后归零
    tracker.release("c1")
    assert tracker.count("c1") == 0


def test_tracker_acquire_sweeps_expired_before_counting():
    """acquire 前先回收过期租约，避免「漏释放攒满后永占」。"""
    clock = {"now": 0.0}
    tracker = InflightTracker(ttl=10.0, now=lambda: clock["now"])
    tracker.acquire("c1")
    clock["now"] = 100.0
    tracker.acquire("c1")                    # 旧的已过期，被顺手回收
    assert tracker.count("c1") == 1


# ---------------------------------------------------------------- InflightGuard


def test_guard_unlimited_never_blocks_but_still_counts():
    """不限并发（≤0）时 blocked 恒 False，但仍登记租约——在途数是调度器的
    负载键，关掉上限时也要反映实时负载（否则并发全落排序确定的同一个号）。"""
    guard = InflightGuard(lambda: 0)
    assert guard.limit == 0
    assert guard.blocked("c1") is False
    guard.acquire("c1")
    assert guard.count("c1") == 1            # 不限也计数
    assert guard.blocked("c1") is False      # 但永不因上限被挡
    guard.release("c1")
    assert guard.count("c1") == 0            # 对称释放


def test_guard_negative_limit_normalized_to_unlimited():
    guard = InflightGuard(lambda: -3)
    assert guard.limit == 0
    assert guard.blocked("c1") is False


def test_guard_blocks_at_limit():
    guard = InflightGuard(lambda: 2)
    assert guard.blocked("c1") is False
    guard.acquire("c1")
    assert guard.blocked("c1") is False
    guard.acquire("c1")
    assert guard.blocked("c1") is True       # 达到上限
    assert guard.blocked("c2") is False      # 其它凭证不受影响
    guard.release("c1")
    assert guard.blocked("c1") is False


def test_guard_reads_limit_lazily_for_hot_update():
    limit = {"value": 0}
    guard = InflightGuard(lambda: limit["value"])
    guard.acquire("c1")
    assert guard.count("c1") == 1            # 不限也登记（负载键）
    assert guard.blocked("c1") is False      # 上限 0 = 不限，不挡
    limit["value"] = 1
    assert guard.blocked("c1") is True       # 热更后的上限立即生效


# --------------------------------------------------------------- 执行器集成


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "inflight.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()


class RecordingProvider:
    """按脚本回放，并记下每次调用实际使用的凭证数据。"""

    id = "trae"

    def __init__(self, script: list) -> None:
        self.script = script
        self.calls = 0
        self.credentials: list = []

    async def stream_chat(self, credential_data, payload, model):
        self.credentials.append(credential_data)
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    async def list_models(self, _credential_data):
        return [Model(id="glm-5.2")]

    def import_credential(self, raw):
        if not raw.get("accessToken"):
            raise UpstreamProtocolViolation("credential missing accessToken")
        return dict(raw)

    def classify(self, status, body=b""):
        return ErrKind.OTHER


def _build(credentials, provider, guard):
    return Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", inflight=guard))


def _stream_request():
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                               "model": "glm-5.2", "stream": True})


def _add(credentials, token, nickname):
    return credentials.add(provider="trae", credential_data={"accessToken": token},
                           nickname=nickname)


async def test_executor_inflight_skips_full_credential(repo):
    """占满在途上限的凭证不参与选号，请求落到空闲凭证并在结束后释放租约。"""
    credentials, _db = repo
    busy_id = _add(credentials, "busy", "busy")
    free_id = _add(credentials, "free", "free")

    guard = InflightGuard(lambda: 1)
    guard.acquire(busy_id)                    # 人为占满 busy
    provider = RecordingProvider([GOOD_trae])
    executor = _build(credentials, provider, guard)

    result = await executor.complete(_request())
    assert result["choices"][0]["message"]["content"] == "hi"
    # 实际使用的凭证是空闲的那个（busy 被过滤掉）
    assert provider.credentials == [{"accessToken": "free"}]
    assert guard.count(free_id) == 0          # 请求结束已释放
    assert guard.count(busy_id) == 1          # 预占的租约与本请求无关


async def test_executor_inflight_all_full_raises_no_healthy(repo):
    """所有候选都占满 → 与限额护栏同口径的「无可用凭证」503。"""
    credentials, _db = repo
    only_id = _add(credentials, "only", "only")
    guard = InflightGuard(lambda: 1)
    guard.acquire(only_id)
    executor = _build(credentials, RecordingProvider([GOOD_trae]), guard)

    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())


async def test_executor_inflight_released_on_stream_close(repo):
    """客户端断开（生成器 aclose）走 finally：租约立即释放，不靠 TTL。"""
    credentials, _db = repo
    cid = _add(credentials, "a", "only")
    guard = InflightGuard(lambda: 1)
    executor = _build(credentials, RecordingProvider([GOOD_trae]), guard)

    stream = executor.stream(_stream_request())
    await stream.__anext__()
    assert guard.count(cid) == 1              # 流已开始，租约持有中
    await stream.aclose()
    assert guard.count(cid) == 0              # 断开即释放


async def test_executor_inflight_released_on_error_rotation(repo):
    """失败轮换也释放：第一轮失败后第二轮换号不受上一轮租约影响。"""
    credentials, _db = repo
    first_id = _add(credentials, "first", "first")
    second_id = _add(credentials, "second", "second")

    class Boom(Exception):
        def kind(self):
            return ErrKind.SOFT

    guard = InflightGuard(lambda: 1)
    provider = RecordingProvider([[Boom()], GOOD_trae])
    executor = _build(credentials, provider, guard)

    result = await executor.complete(_request())
    assert result["choices"][0]["message"]["content"] == "hi"
    assert provider.calls == 2
    # 两个凭证的租约都已归还
    assert guard.count(first_id) == 0
    assert guard.count(second_id) == 0


async def test_executor_stream_inflight_skips_full_credential(repo):
    """流式路径同样按在途过滤（两个入口各有一条过滤/登记路径）。"""
    credentials, _db = repo
    busy_id = _add(credentials, "busy", "busy")
    free_id = _add(credentials, "free", "free")

    guard = InflightGuard(lambda: 1)
    guard.acquire(busy_id)
    provider = RecordingProvider([GOOD_trae])
    executor = _build(credentials, provider, guard)

    from src.engine.sse import SSE_DONE

    chunks = [chunk async for chunk in executor.stream(_stream_request())]
    assert chunks[-1] == SSE_DONE
    assert provider.credentials == [{"accessToken": "free"}]
    assert guard.count(free_id) == 0


async def test_executor_concurrent_requests_spread_across_credentials(repo):
    """并发请求摊到空闲凭证（最少连接），而不是全压排序确定的同一个号。

    上限为 0（不限）时旧实现不登记租约 → 负载键恒 0 → 排序退化到最后一键，
    三个并发全落同一凭证。现在在途参与排序，三个并发应命中三个不同凭证。
    """
    credentials, _db = repo
    ids = [_add(credentials, f"tok{i}", f"n{i}") for i in range(3)]
    # 三个号健康度/余额全打平：纯靠负载键分摊
    for cid in ids:
        credentials.save_success(cid)
    guard = InflightGuard(lambda: 0)          # 不限并发，仅用计数做负载键
    provider = RecordingProvider([GOOD_trae])
    executor = _build(credentials, provider, guard)

    streams = [executor.stream(_stream_request()) for _ in range(3)]
    for stream in streams:                    # 三个都启动（各自登记租约）
        await stream.__anext__()
    # 三个并发落到三个不同凭证（tok0/1/2 各一次）
    assert sorted(c["accessToken"] for c in provider.credentials) == ["tok0", "tok1", "tok2"]
    for stream in streams:
        await stream.aclose()
    assert all(guard.count(cid) == 0 for cid in ids)   # 关闭后全部释放


def test_tracker_total_counts_all_and_reclaims_expired():
    """`total()` 汇总全池在途并顺手回收过期租约（`/metrics` 的唯一遍历入口）。"""
    now = {"value": 0.0}
    tracker = InflightTracker(ttl=10.0, now=lambda: now["value"])
    tracker.acquire("a")
    tracker.acquire("a")
    tracker.acquire("b")
    assert tracker.total() == 3

    now["value"] = 20.0                 # 全部过期
    assert tracker.total() == 0
    assert tracker._leases == {}        # 键被删掉，不会无界增长


def test_guard_total_passes_through_to_tracker():
    guard = InflightGuard(lambda: 0)
    guard.acquire("a")
    assert guard.total() == 1
    guard.release("a")
    assert guard.total() == 0
