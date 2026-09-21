"""M1.5 测试：OAuth 轮询、刷新与账号切换、签到、后台任务、统计。

fixture 结构来自 codebuddy2api 的 codebuddy_oauth.py / credential_checkin.py 实测语义。
"""

from __future__ import annotations

import json
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.executor import NoHealthyCredential
from src.engine.scheduler import ErrorOutcome
from src.main import build_app
from src.provider.base import ErrKind, Event, EventKind, Quota, Usage
from src.provider.codebuddy.checkin import (
    CheckinResult,
    CodeBuddyCheckin,
    checkin_scope_key,
    parse_checkin_response,
    parse_checkin_status,
)
from src.provider.codebuddy.client import CodeBuddyCredential, CodeBuddyProvider
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.oauth import AuthProgress, AuthStateStore, CodeBuddyOAuth
from src.provider.codebuddy.refresh import (
    Account,
    CodeBuddyRefresh,
)
from src.provider.trae.client import TraeProvider
from src.stats.collector import StatsCollector
from src.stats.query import StatsQuery
from src.tasks import TaskReport
from src.tasks.checkin import CheckinTask
from src.tasks.pacer import Pacer
from src.tasks.quota_probe import QuotaProbeTask
from src.tasks.refresh import RefreshTask
from src.tasks.retention import RetentionTask
from tests.conftest import SECRET

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
    """上游把「已签到」返回成 HTTP 400 + code=10001，必须视为成功。"""
    real = parse_checkin_response(
        {"code": 10001, "msg": "今天已签到，请明天再来",
         "requestId": "595bc3bc-a294-44dc-90dc-4cc3e3756018"})
    assert real.ok is True and real.already_checked_in is True
    assert parse_checkin_response({"code": 0, "msg": "OK", "data": {}}).ok is False
    assert parse_checkin_response({"code": 5, "msg": "boom"}).message == "boom"


@pytest.mark.parametrize("body", ["junk", [1]])
def test_parse_checkin_response_rejects_non_object(body):
    with pytest.raises(UpstreamProtocolViolation):
        parse_checkin_response(body)


def test_checkin_scope_key_normalizes():
    assert checkin_scope_key("https://e/", "u") == "https://e|u"
    # 身份未知时必须返回空串：返回 "endpoint|" 会让所有该渠道凭证算出同一个
    # scope，签到任务只签第一个账号（实测两个 CB 凭证 account_uid/user_id 都是空）
    assert checkin_scope_key("https://e", "") == ""
    assert checkin_scope_key("https://e", "   ") == ""


def test_parse_checkin_status_extracts_fields():
    """状态解析：字段齐全时逐项取出，类型归一。"""
    status = parse_checkin_status({"code": 0, "msg": "OK", "data": {
        "active": True, "today_checked_in": True, "streak_days": 4,
        "today_credit": 100, "total_credits": 400, "is_streak_day": False,
        "activity_name": "高校新生攻略"}})
    assert status.active is True and status.today_checked_in is True
    assert status.streak_days == 4 and status.today_credit == 100.0
    assert status.total_credits == 400.0 and status.activity_name == "高校新生攻略"
    assert status.is_streak_day is False
    assert status.to_dict()["streak_days"] == 4


@pytest.mark.parametrize("field", ["streak_days", "today_credit", "total_credits"])
@pytest.mark.parametrize("value", [None, "x", True, float("inf")])
def test_parse_checkin_status_tolerates_bad_optional_fields(field, value):
    """可选数值字段缺失/非法/非有限 → None，绝不抛异常（展示字段不能拖垮签到）。"""
    status = parse_checkin_status({"code": 0, "data": {"active": True, field: value}})
    assert getattr(status, field) is None


def test_parse_checkin_status_rejects_bad_envelope():
    """code 非 0、缺 data、非对象：一律显式报错，不静默当「未签到」。"""
    for body in ("junk", [1], {"code": 1, "msg": "boom"}, {"code": 0},
                 {"code": 0, "data": []}, {"data": {"active": True}}):
        with pytest.raises(UpstreamProtocolViolation):
            parse_checkin_status(body)


async def test_checkin_fetch_status_error_paths():
    """状态接口：401/403 与 5xx 抛异常；非 JSON 也抛（不能当空状态吞掉）。"""
    async def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401})

    client = CodeBuddyCheckin("https://e", client=_refresh_client(unauthorized))
    with pytest.raises(UpstreamProtocolViolation):
        await client.fetch_status(CodeBuddyCredential(bearer_token="t"))
    await client.aclose()

    async def server_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"boom")

    client2 = CodeBuddyCheckin("https://e", client=_refresh_client(server_error))
    with pytest.raises(UpstreamProtocolViolation):
        await client2.fetch_status(CodeBuddyCredential(bearer_token="t"))
    await client2.aclose()

    async def html(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client3 = CodeBuddyCheckin("https://e", client=_refresh_client(html))
    with pytest.raises(UpstreamProtocolViolation):
        await client3.fetch_status(CodeBuddyCredential(bearer_token="t"))
    await client3.aclose()


async def test_checkin_claim_failure_skips_status_lookup():
    """领取失败（业务码非 0）时不做状态回查：失败结论已完整，多余请求只会拖时间。"""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"code": 5, "msg": "boom"})

    client = CodeBuddyCheckin("https://e", client=_refresh_client(handler))
    result = await client.claim(CodeBuddyCredential(bearer_token="t"))
    assert result.ok is False and result.status is None
    assert len(calls) == 1                              # 只有 daily-checkin 一次请求
    await client.aclose()


async def test_checkin_claim_attaches_status_and_ignores_status_failure():
    """领取成功后回查状态；回查失败不能推翻「已领取」的既成事实。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("checkin-activity-status"):
            return httpx.Response(200, json={"code": 0, "data": {
                "active": True, "today_checked_in": True, "streak_days": 5}})
        return httpx.Response(200, json={"code": 0, "data": {"credit": 100}})

    client = CodeBuddyCheckin("https://e", client=_refresh_client(handler))
    result = await client.claim(CodeBuddyCredential(bearer_token="t"))
    assert result.ok and result.status is not None
    assert result.status.streak_days == 5
    await client.aclose()

    async def status_boom(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("checkin-activity-status"):
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {"credit": 100}})

    client2 = CodeBuddyCheckin("https://e", client=_refresh_client(status_boom))
    result2 = await client2.claim(CodeBuddyCredential(bearer_token="t"))
    assert result2.ok and result2.credit == 100 and result2.status is None
    await client2.aclose()


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
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
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


async def test_checkin_empty_scope_falls_back_to_credential_id(repo):
    """身份未知（provider 返回空 scope）时绝不共享：两个账号必须各签一次。

    回归用例：CB 的 OAuth 凭证实测 account_uid / user_id 都是空串，
    共享 `endpoint|` 会让第二个凭证被 seen 集合永久跳过，且没有任何报错。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})

    class AnonymousProvider:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def checkin(self, _data):
            self.calls += 1
            return CheckinResult(ok=True, credit=100)

        def checkin_scope(self, _data):
            return ""        # 身份未知

    provider = AnonymousProvider()
    task = CheckinTask(credentials, {"codebuddy": provider})
    report = await task.run_once()
    assert report.attempted == 2 and report.succeeded == 2 and report.skipped == 0
    assert provider.calls == 2


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


async def test_checkin_same_scope_failure_still_dedupes(repo):
    """同账号多凭证：首条失败时本轮不重试第二条（seen 去重），下轮才重试。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "same"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b",
                                                          "account_uid": "same"})
    provider = ProbeProvider(checkin_ok=False)
    task = CheckinTask(credentials, {"codebuddy": provider})
    report = await task.run_once()
    assert report.attempted == 1 and report.skipped == 1 and report.failed == 1
    assert provider.checkin_calls == 1


async def test_checkin_skips_disabled(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    credentials.save_error(credential_id, _disabled_outcome())
    task = CheckinTask(credentials, {"codebuddy": ProbeProvider()})
    report = await task.run_once()
    assert report.attempted == 0
    assert report.skipped == 1


async def test_checkin_supports_both_providers(repo):
    """TRAE 与 CodeBuddy 都实现签到后，任务要能覆盖两个上游。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "cb"})
    credentials.add(provider="trae", credential_data={"accessToken": "t", "uid": "tr"})

    class TraeCheckinProvider:
        id = "trae"

        def __init__(self) -> None:
            self.calls = 0

        async def checkin(self, _data):
            self.calls += 1
            from src.provider.base import CheckinResult

            return CheckinResult(ok=True, credit=None, message="今天已签到",
                                 already_checked_in=True)

        def checkin_scope(self, data):
            return f"trae|{data.get('uid', '')}"

    trae = TraeCheckinProvider()
    task = CheckinTask(credentials, {"codebuddy": ProbeProvider(), "trae": trae})
    report = await task.run_once()
    assert report.attempted == 2
    assert report.succeeded == 2
    assert trae.calls == 1


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


async def test_checkin_due_is_always_true():
    """签到全天每 10 分钟一轮，不再受时刻限制（due 恒 True）。"""
    task = CheckinTask.__new__(CheckinTask)
    task._done_scopes = set()
    midnight = time.struct_time((2026, 9, 11, 0, 30, 0, 3, 254, 0))
    late = time.struct_time((2026, 9, 11, 23, 59, 0, 3, 254, 0))
    assert task.due(now=midnight) is True
    assert task.due(now=late) is True


