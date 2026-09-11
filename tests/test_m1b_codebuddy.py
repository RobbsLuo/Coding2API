"""M1b 契约测试：CodeBuddy provider（fixture 驱动）。

fixture 从 codebuddy2api 的 tests/test_stream_service.py 提取（真实 SSE 结构）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import Scheduler
from src.engine.sse import parse_frames
from src.main import build_app
from src.provider.base import ErrKind, Event, EventKind
from src.provider.codebuddy import events as cb_events
from src.provider.codebuddy.client import (
    DEFAULT_MODELS,
    CodeBuddyClient,
    CodeBuddyCredential,
    CodeBuddyProvider,
    UpstreamHTTPError,
    build_headers,
    parse_credential,
)
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.headers import encode_department, host_of

FIXTURES = Path(__file__).parent.parent / "src" / "provider" / "fixtures" / "codebuddy"


def _quota_body(accounts: object) -> dict:
    """包出上游真实的个人版额度响应结构（data.Response.Data.Accounts）。"""
    return {"code": 0, "msg": "OK",
            "data": {"Response": {"Data": {"Accounts": accounts},
                                  "RequestId": "<redacted>"}}}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _client(handler) -> CodeBuddyClient:
    transport = httpx.MockTransport(handler)
    return CodeBuddyClient(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None))


# --------------------------------------------------------------- 请求头

def test_host_and_domain_derived_from_same_endpoint():
    headers = build_headers(CodeBuddyCredential(bearer_token="t"), "https://copilot.tencent.com")
    assert headers["Host"] == headers["X-Domain"] == "copilot.tencent.com"


def test_department_encoded_as_utf8_percent():
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", enterprise_id="e",
                            department_full_name="技术部/平台组"),
        "https://copilot.tencent.com")
    encoded = "%E6%8A%80%E6%9C%AF%E9%83%A8%2F%E5%B9%B3%E5%8F%B0%E7%BB%84"
    assert headers["X-Department-Info"] == encoded
    assert headers["X-Enterprise-Id"] == "e"


def test_account_uid_takes_priority_over_user_id():
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", user_id="user", account_uid="acct"),
        "https://copilot.tencent.com")
    assert headers["X-User-Id"] == "acct"


def test_quota_only_headers_drop_enterprise_context():
    """手动凭证的额度探测只切接口，不发企业上下文头（AGENTS.md 约束）。"""
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", enterprise_id="e",
                            department_full_name="技术部", user_id="u"),
        "https://copilot.tencent.com", quota_only=True)
    assert "X-Enterprise-Id" not in headers
    assert "X-Department-Info" not in headers
    assert "X-User-Id" not in headers


def test_host_of_and_encode_department_helpers():
    assert host_of("https://www.codebuddy.ai") == "www.codebuddy.ai"
    assert encode_department("") == ""


# --------------------------------------------------------------- 凭证解析

def test_parse_credential_bearer_only():
    credential = parse_credential({"token": "abc"})
    assert credential.bearer_token == "abc"
    assert credential.auth_source == "manual"
    assert credential.is_oauth is False


@pytest.mark.parametrize("raw", [
    b"{broken", b"[1,2]", b"{}", b'{"token":""}', b'{"token":"   "}',
])
def test_parse_credential_rejects_malformed(raw):
    with pytest.raises(UpstreamProtocolViolation):
        parse_credential(raw)


def test_parse_credential_accepts_all_token_key_names():
    for key in ("bearer_token", "access_token", "token"):
        assert parse_credential({key: "v"}).bearer_token == "v"


def test_auth_source_normalization():
    assert parse_credential({"token": "t", "auth_source": "oauth"}).auth_source == "oauth"
    assert parse_credential({"token": "t", "auth_source": "hacked"}).auth_source == "unknown"
    assert CodeBuddyCredential.from_dict({"auth_source": "manual"}).auth_source == "manual"


def test_quota_probe_mode_normalization():
    enterprise = CodeBuddyCredential.from_dict({"quota_probe_mode": "enterprise"})
    assert enterprise.quota_probe_mode == "enterprise"
    junk = CodeBuddyCredential.from_dict({"quota_probe_mode": "junk"})
    assert junk.quota_probe_mode == "personal"


def test_bearer_only_credential_never_needs_refresh():
    """手动 bearer-only 凭证不得进入 OAuth 刷新流程（AGENTS.md 约束）。"""
    manual = CodeBuddyCredential(bearer_token="t", refresh_token="r", expires_at=1)
    assert manual.needs_refresh(86400, now=10_000) is False
    oauth = CodeBuddyCredential(bearer_token="t", refresh_token="r", expires_at=100,
                                auth_source="oauth")
    assert oauth.needs_refresh(86400, now=10_000) is True
    assert oauth.needs_refresh(1, now=1) is False


def test_credential_dict_roundtrip():
    original = CodeBuddyCredential(bearer_token="t", user_id="u", account_uid="a",
                                   enterprise_id="e", auth_source="oauth",
                                   quota_probe_mode="enterprise", nickname="n")
    assert CodeBuddyCredential.from_dict(original.to_dict()) == original


# ------------------------------------------------------------- 事件映射

def test_basic_fixture_maps_to_events():
    events = [e for e in (cb_events.parse_frame(f)
                          for f in parse_frames(fixture("chat-basic.sse"))) if e]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.CONTENT, EventKind.USAGE]
    assert events[0].content == "你好"
    assert events[1].content == "，世界"
    assert events[2].usage.input_tokens == 11


def test_done_marker_and_empty_frame_yield_nothing():
    assert cb_events.parse_frame(cb_events.SSEFrame(event="", data="[DONE]")) is None
    assert cb_events.parse_frame(cb_events.SSEFrame(event="", data="")) is None


def test_parse_all_events_emits_usage_and_finish_together():
    """收尾帧同时带 finish_reason 与 usage → 两个事件都产出。"""
    frame = cb_events.SSEFrame(
        event="",
        data='{"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3}}')
    kinds = [e.kind for e in cb_events.parse_all_events(frame)]
    assert kinds == [EventKind.USAGE, EventKind.FINISH]


def test_parse_all_events_keeps_reasoning_then_content_priority():
    """reasoning 与 content 同帧时按 content 优先（单事件语义）。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"reasoning_content":"r","content":"c"}}]}')
    events = cb_events.parse_all_events(frame)
    assert [e.kind for e in events] == [EventKind.CONTENT]


