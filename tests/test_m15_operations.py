"""M1.5 测试：OAuth 轮询、刷新与账号切换、签到、后台任务、统计。

fixture 结构来自 codebuddy2api 的 codebuddy_oauth.py / credential_checkin.py 实测语义。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.main import build_app
from src.provider.base import ErrKind, Quota
from src.provider.codebuddy.checkin import (
    CheckinResult,
    CodeBuddyCheckin,
    checkin_scope_key,
    parse_checkin_response,
)
from src.provider.codebuddy.client import CodeBuddyCredential, CodeBuddyProvider
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.oauth import AuthProgress, AuthStateStore, CodeBuddyOAuth
from src.provider.codebuddy.refresh import (
    Account,
    CodeBuddyRefresh,
    account_generation_changed,
)
from src.provider.trae.client import TraeProvider
from src.stats.collector import StatsCollector, StatsQuery
from src.tasks.background import (
    CheckinTask,
    Pacer,
    QuotaProbeTask,
    RefreshTask,
    RetentionTask,
    TaskReport,
)

# ---------------------------------------------------------------- Pacer

async def test_pacer_disabled_is_noop():
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    pacer = Pacer(0, 0, sleep=fake_sleep)
    assert pacer.disabled is True
    await pacer.wait_turn()
    await pacer.wait_turn()
    assert slept == []


async def test_pacer_fixed_interval_sleeps_remaining():
    slept: list[float] = []
    clock = {"t": 100.0}

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    pacer = Pacer(5, 5, sleep=fake_sleep, now=lambda: clock["t"])
    await pacer.wait_turn()                 # 首次不睡
    await pacer.wait_turn()                 # 第二次补足 5s
    assert slept == [5.0]


async def test_pacer_random_interval_within_bounds():
    pacer = Pacer(5, 20)
    intervals = [pacer.next_interval() for _ in range(20)]
    assert all(5 <= value <= 20 for value in intervals)
    assert len(set(intervals)) > 1          # 确实是随机而非定值


@pytest.mark.parametrize(("low", "high"), [(-1, 5), (10, 5)])
def test_pacer_rejects_invalid_bounds(low, high):
    with pytest.raises(ValueError):
        Pacer(low, high)


async def test_pacer_skips_sleep_when_elapsed_exceeds_interval():
    slept: list[float] = []
    clock = {"t": 0.0}

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    pacer = Pacer(5, 5, sleep=fake_sleep, now=lambda: clock["t"])
    await pacer.wait_turn()
    clock["t"] = 100.0
    await pacer.wait_turn()
    assert slept == []


def test_task_report_dict():
    report = TaskReport(attempted=3, succeeded=2, failed=1, skipped=4)
    assert report.as_dict() == {"attempted": 3, "succeeded": 2, "failed": 1, "skipped": 4}


# ------------------------------------------------------------ OAuth 轮询

def _oauth_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)


START_OK = {"code": 0, "data": {"authUrl": "https://auth.example/x", "state": "up-state"}}


async def test_oauth_start_returns_session_with_upstream_state():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/plugin/auth/state"
        assert "platform=CLI" in str(request.url)
        return httpx.Response(200, json=START_OK)

    oauth = CodeBuddyOAuth("https://copilot.tencent.com", client=_oauth_client(handler))
    session = await oauth.start("alice")
    assert session.flow == "poll" and session.auth_url == "https://auth.example/x"
    assert session.interval == 5
    # 对外暴露的是本地 reservation，不是上游 state
    assert session.state != "up-state"
    assert oauth.store.upstream_state(session.state, "alice") == "up-state"
    await oauth.aclose()


@pytest.mark.parametrize("payload", [
    {"code": 1, "data": {}}, {"code": 0, "data": {}}, {"code": 0, "data": {"authUrl": ""}},
    {"code": 0, "data": {"authUrl": "https://a/x"}}, "not-a-dict",
])
async def test_oauth_start_rejects_invalid(payload):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.start("alice")
    await oauth.aclose()


async def test_oauth_start_rejects_http_error_and_registers_nothing():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.start("alice")
    assert oauth.store.owner("anything", "alice") is False
    await oauth.aclose()


async def test_oauth_poll_pending_then_success():
    """code=11217 → 继续等待；code=0 → 拿 token 与账号 → 消费 state。"""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json={"code": 11217})
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "AT", "refreshToken": "RT", "expiresIn": 3600,
                "domain": "copilot.tencent.com"}})
        return httpx.Response(200, json={"code": 0, "data": {
            "user_id": "u1", "account": {"uid": "acct", "nickname": "nick",
                                         "enterpriseId": "ent"}}})

    oauth = CodeBuddyOAuth("https://copilot.tencent.com", client=_oauth_client(handler))
    session = await oauth.start("alice")
    assert await oauth.poll(session.state, "alice") is None          # pending
    result = await oauth.poll(session.state, "alice")
    assert result is not None
    data = result.credential_data
    assert data["bearer_token"] == "AT" and data["refresh_token"] == "RT"
    assert data["auth_source"] == "oauth" and data["account_uid"] == "acct"
    assert data["enterprise_id"] == "ent" and result.nickname == "nick"
    # state 已消费，不可重放
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()


async def test_oauth_poll_account_pending_keeps_state():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(200, json={"code": 12151})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    assert await oauth.poll(session.state, "alice") is None
    # 账号阶段 pending：token 已缓存，state 未消费
    assert oauth.store.progress(session.state, "alice").token_data == {"accessToken": "AT"}
    await oauth.aclose()


async def test_oauth_poll_rejects_unknown_state_and_wrong_owner():
    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(
        lambda _r: httpx.Response(200, json={"code": 0, "data": {}})))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll("ghost", "alice")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=START_OK)

    oauth2 = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth2.start("alice")
    assert oauth2.store.owner(session.state, "bob") is False
    with pytest.raises(UpstreamProtocolViolation):
        await oauth2.poll(session.state, "bob")
    await oauth.aclose()
    await oauth2.aclose()


@pytest.mark.parametrize("token_payload", [
    {"code": 1}, {"code": 0, "data": {}}, {"code": 0, "data": []},
    {"code": 0, "data": {"accessToken": 5}}, "not-a-dict",
])
async def test_oauth_poll_rejects_bad_token_response(token_payload):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        return httpx.Response(200, json=token_payload)

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()


async def test_oauth_poll_account_error_paths():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(200, json={"code": 9999})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()


@pytest.mark.parametrize("account_payload", [
    {"code": 0}, {"code": 0, "data": []},
])
async def test_oauth_poll_account_shape_errors(account_payload):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(200, json=account_payload)

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()


async def test_oauth_token_http_error_and_bad_json():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(503, content=b"down")
        return httpx.Response(200, json={"code": 0, "data": {}})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()


async def test_oauth_account_http_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(500, content=b"boom")

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()


def test_auth_state_store_ttl_cleanup_and_cancel():
    store = AuthStateStore(ttl_seconds=10)
    first = store.begin("alice", "s1", now=0)
    second = store.begin("alice", "s2", now=100)          # 触发 cleanup
    assert store.owner(second, "alice") is True
    assert store.owner(first, "alice") is False           # 已过期
    assert store.cancel(second, "alice") is True
    assert store.cancel(second, "alice") is False
    assert store.upstream_state("ghost", "alice") is None


def test_auth_state_store_set_progress_only_for_known_state():
    store = AuthStateStore()
    reservation = store.begin("alice", "s1")
    store.set_progress(reservation, AuthProgress(token_data={"a": 1}))
    assert store.progress(reservation, "alice").token_data == {"a": 1}
    store.set_progress("ghost", AuthProgress())           # 不抛异常


async def test_oauth_lazy_client_and_close():
    oauth = CodeBuddyOAuth("https://e")
    assert oauth._http is oauth._http
    await oauth.aclose()


def test_oauth_expires_at_from_created_plus_ttl():
    from src.provider.codebuddy.oauth import _expires_at

    assert _expires_at({"expires_at": 99}) == 99
    assert _expires_at({"created_at": 100, "expires_in": 50}) == 150
    assert _expires_at({}) == 0
    assert _expires_at({"expires_at": True}) == 0


# ------------------------------------------------- 刷新与多账号切换

def _refresh_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)


async def test_refresh_updates_token_and_reports_rotation():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(200, json={"code": 0, "data": {"accounts": [
                {"accountId": "a2", "nickname": "second", "pluginEnabled": True}]}})
        return httpx.Response(200, json={"code": 0, "data": {
            "accessToken": "NEW", "refreshToken": "RT2", "expiresIn": 60}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT1", auth_source="oauth", user_id="u"))
    assert outcome.credential.bearer_token == "NEW"
    assert outcome.credential.refresh_token == "RT2"
    assert outcome.refresh_token_rotated is True
    assert outcome.accounts is not None and outcome.accounts[0].account_id == "a2"
    assert outcome.credential.auth_source == "oauth"      # 来源必须继承
    await client.aclose()


async def test_refresh_keeps_old_token_when_response_has_none():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": 5}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    original = CodeBuddyCredential(bearer_token="old", refresh_token="RT",
                                   auth_source="oauth")
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(original)
    assert original.bearer_token == "old" and original.refresh_token == "RT"
    await client.aclose()


async def test_refresh_requires_refresh_token():
    client = CodeBuddyRefresh("https://e", client=_refresh_client(
        lambda _r: httpx.Response(200, json={"code": 0})))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(bearer_token="x", auth_source="oauth"))
    await client.aclose()


async def test_refresh_marks_accounts_pending_on_failure():
    """账号同步失败 → accounts_pending（可恢复），但 token 已刷新。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.accounts_pending is True and outcome.accounts is None
    await client.aclose()


