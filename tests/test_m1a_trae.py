"""M1a 测试：SSE 解析、TRAE 事件映射、payload 改写、OpenAI 适配、执行引擎、API。

契约测试用真实 SSE fixture（从 trae2api-web 的 sse_test.go 提取）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.api.admin_auth import resolve_public_callback_url
from src.compat.openai.errors import UpstreamStreamError, error_payload
from src.compat.openai.request import InvalidRequest, parse_chat_request
from src.compat.openai.response import (
    StreamTranslator,
    aggregate,
)
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import ApiKeyRepository, CredentialRepository
from src.engine.executor import (
    Executor,
    ExecutorDeps,
    NoHealthyCredential,
    NoProviderForModel,
)
from src.engine.model_resolver import UnknownModelError, resolve
from src.engine.scheduler import Scheduler
from src.engine.sse import SSE_DONE, format_openai_frame, iter_frames, parse_frames
from src.main import build_app
from src.provider.base import ErrKind, Event, EventKind, Model, Usage
from src.provider.trae import events as trae_events
from src.provider.trae.callback import (
    build_login_url,
    credential_from_callback,
    new_machine_identity,
    parse_callback_url,
)
from src.provider.trae.client import (
    STATIC_MODELS,
    TraeClient,
    TraeCredential,
    TraeProvider,
    UpstreamHTTPError,
    parse_credential,
    prepare_body,
)
from src.provider.trae.events import UpstreamProtocolViolation
from tests.conftest import SECRET

FIXTURES = Path(__file__).parent.parent / "src" / "provider" / "fixtures" / "trae"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- SSE 解析

def test_parse_frames_from_real_fixture():
    frames = parse_frames(fixture("chat-basic.sse"))
    assert [f.event for f in frames] == [
        "metadata", "timing_cost", "output", "output", "extra_info",
        "token_usage", "done",
    ]
    assert json.loads(frames[2].data)["response"] == "中国"


def test_parse_frames_handles_comments_blank_and_multiline():
    text = ": keepalive\nevent: x\ndata: a\ndata: b\n\n"
    frames = parse_frames(text)
    assert len(frames) == 1 and frames[0].data == "a\nb"


def test_parse_frames_without_trailing_blank_line():
    assert parse_frames("event: x\ndata: 1") == [trae_events.SSEFrame(event="x", data="1")]


def test_parse_frames_no_space_after_colon():
    frames = parse_frames("event:x\ndata:1\n\n")
    assert frames[0].event == "x" and frames[0].data == "1"


def test_parse_frames_ignores_data_less_events():
    assert parse_frames("event: ping\n\n") == []


async def test_iter_frames_streaming_matches_sync_parse():
    raw = fixture("chat-basic.sse").encode()
    chunks = [raw[i:i + 7] for i in range(0, len(raw), 7)]

    async def gen():
        for chunk in chunks:
            yield chunk

    streamed = [f async for f in iter_frames(gen())]
    assert streamed == parse_frames(fixture("chat-basic.sse"))


def test_format_openai_frame_and_done():
    assert format_openai_frame('{"a":1}') == b'data: {"a":1}\n\n'
    assert SSE_DONE == b"data: [DONE]\n\n"


# ------------------------------------------------------- TRAE 事件映射

def test_real_fixture_maps_to_neutral_events():
    events = [e for e in (trae_events.parse_frame(f)
                          for f in parse_frames(fixture("chat-basic.sse"))) if e]
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.CONTENT, EventKind.CONTENT, EventKind.USAGE, EventKind.FINISH]
    assert events[0].content == "中国"
    assert events[0].kind is EventKind.CONTENT          # parse_frame 单事件：content 优先
    assert events[2].usage == Usage(21, 142, 135)
    assert events[3].finish_reason == "stop"


def test_reasoning_only_output_maps_to_reasoning_event():
    frame = trae_events.SSEFrame(event="output",
                                 data='{"response":"","reasoning_content":"想想","tool_calls":null}')
    event = trae_events.parse_frame(frame)
    assert event.kind is EventKind.REASONING and event.content == "想想"


def test_tool_calls_fixture_maps_to_tool_call_event():
    events = [e for e in (trae_events.parse_frame(f)
                          for f in parse_frames(fixture("tool-calls.sse"))) if e]
    assert events[0].kind is EventKind.TOOL_CALLS
    assert events[0].tool_calls[0]["function"]["name"] == "get_weather"
    assert events[1].finish_reason == "tool_calls"


def test_empty_output_frame_yields_nothing():
    frame = trae_events.SSEFrame(event="output",
                                 data='{"response":"","reasoning_content":"","tool_calls":null}')
    assert trae_events.parse_frame(frame) is None


def test_metadata_and_heartbeat_frames_yield_nothing():
    for frame in parse_frames(fixture("chat-basic.sse")):
        if frame.event in ("metadata", "timing_cost", "extra_info"):
            assert trae_events.parse_frame(frame) is None


def test_frames_without_data_yield_nothing():
    assert trae_events.parse_frame(trae_events.SSEFrame(event="output", data="")) is None


@pytest.mark.parametrize("frame", [
    trae_events.SSEFrame(event="output", data="{broken"),
    trae_events.SSEFrame(event="output", data="[1,2]"),
    trae_events.SSEFrame(event="token_usage", data='"text"'),
])
def test_malformed_frames_raise_not_silently(frame):
    with pytest.raises(trae_events.UpstreamProtocolViolation):
        trae_events.parse_frame(frame)


def test_error_fixtures_classify_plan_and_other():
    plan = trae_events.parse_frame(parse_frames(fixture("error-1005.sse"))[0])
    other = trae_events.parse_frame(parse_frames(fixture("error-param.sse"))[0])
    assert plan.error_code == 1005 and trae_events.classify_error_code(1005) is ErrKind.PLAN
    # 4001 = 参数/模型不可用：流内与 HTTP 两条路径都必须判 INVALID（跳过该上游）
    assert other.error_code == 4001 and trae_events.classify_error_code(4001) is ErrKind.INVALID
    assert trae_events.classify_error_code(None) is ErrKind.OTHER


@pytest.mark.parametrize(("status", "expected"), [
    (401, ErrKind.DEAD), (404, ErrKind.SOFT), (429, ErrKind.SOFT),
    (500, ErrKind.OTHER), (400, ErrKind.INVALID), (402, ErrKind.CREDIT),
])
def test_classify_status(status, expected):
    assert trae_events.classify_status(status) is expected


def test_classify_status_detects_1005_in_body():
    assert trae_events.classify_status(400, b'{"code": 1005}') is ErrKind.PLAN


@pytest.mark.parametrize(("status", "body", "expected"), [
    (402, b"", ErrKind.CREDIT),
    (400, b'{"code": 14018}', ErrKind.CREDIT),
    (400, b'{"code": 11102}', ErrKind.BLOCKED),
    (404, b'{"code": 11102}', ErrKind.BLOCKED),
    (429, b'{"code": 6004}', ErrKind.MODEL),
    (429, b'{"code": 500}', ErrKind.SOFT),
    (500, b'{"code": 14018}', ErrKind.CREDIT),      # 业务码优先于状态码
])
def test_classify_status_business_codes(status, body, expected):
    assert trae_events.classify_status(status, body) is expected


@pytest.mark.parametrize(("code", "expected"), [
    (1005, ErrKind.PLAN), (14018, ErrKind.CREDIT), (6004, ErrKind.MODEL),
    (4001, ErrKind.INVALID), (None, ErrKind.OTHER),
])
def test_classify_error_code_mapping(code, expected):
    assert trae_events.classify_error_code(code) is expected


# ---------------------------------------------------------- 凭证与 payload

def test_parse_credential_nested_and_flat():
    nested = {"auth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 10,
                       "machineId": "m", "deviceId": "d"},
              "account": {"uid": "u1", "nickname": "nick"}}
    flat = {"accessToken": "a", "uid": "u1"}
    assert parse_credential(nested).access_token == "a"
    assert parse_credential(nested).nickname == "nick"
    assert parse_credential(flat).uid == "u1"


@pytest.mark.parametrize("raw", [
    b"{broken", b'{"auth":[]}', b'{"auth":"x","account":{}}',
    b'{"auth":{"accessToken":""},"account":{}}', b"[1,2]",
])
def test_parse_credential_rejects_malformed(raw):
    with pytest.raises(trae_events.UpstreamProtocolViolation):
        parse_credential(raw)


def test_credential_needs_refresh():
    assert TraeCredential(access_token="a").needs_refresh(3600, now=0)
    cred = TraeCredential(access_token="a", expires_at=10_000)
    assert cred.needs_refresh(3600, now=7_000)
    assert not cred.needs_refresh(3600, now=1_000)


def test_credential_dict_roundtrip():
    original = TraeCredential(uid="u", access_token="a", refresh_token="r", expires_at=5,
                              machine_id="m", device_id="d", nickname="n")
    assert TraeCredential.from_dict(original.to_dict()) == original


def test_prepare_body_rewrites_openai_shape():
    body = prepare_body({"model": "glm-5.2",
                         "messages": [{"role": "user", "content": "hi"}]}, "glm-5.2")
    assert body["stream"] is True and body["function"] == "solo_work_lite"
    assert body["config_name"] == body["model"] == "glm-5.2"
    assert body["messages"][0]["content"] == [{"type": "text", "text": "hi"}]


def test_prepare_body_keeps_array_content_and_skips_non_dict():
    body = prepare_body({"messages": [
        {"role": "user", "content": [{"type": "text", "text": "x"}]},
        "raw",
    ]}, "m")
    assert body["messages"][0]["content"] == [{"type": "text", "text": "x"}]
    assert body["messages"][1] == "raw"


def test_prepare_body_normalizes_tool_choice_none():
    body = prepare_body({"messages": [], "tool_choice": "none", "tools": [{}],
                         "functions": [{}]}, "m")
    assert "tool_choice" not in body and "tools" not in body and "functions" not in body


def test_prepare_body_derives_function_name():
    body = prepare_body({"messages": [], "tool_choice": {"type": "function",
                                                         "function": {"name": "get_w"}}}, "m")
    assert body["tool_choice"] == "get_w"


def test_prepare_body_drops_unsupported_tool_choice_shape():
    body = prepare_body({"messages": [], "tool_choice": {"type": "auto"}}, "m")
    assert "tool_choice" not in body


def test_prepare_body_drops_empty_tool_calls_and_content():
    body = prepare_body({"messages": [{"role": "assistant", "content": None,
                                       "tool_calls": []}]}, "m")
    assert body["messages"] == []   # 唯一 assistant 被丢弃


def test_prepare_body_skips_non_dict_tool_call_entries():
    """非对象 tool_call 条目跳过（102-103 分支）。"""
    body = prepare_body({"messages": [{"role": "assistant", "content": None,
        "tool_calls": ["junk", {"id": "c", "type": "function",
                                "function": {"name": "bash", "arguments": "{}"}}]}]}, "m")
    tcs = body["messages"][0]["tool_calls"]
    assert len(tcs) == 1 and tcs[0]["function_call"]["name"] == "bash"
    assert body["messages"][0]["content"] is None   # 原实现保留 nil content


def test_prepare_body_keeps_valid_tool_calls():
    """有 name 的 function → function_call；无 function/name 的剔除。"""
    body = prepare_body({"messages": [{"role": "assistant", "content": "x",
        "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "bash", "arguments": "{\"a\":1}"}},
            {"id": "c2", "type": "function", "function": {"arguments": "{}"}},
            {"id": "c3"},
        ]}]}, "m")
    tcs = body["messages"][0]["tool_calls"]
    assert len(tcs) == 1 and tcs[0]["id"] == "c1"
    assert tcs[0]["function_call"] == {"name": "bash", "arguments": "{\"a\":1}"}
    assert "function" not in tcs[0]


# ------------------------------------------------------------ 模型解析

def test_resolve_flat_auto_and_forced():
    assert resolve("glm-5.2", "d").providers == ("codebuddy", "trae")
    assert resolve("", "glm-5.2").model == "glm-5.2"
    assert resolve("auto", "glm-5.2").model == "glm-5.2"
    assert resolve(None, "glm-5.2").model == "glm-5.2"
    forced = resolve("glm-5.2@trae", "d")
    assert forced.providers == ("trae",) and forced.forced and forced.model == "glm-5.2"


@pytest.mark.parametrize("model", ["glm-5.2@nope", "@trae", "glm-5.2@"])
def test_resolve_rejects_bad_provider_suffix(model):
    with pytest.raises(UnknownModelError):
        resolve(model, "d")


# ------------------------------------------------------- 请求校验

def test_parse_chat_request_ok():
    request = parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                                  "model": "glm-5.2", "stream": True})
    assert request.stream is True and request.model == "glm-5.2"


@pytest.mark.parametrize("body", [
    "not-a-dict", {}, {"messages": []}, {"messages": ["x"]},
    {"messages": [{"content": "no role"}]}, {"messages": [{"role": "user"}], "model": 5},
    {"messages": [{"role": "user"}], "stream": "yes"},
])
def test_parse_chat_request_rejects_invalid(body):
    with pytest.raises(InvalidRequest):
        parse_chat_request(body)


# ----------------------------------------------- OpenAI 流式/非流式适配

def test_stream_translator_emits_role_then_content_then_done():
    """首块补 role:assistant（与 codebuddy2api 的 OpenAIStreamNormalizer 语义一致）。"""
    t = StreamTranslator("glm-5.2")
    frames = list(t.translate(Event(kind=EventKind.CONTENT, content="你")))
    frames += list(t.translate(Event(kind=EventKind.FINISH, finish_reason="stop")))
    first = json.loads(frames[0].decode()[6:])
    assert first["choices"][0]["delta"] == {"role": "assistant", "content": "你"}
    assert json.loads(frames[1].decode()[6:])["choices"][0]["finish_reason"] == "stop"
    assert frames[2] == SSE_DONE


def test_stream_translator_usage_is_not_a_frame():
    t = StreamTranslator("m")
    assert list(t.translate(Event(kind=EventKind.USAGE, usage=Usage(1, 2)))) == []


def test_stream_translator_reasoning_and_tools():
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.CONTENT, content="x")))
    frames = list(t.translate(Event(kind=EventKind.REASONING, content="think")))
    frames += list(t.translate(Event(kind=EventKind.TOOL_CALLS,
                                     tool_calls=[{"id": "c1", "function": {"name": "f"}}])))
    reasoning = json.loads(frames[0].decode()[6:])
    tools = json.loads(frames[1].decode()[6:])
    assert reasoning["choices"][0]["delta"] == {"reasoning_content": "think"}
    assert tools["choices"][0]["delta"]["tool_calls"][0]["index"] == 0


def test_stream_translator_missing_tool_index_gets_stable_position():
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.CONTENT, content="x")))
    frames = list(t.translate(Event(kind=EventKind.TOOL_CALLS,
                                    tool_calls=[{"id": "a", "function": {}},
                                                {"id": "b", "function": {}}])))
    indices = [tc["index"] for tc in json.loads(frames[0].decode()[6:])
               ["choices"][0]["delta"]["tool_calls"]]
    assert indices == [0, 1]


def test_stream_translator_error_frame():
    t = StreamTranslator("m")
    frames = list(t.translate(Event(kind=EventKind.ERROR, error_code=1005,
                                    error_message="quota")))
    payload = json.loads(frames[0].decode()[6:])
    assert payload["error"]["code"] == 1005
    assert b"[DONE]" not in frames[0]


def test_stream_translator_finish_without_upstream_done():
    t = StreamTranslator("m")
    frames = list(t.finish())
    assert json.loads(frames[0].decode()[6:])["choices"][0]["finish_reason"] == "stop"
    assert frames[1] == SSE_DONE


def test_stream_translator_finish_after_done_emits_nothing():
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.FINISH, finish_reason="stop")))
    assert list(t.finish()) == []             # DONE 已随 _close 发出，不重复


def test_stream_translator_empty_delta_is_skipped():
    t = StreamTranslator("m")
    assert list(t.translate(Event(kind=EventKind.CONTENT, content=None))) == []


def test_aggregate_builds_completion_from_fixture_events():
    events = [e for e in (trae_events.parse_frame(f)
                          for f in parse_frames(fixture("chat-basic.sse"))) if e]
    result = aggregate(events, "glm-5.2")
    choice = result["choices"][0]
    assert choice["message"]["content"] == "中国的首都是北京。"
    assert choice["finish_reason"] == "stop"
    assert result["usage"]["prompt_tokens"] == 21
    assert result["usage"]["total_tokens"] == 163
    assert result["usage"]["completion_tokens_details"]["reasoning_tokens"] == 135


def test_aggregate_merges_tool_call_arguments_and_reasoning():
    events = [
        Event(kind=EventKind.REASONING, content="想"),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "c1", "index": 0,
                           "function": {"name": "f", "arguments": '{"a"'}}]),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "c1", "index": 0, "function": {"arguments": ":1}"}}]),
        Event(kind=EventKind.FINISH, finish_reason="tool_calls"),
    ]
    message = aggregate(events, "m")["choices"][0]["message"]
    assert message["content"] is None
    assert message["reasoning_content"] == "想"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"a":1}'


def test_aggregate_without_usage_reports_nulls():
    result = aggregate([Event(kind=EventKind.CONTENT, content="x")], "m")
    assert result["usage"]["prompt_tokens"] is None
    assert result["usage"]["total_tokens"] is None


def test_aggregate_raises_on_stream_error():
    with pytest.raises(UpstreamStreamError):
        aggregate([Event(kind=EventKind.ERROR, error_code=1005, error_message="quota")], "m")


def test_error_payload_shape():
    payload = error_payload("msg", "code", 400)
    assert payload["error"]["message"] == "msg" and payload["error"]["status"] == 400


# --------------------------------------------------------------- 回调解析

def test_parse_callback_url_with_refresh_token():
    url = ("http://127.0.0.1:8000/authorize?refreshToken=RT&userInfo="
           "%7B%22uid%22%3A%22u1%22%2C%22nickname%22%3A%22nick%22%7D")
    info = parse_callback_url(url)
    assert info.refresh_token == "RT" and info.uid == "u1" and info.nickname == "nick"


def test_parse_callback_url_falls_back_to_user_jwt():
    url = ('http://x/authorize?userJwt=%7B%22Token%22%3A%22T%22%2C%22RefreshToken%22%3A%22R%22%7D')
    assert parse_callback_url(url).refresh_token == "R"


@pytest.mark.parametrize("url", ["", "   ", "http://x/authorize?other=1"])
def test_parse_callback_url_rejects_missing_token(url):
    with pytest.raises(trae_events.UpstreamProtocolViolation):
        parse_callback_url(url)


def test_build_login_url_contains_configurable_callback():
    url = build_login_url("https://gw.example/authorize", machine_id="m" * 32,
                          device_id="d" * 32)
    assert "auth_callback_url=https%3A%2F%2Fgw.example%2Fauthorize" in url
    assert "login_version=1" in url and "machine_id=" in url


def test_resolve_public_callback_url():
    settings = Settings(_env_file=None, APP_SECRET=SECRET, PUBLIC_BASE_URL="https://gw.example/")
    assert resolve_public_callback_url(settings) == "https://gw.example/authorize"


def test_new_machine_identity_is_hex32():
    machine_id, device_id = new_machine_identity()
    assert len(machine_id) == 32 and len(device_id) == 32 and machine_id != device_id


def test_credential_from_callback():
    url = "http://x/authorize?refreshToken=R&userInfo=%7B%22uid%22%3A%22u%22%7D"
    info = parse_callback_url(url)
    credential = credential_from_callback(info, "A", machine_id="m", device_id="d")
    assert credential.access_token == "A" and credential.refresh_token == "R"
    assert credential.uid == "u" and credential.needs_refresh(0, now=0) is False


# ------------------------------------------------------------ TRAE 客户端

def _client(handler, **kw) -> TraeClient:
    transport = httpx.MockTransport(handler)
    return TraeClient(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None), **kw)


async def test_client_stream_chat_yields_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agent/v3/llm_utils_chat"
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    events = [e async for e in _client(handler).stream_chat(
        TraeCredential(access_token="a"), {"messages": []}, "glm-5.2")]
    assert [e.kind for e in events][-1] is EventKind.FINISH


async def test_client_stream_chat_raises_classified_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"code":1005}')

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat(
            TraeCredential(access_token="a"), {"messages": []}, "m")]
    assert caught.value.kind() is ErrKind.PLAN


async def test_client_fetch_models_and_quota():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("get_detail_param"):
            return httpx.Response(200, json={"config_info_list": [
                {"config_name": "glm-5.2", "display_config": {"display_name": "GLM"}}]})
        return httpx.Response(200, json={"user_entitlement_pack_list": [
            {"entitlement_base_info": {"quota": {"credits_limit": 100}},
             "usage": {"credits_amount": 30}}]})

    client = _client(handler)
    models = await client.fetch_models(TraeCredential(access_token="a"))
    quota = await client.fetch_quota(TraeCredential(access_token="a"))
    assert models == [Model(id="glm-5.2", name="GLM")]
    assert quota.remaining == 70 and quota.total == 100


async def test_client_fetch_models_metadata():
    """倍率（display_contact_config.consumption_rate）与上下文窗口透传。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": [
            {"config_name": "Doubao-Seed-Evolving",
             "display_config": {"display_name": "Seed-Evolving"},
             "context_window_tokens": {"dev": 256000},
             "display_contact_config": json.dumps({
                 "consumption_rate": {"enable": True, "data": {"rate": 0.08}}})},
            {"config_name": "glm-5.2",
             "display_config": {"display_name": "GLM"},
             "display_contact_config": "{bad json",
             "context_window_tokens": "oops"},
            {"config_name": "kimi-k3",
             "display_config": {"display_name": "K3"},
             "display_contact_config": json.dumps({
                 "consumption_rate": {"enable": True, "data": {"rate": "oops"}}})},
        ]})

    models = await _client(handler).fetch_models(TraeCredential(access_token="a"))
    assert models[0].credit_rate == 0.08
    assert models[0].max_input_tokens == 256000
    # 坏数据 → 字段留空，不影响条目
    assert models[1].credit_rate is None and models[1].max_input_tokens is None
    # rate 非数值同样留空
    assert models[2].credit_rate is None


