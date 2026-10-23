"""流式 fallback：首个帧前回退、首个帧后不再回退、轮换预算。"""

from __future__ import annotations

import pytest

from src.engine.executor import NoProviderForModel
from src.provider.base import ErrKind, Event, EventKind
from tests.engine._fallback_support import (
    GOOD,
    INVALID,
    HttpInvalid,
    ModelAwareProvider,
    RaisingProvider,
    ScriptedProvider,
)
from tests.engine._fallback_support import add_credential as _add
from tests.engine._fallback_support import aliases as _aliases
from tests.engine._fallback_support import make_executor as _executor
from tests.helpers.chat import chat_request as _request


@pytest.mark.asyncio
async def test_stream_falls_back_before_first_frame(repo):
    """流式：主模型全 INVALID（未出帧）时回退到下一模型再正常出流。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [INVALID])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert "ok" in joined
    assert "error" not in joined
    assert joined.count("data: [DONE]") == 1
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_stream_http_invalid_exception_falls_back(repo):
    """HTTP 级 INVALID（异常，不是流内事件）同样在首帧前回退。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = RaisingProvider("codebuddy", HttpInvalid("400 model not found"))
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert "ok" in joined and "error" not in joined
    assert cb.calls == 1 and tr.calls == 1
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_stream_exhausted_chain_emits_single_error_frame(repo):
    """整条链都失败：只发一帧错误，且不再继续尝试。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    cb = ScriptedProvider("codebuddy", [INVALID])
    executor = _executor({"codebuddy": cb}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         {"codebuddy": {"glm-5.2": "glm-5.2"}})
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert joined.count('"error"') == 1
    assert cb.calls == 1                    # 回退成员被目录过滤，未白打
    db.close()

@pytest.mark.asyncio
async def test_stream_uses_injected_translator_across_fallback(repo):
    """注入 translator（responses/anthropic）在回退时复用同一实例。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [INVALID])
    tr = ScriptedProvider("trae", [GOOD])

    class Sink:
        def __init__(self, model):
            self.model = model
            self.usage = None
            self.done_sent = False

        def translate(self, event):
            if event.kind is EventKind.CONTENT:
                yield f"data: {event.content}\n\n".encode()

        def finish(self):
            self.done_sent = True
            yield b"data: [DONE]\n\n"

        def error_frame(self, message, code):
            return f"ERROR {code} {message}".encode()

    sink = Sink("glm-5.2")
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    frames = [f async for f in executor.stream(
        _request("glm-5.2"), username="u", translator=sink)]
    assert frames[-1] == b"data: [DONE]\n\n"
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_stream_no_fallback_after_first_frame(repo):
    """已出帧后绝不再换模型：半截输出不可回滚。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [[
        Event(kind=EventKind.CONTENT, content="partial"),
        Event(kind=EventKind.ERROR, error_code="500",
              error_message="boom", error_kind=ErrKind.OTHER)]])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"), max_rotate=1)
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert "partial" in joined
    assert tr.calls == 0                    # 已出帧 → 不换模型
    db.close()

@pytest.mark.asyncio
async def test_stream_no_fallback_after_first_frame_invalid(repo):
    """已出帧后上游回 INVALID：以错误帧收尾，不换模型。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [[
        Event(kind=EventKind.CONTENT, content="partial"),
        Event(kind=EventKind.ERROR, error_code="11102",
              error_message="nope", error_kind=ErrKind.INVALID)]])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert "partial" in joined
    assert "invalid_request" in joined
    assert tr.calls == 0
    db.close()