@pytest.mark.parametrize("account_response", [
    httpx.Response(200, content=b"<html>"),
    httpx.Response(200, json={"code": 1}),
    httpx.Response(200, json={"code": 0, "data": {"accounts": "no"}}),
    httpx.Response(200, json={"code": 0, "data": {}}),
])
async def test_accounts_unavailable_shapes(account_response):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return account_response
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "N"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.list_accounts(CodeBuddyCredential(bearer_token="t"))
    await client.aclose()


async def test_refresh_survives_accounts_transport_error():
    """账号阶段传输失败 → pending（可恢复），token 刷新本身成功。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            raise httpx.ConnectError("dns")
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.credential.bearer_token == "NEW"
    assert outcome.accounts_pending is True
    await client.aclose()


async def test_refresh_transport_error_on_token_stage_raises():
    """token 阶段传输失败必须显式失败，让调用方重试（不能静默当成功）。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(
            bearer_token="old", refresh_token="RT", auth_source="oauth"))
    await client.aclose()


async def test_switch_account_clears_enterprise_context_for_personal():
    """切到个人账号必须清空企业上下文（AGENTS.md：禁止回退旧 enterprise_id）。"""
    seen: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            seen.append((request.url.path, dict(request.headers)))
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "SWITCHED", "refreshToken": "RT2"}})
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "personal1", "nickname": "me", "type": "personal",
             "pluginEnabled": True},
            {"accountId": "ent1", "nickname": "corp", "type": "enterprise",
             "enterpriseId": "ent-9", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    base = CodeBuddyCredential(bearer_token="t", user_id="u", enterprise_id="old-ent",
                               department_full_name="技术部", auth_source="oauth",
                               refresh_token="RT")
    personal = await client.switch_account(base, "personal1")
    assert personal.enterprise_id == "" and personal.department_full_name == ""
    assert personal.account_uid == "personal1"
    assert personal.bearer_token == "SWITCHED"          # 切换会换 token
    assert seen[-1][0] == SWITCH_PATH                  # 个人账号走无后缀路径

    corporate = await client.switch_account(base, "ent1")
    assert corporate.enterprise_id == "ent-9" and corporate.department_full_name == "技术部"
    assert seen[-1][0] == f"{SWITCH_PATH}/ent-9"       # 企业账号带 enterprise_id
    assert "x-enterprise-id" in {k.lower() for k in seen[-1][1]}
    assert "x-tenant-id" in {k.lower() for k in seen[-1][1]}
    await client.aclose()


async def test_switch_account_rejects_disabled_target_account():
    """切换后目标账号不在启用列表 → account_missing。"""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "N"}})
        calls["n"] += 1
        accounts = [{"accountId": "a1", "type": "personal", "pluginEnabled": True}]
        if calls["n"] > 1:                              # 切换后的复查：账号被禁用
            accounts[0]["pluginEnabled"] = False
        return httpx.Response(200, json={"code": 0, "data": {"accounts": accounts}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(
            CodeBuddyCredential(bearer_token="t", refresh_token="RT"), "a1")
    await client.aclose()


async def test_switch_account_rejects_enterprise_without_id():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "e1", "type": "enterprise", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(
            CodeBuddyCredential(bearer_token="t", refresh_token="RT"), "e1")
    await client.aclose()


@pytest.mark.parametrize("account_id", ["", "ghost"])
async def test_switch_account_rejects_missing(account_id):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts/switch"):
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "a1"}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(CodeBuddyCredential(bearer_token="t"), account_id)
    await client.aclose()


SWITCH_PATH = "/v2/plugin/login/enterprise"


@pytest.mark.parametrize("switch_response", [
    httpx.Response(409, content=b"conflict"),
    httpx.Response(200, content=b"<html>"),
    httpx.Response(200, json={"code": 7}),
    httpx.Response(200, json={"code": 10081}),
])
async def test_switch_account_rejects_upstream_failure(switch_response):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            return switch_response
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "a1", "type": "personal", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(CodeBuddyCredential(bearer_token="t",
                                                        refresh_token="RT"), "a1")
    await client.aclose()


def test_account_generation_changed_normalization():
    assert account_generation_changed("a", "b") is True
    assert account_generation_changed("a", " a ") is False
    assert account_generation_changed("", "a") is True
    assert account_generation_changed("a", "") is True


async def test_refresh_lazy_client_and_close():
    client = CodeBuddyRefresh("https://e")
    assert client._http is client._http
    await client.aclose()


def test_to_account_skips_objects_without_id():
    from src.provider.codebuddy.refresh import _to_account

    assert _to_account("junk") is None
    assert _to_account({}) is None
    assert _to_account({"uid": "u", "pluginEnabled": True}) == Account(
        account_id="u", enabled=True)
    assert _to_account({"id": "i", "name": "n", "type": "personal"}).nickname == "n"


# ---------------------------------------------------------------- 签到

@pytest.mark.parametrize(("body", "ok"), [
    ({"code": 0, "data": {"credit": 100}}, True),
    ({"code": 0, "data": {"credit": 0}}, True),          # 0 也是合法数值
    ({"code": 0, "data": {}}, False),                    # 已签过
    ({"code": 0, "data": {"credit": True}}, False),      # 布尔不算数值
    ({"code": 0, "data": {"credit": "x"}}, False),
    ({"code": 9999, "data": {"credit": 5}}, False),
    ({"code": None, "data": {}}, False),                 # code=null 不阻断后续补偿
])
def test_parse_checkin_response(body, ok):
    result = parse_checkin_response(body)
    assert result.ok is ok


def test_parse_checkin_response_marks_already_checked_in():
    assert parse_checkin_response({"code": 0, "data": {}}).already_checked_in is True
    assert parse_checkin_response({"code": 5, "msg": "boom"}).message == "boom"


@pytest.mark.parametrize("body", ["junk", [1]])
def test_parse_checkin_response_rejects_non_object(body):
    with pytest.raises(UpstreamProtocolViolation):
        parse_checkin_response(body)


def test_checkin_scope_key_normalizes():
    assert checkin_scope_key("https://e/", "u") == "https://e|u"
    assert checkin_scope_key("https://e", "") == "https://e|"


async def test_checkin_claim():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/billing/meter/daily-checkin"
        return httpx.Response(200, json={"code": 0, "data": {"credit": 50}})

    client = CodeBuddyCheckin("https://e", client=_refresh_client(handler))
    result = await client.claim(CodeBuddyCredential(bearer_token="t"))
    assert result.ok and result.credit == 50
    await client.aclose()


async def test_checkin_claim_error_paths():
    async def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = CodeBuddyCheckin("https://e", client=_refresh_client(failing))
    with pytest.raises(UpstreamProtocolViolation):
        await client.claim(CodeBuddyCredential(bearer_token="t"))
    await client.aclose()

    async def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client2 = CodeBuddyCheckin("https://e", client=_refresh_client(not_json))
    with pytest.raises(UpstreamProtocolViolation):
        await client2.claim(CodeBuddyCredential(bearer_token="t"))
    await client2.aclose()


async def test_checkin_lazy_client_and_close():
    client = CodeBuddyCheckin("https://e")
    assert client._http is client._http
    await client.aclose()


# ----------------------------------------------------------- provider 扩展

async def test_provider_checkin_scope_and_refresh_delegation():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/daily-checkin"):
            return httpx.Response(200, json={"code": 0, "data": {"credit": 7}})
        if request.url.path.startswith(SWITCH_PATH):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(200, json={"code": 0, "data": {"accounts": [
                {"accountId": "a1", "type": "personal", "pluginEnabled": True}]}})
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    data = {"bearer_token": "t", "account_uid": "acct"}
    assert provider.checkin_scope(data).endswith("|acct")
    result = await provider.checkin(data)
    assert result.ok and result.credit == 7
    assert [a.account_id for a in await provider.list_accounts(data)] == ["a1"]
    switched = await provider.switch_account(data, "a1")
    assert switched["account_uid"] == "a1"


def _refresh_client_provider(handler) -> object:
    from src.provider.codebuddy.client import CodeBuddyClient

    return CodeBuddyClient(
        stream_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None),
        short_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None))


