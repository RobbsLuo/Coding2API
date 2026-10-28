"""成功请求的用量 ↔ 正文体量对账（上游静默替换/截断的观测信号）。"""

from __future__ import annotations

import logging

import pytest

from src.engine.executor import USAGE_MIN_CHARS, USAGE_UNDERREPORT_RATIO, Executor, ExecutorDeps
from src.engine.scheduler import Scheduler


def _executor() -> Executor:
    return Executor(ExecutorDeps(
        providers={}, credentials=object(), scheduler=Scheduler(),
        default_model="m"))


def _call(executor: Executor, chars: int, tokens: int | None):
    executor._check_usage_consistency(
        provider_id="trae", credential_id="c1", model="glm-5.2",
        content_chars=chars, output_tokens=tokens)


def test_short_content_is_ignored(caplog):
    """正文太短不判定：短回复的 token/字符比噪声极大。"""
    with caplog.at_level(logging.WARNING):
        _call(_executor(), USAGE_MIN_CHARS - 1, None)
    assert caplog.records == []


def test_missing_output_tokens_warns(caplog):
    with caplog.at_level(logging.WARNING):
        _call(_executor(), 500, None)
    assert any("未上报输出 token" in r.getMessage() for r in caplog.records)


def test_zero_output_tokens_warns(caplog):
    with caplog.at_level(logging.WARNING):
        _call(_executor(), 500, 0)
    assert any("未上报输出 token" in r.getMessage() for r in caplog.records)


def test_underreported_tokens_warn(caplog):
    """远超正常字符/token 比 → 疑似 usage 低估或模型被替换。"""
    with caplog.at_level(logging.WARNING):
        _call(_executor(), 5000, 100)
    assert any("字符/token" in r.getMessage() for r in caplog.records)


def test_ratio_at_threshold_is_silent(caplog):
    """恰好等于阈值不告警（判据是严格大于）。"""
    tokens = int(USAGE_MIN_CHARS * USAGE_UNDERREPORT_RATIO)
    with caplog.at_level(logging.WARNING):
        _call(_executor(), USAGE_MIN_CHARS, tokens)
    assert caplog.records == []


def test_normal_ratio_is_silent(caplog):
    with caplog.at_level(logging.WARNING):
        _call(_executor(), 800, 400)
    assert caplog.records == []


@pytest.mark.parametrize("tokens", [1, 100, 999])
def test_ratio_boundary_uses_content_chars(tokens, caplog):
    chars = 4000
    ratio = chars / tokens
    with caplog.at_level(logging.WARNING):
        _call(_executor(), chars, tokens)
    warned = any("字符/token" in r.getMessage() for r in caplog.records)
    assert warned is (ratio > USAGE_UNDERREPORT_RATIO)


def test_release_keeps_remaining_probe_leases():
    """同一凭证有多个探测位时，释放一个不删整组（剩余留给后续释放）。"""

    class _HalfOpen:
        def __init__(self):
            self.released: list[str] = []

        def release(self, key: str) -> None:
            self.released.append(key)

    guard = _HalfOpen()
    executor = Executor(ExecutorDeps(providers={}, credentials=object(),
                                     scheduler=Scheduler(), default_model="m",
                                     half_open=guard))
    executor._probe_leases["c1"] = ["k1", "k2"]
    executor._release("c1")
    assert guard.released == ["k2"]
    assert executor._probe_leases["c1"] == ["k1"]
    executor._release("c1")
    assert guard.released == ["k2", "k1"]
    assert "c1" not in executor._probe_leases
