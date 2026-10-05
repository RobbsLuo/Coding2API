"""按渠道出站代理（P1-6）测试。

覆盖三块：纯解析函数 `parse_provider_proxies`、`build_client` 工厂、
各 provider 客户端 / OAuth 流把代理透传到 httpx，以及 main 装配接线。
"""

from __future__ import annotations

import httpx
import pytest

from src.config import load_settings
from src.engine.model_resolver import KNOWN_PROVIDERS
from src.provider.proxy import PROXY_SCHEMES, build_client, parse_provider_proxies

# ------------------------------------------------------------------ 解析


def test_parse_empty_is_direct():
    assert parse_provider_proxies("", KNOWN_PROVIDERS) == {}


def test_parse_tolerates_blank_segments():
    # 结尾/中间的空白段被忽略，不影响合法段
    parsed = parse_provider_proxies("trae=http://p:1;;  ;zen=http://p:2;",
                                    KNOWN_PROVIDERS)
    assert parsed == {"trae": "http://p:1", "zen": "http://p:2"}


def test_parse_lowercases_and_trims():
    parsed = parse_provider_proxies("  TRAE = socks5://127.0.0.1:1080 ",
                                    KNOWN_PROVIDERS)
    assert parsed == {"trae": "socks5://127.0.0.1:1080"}


def test_parse_duplicate_provider_last_wins():
    parsed = parse_provider_proxies(
        "trae=http://a;trae=http://b", KNOWN_PROVIDERS)
    assert parsed == {"trae": "http://b"}


@pytest.mark.parametrize("scheme", PROXY_SCHEMES)
def test_parse_accepts_all_supported_schemes(scheme):
    parsed = parse_provider_proxies(f"kilo={scheme}://p:9", KNOWN_PROVIDERS)
    assert parsed == {"kilo": f"{scheme}://p:9"}


@pytest.mark.parametrize("raw", [
    "trae",                      # 缺 =
    "=http://p:1",               # 空渠道
    "trae=",                     # 空 URL
    "trae=ftp://p:1",            # 非法协议
    "trae=p:1",                  # 无协议头
    "unknown=http://p:1",        # 未知渠道
])
def test_parse_rejects_bad_segments(raw):
    # 启动期配置：宁可启动失败也不静默走直连
    with pytest.raises(ValueError):
        parse_provider_proxies(raw, KNOWN_PROVIDERS)


def test_parse_known_set_can_be_narrowed():
    # 渠道集合由调用方给：不在集合内即报错
    with pytest.raises(ValueError):
        parse_provider_proxies("zen=http://p:1", ("trae",))


# ------------------------------------------------------------- build_client