@pytest.mark.parametrize("payload", [
    {}, {"config_info_list": "no"}, {"config_info_list": []},
])
async def test_fetch_models_rejects_bad_shapes(payload):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(handler).fetch_models(TraeCredential(access_token="a"))


async def test_fetch_quota_skips_zero_and_bad_packs():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user_entitlement_pack_list": [
            "junk", {"entitlement_base_info": {"quota": {"credits_limit": 0}}},
            {"entitlement_base_info": {"quota": {"credits_limit": 10}}, "usage": {}},
        ]})

    quota = await _client(handler).fetch_quota(TraeCredential(access_token="a"))
    assert quota.total == 10 and quota.remaining == 10
    # 只落入有额度的那个包，且名称缺失时给空串（而不是 None）
    assert quota.packages == [{"name": "", "total": 10.0, "used": 0.0, "end": None}]


async def test_fetch_quota_collects_pack_details():
    """奖励积分列表：包名逐级回落，到期取 end_time/expire_time，
    已用缺失当 0；同时**不能**填 expiry_ladder（TRAE 无周期概念，
    填了会改变选号排序）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user_entitlement_pack_list": [
            # 完整字段：package_extra.package_name 优先
            {"entitlement_base_info": {
                "quota": {"credits_limit": 2000},
                "end_time": 1791708834,
                "product_extra": {"package_extra": {"package_name": "福利积分"}}},
             "display_desc": "老用户福利", "usage": {}},
            # 无 package_name → 回落 group_name；无 end_time → 回落 expire_time
            {"entitlement_base_info": {"quota": {"credits_limit": 500}},
             "group_name": "每月登录积分", "expire_time": 1790783999,
             "usage": {"credits_amount": 76.564}},
            # 无 group_name → 回落 display_desc；无到期 → None
            {"entitlement_base_info": {"quota": {"credits_limit": 150},
                                      "product_extra": {"package_extra": {}}},
             "display_desc": "签到奖励", "usage": {"credits_amount": "bad"}},
        ]})

    quota = await _client(handler).fetch_quota(TraeCredential(access_token="a"))
    assert quota.total == 2650 and quota.remaining == 2650 - 76.564
    assert quota.packages == [
        {"name": "福利积分", "total": 2000.0, "used": 0.0, "end": 1791708834},
        {"name": "每月登录积分", "total": 500.0, "used": 76.564, "end": 1790783999},
        {"name": "签到奖励", "total": 150.0, "used": 0.0, "end": None},
    ]
    # 调度指标保持 None：TRAE 不是按包独立到期，不参与"快过期优先"
    assert quota.expiry_ladder is None


async def test_fetch_quota_rejects_missing_list():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(handler).fetch_quota(TraeCredential(access_token="a"))


async def test_refresh_token_normalizes_millisecond_expiry():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {"Token": "new", "RefreshToken": "r2",
                                                    "TokenExpireAt": 1_786_847_930_141}})

    refreshed = await _client(handler).refresh_token(
        TraeCredential(access_token="a", refresh_token="r1", expires_at=1))
    assert refreshed.access_token == "new" and refreshed.refresh_token == "r2"
    assert refreshed.expires_at == 1_786_847_930


async def test_refresh_token_uses_duration_when_no_timestamp():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {"Token": "new",
                                                    "TokenExpireDuration": 3600}})

    refreshed = await _client(handler).refresh_token(
        TraeCredential(access_token="a", refresh_token="r"))
    assert refreshed.expires_at > 0 and refreshed.refresh_token == "r"


async def test_refresh_token_failure_leaves_original_untouched():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {}})

    original = TraeCredential(access_token="a", refresh_token="r")
    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(handler).refresh_token(original)
    assert original.access_token == "a" and original.refresh_token == "r"


async def test_get_user_info():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {"UserID": "u1", "ScreenName": "n"}})

    assert await _client(handler).get_user_info(TraeCredential(access_token="a")) == ("u1", "n")


async def test_get_user_info_rejects_bad_shape():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(handler).get_user_info(TraeCredential(access_token="a"))


async def test_post_json_surfaces_http_and_non_json_errors():
    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await _client(failing).fetch_quota(TraeCredential(access_token="a"))

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(not_json).fetch_quota(TraeCredential(access_token="a"))

    def not_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(not_object).fetch_quota(TraeCredential(access_token="a"))


async def test_client_lazy_and_close_paths():
    client = TraeClient()
    assert client._stream() is client._stream()
    assert client._short() is client._short()
    await client.aclose()


async def test_provider_import_classify_and_models():
    provider = TraeProvider()
    data = provider.import_credential({"accessToken": "a", "uid": "u"})
    assert data["uid"] == "u"
    assert provider.classify(429, b"") is ErrKind.SOFT
    models = await provider.list_models({})
    assert [m.id for m in models] == list(STATIC_MODELS)
    with pytest.raises(trae_events.UpstreamProtocolViolation):
        provider.import_credential({"accessToken": "a"})


def test_provider_import_rejects_foreign_api_host():
    """apiHost 是用户可控输入：非白名单立即拒绝导入，不落库（PROPOSAL §8）。"""
    provider = TraeProvider()
    with pytest.raises(trae_events.UpstreamProtocolViolation) as error:
        provider.import_credential(
            {"accessToken": "a", "uid": "u", "apiHost": "https://evil.example"})
    assert "not in the TRAE allowed upstream hosts" in str(error.value)
    # 官方白名单地址（含尾斜杠归一）正常放行
    for host in ("https://api.trae.com.cn", "https://api.trae.com.cn/", ""):
        assert provider.import_credential(
            {"accessToken": "a", "uid": "u", "apiHost": host})["uid"] == "u"


async def test_refresh_ignores_foreign_api_host_from_legacy_credential():
    """旧库里带任意 apiHost 的凭证：刷新退回官方地址，绝不把 refreshToken 发出去。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"Result": {"Token": "t2", "RefreshToken": "r2"}})

    provider = TraeProvider(client=_client(handler))
    refreshed = await provider.refresh(
        {"accessToken": "a", "refreshToken": "r", "uid": "u",
         "apiHost": "https://evil.example"})
    assert refreshed["accessToken"] == "t2"
    assert all(url.startswith("https://api.trae.com.cn/") for url in seen)


