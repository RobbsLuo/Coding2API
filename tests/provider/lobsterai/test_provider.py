"""LobsteraiProvider 门面：凭证导入、节流、分类、登录轨道、签到隔离键。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.provider.base import (
    AuthFlow,
    ErrKind,
    EventKind,
    UpstreamProtocolViolation,
)
from src.provider.lobsterai import LobsteraiProvider, credential_key
from src.provider.lobsterai.client import LobsteraiClient
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_text_done
from tests.provider.lobsterai._support import client as _client
from tests.provider.lobsterai._support import cred as _cred
from tests.provider.lobsterai._support import envelope

# ------------------------------------------------------------------ 身份

def test_credential_key_prefers_uid():
    assert credential_key(_cred(uid="u")) == credential_key(
        _cred(uid="u", uuid="other"))
    # uid 缺失时退到 uuid（同一账号多凭证共享节流）
    assert credential_key(_cred(uid="", uuid="u1")) == credential_key(
        _cred(uid="", uuid="u1"))
    assert credential_key(_cred(uid="", uuid="u1")) != credential_key(
        _cred(uid="", uuid="u2"))


def test_credential_key_is_channel_scoped():
    """跨渠道同名账号不能共桶：否则两个上游的请求会互相排队。"""
    from src.tasks.pacer import stable_key

    assert credential_key(_cred(uid="a")) == stable_key("lobsterai", "a")


def test_provider_defaults():
    provider = LobsteraiProvider()
    assert provider.id == "lobsterai"
    assert isinstance(provider.client, LobsteraiClient)
    assert provider.pacer is None


# ------------------------------------------------------------------ 导入

def _importable(**over) -> dict:
    base = {
        "accessToken": "AT", "refreshToken": "RT", "uuid": "U",
        "firstKeyfrom": "F", "latestKeyfrom": "L", "uid": "uid-1",
    }
    base.update(over)
    return base


def test_import_credential_requires_identity_triple():
    """身份三元组是续期的前提，缺一个就导入即拒绝。"""
    provider = LobsteraiProvider()
    saved = provider.import_credential(_importable())
    assert saved["uuid"] == "U"
    assert saved["firstKeyfrom"] == "F"
    assert saved["latestKeyfrom"] == "L"

    for missing in ("uuid", "firstKeyfrom", "latestKeyfrom"):
        with pytest.raises(UpstreamProtocolViolation, match=missing):
            provider.import_credential(_importable(**{missing: ""}))


def test_import_credential_rejects_missing_access_token():
    with pytest.raises(UpstreamProtocolViolation, match="accessToken"):
        LobsteraiProvider().import_credential(
            _importable(accessToken=""))


def test_import_credential_rejects_non_object():
    with pytest.raises(UpstreamProtocolViolation):
        LobsteraiProvider().import_credential("nope")  # type: ignore[arg-type]


def test_credential_from_round_trips():
    provider = LobsteraiProvider()
    credential = provider.credential_from(_importable())
    assert credential.access_token == "AT"
    assert credential.uuid == "U"


# ------------------------------------------------------------------ 分类

def test_provider_classify_delegates():
    assert LobsteraiProvider().classify(402, b"") is ErrKind.CREDIT
    assert LobsteraiProvider().classify(401, b"") is ErrKind.DEAD


# ------------------------------------------------------------------ 执行

async def test_provider_list_models_delegates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/models/available"
        return httpx.Response(200, json=envelope([{"modelId": "m"}]))

    provider = LobsteraiProvider(client=_client(handler))
    models = await provider.list_models(_importable())
    assert [model.id for model in models] == ["m"]
    await provider.aclose()


async def test_provider_stream_chat_with_pacer():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_text_done(
            chunk({"content": "hi"}, finish="stop")))

    class Pacer:
        def __init__(self) -> None:
            self.turns = 0
            self.released = 0

        async def wait_turn(self, key=None) -> None:
            self.turns += 1

        def release(self, key=None) -> None:
            self.released += 1

    pacer = Pacer()
    provider = LobsteraiProvider(client=_client(handler), pacer=pacer)
    events = [event async for event in provider.stream_chat(
        _importable(), {"messages": []}, "m")]
    assert any(event.kind is EventKind.CONTENT for event in events)
    assert pacer.turns == 1 and pacer.released == 1     # 并发模式必须配对归还
    await provider.aclose()


async def test_provider_stream_chat_without_pacer():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_text_done(
            chunk({"content": "hi"}, finish="stop")))

    provider = LobsteraiProvider(client=_client(handler))
    events = [event async for event in provider.stream_chat(
        _importable(), {"messages": []}, "m")]
    assert any(event.kind is EventKind.CONTENT for event in events)
    await provider.aclose()


async def test_provider_probe_quota_delegates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/user/profile-summary"
        return httpx.Response(200, json=envelope(
            {"totalCreditsRemaining": 100, "creditItems": []}))

    provider = LobsteraiProvider(client=_client(handler))
    quota = await provider.probe_quota(_importable())
    assert quota.remaining == 100
    await provider.aclose()


async def test_provider_refresh_delegates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/refresh"
        return httpx.Response(200, json=envelope(
            {"accessToken": "AT2", "refreshToken": "RT2", "expiresIn": 3600}))

    provider = LobsteraiProvider(client=_client(handler))
    data = await provider.refresh(_importable())
    assert data["accessToken"] == "AT2"
    assert data["refreshToken"] == "RT2"
    await provider.aclose()


# ------------------------------------------------------------------ 签到

async def test_provider_checkin_delegates():
    context_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/client-activities/slot":
            return httpx.Response(200, json=envelope({
                "slotState": "available",
                "activity": {"activityCode": "c", "configRevision": 1}}))
        if request.url.path.endswith("/context"):
            # 第一次查 = 今天还没领；复核那次 = 已领（claimedToday 翻转）
            context_calls["n"] += 1
            return httpx.Response(200, json=envelope({
                "state": {"claimedToday": context_calls["n"] > 1},
                "actions": ["check_in"]}))
        return httpx.Response(200, json=envelope(
            {"result": {"creditsGranted": 100}}))

    provider = LobsteraiProvider(client=_client(handler))
    result = await provider.checkin(_importable())
    assert result.ok is True
    await provider.aclose()


async def test_provider_checkin_status_delegates():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/client-activities/slot":
            return httpx.Response(200, json=envelope({
                "slotState": "available",
                "activity": {"activityCode": "c", "configRevision": 1}}))
        return httpx.Response(200, json=envelope({
            "state": {"claimedToday": True, "rewardCredits": 100},
            "actions": ["check_in"]}))

    provider = LobsteraiProvider(client=_client(handler))
    status = await provider.checkin_status(_importable())
    assert status["today_checked_in"] is True
    await provider.aclose()


def test_checkin_scope_isolates_accounts():
    provider = LobsteraiProvider()
    assert provider.checkin_scope(_importable()) == "lobsterai|uid-1"
    # 身份未知 → 空串（调用方回落凭证 ID），不能与别的账号撞同一个 scope
    assert provider.checkin_scope(_importable(uid="")) == ""


# ------------------------------------------------------------------ 登录

def test_start_auth_builds_paste_login_url():
    """门户只接受回环回调 → 走 paste 轨道（用户把地址栏链接粘回来）。"""
    provider = LobsteraiProvider()
    session = provider.start_auth("http://service/authorize")
    assert session.flow is AuthFlow.PASTE
    assert session.interval is None
    assert session.callback_url is None
    assert "source=electron" in (session.auth_url or "")
    uuid, first_keyfrom, upstream_state = session.state.split(":", 2)
    assert uuid and first_keyfrom.isdigit() and upstream_state


async def test_complete_callback_exchanges_code():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/exchange"
        body = json.loads(request.content)
        assert body["authCode"] == "CODE"
        assert body["uuid"] == "uuid-1"
        assert body["firstKeyfrom"] == "F1"
        return httpx.Response(200, json=envelope({
            "accessToken": "AT", "refreshToken": "RT", "expiresIn": 3600,
            "user": {"id": "u1", "nickname": "bob"}}))

    provider = LobsteraiProvider(client=_client(handler))
    data = await provider.complete_callback(
        "http://127.0.0.1:52641/auth/callback?code=CODE&state=S",
        "uuid-1:F1:S")
    assert data["accessToken"] == "AT"
    assert data["uid"] == "u1"
    await provider.aclose()


async def test_complete_callback_rejects_mismatched_state():
    """粘贴的回调不属于当前这次登录 → 拒绝（防止把另一次登录的凭证贴进来）。"""
    provider = LobsteraiProvider(client=_client(lambda _r: httpx.Response(200)))
    with pytest.raises(UpstreamProtocolViolation, match="state"):
        await provider.complete_callback(
            "http://127.0.0.1:52641/auth/callback?code=CODE&state=OTHER",
            "uuid-1:F1:S")
    await provider.aclose()


async def test_complete_callback_allows_missing_state():
    """回调没有 state（浏览器可能丢掉）时仍放行：code 本身是一次性的。"""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["authCode"] == "CODE"
        return httpx.Response(200, json=envelope({
            "accessToken": "AT", "expiresIn": 3600, "user": {"id": "u1"}}))

    provider = LobsteraiProvider(client=_client(handler))
    data = await provider.complete_callback(
        "http://127.0.0.1:52641/auth/callback?code=CODE", "uuid-1:F1:S")
    assert data["accessToken"] == "AT"
    await provider.aclose()


def test_split_state_handles_short_state():
    from src.provider.lobsterai import _split_state

    assert _split_state("a:b:c") == ("a", "b", "c")
    assert _split_state("a") == ("a", "", "")
    assert _split_state("") == ("", "", "")
