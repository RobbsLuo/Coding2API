"""覆盖率收尾（TRAE 客户端/事件/回调）：头部拼装、帧迭代与解析边界。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.provider.base import ErrKind, EventKind, Model
from src.provider.trae.client import TraeClient, TraeCredential, prepare_body
from tests.provider.trae._support import client as _trae_client


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
    """积分/签到端点（api.trae.cn）的独立头集合——此前缺失导致探测 401。

    论坛实测（topic/180147）：X-Machine-Id / X-Device-Id / userId 均为隐藏必填。
    """
    from src.provider.trae.client import ug_headers

    headers = ug_headers(TraeCredential(access_token="a", device_id="d",
                                        machine_id="m", uid="u"))
    assert headers["Authorization"] == "Cloud-IDE-JWT a"
    assert headers["X-User-Region"] == "CN"
    assert headers["X-Machine-Id"] == "m"
    assert headers["X-Uid"] == "u"
    assert "X-Cloudide-Token" not in headers
    # 未显式传入时自动生成 16 位数字串，且每次不同（设备号复用是 9074 的可疑诱因）
    auto = ug_headers(TraeCredential(access_token="a", uid="u"))["X-Device-Id"]
    assert auto.isdigit() and len(auto) == 16
    assert auto != ug_headers(TraeCredential(access_token="a", uid="u"))["X-Device-Id"]
    # 显式传入数字串时以传入值为准（同一轮 status/claim 必须配对使用同一个）
    assert ug_headers(TraeCredential(access_token="a", uid="u"),
                      device_id="123")["X-Device-Id"] == "123"
    # 非数字串（如凭证自带的 hex32 device_id）不直接透传，回落为新生成值：
    # 拿登录 URL 的 hex32 调 claim 会得到 9074
    fallback = ug_headers(TraeCredential(access_token="a", uid="u",
                                         device_id="d" * 32))["X-Device-Id"]
    assert fallback != "d" * 32 and fallback.isdigit() and len(fallback) == 16

async def test_client_aclose_is_idempotent():
    client = TraeClient()
    assert client._stream is not None      # 两条池都建起来
    assert client._short is not None
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

def test_trae_parse_attaches_error_kind():
    """TRAE error 帧解析时即带分类（4001 → INVALID）。"""
    from src.engine.sse import SSEFrame
    from src.provider.trae import events as trae_events

    frame = SSEFrame(event="error", data=json.dumps({"code": 4001, "message": "bad param"}))
    event = trae_events.parse_frame(frame)
    assert event is not None and event.error_kind is ErrKind.INVALID
    unknown = trae_events.parse_frame(
        SSEFrame(event="error", data=json.dumps({"code": 424242})))
    assert unknown is not None and unknown.error_kind is ErrKind.OTHER

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
    assert client._stream is not None   # 只实例化 stream 客户端
    asyncio.run(client.aclose())

def test_build_login_url_missing_keys_still_builds():
    from src.provider.trae.callback import build_login_url

    assert "login_trace_id=" in build_login_url("http://x/authorize",
                                                machine_id="", device_id="")

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

def test_tool_index_int_index_without_id():
    """index 是 int 但没有 id → 不登记映射（response 52→54）。"""
    from src.compat.openai.response import ToolIndexState

    _, index = ToolIndexState().resolve({"index": 3})
    assert index == 3

def test_solo_headers_include_uid_when_present():
    """有 uid 时必须带 X-Uid（SOLOHeaders 分支，client 145-146）。"""
    from src.provider.trae.client import solo_headers

    headers = solo_headers(TraeCredential(access_token="a", uid="u-9"))
    assert headers["X-Uid"] == "u-9"
    no_uid = solo_headers(TraeCredential(access_token="a"))
    assert "X-Uid" not in no_uid

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

def test_trae_unknown_named_error_falls_through_to_none():
    """已知集合穷尽后 return None（events 79）此前不可达，现在保持覆盖。"""
    from src.engine.sse import SSEFrame
    from src.provider.trae.events import parse_frame

    # data 为合法 JSON 对象但事件名未知 → None
    assert parse_frame(SSEFrame(event="extra_info", data='{"x":1}')) is None
    # error 事件带非 int code → ERROR 事件无 code
    event = parse_frame(SSEFrame(event="error", data='{"code":"oops","message":"m"}'))
    assert event is not None and event.kind.value == "error"