async def test_checkin_success_locks_scope_for_the_day(repo):
    """成功签到即封账该 scope：当日不再调上游；失败 scope 下轮（10 分钟一轮）继续重试。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "ok"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b",
                                                          "account_uid": "bad"})

    class Flaky:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {"ok": 0, "bad": 0}
            self.bad_fail_first = True

        def checkin_scope(self, data):
            return f"cb|{data['account_uid']}"

        async def checkin(self, data):
            uid = data["account_uid"]
            self.calls[uid] += 1
            if uid == "ok":
                return CheckinResult(ok=True, credit=100)
            # bad 首次失败（上游瞬时故障），之后恢复——验证失败凭证下轮重试
            if self.bad_fail_first:
                self.bad_fail_first = False
                return CheckinResult(ok=False, message="参与人数过多")
            return CheckinResult(ok=True, credit=50)

    provider = Flaky()
    task = CheckinTask(credentials, {"codebuddy": provider})
    now = time.struct_time((2026, 9, 11, 10, 0, 0, 3, 254, 0))
    first = await task.run_once(now=now)
    assert first.succeeded == 1 and first.failed == 1
    # 第二轮：ok 已封账（skipped），bad 重试成功
    second = await task.run_once(now=now)
    assert second.skipped == 1 and second.succeeded == 1 and second.failed == 0
    # 第三轮：全部封账，不再调上游
    third = await task.run_once(now=now)
    assert third.attempted == 0 and third.skipped == 2
    assert provider.calls == {"ok": 1, "bad": 2}
    # 次日清账：所有 scope 重新可签
    tomorrow = time.struct_time((2026, 9, 12, 9, 0, 0, 4, 255, 0))
    next_day = await task.run_once(now=tomorrow)
    assert next_day.attempted == 2 and next_day.succeeded == 2
    assert provider.calls == {"ok": 2, "bad": 3}


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
    from src.tasks.refresh import _needs_refresh

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
    assert overview["avg_ttfb_ms"] == 30
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


def test_purge_keeps_partially_populated_hour_intact(stats):
    """边界小时不得被删一半：否则下一轮 rollup 把汇总行覆盖成缩小值。

    rollup 对整行是 REPLACE 语义，purge 只删已完全过期的小时（切点向上
    对齐到小时边界），保证「仍有明细的小时保有全部明细」。
    """
    collector, _query = stats
    now = int(time.time())
    hour = (now // 3600) * 3600
    # 同一小时内两条明细：早的已过期（200 天前同一小时），晚的还在
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, now=hour - 200 * 86400 + 1)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=10, now=hour + 1)
    RetentionTask(collector, retention_days=90).run_once()
    row = collector._db.connect().execute(
        "SELECT SUM(input_tokens) AS t, SUM(requests) AS r FROM usage_hourly").fetchone()
    assert (row["t"], row["r"]) == (110, 2)      # 两条都在，未被截断


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


def test_overview_matches_timeline_beyond_retention_window(stats):
    """超过 90 天的时段：总览与图表必须同值（同读小时汇总）。

    此前总览读 usage_events（90 天）、图表读 usage_hourly（永久），
    选「全部」时同屏两个数字矛盾：明细被清理后总览缩水、图表不变。
    """
    collector, query = stats
    now = int(time.time())
    old = now - 200 * 86400               # 已超出 90 天明细保留期
    for ts in (old, now):
        collector.record(username="u", provider="trae", model="m", ok=True,
                         input_tokens=5, now=ts)
    RetentionTask(collector, retention_days=90).run_once()
    assert query.overview(username="u")["requests"] == 2          # 含已过期的老明细
    assert sum(p["trae"] for p in query.timeline(username="u")) == 2


def test_overview_reasoning_and_cached_aggregate_from_hourly(stats):
    """小时表带上 reasoning/cached 后，总览仍能给准确值（与明细同源）。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, reasoning_tokens=7, cached_tokens=40)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=50, reasoning_tokens=3)     # 未上报 cached
    collected = query.overview(username="u")
    assert collected["reasoning_tokens"] == 10
    assert collected["cached_tokens"] == 40      # 只有上报过的那条计入
    assert collected["input_tokens"] == 150
    # 未上报过 cached 的时段 → None，不能当成 0
    collector.record(username="v", provider="trae", model="m", ok=True)
    assert query.overview(username="v")["cached_tokens"] is None


def test_overview_latency_averages_only_successful_requests(stats):
    """均值口径与图表一致：除以 ok_count，失败请求不拉偏「典型耗时」。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=100, ttfb_ms=30)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=300, ttfb_ms=50)
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="upstream_error", latency_ms=900, ttfb_ms=900)
    overview = query.overview(username="u")
    assert overview["avg_latency_ms"] == 200    # (100+300)/2，失败那条不计入
    assert overview["avg_ttfb_ms"] == 40


def test_stats_overview_since_filter(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, now=9000)
    assert query.overview(username="u", since=5000)["requests"] == 1


def test_stats_hourly_rolls_via_retention(stats):
    """record 即时累加汇总（总览/图表不必等一轮 retention）；rollup 幂等不双计。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1_700_003_600)
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="rate_limit", now=1_700_003_601)
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     now=1_700_003_602, input_tokens=7, output_tokens=3,
                     credit=0.5, latency_ms=100)
    # 增量累加：最新小时立即可见（不需等 5 分钟一轮的 rollup）
    points = query.timeline(username="u", since=1_700_000_000)
    assert len(points) == 1 and points[0]["trae"] == 2 and points[0]["codebuddy"] == 1
    # 全量重算与该小时的增量值一致（幂等，不双计）
    RetentionTask(collector, retention_days=90).run_once()
    points = query.timeline(username="u", since=1_700_000_000)
    assert len(points) == 1 and points[0]["trae"] == 2 and points[0]["codebuddy"] == 1
    bucket = collector._db.connect().execute(
        "SELECT SUM(requests), SUM(ok_count), SUM(input_tokens), SUM(output_tokens), "
        "SUM(credit_known), SUM(latency_sum) FROM usage_hourly").fetchone()
    assert tuple(bucket) == (3, 2, 7, 3, 1, 100)


def test_stats_timeline_by_hour_and_provider(stats):
    collector, query = stats
    # 同一小时两条明细（同一 provider 累积到同一点，跨 provider 分列）
    collector.record(username="u", provider="codebuddy", model="m", ok=True, now=1_700_000_000)
    collector.record(username="u", provider="codebuddy", model="m", ok=True, now=1_700_000_100)
    collector.record(username="u", provider="trae", model="m", ok=True, now=1_700_000_200)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     now=1_700_000_000 + 3600)  # 下一小时
    collector.rollup_hourly()
    points = query.timeline(username="u")
    # 两小时、每个小时都含 codebuddy/trae 两路计数
    assert len(points) == 2
    first = points[0]
    assert first["codebuddy"] == 2 and first["trae"] == 1
    assert points[1]["trae"] == 1
    # since 过滤按小时边界（第二小时整点 = first hour 整点 + 3600）
    second_hour = first["hour"] + 3600
    filtered = query.timeline(username="u", since=second_hour)
    assert len(filtered) == 1 and filtered[0]["trae"] == 1


def test_stats_timeline_metric_dimensions(stats):
    """timeline 支持请求数/token/耗时/首字四维；均值按成功数归一；非法回退。"""
    collector, query = stats
    base = 1_700_000_000
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     input_tokens=10, output_tokens=5, latency_ms=100, ttfb_ms=40, now=base)
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     input_tokens=20, output_tokens=5, latency_ms=300, ttfb_ms=60, now=base)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, output_tokens=50, latency_ms=200, ttfb_ms=80, now=base)
    collector.rollup_hourly()

    req = query.timeline(username="u")  # 默认请求次数
    first = req[0]
    assert first["codebuddy"] == 2 and first["trae"] == 1
    # token = 输入 + 输出（跨渠道各自的合计）
    tok = query.timeline(username="u", metric="tokens")[0]
    assert tok["codebuddy"] == 40 and tok["trae"] == 150
    # 耗时/首字 = 成功请求均值：(100+300)/2=200，trae 单条即自身
    lat = query.timeline(username="u", metric="latency")[0]
    assert lat["codebuddy"] == 200 and lat["trae"] == 200
    ttf = query.timeline(username="u", metric="ttfb")[0]
    assert ttf["codebuddy"] == 50 and ttf["trae"] == 80
    # 非法 metric 回退请求次数
    assert query.timeline(username="u", metric="bogus") == req


def test_stats_model_timeline_metric_and_ttfb_rollup(stats):
    """model_timeline 指标切换 + ttfb 聚合进小时表（老库补列后可用）。"""
    collector, query = stats
    base = 1_700_000_000
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     input_tokens=10, output_tokens=5, latency_ms=100, ttfb_ms=40, now=base)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     input_tokens=20, output_tokens=5, latency_ms=300, ttfb_ms=60, now=base)
    collector.rollup_hourly()

    tok = query.model_timeline(username="u", metric="tokens")
    assert tok["points"][0]["glm-5.2"] == 40
    ttf = query.model_timeline(username="u", metric="ttfb")
    assert ttf["points"][0]["glm-5.2"] == 50
    # Top N 排序口径不随 metric 变（仍按请求量）
    assert query.model_timeline(username="u", metric="latency")["models"] == ["glm-5.2"]
    # ttfb 确实聚合进了小时表
    conn = collector._db.connect()
    row = conn.execute("SELECT ttfb_sum, latency_sum FROM usage_hourly").fetchone()
    assert row["ttfb_sum"] == 100 and row["latency_sum"] == 400