async def test_user_info_ignores_foreign_api_host():
    """GetUserInfo 走同一个白名单：伪造 apiHost 不会收到 accessToken。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"Result": {"UserID": "u1", "ScreenName": "n"}})

    client = _client(handler)
    provider = TraeProvider(client=client)
    uid, nickname = await provider.client.get_user_info(
        TraeCredential(access_token="a", api_host="https://evil.example"))
    assert (uid, nickname) == ("u1", "n")
    assert all(url.startswith("https://api.trae.com.cn/") for url in seen)


async def test_provider_model_failure_negative_cache():
    """拉取失败后 5 分钟内不再打上游（静态表兜底），缓存过期后恢复拉取。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    provider = TraeProvider(client=_client(handler))
    first = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert calls["n"] == 1
    assert [m.id for m in first] == list(STATIC_MODELS)
    # 负缓存生效：第二次不请求上游，仍回静态表
    second = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert calls["n"] == 1
    assert [m.id for m in second] == list(STATIC_MODELS)
    # 模拟缓存过期：恢复动态拉取
    provider._dynamic_models_blocked_until = 0
    await provider.list_models({"accessToken": "a", "uid": "u"})
    assert calls["n"] == 2


async def test_provider_model_success_clears_negative_cache():
    """动态拉取成功后清除负缓存，后续请求恢复直连上游。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"config_info_list": [
            {"config_name": "glm-5.2",
             "display_config": {"display_name": "GLM"}}]})

    provider = TraeProvider(client=_client(handler))
    await provider.list_models({"accessToken": "a", "uid": "u"})
    assert provider._dynamic_models_blocked_until is not None
    # 缓存过期后重试成功 → 清除负缓存
    provider._dynamic_models_blocked_until = 0
    models = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert any(m.id == "glm-5.2" for m in models)
    assert provider._dynamic_models_blocked_until is None
    # 负缓存已清除：再次调用重新拉取（成功路径）
    before = calls["n"]
    await provider.list_models({"accessToken": "a", "uid": "u"})
    assert calls["n"] == before + 1


async def test_provider_stream_and_refresh_via_client():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = TraeProvider(client=_client(handler))
    events = [e async for e in provider.stream_chat({"accessToken": "a"}, {}, "m")]
    assert events[-1].kind is EventKind.FINISH


# -------------------------------------------------------------- 执行引擎

@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "e.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), ApiKeyRepository(db), db
    db.close()


class FakeProvider:
    id = "trae"

    def __init__(self, script: list) -> None:
        self.script = script
        self.calls = 0

    async def stream_chat(self, credential_data, payload, model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    async def list_models(self, _credential_data):
        return [Model(id="glm-5.2")]

    def import_credential(self, raw):
        if not raw.get("accessToken"):
            raise UpstreamProtocolViolation("credential missing accessToken")
        return dict(raw)

    def classify(self, status, body=b""):
        return ErrKind.OTHER


class UpstreamError(Exception):
    def __init__(self, kind: ErrKind) -> None:
        super().__init__(f"upstream {kind}")
        self._kind = kind

    def kind(self) -> ErrKind:
        return self._kind


def build_executor(repo_tuple, script, **kwargs):
    credentials, _keys, _db = repo_tuple
    provider = FakeProvider(script)
    executor = Executor(ExecutorDeps(providers={"trae": provider}, credentials=credentials,
                                     scheduler=Scheduler(**kwargs), default_model="glm-5.2"))
    return executor, provider, credentials


def add_credential(credentials, **kw):
    return credentials.add(provider="trae", credential_data={"accessToken": "a"}, **kw)


GOOD = [Event(kind=EventKind.CONTENT, content="hi"),
        Event(kind=EventKind.USAGE, usage=Usage(1, 2, 0)),
        Event(kind=EventKind.FINISH, finish_reason="stop")]


async def test_executor_complete_success(repo, tmp_path):
    add_credential(repo[0])
    executor, _provider, _credentials = build_executor(repo, [GOOD])
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"


async def test_executor_stream_success_frames(repo):
    add_credential(repo[0])
    executor, _provider, _credentials = build_executor(repo, [GOOD])
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert chunks[-1] == SSE_DONE
    assert b'"hi"' in chunks[0]                      # 首帧即带 role + content


async def test_executor_rotates_on_http_error(repo):
    add_credential(repo[0], nickname="first")
    add_credential(repo[0], nickname="second")
    script = [[UpstreamError(ErrKind.SOFT)], GOOD]
    executor, provider, _credentials = build_executor(repo, script)
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"
    assert provider.calls == 2


async def test_executor_cools_plan_error_then_fails_when_exhausted(repo):
    add_credential(repo[0])
    executor, _provider, credentials = build_executor(repo, [[UpstreamError(ErrKind.PLAN)]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert credentials.candidates()[0].cooling_until is not None


async def test_executor_stream_reports_no_credential(repo):
    executor, _provider, _credentials = build_executor(repo, [GOOD])
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"no_healthy_credential" in chunks[0]


async def test_executor_stream_inline_error_triggers_rotation(repo):
    add_credential(repo[0])
    add_credential(repo[0])
    script = [[Event(kind=EventKind.ERROR, error_code=1005, error_message="quota")], GOOD]
    executor, provider, _credentials = build_executor(repo, script)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert provider.calls == 2 and chunks[-1] == SSE_DONE


async def test_executor_unknown_error_propagates(repo):
    add_credential(repo[0])
    executor, _provider, _credentials = build_executor(repo, [[RuntimeError("boom")]])
    with pytest.raises(RuntimeError):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))


async def test_executor_dead_error_disables_credential(repo):
    add_credential(repo[0])
    executor, _provider, credentials = build_executor(repo, [[UpstreamError(ErrKind.DEAD)]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert credentials.candidates()[0].disabled is True


async def test_executor_reports_no_credential_as_service_unavailable(repo):
    """provider 已注册但无凭证 → NoHealthyCredential（503），不是 400。"""
    executor, _provider, _credentials = build_executor(repo, [GOOD])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))


async def test_executor_rejects_model_without_provider(repo):
    add_credential(repo[0])
    executor, _provider, _credentials = build_executor(repo, [GOOD])
    with pytest.raises(NoProviderForModel):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "model": "x@codebuddy"}))


# ------------------------------------------------------------------- API

@pytest.fixture()
def client(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": FakeProvider([GOOD])})
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_api_requires_key(client):
    response = client.post("/v1/chat/completions", json={"messages": [{"role": "user"}]})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_api_rejects_bad_key(client):
    response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer nope"},
                           json={"messages": [{"role": "user"}]})
    assert response.status_code == 401


def test_full_flow_apikey_chat_and_models(client, tmp_path):
    app = client.app
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    created = app.state.api_keys.create("root", "test")
    headers = {"Authorization": f"Bearer {created['api_key']}"}
    models = client.get("/v1/models", headers=headers)
    assert models.status_code == 200
    assert models.json()["data"][0]["providers"] == ["trae"]
    chat = client.post("/v1/chat/completions", headers=headers,
                       json={"messages": [{"role": "user", "content": "hi"}]})
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["message"]["content"] == "hi"


def test_api_validation_error(client):
    app = client.app
    key = app.state.api_keys.create("root")["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    response = client.post("/v1/chat/completions", headers=headers, json={"messages": []})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_admin_endpoints_require_session(client):
    assert client.get("/api/credentials").status_code == 401


def _login(client, username="root"):
    from src.auth.session import create_session_token

    token = create_session_token(username, SECRET)
    client.cookies.set("coding2api_session", token)


def test_admin_credential_lifecycle(client):
    _login(client)
    created = client.post("/api/credentials", json={
        "provider": "trae", "credential": {"accessToken": "a", "uid": "u"}, "nickname": "n"})
    assert created.status_code == 200
    credential_id = created.json()["id"]
    assert client.get("/api/credentials").json()["credentials"][0]["id"] == credential_id
    pinned = client.post("/api/credentials/pin", json={"credential_id": credential_id})
    assert pinned.status_code == 200
    assert client.post(f"/api/credentials/{credential_id}/toggle",
                       json={"enabled": False}).status_code == 200
    assert client.delete(f"/api/credentials/{credential_id}").status_code == 200
    assert client.delete(f"/api/credentials/{credential_id}").status_code == 400


def test_admin_import_rejects_unknown_provider(client):
    _login(client)
    response = client.post("/api/credentials", json={"provider": "nope", "credential": {}})
    assert response.status_code == 400


def test_admin_import_rejects_bad_credential(client):
    _login(client)
    response = client.post("/api/credentials", json={"provider": "trae", "credential": {}})
    assert response.status_code == 400


def test_api_key_crud(client):
    _login(client)
    created = client.post("/api/api-keys", json={"name": "k"})
    assert created.status_code == 200 and created.json()["api_key"].startswith("sk-")
    key_id = created.json()["id"]
    assert client.get("/api/api-keys").json()["api_keys"][0]["id"] == key_id
    assert client.delete(f"/api/api-keys/{key_id}").status_code == 200
    assert client.delete(f"/api/api-keys/{key_id}").status_code == 400


def test_non_admin_cannot_write(client):
    _login(client, username="guest")
    assert client.post("/api/credentials", json={"provider": "trae",
                                                 "credential": {}}).status_code == 403


def test_authorize_rejects_callback_without_pending_login(client):
    """没有进行中的登录时，任意回调不得被塞进凭证池。"""
    response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "refreshToken=RT" in client.app.state.last_callback_url


def test_prepare_body_stringifies_tool_parameters():
    """OpenAI tools.parameters(object) → TRAE 要求 JSON 字符串（code=4001 回归）。"""
    from src.provider.trae.client import prepare_body

    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {
                "name": "read", "description": "d",
                "parameters": {"type": "object", "properties": {"p": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "no_params"}},   # 缺 parameters
            "junk",                                                     # 非对象跳过
        ],
        "tool_choice": "auto",
    }
    body = prepare_body(payload, "m")
    tools = body["tools"]
    assert isinstance(tools, list) and len(tools) == 2
    first = tools[0]["function"]["parameters"]
    assert isinstance(first, str) and json.loads(first)["type"] == "object"
    assert tools[1]["function"]["parameters"] == "{}"
    # tool_choice 归一化不受影响
    assert body["tool_choice"] == "auto"


def test_prepare_body_tools_edge_cases():
    """tools 非 list / parameters 为 list / 带 parameters 的字符串透传。"""
    from src.provider.trae.client import prepare_body

    # tools 不是 list → 原样保留（走 _stringify 的早退分支）
    body = prepare_body({"messages": [], "tools": "nope"}, "m")
    assert body["tools"] == "nope"

    # parameters 为 list → 同样字符串化
    body = prepare_body({"messages": [], "tools": [
        {"type": "function", "function": {"name": "f", "parameters": ["a"]}},
    ]}, "m")
    assert body["tools"][0]["function"]["parameters"] == '["a"]'

    # parameters 已是字符串 → 原样保留
    body = prepare_body({"messages": [], "tools": [
        {"type": "function", "function": {"name": "f", "parameters": "{}"}},
    ]}, "m")
    assert body["tools"][0]["function"]["parameters"] == "{}"


async def test_trae_pacer_wait_and_disable():
    """TRAE stream_chat 前等待 pacer；None 不等待（与 CB 对称）。"""
    import time as _time

    from src.provider.trae.client import TraeProvider

    waited = []

    class FakePacer:
        async def wait_turn(self):
            waited.append(_time.monotonic())

    class FakeClient:
        async def stream_chat(self, cred, payload, model):
            yield Event(kind=EventKind.CONTENT, content="ok")

    provider = TraeProvider(client=FakeClient(), pacer=FakePacer())
    events = [e async for e in provider.stream_chat({"accessToken": "a"}, {}, "m")]
    assert events and waited

    provider2 = TraeProvider(client=FakeClient(), pacer=None)
    waited.clear()
    _ = [e async for e in provider2.stream_chat({"accessToken": "a"}, {}, "m")]
    assert not waited


def test_prepare_body_drops_unnamed_function_call():
    """function_call 无 name 剔除后若全空 → 整个 tool_calls 删除（102-103）。"""
    body = prepare_body({"messages": [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c", "type": "function",
                         "function": {"arguments": "{}"}}]},
    ]}, "m")
    assert body["messages"] == []   # 唯一 assistant 被丢弃



def test_prepare_body_normalizes_developer_role():
    """TRAE 不认 developer 角色（静默空流 3003）→ 归一 system。"""
    body = prepare_body({"messages": [
        {"role": "developer", "content": "You are PI."},
        {"role": "user", "content": "hi"},
    ]}, "m")
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]


def test_prepare_body_drops_empty_assistant_with_kept_tool_calls():
    """tool_call 有 name 保留 + assistant 占位删除后悬空 tool 也清（127-129）。"""
    body = prepare_body({"messages": [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "keep", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "keep", "content": "out"},
    ]}, "m")
    assert len(body["messages"]) == 2
    assert body["messages"][0]["tool_calls"][0]["id"] == "keep"
    assert body["messages"][1]["role"] == "tool"


def test_prepare_body_drops_placeholder_assistant_when_all_calls_dropped():
    """全部 tool_call 被剔 + content=None → 占位 assistant 整条丢弃（127-129）。"""
    body = prepare_body({"messages": [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "x", "type": "function",
                         "function": {"arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "x", "content": "out"},
    ]}, "m")
    assert [m["role"] for m in body["messages"]] == []


def test_prepare_body_keeps_assistant_placeholder_when_content_present():
    """占位 assistant 带 content → 保留（127-129 不走 continue 分支）。"""
    body = prepare_body({"messages": [
        {"role": "assistant", "content": "文本",
         "tool_calls": [{"id": "x", "type": "function",
                         "function": {"arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "x", "content": "out"},
    ]}, "m")
    assert body["messages"][0]["content"] == [{"type": "text", "text": "文本"}]
    assert "tool_calls" not in body["messages"][0]
    # 悬空 tool 消息被成对清理
    assert [m["role"] for m in body["messages"][1:]] == []


def test_parse_all_events_non_dict_tool_call_entry_skipped():
    """非对象 tool_call 条目（88）与全空结果（158-160）覆盖。"""
    frame = trae_events.SSEFrame(
        event="output",
        data='{"tool_calls":["junk",{"function_call":{"name":"f","arguments":"{}"}}]}')
    events = trae_events.parse_all_events(frame)
    tools = [e for e in events if e.kind is EventKind.TOOL_CALLS]
    assert len(tools) == 1 and tools[0].tool_calls[0]["function"]["name"] == "f"

    empty = trae_events.SSEFrame(
        event="output",
        data='{"tool_calls":["junk"],"response":"x"}')
    kinds = [e.kind for e in trae_events.parse_all_events(empty)]
    assert EventKind.TOOL_CALLS not in kinds

def test_trae_usage_cached_tokens():
    """TRAE usage：details 路径 + 顶层兜底 + TRAE 官方字段 + 缺省 None。"""
    from src.provider.trae.events import SSEFrame

    frame = SSEFrame(event="token_usage", data=(
        '{"prompt_tokens":9,'
        '"prompt_tokens_details":{"cached_tokens":6}}'))
    assert trae_events.parse_frame(frame).usage.cached_tokens == 6

    frame = SSEFrame(event="token_usage", data='{"prompt_tokens":9,"cached_tokens":2}')
    assert trae_events.parse_frame(frame).usage.cached_tokens == 2

    # TRAE 官方字段：命中>0 与未命中=0 都要保留（0 是有效值不是缺失）
    frame = SSEFrame(event="token_usage", data=(
        '{"prompt_tokens":13,"completion_tokens":159,'
        '"cache_read_input_tokens":7,"reasoning_tokens":148}'))
    assert trae_events.parse_frame(frame).usage.cached_tokens == 7
    frame = SSEFrame(event="token_usage", data=(
        '{"prompt_tokens":13,"cache_read_input_tokens":0}'))
    assert trae_events.parse_frame(frame).usage.cached_tokens == 0

    # OpenAI 惯例字段优先于 TRAE 字段
    frame = SSEFrame(event="token_usage", data=(
        '{"prompt_tokens":9,"cached_tokens":2,"cache_read_input_tokens":5}'))
    assert trae_events.parse_frame(frame).usage.cached_tokens == 2

    frame = SSEFrame(event="token_usage", data='{"prompt_tokens":9}')
    assert trae_events.parse_frame(frame).usage.cached_tokens is None

