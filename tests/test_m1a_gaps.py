"""M1a 覆盖率收尾：把剩余边界分支推到 100%。"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.compat.openai.request import parse_chat_request
from src.config import Settings
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import Scheduler
from src.main import build_app
from src.provider.base import ErrKind, Event, EventKind, Model
from src.provider.trae.client import (
    TraeClient,
    TraeCredential,
    prepare_body,
)
from src.provider.trae.events import UpstreamProtocolViolation
from tests.conftest import SECRET


class _Provider:
    id = "trae"

    def __init__(self, script=None):
        self.script = script or []
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    def list_models(self, _credential_data):
        return [Model(id="glm-5.2")]

    def import_credential(self, raw):
        if not raw.get("accessToken"):
            raise UpstreamProtocolViolation("credential missing accessToken")
        return dict(raw)


GOOD = [Event(kind=EventKind.CONTENT, content="hi"),
        Event(kind=EventKind.FINISH, finish_reason="stop")]


def _repo(tmp_path):
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository

    db = Database(tmp_path / "c.sqlite3")
    apply_schema(db.connect())
    return CredentialRepository(db, CredentialCipher(SECRET)), db


def _executor(credentials, provider, **kw):
    return Executor(ExecutorDeps(providers={"trae": provider}, credentials=credentials,
                                 scheduler=Scheduler(**kw), default_model="glm-5.2"))


# ----------------------------------------------------------- executor 分支

class SoftError(Exception):
    def __init__(self) -> None:
        super().__init__("upstream soft rate limit")

    def kind(self):  # noqa: ANN201
        return ErrKind.SOFT


class PlanError(Exception):
    def __init__(self) -> None:
        super().__init__("upstream plan exhausted")

    def kind(self):  # noqa: ANN201
        return ErrKind.PLAN


async def test_stream_error_then_second_credential_succeeds(tmp_path):
    """第一次 SOFT 失败 → 换号 → 成功；覆盖 rotate 后继续的分支。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    executor = _executor(credentials, _Provider([[SoftError()], GOOD]))
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"hi" in chunks[0]
    db.close()


async def test_stream_unknown_error_propagates(tmp_path):
    """不认识的上游异常必须原样抛出（不静默、不误判为冷却）。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    executor = _executor(credentials, _Provider([[RuntimeError("boom")]]))
    with pytest.raises(RuntimeError):
        [c async for c in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    db.close()


async def test_stream_inline_error_frame_classified_as_plan(tmp_path):
    """流内 1005 → PLAN 长冷却（_event_kind 分支）。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    provider = _Provider([[Event(kind=EventKind.ERROR, error_code=1005)],
                          [Event(kind=EventKind.ERROR, error_code=500)]])
    executor = _executor(credentials, provider)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"no_healthy_credential" in chunks[-1]
    assert credentials.candidates()[0].cooling_until is not None
    db.close()


async def test_complete_rotates_then_exhausts(tmp_path):
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    executor = _executor(credentials, _Provider([[PlanError()]]))
    with pytest.raises(NoHealthyCredential) as caught:
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert "upstream" in str(caught.value)          # last_error 被拼进消息
    db.close()


# --------------------------------------------------------------- main 分支

def _client(tmp_path, provider=None):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": provider or _Provider([GOOD])})
    return app, TestClient(app)


def test_forbidden_handler_returns_403(tmp_path):
    app, client = _client(tmp_path)
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    with client:
        response = client.post("/api/credentials", json={"provider": "trae",
                                                         "credential": {}})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_unknown_model_provider_returns_400(tmp_path):
    app, client = _client(tmp_path)
    key = app.state.api_keys.create("root")["api_key"]
    with client:
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"messages": [{"role": "user", "content": "x"}],
                                     "model": "glm-5.2@nope"})
    assert response.status_code == 400