@pytest.mark.asyncio
async def test_stream_no_fallback_after_first_frame_invalid_budget_spent(repo):
    """已出帧后 INVALID 且轮换预算即刻用尽：直接以错误帧收尾。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [[
        Event(kind=EventKind.CONTENT, content="partial"),
        Event(kind=EventKind.ERROR, error_code="11102",
              error_message="nope", error_kind=ErrKind.INVALID)]])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"), max_rotate=1)
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert "partial" in joined
    assert "invalid_request" in joined
    assert tr.calls == 0
    db.close()

@pytest.mark.asyncio
async def test_stream_no_rotate_after_first_frame(repo):
    """已出帧后绝不换号：同一渠道换凭证会把两段输出拼进同一条回复。

    上游在吐出内容后回 ERROR（非 INVALID）→ 换号预算仍够，但已出帧就不该
    再打下一个凭证：半截输出不可回滚，且上游两边都真实计费。
    """
    credentials, db = repo
    _add(credentials, "codebuddy", "cb1")
    _add(credentials, "codebuddy", "cb2")
    cb = ScriptedProvider("codebuddy", [[
        Event(kind=EventKind.CONTENT, content="AAA"),
        Event(kind=EventKind.ERROR, error_code="500",
              error_message="boom", error_kind=ErrKind.OTHER)]])
    executor = _executor({"codebuddy": cb}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert "AAA" in joined
    assert cb.calls == 1                    # 已出帧 → 不换号
    assert "no_healthy_credential" in joined
    db.close()

@pytest.mark.asyncio
async def test_stream_no_healthy_credential_before_frame_falls_back(repo):
    """链首候选渠道无凭证（未出帧）→ 也回退到下一项。

    目录里 `glm-5.2` 只挂在 codebuddy 名下，而 codebuddy 一条凭证都没有：
    不放宽到没登记它的 trae（那是拿请求逐个渠道试错），直接让回退链走下一
    成员 `glm-4.6`——trae 只被它认识的模型打一次。
    """
    credentials, db = repo
    _add(credentials, "trae", "tr")
    tr = ModelAwareProvider("trae", {"glm-4.6"})
    executor = _executor({"codebuddy": ScriptedProvider("codebuddy"),
                          "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert "ok" in joined
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_preflight_allows_fallback_chain(repo):
    """链首无注册上游但有回退项时 preflight 放行。"""
    credentials, db = repo
    executor = _executor({"trae": ScriptedProvider("trae")}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    target = executor.preflight(_request("glm-5.2"))
    assert target.model == "glm-5.2"
    db.close()

@pytest.mark.asyncio
async def test_preflight_raises_when_no_provider_registered(repo):
    credentials, db = repo
    executor = _executor({}, credentials, {"chat": ("glm-5.2", "glm-4.6")})
    with pytest.raises(NoProviderForModel):
        executor.preflight(_request("glm-5.2"))
    db.close()

@pytest.mark.asyncio
async def test_invalid_provider_with_many_creds_keeps_rotation_budget(repo):
    """H2 回归：一个不认模型的上游即使凭证数 ≥ max_rotate，也只算「一次尝试」。

    旧逻辑用 `tried` 集合大小当预算，`_skip_provider` 会把该上游全部凭证塞进
    `tried`，一个上游就占满预算 → 直接 400，永远试不到真正持有该模型的
    trae。修复后按真正打过的凭证数计预算，仍能轮到 trae。
    """
    credentials, db = repo
    for token in ("a", "b", "c", "d"):      # 4 个凭证 > max_rotate(3)
        _add(credentials, "codebuddy", token)
    _add(credentials, "trae", "tr")
    # pin codebuddy 保证它先被选中，从而真正走到「400 → 跳过 → trae 接住」路径
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'codebuddy'")
    cb = ScriptedProvider("codebuddy", [INVALID])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials, {})
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls == 1 and tr.calls == 1
    db.close()

@pytest.mark.asyncio
async def test_invalid_provider_many_creds_stream_keeps_rotation_budget(repo):
    """H2 流式路径同非流式：跳过的不认模型上游不占满轮换预算。"""
    credentials, db = repo
    for token in ("a", "b", "c", "d"):
        _add(credentials, "codebuddy", token)
    _add(credentials, "trae", "tr")
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'codebuddy'")
    cb = ScriptedProvider("codebuddy", [INVALID])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials, {})
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert "ok" in joined
    assert "error" not in joined
    assert cb.calls == 1 and tr.calls == 1
    db.close()
