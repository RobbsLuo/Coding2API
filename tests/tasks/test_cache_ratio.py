"""缓存命中率校准任务：用余额差反推 TRAE 实际缓存命中率并回写运行时配置。

这一组守着几条容易退化的语义：
1. 反解公式与口径（整窗合并、正负 delta 的取舍、未收录模型、最小 token）；
2. 只有漂移足够才写，且异常向上抛（不静默吞掉写坏的比例）；
3. 样本不足 / 非 TRAE 部署不写配置、报告如实说明。

**为什么按整窗合并而不是按天**：余额差是两次探测之间的净变化，与 UTC 天边界
无关；按天切会把消费与 token 分到不同天（生产实测按天 0.934、整窗 0.979）。
"""

from __future__ import annotations

import pytest

from src.tasks.cache_ratio import CacheRatioTask
from tests.helpers.db import credential_repo, insert_usage_event

# deepseek-v4.1-flash 的实测有效价（积分 / 百万）：(输入, 输出, 缓存)
DEEPSEEK = "deepseek-v4.1-flash"
IN_PRICE, OUT_PRICE, CACHE_PRICE = 28.0, 112.0, 2.8

DAY = 86_400
NOW = 1_764_000_000            # 固定时钟：窗口可复现


class FakeRuntime:
    """最小运行时配置替身：只提供本任务读写的那两项。"""

    def __init__(self, ratio: float = 0.95) -> None:
        self.trae_assumed_cache_ratio = ratio
        self.sets: list[tuple[str, object]] = []
        self.error: Exception | None = None

    def set(self, key: str, value: object) -> None:  # 由任务在漂移足够时调用
        if self.error is not None:
            raise self.error
        self.sets.append((key, value))
        if key == "trae_assumed_cache_ratio":
            self.trae_assumed_cache_ratio = float(value)


def _credit(db, credential_id: str, *, ts: int, delta: float | None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO credit_events (id, credential_id, ts, window_start, before,"
            " after, delta, source, ledger) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"credit_{credential_id}_{ts}_{delta}", credential_id, ts, None,
             None, None, delta, "observed", "pool"))


def _usage(db, credential_id: str, *, ts: int, model: str = DEEPSEEK,
           input_tokens: int = 1_000_000, output_tokens: int = 0,
           provider: str = "trae", ok: int = 1) -> None:
    insert_usage_event(db, id=f"evt_{credential_id}_{ts}_{model}_{input_tokens}",
                       ts=ts, provider=provider, model=model,
                       credential_id=credential_id, input_tokens=input_tokens,
                       output_tokens=output_tokens, ok=ok)


def _spend_for_ratio(ratio: float, input_tokens: int = 1_000_000) -> float:
    """给定命中率，算 1M 输入 / 0 输出应付的积分（全价 − 命中省下的钱）。"""
    full = input_tokens * IN_PRICE / 1_000_000
    denom = input_tokens * (IN_PRICE - CACHE_PRICE) / 1_000_000
    return full - ratio * denom


def _task(db, runtime, *, window_days: int = 7,
          min_input_tokens: int = 1_000_000) -> CacheRatioTask:
    return CacheRatioTask(db, runtime, now=lambda: NOW,
                          window_days=window_days,
                          min_input_tokens=min_input_tokens)


@pytest.fixture()
def env(tmp_path):
    """(凭证 id, Database, FakeRuntime)，一条 TRAE 凭证。"""
    credentials, db = credential_repo(tmp_path)
    credential_id = credentials.add(provider="trae", credential_data={})
    try:
        yield credential_id, db, FakeRuntime()
    finally:
        db.close()


# ------------------------------------------------------------------ 反解与写回

def test_writes_ratio_when_drift_is_large(env):
    """整窗反解 0.80，与默认 0.95 差 0.15 → 回写。"""
    credential_id, db, runtime = env
    _usage(db, credential_id, ts=NOW - DAY)
    _credit(db, credential_id, ts=NOW - DAY, delta=-_spend_for_ratio(0.8))

    report = _task(db, runtime).run_once()

    assert runtime.sets == [("trae_assumed_cache_ratio", 0.8)]
    assert report["applied"] is True
    assert report["ratio"] == 0.8
    assert report["samples"] == 1
    assert report["window_days"] == 7