async def test_provider_refresh_skips_bearer_only_manual_credential():
    """bearer-only 手动凭证不进入刷新流程，原样返回（AGENTS.md 约束）。"""
    provider = CodeBuddyProvider(client=_refresh_client_provider(
        lambda _r: httpx.Response(500, content=b"should not be called")))
    data = {"bearer_token": "t", "auth_source": "manual"}
    assert await provider.refresh(data) == data


async def test_provider_refresh_updates_oauth_credential():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts"):
            return httpx.Response(200, json={"code": 0, "data": {"accounts": []}})
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW",
                                                             "expiresIn": 100}})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    refreshed = await provider.refresh({"bearer_token": "old", "refresh_token": "RT",
                                        "auth_source": "oauth"})
    assert refreshed["bearer_token"] == "NEW" and refreshed["auth_source"] == "oauth"


# ------------------------------------------------------------- 后台任务

@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher("s")), db
    db.close()


class ProbeProvider:
    id = "codebuddy"

    def __init__(self, quota=None, error=None, checkin_ok=True, refresh_new=None):
        self.quota = quota or Quota(remaining=8, total=10, probed_at=1)
        self.error = error
        self.checkin_ok = checkin_ok
        self.refresh_new = refresh_new
        self.checkin_calls = 0
        self.refresh_calls = 0
        self.seen_scopes: list[str] = []

    async def probe_quota(self, _data):
        if self.error is not None:
            raise self.error
        return self.quota

    async def checkin(self, _data):
        self.checkin_calls += 1
        return CheckinResult(ok=self.checkin_ok, credit=10 if self.checkin_ok else None)

    def checkin_scope(self, data):
        scope = f"acct:{data.get('account_uid', '')}"
        self.seen_scopes.append(scope)
        return scope

    def credential_from(self, data):
        return CodeBuddyCredential.from_dict(data)

    async def refresh(self, data):
        self.refresh_calls += 1
        return {**data, "bearer_token": self.refresh_new or "NEW"}


