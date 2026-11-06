"""reasoning 档位映射与思考内容剥离（纯函数 + executor 装配点）。

三条不变量：
1. 默认（留空 / 未装配）必须与旧行为**逐字节等价**——不写任何上游字段、
   不改任何响应事件。
2. 只动客户端**显式**给的档位；渠道自身 `setdefault` 补的默认档不受影响。
3. 解析宽容：坏段只丢自己，不连累同配置里的其他合法条目，也不抛异常
   （一行笔误不能让整个网关起不来）。
"""

from __future__ import annotations

import pytest

from src.compat.anthropic.request import parse_messages_request
from src.compat.openai.request import parse_chat_request
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.executor import Executor, ExecutorDeps
from src.engine.reasoning import (
    map_effort,
    parse_effort_map,
    parse_thinking_efforts,
    thinking_effort,
)
from src.engine.scheduler import Scheduler
from src.provider.base import Event, EventKind
from tests.helpers.constants import SECRET
from tests.helpers.providers import ScriptedProvider

# ------------------------------------------------------------ 出站档位映射


def test_parse_effort_map_channels_and_wildcard():
    mapping = parse_effort_map("codebuddy=high:medium,medium:low;*=high:high")
    assert mapping == {"codebuddy": {"high": "medium", "medium": "low"},
                       "*": {"high": "high"}}


def test_parse_effort_map_tolerates_bad_segments():
    """空段 / 缺 `=` / 缺 `:` / 空源 / 空目标 / 渠道无合法条目都只丢自己。"""
    mapping = parse_effort_map(
        " ; =x ; codebuddy ; codebuddy=high ,low: ,:x,high:medium;trae=,")
    assert mapping == {"codebuddy": {"high": "medium"}}


def test_parse_effort_map_empty_and_blank():
    assert parse_effort_map("") == {}
    assert parse_effort_map("   ") == {}


def test_parse_effort_map_later_duplicate_wins():
    mapping = parse_effort_map("trae=high:low,high:medium")
    assert mapping["trae"] == {"high": "medium"}


def test_map_effort_channel_takes_precedence_over_wildcard():
    mapping = parse_effort_map("trae=high:low;*=high:medium")
    assert map_effort(mapping, "trae", "high") == "low"
    assert map_effort(mapping, "qoder", "high") == "medium"


def test_map_effort_none_target_means_delete():
    mapping = parse_effort_map("trae=high:none")
    assert map_effort(mapping, "trae", "high") is None


def test_map_effort_unmatched_returns_original():
    mapping = parse_effort_map("trae=high:low")
    # 渠道没命中、源档位没命中：都原样返回（调用方据此判断「无需改写」）
    assert map_effort(mapping, "qoder", "high") == "high"
    assert map_effort(mapping, "trae", "medium") == "medium"
    assert map_effort(mapping, None, "high") == "high"
    assert map_effort({}, "trae", "high") == "high"


# ------------------------------------------------- Anthropic thinking → 档位


def test_parse_thinking_efforts_thresholds_sorted_desc():
    disabled, thresholds = parse_thinking_efforts("4096:low,32768:medium,disabled:high")
    assert disabled == "high"
    assert thresholds == ((32768, "medium"), (4096, "low"))


def test_parse_thinking_efforts_tolerates_bad_segments():
    disabled, thresholds = parse_thinking_efforts(
        " ,abc,4096 , :low,notanumber:high,1024:low,4096:medium")
    assert disabled is None
    assert thresholds == ((4096, "medium"), (1024, "low"))


def test_parse_thinking_efforts_blank():
    assert parse_thinking_efforts("") == (None, ())


def test_thinking_effort_picks_largest_threshold_at_or_below_budget():
    table = "4096:low,32768:medium"
    assert thinking_effort(table, {"type": "enabled", "budget_tokens": 4096}) == "low"
    assert thinking_effort(table, {"type": "enabled", "budget_tokens": 99999}) == "medium"
    # 低于最小阈值不映射
    assert thinking_effort(table, {"type": "enabled", "budget_tokens": 1024}) is None