def test_does_not_write_within_threshold(env):
    """反解 0.951 与当前 0.95 差 0.001 < 0.01 → 不写，但报告仍带比例。"""
    credential_id, db, runtime = env
    _usage(db, credential_id, ts=NOW - DAY)
    _credit(db, credential_id, ts=NOW - DAY, delta=-_spend_for_ratio(0.951))

    report = _task(db, runtime).run_once()

    assert runtime.sets == []
    assert report == {"applied": False, "ratio": 0.951, "samples": 1,
                      "window_days": 7}


def test_insufficient_volume_does_not_write(env):
    """输入 token 低于最小样本量 → 「样本不足」，不写配置。"""
    credential_id, db, runtime = env
    _usage(db, credential_id, ts=NOW - DAY, input_tokens=1000)
    _credit(db, credential_id, ts=NOW - DAY, delta=-0.02)

    report = _task(db, runtime).run_once()

    assert runtime.sets == []
    assert report["applied"] is False and report["ratio"] is None
    assert report["samples"] == 1 and report["reason"] == "样本不足"


def test_zero_spend_does_not_write(env):
    """只有 sync 基线行（delta 为 NULL）→ 消费 0，凑不出反解值。"""
    credential_id, db, runtime = env
    _usage(db, credential_id, ts=NOW - DAY)
    _credit(db, credential_id, ts=NOW - DAY, delta=None)

    report = _task(db, runtime).run_once()

    assert report["samples"] == 1 and report["reason"] == "样本不足"
    assert runtime.sets == []


def test_unknown_model_prices_are_skipped(env):
    """未收录模型算不出全价 / 分母 → 不计入，窗口无有效样本。"""
    credential_id, db, runtime = env
    _usage(db, credential_id, ts=NOW - DAY, model="not-a-real-model")
    _credit(db, credential_id, ts=NOW - DAY, delta=-10.0)

    report = _task(db, runtime).run_once()

    assert report["samples"] == 1 and report["ratio"] is None
    assert runtime.sets == []


def test_positive_credit_deltas_are_rewards_not_spend(env):
    """正向 delta 是签到 / 奖励，不参与消费口径：加一条正向行不影响反解值。"""
    credential_id, db, runtime = env
    ts = NOW - DAY
    _usage(db, credential_id, ts=ts)
    _credit(db, credential_id, ts=ts, delta=-_spend_for_ratio(0.5))
    # 同一窗口还有 +30 奖励：若被当成负消费，r 会被算成负数 / 越界丢弃
    _credit(db, credential_id, ts=ts, delta=30.0)

    report = _task(db, runtime).run_once()

    assert report["ratio"] == 0.5
    assert runtime.sets == [("trae_assumed_cache_ratio", 0.5)]


def test_out_of_range_ratio_is_dropped(env):
    """消费低于纯缓存价 → 反解 r > 1（价格表或窗口错位），不写。"""
    credential_id, db, runtime = env
    _usage(db, credential_id, ts=NOW - DAY)
    # 1.0 积分远低于命中率 1.0 时的 2.8 积分：r ≈ 1.07
    _credit(db, credential_id, ts=NOW - DAY, delta=-1.0)

    report = _task(db, runtime).run_once()

    assert report["ratio"] is None and runtime.sets == []


def test_tiny_float_overshoot_is_clamped(env):
    """r 略低于 0（浮点误差）→ 钳到 0 而不是丢弃。"""
    credential_id, db, runtime = env
    full = _spend_for_ratio(0.0)
    _usage(db, credential_id, ts=NOW - DAY)
    _credit(db, credential_id, ts=NOW - DAY, delta=-(full + 1e-12))

    report = _task(db, runtime).run_once()

    assert report["ratio"] == 0.0
    assert runtime.sets == [("trae_assumed_cache_ratio", 0.0)]


def test_tiny_float_overshoot_above_one_is_clamped(env):
    """r 略高于 1（浮点误差）→ 钳到 1 而不是丢弃。"""
    credential_id, db, runtime = env
    denom = 1_000_000 * (IN_PRICE - CACHE_PRICE) / 1_000_000
    spend = 1_000_000 * IN_PRICE / 1_000_000 - denom - 1e-12
    _usage(db, credential_id, ts=NOW - DAY)
    _credit(db, credential_id, ts=NOW - DAY, delta=-spend)

    report = _task(db, runtime).run_once()

    assert report["ratio"] == 1.0
    assert runtime.sets == [("trae_assumed_cache_ratio", 1.0)]


