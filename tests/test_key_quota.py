"""Key 级配额：解析/校验、滑动窗口与准入判定（纯逻辑，注入时钟）。"""

from __future__ import annotations

import json

import pytest

from src.engine.limits import day_start
from src.key_quota import (
    KeyQuota,
    KeyQuotaGuard,
    KeyUsage,
    QuotaExceeded,
    load_key_quota,
    parse_key_quota,
    quota_text,
)

# 一个固定的「某天中午」：北京时间 2026-10-18 12:00:00
NOON = 1_792_296_000


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------ 解析


def test_parse_empty_forms_mean_unrestricted():
    for raw in (None, "", {}, ""):
        assert parse_key_quota(raw) == KeyQuota()
        assert parse_key_quota(raw).is_empty() is True


def test_parse_accepts_dict_and_json_text():
    expected = KeyQuota(daily_requests=100, daily_tokens=1000, daily_cost_usd=1.5,
                        rpm=10, tpm=200, max_concurrency=2)
    payload = {"daily_requests": 100, "daily_tokens": 1000, "daily_cost_usd": 1.5,
               "rpm": 10, "tpm": 200, "max_concurrency": 2}
    assert parse_key_quota(payload) == expected
    assert parse_key_quota(json.dumps(payload)) == expected
    # 整数项接受整值浮点（前端 input 可能给 10.0）
    assert parse_key_quota({"rpm": 10.0}).rpm == 10


def test_parse_rejects_unknown_key():
    with pytest.raises(ValueError, match="未知配额项"):
        parse_key_quota({"daily_request": 1})


def test_parse_rejects_non_object_and_bad_json():
    with pytest.raises(ValueError, match="JSON 对象"):
        parse_key_quota("[1,2]")
    with pytest.raises(ValueError, match="JSON 对象"):
        parse_key_quota("not json")


@pytest.mark.parametrize("value", [-1, -0.5])
def test_parse_rejects_negative(value):
    with pytest.raises(ValueError, match="不能为负数"):
        parse_key_quota({"rpm": value})


@pytest.mark.parametrize("value", ["10", True, None, [1]])
def test_parse_rejects_non_numeric(value):
    with pytest.raises(ValueError, match="必须是数字"):
        parse_key_quota({"rpm": value})


def test_parse_rejects_inf_and_fraction_for_int_fields():
    with pytest.raises(ValueError, match="有限数字"):
        parse_key_quota({"rpm": float("inf")})
    with pytest.raises(ValueError, match="必须是整数"):
        parse_key_quota({"daily_requests": 1.5})


def test_parse_zero_values_are_off():
    quota = parse_key_quota({"daily_requests": 0, "rpm": 0})
    assert quota.is_empty() is True
    assert quota.as_dict() == {"daily_requests": 0, "daily_tokens": 0,
                              "daily_cost_usd": 0, "rpm": 0, "tpm": 0,
                              "max_concurrency": 0}


def test_load_key_quota_tolerates_dirty_text(caplog):
    """读路径宽容：脏配置退化为「不限制」，并留下 warning。"""
    with caplog.at_level("WARNING"):
        assert load_key_quota("{bad json").is_empty() is True
    assert "无法解析" in caplog.text
    # 合法文本照常解析
    assert load_key_quota('{"rpm": 3}').rpm == 3


def test_quota_text_round_trips_and_drops_zeros():
    quota = KeyQuota(daily_tokens=1000, rpm=5)
    text = quota_text(quota)
    assert json.loads(text) == {"daily_tokens": 1000, "rpm": 5}
    assert parse_key_quota(text) == quota
    # 全关 → 空串（列默认值 = 不受限）
    assert quota_text(KeyQuota()) == ""


# ------------------------------------------------------- 滑动窗口与并发


def test_guard_skips_everything_when_quota_empty():
    guard = KeyQuotaGuard(now_fn=_Clock())
    lease = guard.admit("k1", KeyQuota())
    assert lease.counted is False
    assert guard.inflight("k1") == 0
    assert guard.window_usage("k1") == (0, 0)