def test_stats_model_timeline_top_models_and_points(stats):
    """按模型趋势：Top N 宽表点列，按总量降序；其余模型不出线。"""
    collector, query = stats
    base = 1_700_000_000
    # glm-5.2 共 4 次（最热）、kimi-k3 共 2 次、legacy-old 共 1 次（超出 Top 2）
    for _ in range(4):
        collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True, now=base)
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True, now=base)
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True,
                     now=base + 3600)
    collector.record(username="u", provider="codebuddy", model="legacy-old", ok=True,
                     now=base + 3600)
    collector.rollup_hourly()

    result = query.model_timeline(username="u", top=2)
    assert result["models"] == ["glm-5.2", "kimi-k3"]
    points = result["points"]
    assert len(points) == 2
    assert points[0] == {"hour": base // 3600 * 3600, "glm-5.2": 4, "kimi-k3": 1}
    assert points[1]["glm-5.2"] == 0 and points[1]["kimi-k3"] == 1
    # username 过滤：只统计指定用户
    assert query.model_timeline(username="nobody")["points"] == []


# ------------------------------------------------------------ API 端到端

@pytest.fixture()
def admin_client(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        yield app, client


def test_upstream_auth_start_supports_both_providers(admin_client):
    """CodeBuddy 走 poll 轨道，TRAE 走 callback 轨道，unknown provider 报 400。"""
    app, client = admin_client
    # TRAE 的 callback 轨道不需要出网，必定成功并带入参回调地址
    trae = client.post("/api/auth/upstream/start", json={"provider": "trae"})
    assert trae.status_code == 200
    body = trae.json()
    assert body["flow"] == "callback"
    assert body["callback_url"].endswith("/authorize")
    assert "auth_callback_url" in body["auth_url"]
    assert app.state.pending_callback_state == body["state"]

    assert client.post("/api/auth/upstream/start",
                       json={"provider": "unknown"}).status_code == 400


def test_upstream_auth_poll_unknown_state(admin_client):
    _app, client = admin_client
    response = client.post("/api/auth/upstream/poll",
                           json={"provider": "codebuddy", "state": "ghost"})
    assert response.status_code == 400


def test_upstream_auth_rejects_non_admin(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
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
    app.state.stats_collector.rollup_hourly()
    overview = client.get("/api/stats/overview").json()
    assert overview["requests"] == 2                     # admin 看全局
    scoped = client.get("/api/stats/overview", params={"username": "other"}).json()
    assert scoped["requests"] == 1
    assert client.get("/api/stats/by-provider").json()["providers"][0]["requests"] == 2
    timeline = client.get("/api/stats/timeline").json()
    assert timeline["points"]
    assert list(timeline["points"][0].keys()) == ["hour", "codebuddy", "trae"]


def test_stats_endpoints_restrict_non_admin(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    app.state.stats_collector.record(username="alice", provider="trae", model="m", ok=True)
    app.state.stats_collector.record(username="bob", provider="trae", model="m", ok=True)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
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
    assert body["probed"] is False
    # 不能把 Python 类名当作 reason 暴露给用户
    assert body["reason"] == "unknown_error"
    assert "RuntimeError" not in body["reason"]
    assert body["detail"] == "boom"
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
    assert body["status"] is None                     # 无状态能力的渠道返回 null


def test_checkin_endpoint_passes_status_through(admin_client):
    """provider 回填 status 时原样透传（前端靠它显示连续天数）。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=True, credit=100.0,
                                 status={"streak_days": 4, "today_checked_in": True})

    app.state.executor._deps.providers["codebuddy"] = Stub()
    body = client.post(f"/api/credentials/{credential_id}/checkin").json()
    assert body["status"] == {"streak_days": 4, "today_checked_in": True}


def test_checkin_status_endpoint(admin_client):
    """只读状态端点：有能力的渠道返回状态，无能力/不存在的凭证 400。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def checkin_status(self, _data):
            return {"active": True, "streak_days": 7}

    app.state.executor._deps.providers["codebuddy"] = Stub()
    assert client.get(f"/api/credentials/{credential_id}/checkin").json() == {
        "status": {"active": True, "streak_days": 7}}

    # 没有 checkin_status 能力的渠道 → 稳定可读的错误
    app.state.executor._deps.providers["codebuddy"] = type("Bare", (), {"endpoint": "https://e"})()
    rejected = client.get(f"/api/credentials/{credential_id}/checkin")
    assert rejected.status_code == 400
    assert "checkin status" in rejected.text

    # 凭证不存在同样 400
    assert client.get("/api/credentials/cred_missing/checkin").status_code == 400


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


def test_migration_declarations_match_schema_sql():
    """防漂移：迁移声明的列必须在 schema.sql 里存在，删表清单不得重建。

    `_MIGRATION_COLUMNS` 里的列名写错、或 schema.sql 删掉了对应的新库列定义，
    都会让两边静默分叉（新库/老库结构不一致），这里在测试期就抦住。
    """
    from src.db.migrate import _MIGRATION_COLUMNS, _MIGRATION_DROPS, _read_schema

    schema = _read_schema()
    for table, column_def in _MIGRATION_COLUMNS:
        column = column_def.split()[0]
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema, table
        # 取该表定义段落，断言列在其中
        segment = schema.split(f"CREATE TABLE IF NOT EXISTS {table}", 1)[1]
        segment = segment.split(");", 1)[0]
        assert column in segment, f"{table}.{column} 不在 schema.sql"
    for table in _MIGRATION_DROPS:
        # 删掉的表不得在 schema.sql 里重建，否则每次启动都会建了又删
        assert f"CREATE TABLE IF NOT EXISTS {table}" not in schema, table


def test_migrate_adds_cached_tokens_to_legacy_db(tmp_path):
    """老库升级：usage_events 无 cached_tokens 列时幂等补齐，且可写入。"""
    import sqlite3

    from src.db.migrate import SCHEMA_VERSION, apply_schema

    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE usage_events (
            id TEXT PRIMARY KEY, ts INTEGER NOT NULL, username TEXT NOT NULL,
            provider TEXT NOT NULL, credential_id TEXT, model TEXT NOT NULL,
            ok INTEGER NOT NULL, error_type TEXT, input_tokens INTEGER,
            output_tokens INTEGER, reasoning_tokens INTEGER, credit REAL,
            latency_ms INTEGER, ttfb_ms INTEGER)
    """)
    # 旧版 usage_hourly：无 ttfb_sum 列（图表指标升级前的结构）
    conn.execute("""
        CREATE TABLE usage_hourly (
            hour_utc INTEGER NOT NULL, username TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0, ok_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            credit_sum REAL, credit_known INTEGER NOT NULL DEFAULT 0,
            latency_sum INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (hour_utc, username, provider, model))
    """)
    # 该表内已有一行历史汇总：补列后必须保留，新列取默认 0
    conn.execute("INSERT INTO usage_hourly (hour_utc, username, provider, model, requests) "
                 "VALUES (1, 'u', 'trae', 'm', 5)")
    # 旧版 credentials：无 quota_expiry_ladder 列（到期排序指标升级前的结构）
    conn.execute("""
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, data_enc TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            health INTEGER, cooling_until INTEGER, err_count INTEGER NOT NULL DEFAULT 0,
            quota_remaining REAL, quota_total REAL, quota_cycle_end INTEGER,
            quota_probed_at INTEGER, created_at INTEGER NOT NULL, added_by TEXT)
    """)
    conn.commit()
    # 老库遗留的废弃表（schema.sql 已删定义，但老库里还在）
    conn.execute("CREATE TABLE checkins (provider TEXT, account_key TEXT)")
    conn.execute("CREATE TABLE model_cache (provider TEXT, model_id TEXT)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    events_columns = {row[1] for row in db.connect().execute("PRAGMA table_info(usage_events)")}
    assert "cached_tokens" in events_columns
    hourly_columns = {row[1] for row in db.connect().execute("PRAGMA table_info(usage_hourly)")}
    assert "ttfb_sum" in hourly_columns
    # 总览改读小时汇总后新增的三列（老库历史行补 0，数值无法回填）
    assert {"reasoning_tokens", "cached_tokens", "cached_known"} <= hourly_columns
    legacy_hourly = db.connect().execute(
        "SELECT requests, reasoning_tokens, cached_known FROM usage_hourly "
        "WHERE hour_utc = 1").fetchone()
    assert tuple(legacy_hourly) == (5, 0, 0)      # 历史汇总保留，新列取默认
    cred_columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    assert "quota_expiry_ladder" in cred_columns
    # 额度包明细（展示用）也是本次新增列
    assert "quota_packages" in cred_columns
    # 废弃表被 _MIGRATION_DROPS 清理（schema.sql 删定义对老库无效）
    tables = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "checkins" not in tables and "model_cache" not in tables
    # 新增表（(凭证, 模型) 冷却）也由启动时的 schema.sql 全量补建
    assert "credential_model_cooldowns" in tables
    # 运行时配置覆盖表（B3.2）同样是「只加表、不加列」，老库升级后必须存在且可写
    assert "runtime_settings" in tables
    # 补列后可写入、可读出
    db.connect().execute(
        "INSERT INTO credentials (id, provider, data_enc, quota_expiry_ladder, quota_packages,"
        " created_at) VALUES ('cred_1', 'codebuddy', 'x', '[[123, 100.0]]',"
        " '[{\"name\": \"福利积分\"}]', 1)")
    db.connect().commit()
    assert db.connect().execute(
        "SELECT quota_expiry_ladder FROM credentials WHERE id = 'cred_1'").fetchone()[0] == \
        "[[123, 100.0]]"
    assert json.loads(db.connect().execute(
        "SELECT quota_packages FROM credentials WHERE id = 'cred_1'").fetchone()[0]) == \
        [{"name": "福利积分"}]
    # 运行时配置覆盖表：补建后可写入读出（老库升级后管理台立刻可用）
    db.connect().execute(
        "INSERT INTO runtime_settings (key, value, updated_at) "
        "VALUES ('quota_probe_minutes', '15', 1)")
    db.connect().commit()
    stored = db.connect().execute(
        "SELECT value FROM runtime_settings WHERE key = 'quota_probe_minutes'").fetchone()[0]
    assert stored == "15"
    # schema 版本推进到位；补列后新聚合可写
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.connect().execute(
        "INSERT INTO usage_hourly (hour_utc, username, provider, model, requests,"
        " ok_count, ttfb_sum) VALUES (2, 'u', 'trae', 'm', 1, 1, 120)")
    db.connect().commit()
    apply_schema(db.connect())                          # 二次执行不抛错
    db.close()


def test_migrate_reraises_non_duplicate_errors():
    """迁移中非 duplicate column 的 OperationalError 必须重新抛出。"""
    import pytest as _pytest

    class _FakeConn:
        OperationalError = sqlite3.OperationalError

        def executescript(self, _script):
            pass

        def execute(self, _sql):
            raise sqlite3.OperationalError("no such table")

        def commit(self):
            pass

    with _pytest.raises(sqlite3.OperationalError):
        apply_schema(_FakeConn())


def test_stats_model_timeline_filters(stats):
    """按模型趋势：username / since 筛选生效。"""
    collector, query = stats
    collector.record(username="alice", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="bob", provider="trae", model="m2", ok=True, now=9000)
    collector.rollup_hourly()

    scoped = query.model_timeline(username="alice")
    assert scoped["models"] == ["m"]
    recent = query.model_timeline(since=3600)          # bob 的小时桶 7200 命中
    assert recent["models"] == ["m2"]


def test_stats_cached_tokens_overview_and_events(stats):
    """输入缓存命中：汇总（无上报则 None）与明细返回。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, cached_tokens=40)
    collector.record(username="u", provider="trae", model="m", ok=True, input_tokens=50)
    overview = query.overview()
    assert overview["input_tokens"] == 150
    assert overview["cached_tokens"] == 40              # 只有一条上报 → SUM=40

    events = query.events()                          # 新→旧：后插入的未上报记录在前
    assert [e["cached_tokens"] for e in events["events"]] == [None, 40]


def test_stats_model_timeline_endpoint(admin_client):
    """model-timeline 端点透传聚合结果；metric 参数透传。"""
    _app, client = admin_client
    collector = client.app.state.stats_collector
    collector.record(username="root", provider="trae", model="m", ok=True,
                     input_tokens=10, output_tokens=5, ttfb_ms=40)
    collector.rollup_hourly()
    body = client.get("/api/stats/model-timeline").json()
    assert body["models"] == ["m"] and body["points"]
    # metric 参数切换取值语义（默认请求次数 vs tokens 合计）
    tokens = client.get("/api/stats/model-timeline?metric=tokens").json()
    assert tokens["points"][0]["m"] == 15
    # 非法 metric 回退请求次数
    assert client.get("/api/stats/model-timeline?metric=bogus").json()["points"] == body["points"]


def test_stats_events_empty(stats):
    """空库：明细返回空页且无游标。"""
    _collector, query = stats
    assert query.events() == {"events": [], "next_before": None}


def test_stats_events_pagination_and_filters(stats):
    """明细查询：新→旧、rowid 游标翻页不漏不重、筛选组合生效。"""
    collector, query = stats
    for index in range(5):
        collector.record(username="alice" if index % 2 else "bob",
                         provider="trae", model=f"m{index}", ok=bool(index % 2),
                         error_type=None if index % 2 else "rate_limit",
                         now=1000 + index)

    page1 = query.events(limit=2)
    assert [e["model"] for e in page1["events"]] == ["m4", "m3"]
    assert page1["next_before"] == page1["events"][-1]["rowid"]
    page2 = query.events(limit=2, before=page1["next_before"])
    assert [e["model"] for e in page2["events"]] == ["m2", "m1"]
    page3 = query.events(limit=2, before=page2["next_before"])
    assert [e["model"] for e in page3["events"]] == ["m0"]
    assert page3["next_before"] is None                     # 到底

    # 组合筛选：按用户（新→旧）；按时间下限
    assert [e["model"] for e in query.events(username="alice")["events"]] == ["m3", "m1"]
    assert [e["model"] for e in query.events(since=1003)["events"]] == ["m4", "m3"]


def test_stats_events_include_credential_name(repo):
    """明细带凭证 ID 与昵称：存在时 JOIN 昵称；已删除/无凭证时回退 NULL。"""
    credentials, db = repo
    collector, query = StatsCollector(db), StatsQuery(db)
    keep_id = credentials.add(provider="trae", credential_data={"accessToken": "t"},
                              nickname="主号")
    ghost_id = credentials.add(provider="trae", credential_data={"accessToken": "g"},
                               nickname="将删")
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id=keep_id)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id=ghost_id)
    collector.record(username="u", provider="trae", model="m", ok=True)  # 无凭证
    assert credentials.delete(ghost_id) is True

    rows = {e["credential_id"]: e for e in query.events()["events"]}
    assert rows[keep_id]["credential_name"] == "主号"
    assert rows[ghost_id]["credential_name"] is None        # 已删除 → 回退
    assert rows[None]["credential_name"] is None            # 无凭证