async def test_build_client_without_proxy_ignores_env(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://env-proxy:1")
    client = build_client(timeout=httpx.Timeout(5.0))
    try:
        # trust_env=False：环境代理不被采信（无显式 proxy → 无 mount）
        assert client._mounts == {}
    finally:
        await client.aclose()


async def test_build_client_with_proxy():
    client = build_client(timeout=httpx.Timeout(5.0), proxy="http://127.0.0.1:7890")
    try:
        # 显式代理 → 单一 mount（所有请求走代理）
        assert len(client._mounts) == 1
    finally:
        await client.aclose()


# ------------------------------------------------ provider 客户端透传代理


def _capture_build(monkeypatch, module):
    """把模块内的 build_client 换成记录参数的替身。"""
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return httpx.AsyncClient()

    monkeypatch.setattr(module, "build_client", fake)
    return calls


def _trae_client(proxy):
    from src.provider.trae.client import TraeClient

    return TraeClient(proxy=proxy)


def _zen_client(proxy):
    from src.provider.zen.client import ZenClient

    return ZenClient(proxy=proxy)


def _kilo_client(proxy):
    from src.provider.kilo.client import KiloClient

    return KiloClient(proxy=proxy)


def _codebuddy_client(proxy):
    from src.provider.codebuddy.client import CodeBuddyClient

    return CodeBuddyClient(proxy=proxy)


def _qoder_client(proxy):
    from src.provider.qoder.client import QoderClient

    return QoderClient(proxy=proxy)


def _codearts_client(proxy):
    from src.provider.codearts.client import CodeArtsClient

    return CodeArtsClient(proxy=proxy)


@pytest.mark.parametrize("factory,module_path,is_property", [
    (_trae_client, "src.provider.trae.client", False),
    (_zen_client, "src.provider.zen.client", False),
    (_kilo_client, "src.provider.kilo.client", False),
    (_codebuddy_client, "src.provider.codebuddy.client", True),
    (_qoder_client, "src.provider.qoder.client", True),
    (_codearts_client, "src.provider.codearts.client", False),
])
async def test_provider_client_forwards_proxy(monkeypatch, factory, module_path,
                                              is_property):
    import importlib

    module = importlib.import_module(module_path)
    calls = _capture_build(monkeypatch, module)
    client = factory("socks5://127.0.0.1:1080")

    stream = client._stream if is_property else client._stream()
    short = client._short if is_property else client._short()
    assert calls[0]["proxy"] == "socks5://127.0.0.1:1080"
    assert calls[1]["proxy"] == "socks5://127.0.0.1:1080"
    assert stream is not None and short is not None
    await client.aclose()


def _codearts_login():
    from src.provider.codearts.auth import LoginConfig

    return LoginConfig(snap_manager="https://snap.example",
                       sts_host="https://sts.example")


async def test_codebuddy_oauth_forwards_proxy(monkeypatch):
    import src.provider.codebuddy.oauth as module

    calls = _capture_build(monkeypatch, module)
    oauth = module.CodeBuddyOAuth("https://copilot.tencent.com", proxy="http://p:1")
    _ = oauth._http
    assert calls[0]["proxy"] == "http://p:1"
    await oauth.aclose()


async def test_qoder_oauth_forwards_proxy(monkeypatch):
    import src.provider.qoder.auth as module

    calls = _capture_build(monkeypatch, module)
    oauth = module.QoderOAuth(proxy="http://p:1")
    _ = oauth._http
    assert calls[0]["proxy"] == "http://p:1"
    await oauth.aclose()


async def test_codearts_oauth_forwards_proxy(monkeypatch):
    import src.provider.codearts.oauth as module

    calls = _capture_build(monkeypatch, module)
    oauth = module.CodeArtsOAuth(_codearts_login(), proxy="http://p:1")
    _ = oauth._http
    assert calls[0]["proxy"] == "http://p:1"
    await oauth.aclose()


# ------------------------------------------------------------- main 装配


def _settings(**overrides):
    base = {"app_secret": "test-secret-0123456789", "data_dir": "/tmp/c2a-proxy"}
    return load_settings({**base, **overrides})


def test_build_app_applies_provider_proxies(monkeypatch, tmp_path):
    import src.main as main

    captured: dict[str, str | None] = {}

    class _FakeTraeClient:
        def __init__(self, *, proxy=None, **kwargs):
            captured["trae"] = proxy

    class _FakeCodeBuddyClient:
        def __init__(self, *, proxy=None, **kwargs):
            captured["codebuddy"] = proxy

    monkeypatch.setattr(main, "TraeClient", _FakeTraeClient)
    monkeypatch.setattr(main, "CodeBuddyClient", _FakeCodeBuddyClient)

    settings = _settings(data_dir=str(tmp_path),
                         provider_proxies="trae=http://p:1;codebuddy=socks5://p:2")
    app = main.build_app(settings)
    assert app.state.executor is not None
    assert captured == {"trae": "http://p:1", "codebuddy": "socks5://p:2"}


def test_upstream_auth_applies_proxies(monkeypatch):
    import src.main as main
    from src.provider.codebuddy.client import CodeBuddyClient, CodeBuddyProvider
    from src.provider.qoder import QoderProvider
    from src.provider.qoder.client import QoderClient

    captured: dict[str, str | None] = {}

    class _SpyCB:
        def __init__(self, endpoint, *, proxy=None, **kwargs):
            captured["codebuddy"] = proxy

    class _SpyQoder:
        def __init__(self, realm, *, proxy=None, **kwargs):
            captured["qoder"] = proxy

    monkeypatch.setattr(main, "CodeBuddyOAuth", _SpyCB)
    monkeypatch.setattr(main, "QoderOAuth", _SpyQoder)

    registry = {
        "codebuddy": CodeBuddyProvider(client=CodeBuddyClient()),
        "qoder": QoderProvider(client=QoderClient()),
    }
    settings = _settings(provider_proxies="codebuddy=http://p:1;qoder=http://p:2")
    main._upstream_auth(registry, settings)
    assert captured == {"codebuddy": "http://p:1", "qoder": "http://p:2"}


def test_upstream_auth_without_registry_returns_empty():
    import src.main as main

    assert main._upstream_auth({}, _settings()) == {}


def test_build_app_rejects_bad_provider_proxies(tmp_path):
    from src.main import build_app

    settings = _settings(data_dir=str(tmp_path), provider_proxies="nope=http://p:1")
    with pytest.raises(ValueError):
        build_app(settings)