"""冷却到期半开探测。

- 冷却**已过期**（库里那行还没被留存任务清掉）≠ 立刻全量回池：先放 1 个在途；
- 探测位被占时换别的号；没有别的号 → 无可用（调用方 503/重试）；
- 探通（`confirm`）后在 `RECOVERED_TTL_SECONDS` 内正常放行；
- 失败/中断只归还探测位，不标记恢复；再记一次错误则清掉恢复结论；
- 模型级冷却按 `(凭证, 上游模型名)` 独立探测，与账号级互不阻挡；
- 探测位有 TTL 兜底，漏释放也不会把号永久卡住。
"""

from __future__ import annotations

import time

import pytest

from src.engine.executor import Executor, ExecutorDeps
from src.engine.half_open import HalfOpenGuard
from src.engine.model_resolver import resolve
from src.engine.scheduler import ErrorOutcome, Scheduler
from tests.helpers.events import GOOD

_TARGET = resolve("glm-5.2@trae", "glm-5.2")


class _Provider:
    id = "trae"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        self.calls += 1
        for event in GOOD:
            yield event


def _executor(credentials, guard=None, **deps) -> Executor:
    """默认装配 `guard`；`deps` 里显式给 `half_open` 时以它为准。"""
    deps.setdefault("half_open", guard)
    return Executor(ExecutorDeps(providers={"trae": _Provider()},
                                 credentials=credentials,
                                 scheduler=Scheduler(), default_model="glm-5.2",
                                 **deps))


def _expired_account_cooling(credentials, credential_id: str) -> None:
    """把账号级冷却写在**过去**（模拟「刚到期、留存任务还没清」）。"""
    credentials.save_error(
        credential_id, ErrorOutcome(cooling_until=int(time.time()) - 1))


def _expired_model_cooling(credentials, credential_id: str, model: str) -> None:
    from src.engine.scheduler import ModelCooldown

    credentials.save_error(
        credential_id,
        ErrorOutcome(model_cooldowns={
            model: ModelCooldown(cooling_until=int(time.time()) - 1, hits=1)}))


# ------------------------------------------------------------------ guard 单元


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_guard_single_probe_and_release():
    clock = _FakeClock()
    guard = HalfOpenGuard(now=clock)
    key = ("c1", "")
    assert guard.acquire(key) is True
    assert guard.acquired(key) is True
    assert guard.acquire(key) is False
    guard.release(key)
    assert guard.acquired(key) is False
    assert guard.recovered(key) is False
    assert guard.acquire(key) is True


def test_guard_confirm_marks_recovered_with_ttl():
    clock = _FakeClock()
    guard = HalfOpenGuard(now=clock, recovered_ttl=30)
    key = ("c1", "glm-5.2")
    assert guard.acquire(key) is True
    guard.confirm(key)
    assert guard.recovered(key) is True
    assert guard.acquired(key) is False
    clock.now += 29
    assert guard.recovered(key) is True
    clock.now += 1
    assert guard.recovered(key) is False


def test_guard_reset_clears_recovered():
    guard = HalfOpenGuard(now=_FakeClock())
    key = ("c1", "")
    guard.acquire(key)
    guard.confirm(key)
    guard.reset("c1", "")
    assert guard.recovered(key) is False


def test_guard_probe_ttl_recovers_leaked_slot():
    clock = _FakeClock()
    guard = HalfOpenGuard(now=clock, ttl=10)
    key = ("c1", "")
    assert guard.acquire(key) is True
    clock.now += 10
    assert guard.acquired(key) is False
    assert guard.acquire(key) is True


def test_guard_recovered_missing_and_other_scope():
    guard = HalfOpenGuard(now=_FakeClock())
    assert guard.recovered(("nope", "")) is False
    guard.acquire(("c1", ""))
    guard.confirm(("c1", ""))
    # 另一个作用域（同凭证的另一个模型）不受影响
    assert guard.recovered(("c1", "other")) is False
    assert guard.acquire(("c1", "other")) is True


# --------------------------------------------------------------- executor 集成


def test_expired_cooling_is_probed_one_at_a_time(repo):
    """两个号都刚解冻：第一个请求占探测位，第二个请求换到另一个号。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    b = credentials.add(provider="trae", credential_data={"accessToken": "b"})
    _expired_account_cooling(credentials, a)
    _expired_account_cooling(credentials, b)
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)

    first = executor._select(_TARGET, set())
    assert first in (a, b)
    second = executor._select(_TARGET, set())
    assert second in (a, b) and second != first
    # 两个号的探测位都占满 → 无可用
    assert executor._select(_TARGET, set()) is None
    db.close()


def test_only_candidate_in_probe_yields_none(repo):
    """唯一候选在探测中：直接判定无可用，不放弃半开去莽撞。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _expired_account_cooling(credentials, a)
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)

    assert executor._select(_TARGET, set()) == a
    assert executor._select(_TARGET, set()) is None
    db.close()