def test_no_provider_for_model_returns_400(tmp_path):
    """模型强制指定到未注册的 provider → 400。"""
    app, client = _client(tmp_path, provider=_Provider([GOOD]))
    key = app.state.api_keys.create("root")["api_key"]
    with client:
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"messages": [{"role": "user", "content": "x"}],
                                     "model": "glm-5.2@codebuddy"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_no_healthy_credential_returns_503(tmp_path):
    app, client = _client(tmp_path, provider=_Provider([GOOD]))
    key = app.state.api_keys.create("root")["api_key"]
    with client:
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"messages": [{"role": "user", "content": "x"}]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_healthy_credential"


def test_streaming_endpoint_returns_sse(tmp_path):
    app, client = _client(tmp_path)
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    with client:
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"messages": [{"role": "user", "content": "hi"}],
                                     "stream": True})
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert "data: [DONE]" in response.text


def test_toggle_missing_credential_returns_400(tmp_path):
    app, client = _client(tmp_path)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        response = client.post("/api/credentials/ghost/toggle", json={"enabled": False})
    assert response.status_code == 400


# ------------------------------------------------------- trae client 分支

def _mock_client(handler) -> TraeClient:
    transport = httpx.MockTransport(handler)
    return TraeClient(stream_client=httpx.AsyncClient(transport=transport, timeout=None),
                      short_client=httpx.AsyncClient(transport=transport, timeout=None))


def test_prepare_body_function_choice_without_name_is_dropped():
    body = prepare_body({"messages": [], "tool_choice": {"type": "function",
                                                         "function": {}}}, "m")
    assert "tool_choice" not in body


def test_solo_headers_include_device_and_auth():
    from src.provider.trae.client import solo_headers

    headers = solo_headers(TraeCredential(access_token="a", machine_id="m", device_id="d"))
    assert headers["X-Machine-Id"] == "m" and headers["X-Device-Id"] == "d"
    # 原 SOLOHeaders 实测必须的头：Authorization 与 X-Cloudide-Token 并存
    assert headers["Authorization"] == "Cloud-IDE-JWT a"
    assert headers["X-Cloudide-Token"] == "a"
    assert headers["Request-Traffic-Type"] == "prod"


def test_ug_headers_carry_region_and_auth():
    """积分/签到端点（api.trae.cn）的独立头集合——此前缺失导致探测 401。"""
    from src.provider.trae.client import ug_headers

    headers = ug_headers(TraeCredential(access_token="a", device_id="d"))
    assert headers["Authorization"] == "Cloud-IDE-JWT a"
    assert headers["X-User-Region"] == "CN"
    assert headers["X-Device-Id"] == "d"
    assert "X-Cloudide-Token" not in headers


async def test_client_aclose_is_idempotent():
    client = TraeClient()
    client._stream()
    client._short()
    await client.aclose()
    await client.aclose()


async def test_provider_refresh_delegates_to_client():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {"Token": "t2", "RefreshToken": "r2"}})

    provider_refresh = _mock_client(handler)
    from src.provider.trae.client import TraeProvider

    provider = TraeProvider(client=provider_refresh)
    refreshed = await provider.refresh({"accessToken": "a", "refreshToken": "r"})
    assert refreshed["accessToken"] == "t2" and refreshed["refreshToken"] == "r2"


async def test_fetch_models_accepts_response_missing_display_config():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": [{"config_name": "ok"}]})

    models = await _mock_client(handler).fetch_models(TraeCredential(access_token="a"))
    assert models == [Model(id="ok", name="")]


# --------------------------------------------------------- events 剩余分支

def test_classify_status_2xx_falls_back_to_other():
    from src.provider.trae import events as trae_events

    assert trae_events.classify_status(200) is ErrKind.OTHER


def test_output_frame_with_non_string_reasoning_is_ignored():
    from src.provider.trae import events as trae_events

    frame = trae_events.SSEFrame(event="output",
                                 data='{"response":"x","reasoning_content":5}')
    assert [e.kind for e in trae_events.parse_all_events(frame)] == [EventKind.CONTENT]