def test_reasoning_fixture_events():
    events = [e for e in (cb_events.parse_frame(f)
                          for f in parse_frames(fixture("chat-reasoning.sse"))) if e]
    assert [e.kind for e in events] == [EventKind.REASONING, EventKind.REASONING,
                                        EventKind.CONTENT]
    assert events[0].content == "我"


def test_tool_calls_fixture_keeps_upstream_ids():
    events = [e for e in (cb_events.parse_frame(f)
                          for f in parse_frames(fixture("tool-calls.sse"))) if e]
    assert all(e.kind is EventKind.TOOL_CALLS for e in events)
    assert events[0].tool_calls[0]["id"] == "call_1"
    assert "id" not in events[1].tool_calls[0]          # 分片不重生成 id


def test_tool_call_list_with_non_dict_entries_is_filtered():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"tool_calls":[1,{"id":"ok"}]}}]}')
    assert cb_events.parse_frame(frame).tool_calls == [{"id": "ok"}]


@pytest.mark.parametrize("data", ["{broken", "[1,2]", '{"choices":"no"}',
                                  '{"choices":[5]}'])
def test_malformed_frames_raise(data):
    with pytest.raises(UpstreamProtocolViolation):
        cb_events.parse_frame(cb_events.SSEFrame(event="", data=data))


def test_choices_empty_array_is_tolerated():
    frame = cb_events.SSEFrame(event="", data='{"choices":[],"usage":{"prompt_tokens":3}}')
    assert cb_events.parse_frame(frame).usage.input_tokens == 3