def test_rpm_limit_and_window_slide():
    clock = _Clock()
    guard = KeyQuotaGuard(now_fn=clock)
    quota = KeyQuota(rpm=2)
    first = guard.admit("k1", quota)
    guard.admit("k1", quota)
    with pytest.raises(QuotaExceeded) as info:
        guard.admit("k1", quota)
    # 窗口最早一条在 now+60 才滑出 → Retry-After = 60
    assert info.value.retry_after == 60
    assert "requests per minute" in str(info.value)
    # 另一把 Key 独立计数
    guard.admit("k2", quota)
    first.release()
    clock.now += 60
    guard.admit("k1", quota)          # 窗口已滑过，重新放行


def test_rpm_retry_after_rounds_up_to_earliest_slide():
    clock = _Clock()
    guard = KeyQuotaGuard(now_fn=clock)
    quota = KeyQuota(rpm=1)
    guard.admit("k1", quota)
    clock.now += 10.5
    with pytest.raises(QuotaExceeded) as info:
        guard.admit("k1", quota)
    assert info.value.retry_after == 50      # ceil(60 - 10.5)


def test_tpm_uses_estimated_tokens_and_accumulates():
    clock = _Clock()
    guard = KeyQuotaGuard(now_fn=clock)
    quota = KeyQuota(tpm=100)
    guard.admit("k1", quota, tokens=60)
    with pytest.raises(QuotaExceeded) as info:
        guard.admit("k1", quota, tokens=50)
    assert "tokens per minute" in str(info.value)
    assert info.value.retry_after == 60
    # 恰好用满不超（> 而不是 >=）：再来 0 token 仍可
    guard.admit("k1", quota, tokens=40)


def test_max_concurrency_counts_inflight_and_releases_idempotently():
    guard = KeyQuotaGuard(now_fn=_Clock())
    quota = KeyQuota(max_concurrency=1)
    lease = guard.admit("k1", quota)
    assert guard.inflight("k1") == 1
    with pytest.raises(QuotaExceeded) as info:
        guard.admit("k1", quota)
    assert "concurrent" in str(info.value)
    assert info.value.retry_after == 1
    lease.release()
    lease.release()                   # 幂等：重复释放不再减
    assert guard.inflight("k1") == 0
    guard.admit("k1", quota)


def test_release_never_goes_negative():
    guard = KeyQuotaGuard(now_fn=_Clock())
    guard.release("ghost")
    assert guard.inflight("ghost") == 0


def test_window_entries_are_pruned_to_avoid_unbounded_growth():
    clock = _Clock()
    guard = KeyQuotaGuard(now_fn=clock)
    guard.admit("k1", KeyQuota(rpm=100), tokens=5)
    clock.now += 61
    assert guard.window_usage("k1") == (0, 0)
    assert guard._events == {}        # noqa: SLF001 - 断言内部结构已回收


def test_window_retry_without_events_falls_back_to_one():
    """窗口内已无事件（理论上不会走到）时给最小退避 1 秒。"""
    from src.key_quota import _window_retry

    assert _window_retry(__import__("collections").deque(), 100.0, 60.0) == 1


# ------------------------------------------------------------ 当日配额


def _usage_source(usage: KeyUsage):
    def source(key_id: str, *, since: int) -> KeyUsage:
        assert key_id == "k1"
        return usage
    return source


def test_daily_requests_limit_uses_cst_day_and_retry_to_midnight():
    guard = KeyQuotaGuard(_usage_source(KeyUsage(requests=10)), now_fn=lambda: NOON)
    with pytest.raises(QuotaExceeded) as info:
        guard.admit("k1", KeyQuota(daily_requests=10))
    assert "requests per day" in str(info.value)
    # 北京时间 12:00 → 距下一个 0 点 12 小时
    assert info.value.retry_after == 12 * 3600
    # 断言 NOON 确实是北京时间正午（0 点 + 12h）
    assert day_start(NOON) + 12 * 3600 == NOON


def test_daily_tokens_and_cost_limits():
    guard = KeyQuotaGuard(_usage_source(KeyUsage(input_tokens=60, output_tokens=50,
                                                 cost_usd=1.25)),
                          now_fn=lambda: NOON)
    with pytest.raises(QuotaExceeded, match="tokens per day"):
        guard.admit("k1", KeyQuota(daily_tokens=110))
    with pytest.raises(QuotaExceeded, match="per day"):
        guard.admit("k1", KeyQuota(daily_cost_usd=1.25))