def test_usage_frame_from_sync_parse_keeps_nulls_for_missing_keys():
    from src.provider.trae import events as trae_events

    event = trae_events.parse_frame(trae_events.SSEFrame(event="token_usage", data="{}"))
    assert event.usage == event.usage and event.usage.input_tokens is None


# ------------------------------------------------- response 剩余分支

def test_tool_call_index_bool_is_treated_as_missing():
    from src.compat.openai.response import ToolIndexState

    state = ToolIndexState()
    _, index = state.resolve({"id": "x", "index": True})
    assert index == 0


def test_translator_reasoning_after_role_already_sent():
    from src.compat.openai.response import StreamTranslator

    translator = StreamTranslator("m")
    list(translator.translate(Event(kind=EventKind.CONTENT, content="a")))
    frames = list(translator.translate(Event(kind=EventKind.REASONING, content="r")))
    assert b"reasoning_content" in frames[0]


def test_aggregate_tool_call_without_existing_function_key():
    from src.compat.openai.response import aggregate

    result = aggregate([
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[{"id": "a", "index": 0}]),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "a", "index": 0, "function": {"arguments": "{}"}}]),
    ], "m")
    args = result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert args == "{}"


# ------------------------------------------------- 最后一批边界分支

async def test_complete_inline_error_frame_rotates(tmp_path):
    """聚合路径遇到流内错误事件 → 冷却 + 换号（executor 127-130）。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    provider = _Provider([[Event(kind=EventKind.ERROR, error_code=500)], GOOD])
    executor = _executor(credentials, provider)
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"
    assert provider.calls == 2
    db.close()


async def test_complete_exhausts_rotation_without_last_error(tmp_path):
    """换号次数用尽且无 last_error 时的消息分支（executor 135）。

    路径：唯一凭证被硬禁用 → 选不到号 → 无 last_error 的 503 消息。
    """
    credentials, db = _repo(tmp_path)
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.save_error(credential_id, _outcome_disabled())
    executor = _executor(credentials, _Provider([GOOD]))
    with pytest.raises(NoHealthyCredential) as caught:
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert str(caught.value) == "all credentials unavailable"
    db.close()


def _outcome_disabled():
    from src.engine.scheduler import Scheduler
    from src.provider.base import ErrKind

    return Scheduler().note_error(
        __import__("src.engine.scheduler", fromlist=["Candidate"]).Candidate(
            credential_id="c", provider="trae"), ErrKind.DEAD, 0)


def test_pick_returns_none_when_all_cooling(tmp_path):
    """_pick 在候选全部冷却时返回 None（executor 154-155）。"""
    import time

    from src.engine.model_resolver import resolve
    from src.engine.scheduler import Scheduler
    from src.provider.base import ErrKind

    credentials, db = _repo(tmp_path)
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    candidate = credentials.candidates()[0]
    credentials.save_error(
        credential_id, Scheduler().note_error(candidate, ErrKind.PLAN, int(time.time())))
    executor = _executor(credentials, _Provider([GOOD]))
    assert executor._pick(resolve("glm-5.2", "glm-5.2"), set()) is None
    db.close()


def test_event_kind_helper_covers_1005_and_other():
    from src.engine.executor import _event_kind

    assert _event_kind(Event(kind=EventKind.ERROR, error_code=1005)) is ErrKind.PLAN
    assert _event_kind(Event(kind=EventKind.ERROR, error_code=500)) is ErrKind.OTHER


async def test_iter_frames_final_buffer_without_newline_is_flushed():
    """末行无换行符 → feed_line(buffer) 分支（sse 87）。"""
    from src.engine.sse import iter_frames

    async def gen():
        yield b"event: x"
        yield b"\ndata: 1"

    frames = [f async for f in iter_frames(gen())]
    assert frames == [__import__("src.engine.sse", fromlist=["SSEFrame"]).SSEFrame(
        event="x", data="1")]


def test_callback_json_param_exhausts_decode_attempts():
    """两次解码仍失败 → 返回空（callback 67-71）。"""
    from src.provider.trae.callback import _parse_json_param

    assert _parse_json_param("%25ZZ") == {}
    assert _parse_json_param("%5B1%5D") == {}          # 解析成功但不是对象


def test_callback_user_info_array_is_ignored():
    from src.provider.trae.callback import parse_callback_url

    info = parse_callback_url("http://x/authorize?refreshToken=R&userInfo=%5B1%2C2%5D")
    assert info.refresh_token == "R" and info.uid == ""


def test_parse_all_events_with_empty_tool_calls_list():
    """tool_calls 为空数组时不产出事件（events 120->122）。"""
    from src.provider.trae import events as trae_events

    frame = trae_events.SSEFrame(event="output",
                                 data='{"response":"x","tool_calls":[]}')
    assert [e.kind for e in trae_events.parse_all_events(frame)] == [EventKind.CONTENT]


def test_aggregate_tool_call_fragment_when_function_missing():
    """合并分片时 function 缺失 → setdefault 分支（response 155）。"""
    from src.compat.openai.response import aggregate

    result = aggregate([
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[{"id": "a", "index": 0}]),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "a", "index": 0, "function": {"arguments": "1"}}]),
    ], "m")
    message = result["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["arguments"] == "1"


def test_aggregate_reasoning_after_tool_calls():
    """tool_calls 之后再有 reasoning（response 146 分支顺序）。"""
    from src.compat.openai.response import aggregate

    result = aggregate([
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[{"id": "a", "index": 0}]),
        Event(kind=EventKind.REASONING, content="r"),
        Event(kind=EventKind.CONTENT, content="c"),
    ], "m")
    message = result["choices"][0]["message"]
    assert message["content"] == "c" and message["reasoning_content"] == "r"


def test_tool_index_bool_index_falls_back_to_new_slot():
    """index 为 bool 视为缺失（response 52->54）。"""
    from src.compat.openai.response import ToolIndexState

    state = ToolIndexState()
    state.resolve({"id": "a", "index": 0})
    _, index = state.resolve({"id": "b", "index": False})
    assert index == 1


def test_prepare_body_returns_without_tools_key():
    """tools 为 None → pop 分支（client 109->exit）。"""
    body = prepare_body({"messages": [], "tools": None, "tool_choice": "auto"}, "m")
    assert "tools" not in body


def test_close_when_only_one_client_created():
    """只创建了 stream 客户端时 aclose 跳过另一个（client 160->159）。"""
    import asyncio

    from src.provider.trae.client import TraeClient

    client = TraeClient()
    client._stream()                     # 只实例化 stream 客户端
    asyncio.run(client.aclose())


def test_build_login_url_missing_keys_still_builds():
    from src.provider.trae.callback import build_login_url

    assert "login_trace_id=" in build_login_url("http://x/authorize",
                                                machine_id="", device_id="")


async def test_complete_rotation_exhausted_after_classified_error(tmp_path):
    """max_rotate=1：一次失败后轮换额度即用尽 → 末尾的 503 抛出分支。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    executor = _executor(credentials, _Provider([[SoftError()]]), max_rotate=1)
    with pytest.raises(NoHealthyCredential) as caught:
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert "upstream" in str(caught.value)
    db.close()