def test_finish_only_frame():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{},"finish_reason":"length"}]}')
    assert cb_events.parse_frame(frame).finish_reason == "length"


def test_usage_credit_is_optional():
    frame = cb_events.SSEFrame(event="", data='{"usage":{"credit":0.25,"prompt_tokens":1}}')
    usage = cb_events.parse_frame(frame).usage
    assert usage.credit == 0.25


def test_usage_ignores_boolean_and_non_numeric_values():
    frame = cb_events.SSEFrame(
        event="", data='{"usage":{"prompt_tokens":true,"credit":true,"completion_tokens":"x"}}')
    usage = cb_events.parse_frame(frame).usage
    assert usage.input_tokens is None and usage.output_tokens is None and usage.credit is None


# ----------------------------------------------------------- 错误分类

@pytest.mark.parametrize(("status", "expected"), [
    (401, ErrKind.DEAD), (403, ErrKind.DEAD), (404, ErrKind.SOFT), (429, ErrKind.SOFT),
    (500, ErrKind.OTHER), (400, ErrKind.OTHER), (200, ErrKind.OTHER),
])
def test_classify_status(status, expected):
    assert cb_events.classify_status(status) is expected


def test_classify_body_markers():
    assert cb_events.classify_status(400, fixture("error-1005.json").encode()) is ErrKind.PLAN
    assert cb_events.classify_status(400, b'{"code": 1005, "plan": "x"}') is ErrKind.PLAN
    assert cb_events.classify_status(401, fixture("error-401.json").encode()) is ErrKind.DEAD
    assert cb_events.classify_error_code(1005) is ErrKind.PLAN
    assert cb_events.classify_error_code(500) is ErrKind.OTHER


# ------------------------------------------------------------- 客户端

async def test_stream_chat_yields_events_from_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/chat/completions"
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    events = [e async for e in _client(handler).stream_chat(
        CodeBuddyCredential(bearer_token="t"), {"messages": []}, "glm-5.2")]
    # 收尾帧同时带 finish_reason 与 usage → 两个事件都产出
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.CONTENT,
                                        EventKind.USAGE, EventKind.FINISH]


async def test_stream_chat_raises_classified_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"rate limited")

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat(
            CodeBuddyCredential(bearer_token="t"), {}, "m")]
    assert caught.value.kind() is ErrKind.SOFT


async def test_stream_chat_forces_stream_true_even_if_client_sent_false():
    """上游只有流式；客户端 stream=false 也必须强制 true（AGENTS.md 约束）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    [e async for e in _client(handler).stream_chat(
        CodeBuddyCredential(bearer_token="t"), {"stream": False}, "glm-5.2")]
    assert seen["stream"] is True and seen["model"] == "glm-5.2"


async def test_fetch_personal_quota_parses_real_nested_response():
    """真实响应是 data.Response.Data.Accounts，且 *Precise 是字符串。

    用抓取的真实结构做契约测试：层级读错或忽略字符串都会让有效凭证
    被误判成「未探测到额度」。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("quota-personal.json"))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    # fixture 是脱敏后的真实响应：两个 Status=0 的套餐
    #   CodeBuddy个人体验版        500 / 500
    #   CodeBuddy个人版国内运营裂变包 5000 / 4767.50000158
    assert quota.total == 5500.0
    assert quota.remaining == pytest.approx(5267.50000158)
    assert quota.cycle_end is not None
    assert quota.probe_failed is False
    assert quota.remaining < quota.total      # 真实已用量必须体现出来


async def test_fetch_personal_quota_prefers_precise_over_plain():
    """Precise 优先，且能解析字符串；被禁用的套餐（Status!=0）要跳过。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "60", "CycleCapacitySize": 999,
             "CycleEndTime": "2026-12-31 23:59:59"},
            {"Status": 0, "CycleCapacitySize": 50, "CycleCapacityRemain": 10},
            {"Status": 1, "CycleCapacitySizePrecise": "777",
             "CycleCapacityRemainPrecise": "777"},
            "junk",
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 150.0      # 100(Precise 字符串) + 50(回退非 Precise)
    assert quota.remaining == 70.0
    assert quota.cycle_end is not None


@pytest.mark.parametrize("accounts", [None, []])
async def test_fetch_quota_accepts_null_accounts_as_no_personal_quota(accounts):
    """个人版 Accounts: null / [] 表示探测成功但没有额度（AGENTS.md 约束）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 0 and quota.remaining == 0
    assert quota.probe_failed is False