def test_daily_limits_pass_when_under_quota():
    guard = KeyQuotaGuard(_usage_source(KeyUsage(requests=1)), now_fn=lambda: NOON)
    lease = guard.admit("k1", KeyQuota(daily_requests=10))
    assert lease.counted is True


def test_daily_cost_blocks_when_unknown_cost_requests_present():
    """无价请求 → 成本配额 fail closed（否则配额永不触发）。

    价表无该模型时 `cost_usd` 落库为 NULL，按 0 累加会让「已知成本」永远
    低于上限；未知成本不能猜，只能阻断并说明原因。
    """
    usage = KeyUsage(requests=3, input_tokens=100, output_tokens=50,
                     cost_usd=0.01, cost_known_requests=2,
                     cost_unknown_requests=1)
    guard = KeyQuotaGuard(_usage_source(usage), now_fn=lambda: NOON)
    with pytest.raises(QuotaExceeded) as info:
        guard.admit("k1", KeyQuota(daily_cost_usd=10.0))
    message = str(info.value)
    assert "无法定价" in message and "成本配额无法精确判定" in message
    # 与「已知成本超限」文案可辨：带上括号说明，不冒充金额超限
    assert "per day（" in message


def test_daily_cost_blocks_on_known_amount_only():
    """全部有价：按金额阻断，文案不带「无法定价」。"""
    usage = KeyUsage(requests=2, cost_usd=2.5,
                     cost_known_requests=2, cost_unknown_requests=0)
    guard = KeyQuotaGuard(_usage_source(usage), now_fn=lambda: NOON)
    with pytest.raises(QuotaExceeded) as info:
        guard.admit("k1", KeyQuota(daily_cost_usd=2.0))
    assert "无法定价" not in str(info.value)
    assert "$2 per day" in str(info.value)
    # 未超：放行
    assert guard.admit("k1", KeyQuota(daily_cost_usd=3.0)).counted is True


def test_daily_cost_unconfigured_ignores_unknown_cost_requests():
    """`daily_cost_usd=0`（未配置）：无价请求不影响任何判定。"""
    usage = KeyUsage(requests=1, cost_usd=0.0, cost_unknown_requests=99)
    guard = KeyQuotaGuard(_usage_source(usage), now_fn=lambda: NOON)
    assert guard.admit("k1", KeyQuota(daily_requests=10)).counted is True
    # 全关（不限制）：直接放行，不查聚合也不受无价请求影响
    assert guard.admit("k1", KeyQuota()).counted is False



def test_daily_check_skipped_when_no_usage_source():
    """未装配聚合源（纯内存场景）：当日类限额无从判定，不因此拒绝。"""
    guard = KeyQuotaGuard(now_fn=lambda: NOON)
    assert guard.admit("k1", KeyQuota(daily_requests=1)).counted is True


def test_only_rate_limits_do_not_query_usage_source():
    calls: list[str] = []

    def source(key_id: str, *, since: int) -> KeyUsage:
        calls.append(key_id)
        return KeyUsage()

    guard = KeyQuotaGuard(source, now_fn=lambda: NOON)
    guard.admit("k1", KeyQuota(rpm=5))
    assert calls == []


def test_admit_swallows_unexpected_errors_and_allows(caplog):
    """判定异常必须放行（配额是治理不是安全边界），并留下 warning。"""

    def boom(key_id: str, *, since: int) -> KeyUsage:
        raise RuntimeError("db is gone")

    guard = KeyQuotaGuard(boom, now_fn=lambda: NOON)
    with caplog.at_level("WARNING"):
        lease = guard.admit("k1", KeyQuota(daily_requests=1))
    assert lease.counted is False
    assert "配额判定失败" in caplog.text


def test_lease_release_is_noop_when_not_counted():
    guard = KeyQuotaGuard(now_fn=_Clock())
    lease = guard.admit("k1", KeyQuota())
    lease.release()
    assert guard.inflight("k1") == 0
    assert lease.released is True