# ------------------------------------------- 短路组合的最后分支

def test_parse_all_events_tool_calls_non_list_and_empty():
    """tool_calls 非 list / 空 list 时短路分支（events 120）。"""
    from src.provider.trae import events as trae_events

    non_list = trae_events.SSEFrame(event="output",
                                    data='{"response":"x","tool_calls":"bad"}')
    empty = trae_events.SSEFrame(event="output", data='{"tool_calls":[]}')
    assert [e.kind for e in trae_events.parse_all_events(non_list)] == [EventKind.CONTENT]
    assert trae_events.parse_all_events(empty) == []


def test_parse_all_events_reasoning_only_and_content_empty():
    """content 为空串、reasoning 命中（events 120→122 短路）。"""
    from src.provider.trae import events as trae_events

    frame = trae_events.SSEFrame(event="output",
                                 data='{"response":"","reasoning_content":"r"}')
    assert [e.kind for e in trae_events.parse_all_events(frame)] == [EventKind.REASONING]


def test_tool_index_non_int_index_falls_back():
    """index 为字符串 → 走 id 回退（response 52→54）。"""
    from src.compat.openai.response import ToolIndexState

    state = ToolIndexState()
    _, index = state.resolve({"id": "a", "index": "0"})
    assert index == 0


def test_aggregate_reasoning_before_content_branch_order():
    """聚合里 reasoning 先于 content 出现，覆盖 elif 顺序（response 146→135）。"""
    from src.compat.openai.response import aggregate

    message = aggregate([Event(kind=EventKind.REASONING, content="r"),
                         Event(kind=EventKind.CONTENT, content="c")], "m")["choices"][0]["message"]
    assert message["reasoning_content"] == "r" and message["content"] == "c"