def test_thinking_effort_disabled_and_none_target():
    assert thinking_effort("disabled:low", {"type": "disabled"}) == "low"
    # 未配 disabled 档位时不动
    assert thinking_effort("4096:low", {"type": "disabled"}) is None
    # 目标 none = 不映射（Anthropic 入站本来就没有 reasoning_effort 可删）
    assert thinking_effort("disabled:none", {"type": "disabled"}) is None


@pytest.mark.parametrize("thinking", [
    None, "enabled", [], {"type": "adaptive"},
    {"type": "enabled"},                       # 缺 budget_tokens
    {"type": "enabled", "budget_tokens": "4096"},   # 非 int
    {"type": "enabled", "budget_tokens": True},     # bool 不算 int
])
def test_thinking_effort_unknown_shapes_do_not_map(thinking):
    assert thinking_effort("4096:low", thinking) is None


def test_thinking_effort_blank_table_never_maps():
    """留空 = 完全不映射：连 disabled 也不动（现状）。"""
    assert thinking_effort("", {"type": "disabled"}) is None
    assert thinking_effort("", {"type": "enabled", "budget_tokens": 32768}) is None


# ---------------------------------------------------- output_config.effort


def test_output_config_effort_passes_through():
    """Claude Code 2.1.318 的 `/effort` 落在 `output_config.effort`。

    此前整块被丢弃 → 用户选档位静默失效且不报错（比 400 更难排查）。
    """
    parsed = parse_messages_request({
        "model": "m", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
        "output_config": {"effort": "low"},
        "thinking": {"type": "adaptive"},
    })
    assert parsed.raw["reasoning_effort"] == "low"


def test_output_config_effort_wins_over_thinking_table():
    """两个来源同时存在时以 `output_config` 为准（那是用户显式选的档位）。"""
    parsed = parse_messages_request({
        "model": "m", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
        "output_config": {"effort": "HIGH"},
        "thinking": {"type": "enabled", "budget_tokens": 4096},
    }, thinking_efforts="4096:minimal")
    assert parsed.raw["reasoning_effort"] == "high"


@pytest.mark.parametrize("output_config", [
    None, "effort", [], {"effort": ""}, {"effort": "  "}, {"effort": 5}, {},
    {"format": {"type": "json"}},
])
def test_output_config_effort_absent_or_malformed_never_maps(output_config):
    """缺 `effort` / 非字符串一律不映射：`output_config` 其余成员仍忽略。"""
    body = {"model": "m", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]}
    if output_config is not None:
        body["output_config"] = output_config
    assert "reasoning_effort" not in parse_messages_request(body).raw


def test_output_config_effort_falls_back_to_thinking_table():
    """`output_config` 没给 effort 时，`thinking.budget_tokens` 映射照旧生效。"""
    parsed = parse_messages_request({
        "model": "m", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
        "output_config": {"format": {"type": "json"}},
        "thinking": {"type": "enabled", "budget_tokens": 4096},
    }, thinking_efforts="4096:low")
    assert parsed.raw["reasoning_effort"] == "low"


def test_parse_messages_request_maps_thinking_budget():
    parsed = parse_messages_request(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}],
         "thinking": {"type": "enabled", "budget_tokens": 8192}},
        thinking_efforts="4096:low,32768:medium")
    assert parsed.raw["reasoning_effort"] == "low"


def test_parse_messages_request_leaves_thinking_alone_by_default():
    """默认空表：`thinking` 与现状一样被忽略，不写任何上游字段。"""
    parsed = parse_messages_request(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}],
         "thinking": {"type": "enabled", "budget_tokens": 8192}})
    assert "reasoning_effort" not in parsed.raw


# ------------------------------------------------------- executor 装配点


@pytest.fixture()
def credentials(tmp_path):
    db = Database(tmp_path / "r.sqlite3")
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))
    repo.add(provider="trae", credential_data={"accessToken": "a"})
    yield repo
    db.close()


def _executor(credentials, script, **deps):
    return Executor(ExecutorDeps(providers={"trae": ScriptedProvider(script)},
                                 credentials=credentials, scheduler=Scheduler(),
                                 default_model="glm-5.2", **deps))