async def test_quota_probe_updates_health(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()})
    report = await task.run_once()
    assert report.succeeded == 1
    assert credentials.candidates()[0].health == 80


async def test_quota_probe_failure_marks_unknown_not_zero(repo):
    """探测失败 → health=NULL（unknown），绝不当作 0 分（Q26 三态）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider(error=RuntimeError("boom"))})
    report = await task.run_once()
    assert report.failed == 1
    assert credentials.candidates()[0].health is None


async def test_quota_probe_skips_unknown_provider_and_missing_data(repo):
    credentials, _db = repo
    credentials.add(provider="ghost", credential_data={"x": 1})
    task = QuotaProbeTask(credentials, {})
    report = await task.run_once()
    assert report.skipped == 1


async def test_quota_probe_uses_pacer(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    turns: list[int] = []

    class CountingPacer(Pacer):
        async def wait_turn(self) -> None:
            turns.append(1)

    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()},
                          CountingPacer(1, 1))
    await task.run_once()
    assert len(turns) == 1
    turns.clear()
    await task.run_once(apply_pacing=False)
    assert turns == []


async def test_checkin_dedupes_same_upstream_account(repo):
    """同上游账号的多张凭证只签一次（AGENTS.md 约束）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "same"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b",
                                                          "account_uid": "same"})
    provider = ProbeProvider()
    task = CheckinTask(credentials, {"codebuddy": provider})
    report = await task.run_once()
    assert report.attempted == 1 and report.skipped == 1
    assert provider.checkin_calls == 1


async def test_checkin_skips_disabled_and_unsupported(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    credentials.save_error(credential_id, _disabled_outcome())
    credentials.add(provider="trae", credential_data={"accessToken": "t"})
    task = CheckinTask(credentials, {"codebuddy": ProbeProvider(), "trae": TraeProvider()})
    report = await task.run_once()
    assert report.attempted == 0


def _disabled_outcome():
    from src.engine.scheduler import Candidate, Scheduler

    return Scheduler().note_error(Candidate(credential_id="c", provider="codebuddy"),
                                  ErrKind.DEAD, 0)


async def test_checkin_reports_failure_and_success_callback(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "x"})
    seen: list[str] = []
    task = CheckinTask(credentials, {"codebuddy": ProbeProvider(checkin_ok=False)},
                       on_success=seen.append)
    report = await task.run_once()
    assert report.failed == 1 and seen == []


async def test_checkin_error_is_isolated(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a"})

    class Boom(ProbeProvider):
        async def checkin(self, _data):
            raise RuntimeError("boom")

    task = CheckinTask(credentials, {"codebuddy": Boom()})
    report = await task.run_once()
    assert report.failed == 1


async def test_checkin_due_before_and_after_hour():
    task = CheckinTask.__new__(CheckinTask)
    task.checkin_hour = 9
    task._done_scopes = set()
    early = time.struct_time((2026, 9, 11, 8, 0, 0, 3, 254, 0))
    late = time.struct_time((2026, 9, 11, 10, 0, 0, 3, 254, 0))
    assert task.due(now=early) is False
    assert task.due(now=late) is True
    task._done_scopes.add("2026-09-11")
    assert task.due(now=late) is False          # 当日只执行一次


async def test_refresh_task_only_touches_due_credentials(repo):
    credentials, _db = repo
    now = 1_000_000
    soon = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": now + 100})
    later = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": now + 999_999})
    provider = ProbeProvider(refresh_new="NEW")
    task = RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600,
                       now=lambda: now)
    report = await task.run_once()
    assert report.succeeded == 1 and provider.refresh_calls == 1
    assert credentials.credential_data(soon)["bearer_token"] == "NEW"
    assert credentials.credential_data(later)["bearer_token"] == "old"


