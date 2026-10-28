"""推理档位自适应降级（`reasoning_effort` 被上游拒收时原号降一档重试）。

覆盖两条入口（流式 / 非流式）× 两种错误形态（HTTP 异常 / 流内 error 事件），
以及不该降的场景：已降过一次、客户端没给档位、错误与档位无关、已是最低档。
"""

from __future__ import annotations

import pytest

from src.compat.openai.request import parse_chat_request
from src.engine.executor import Executor, ExecutorDeps
from src.engine.reasoning import downgrade_effort, is_effort_rejection
from src.engine.scheduler import Scheduler
from src.provider.base import ErrKind, Event, EventKind, Usage


class _EffortRejected(Exception):
    """HTTP 400：上游不认这个推理档位。"""

    def kind(self):
        return ErrKind.INVALID


def _request(effort: str | None = "high", *, stream: bool = True):
    payload: dict = {"messages": [{"role": "user", "content": "hi"}],
                     "stream": stream}
    if effort is not None:
        payload["reasoning_effort"] = effort
    return parse_chat_request(payload)


class _Provider:
    """按脚本逐次响应，并记录每次出站载荷里的档位。"""

    id = "codebuddy"

    def __init__(self, script) -> None:
        self.script = script
        self.calls = 0
        self.efforts: list[str | None] = []

    async def stream_chat(self, _data, payload, _model):
        self.efforts.append(payload.get("reasoning_effort"))
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    def list_models(self, _data):  # pragma: no cover - 本用例不调用
        return []


class _Collector:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, **fields) -> None:
        self.events.append(fields)


def _executor(repo, provider: _Provider, collector: _Collector) -> Executor:
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    return Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=collector))


_GOOD = [Event(kind=EventKind.CONTENT, content="ok"),
         Event(kind=EventKind.USAGE, usage=Usage(1, 2, 0)),
         Event(kind=EventKind.FINISH, finish_reason="stop")]

_REJECT = [_EffortRejected("400 unsupported reasoning_effort: high")]

_STREAM_REJECT = [Event(kind=EventKind.ERROR, error_kind=ErrKind.INVALID,
                        error_message="invalid effort level high")]


# ------------------------------------------------------------------ 纯函数

def test_downgrade_effort_walks_one_step_down():
    assert downgrade_effort("xhigh") == "high"
    assert downgrade_effort("high") == "medium"
    assert downgrade_effort("medium") == "low"
    assert downgrade_effort("low") == "minimal"
    assert downgrade_effort(" HIGH ") == "medium"
    # 已是链底 / 未知档位：不猜，直接放弃重试
    assert downgrade_effort("minimal") is None
    assert downgrade_effort("turbo") is None


def test_is_effort_rejection_matches_only_explicit_markers():
    assert is_effort_rejection("unsupported reasoning_effort: xhigh")
    assert is_effort_rejection("Invalid effort level: high")
    assert is_effort_rejection("thinking budget_tokens too large")
    assert is_effort_rejection("不支持的推理档位")
    # 普通参数错误不降档（否则白打一次上游）
    assert not is_effort_rejection("model not found")
    assert not is_effort_rejection("")
    assert not is_effort_rejection(None)


# ---------------------------------------------------------------- 流式：异常

async def test_stream_http_effort_rejection_retries_lower_on_same_credential(repo):
    provider = _Provider([_REJECT, _GOOD])
    collector = _Collector()
    executor = _executor(repo, provider, collector)

    frames = [f async for f in executor.stream(_request("high"), username="alice")]

    # 原号重试：两次都打到同一个 provider，档位从 high 降到 medium
    assert provider.efforts == ["high", "medium"]
    assert b"[DONE]" in b"".join(frames)
    assert collector.events[-1]["ok"] is True
    assert collector.events[-1].get("error_type") is None