def test_aggregate_tool_call_fragment_non_string_is_ignored():
    """arguments 非字符串 → 不合并（response 155→147）。"""
    from src.compat.openai.response import aggregate

    result = aggregate([
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "a", "index": 0, "function": {"name": "f"}}]),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "a", "index": 0, "function": {"arguments": 5}}]),
    ], "m")
    fn = result["choices"][0]["message"]["tool_calls"][0]["function"]
    assert fn["name"] == "f" and "arguments" not in fn


def test_provider_probe_quota_uses_client(tmp_path):
    """TraeProvider.probe_quota 走真实客户端（client 306）。"""
    import asyncio

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user_entitlement_pack_list": [
            {"entitlement_base_info": {"quota": {"credits_limit": 8}},
             "usage": {"credits_amount": 3}}]})

    from src.provider.trae.client import TraeProvider

    provider = TraeProvider(client=_mock_client(handler))
    quota = asyncio.run(provider.probe_quota({"accessToken": "a"}))
    assert quota.total == 8 and quota.remaining == 5


def test_prepare_body_tools_present_keeps_key():
    """tools 存在时不 pop（client 109→exit 的另一侧）。"""
    body = prepare_body({"messages": [], "tools": [{"type": "function"}]}, "m")
    assert body["tools"] == [{"type": "function"}]


async def test_stream_rotation_continues_while_budget_remains(tmp_path):
    """流式路径：第一次失败但仍有轮换额度 → 继续循环（executor 92→94）。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    executor = _executor(credentials, _Provider([[SoftError()], GOOD]), max_rotate=5)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"hi" in chunks[0]
    db.close()



async def test_executor_skips_concurrently_deleted_credential(tmp_path):
    """凭证在选择后、读取前被删除 → 跳过它继续选下一个（executor 154-155）。

    用计数器强制第一次读取返回 None，不依赖调度器的挑选顺序。
    """
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    original = credentials.credential_data
    calls = {"n": 0}

    def flaky(credential_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None                       # 模拟并发删除
        return original(credential_id)

    credentials.credential_data = flaky        # type: ignore[method-assign]
    executor = _executor(credentials, _Provider([GOOD]))
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"
    assert calls["n"] >= 2
    db.close()


async def test_stream_rotation_budget_exhausted_with_last_error(tmp_path):
    """流式路径：max_rotate=1 且已记录 last_error → 90-94 的 503 分支。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    executor = _executor(credentials, _Provider([[SoftError()]]), max_rotate=1)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"upstream soft rate limit" in chunks[-1]
    db.close()


def test_tool_index_int_index_without_id():
    """index 是 int 但没有 id → 不登记映射（response 52→54）。"""
    from src.compat.openai.response import ToolIndexState

    _, index = ToolIndexState().resolve({"index": 3})
    assert index == 3


def test_aggregate_tool_calls_event_with_none_payload():
    """kind 为 TOOL_CALLS 但 tool_calls 为空 → 跳过（response 146→135）。"""
    from src.compat.openai.response import aggregate

    result = aggregate([Event(kind=EventKind.TOOL_CALLS, tool_calls=None),
                        Event(kind=EventKind.CONTENT, content="c")], "m")
    assert result["choices"][0]["message"]["content"] == "c"
    assert "tool_calls" not in result["choices"][0]["message"]