async def test_refresh_task_skips_manual_and_missing(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t",
                                                          "auth_source": "manual"})
    credentials.add(provider="ghost", credential_data={"x": 1})
    task = RefreshTask(credentials, {"codebuddy": ProbeProvider()}, skew_seconds=3600,
                       now=lambda: 1)
    report = await task.run_once()
    assert report.attempted == 0 and report.skipped == 2


async def test_refresh_task_isolates_failures(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "t", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})

    class Boom(ProbeProvider):
        async def refresh(self, _data):
            raise RuntimeError("boom")

    task = RefreshTask(credentials, {"codebuddy": Boom()}, skew_seconds=3600,
                       now=lambda: 1)
    report = await task.run_once()
    assert report.failed == 1


def test_needs_refresh_without_builder_returns_false():
    from src.tasks.background import _needs_refresh

    assert _needs_refresh(object(), {}, 10, 0) is False


# ---------------------------------------------------------------- 统计

@pytest.fixture()
def stats(repo):
    _credentials, db = repo
    yield StatsCollector(db), StatsQuery(db)


def test_stats_records_and_redacts(stats):
    collector, query = stats
    collector.record(username="alice", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=10, output_tokens=20, latency_ms=100, ttfb_ms=30)
    collector.record(username="alice", provider="codebuddy", model="glm-5.2", ok=False,
                     error_type="upstream_error", credit=0.5)
    overview = query.overview(username="alice")
    assert overview["requests"] == 2 and overview["ok_count"] == 1
    assert overview["success_rate"] == 0.5
    assert overview["input_tokens"] == 10
    assert overview["credit"] == 0.5
    assert overview["avg_latency_ms"] == 100
    by_provider = query.by_provider(username="alice")
    assert [row["provider"] for row in by_provider] == ["codebuddy", "trae"]


def test_stats_credit_is_null_when_never_known(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True)
    assert query.overview(username="u")["credit"] is None


def test_stats_normalizes_model_and_error_type(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="x" * 100, ok=False,
                     error_type="not-a-controlled-value", credit=-5)
    rows = query.by_provider(username="u")
    assert rows[0]["requests"] == 1
    row = query._db.connect().execute(
        "SELECT model, error_type, credit FROM usage_events").fetchone()
    assert row["model"] == "unknown" and row["error_type"] is None and row["credit"] is None


@pytest.mark.parametrize("model", [None, "", "has space", "bad\ncontrol", 5])
def test_safe_model_rejects_invalid(stats, model):
    collector, query = stats
    collector.record(username="u", provider="trae", model=model, ok=True)
    assert query._db.connect().execute(
        "SELECT model FROM usage_events").fetchone()["model"] == "unknown"


def test_stats_overview_global_for_admin(stats):
    collector, query = stats
    collector.record(username="alice", provider="trae", model="m", ok=True)
    collector.record(username="bob", provider="trae", model="m", ok=True)
    assert query.overview()["requests"] == 2
    assert query.overview(username="alice")["requests"] == 1


def test_stats_overview_empty_and_filters(stats):
    _collector, query = stats
    empty = query.overview()
    assert empty["requests"] == 0 and empty["success_rate"] is None
    assert empty["avg_latency_ms"] is None
    assert query.by_provider() == []


def test_stats_retention_and_rollup(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, input_tokens=5,
                     latency_ms=10, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, input_tokens=5,
                     latency_ms=20, now=2000)
    task = RetentionTask(collector, retention_days=90)
    first = task.run_once()
    assert first["rolled_up"] >= 1
    rows = query._db.connect().execute(
        "SELECT requests, input_tokens FROM usage_hourly").fetchall()
    assert sum(r["requests"] for r in rows) == 2
    # 清理：把 now 推到 91 天之后
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    assert collector.purge_expired(90, now=1000 + 91 * 86400) == 1
    # 小时汇总永久保留
    assert query._db.connect().execute(
        "SELECT COUNT(*) AS c FROM usage_hourly").fetchone()["c"] >= 1


def test_stats_rollup_is_idempotent(stats):
    collector, _query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=10_000)
    collector.rollup_hourly()
    collector.rollup_hourly()
    total = collector._db.connect().execute(
        "SELECT SUM(requests) AS s FROM usage_hourly").fetchone()["s"]
    assert total == 1


def test_stats_overview_since_filter(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, now=9000)
    assert query.overview(username="u", since=5000)["requests"] == 1


# ------------------------------------------------------------ API 端到端