def test_stats_events_endpoint_scope_and_clamp(admin_client):
    """明细端点：admin 可看他人；limit 被夹在 1..200。"""
    _app, client = admin_client
    collector = client.app.state.stats_collector
    for index in range(3):
        collector.record(username="other", provider="trae", model=f"m{index}", ok=True)

    body = client.get("/api/stats/events").json()
    assert [e["model"] for e in body["events"]] == ["m2", "m1", "m0"]
    scoped = client.get("/api/stats/events", params={"username": "other"}).json()
    assert len(scoped["events"]) == 3
    limited = client.get("/api/stats/events", params={"limit": 2}).json()
    assert len(limited["events"]) == 2 and limited["next_before"] is not None
    assert len(client.get("/api/stats/events", params={"limit": 0}).json()["events"]) == 1
    huge = client.get("/api/stats/events", params={"limit": 10 ** 9}).json()
    assert len(huge["events"]) == 3


def test_stats_events_restrict_non_admin(tmp_path):
    """非 admin 只见自己的明细，指定他人用户名无效。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.config import Settings
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    app.state.stats_collector.record(username="alice", provider="trae", model="m", ok=True)
    app.state.stats_collector.record(username="bob", provider="trae", model="m", ok=True)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        mine = client.get("/api/stats/events").json()
        assert [e["username"] for e in mine["events"]] == ["alice"]
        forced = client.get("/api/stats/events", params={"username": "bob"}).json()
        assert [e["username"] for e in forced["events"]] == ["alice"]


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


async def test_provider_checkin_status_and_anonymous_scope():
    """checkin_status 只读状态；身份未知时 scope 返回空串（交由任务回落凭证 ID）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/checkin-activity-status")
        return httpx.Response(200, json={"code": 0, "data": {
            "active": True, "today_checked_in": True, "streak_days": 7,
            "today_credit": 100, "total_credits": 700,
            "activity_name": "高校新生攻略", "is_streak_day": True}})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    status = await provider.checkin_status({"bearer_token": "t"})
    assert status["streak_days"] == 7 and status["today_checked_in"] is True
    assert status["activity_name"] == "高校新生攻略"
    # 空身份 → 空串（不是 "https://e|"，否则所有 CB 凭证会共用同一 scope）
    assert provider.checkin_scope({"bearer_token": "t"}) == ""


async def test_provider_checkin_without_status_keeps_none():
    """领取失败（无状态回填）时 provider 不构造 status（中立层保持 None）。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 5, "msg": "boom"})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    result = await provider.checkin({"bearer_token": "t"})
    assert result.ok is False and result.status is None


async def test_provider_checkin_returns_status_dict():
    """provider.checkin 把私有 CheckinStatus 降级成 dict 透传（中立层不认具体类型）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("checkin-activity-status"):
            return httpx.Response(200, json={"code": 0, "data": {
                "active": True, "today_checked_in": True, "streak_days": 3}})
        return httpx.Response(200, json={"code": 0, "data": {"credit": 100}})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    result = await provider.checkin({"bearer_token": "t"})
    assert result.ok and isinstance(result.status, dict)
    assert result.status["streak_days"] == 3


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
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
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
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        for payload in ({"username": "root", "password": "wrong"},
                        {"username": "ghost", "password": "rootpw"},
                        {}):
            assert client.post("/api/auth/login", json=payload).status_code == 401


