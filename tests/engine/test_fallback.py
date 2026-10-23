"""非流式 fallback：链回退、成员去重、错误优先级与单例语义。"""

from __future__ import annotations

import pytest

from src.compat.openai.request import InvalidRequest
from src.engine.model_resolver import ModelTarget
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

TRANSIENT = [Event(kind=EventKind.ERROR, error_code="500",
                   error_message="boom", error_kind=ErrKind.OTHER)]

@pytest.mark.asyncio
async def test_complete_falls_back_when_primary_all_invalid(repo):
    """主模型（组内成员）全 INVALID → 回退到下一成员。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [INVALID])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls == 1 and tr.calls == 1
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_http_invalid_exception_skips_provider(repo):
    """HTTP 级 INVALID（异常）与非流内 INVALID 同语义：跳过该上游，不冷却凭证。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = RaisingProvider("codebuddy", HttpInvalid("400 model not found"))
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls == 1 and tr.calls == 1
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_group_name_entry(repo):
    """请求兼容组名：以组内首成员发起。"""
    credentials, db = repo
    _add(credentials, "trae", "tr")
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"trae": tr}, credentials,
                         {"fast": ("glm-4.6", "glm-5")},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    result = await executor.complete(_request("fast"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_skips_member_missing_from_catalog(repo):
    """目录确认成员不挂在任何候选渠道 → 跳过，不白打上游。"""
    credentials, db = repo
    _add(credentials, "trae", "tr")
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"trae": tr}, credentials,
                         {"fast": ("ghost-model", "glm-4.6")},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    result = await executor.complete(_request("fast"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_dedupes_repeated_member(repo):
    """组配置写重了不重复尝试。"""
    credentials, db = repo
    _add(credentials, "trae", "tr")
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"trae": tr}, credentials,
                         {"fast": ("glm-4.6", "glm-4.6")},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    result = await executor.complete(_request("fast"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_raises_last_error_when_chain_exhausted(repo):
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    cb = ScriptedProvider("codebuddy", [INVALID])
    executor = _executor({"codebuddy": cb}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         {"codebuddy": {"glm-5.2": "glm-5.2"}})
    with pytest.raises(InvalidRequest):
        await executor.complete(_request("glm-5.2"))
    db.close()

@pytest.mark.asyncio
async def test_complete_no_fallback_when_groups_empty(repo):
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    cb = ScriptedProvider("codebuddy", [GOOD])
    executor = _executor({"codebuddy": cb}, credentials, {})
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    db.close()

@pytest.mark.asyncio
async def test_complete_no_fallback_when_model_not_in_group(repo):
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    cb = ScriptedProvider("codebuddy", [GOOD])
    executor = _executor({"codebuddy": cb}, credentials, {"fast": ("glm-4.6",)})
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    db.close()

@pytest.mark.asyncio
async def test_forced_provider_never_falls_back(repo):
    """`@渠道` 把渠道钉死，用户明确意图：不参与跨渠道回退。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [INVALID])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    with pytest.raises(InvalidRequest):
        await executor.complete(_request("glm-5.2@codebuddy"))
    assert tr.calls == 0
    db.close()

def test_fallback_chain_skips_unknown_provider_member(repo):
    """兼容组成员写了未知渠道：跳过该成员，不因此丢掉整条链。"""
    credentials, db = repo
    executor = _executor({}, credentials, {"fast": ("a", "b@nosuchchannel")})
    chain = executor._fallback_chain(ModelTarget(model="fast", providers=("trae",)))
    assert [t.model for t in chain] == ["a"]
    db.close()

def test_fallback_chain_empty_when_all_members_unknown(repo):
    """所有成员都写了未知渠道：退化为请求模型本身，交给执行层报 400。"""
    credentials, db = repo
    executor = _executor({}, credentials, {"fast": ("a@nope",)})
    chain = executor._fallback_chain(ModelTarget(model="fast", providers=("trae",)))
    assert [t.model for t in chain] == ["fast"]
    db.close()

def test_fallback_chain_filters_pure_alias_group(repo):
    """组名是纯别名（不在目录里）→ 不进链；成员命中则用之。"""
    credentials, db = repo
    executor = _executor({}, credentials, {"fast": ("glm-4.6",)},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    chain = executor._fallback_chain(ModelTarget(model="fast", providers=("trae",)))
    assert [t.model for t in chain] == ["glm-4.6"]
    db.close()

def test_fallback_chain_all_members_filtered_returns_alias(repo):
    """组名别名 + 所有成员都不在目录：退化为请求模型本身。"""
    credentials, db = repo
    executor = _executor({}, credentials, {"fast": ("ghost",)},
                         {"trae": {"glm-4.6": "glm-4.6"}})
    chain = executor._fallback_chain(ModelTarget(model="fast", providers=("trae",)))
    assert [t.model for t in chain] == ["fast"]
    db.close()

@pytest.mark.asyncio
async def test_fallback_with_multiple_primary_credentials(repo):
    """主模型多凭证依次软失败后仍能回退（每链项各自 3 次预算）。"""
    credentials, db = repo
    for token in ("cb1", "cb2", "cb3"):
        _add(credentials, "codebuddy", token)
    _add(credentials, "trae", "tr")
    cb = ScriptedProvider("codebuddy", [TRANSIENT])
    tr = ScriptedProvider("trae", [GOOD])
    executor = _executor({"codebuddy": cb, "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls == 3 and tr.calls == 1
    db.close()

@pytest.mark.asyncio
async def test_complete_503_primary_then_fallback_ok(repo):
    """主模型无可用凭证（唯一登记渠道无凭证）→ 回退链尝试下一项。

    同流式路径：不在未登记 `glm-5.2` 的 trae 上白打一次（旧逻辑会先拿原始名
    打过去、拿一个 INVALID 再回退），直接从链首的「无可用凭证」进下一成员。
    """
    credentials, db = repo
    # 只有 trae 有凭证；codebuddy 一条都没有
    _add(credentials, "trae", "tr")
    tr = ModelAwareProvider("trae", {"glm-4.6"})
    executor = _executor({"codebuddy": ScriptedProvider("codebuddy"),
                          "trae": tr}, credentials,
                         {"chat": ("glm-5.2", "glm-4.6")},
                         _aliases("glm-5.2", "glm-4.6"))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "ok"
    assert tr.models_seen == ["glm-4.6"]
    db.close()

@pytest.mark.asyncio
async def test_complete_all_members_skipped_raises_primary_error(repo):
    """回退成员都被目录过滤：以链首错误收尾。"""
    credentials, db = repo
    _add(credentials, "codebuddy", "cb")
    cb = ScriptedProvider("codebuddy", [INVALID])
    executor = _executor({"codebuddy": cb, "trae": ScriptedProvider("trae")},
                         credentials, {"chat": ("glm-5.2", "ghost")},
                         {"codebuddy": {"glm-5.2": "glm-5.2"}})
    with pytest.raises(InvalidRequest):
        await executor.complete(_request("glm-5.2"))
    db.close()

def test_with_model_returns_same_instance_when_unchanged(repo):
    credentials, db = repo
    executor = _executor({}, credentials, {})
    request = _request("glm-5.2")
    assert executor._with_model(request, "glm-5.2") is request
    swapped = executor._with_model(request, "glm-4.6")
    assert swapped.model == "glm-4.6" and swapped.raw["model"] == "glm-4.6"
    db.close()

def test_fallback_allowed_without_catalog(repo):
    """目录未就绪（aliases 空）时放行回退，交给执行层兜底候选。"""
    credentials, db = repo
    executor = _executor({}, credentials, {})
    assert executor._fallback_allowed(ModelTarget(model="anything", providers=("trae",)))
    db.close()