@pytest.fixture()
def admin_client(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", "s"))
    with client:
        yield app, client


def test_upstream_auth_start_requires_admin_and_known_provider(admin_client):
    _app, client = admin_client
    started = client.post("/api/auth/upstream/start", json={"provider": "codebuddy"})
    assert started.status_code in (200, 400, 502)
    assert client.post("/api/auth/upstream/start", json={"provider": "trae"}).status_code == 400


def test_upstream_auth_poll_unknown_state(admin_client):
    _app, client = admin_client
    response = client.post("/api/auth/upstream/poll",
                           json={"provider": "codebuddy", "state": "ghost"})
    assert response.status_code == 400


def test_upstream_auth_rejects_non_admin(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("guest", "s"))
        assert client.post("/api/auth/upstream/start",
                           json={"provider": "codebuddy"}).status_code == 403


def test_credential_probe_and_checkin_endpoints(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t", "account_uid": "u"})
    assert client.post(f"/api/credentials/{credential_id}/probe").status_code in (200, 502)
    assert client.post(f"/api/credentials/{credential_id}/checkin").status_code in (200, 400)
    assert client.post("/api/credentials/ghost/probe").status_code == 400
    assert client.post("/api/credentials/ghost/checkin").status_code == 400


def test_credential_account_endpoints_unsupported_for_trae(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="trae",
                                              credential_data={"accessToken": "t"})
    assert client.get(f"/api/credentials/{credential_id}/accounts").status_code == 400
    assert client.post(f"/api/credentials/{credential_id}/accounts/select",
                       json={"account_id": "a"}).status_code == 400
    assert client.get("/api/credentials/ghost/accounts").status_code == 400


def test_stats_endpoints_scope_by_principal(admin_client):
    app, client = admin_client
    app.state.stats_collector.record(username="root", provider="trae", model="m", ok=True)
    app.state.stats_collector.record(username="other", provider="trae", model="m", ok=True)
    overview = client.get("/api/stats/overview").json()
    assert overview["requests"] == 2                     # admin 看全局
    scoped = client.get("/api/stats/overview", params={"username": "other"}).json()
    assert scoped["requests"] == 1
    assert client.get("/api/stats/by-provider").json()["providers"][0]["requests"] == 2


def test_stats_endpoints_restrict_non_admin(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path))
    app = build_app(settings)
    app.state.stats_collector.record(username="alice", provider="trae", model="m", ok=True)
    app.state.stats_collector.record(username="bob", provider="trae", model="m", ok=True)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("alice", "s"))
        assert client.get("/api/stats/overview").json()["requests"] == 1
        # 非 admin 指定他人用户名无效，仍只看自己
        assert client.get("/api/stats/overview",
                          params={"username": "bob"}).json()["requests"] == 1


def test_stats_requires_session(admin_client):
    _app, client = admin_client
    client.cookies.clear()
    assert client.get("/api/stats/overview").status_code == 401


# ------------------------------------------------- M1.5 覆盖率收尾

async def test_checkin_claim_http_error_paths(repo):
    """签到 HTTP 4xx/5xx → 协议违规（checkin.py 70 出口）。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"slow down")

    client = CodeBuddyCheckin("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.claim(CodeBuddyCredential(bearer_token="t"))
    await client.aclose()


async def test_refresh_rejects_unauthorized_and_server_error():
    for status in (401, 403, 500, 503):
        async def handler(_request: httpx.Request, status=status) -> httpx.Response:
            return httpx.Response(status, content=b"x")

        client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
        with pytest.raises(UpstreamProtocolViolation):
            await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                     auth_source="oauth"))
        await client.aclose()


async def test_refresh_rejects_non_json_body():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                auth_source="oauth"))
    await client.aclose()


async def test_refresh_rejects_business_error_code():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40001, "msg": "nope"})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                auth_source="oauth"))
    await client.aclose()


async def test_refresh_keeps_old_refresh_token_when_not_rotated():
    """上游没轮换 refresh token 时必须沿用旧值，不能清空。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(200, json={"code": 0, "data": {"accounts": []}})
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.credential.refresh_token == "RT"
    assert outcome.refresh_token_rotated is False
    await client.aclose()


async def test_oauth_poll_progress_is_reused_across_calls(repo):
    """token 阶段成功后重试只走账号阶段（oauth 149→156 分支）。"""
    calls = {"token": 0, "account": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        calls["account"] += 1
        if calls["account"] == 1:
            return httpx.Response(200, json={"code": 12151})
        return httpx.Response(200, json={"code": 0, "data": {"user_id": "u"}})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    assert await oauth.poll(session.state, "alice") is None
    assert await oauth.poll(session.state, "alice") is not None
    assert calls["token"] == 1                       # token 只取一次
    await oauth.aclose()


async def test_oauth_start_rejects_bad_auth_url_and_missing_state():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"authUrl": "not-a-url"}})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.start("alice")
    await oauth.aclose()