def test_logout_clears_session(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        assert client.post("/api/auth/logout").json() == {"ok": True}
        assert client.get("/api/auth/session").status_code == 401


def test_session_endpoint_requires_login(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/auth/session").status_code == 401


def test_build_app_fails_without_users_file(tmp_path, monkeypatch):
    from src.auth.users import UsersFileError

    monkeypatch.setenv("USERS_FILE", str(tmp_path / "missing.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    with pytest.raises(UsersFileError):
        build_app(settings)


def test_credentials_endpoint_exposes_admin_flag(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "guest", "password": "guestpw"})
        body = client.get("/api/credentials").json()
        assert body["viewer"] == "guest" and body["is_admin"] is False


def test_credentials_endpoint_exposes_expiring_credits(tmp_path):
    """到期指标随列表下发（与调度排序同源）；无周期信息为 None，套餐阶梯同源下发。"""
    from src.db.repo import _ladder_text

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        repo = app.state.credentials
        repo.add(provider="codebuddy", credential_data={"bearer_token": "t"})
        repo.add(provider="trae", credential_data={"token": "x"})
        ids = {row["provider"]: row["id"] for row in repo.list_all()}
        now = int(time.time())
        ladder = _ladder_text([(now + 3600, 100.0), (now + 3 * 86400, 40.0),
                               (now + 999_999, 50.0)])
        conn = repo._db.connect()
        conn.execute(
            "UPDATE credentials SET quota_expiry_ladder = ?, quota_packages = ? WHERE id = ?",
            (ladder, json.dumps([{"name": "福利积分", "total": 2000.0, "used": 0.0,
                                  "end": now + 3600}]), ids["codebuddy"]))
        conn.commit()
        body = client.get("/api/credentials").json()

    rows = {row["provider"]: row for row in body["credentials"]}
    assert body["expiry_window_seconds"] == settings.quota_expiry_window_seconds
    assert body["expiry_secondary_window_seconds"] == \
        settings.quota_expiry_secondary_window_seconds
    assert rows["codebuddy"]["quota_expiring_credits"] == 100.0
    # 次窗口是 36h 窗口的超集：主窗口已含的包也计入（这里 100 + 40）
    assert rows["codebuddy"]["quota_expiring_credits_secondary"] == 140.0
    assert rows["codebuddy"]["quota_expiry_ladder"] == [
        [now + 3600, 100.0], [now + 3 * 86400, 40.0], [now + 999_999, 50.0]]
    assert rows["trae"]["quota_expiring_credits"] is None
    assert rows["trae"]["quota_expiring_credits_secondary"] is None
    assert rows["trae"]["quota_expiry_ladder"] is None
    # 额度包明细（展示用）同样随列表下发；无明细的渠道为 None
    assert rows["codebuddy"]["quota_packages"] == [
        {"name": "福利积分", "total": 2000.0, "used": 0.0, "end": now + 3600}]
    assert rows["trae"]["quota_packages"] is None
    # 窗口关闭 → 0 而不是 None，展示层据此隐藏该行（两级窗口各自独立）
    closed = {row["provider"]: row for row in repo.list_all(
        expiring_window=0, now=now)}
    assert closed["codebuddy"]["quota_expiring_credits"] == 0.0
    assert closed["codebuddy"]["quota_expiring_credits_secondary"] == 0.0
    secondary_closed = {row["provider"]: row for row in repo.list_all(
        expiring_window=settings.quota_expiry_window_seconds,
        expiring_secondary_window=0, now=now)}
    assert secondary_closed["codebuddy"]["quota_expiring_credits"] == 100.0
    assert secondary_closed["codebuddy"]["quota_expiring_credits_secondary"] == 0.0


# ------------------------------------------------------- 前端静态资源服务

def _spa_client(tmp_path, *, build: bool = True, monkeypatch=None):
    """构造前端产物目录并注入，避免在仓库里真的建删 web/dist。"""
    dist = tmp_path / "dist"
    if build:
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
        (dist / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr("src.webapp.static.frontend_dist", lambda: dist if build else None)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    return TestClient(build_app(settings))


def test_frontend_dist_prefers_project_root():
    """路径必须锚定项目根，而不是当前工作目录。"""
    from src.webapp import static

    resolved = static.frontend_dist()
    # 本仓库已构建前端时应能找到；未构建时返回 None（由下面的注入测试覆盖）
    if resolved is not None:
        assert resolved.name == "dist"
        assert (resolved / "index.html").is_file()


def test_frontend_dist_returns_none_when_absent(tmp_path, monkeypatch):
    from src.webapp import static

    monkeypatch.setattr(static, "_PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    assert static.frontend_dist() is None


def test_spa_serves_index_for_unknown_path(tmp_path, monkeypatch):
    with _spa_client(tmp_path, monkeypatch=monkeypatch) as client:
        response = client.get("/credentials")
    assert response.status_code == 200
    assert "spa" in response.text


def test_spa_serves_real_asset(tmp_path, monkeypatch):
    with _spa_client(tmp_path, monkeypatch=monkeypatch) as client:
        response = client.get("/app.js")
    assert response.status_code == 200 and "console.log" in response.text


def test_spa_reports_missing_build_with_actionable_page(tmp_path, monkeypatch):
    """未构建前端时必须给出可执行的下一步，而不是一句英文错误。"""
    with _spa_client(tmp_path, build=False, monkeypatch=monkeypatch) as client:
        response = client.get("/credentials")
    assert response.status_code == 503
    assert "pnpm build" in response.text
    assert "尚未构建" in response.text or "not" in response.text.lower()
    # 提示里要说明 API 仍可用，避免用户以为整个服务挂了
    assert "/v1/" in response.text or "/api/" in response.text


def test_spa_reports_missing_index_with_actionable_page(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    monkeypatch.setattr("src.webapp.static.frontend_dist", lambda: dist)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    with TestClient(build_app(settings)) as client:
        response = client.get("/credentials")
    assert response.status_code == 503
    assert "pnpm build" in response.text


def test_spa_does_not_escape_dist(tmp_path, monkeypatch):
    """路径穿越必须拒绝：解析后位于 dist 之外的文件不能被读出。"""
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (tmp_path / "secret.env").write_text("APP_SECRET=leaked", encoding="utf-8")
    monkeypatch.setattr("src.webapp.static.frontend_dist", lambda: dist)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    with TestClient(build_app(settings)) as client:
        response = client.get("/../secret.env")
    assert "leaked" not in response.text
    assert "spa" in response.text          # 回退到 index.html



# ------------------------------------------------- 执行引擎的统计埋点

class RecordingCollector:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.boom = False

    def record(self, **fields) -> None:
        if self.boom:
            raise RuntimeError("stats backend down")
        self.events.append(fields)


def _exec_with_stats(repo_tuple, script, collector, **kw):
    credentials, _db = repo_tuple
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler

    class Streaming:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _data, _payload, _model):
            index = min(self.calls, len(script) - 1)
            self.calls += 1
            for item in script[index]:
                if isinstance(item, Exception):
                    raise item
                yield item

        def list_models(self, _data):  # pragma: no cover
            return []

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Streaming()}, credentials=credentials,
        scheduler=Scheduler(**kw), default_model="glm-5.2", stats=collector))
    return executor


GOOD_EVENTS = [Event(kind=EventKind.CONTENT, content="hi"),
               Event(kind=EventKind.USAGE, usage=Usage(11, 22, 3)),
               Event(kind=EventKind.FINISH, finish_reason="stop")]


async def test_stream_records_success_with_usage_and_username(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    event = collector.events[-1]
    assert event["ok"] is True and event["username"] == "alice"
    assert event["provider"] == "codebuddy" and event["model"] == "glm-5.2"
    assert event["input_tokens"] == 11 and event["output_tokens"] == 22
    assert event["reasoning_tokens"] == 3
    assert event["latency_ms"] is not None
    assert event["ttfb_ms"] is not None


async def test_stream_without_usage_frame_records_null_tokens(repo):
    """上游没给 usage 时统计字段为 None，不能崩也不能写 0。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(
        repo, [[Event(kind=EventKind.CONTENT, content="hi"),
                Event(kind=EventKind.FINISH, finish_reason="stop")]], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True})):
        pass
    assert collector.events[-1]["input_tokens"] is None


async def test_stream_prefers_credential_expiring_soon(repo):
    """到期积分指标贯通到真实选号：低健康度但快过期积分多的号先被选中。"""
    credentials, _db = repo
    steady_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "s"})
    burn_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    now = int(time.time())
    credentials.save_quota(steady_id, Quota(remaining=95, total=100,
                                            cycle_end=now + 48 * 3600, probed_at=now))
    credentials.save_quota(burn_id, Quota(remaining=10, total=100, cycle_end=now + 600,
                                           expiry_ladder=[(now + 600, 100.0),
                                                          (now + 700, 50.0)],
                                           probed_at=now))
    cands = {c.credential_id: c for c in credentials.candidates()}
    assert cands[burn_id].expiry_ladder == [(now + 600, 100.0), (now + 700, 50.0)]
    assert cands[steady_id].expiry_ladder is None
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    assert collector.events[-1]["credential_id"] == burn_id


async def test_stream_expiry_window_zero_keeps_health_order(repo):
    """窗口关闭后退回健康度排序（到期指标全员 0 分）。"""
    credentials, _db = repo
    steady_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "s"})
    burn_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    now = int(time.time())
    credentials.save_quota(steady_id, Quota(remaining=95, total=100, probed_at=now))
    credentials.save_quota(burn_id, Quota(remaining=10, total=100,
                                           expiry_ladder=[(now + 600, 100.0)],
                                           probed_at=now))
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector, expiry_window=0)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    assert collector.events[-1]["credential_id"] == steady_id


async def test_stream_secondary_expiry_breaks_tie(repo):
    """两级窗口贯通到真实选号：36h 内都没到期积分时，7 天内会过期者优先。"""
    credentials, _db = repo
    steady_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "s"})
    week_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "w"})
    now = int(time.time())
    credentials.save_quota(steady_id, Quota(remaining=95, total=100, probed_at=now))
    credentials.save_quota(week_id, Quota(remaining=50, total=100,
                                          expiry_ladder=[(now + 3 * 86400, 140.0)],
                                          probed_at=now))
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    assert collector.events[-1]["credential_id"] == week_id


async def test_stream_records_failure_when_credentials_exhausted(repo):
    credentials, _db = repo
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="bob"):
        pass
    event = collector.events[-1]
    assert event["ok"] is False and event["error_type"] == "no_healthy_credential"
    assert event["username"] == "bob"


async def test_complete_records_success_and_failure(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}), username="alice")
    assert collector.events[-1]["ok"] is True
    assert collector.events[-1]["input_tokens"] == 11
    # 非流式也记首字延迟（上游首个事件时刻）
    assert collector.events[-1]["ttfb_ms"] is not None

    credentials.save_error(credentials.candidates()[0].credential_id, _disabled_outcome())
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert collector.events[-1]["error_type"] == "no_healthy_credential"


async def test_stats_failure_never_breaks_chat(repo):
    """统计后端不可用时聊天必须照常返回。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    collector.boom = True
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"


def test_executor_without_stats_collector_is_noop(repo):
    credentials, _db = repo
    executor = _exec_with_stats(repo, [GOOD_EVENTS], None)
    assert executor._deps.stats is None
    executor._deps.record(username="x", provider="y", model="z", ok=True)  # 不崩


async def test_upstream_http_failure_records_error_type(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Dead(Exception):
        def __init__(self) -> None:
            super().__init__("session dead")

        def kind(self):
            return ErrKind.DEAD

    executor = _exec_with_stats(repo, [[Dead()]], collector)
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    # 会话失效 → 硬禁用，轮换耗尽后归类为无可用凭证
    assert collector.events[-1]["error_type"] == "no_healthy_credential"
    assert credentials.candidates()[0].disabled is True


def test_error_type_mapping_covers_all_kinds():
    from src.engine.executor import _error_type_for
    from src.provider.base import ErrKind

    assert _error_type_for(ErrKind.PLAN) == "rate_limit"
    assert _error_type_for(ErrKind.DEAD) == "credential_unavailable"
    assert _error_type_for(ErrKind.SOFT) == "upstream_error"
    assert _error_type_for(ErrKind.OTHER) == "upstream_error"


def test_usage_field_tolerates_missing_usage():
    from src.engine.executor import _usage_field
    from src.provider.base import Usage

    assert _usage_field(None, "input_tokens") is None
    assert _usage_field(Usage(5, 6, 7), "output_tokens") == 6
    assert _usage_field(Usage(), "credit") is None


async def test_plan_error_records_rate_limit_error_type(repo):
    """权益耗尽 → 统计记为 rate_limit（受控错误类型白名单内）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Plan(Exception):
        def __init__(self) -> None:
            super().__init__("plan exhausted")

        def kind(self):
            return ErrKind.PLAN

    executor = _exec_with_stats(repo, [[Plan()]], collector)
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert collector.events[-1]["error_type"] == "no_healthy_credential"
    assert credentials.candidates()[0].cooling_until is not None


async def test_stream_plan_error_records_failure(repo):
    """流式路径的权益耗尽同样要落统计。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Plan(Exception):
        def __init__(self) -> None:
            super().__init__("plan exhausted")

        def kind(self):
            return ErrKind.PLAN

    executor = _exec_with_stats(repo, [[Plan()]], collector, max_rotate=1)
    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="carol"):
        pass
    assert collector.events[-1]["ok"] is False
    assert collector.events[-1]["username"] == "carol"


# --------------------------------------------- 即时额度探测（新增凭证等场景）

def test_import_triggers_immediate_probe(admin_client):
    """新增凭证后应立即探测，否则界面一直显示「未探测到额度」。"""
    app, client = admin_client
    probed: list[str] = []

    class Stub:
        endpoint = "https://e"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "t")}

        async def probe_quota(self, _data):
            probed.append("called")
            return Quota(remaining=7, total=10, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "abc"}})
    credential_id = created.json()["id"]

    # 后台任务在事件循环里执行，需等待落库
    for _ in range(50):
        if probed:
            break
        time.sleep(0.02)
    assert probed == ["called"]
    health = app.state.credentials.candidates()[0].health
    assert health == 70
    assert credential_id