REASONING_SCRIPT = [[Event(kind=EventKind.REASONING, content="think"),
                     Event(kind=EventKind.CONTENT, content="hi"),
                     Event(kind=EventKind.FINISH, finish_reason="stop")]]


async def test_force_disable_thinking_drops_reasoning_in_stream(credentials):
    executor = _executor(credentials, REASONING_SCRIPT, force_disable_thinking=True)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    body = b"".join(chunks)
    assert b"reasoning_content" not in body
    assert b"hi" in body


async def test_force_disable_thinking_drops_reasoning_in_complete(credentials):
    executor = _executor(credentials, REASONING_SCRIPT, force_disable_thinking=True)
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert "reasoning_content" not in result["choices"][0]["message"]


async def test_reasoning_kept_by_default(credentials):
    """默认关：思考内容照旧透出（与旧行为逐字节等价）。"""
    executor = _executor(credentials, REASONING_SCRIPT)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"think" in b"".join(chunks)


async def test_reasoning_effort_map_rewrites_explicit_value(credentials):
    provider = ScriptedProvider([[Event(kind=EventKind.CONTENT, content="ok"),
                                  Event(kind=EventKind.FINISH, finish_reason="stop")]])
    seen: list[dict] = []

    async def capture(_credential_data, payload, _model):
        seen.append(dict(payload))
        for event in provider.script[0]:
            yield event

    class Capturing:
        id = "trae"

        stream_chat = staticmethod(capture)

    executor = Executor(ExecutorDeps(
        providers={"trae": Capturing()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2",
        reasoning_effort_map=lambda: "trae=high:low;*=high:medium"))
    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"}))
    assert seen[0]["reasoning_effort"] == "low"


async def test_reasoning_effort_map_delete_target(credentials):
    seen: list[dict] = []

    class Capturing:
        id = "trae"

        @staticmethod
        async def stream_chat(_credential_data, payload, _model):
            seen.append(dict(payload))
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    executor = Executor(ExecutorDeps(
        providers={"trae": Capturing()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2",
        reasoning_effort_map=lambda: "trae=high:none"))
    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"}))
    assert "reasoning_effort" not in seen[0]


async def test_reasoning_effort_map_untouched_when_client_silent(credentials):
    """客户端没给档位 → 一个字段都不加（渠道自身 setdefault 补值不归这里管）。"""
    seen: list[dict] = []

    class Capturing:
        id = "trae"

        @staticmethod
        async def stream_chat(_credential_data, payload, _model):
            seen.append(dict(payload))
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    executor = Executor(ExecutorDeps(
        providers={"trae": Capturing()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2",
        reasoning_effort_map=lambda: "trae=high:low"))
    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert "reasoning_effort" not in seen[0]


async def test_reasoning_effort_map_blank_or_unmatched_keeps_payload(credentials):
    """空映射 / 未命中：原样透传（不复制、不改写）。"""
    seen: list[dict] = []

    class Capturing:
        id = "trae"

        @staticmethod
        async def stream_chat(_credential_data, payload, _model):
            seen.append(dict(payload))
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    for raw in ("", "trae=medium:low"):
        executor = Executor(ExecutorDeps(
            providers={"trae": Capturing()}, credentials=credentials,
            scheduler=Scheduler(), default_model="glm-5.2",
            reasoning_effort_map=lambda raw=raw: raw))
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"}))
    assert [item["reasoning_effort"] for item in seen] == ["high", "high"]


async def test_reasoning_effort_map_does_not_mutate_request_raw(credentials):
    """改写必须落在新 dict 上：`request.raw` 是会话粘性指纹的输入。"""
    class Capturing:
        id = "trae"

        @staticmethod
        async def stream_chat(_credential_data, _payload, _model):
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    request = parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"})
    executor = Executor(ExecutorDeps(
        providers={"trae": Capturing()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2",
        reasoning_effort_map=lambda: "trae=high:low"))
    await executor.complete(request)
    assert request.raw["reasoning_effort"] == "high"
