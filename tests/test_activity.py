"""B1.7 活跃上报：协议层 + 任务层 + 管理台入口。

实测形状（2026-09-21 直连 CN 上游）：
- POST {endpoint}/v2/report，body 为事件数组，eventCode=chat_request_send
- userId 必填（凭证 user_id/account_uid 实测为空，回落 bearer JWT 的 sub）
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository, GrowthRepository
from src.main import build_app
from src.provider.codebuddy.activity import (
    EP_REPORT,
    ActivityRejected,
    ActivityResult,
    CodeBuddyActivity,
    chat_request_event,
    resolve_user_id,
    user_id_from_token,
)
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.tasks.activity import ActivityTask
from tests.conftest import SECRET


def _jwt(claims: dict) -> str:
    """造一个只有 payload 有意义的假 JWT（签名段任意）。"""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def _client(handler) -> CodeBuddyActivity:
    transport = httpx.MockTransport(handler)
    return CodeBuddyActivity(
        "https://copilot.tencent.com",
        client=httpx.AsyncClient(transport=transport, timeout=None))


# ------------------------------------------------------- user_id_from_token

def test_user_id_from_token_reads_sub():
    assert user_id_from_token(_jwt({"sub": "abc-123"})) == "abc-123"
    # sub 前后空白归一
    assert user_id_from_token(_jwt({"sub": " abc "})) == "abc"


@pytest.mark.parametrize("token", [
    "", "not-a-jwt", "a.b", "a.b.c.d",           # 段数不对
    "header.!!!.sig",                             # payload 非 base64
    _jwt({"sub": ""}), _jwt({"sub": 123}),        # sub 缺失/非字符串
    _jwt({"other": "x"}),                         # 无 sub
    "header." + base64.urlsafe_b64encode(b'"str"').decode().rstrip("=") + ".sig",
])
def test_user_id_from_token_rejects_bad_input(token):
    assert user_id_from_token(token) == ""


def test_resolve_user_id_prefers_credential_then_token():
    # 凭证里有 account_uid 优先
    cred = CodeBuddyCredential(account_uid="uid", bearer_token=_jwt({"sub": "sub"}))
    assert resolve_user_id(cred) == "uid"
    # 回落 user_id
    cred = CodeBuddyCredential(user_id="uid2", bearer_token=_jwt({"sub": "sub"}))
    assert resolve_user_id(cred) == "uid2"
    # 都为空 → 用 token sub
    cred = CodeBuddyCredential(bearer_token=_jwt({"sub": "sub"}))
    assert resolve_user_id(cred) == "sub"
    # 全无 → 空串
    assert resolve_user_id(CodeBuddyCredential()) == ""


# -------------------------------------------------------- chat_request_event

def test_chat_request_event_shape():
    event = chat_request_event("u1", now_ms=1700000000000)
    assert event["eventCode"] == "chat_request_send"
    assert event["userId"] == "u1"
    assert event["timestamp"] == event["presentAt"] == 1700000000000
    assert event["conversationId"] == event["requestId"] == event["rootRequestId"]
    assert event["parentConversationId"] == event["conversationId"]
    assert event["agentName"] == "default" and event["agentType"] == "conversation"
    assert event["mentionContexts"] == [] and event["knowledgeId"] == []
    # 全字段形状：键数与参考实现一致（36 键），防上游后续加严
    assert len(event) == 36
    # 显式 conversationId 可用
    fixed = chat_request_event("u1", conversation_id="conv-1")
    assert fixed["conversationId"] == fixed["requestId"] == "conv-1"
    assert fixed["parentConversationId"] == "conv-1"


# -------------------------------------------------------- report_chat

async def test_report_chat_posts_event_array_with_user_id_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0, "msg": "OK"})

    cred = CodeBuddyCredential(bearer_token=_jwt({"sub": "uid-9"}))
    result = await _client(handler).report_chat(cred)
    assert result.ok is True and result.user_id == "uid-9"
    assert seen["url"] == f"https://copilot.tencent.com{EP_REPORT}"
    assert seen["headers"]["x-user-id"] == "uid-9"
    assert isinstance(seen["body"], list) and len(seen["body"]) == 1
    assert seen["body"][0]["userId"] == "uid-9"
    assert seen["body"][0]["eventCode"] == "chat_request_send"


async def test_report_chat_without_user_id_never_calls_upstream():
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"code": 0})

    result = await _client(handler).report_chat(CodeBuddyCredential(bearer_token="x"))
    assert result.ok is False and "userId" in result.message
    assert called is False


@pytest.mark.parametrize("status,body", [
    (500, {"code": 0, "msg": "boom"}),
    (401, {"code": 11101, "msg": "token expired"}),
    (200, {"code": 1234, "msg": "rejected"}),          # 业务失败也是 200
])
async def test_report_chat_surfaces_failures(status, body):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    with pytest.raises((ActivityRejected, UpstreamProtocolViolation)):
        await _client(handler).report_chat(
            CodeBuddyCredential(bearer_token=_jwt({"sub": "u1"})))


async def test_report_chat_rejects_non_json_and_non_object():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("nonjson"):
            return httpx.Response(200, content=b"<html>")
        return httpx.Response(200, json=["array"])

    cred = CodeBuddyCredential(bearer_token=_jwt({"sub": "u1"}))
    # 非 JSON → UpstreamProtocolViolation
    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).report_chat(cred)
    # 非对象信封 → UpstreamProtocolViolation（另一个 handler）
    def handler2(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["array"])

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler2).report_chat(cred)


async def test_activity_client_lazy_http_and_close():
    """未注入 client 时惰性建连；aclose 幂等（未建连也不报错）。"""
    client = CodeBuddyActivity("https://copilot.tencent.com")
    assert client._http is client._http          # noqa: SLF001 - 同一实例复用
    await client.aclose()
    await client.aclose()                        # 再关一次不报错
    empty = CodeBuddyActivity("https://x")
    await empty.aclose()                         # 从未建连


async def test_report_chat_non_json_body():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).report_chat(
            CodeBuddyCredential(bearer_token=_jwt({"sub": "u1"})))


def test_activity_message_of_falls_back_to_empty():
    """失败响应非 JSON / 非对象 / message 非字符串时返回空串。"""
    from src.provider.codebuddy.activity import _message_of

    request = httpx.Request("POST", "https://x/y")
    assert _message_of(httpx.Response(500, text="oops", request=request)) == ""
    assert _message_of(httpx.Response(500, json=["array"], request=request)) == ""
    assert _message_of(httpx.Response(
        500, json={"message": 7}, request=request)) == ""
    assert _message_of(httpx.Response(
        500, json={"message": "fallback"}, request=request)) == "fallback"


# ---------------------------------------------------------- 任务层

class _FakeProvider:
    id = "codebuddy"

    def __init__(self, *, ok: bool = True, scope: str = "s1") -> None:
        self._ok, self._scope = ok, scope
        self.calls = 0

    def activity_scope(self, _data: dict) -> str:
        return self._scope

    async def activity(self, _data: dict):
        self.calls += 1
        return ActivityResult(ok=self._ok, message="" if self._ok else "失败原因")


class _NoActivityProvider:
    id = "trae"


class _DispatchProvider:
    """按 token 分派失败模式（验证两条失败分支互不干扰）。"""

    id = "codebuddy"

    def activity_scope(self, data: dict) -> str:
        return data["bearer_token"]

    async def activity(self, data: dict):
        if data["bearer_token"] == "a":          # 第一个：正常返回 ok=False
            return ActivityResult(ok=False, message="失败原因")
        raise RuntimeError("boom")               # 第二个：抛异常


@pytest.fixture
def activity_repo(tmp_path):
    db = Database(tmp_path / "activity.sqlite3")
    apply_schema(db.connect())
    return CredentialRepository(db, CredentialCipher(SECRET))


def _add(repo, *, provider="codebuddy", token="t", disabled=False):
    credential_id = repo.add(provider=provider,
                             credential_data={"bearer_token": token})
    if disabled:
        from src.engine.scheduler import Candidate, Scheduler
        from src.provider.base import ErrKind

        repo.save_error(credential_id, Scheduler().note_error(
            Candidate(credential_id=credential_id, provider=provider),
            ErrKind.DEAD, 0))
    return credential_id


def _cn(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 21, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


async def test_activity_task_reports_once_and_closes_day(activity_repo):
    _add(activity_repo)
    provider = _FakeProvider()
    task = ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    first = await task.run_once()
    assert (first.attempted, first.succeeded) == (1, 1)
    # 当日再跑：已封账，不再调上游
    second = await task.run_once()
    assert second.attempted == 0 and second.skipped == 1
    assert provider.calls == 1


async def test_activity_task_skips_unsupported_and_disabled(activity_repo):
    _add(activity_repo, token="a")
    _add(activity_repo, token="b", disabled=True)
    _add(activity_repo, provider="trae", token="c")       # 无 activity 能力
    provider = _FakeProvider()
    task = ActivityTask(activity_repo,
                        {"codebuddy": provider, "trae": _NoActivityProvider()}, hour=10)
    report = await task.run_once()
    assert report.succeeded == 1
    assert report.skipped == 2
    assert provider.calls == 1


async def test_activity_task_skips_missing_provider_and_data():
    """provider 不认识该渠道、或凭证数据缺失 → 跳过（不调上游）。"""
    class _Repo:
        def candidates(self):
            from src.engine.scheduler import Candidate

            return [Candidate(credential_id="c1", provider="unknown"),
                    Candidate(credential_id="c2", provider="codebuddy")]

        def credential_data(self, credential_id):
            return None

    provider = _FakeProvider()
    task = ActivityTask(_Repo(), {"codebuddy": provider}, hour=10)
    report = await task.run_once()
    assert report.skipped == 2 and provider.calls == 0


async def test_activity_task_dedupes_scope_within_run(activity_repo):
    """同账号多凭证共享一次（同一轮 scope 去重；失败不封账故走到 seen 分支）。"""
    _add(activity_repo, token="a")
    _add(activity_repo, token="b")
    provider = _FakeProvider(ok=False, scope="same")
    task = ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    report = await task.run_once()
    assert (report.attempted, report.failed, report.skipped) == (1, 1, 1)
    assert provider.calls == 1


async def test_activity_task_empty_scope_falls_back_to_credential_id(activity_repo):
    """身份未知（空 scope）时绝不能共享，否则第二个账号被永久跳过。"""
    _add(activity_repo, token="a")
    _add(activity_repo, token="b")
    provider = _FakeProvider(scope="")
    task = ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    report = await task.run_once()
    assert report.attempted == 2 and provider.calls == 2


async def test_activity_task_failure_retries_and_does_not_abort(activity_repo):
    """ok=False 与抛异常都算失败、不封账（下轮重试），且互不打断。"""
    _add(activity_repo, token="a")
    _add(activity_repo, token="b")
    task = ActivityTask(activity_repo, {"codebuddy": _DispatchProvider()}, hour=10)
    report = await task.run_once()
    assert report.attempted == 2 and report.failed == 2
    again = await task.run_once()
    assert again.attempted == 2


async def test_activity_task_records_growth_event_on_success(activity_repo):
    _add(activity_repo)
    events = GrowthRepository(activity_repo._db)  # noqa: SLF001
    provider = _FakeProvider()
    task = ActivityTask(activity_repo, {"codebuddy": provider}, events=events, hour=10)
    await task.run_once()
    latest = events.latest_for(_only_id(activity_repo))
    assert latest is not None and latest["ok"] == 1
    assert "活跃上报" in latest["report"]


def _only_id(repo) -> str:
    return repo.list_all()[0]["id"]


def test_activity_task_due_only_in_configured_hour():
    task = ActivityTask.__new__(ActivityTask)
    task._hour = 10
    task._now = _cn(10, 5)
    assert task.due() is True
    task._now = _cn(9, 59)
    assert task.due() is False
    task._now = _cn(11, 0)
    assert task.due() is False


def test_activity_task_day_key_is_cn_local():
    task = ActivityTask.__new__(ActivityTask)
    task._now = _cn(0, 30)
    assert task._day_key() == "2026-09-21"


# ---------------------------------------------------------- 配置装配

def test_build_runner_activity_toggle(tmp_path):
    """活跃上报恒建对象，开关只决定跑不跑（B3.2 起可热更）。

    以前是「关闭就不装配」，那样管理台把开关打开后必须重启才生效；现在
    对象总在，`_sync_activity` 每轮现读开关。
    """
    from src.tasks.runner import build_runner

    db = Database(str(tmp_path / "runner.sqlite3"))
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))

    class _Collector:
        pass

    off = build_runner(repo, {}, _Collector(), Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path)))
    assert off._activity is not None            # noqa: SLF001 - 恒建对象
    assert off._activity_enabled() is False     # noqa: SLF001 - 默认关闭
    on = build_runner(repo, {}, _Collector(), Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
        ACTIVITY_REPORT_ENABLED="true", ACTIVITY_REPORT_HOUR="7"))
    assert on._activity is not None             # noqa: SLF001
    assert on._activity_enabled() is True       # noqa: SLF001
    assert on._activity._hour == 7              # noqa: SLF001


async def test_runner_sync_activity_skips_when_disabled(activity_repo):
    """开关关闭时 _sync_activity 是 no-op，即使正处在配置小时窗口内。"""
    from src.tasks.activity import ActivityTask as _ActivityTask
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask
    from src.tasks.retention import RetentionTask
    from src.tasks.runner import TaskRunner

    _add(activity_repo)
    provider = _FakeProvider()
    activity = _ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    activity._now = _cn(10, 5)                  # 窗口内
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(activity_repo, {"codebuddy": provider}, None),
        checkin=CheckinTask(activity_repo, {"codebuddy": provider}),
        activity=activity,
        refresh=RefreshTask(activity_repo, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(_NullCollector()),
        activity_enabled=lambda: False,
    )
    assert await runner._sync_activity() is None      # noqa: SLF001 - 开关关
    assert provider.calls == 0


async def test_runner_sync_activity_respects_hour_window(activity_repo):
    """_sync_activity 只在配置小时窗口内真的跑一轮，窗口外是 no-op。"""
    from src.tasks.activity import ActivityTask as _ActivityTask
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask
    from src.tasks.retention import RetentionTask
    from src.tasks.runner import TaskRunner

    _add(activity_repo)
    provider = _FakeProvider()
    activity = _ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    activity._now = _cn(9, 0)              # 窗口外
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(activity_repo, {"codebuddy": provider}, None),
        checkin=CheckinTask(activity_repo, {"codebuddy": provider}),
        activity=activity,
        refresh=RefreshTask(activity_repo, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(_NullCollector()),
    )
    assert await runner._sync_activity() is None      # noqa: SLF001 - 窗口外
    assert provider.calls == 0
    activity._now = _cn(10, 0)                        # 窗口内
    await runner._sync_activity()                     # noqa: SLF001
    assert provider.calls == 1


async def test_runner_start_includes_activity_loop(activity_repo):
    """装配了 activity 时，start 要多起一条「活跃上报」循环。"""
    from src.tasks.activity import ActivityTask as _ActivityTask
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask
    from src.tasks.retention import RetentionTask
    from src.tasks.runner import TaskRunner

    provider = _FakeProvider()
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(activity_repo, {"codebuddy": provider}, None),
        checkin=CheckinTask(activity_repo, {"codebuddy": provider}),
        activity=_ActivityTask(activity_repo, {"codebuddy": provider}, hour=10),
        refresh=RefreshTask(activity_repo, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(_NullCollector()),
    )
    await runner.start()
    try:
        # 额度/刷新/清理/签到/活跃上报
        assert len(runner._tasks) == 5      # noqa: SLF001
    finally:
        await runner.stop()


class _NullCollector:
    """RetentionTask 需要的最小 collector 接口（本用例不触发清理）。"""

    def purge_before(self, _cutoff):  # pragma: no cover - 本用例不调用
        return 0


# ---------------------------------------------------------- provider 方法


async def test_provider_activity_uses_token_sub(monkeypatch):
    """CodeBuddyProvider.activity 走 _cached_activity，userId 取自 JWT sub。"""
    from src.provider.codebuddy.client import CodeBuddyProvider

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"code": 0, "msg": "OK"})

    transport = httpx.MockTransport(handler)
    provider = CodeBuddyProvider()
    provider.client._short_client = httpx.AsyncClient(  # noqa: SLF001
        transport=transport, timeout=None)
    credential = {"bearer_token": _jwt({"sub": "uid-77"})}
    result = await provider.activity(credential)
    assert result.ok is True
    await provider.activity(credential)          # 第二次命中 _ACTIVITY_CACHE
    assert seen["body"][0]["userId"] == "uid-77"
    assert seen["headers"]["x-user-id"] == "uid-77"
    # activity_scope：endpoint + userId
    assert provider.activity_scope(credential) == (
        f"{provider.client.endpoint.rstrip('/')}|uid-77")
    # 身份完全缺失 → 空 scope（调用方回落 credential_id）
    assert provider.activity_scope({"bearer_token": "x"}) == ""
    await provider.aclose()


async def test_provider_activity_rejects_missing_user_id():
    from src.provider.codebuddy.client import CodeBuddyProvider

    provider = CodeBuddyProvider()
    result = await provider.activity({"bearer_token": "not-a-jwt"})
    assert result.ok is False and "userId" in result.message


# ---------------------------------------------------------- 管理台入口

def test_admin_activity_endpoint(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")

    class _Provider:
        id = "codebuddy"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "")}

        def activity_scope(self, _data):
            return "s1"

        async def activity(self, _data):
            return ActivityResult(ok=True, message="", user_id="u1")

        async def list_models(self, _data):
            return []

        async def probe_quota(self, _data):  # pragma: no cover - 未被本用例调用
            raise NotImplementedError

    app = build_app(settings, providers={"codebuddy": _Provider()})
    from src.auth.session import create_session_token

    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
        assert created.status_code == 200
        cid = created.json()["id"]
        ok = client.post(f"/api/credentials/{cid}/activity")
        assert ok.status_code == 200 and ok.json()["ok"] is True
        # 已落一条 growth_events
        listed = client.get(f"/api/credentials/{cid}/growth").json()["events"]
        assert listed and "活跃上报" in listed[0]["report"]


def test_admin_activity_endpoint_failure_records_nothing(tmp_path):
    """上报失败（ok=False）返回 200 但不落 growth_events（无事可汇报）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")

    class _Failing:
        id = "codebuddy"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "")}

        def activity_scope(self, _data):
            return "s1"

        async def activity(self, _data):
            return ActivityResult(ok=False, message="无法确定账号 userId")

        async def list_models(self, _data):
            return []

        async def probe_quota(self, _data):  # pragma: no cover - 未被本用例调用
            raise NotImplementedError

    app = build_app(settings, providers={"codebuddy": _Failing()})
    from src.auth.session import create_session_token

    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
        cid = created.json()["id"]
        resp = client.post(f"/api/credentials/{cid}/activity")
        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "message": "无法确定账号 userId"}
        assert client.get(f"/api/credentials/{cid}/growth").json()["events"] == []


def test_admin_activity_endpoint_rejects_unsupported(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")

    class _NoActivity:
        id = "codebuddy"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "")}

        async def list_models(self, _data):
            return []

        async def probe_quota(self, _data):  # pragma: no cover - 未被本用例调用
            raise NotImplementedError

    app = build_app(settings, providers={"codebuddy": _NoActivity()})
    from src.auth.session import create_session_token

    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
        cid = created.json()["id"]
        bad = client.post(f"/api/credentials/{cid}/activity")
        assert bad.status_code == 400
        missing = client.post("/api/credentials/cred_nope/activity")
        assert missing.status_code == 400