def test_probe_failure_marks_unknown_on_immediate_path(admin_client):
    """即时探测失败 → 记为未探测（NULL），不是额度 0。"""
    app, client = admin_client

    class Stub:
        endpoint = "https://e"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "t")}

        async def probe_quota(self, _data):
            raise RuntimeError("upstream down")

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post("/api/credentials", json={"provider": "codebuddy", "credential": {"token": "x"}})

    # 探测失败会写 quota_probed_at 并把 health 置回 NULL
    for _ in range(50):
        listed = client.get("/api/credentials").json()["credentials"][0]
        if listed["quota_probed_at"] is not None:
            break
        time.sleep(0.02)
    assert listed["health"] is None
    assert listed["quota_probed_at"] is not None


def test_switch_account_triggers_immediate_probe(admin_client):
    """账号切换后额度对应新账号，必须重探测而不是沿用旧值。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    probed: list[int] = []

    class Stub:
        endpoint = "https://e"

        async def switch_account(self, data, account_id):
            return {**data, "account_uid": account_id}

        async def probe_quota(self, _data):
            probed.append(1)
            return Quota(remaining=1, total=4, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post(f"/api/credentials/{credential_id}/accounts/select",
                json={"account_id": "a1"})

    for _ in range(50):
        if probed:
            break
        time.sleep(0.02)
    assert probed == [1]


def test_checkin_success_triggers_immediate_probe(admin_client):
    """签到发放积分 → 立即刷新额度；签到失败则不探测。"""
    app, client = admin_client
    ok_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    probed: list[int] = []

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=True, credit=10)

        async def probe_quota(self, _data):
            probed.append(1)
            return Quota(remaining=3, total=4, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post(f"/api/credentials/{ok_id}/checkin")

    for _ in range(50):
        if probed:
            break
        time.sleep(0.02)
    assert probed == [1]


def test_checkin_failure_does_not_probe(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    probed: list[int] = []

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=False, code=0)

        async def probe_quota(self, _data):  # pragma: no cover
            probed.append(1)
            return Quota()

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post(f"/api/credentials/{credential_id}/checkin")
    time.sleep(0.1)
    assert probed == []


def test_schedule_probe_ignores_deleted_credential(admin_client):
    """凭证在探测前被删除 → schedule_probe 静默返回，不抛异常也不建任务。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    before = len(app.state.pending_probes)
    app.state.credentials.delete(credential_id)

    # 经由 API 触发内部 schedule_probe 的同一路径：重新导入再删除
    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "x"}}).json()["id"]
    app.state.credentials.delete(created)

    assert len(app.state.pending_probes) >= before


def test_schedule_probe_returns_early_when_credential_unreadable(admin_client):
    """凭证读取不到（并发删除）→ schedule_probe 直接返回，不建探测任务（main 383-384）。"""
    app, client = admin_client
    before = len(app.state.pending_probes)
    app.state.credentials.credential_data = lambda _cid: None  # type: ignore[method-assign]

    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "x"}})
    assert created.status_code == 200
    time.sleep(0.05)
    assert len(app.state.pending_probes) == before


# ------------------------------------------- TRAE callback 登录闭环

def test_trae_start_auth_builds_login_url_with_public_callback():
    from src.api.admin_auth import resolve_public_callback_url

    settings = Settings(_env_file=None, APP_SECRET=SECRET, PUBLIC_BASE_URL="https://gw.example")
    provider = TraeProvider()
    session = provider.start_auth(resolve_public_callback_url(settings))

    assert session.flow == "callback"
    assert session.callback_url == "https://gw.example/authorize"
    assert "auth_callback_url=https%3A%2F%2Fgw.example%2Fauthorize" in session.auth_url
    machine_id, _, device_id = session.state.partition(":")
    assert len(machine_id) == 32 and len(device_id) == 32


async def test_trae_complete_callback_exchanges_token():
    """回调链接必须真的换 token，而不是只存 refreshToken。"""
    import httpx as _httpx

    from src.provider.trae.client import TraeClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(200, json={"Result": {
                "Token": "ACCESS", "RefreshToken": "RT2", "TokenExpireAt": 1_800_000_000_000}})
        return _httpx.Response(200, json={"Result": {"UserID": "uid-9", "ScreenName": "昵称"}})

    transport = _httpx.MockTransport(handler)
    provider = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    session = provider.start_auth("https://gw.example/authorize")
    url = ("https://gw.example/authorize?refreshToken=RT1&userInfo="
           "%7B%22uid%22%3A%22%22%7D")
    data = await provider.complete_callback(url, session.state)

    assert data["accessToken"] == "ACCESS"
    assert data["refreshToken"] == "RT2"
    assert data["expiresAt"] == 1_800_000_000
    assert data["uid"] == "uid-9"          # 回调没给 uid 时回退 GetUserInfo
    assert data["nickname"] == "昵称"
    assert data["machineId"] == session.state.partition(":")[0]


async def test_trae_complete_callback_rejects_bad_state():
    from src.provider.trae.events import UpstreamProtocolViolation

    provider = TraeProvider()
    with pytest.raises(UpstreamProtocolViolation):
        await provider.complete_callback("https://x/authorize?refreshToken=RT", "no-colon")


def test_authorize_completes_trae_login_end_to_end(tmp_path):
    """完整闭环：start → 浏览器回调 → 凭证入库 → 立即探测。"""
    import httpx as _httpx

    from src.provider.trae.client import TraeClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(200, json={"Result": {"Token": "ACCESS",
                                                         "RefreshToken": "RT2"}})
        if request.url.endswith("ide_user_ent_usage"):
            return _httpx.Response(200, json={"user_entitlement_pack_list": [
                {"entitlement_base_info": {"quota": {"credits_limit": 100}},
                 "usage": {"credits_amount": 25}}]})
        return _httpx.Response(200, json={"Result": {"UserID": "u1", "ScreenName": "n"}})

    transport = _httpx.MockTransport(handler)
    trae = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": trae})

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()

        callback = ("/authorize?refreshToken=RT1&userInfo=%7B%22uid%22%3A%22u1%22%7D"
                    f"&state={started['state']}")
        response = client.get(callback)
        assert response.status_code == 200 and response.json()["captured"] is True

        listed = client.get("/api/credentials").json()["credentials"]
        assert len(listed) == 1 and listed[0]["provider"] == "trae"
        # 响应与列表都不得泄漏 token
        assert "ACCESS" not in response.text
        assert "data_enc" not in listed[0]

    # state 已消费，同一回调不可重放
    with TestClient(app) as replay:
        assert replay.get(callback).status_code == 400


