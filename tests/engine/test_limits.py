"""凭证级限额护栏：判定纯逻辑 + 选号前快照缓存。"""

from __future__ import annotations

import time

from src.engine.limits import (
    CredentialLimits,
    LimitGuard,
    LimitState,
    Usage,
    day_start,
    evaluate,
)


def _candidate(credential_id="cred_1", quota_remaining=None):
    from src.engine.scheduler import Candidate

    return Candidate(credential_id=credential_id, provider="codebuddy",
                     quota_remaining=quota_remaining)


# --------------------------------------------------------------- day_start


def test_day_start_is_local_midnight():
    """当日起点 = 北京日 0 点；同一天任意时刻返回同一个起点。"""
    import datetime as dt

    # 2026-01-02 08:00 北京时间（UTC 00:00）；+5h 仍在同一北京日内
    moment = int(dt.datetime(2026, 1, 2, 0, 0, tzinfo=dt.UTC).timestamp())
    start = day_start(moment)
    assert start == day_start(moment + 5 * 3600)
    # 起点当刻的北京日时分应为 00:00
    local = dt.datetime.fromtimestamp(start, dt.timezone(dt.timedelta(hours=8)))
    assert (local.hour, local.minute, local.second) == (0, 0, 0)


# ---------------------------------------------------------------- evaluate


def test_evaluate_no_limits_never_blocks():
    assert evaluate(None, Usage(tokens=10**9, credits=10**9),
                    quota_remaining=0.0) == LimitState()


def test_evaluate_keep_credits_blocks_at_or_below():
    limits = CredentialLimits(credential_id="c", keep_credits=100.0)
    # 高于保留值：放行
    assert not evaluate(limits, Usage(), quota_remaining=100.01).blocked
    # 正好等于保留值：停用（留底语义，不让余额跌穿）
    assert evaluate(limits, Usage(), quota_remaining=100.0).blocked
    # 余额未知（渠道不上报）不猜，不因此停用
    assert not evaluate(limits, Usage(), quota_remaining=None).blocked


def test_evaluate_daily_tokens_and_credits():
    limits = CredentialLimits(credential_id="c", daily_tokens=1000, daily_credits=5.0)
    assert not evaluate(limits, Usage(tokens=999, credits=4.9),
                        quota_remaining=None).blocked
    assert evaluate(limits, Usage(tokens=1000, credits=0),
                    quota_remaining=None).blocked
    assert evaluate(limits, Usage(tokens=0, credits=5.0),
                    quota_remaining=None).blocked


def test_evaluate_per_model_blocks_only_that_model():
    limits = CredentialLimits(credential_id="c",
                              per_model_tokens={"glm-5": 100, "kimi": 0})
    state = evaluate(limits, Usage(per_model={"glm-5": 100, "kimi": 999}),
                     quota_remaining=None)
    assert state.blocked is False
    # 达到上限的模型停用；上限为 0 的项视为关闭，不因用量大而停用
    assert state.blocked_models == frozenset({"glm-5"})


def test_credential_limits_is_empty_and_as_dict():
    empty = CredentialLimits(credential_id="c")
    assert empty.is_empty() is True
    assert empty.as_dict()["per_model_tokens"] == {}
    # 任一护栏开启即非空
    assert CredentialLimits(credential_id="c", daily_tokens=1).is_empty() is False
    assert CredentialLimits(credential_id="c", keep_credits=0).is_empty() is True
    assert CredentialLimits(credential_id="c",
                            per_model_tokens={"m": 1}).is_empty() is False


# --------------------------------------------------------------- LimitGuard


class _Limits:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = 0

    def map(self):
        self.calls += 1
        return self.mapping


class _Usage:
    def __init__(self, mapping):
        self.mapping = mapping
        self.sinces: list[int] = []

    def __call__(self, since):
        self.sinces.append(since)
        return self.mapping


