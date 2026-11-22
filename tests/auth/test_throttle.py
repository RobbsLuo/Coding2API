"""登录限流：回环豁免只针对本机直连，带 XFF 时必须正常计数。

docker-compose 端口绑 127.0.0.1、TRUST_PROXY 默认 false 时，经 nginx/frp/
Caddy 回源的 peer 恒为 127.0.0.1；若无条件豁免回环，IP 窗口完全失效，
只剩用户名窗口（5/60s），攻击者可跨用户名撞库。
"""

from __future__ import annotations

import pytest

from src.auth.throttle import LoginThrottle, ThrottledError, ThrottleLimits


def test_loopback_without_forwarded_stays_exempt():
    """本机直连（无 XFF）：回环桶再满也不限流。"""
    throttle = LoginThrottle(ThrottleLimits(max_per_ip=1, max_per_user=100))
    for _ in range(5):
        throttle.record_failure(ip="127.0.0.1", username="x")
    throttle.check_transport_windows(ip="127.0.0.1")          # 不抛
    throttle.check(ip="127.0.0.1", username="other")          # 用户名窗口也干净


def test_loopback_with_forwarded_counts_and_blocks():
    """带 XFF：说明前面有反代，回环 peer 代表不了来源，IP 窗口必须生效。"""
    throttle = LoginThrottle(ThrottleLimits(max_per_ip=2, max_per_user=100))
    for _ in range(2):
        throttle.record_failure(ip="127.0.0.1", username="a")
    with pytest.raises(ThrottledError):
        throttle.check_transport_windows(ip="127.0.0.1", forwarded=True)
    with pytest.raises(ThrottledError):
        throttle.check(ip="127.0.0.1", username="b", forwarded=True)
    # 未超限时放行（forwarded 只影响回环豁免，不额外收紧）
    fresh = LoginThrottle(ThrottleLimits(max_per_ip=2, max_per_user=100))
    fresh.record_failure(ip="::1", username="a")
    fresh.check_transport_windows(ip="::1", forwarded=True)
    fresh.check(ip="::1", username="b", forwarded=True)


def test_non_loopback_unaffected_by_forwarded_flag():
    """非回环地址：两个入口、有无 forwarded 的口径完全一致。"""
    for forwarded in (False, True):
        throttle = LoginThrottle(ThrottleLimits(max_per_ip=1, max_per_user=100))
        throttle.record_failure(ip="9.9.9.9", username="a")
        with pytest.raises(ThrottledError):
            throttle.check_transport_windows(ip="9.9.9.9", forwarded=forwarded)
        with pytest.raises(ThrottledError):
            throttle.check(ip="9.9.9.9", username="b", forwarded=forwarded)


def test_at_limit_uses_same_loopback_exemption():
    """淘汰候选判定与拒绝判定同口径：回环带 forwarded 才算已限流。"""
    throttle = LoginThrottle(ThrottleLimits(max_per_ip=1, max_per_user=100))
    throttle.record_failure(ip="127.0.0.1", username="a")
    # 默认（本机直连）不算已限流
    assert throttle._at_limit(("i", "127.0.0.1")) is False
    assert throttle._at_limit(("i", "127.0.0.1"), forwarded=True) is True
    # 全局键与用户名键不受影响
    throttle.record_failure(ip="127.0.0.1", username="b")
    assert throttle._at_limit(("g", "")) is False
    assert throttle._at_limit(("u", "nobody")) is False