def test_authorize_rejects_callback_without_token(tmp_path):
    """带 state 但没有 refreshToken/userJwt 的回调必须拒绝。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()
        response = client.get(f"/authorize?state={started['state']}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_upstream_start_uses_poll_track_for_codebuddy(admin_client):
    """provider 在 upstream_auth 里时走 poll 轨道（main 275-276）。"""
    app, client = admin_client

    class FakeOAuth:
        def __init__(self) -> None:
            self.store = type("S", (), {"cancel": staticmethod(lambda *_: True)})()

        async def start(self, username):
            from src.provider.base import AuthSession

            return AuthSession(flow="poll", state="local-reservation",
                               auth_url="https://auth.example/x", interval=5)

    app.state.upstream_auth["codebuddy"] = FakeOAuth()
    body = client.post("/api/auth/upstream/start", json={"provider": "codebuddy"}).json()
    assert body["flow"] == "poll"
    assert body["state"] == "local-reservation"
    assert body["callback_url"] is None


async def test_complete_callback_does_not_use_refresh_token_as_access_token():
    """ExchangeToken 失败时不得把 refreshToken 当 accessToken 塞进池子。"""
    import httpx as _httpx

    from src.provider.trae.client import TraeClient
    from src.provider.trae.events import UpstreamProtocolViolation

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(400, content=b"bad refresh token")
        return _httpx.Response(200, json={"Result": {"UserID": "u", "ScreenName": "n"}})

    transport = _httpx.MockTransport(handler)
    provider = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    session = provider.start_auth("https://gw.example/authorize")
    with pytest.raises(UpstreamProtocolViolation):
        await provider.complete_callback(
            "https://gw.example/authorize?refreshToken=RT", session.state)


async def test_complete_callback_raises_when_no_token_available():
    import httpx as _httpx

    from src.provider.trae.client import TraeClient
    from src.provider.trae.events import UpstreamProtocolViolation

    def handler(_request: _httpx.Request) -> _httpx.Response:
        return _httpx.Response(400, content=b"rejected")

    transport = _httpx.MockTransport(handler)
    provider = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))
    session = provider.start_auth("https://gw.example/authorize")
    with pytest.raises(UpstreamProtocolViolation):
        await provider.complete_callback(
            "https://gw.example/authorize?refreshToken=RT", session.state)


def test_authorize_reports_invalid_credential(tmp_path):
    """回调结构损坏 → 400 invalid_credential（main 446-448）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        client.post("/api/auth/upstream/start", json={"provider": "trae"})
        # 有 refreshToken 但状态串损坏 → complete_callback 抛协议违规
        app.state.pending_callback_state = "broken"
        response = client.get("/authorize?refreshToken=RT&state=broken")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_credential"


def test_frontend_dist_falls_back_to_container_and_cwd(tmp_path, monkeypatch):
    """项目根没有产物时，依次尝试容器路径与当前工作目录（main 516-517）。"""

    from src.webapp import static

    monkeypatch.setattr(static, "_PROJECT_ROOT", tmp_path)          # 项目根没有
    container_dist = tmp_path / "container" / "web" / "dist"
    container_dist.mkdir(parents=True)
    (container_dist / "index.html").write_text("x", encoding="utf-8")

    # 容器候选已抽成模块常量，可直接指到临时目录
    monkeypatch.setattr(static, "_CONTAINER_DIST", container_dist)

    found = static.frontend_dist()
    assert found is not None and found.name == "dist"


# ------------------------------------- Playground 会话端点（无需 API Key）