async def test_stream_effort_rejection_at_lowest_effort_is_not_retried(repo):
    provider = _Provider([[_EffortRejected("bad effort")], _GOOD])
    collector = _Collector()
    executor = _executor(repo, provider, collector)

    frames = [f async for f in executor.stream(_request("minimal"), username="alice")]

    # 没有更低档位可降 → 走既有 INVALID 路径（一次尝试后 400 收尾）
    assert provider.efforts == ["minimal"]
    assert b"invalid_request" in b"".join(frames)


async def test_stream_effort_rejection_retries_only_once(repo):
    provider = _Provider([[_EffortRejected("unsupported reasoning_effort")]] * 2)
    collector = _Collector()
    executor = _executor(repo, provider, collector)

    frames = [f async for f in executor.stream(_request("high"), username="alice")]

    # 只降一档：第二次仍被拒时不再循环（high → medium → 结束）
    assert provider.efforts == ["high", "medium"]
    assert b"invalid_request" in b"".join(frames)


async def test_stream_invalid_without_effort_is_not_retried(repo):
    provider = _Provider([[_EffortRejected("unsupported reasoning_effort")], _GOOD])
    collector = _Collector()
    executor = _executor(repo, provider, collector)

    await anext(executor.stream(_request(None), username="alice"))

    # 客户端没给档位：没有可降的东西，不重试
    assert provider.efforts == [None]


async def test_stream_invalid_non_effort_message_is_not_retried(repo):
    provider = _Provider([[_EffortRejected("model not found")], _GOOD])
    collector = _Collector()
    executor = _executor(repo, provider, collector)

    frames = [f async for f in executor.stream(_request("high"), username="alice")]

    assert provider.efforts == ["high"]
    assert b"invalid_request" in b"".join(frames)


# ------------------------------------------------------------ 流式：error 事件

async def test_stream_event_effort_rejection_retries_lower(repo):
    provider = _Provider([_STREAM_REJECT, _GOOD])
    collector = _Collector()
    executor = _executor(repo, provider, collector)

    frames = [f async for f in executor.stream(_request("medium"), username="alice")]

    assert provider.efforts == ["medium", "low"]
    assert b"[DONE]" in b"".join(frames)
    assert collector.events[-1]["ok"] is True


# -------------------------------------------------------------------- 非流式

async def test_complete_http_effort_rejection_retries_lower(repo):
    provider = _Provider([_REJECT, _GOOD])
    collector = _Collector()
    executor = _executor(repo, provider, collector)

    result = await executor.complete(_request("high", stream=False), username="alice")

    assert provider.efforts == ["high", "medium"]
    assert result["choices"][0]["message"]["content"] == "ok"
    assert collector.events[-1]["ok"] is True


async def test_complete_event_effort_rejection_retries_lower(repo):
    provider = _Provider([_STREAM_REJECT, _GOOD])
    collector = _Collector()
    executor = _executor(repo, provider, collector)

    result = await executor.complete(_request("high", stream=False), username="alice")

    assert provider.efforts == ["high", "medium"]
    assert result["choices"][0]["message"]["content"] == "ok"


async def test_complete_effort_rejection_retries_only_once(repo):
    provider = _Provider([_REJECT] * 2)
    collector = _Collector()
    executor = _executor(repo, provider, collector)

    from src.compat.openai.request import InvalidRequest

    with pytest.raises(InvalidRequest):
        await executor.complete(_request("high", stream=False), username="alice")

    assert provider.efforts == ["high", "medium"]


# ------------------------------------------------------------ 私有口径直测

def test_clamp_effort_ignores_missing_or_equal_effort(repo):
    collector = _Collector()
    provider = _Provider([_GOOD])
    executor = _executor(repo, provider, collector)
    payload = {"reasoning_effort": "high"}

    assert executor._clamp_effort(payload, None) is payload
    assert executor._clamp_effort({"messages": []}, "low") == {"messages": []}
    assert executor._clamp_effort(payload, "high") is payload
    assert executor._clamp_effort(payload, "medium")["reasoning_effort"] == "medium"
    # 不改入参
    assert payload["reasoning_effort"] == "high"