def test_solo_headers_include_uid_when_present():
    """有 uid 时必须带 X-Uid（SOLOHeaders 分支，client 145-146）。"""
    from src.provider.trae.client import solo_headers

    headers = solo_headers(TraeCredential(access_token="a", uid="u-9"))
    assert headers["X-Uid"] == "u-9"
    no_uid = solo_headers(TraeCredential(access_token="a"))
    assert "X-Uid" not in no_uid


# ------------------------------------------- TRAE 签到（三态 + 真实请求）

def _trae_client(handler) -> TraeClient:

    transport = httpx.MockTransport(handler)
    return TraeClient(stream_client=httpx.AsyncClient(transport=transport, timeout=None),
                      short_client=httpx.AsyncClient(transport=transport, timeout=None))


async def test_trae_checkin_status_and_claim_use_ug_headers():
    """签到两端点必须走 ug_headers（含 X-User-Region），且用 POST。"""
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.headers)))
        if request.url.path.endswith("checkin_credits/status"):
            return httpx.Response(200, json={"checked_in": False, "credits": 0,
                                             "enable": True})
        return httpx.Response(200, json={"credits": 200})

    client = _trae_client(handler)
    status = await client.fetch_checkin_status(TraeCredential(access_token="a", device_id="d"))
    assert status == {"checked_in": False, "credits": 0, "enable": True}

    claim = await client.claim_checkin(TraeCredential(access_token="a", device_id="d"))
    assert claim == {"credits": 200}

    assert len(seen) == 2
    for path, headers in seen:
        assert "checkin_credits" in path
        # httpx 内部存储全小写
        assert headers.get("x-user-region") == "CN"
        assert headers.get("authorization") == "Cloud-IDE-JWT a"


async def test_trae_provider_checkin_already_is_success():
    """已签到（status.checked_in=true）→ ok=true + already_checked_in。"""
    from src.provider.trae.client import TraeProvider

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"checked_in": True, "credits": 0, "enable": True})

    provider = TraeProvider(client=_trae_client(handler))
    result = await provider.checkin({"accessToken": "a"})
    assert result.ok is True and result.already_checked_in is True
    assert result.message == "今天已签到"


async def test_trae_provider_checkin_disabled_reports_not_ok():
    """status.enable=false → 不可签到。"""
    from src.provider.trae.client import TraeProvider

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"checked_in": False, "credits": 0, "enable": False})

    provider = TraeProvider(client=_trae_client(handler))
    result = await provider.checkin({"accessToken": "a"})
    assert result.ok is False


async def test_trae_provider_checkin_claims_when_eligible():
    """未签且可签 → 调 claim 并成功。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("checkin_credits/status"):
            return httpx.Response(200, json={"checked_in": False, "credits": 0,
                                             "enable": True})
        return httpx.Response(200, json={"credits": 500})

    from src.provider.trae.client import TraeProvider

    provider = TraeProvider(client=_trae_client(handler))
    result = await provider.checkin({"accessToken": "a"})
    assert result.ok is True and result.already_checked_in is False
    assert any(p.endswith("checkin_credits/claim") for p in paths)


def test_trae_checkin_scope_uses_uid():
    from src.provider.trae.client import TraeProvider

    provider = TraeProvider()
    assert provider.checkin_scope({"uid": "u1"}) == "trae|u1"
    assert provider.checkin_scope({}) == "trae|"


async def test_checkin_task_skips_provider_without_scope(tmp_path):
    """provider 没有 checkin_scope 时跳过（background 148-150）。"""
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository

    db = Database(tmp_path / "ns.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})

    from src.tasks.checkin import CheckinTask

    class NoScope:
        id = "codebuddy"

    task = CheckinTask(credentials, {"codebuddy": NoScope()})
    report = await task.run_once()
    assert report.skipped == 1
    db.close()


async def test_trae_dynamic_models_with_valid_credential():
    """有 access_token → 动态拉取；失败回退静态（trae client 375-381）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("get_detail_param"):
            return httpx.Response(200, json={"config_info_list": [
                {"config_name": "DeepSeek-V4-Flash"},
                {"config_name": "glm-5.2"},
            ]})
        return httpx.Response(200, json={})

    from src.provider.trae.client import STATIC_MODELS, TraeProvider

    provider = TraeProvider(client=_trae_client(handler))
    models = await provider.list_models({"accessToken": "a"})
    # 静态表为骨架（实测大小写），动态独有模型附后；重名不重复出现
    assert [m.id for m in models] == list(STATIC_MODELS)