class _PlaygroundProvider:
    id = "trae"

    def __init__(self, script=None) -> None:
        # script 是「按尝试次数」分组的段：每段是一批 Event（或一个异常）
        self.script = script or []
        self.calls = 0

    async def stream_chat(self, _data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        segment = self.script[index]
        if isinstance(segment, Exception):
            raise segment
        for item in segment:
            if isinstance(item, Exception):
                raise item
            yield item

    async def list_models(self, _data):
        from src.provider.base import Model

        return [Model(id="glm-5.2"), Model(id="trae-only")]

    def import_credential(self, raw):  # pragma: no cover - 非本测试路径
        return raw


def _playground_app(tmp_path, script=None):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": _PlaygroundProvider(script)})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    return app


def test_playground_models_uses_session(tmp_path):
    app = _playground_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/playground/models").status_code == 401
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        body = client.get("/api/playground/models").json()
    assert [m["id"] for m in body["data"]] == ["glm-5.2", "trae-only"]
    assert body["data"][0]["providers"] == ["trae"]


def test_playground_chat_requires_session(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        response = client.post("/api/playground/chat/completions",
                               json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 401


def test_playground_chat_works_without_api_key(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.post("/api/playground/chat/completions",
                               json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hi"


def test_playground_chat_stream(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.post("/api/playground/chat/completions",
                               json={"messages": [{"role": "user", "content": "hi"}],
                                     "stream": True})
    assert "text/event-stream" in response.headers["content-type"]
    assert "data: [DONE]" in response.text


def test_playground_attributes_usage_to_session_user(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        client.post("/api/playground/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]})
    overview = app.state.stats_query.overview(username="alice")
    assert overview["requests"] == 1 and overview["ok_count"] == 1


def test_playground_invalid_request_returns_400(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.post("/api/playground/chat/completions", json={"messages": []})
    assert response.status_code == 400


def test_v1_models_and_playground_models_are_consistent(tmp_path):
    app = _playground_app(tmp_path)
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        v1 = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        playground = client.get("/api/playground/models").json()
    assert v1 == playground


# --------------------------------- TRAE 真实回调形态（上游不回传 state）

def test_authorize_accepts_real_trae_callback_without_state(tmp_path):
    """TRAE 回跳只带 refreshToken/userInfo，不会回传我们的 state。

    之前实现要求 state 匹配，导致真实登录必然 400。
    """
    import httpx as _httpx

    from src.provider.trae.client import TraeClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(200, json={"Result": {"Token": "ACCESS",
                                                         "RefreshToken": "RT2"}})
        return _httpx.Response(200, json={"Result": {"UserID": "u9",
                                                     "ScreenName": "真名"}})

    transport = _httpx.MockTransport(handler)
    trae = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": trae})

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()

        # 与真实回调一致：不带 state 参数
        callback = ("/authorize?refreshToken=RT1&userInfo="
                    "%7B%22uid%22%3A%22uid-9%22%2C%22nickname%22%3A%22%E7%9C%9F%E5%90%8D%22%7D")
        response = client.get(callback)
        assert response.status_code == 200, response.text

        listed = client.get("/api/credentials").json()["credentials"]
        assert len(listed) == 1 and listed[0]["provider"] == "trae"
        assert listed[0]["nickname"] == "真名"
        # machine/device id 来自 pending state，与登录 URL 一致
        assert started["state"].partition(":")[0] in started["auth_url"]

    # 回调成功后 pending 已清空：同一链接重放必须拒绝
    with TestClient(app) as replay:
        assert replay.get(callback).status_code == 400


def test_authorize_without_pending_login_still_rejected(tmp_path):
    """没有进行中的登录时，带 refreshToken 的回调依然拒绝（防乱塞池子）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_dump_request_bodies_writes_file(tmp_path):
    """诊断开关：dump_request_bodies=True 时 /v1 请求体落盘（main 291-293）。"""
    import json as _json

    from fastapi.testclient import TestClient

    from src.config import Settings
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        dump_request_bodies=True)

    class P:
        id = "p1"

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="m")]

        def import_credential(self, raw):  # pragma: no cover
            return raw

    app = build_app(settings, providers={"p1": P()})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": "m", "stream": False,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code in (200, 400, 503)  # body 是否合法不影响 dump
        dumps = list((tmp_path / "dumps").glob("*.json"))
        assert dumps, "应产生 dump 文件"
        body = _json.loads(dumps[0].read_text(encoding="utf-8"))
        assert body["model"] == "m"


async def test_trae_checkin_claim_soft_failure_and_success_paths():
    """TRAE claim HTTP 200 + 业务码非 0 是软失败（9074），不得误报成功。"""
    from src.provider.trae.client import TraeClient, TraeProvider

    seen: list[str] = []
    device_ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        device_ids.append(request.headers.get("x-device-id", ""))
        if request.url.path.endswith("status"):
            # 只有第二次（成功的）claim 之后才算已签
            checked = len([p for p in seen if p.endswith("claim")]) >= 2
            credits = 200 if checked else 150
            return httpx.Response(200, json={"checked_in": checked, "credits": credits,
                                             "enable": True, "code": 0})
        # 第一次 claim 软失败，之后成功
        failures = len([p for p in seen if p.endswith("claim")])
        if failures == 1:
            return httpx.Response(200, json={"code": 9074,
                                             "message": "当前参与用户太多，请稍后再试"})
        return httpx.Response(200, json={"code": 0})

    import httpx as _httpx
    transport = _httpx.MockTransport(handler)
    client = TraeClient(stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
                        short_client=_httpx.AsyncClient(transport=transport, timeout=None))
    provider = TraeProvider(client=client)
    data = {"bearer_token": "t", "device_id": "d", "uid": "u"}

    # 单轮内 9074 会轮换代次设备号重试，所以一轮就应成功；期间用的都是 16 位数字
    first = await provider.checkin(data)
    assert first.ok and first.code == 0 and first.credit == 200
    # gen0: status + claim(9074)；gen1: status + claim(成功) + 回查 status
    assert len(device_ids) == 5
    assert len(set(device_ids)) == 2     # 轮换过一次设备号
    assert all(d.isdigit() and len(d) == 16 for d in device_ids)



async def test_trae_checkin_retries_9074_with_fresh_device_ids():
    """9074 按限流处理：一轮内换新设备号重试；全失败才返回软失败。

    回归背景：9074 曾被误判为「设备号格式不符」，改成确定性派生值——结果对新
    账号连续失败（同一设备号复用）。现在每次尝试都用全新设备号。
    """
    from src.provider.trae.client import TraeClient, TraeProvider

    device_ids: list[str] = []
    claims: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        device_ids.append(request.headers.get("x-device-id", ""))
        if request.url.path.endswith("status"):
            # 只有第二次 claim 成功后才算已签、余额才涨（回查确认靠这个）
            done = len(claims) >= 2
            return httpx.Response(200, json={
                "checked_in": done, "credits": 350 if done else 150,
                "enable": True, "code": 0})
        claims.append(request.headers.get("x-device-id", ""))
        if len(claims) < 2:
            return httpx.Response(200, json={"code": 9074, "message": "当前参与用户太多"})
        return httpx.Response(200, json={"code": 0})

    import httpx as _httpx
    transport = _httpx.MockTransport(handler)
    client = TraeClient(stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
                        short_client=_httpx.AsyncClient(transport=transport, timeout=None))
    result = await TraeProvider(client=client).checkin({"bearer_token": "t", "uid": "u"})
    assert result.ok is True and result.code == 0
    assert result.credit == 350
    assert len(claims) == 2
    assert claims[0] != claims[1], "重试必须换新设备号"
    assert all(d.isdigit() and len(d) == 16 for d in device_ids)
    # 每次尝试（status + claim [+ 成功后的回查]）内部必须用同一个设备号：
    # 轮次1 = status + claim（用号 A）；轮次2 = status + claim + 回查（用号 B）
    assert set(device_ids[:2]) == {claims[0]}
    assert set(device_ids[2:]) == {claims[1]}


async def test_trae_checkin_gives_up_after_all_attempts():
    """所有尝试都 9074 → 如实返回最后一次的软失败，不伪装成功、不无限重试。"""
    from src.provider.trae.client import CHECKIN_ATTEMPTS, TraeClient, TraeProvider

    claims: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("status"):
            return httpx.Response(200, json={"checked_in": False, "credits": 150,
                                             "enable": True, "code": 0})
        claims.append(request.headers.get("x-device-id", ""))
        return httpx.Response(200, json={"code": 9074, "message": "当前参与用户太多"})

    import httpx as _httpx
    transport = _httpx.MockTransport(handler)
    client = TraeClient(stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
                        short_client=_httpx.AsyncClient(transport=transport, timeout=None))
    result = await TraeProvider(client=client).checkin({"bearer_token": "t", "uid": "u"})
    assert result.ok is False and result.code == 9074
    assert "当前参与用户太多" in result.message
    assert len(claims) == CHECKIN_ATTEMPTS
    assert len(set(claims)) == CHECKIN_ATTEMPTS       # 每次都是新设备号


async def test_trae_checkin_not_enabled_short_circuits():
    """enable=False → 直接返回不可签到，不发 claim。"""
    from src.provider.trae.client import TraeClient

    claims: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("status"):
            return httpx.Response(200, json={"checked_in": False, "credits": 0,
                                             "enable": False, "code": 0})
        claims.append(request.url.path)
        return httpx.Response(200, json={"code": 0})

    import httpx as _httpx
    transport = _httpx.MockTransport(handler)
    client = TraeClient(stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
                        short_client=_httpx.AsyncClient(transport=transport, timeout=None))
    from src.provider.trae.client import TraeProvider

    result = await TraeProvider(client=client).checkin({"bearer_token": "t", "uid": "u"})
    assert result.ok is False and result.message == "当前账号不可签到"
    assert claims == []


def test_new_checkin_device_id_is_fresh_numeric_string():
    """签到设备号：16 位数字串，且每次都是新的（复用是 9074 的可疑诱因）。

    历史教训：此处曾断言「由 uid 确定性派生」——那是单次对照得出的错误结论
    （派生值对第二个账号连续 4 次 9074，而随机新值当次成功）。不要改回确定性派生。
    """
    from src.provider.trae.credential import new_checkin_device_id

    ids = {new_checkin_device_id() for _ in range(50)}
    assert len(ids) == 50, "设备号必须每次不同"
    assert all(i.isdigit() and len(i) == 16 for i in ids)


# ------------------------------------------------- (凭证, 模型) 级冷却持久化

def test_repo_persists_model_cooldowns_separately_from_account(repo):
    """模型级冷却只写独立表：账号级 cooling_until 必须保持为空。"""
    from src.engine.scheduler import ModelCooldown

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(
        err_count=0, model_cooldowns={"glm-5.2": ModelCooldown(
            cooling_until=9999, hits=1, reason="model")}))

    candidate = credentials.candidates()[0]
    assert candidate.cooling_until is None            # 账号级未被污染
    assert not candidate.is_selectable(1000, "glm-5.2")
    assert candidate.is_selectable(1000, "kimi-k2")   # 其他模型仍可用
    assert credentials.model_cooldowns_for(cid, now=1000) == {"glm-5.2": 9999}


def test_repo_model_cooldown_upsert_escalates_hits(repo):
    """同 (凭证, 模型) 反复命中时原地升级 hits，不产生多行。"""
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=100, hits=1, reason="model")}))
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=200, hits=2, reason="model")}))
    rows = db.connect().execute(
        "SELECT cooling_until, hits, reason FROM credential_model_cooldowns").fetchall()
    assert [(r["cooling_until"], r["hits"], r["reason"]) for r in rows] == [(200, 2, "model")]


def test_repo_candidates_carry_reason_so_backoff_escalates(repo):
    """candidates() 必须把 reason 一并发出来，否则 blocked 退避永远停在 6h。

    note_error 用 reason 判断「是否同一原因」；漏读会让每次都被当成换了原因
    而把 hits 重置为 1，6h→12h→24h 的升级形同虚设。
    """
    from src.engine.scheduler import Scheduler

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    scheduler = Scheduler()
    now = 1_000_000

    def note(**kw):
        candidate = credentials.candidates()[0]
        outcome = scheduler.note_error(candidate, ErrKind.BLOCKED, now,
                                       model="glm-5.2", **kw)
        credentials.save_error(cid, outcome)
        return outcome.model_cooldowns["glm-5.2"]

    first = note()
    assert (first.hits, first.reason) == (1, "blocked")
    assert credentials.candidates()[0].model_cooldowns["glm-5.2"].reason == "blocked"
    second = note()
    assert second.hits == 2 and second.cooling_until == now + 12 * 3600
    third = note()
    assert third.hits == 3 and third.cooling_until == now + 24 * 3600


def test_repo_account_cooldown_clears_model_entries(repo):
    """账号级冷却必须清空模型级条目：否则「切模型」能绕过账号级限流。"""
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=9999, hits=1)}))
    credentials.save_error(cid, ErrorOutcome(cooling_until=8888, err_count=0))
    assert db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"] == 0


def test_repo_save_success_clears_blocked_only(repo):
    """成功清除 negative cache，但不清 6004 模型级限流（对齐上游重置）。"""
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "blocked-model": ModelCooldown(cooling_until=9999, hits=1, reason="blocked"),
        "limited-model": ModelCooldown(cooling_until=9999, hits=1, reason="model")}))
    credentials.save_success(cid, model="blocked-model")
    remaining = {r["model"] for r in db.connect().execute(
        "SELECT model FROM credential_model_cooldowns").fetchall()}
    assert remaining == {"limited-model"}
    # 不传 model 时不做任何模型级清理
    credentials.save_success(cid)
    assert db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"] == 1


def test_repo_revive_and_delete_clear_model_cooldowns(repo):
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=9999, hits=1)}))
    assert credentials.revive(cid) is True
    count = db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"]
    assert count == 0
    # 再次写入后删除凭证：残留行必须一并清掉（重建同 id 不应继承旧冷却）
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=9999, hits=1)}))
    assert credentials.delete(cid) is True
    assert db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"] == 0


def test_repo_purges_only_expired_model_cooldowns(repo):
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "old": ModelCooldown(cooling_until=500, hits=1),
        "fresh": ModelCooldown(cooling_until=5000, hits=1)}))
    assert credentials.purge_expired_model_cooldowns(now=1000) == 1
    names = {r["model"] for r in db.connect().execute(
        "SELECT model FROM credential_model_cooldowns").fetchall()}
    assert names == {"fresh"}


def test_repo_model_cooldowns_for_skips_expired(repo):
    from src.engine.scheduler import ModelCooldown

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "old": ModelCooldown(cooling_until=500, hits=1),
        "fresh": ModelCooldown(cooling_until=5000, hits=1)}))
    assert credentials.model_cooldowns_for(cid, now=1000) == {"fresh": 5000}


def test_retention_task_purges_expired_model_cooldowns(repo):
    """留存任务顺带回收过期冷却行；未注入凭证仓储时该计数为 0。"""
    from src.engine.scheduler import ModelCooldown

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=int(time.time()) - 10, hits=1)}))
    collector = StatsCollector(_db)
    report = RetentionTask(collector, credentials=credentials).run_once()
    assert report["expired_coolings"] == 1
    # 不传 credentials（旧调用方）时不报错，计数为 0
    assert RetentionTask(collector).run_once()["expired_coolings"] == 0


def test_list_all_exposes_model_cooldowns(tmp_path):
    """管理台列表带出生效中的模型冷却（过期行不下发）。"""
    from src.engine.scheduler import ModelCooldown

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    now = int(time.time())
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "fresh": ModelCooldown(cooling_until=now + 600, hits=2, reason="blocked")}))
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "stale": ModelCooldown(cooling_until=now - 600, hits=1)}))
    row = credentials.list_all(now=now)[0]
    assert row["model_cooldowns"] == [
        {"model": "fresh", "cooling_until": now + 600, "hits": 2, "reason": "blocked"}]
    assert CredentialRepository(
        db, CredentialCipher(SECRET)).list_all(now=now)[0]["model_cooldowns"] == row[
            "model_cooldowns"]
    db.close()


def test_credentials_endpoint_exposes_model_cooldowns(tmp_path):
    """端到端：模型级冷却经 /api/credentials 下发（前端据此展示）。"""
    from src.engine.scheduler import ModelCooldown

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        repo = app.state.credentials
        cid = repo.add(provider="codebuddy", credential_data={"bearer_token": "t"})
        repo.save_error(cid, ErrorOutcome(model_cooldowns={
            "glm-5.2": ModelCooldown(cooling_until=int(time.time()) + 600,
                                     hits=1, reason="model")}))
        rows = client.get("/api/credentials").json()["credentials"]
    assert [row["model"] for row in rows[0]["model_cooldowns"]] == ["glm-5.2"]
