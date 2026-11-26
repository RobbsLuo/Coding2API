"""成功请求的用量 ↔ 正文体量对账（上游静默替换/截断的观测信号）。"""

from __future__ import annotations

import logging

import pytest

from src.engine.executor import USAGE_MIN_CHARS, USAGE_UNDERREPORT_RATIO, Executor, ExecutorDeps
from src.engine.scheduler import Scheduler
from src.provider.base import Event, EventKind, Usage


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


# ------------------------------------------------ record 同时落「真值 + 有效命中」

class _RecordingStats:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, **fields) -> None:
        self.events.append(fields)


class _OneShotProvider:
    """一次固定脚本的假渠道；stream / complete 两条路径共用。"""

    id = "trae"

    def __init__(self) -> None:
        self.usage = Usage(input_tokens=1000, output_tokens=10,
                           cached_tokens=None, cached_tokens_effective=500)

    async def stream_chat(self, _credential_data, _payload, _model):
        yield Event(kind=EventKind.CONTENT, content="hi")
        yield Event(kind=EventKind.USAGE, usage=self.usage)
        yield Event(kind=EventKind.FINISH, finish_reason="stop")

    def list_models(self, _credential_data):  # pragma: no cover - 由 main 调用
        return []


def _usage_executor(repo, stats):
    credentials, _db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    return Executor(ExecutorDeps(providers={"trae": _OneShotProvider()},
                                 credentials=credentials, scheduler=Scheduler(),
                                 default_model="glm-5.2", stats=stats))


async def test_stream_record_passes_cached_tokens_effective(repo):
    """流式记账：上游真值（NULL）与定价用的有效命中分开落库。"""
    from src.compat.openai.request import parse_chat_request

    stats = _RecordingStats()
    executor = _usage_executor(repo, stats)
    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True})):
        pass
    event = stats.events[-1]
    assert event["cached_tokens"] is None
    assert event["cached_tokens_effective"] == 500


async def test_complete_record_passes_cached_tokens_effective(repo):
    """非流式记账：从最后一个 USAGE 事件取有效命中，不能因聚合丢字段。"""
    from src.compat.openai.request import parse_chat_request

    stats = _RecordingStats()
    executor = _usage_executor(repo, stats)
    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    event = stats.events[-1]
    assert event["cached_tokens"] is None
    assert event["cached_tokens_effective"] == 500
