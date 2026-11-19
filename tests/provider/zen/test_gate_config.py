"""Zen 门禁的**可配置部分**：UA 形态、API 根、门禁工具集合。

三件套都是移动靶（上游随时收紧），因此全部走配置/构造参数，默认值保持
仓库实测通过的行为：

- UA：`opencode/<version>`；模板可用 `{version}` 占位；
- 根：`/zen/v1`（默认）或 `/inference/v1`；
- 门禁工具：默认 `bash,read`，可切成第三方实测的四件套。

逐字节 hash 未实现：上游当前是否校验 tools 定义 hash **未核实**（本仓
2026-10-28 复验受免费额度 429 阻断），凭空猜 hash 只会把可用状态换成 403。
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.provider.zen.client import (
    DEFAULT_API_ROOT,
    ENV_BLOCK,
    EP_CHAT,
    MIN_OPENCODE_VERSION,
    REQUIRED_TOOLS,
    ZenClient,
    _normalize_api_root,
    ensure_env_block,
    ensure_gate_tools,
    gate_headers,
    has_env_block,
    prepare_body,
)
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_text_done as _sse

pytestmark = pytest.mark.real_upstream("zen")


def _handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=_sse(chunk({"content": "ok"}, finish="stop")))


def _client(handler, **kwargs) -> ZenClient:
    transport = httpx.MockTransport(handler)
    return ZenClient(stream_client=httpx.AsyncClient(transport=transport),
                     short_client=httpx.AsyncClient(transport=transport),
                     **kwargs)


# ------------------------------------------------------------------ UA 形态


def test_default_ua_shape():
    assert gate_headers(version="2.3.4")["User-Agent"] == "opencode/2.3.4"


def test_ua_template_renders_version():
    headers = gate_headers(version="2.0.29", user_agent="opencode/latest/{version}/cli")
    assert headers["User-Agent"] == "opencode/latest/2.0.29/cli"


def test_ua_without_placeholder_is_verbatim():
    headers = gate_headers(version="9.9.9", user_agent="opencode/edge")
    assert headers["User-Agent"] == "opencode/edge"


async def test_client_sends_configured_ua():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return _handler(request)

    client = _client(handler, version="2.0.29",
                     user_agent="opencode/latest/{version}/cli")
    [e async for e in client.stream_chat({"messages": []}, "m")]
    assert seen == ["opencode/latest/2.0.29/cli"]


# ------------------------------------------------------------------ API 根


def test_normalize_api_root_strips_trailing_slash():
    assert _normalize_api_root("/inference/v1/") == "/inference/v1"


def test_normalize_api_root_defaults_for_empty():
    assert _normalize_api_root("  ") == DEFAULT_API_ROOT


@pytest.mark.parametrize("bad", ["zen/v1", "/zen/v1?x=1", "/zen/v1#frag",
                                 "/zen v1"])
def test_normalize_api_root_rejects_malformed(bad):
    with pytest.raises(ValueError):
        _normalize_api_root(bad)


async def test_client_uses_configured_root():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _handler(request)

    client = _client(handler, api_root="/inference/v1")
    [e async for e in client.stream_chat({"messages": []}, "m")]
    assert paths == ["/inference/v1/chat/completions"]
    assert EP_CHAT == "/zen/v1/chat/completions"   # 默认常量不变


# -------------------------------------------------------------- 门禁工具集合


def test_default_gate_tools_unchanged():
    body: dict = {}
    injected = ensure_gate_tools(body)
    assert injected == frozenset(REQUIRED_TOOLS)
    assert [t["function"]["name"] for t in body["tools"]] == list(REQUIRED_TOOLS)


def test_custom_gate_tools_four_piece_set():
    body: dict = {"tools": [{"type": "function", "function": {"name": "bash"}}]}
    injected = ensure_gate_tools(body, ("bash", "glob", "grep", "read"))
    assert injected == frozenset({"glob", "grep", "read"})
    names = [t["function"]["name"] for t in body["tools"]]
    assert names == ["bash", "glob", "grep", "read"]


def test_prepare_body_forwards_required_tools():
    payload = {"messages": [{"role": "user", "content": "x"}]}
    body, injected = prepare_body(payload, "m", ("bash", "read", "grep"))
    assert injected == frozenset({"bash", "read", "grep"})
    assert body["stream"] is True
    # 原 payload 不被原地改写（消息深拷、tools 只在 body 上补）
    assert "tools" not in payload


async def test_client_sends_configured_tool_set():
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append([t["function"]["name"] for t in payload["tools"]])
        return _handler(request)

    client = _client(handler, gate_tools=("bash", "glob", "grep", "read"))
    [e async for e in client.stream_chat({"messages": []}, "m")]
    assert seen == [["bash", "glob", "grep", "read"]]


def test_empty_gate_tools_falls_back_to_default():
    assert _client(_handler, gate_tools=()).gate_tools == REQUIRED_TOOLS


def test_client_defaults_match_upstream_known_state():
    client = _client(_handler)
    assert client.api_root == DEFAULT_API_ROOT
    assert client.models_root == DEFAULT_API_ROOT
    assert client.profile == "legacy"
    assert client.version == MIN_OPENCODE_VERSION
    assert client.user_agent == ""


# ------------------------------------------------------------ v2 客户端指纹


def test_gate_headers_v2_repeats_session_and_trace():
    headers = gate_headers(version="2.2.2", profile="v2",
                           session_id="ses_" + "a" * 12 + "b" * 14,
                           trace_id="f" * 32)
    session = "ses_" + "a" * 12 + "b" * 14
    assert headers["User-Agent"] == "opencode/latest/2.2.2/cli"
    assert headers["x-opencode-client"] == "cli"
    assert headers["x-opencode-project"] == "global"
    assert headers["x-opencode-session"] == session
    assert headers["x-opencode-session-id"] == session
    assert headers["x-session-affinity"] == session
    assert headers["x-session-id"] == session
    traceparent = headers["traceparent"].split("-")
    assert traceparent[0] == "00"
    assert traceparent[1] == "f" * 32
    assert traceparent[3] == "01"
    b3 = headers["b3"].split("-")
    assert b3[:2] == traceparent[1:3]
    assert b3[2] == "1"
    assert len(b3[3]) == 16


def test_gate_headers_v2_mints_trace_when_not_injected():
    headers = gate_headers(profile="v2")
    assert len(headers["traceparent"].split("-")[1]) == 32


def test_gate_headers_v2_honours_explicit_ua():
    headers = gate_headers(profile="v2", user_agent="opencode/edge")
    assert headers["User-Agent"] == "opencode/edge"


def test_gate_headers_legacy_omits_v2_headers():
    headers = gate_headers()
    assert "x-opencode-client" not in headers
    assert "x-session-id" not in headers


def test_unknown_client_profile_rejected():
    with pytest.raises(ValueError, match="未知 ZEN 客户端档位"):
        gate_headers(profile="v3")
    with pytest.raises(ValueError, match="未知 ZEN 客户端档位"):
        _client(_handler, profile="v3")


# --------------------------------------------------------------- `<env>` 块


def test_has_env_block_requires_pair_and_workdir():
    assert has_env_block("<env>Working directory: /tmp</env>")
    assert not has_env_block("no block here")
    assert not has_env_block("<env>Working directory: /tmp")
    assert not has_env_block("<env>Workspace root: /tmp</env>")
    # 只认第一对：空对在前 → 无效
    assert not has_env_block("<env></env><env>Working directory: /</env>")


def test_ensure_env_block_inserts_system_message_when_missing():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert ensure_env_block(body) is True
    assert body["messages"][0] == {"role": "system", "content": ENV_BLOCK}
    assert body["messages"][1]["role"] == "user"


def test_ensure_env_block_keeps_existing_valid_block():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "<env>Working directory: /root</env>"},
    ]
    assert ensure_env_block({"messages": messages}) is False
    assert len(messages) == 2


def test_ensure_env_block_appends_to_plain_system_text():
    messages = [{"role": "system", "content": "be nice"}]
    assert ensure_env_block({"messages": messages}) is True
    assert messages[0]["content"] == f"be nice\n\n{ENV_BLOCK}"


def test_ensure_env_block_fills_empty_system_text():
    messages = [{"role": "system", "content": ""}]
    assert ensure_env_block({"messages": messages}) is True
    assert messages[0]["content"] == ENV_BLOCK


def test_ensure_env_block_prepends_when_invalid_tag_present():
    messages = [{"role": "system", "content": "<env></env>ctx"}]
    assert ensure_env_block({"messages": messages}) is True
    assert messages[0]["content"] == f"{ENV_BLOCK}\n\n<env></env>ctx"


def test_ensure_env_block_handles_content_parts():
    plain = [{"role": "system", "content": [{"type": "text", "text": "ctx"}]}]
    assert ensure_env_block({"messages": plain}) is True
    assert plain[0]["content"][-1] == {"type": "text", "text": ENV_BLOCK}

    tagged = [{"role": "system", "content": [{"type": "text", "text": "<env>"}]}]
    assert ensure_env_block({"messages": tagged}) is True
    assert tagged[0]["content"][0] == {"type": "text", "text": ENV_BLOCK}


def test_ensure_env_block_replaces_other_content_shapes():
    messages = [{"role": "system", "content": None}]
    assert ensure_env_block({"messages": messages}) is True
    assert messages[0]["content"] == ENV_BLOCK


def test_ensure_env_block_ignores_non_list_messages():
    assert ensure_env_block({}) is False
    assert ensure_env_block({"messages": "bad"}) is False


def test_prepare_body_v2_injects_env_on_copy():
    payload = {"messages": [{"role": "user", "content": "x"}]}
    body, _ = prepare_body(payload, "m", profile="v2")
    assert body["messages"][0]["role"] == "system"
    assert "Working directory:" in body["messages"][0]["content"]
    assert payload["messages"][0]["role"] == "user"   # 原 payload 不被改写


def test_prepare_body_legacy_skips_env():
    body, _ = prepare_body({"messages": [{"role": "user", "content": "x"}]}, "m")
    assert body["messages"][0]["role"] == "user"


async def test_v2_stream_sends_identity_headers_and_env():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        seen["body"] = json.loads(request.content)
        return _handler(request)

    client = _client(handler, profile="v2", version="2.2.2")
    [e async for e in client.stream_chat(
        {"messages": [{"role": "user", "content": "x"}]}, "m")]
    assert seen["user-agent"] == "opencode/latest/2.2.2/cli"
    assert seen["x-opencode-session-id"] == seen["x-opencode-session"]
    assert seen["x-session-affinity"] == seen["x-opencode-session"]
    assert seen["body"]["messages"][0]["role"] == "system"
    assert "Working directory:" in seen["body"]["messages"][0]["content"]


async def test_models_root_is_decoupled_from_chat_root():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/inference/v1/models":
            return httpx.Response(200, json={"data": [{"id": "a-free"}]})
        return _handler(request)

    client = _client(handler, api_root="/inference/openai/v1",
                     models_root="/inference/v1")
    models = await client.fetch_models()
    assert [m.id for m in models] == ["a-free"]
    assert paths == ["/inference/v1/models",
                     "/inference/openai/v1/chat/completions"]
