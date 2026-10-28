"""上游自报模型观测：与请求模型不一致只告警（不改响应、不冷却、不写失败归因）。"""

from __future__ import annotations

import logging

from src.compat.openai.request import parse_chat_request
from src.engine.executor import Executor, ExecutorDeps, _model_mismatch
from src.engine.scheduler import Scheduler
from src.provider.base import Event, EventKind, Usage


class _Provider:
    id = "codebuddy"

    def __init__(self, events) -> None:
        self.events = events

    async def stream_chat(self, _data, _payload, _model):
        for event in self.events:
            yield event

    def list_models(self, _data):  # pragma: no cover - 本用例不调用
        return []


class _Collector:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, **fields) -> None:
        self.events.append(fields)


def _executor(repo, provider: _Provider) -> Executor:
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    return Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=_Collector()))


def _request(stream: bool = True):
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                               "stream": stream})


def test_model_mismatch_tolerates_version_suffixes():
    assert _model_mismatch("glm-5.2", "glm-5.2") is False
    assert _model_mismatch("GLM-5.2", "glm-5.2-20261001") is False
    assert _model_mismatch("glm-5.2-20261001", "glm-5.2") is False
    # 真的换成了另一个模型才命中
    assert _model_mismatch("glm-5.2", "glm-4.6") is True
    assert _model_mismatch("", "glm-5.2") is False
    assert _model_mismatch("glm-5.2", "") is False
    # 占位名（Qoder 每帧回 auto）不算替换证据
    assert _model_mismatch("glm-5.2", "auto") is False
    assert _model_mismatch("glm-5.2", "DEFAULT") is False


async def test_stream_warns_when_upstream_reports_other_model(repo, caplog):
    provider = _Provider([
        Event(kind=EventKind.CONTENT, content="ok", model="glm-4.6"),
        Event(kind=EventKind.USAGE, usage=Usage(1, 2, 0), model="glm-4.6"),
        Event(kind=EventKind.FINISH, finish_reason="stop", model="glm-4.6"),
    ])
    executor = _executor(repo, provider)

    with caplog.at_level(logging.WARNING, logger="src.engine.executor"):
        async for _ in executor.stream(_request(), username="alice"):
            pass

    warnings = [r.getMessage() for r in caplog.records if "不一致" in r.getMessage()]
    assert len(warnings) == 1
    assert "glm-4.6" in warnings[0] and "glm-5.2" in warnings[0]
    # 只告警：响应照常成功
    assert "成功" not in warnings[0]


async def test_stream_matching_model_does_not_warn(repo, caplog):
    provider = _Provider([
        Event(kind=EventKind.CONTENT, content="ok", model="glm-5.2-20261001"),
        Event(kind=EventKind.FINISH, finish_reason="stop", model="glm-5.2-20261001"),
    ])
    executor = _executor(repo, provider)

    with caplog.at_level(logging.WARNING, logger="src.engine.executor"):
        async for _ in executor.stream(_request(), username="alice"):
            pass

    assert not [r for r in caplog.records if "不一致" in r.getMessage()]


async def test_stream_without_model_field_skips_check(repo, caplog):
    provider = _Provider([
        Event(kind=EventKind.CONTENT, content="ok"),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ])
    executor = _executor(repo, provider)

    with caplog.at_level(logging.WARNING, logger="src.engine.executor"):
        async for _ in executor.stream(_request(), username="alice"):
            pass

    assert not [r for r in caplog.records if "不一致" in r.getMessage()]


async def test_complete_warns_when_upstream_reports_other_model(repo, caplog):
    provider = _Provider([
        Event(kind=EventKind.CONTENT, content="ok", model="glm-4.6"),
        Event(kind=EventKind.USAGE, usage=Usage(1, 2, 0)),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ])
    executor = _executor(repo, provider)

    with caplog.at_level(logging.WARNING, logger="src.engine.executor"):
        result = await executor.complete(_request(stream=False), username="alice")

    assert result["choices"][0]["message"]["content"] == "ok"
    assert [r for r in caplog.records if "不一致" in r.getMessage()]