def test_probe_success_allows_full_pool(repo):
    """探通后（`_confirm_probe`）恢复期间直接放行，不再逐个排队。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _expired_account_cooling(credentials, a)
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)

    assert executor._select(_TARGET, set()) == a
    executor._lease(a)
    executor._confirm_probe(a)
    executor._release(a)
    # 已探通：没有探测位也能再选
    assert executor._select(_TARGET, set()) == a
    assert executor._select(_TARGET, set()) == a
    db.close()


def test_probe_failure_releases_slot_for_next_round(repo):
    """探测失败（只 `_release` 不 confirm）：探测位归还，下次仍能再探。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _expired_account_cooling(credentials, a)
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)

    assert executor._select(_TARGET, set()) == a
    executor._lease(a)
    executor._release(a)
    assert executor._select(_TARGET, set()) == a
    db.close()


def test_model_level_probe_uses_upstream_model_scope(repo):
    """模型级冷却同样半开，作用域是上游原始模型名。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _expired_model_cooling(credentials, a, "glm-5.2")
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)

    assert executor._select(_TARGET, set()) == a
    assert guard.acquired((a, "glm-5.2")) is True
    executor._lease(a)
    executor._release(a)
    assert guard.acquired((a, "glm-5.2")) is False
    db.close()


def test_account_cooldown_takes_precedence_over_model(repo):
    """账号级与模型级都过期：按账号级探测键占位。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _expired_account_cooling(credentials, a)
    _expired_model_cooling(credentials, a, "glm-5.2")
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)

    assert executor._select(_TARGET, set()) == a
    assert guard.acquired((a, "")) is True
    db.close()


def test_no_probe_when_cooling_is_in_the_future(repo):
    """仍在冷却中的号照旧被滤掉，与历史行为一致。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.save_error(
        a, ErrorOutcome(cooling_until=int(time.time()) + 60))
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)

    assert executor._select(_TARGET, set()) is None
    db.close()


def test_drop_probe_returns_slot(repo):
    """选中的号在使用前被淘汰：探测位立即归还，不卡住后续请求。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _expired_account_cooling(credentials, a)
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)

    assert executor._select(_TARGET, set()) == a
    executor._drop_probe(a)
    assert executor._select(_TARGET, set()) == a
    db.close()


def test_drop_probe_is_noop_without_pending(repo):
    """没有待登记的探测位时调用是幂等的（并发删除路径可能重复触发）。"""
    credentials, db = repo
    executor = _executor(credentials, half_open=None)
    executor._drop_probe("missing")
    executor._confirm_probe(None)
    executor._release("missing")
    db.close()


def test_new_error_resets_recovered_conclusion(repo):
    """探通后又报错：清掉「已恢复」，下次到期重新半开（不永久免检）。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _expired_account_cooling(credentials, a)
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)

    assert executor._select(_TARGET, set()) == a
    executor._lease(a)
    executor._confirm_probe(a)
    executor._release(a)
    assert guard.recovered((a, "")) is True

    from src.provider.base import ErrKind

    executor._note_upstream_error(a, ErrKind.SOFT, "trae", "glm-5.2",
                                  username="u", started=time.time())
    assert guard.recovered((a, "")) is False
    db.close()


def test_queue_error_keeps_recovered_conclusion(repo):
    """排队不是凭证故障：不重置「已恢复」结论。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)
    guard.acquire((a, ""))
    guard.confirm((a, ""))

    from src.provider.base import ErrKind

    executor._note_upstream_error(a, ErrKind.QUEUE, "trae", "glm-5.2",
                                  username="u", started=time.time())
    assert guard.recovered((a, "")) is True
    db.close()


async def test_request_error_skips_reset(repo):
    """请求级错误（REQUEST）零动作：不冷却、也不重置半开结论。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    guard = HalfOpenGuard()
    executor = _executor(credentials, guard)
    guard.acquire((a, ""))
    guard.confirm((a, ""))

    from src.provider.base import ErrKind

    assert executor._note_upstream_error(
        a, ErrKind.REQUEST, "trae", "glm-5.2", username="u",
        started=time.time()) is False
    assert guard.recovered((a, "")) is True
    db.close()


@pytest.mark.parametrize("half_open", [None])
def test_guard_disabled_keeps_old_behavior(repo, half_open):
    """未装配 guard：冷却一到期就照旧全量回池（向后兼容）。"""
    credentials, db = repo
    a = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _expired_account_cooling(credentials, a)
    executor = _executor(credentials, half_open=half_open)

    assert executor._select(_TARGET, set()) == a
    assert executor._select(_TARGET, set()) == a
    db.close()
