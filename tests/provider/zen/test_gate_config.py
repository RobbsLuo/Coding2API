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
    EP_CHAT,
    MIN_OPENCODE_VERSION,
    REQUIRED_TOOLS,
    ZenClient,
    _normalize_api_root,
    ensure_gate_tools,
    gate_headers,
    prepare_body,
)
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_text_done as _sse


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
    assert client.version == MIN_OPENCODE_VERSION
    assert client.user_agent == ""