@pytest.mark.parametrize("body", [
    {},                                     # 完全没有 data
    {"data": None},
    {"data": {}},                           # data 里没有 Accounts
    {"data": {"Response": {}}},
    {"data": {"Response": {"Data": {}}}},
    {"data": {"Accounts": "no"}},           # 类型错误
])
async def test_fetch_quota_missing_accounts_fails(body):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))


async def test_fetch_enterprise_quota_requires_oauth():
    """企业额度接口只对 OAuth 凭证开放；手动凭证走个人版。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("get-enterprise-user-usage"):
            return httpx.Response(200, json={"credit": 30, "limitNum": 100})
        return httpx.Response(200, json=_quota_body([]))

    oauth = CodeBuddyCredential(bearer_token="t", auth_source="oauth",
                                enterprise_id="e", quota_probe_mode="enterprise")
    quota = await _client(handler).fetch_quota(oauth)
    assert quota.total == 100 and quota.remaining == 70
    assert calls[-1].endswith("get-enterprise-user-usage")

    manual = CodeBuddyCredential(bearer_token="t", quota_probe_mode="enterprise")
    await _client(handler).fetch_quota(manual)
    assert calls[-1].endswith("get-user-resource")


async def test_fetch_enterprise_quota_rejects_missing_limit():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credit": 5})

    oauth = CodeBuddyCredential(bearer_token="t", auth_source="oauth",
                                quota_probe_mode="enterprise")
    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_quota(oauth)


async def test_post_json_error_paths():
    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await _client(failing).fetch_quota(CodeBuddyCredential(bearer_token="t"))

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(not_json).fetch_quota(CodeBuddyCredential(bearer_token="t"))

    def not_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1])

    with pytest.raises(UpstreamProtocolViolation):
        await _client(not_object).fetch_quota(CodeBuddyCredential(bearer_token="t"))


async def test_client_lazy_properties_and_close():
    client = CodeBuddyClient()
    assert client._stream is client._stream
    assert client._short is client._short
    await client.aclose()
    await client.aclose()


def test_provider_import_classify_and_models():
    provider = CodeBuddyProvider()
    data = provider.import_credential({"token": "abc"})
    assert data["bearer_token"] == "abc" and data["auth_source"] == "manual"
    assert provider.classify(429, b"") is ErrKind.SOFT
    assert [m.id for m in provider.list_models({})] == list(DEFAULT_MODELS)
    assert provider.host() == "copilot.tencent.com"
    with pytest.raises(UpstreamProtocolViolation):
        provider.import_credential({})


async def test_provider_stream_and_probe_delegate():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(handler))
    events = [e async for e in provider.stream_chat({"bearer_token": "t"}, {}, "m")]
    assert events[-1].kind is EventKind.FINISH
    assert any(e.kind is EventKind.USAGE for e in events)


# ------------------------------------------------ 双 provider 端到端（Q25=A）

@pytest.fixture()
def dual_repo(tmp_path):
    db = Database(tmp_path / "dual.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher("s")), db
    db.close()


class DualProvider:
    """同一脚本双 provider：验证调度层对两个上游行为一致。"""

    def __init__(self, provider_id: str, script) -> None:
        self.id = provider_id
        self.script = script
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    def list_models(self, _credential_data):
        return []


GOOD = [Event(kind=EventKind.CONTENT, content="ok"),
        Event(kind=EventKind.FINISH, finish_reason="stop")]


def _dual_executor(credentials, trae_script, cb_script, **kw):
    trae = DualProvider("trae", trae_script)
    codebuddy = DualProvider("codebuddy", cb_script)
    executor = Executor(ExecutorDeps(
        providers={"trae": trae, "codebuddy": codebuddy}, credentials=credentials,
        scheduler=Scheduler(**kw), default_model="glm-5.2"))
    return executor, trae, codebuddy


def _request(model="glm-5.2"):
    from src.compat.openai.request import parse_chat_request

    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                               "model": model})


class Boom(Exception):
    def __init__(self, kind: ErrKind, message: str = "upstream error") -> None:
        super().__init__(message)
        self._kind = kind

    def kind(self) -> ErrKind:
        return self._kind


async def test_dual_provider_same_model_routes_and_fails_over(dual_repo):
    """同一扁平模型名下，一个上游失败 → 自动落到另一个（Q21=C 的核心价值）。

    用 pin 固定首个上游，避免依赖同分时的字母序挑选。
    """
    credentials, db = dual_repo
    trae_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    credentials.set_pinned(trae_id)          # 固定先走 trae
    executor, trae, codebuddy = _dual_executor(
        credentials, [[Boom(ErrKind.SOFT)]], [GOOD])
    result = await executor.complete(_request())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and codebuddy.calls == 1
    assert credentials.candidates(["trae"])[0].cooling_until is not None
    assert credentials.candidates(["codebuddy"])[0].cooling_until is None
    db.close()


async def test_dual_provider_forced_suffix_targets_single_upstream(dual_repo):
    """model@codebuddy 强制只走 codebuddy，不落到 trae。"""
    credentials, db = dual_repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    executor, trae, codebuddy = _dual_executor(credentials, [GOOD], [GOOD])
    await executor.complete(_request("glm-5.2@codebuddy"))
    assert codebuddy.calls == 1 and trae.calls == 0
    db.close()


async def test_dual_provider_error_classification_is_consistent(dual_repo):
    """两个 provider 的错误分类都落到同一冷却语义（Q25=A 验证点）。"""
    credentials, db = dual_repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    executor, _trae, _codebuddy = _dual_executor(
        credentials, [[Boom(ErrKind.PLAN)]], [[Boom(ErrKind.PLAN)]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    for candidate in credentials.candidates():
        assert candidate.cooling_until is not None, candidate
    db.close()


async def test_dual_provider_no_credential_when_both_registered(dual_repo):
    credentials, db = dual_repo
    executor, _trae, _codebuddy = _dual_executor(credentials, [GOOD], [GOOD])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    db.close()


# ------------------------------------------------------------------- API

def test_default_registry_contains_both_providers(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        key = app.state.api_keys.create("root")["api_key"]
        data = client.get("/v1/models",
                          headers={"Authorization": f"Bearer {key}"}).json()["data"]
    by_id = {item["id"]: item["providers"] for item in data}
    assert by_id["glm-5.2"] == ["codebuddy", "trae"]
    assert "deepseek-v4-pro" in by_id


def test_codebuddy_import_via_api(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    from src.auth.session import create_session_token

    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", "s"))
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
        assert created.status_code == 200
        listed = client.get("/api/credentials").json()["credentials"]
        assert listed[0]["provider"] == "codebuddy"
        bad = client.post("/api/credentials", json={"provider": "codebuddy",
                                                    "credential": {}})
        assert bad.status_code == 400


# ---------------------------------------------------- 覆盖率收尾

async def test_fetch_quota_skips_packages_with_zero_total():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body([
            {"Status": 0, "CycleCapacitySizePrecise": "0", "CycleCapacityRemainPrecise": "5"},
            {"Status": 0, "CycleCapacitySizePrecise": "10",
             "CycleCapacityRemainPrecise": "10"},
        ]))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 10 and quota.remaining == 10


async def test_fetch_quota_tolerates_bad_cycle_end_format():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body([
            {"Status": 0, "CycleCapacitySizePrecise": "5",
             "CycleCapacityRemainPrecise": "5", "CycleEndTime": "not-a-date"},
        ]))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.cycle_end is None


def test_cycle_end_accepts_iso_separator():
    from src.provider.codebuddy.client import _cycle_end_epoch

    assert _cycle_end_epoch("2026-12-31T23:59:59") is not None
    assert _cycle_end_epoch(None) is None
    assert _cycle_end_epoch("") is None


async def test_provider_probe_quota_delegates():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("quota-personal.json"))

    provider = CodeBuddyProvider(client=_client(handler))
    quota = await provider.probe_quota({"bearer_token": "t"})
    assert quota.total == 5500.0


def test_auth_start_headers_mark_anonymous():
    from src.provider.codebuddy.headers import auth_start_headers

    headers = auth_start_headers("copilot.tencent.com")
    assert headers["X-No-Authorization"] == "true"
    assert "Authorization" not in headers


def test_parse_all_events_returns_empty_for_done_and_blank():
    assert cb_events.parse_all_events(cb_events.SSEFrame(event="", data="[DONE]")) == []
    assert cb_events.parse_all_events(cb_events.SSEFrame(event="", data="")) == []


@pytest.mark.parametrize("data", ["{broken", "[1,2]"])
def test_parse_all_events_raises_on_malformed(data):
    with pytest.raises(UpstreamProtocolViolation):
        cb_events.parse_all_events(cb_events.SSEFrame(event="", data=data))


def test_parse_all_events_with_tool_calls_does_not_duplicate_finish():
    """带 tool_calls 的收尾帧：工具事件 + finish，且 usage 不重复。"""
    frame = cb_events.SSEFrame(event="", data=(
        '{"choices":[{"delta":{"tool_calls":[{"id":"c"}]},"finish_reason":"tool_calls"}],'
        '"usage":{"prompt_tokens":2}}'))
    kinds = [e.kind for e in cb_events.parse_all_events(frame)]
    assert kinds == [EventKind.TOOL_CALLS, EventKind.USAGE, EventKind.FINISH]


def test_tool_calls_list_of_only_non_dicts_falls_through():
    """tool_calls 全是非 dict → 不算工具事件，继续走 content（events 42→45）。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"tool_calls":[1,2],"content":"c"}}]}')
    assert cb_events.parse_frame(frame).kind is EventKind.CONTENT