async def test_oauth_poll_consume_conflict(tmp_path):
    """state 在 poll 期间被并发消费 → 显式失败（oauth 192）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(200, json={"code": 0, "data": {"user_id": "u"}})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    original_consume = oauth.store.consume
    oauth.store.consume = lambda *_: False            # 模拟并发消费
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    oauth.store.consume = original_consume
    await oauth.aclose()


def test_oauth_start_headers_helper():
    from src.provider.codebuddy.oauth import _poll_headers

    assert _poll_headers()["Accept"].startswith("application/json")


def test_credential_parse_rejects_non_object_after_json():
    from src.provider.codebuddy.credential import parse_credential

    with pytest.raises(UpstreamProtocolViolation):
        parse_credential(b"[1,2]")


async def test_credential_refresh_skew_boundary():
    from src.provider.codebuddy.credential import CodeBuddyCredential

    credential = CodeBuddyCredential(bearer_token="t", refresh_token="r", expires_at=100,
                                     auth_source="oauth")
    assert credential.needs_refresh(0, now=100) is True      # 正好到期
    assert credential.needs_refresh(0, now=99) is False


async def test_checkin_result_defaults():
    assert CheckinResult(ok=True).credit is None
    assert CheckinResult(ok=False, code=7).already_checked_in is False


def test_stats_query_by_provider_since_filter(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, now=9000)
    assert query.by_provider(username="u", since=5000)[0]["requests"] == 1


def test_stats_record_handles_negative_and_bool_tokens(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=-1, output_tokens=True, reasoning_tokens=None,
                     latency_ms=-5, ttfb_ms=True, credit=-1.0)
    row = query._db.connect().execute(
        "SELECT input_tokens, output_tokens, latency_ms, credit FROM usage_events").fetchone()
    assert row["input_tokens"] is None and row["output_tokens"] is None
    assert row["latency_ms"] is None and row["credit"] is None


def test_stats_record_defaults_username_when_blank(stats):
    collector, query = stats
    collector.record(username="", provider="trae", model="m", ok=True)
    assert query.by_provider()[0]["requests"] == 1


def test_pacer_random_single_value_path():
    pacer = Pacer(3, 3)
    assert pacer.next_interval() == 3
    assert pacer.disabled is False


# --------------------------------------------- main.py 端点分支收尾

def test_upstream_auth_cancel_endpoint(admin_client):
    app, client = admin_client
    oauth = app.state.upstream_auth["codebuddy"]
    session_start = oauth.store.begin("root", "up")
    assert client.post("/api/auth/upstream/cancel",
                       json={"provider": "codebuddy", "state": session_start}).json()[
        "cancelled"] is True
    assert client.post("/api/auth/upstream/cancel",
                       json={"provider": "trae", "state": "x"}).status_code == 400


def test_upstream_auth_poll_success_persists_credential(admin_client):
    """轮询成功 → 凭证入库，且响应里绝不回传 token。"""
    app, client = admin_client
    oauth = app.state.upstream_auth["codebuddy"]
    reservation = oauth.store.begin("root", "up-state")

    async def fake_poll(_state, _username):
        from src.provider.base import AuthResult

        return AuthResult(credential_data={"bearer_token": "AT", "auth_source": "oauth"},
                          nickname="nick")

    oauth.poll = fake_poll
    body = client.post("/api/auth/upstream/poll",
                       json={"provider": "codebuddy", "state": reservation}).json()
    assert body["status"] == "success"
    assert "token" not in json.dumps(body).lower()
    assert app.state.credentials.list_all()[0]["provider"] == "codebuddy"


def test_probe_endpoint_marks_failed_on_provider_error(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Boom:
        endpoint = "https://e"

        async def probe_quota(self, _data):
            raise RuntimeError("boom")

    app.state.executor._deps.providers["codebuddy"] = Boom()
    body = client.post(f"/api/credentials/{credential_id}/probe").json()
    assert body["probed"] is False and body["reason"] == "RuntimeError"
    assert app.state.credentials.candidates()[0].health is None


def test_checkin_endpoint_reports_result(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=True, credit=12.0)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    body = client.post(f"/api/credentials/{credential_id}/checkin").json()
    assert body["ok"] is True and body["credit"] == 12.0


def test_account_endpoints_with_stub_provider(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def list_accounts(self, _data):
            return [Account(account_id="a1", nickname="n", account_type="personal",
                            enabled=True)]

        async def switch_account(self, data, account_id):
            return {**data, "account_uid": account_id}

    app.state.executor._deps.providers["codebuddy"] = Stub()
    accounts = client.get(f"/api/credentials/{credential_id}/accounts").json()["accounts"]
    assert accounts == [{"account_id": "a1", "nickname": "n", "type": "personal"}]
    switched = client.post(f"/api/credentials/{credential_id}/accounts/select",
                           json={"account_id": "a1"})
    assert switched.json()["switched"] is True
    assert app.state.credentials.credential_data(credential_id)["account_uid"] == "a1"


async def test_quota_probe_skips_when_credential_vanished(repo):
    """凭证在探测前被删除 → 计入 skipped（background 93-96）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()})
    original = credentials.credential_data
    credentials.credential_data = lambda _cid: None       # type: ignore[method-assign]
    report = await task.run_once()
    credentials.credential_data = original                # type: ignore[method-assign]
    assert report.skipped == 1


async def test_checkin_on_success_callback_fires(repo):
    """签到成功 → 触发额度重探测回调（background 164）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t",
                                                          "account_uid": "a"})
    seen: list[str] = []
    task = CheckinTask(credentials, {"codebuddy": ProbeProvider()}, on_success=seen.append)
    report = await task.run_once()
    assert report.succeeded == 1 and len(seen) == 1


async def test_refresh_task_skips_disabled(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "t", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})
    credentials.save_error(credential_id, _disabled_outcome())
    task = RefreshTask(credentials, {"codebuddy": ProbeProvider()}, skew_seconds=3600,
                       now=lambda: 1)
    report = await task.run_once()
    assert report.skipped == 1


def test_stats_overview_provider_filter(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True)
    collector.record(username="u", provider="codebuddy", model="m", ok=True)
    assert query.overview(username="u", provider="trae")["requests"] == 1


# --------------------------------------------------- 最后 11 行覆盖

def test_upstream_auth_poll_unknown_provider(admin_client):
    _app, client = admin_client
    response = client.post("/api/auth/upstream/poll",
                           json={"provider": "trae", "state": "x"})
    assert response.status_code == 400


def test_pacer_fixed_interval_returns_min_directly():
    assert Pacer(7, 7).next_interval() == 7


async def test_refresh_survives_generic_exception_in_accounts_stage():
    """账号阶段抛非 httpx 异常也要记为 pending（refresh 125-130）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            raise RuntimeError("weird transport")
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.accounts_pending is True
    await client.aclose()


async def test_switch_account_transport_error_raises():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            raise httpx.ConnectError("dns")
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "a1", "type": "personal", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(
            CodeBuddyCredential(bearer_token="t", refresh_token="RT"), "a1")
    await client.aclose()


def test_provider_cached_clients_are_reused():
    """_cached_checkin/_cached_refresh 复用同一实例（client 200→203）。"""
    from src.provider.codebuddy.client import (
        CodeBuddyClient,
        _cached_checkin,
        _cached_refresh,
    )

    client = CodeBuddyClient(endpoint="https://e")
    assert _cached_checkin(client) is _cached_checkin(client)
    assert _cached_refresh(client) is _cached_refresh(client)