async def test_trae_list_models_merges_metadata_and_dedupes():
    """重名条目：id 用静态表实测大小写，元数据继承动态结果；
    动态独有模型附后；负缓存期间静态表用上次动态元数据填充。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"config_info_list": [
                {"config_name": "glm-5.2",
                 "display_contact_config": json.dumps({
                     "consumption_rate": {"enable": True, "data": {"rate": 0.78}}}),
                "context_window_tokens": {"dev": 128000}},
                {"config_name": "New-Dynamic-Model"},
            ]})
        return httpx.Response(500)

    from src.provider.trae.client import STATIC_MODELS, TraeProvider

    provider = TraeProvider(client=_trae_client(handler))
    first = {m.id: m for m in await provider.list_models(
        {"accessToken": "a", "uid": "u"})}
    # 重名：id 用静态表实测大小写，倍率继承动态
    assert first["glm-5.2"].credit_rate == 0.78
    assert first["glm-5.2"].max_input_tokens == 128000
    assert "New-Dynamic-Model" in first          # 动态独有附后
    # 第二次拉取失败（负缓存）：只剩静态表骨架，但重名模型的倍率不丢
    second_list = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert len(second_list) == len(STATIC_MODELS)
    second = {m.id: m for m in second_list}
    assert second["glm-5.2"].credit_rate == 0.78
    assert second["glm-5.2"].max_input_tokens == 128000

    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"down")

    provider2 = TraeProvider(client=_trae_client(failing))
    fallback = await provider2.list_models({"accessToken": "a"})
    assert [m.id for m in fallback] == list(
        __import__("src.provider.trae.client", fromlist=["STATIC_MODELS"]).STATIC_MODELS)


def test_lifespan_warmup_failure_is_logged_not_raised(tmp_path, caplog):
    """启动预热模型列表失败 → 仅记日志，服务正常起（main 74-75）。"""
    import logging

    from fastapi.testclient import TestClient

    from src.config import Settings
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Exploding:
        id = "boom"

        async def list_models(self, _data):
            raise RuntimeError("upstream down")

        def import_credential(self, raw):  # pragma: no cover
            return raw

    app = build_app(settings, providers={"boom": Exploding()})
    # candidates 在单上游 try 之外，让它抛错以触发 lifespan 兜底 except
    app.state.credentials.candidates = (
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db gone")))
    with caplog.at_level(logging.WARNING), TestClient(app):
        pass
    assert any("预热模型列表失败" in r.getMessage() for r in caplog.records)


def test_trae_unknown_event_with_non_object_data_is_skipped():
    """progress_notice 等未知事件 data 非 JSON 对象 → 跳过而非致命（回归）。"""
    from src.engine.sse import SSEFrame
    from src.provider.trae.events import UpstreamProtocolViolation, parse_frame

    assert parse_frame(SSEFrame(event="progress_notice",
                                data='[1,2,"x"]')) is None
    assert parse_frame(SSEFrame(event="progress_notice", data='"plain"')) is None
    assert parse_frame(SSEFrame(event="timing_cost", data="123")) is None
    # 有语义的事件 data 非对象仍然是协议违规
    with pytest.raises(UpstreamProtocolViolation):
        parse_frame(SSEFrame(event="output", data='["no"]'))
    with pytest.raises(UpstreamProtocolViolation):
        parse_frame(SSEFrame(event="done", data='["no"]'))


def test_similar_models_suggests_available_alternatives():
    """400 建议列表：相近模型优先，前缀兜底，排除自身。"""
    from src.main import _similar_models

    aliases = {"trae": {"qwen-3.7-plus": "qwen-3.7-plus", "glm-5.2": "GLM-5.2"},
               "codebuddy": {"glm-5.2": "glm-5.2", "kimi-k2.6": "kimi-k2.6"}}
    # 前缀兜底：不存在 qwen3.8-max，但存在 qwen 开头的
    assert _similar_models("qwen3.8-max", aliases) == ["qwen-3.7-plus"]
    # 完全不相关 → 空列表
    assert _similar_models("xyzzy", aliases) == []
    # 自身被排除
    assert _similar_models("GLM-5.2", aliases) == []


async def test_suggestion_callback_failure_does_not_break_400():
    """建议回调抛错 → 主错误照常（executor 74-75）。"""
    from src.compat.openai.request import InvalidRequest
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler
    from src.provider.codebuddy.client import CodeBuddyProvider
    from tests.test_m1b_codebuddy import _client, _request

    db = Database(str(_tmpdir() / "s.sqlite3"))
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})

    def handler(_request):
        return httpx.Response(400, json={"code": 1002, "msg": "no"})

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": CodeBuddyProvider(client=_client(handler))},
        credentials=repo, scheduler=Scheduler(), default_model="qwen3.8",
        model_suggestions=lambda _name: (_ for _ in ()).throw(RuntimeError("boom"))))

    with pytest.raises(InvalidRequest):
        await executor.complete(_request("qwen3.8"), username="u")
    db.close()


def _tmpdir():
    import tempfile

    return pathlib.Path(tempfile.mkdtemp())


def test_trae_unknown_named_error_falls_through_to_none():
    """已知集合穷尽后 return None（events 79）此前不可达，现在保持覆盖。"""
    from src.engine.sse import SSEFrame
    from src.provider.trae.events import parse_frame

    # data 为合法 JSON 对象但事件名未知 → None
    assert parse_frame(SSEFrame(event="extra_info", data='{"x":1}')) is None
    # error 事件带非 int code → ERROR 事件无 code
    event = parse_frame(SSEFrame(event="error", data='{"code":"oops","message":"m"}'))
    assert event is not None and event.kind.value == "error"


async def test_stream_finish_frames_yielded_when_upstream_never_sent_done():
    """上游没发 done 就正常结束 → finish() 补帧（executor 147-148）。"""
    from src.provider.base import Event as E
    from src.provider.base import EventKind as EK
    from tests.test_m1b_codebuddy import _RejectProvider, _request

    class _NoDoneProvider(_RejectProvider):
        """正常输出 content 但不发 FINISH。"""

        async def stream_chat(self, _credential_data, _payload, _model):
            self.calls += 1
            yield E(kind=EK.CONTENT, content="ok")

    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler

    db = Database(str(_tmpdir() / "nd.sqlite3"))
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))
    repo.add(provider="trae", credential_data={"accessToken": "t"})
    executor = Executor(ExecutorDeps(
        providers={"trae": _NoDoneProvider("trae", [E(kind=EK.CONTENT, content="ok")])},
        credentials=repo, scheduler=Scheduler(), default_model="glm-5.2"))

    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    assert any(b"[DONE]" in f for f in frames)     # finish() 补的 DONE 已发出
    db.close()


def test_stream_error_4001_is_invalid_not_other():
    """TRAE 流内 4001 → INVALID（跳过上游不冷却凭证）。"""
    from src.engine.executor import _event_kind
    from src.provider.base import ErrKind, Event, EventKind

    assert _event_kind(Event(kind=EventKind.ERROR, error_code=4001)) is ErrKind.INVALID
    assert _event_kind(Event(kind=EventKind.ERROR, error_code=1005)) is ErrKind.PLAN
    assert _event_kind(Event(kind=EventKind.ERROR, error_code=500)) is ErrKind.OTHER