def test_content_non_string_falls_through_to_reasoning():
    """content 非字符串 → 继续看 reasoning（events 46→下一分支）。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"content":5,"reasoning_content":"r"}}]}')
    assert cb_events.parse_frame(frame).kind is EventKind.REASONING


def test_reasoning_non_string_is_ignored():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"reasoning_content":5}}]}')
    assert cb_events.parse_frame(frame) is None


def test_finish_reason_non_string_is_ignored():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{},"finish_reason":5}]}')
    assert cb_events.parse_frame(frame) is None


def test_choices_missing_returns_none_choice():
    """没有 choices 键 → 不报错，按无 choice 处理。"""
    frame = cb_events.SSEFrame(event="", data='{"usage":{"prompt_tokens":1}}')
    assert cb_events.parse_frame(frame).kind is EventKind.USAGE


async def test_aclose_when_only_short_client_was_created():
    """只创建 short 客户端时，aclose 跳过 stream（client 159→158）。"""
    client = CodeBuddyClient()
    assert client._short is not None    # 只实例化 short
    await client.aclose()


# --------------------------------------------- _number 的字符串解析

@pytest.mark.parametrize(("value", "expected"), [
    (500, 500.0),
    (500.5, 500.5),
    ("500", 500.0),          # 上游 Precise 字段是字符串
    (" 4767.50000158 ", 4767.50000158),
    ("", None),              # 空字符串
    ("   ", None),
    ("not-a-number", None),  # 无法解析
    (None, None),
    ([], None),
    (True, None),            # 布尔不算数字
    (False, None),
])
def test_number_parses_strings_and_rejects_junk(value, expected):
    """额度解析必须能处理字符串型 Precise，否则整批额度会被算成 0。"""
    from src.provider.codebuddy.client import _number

    result = _number(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)