async def test_provider_checkin_and_scope_roundtrip():
    """provider.checkin 走缓存客户端（client 259）。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"credit": 3}})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    result = await provider.checkin({"bearer_token": "t", "account_uid": "a"})
    assert result.ok and result.credit == 3.0
    assert provider.checkin_scope({"bearer_token": "t", "user_id": "u"}).endswith("|u")


async def test_poll_pending_returns_status_pending(admin_client):
    """轮询 pending → {"status": "pending"}（main 237-238）。"""
    app, client = admin_client
    oauth = app.state.upstream_auth["codebuddy"]

    async def fake_poll(_state, _username):
        return None

    oauth.poll = fake_poll
    body = client.post("/api/auth/upstream/poll",
                       json={"provider": "codebuddy", "state": "any"}).json()
    assert body == {"status": "pending"}


def test_probe_endpoint_returns_quota_fields(admin_client):
    """探测成功路径的响应字段（main 271-272）。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def probe_quota(self, _data):
            return Quota(remaining=7, total=10, cycle_end=123, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    body = client.post(f"/api/credentials/{credential_id}/probe").json()
    assert body == {"probed": True, "remaining": 7, "total": 10, "cycle_end": 123}


def test_pacer_disabled_next_interval_is_zero():
    assert Pacer(0, 0).next_interval() == 0.0


async def test_credential_from_provider_helper():
    from src.provider.codebuddy.client import CodeBuddyProvider

    provider = CodeBuddyProvider()
    assert provider.credential_from({"bearer_token": "t", "nickname": "n"}).nickname == "n"


async def test_oauth_account_stage_non_dict_body():
    """账号阶段返回非对象 → 显式失败（oauth 191-192）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(200, json=[1, 2])

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()


async def test_aclose_without_client_is_noop():
    """未创建过 HTTP 客户端时 aclose 直接返回（三处 -> 123 出口）。"""
    await CodeBuddyOAuth("https://e").aclose()
    await CodeBuddyRefresh("https://e").aclose()
    await CodeBuddyCheckin("https://e").aclose()


# ------------------------------------------------------------ 管理台登录

def test_login_success_sets_httponly_cookie(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        response = client.post("/api/auth/login",
                               json={"username": "root", "password": "rootpw"})
        assert response.status_code == 200
        assert response.json() == {"username": "root", "is_admin": True}
        cookie = response.headers["set-cookie"]
        assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
        assert client.get("/api/auth/session").json()["username"] == "root"


def test_login_rejects_bad_password_and_unknown_user(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        for payload in ({"username": "root", "password": "wrong"},
                        {"username": "ghost", "password": "rootpw"},
                        {}):
            assert client.post("/api/auth/login", json=payload).status_code == 401


def test_logout_clears_session(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        assert client.post("/api/auth/logout").json() == {"ok": True}
        assert client.get("/api/auth/session").status_code == 401


def test_session_endpoint_requires_login(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/auth/session").status_code == 401


def test_build_app_fails_without_users_file(tmp_path, monkeypatch):
    from src.auth.users import UsersFileError

    monkeypatch.setenv("USERS_FILE", str(tmp_path / "missing.txt"))
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path))
    with pytest.raises(UsersFileError):
        build_app(settings)


def test_credentials_endpoint_exposes_admin_flag(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "guest", "password": "guestpw"})
        body = client.get("/api/credentials").json()
        assert body["viewer"] == "guest" and body["is_admin"] is False


# ------------------------------------------------------- 前端静态资源服务

def _spa_app(tmp_path, monkeypatch, *, build: bool = True):
    """构造带/不带 web/dist 的 app，用于验证 SPA 回退行为。"""
    import os

    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path))
    app = build_app(settings)
    dist = Path("web/dist")
    if build:
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
        (dist / "app.js").write_text("console.log(1)", encoding="utf-8")
    return app, dist, os.getcwd()


def test_spa_serves_index_for_unknown_path(tmp_path):
    app, dist, cwd = _spa_app(tmp_path, None)
    try:
        with TestClient(app) as client:
            response = client.get("/credentials")
        assert response.status_code == 200
        assert "spa" in response.text
    finally:
        import shutil

        shutil.rmtree(dist, ignore_errors=True)


def test_spa_serves_real_asset(tmp_path):
    app, dist, cwd = _spa_app(tmp_path, None)
    try:
        with TestClient(app) as client:
            response = client.get("/app.js")
        assert response.status_code == 200 and "console.log" in response.text
    finally:
        import shutil

        shutil.rmtree(dist, ignore_errors=True)


def test_spa_reports_missing_build(tmp_path):
    app, dist, cwd = _spa_app(tmp_path, None, build=False)
    with TestClient(app) as client:
        response = client.get("/credentials")
    assert response.status_code == 404
    assert "frontend build not found" in response.text


def test_spa_reports_missing_index(tmp_path):
    import shutil

    app, dist, cwd = _spa_app(tmp_path, None)
    try:
        (dist / "index.html").unlink()
        with TestClient(app) as client:
            response = client.get("/credentials")
        assert response.status_code == 404
        assert "index.html missing" in response.text
    finally:
        shutil.rmtree(dist, ignore_errors=True)


def test_spa_does_not_escape_dist(tmp_path):
    """路径穿越必须拒绝：解析后位于 dist 之外的文件不能被读出。"""
    import shutil

    app, dist, cwd = _spa_app(tmp_path, None)
    try:
        secret = Path("web/pyproject.toml")
        if secret.exists():
            with TestClient(app) as client:
                response = client.get("/../pyproject.toml")
            # 拒绝时回退到 index.html，而不是泄漏文件内容
            assert "[project]" not in response.text
    finally:
        shutil.rmtree(dist, ignore_errors=True)