def test_guard_blocks_only_configured_credentials():
    limits = _Limits({"cred_1": CredentialLimits(credential_id="cred_1",
                                                 daily_tokens=10)})
    usage = _Usage({"cred_1": Usage(tokens=10)})
    guard = LimitGuard(limits, usage)
    assert guard.blocked(_candidate("cred_1"), "glm-5") is True
    # 未配置限额的凭证恒放行
    assert guard.blocked(_candidate("cred_2"), "glm-5") is False
    # 当日用量按北京日 0 点起算
    assert usage.sinces and usage.sinces[0] == day_start(int(time.time()))


def test_guard_skips_usage_query_without_limits():
    limits = _Limits({})
    usage = _Usage({})
    guard = LimitGuard(limits, usage)
    assert guard.blocked(_candidate("cred_1"), "glm-5") is False
    assert usage.sinces == []           # 无任何限额配置 → 不查当日用量


def test_guard_caches_snapshot_and_invalidate():
    limits = _Limits({"cred_1": CredentialLimits(credential_id="cred_1",
                                                 daily_tokens=10)})
    usage = _Usage({"cred_1": Usage(tokens=1)})
    guard = LimitGuard(limits, usage)
    assert guard.blocked(_candidate("cred_1"), "glm-5") is False
    assert guard.blocked(_candidate("cred_1"), "glm-5") is False
    assert limits.calls == 1 and len(usage.sinces) == 1     # 快照复用
    guard.invalidate()
    guard.blocked(_candidate("cred_1"), "glm-5")
    assert limits.calls == 2                                # 失效后重读


def test_guard_snapshot_expires_after_ttl():
    limits = _Limits({"cred_1": CredentialLimits(credential_id="cred_1",
                                                 daily_tokens=10)})
    usage = _Usage({"cred_1": Usage(tokens=1)})
    clock = {"now": 1000.0}
    guard = LimitGuard(limits, usage, ttl=10.0, now_fn=lambda: clock["now"])
    guard.blocked(_candidate("cred_1"), "glm-5")
    clock["now"] += 5
    guard.blocked(_candidate("cred_1"), "glm-5")
    assert limits.calls == 1                                # TTL 内复用
    clock["now"] += 6
    guard.blocked(_candidate("cred_1"), "glm-5")
    assert limits.calls == 2                                # 过期后重读


def test_guard_blocks_per_model():
    limits = _Limits({"cred_1": CredentialLimits(
        credential_id="cred_1", per_model_tokens={"glm-5": 5})})
    usage = _Usage({"cred_1": Usage(per_model={"glm-5": 5})})
    guard = LimitGuard(limits, usage)
    assert guard.blocked(_candidate("cred_1"), "glm-5") is True
    assert guard.blocked(_candidate("cred_1"), "kimi") is False


def test_invalidate_within_bucket_reuses_usage_aggregate():
    """invalidate 后重读限额表，但同一 5 秒桶内的整日聚合只查一次。

    热路径上的整日 GROUP BY 是请求毛刺来源：桶内复用把它从「每次 TTL 过期
    都查」压到「每个桶最多一次」，且不改 `blocked()` 的同步签名。
    """
    limits = _Limits({"cred_1": CredentialLimits(credential_id="cred_1",
                                                 daily_tokens=10)})
    usage = _Usage({"cred_1": Usage(tokens=1)})
    clock = {"now": 1000.0}
    guard = LimitGuard(limits, usage, ttl=0.0, now_fn=lambda: clock["now"])
    guard.blocked(_candidate("cred_1"), "glm-5")
    guard.blocked(_candidate("cred_1"), "glm-5")
    # TTL=0 → 每次都重读限额表；但用量仍在同一桶（1000//5=200）内复用
    assert limits.calls == 2 and len(usage.sinces) == 1
    clock["now"] += 1                      # 1001 仍在同一桶
    guard.blocked(_candidate("cred_1"), "glm-5")
    assert len(usage.sinces) == 1
    clock["now"] += 5                      # 跨桶（1006//5=201）→ 重查一次
    guard.blocked(_candidate("cred_1"), "glm-5")
    assert len(usage.sinces) == 2


def test_usage_bucket_seconds_is_short():
    from src.engine.limits import USAGE_BUCKET_SECONDS

    # 桶过长会让限额判定明显滞后（用量最多晚这么多秒生效）
    assert 0 < USAGE_BUCKET_SECONDS <= 5