def test_window_excludes_old_rows(env):
    """窗口外的 usage / credit 都不参与：只剩窗口内的 1M 输入 → 反解该窗口。"""
    credential_id, db, runtime = env
    old = NOW - 30 * DAY
    _usage(db, credential_id, ts=old)
    _credit(db, credential_id, ts=old, delta=-_spend_for_ratio(0.1))
    _usage(db, credential_id, ts=NOW - DAY)
    _credit(db, credential_id, ts=NOW - DAY, delta=-_spend_for_ratio(0.5))

    report = _task(db, runtime, window_days=7).run_once()

    assert report["ratio"] == 0.5 and report["samples"] == 1


def test_non_trae_deployments_produce_no_samples(env):
    """非 TRAE-only 部署：CodeBuddy 的用量与流水都不参与，恒不写。"""
    _credential_id, db, runtime = env
    cb_credential = "cred_cb"
    _usage(db, cb_credential, ts=NOW - DAY, provider="codebuddy")
    _credit(db, cb_credential, ts=NOW - DAY, delta=-_spend_for_ratio(0.4))

    report = _task(db, runtime).run_once()

    assert report["samples"] == 0 and runtime.sets == []
    assert report["applied"] is False


def test_failed_requests_are_excluded_from_usage(env):
    """失败请求不产生消费，计入会把分母做大、命中率算低。"""
    credential_id, db, runtime = env
    ts = NOW - DAY
    _usage(db, credential_id, ts=ts, ok=1)
    _usage(db, credential_id, ts=ts, ok=0, model=DEEPSEEK, input_tokens=5_000_000)
    _credit(db, credential_id, ts=ts, delta=-_spend_for_ratio(0.5))

    report = _task(db, runtime).run_once()

    assert report["ratio"] == 0.5


def test_multiple_models_combine(env):
    """同窗口多模型：全价与分母各自累加，输出 token 只进全价不进分母。"""
    credential_id, db, runtime = env
    ts = NOW - DAY
    _usage(db, credential_id, ts=ts, model=DEEPSEEK, input_tokens=1_000_000,
           output_tokens=1_000_000)
    # 同窗口第二模型（glm-5.2 折扣价 216 积分/M 输入、54 缓存价）
    _usage(db, credential_id, ts=ts, model="glm-5.2", input_tokens=1_000_000)
    full = 28.0 + 112.0 + 216.0
    denom = (28.0 - 2.8) + (216.0 - 54.0)
    _credit(db, credential_id, ts=ts, delta=-(full - 0.6 * denom))

    report = _task(db, runtime).run_once()

    assert report["ratio"] == 0.6


def test_multiple_credentials_merge_by_tokens(env):
    """多凭证合并反解：token 与消费各自相加，等价于按 token 加权平均。"""
    first, db, runtime = env
    second = "cred_second"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO credentials (id, provider, data_enc, created_at)"
            " VALUES (?, 'trae', '', 0)", (second,))
    _usage(db, first, ts=NOW - DAY, input_tokens=3_000_000)
    _credit(db, first, ts=NOW - DAY, delta=-_spend_for_ratio(0.9, 3_000_000))
    _usage(db, second, ts=NOW - DAY, input_tokens=1_000_000)
    _credit(db, second, ts=NOW - DAY, delta=-_spend_for_ratio(0.5, 1_000_000))

    report = _task(db, runtime).run_once()

    assert report["samples"] == 2
    assert report["ratio"] == pytest.approx(0.8, abs=1e-3)   # (3×0.9 + 1×0.5) / 4


def test_runtime_set_errors_propagate(env):
    """回写失败必须向上抛（由 TaskRunner 记任务失败），不静默吞掉。"""
    credential_id, db, runtime = env
    runtime.error = RuntimeError("db is locked")
    _usage(db, credential_id, ts=NOW - DAY)
    _credit(db, credential_id, ts=NOW - DAY, delta=-_spend_for_ratio(0.5))

    with pytest.raises(RuntimeError, match="db is locked"):
        _task(db, runtime).run_once()
