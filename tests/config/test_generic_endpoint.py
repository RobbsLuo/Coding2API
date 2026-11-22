"""通用渠道端点校验：非回环主机强制 https。

凭证是真实 API key，明文 http 会让 key 在同网段上裸奔。所以「白名单为空 =
放行任意端点」只对 https 成立；http 仅限回环（本机自建服务 + 隧道）。
"""

from __future__ import annotations

import pytest

from src.config import Settings, validate_generic_endpoint_allowed
from tests.helpers.constants import SECRET


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, APP_SECRET=SECRET, **overrides)


@pytest.mark.parametrize("endpoint", [
    "https://api.example.com/v1",
    "https://10.0.0.5:8443/v1",
    "https://api.example.com/v1/",
])
def test_https_any_host_allowed_when_whitelist_empty(endpoint):
    assert validate_generic_endpoint_allowed(endpoint, _settings())


@pytest.mark.parametrize("endpoint", [
    "http://10.0.0.5:8080/v1",
    "http://api.example.com/v1",
    "http://192.168.1.9:8080/v1",
])
def test_http_public_host_rejected(endpoint):
    assert not validate_generic_endpoint_allowed(endpoint, _settings())


@pytest.mark.parametrize("endpoint", [
    "http://localhost:9000/v1",
    "http://127.0.0.1:9000/v1",
    "http://[::1]:9000/v1",
])
def test_http_loopback_allowed(endpoint):
    assert validate_generic_endpoint_allowed(endpoint, _settings())


@pytest.mark.parametrize("endpoint", [
    "file:///etc/passwd",
    "gopher://127.0.0.1/x",
    "ftp://api.example.com/x",
])
def test_non_http_schemes_rejected_even_for_loopback(endpoint):
    assert not validate_generic_endpoint_allowed(endpoint, _settings())


def test_whitelist_hit_and_miss():
    settings = _settings(
        GENERIC_ALLOWED_ENDPOINTS="https://api.example.com/v1,http://127.0.0.1:9000/v1")
    assert validate_generic_endpoint_allowed("https://api.example.com/v1", settings)
    assert validate_generic_endpoint_allowed("http://127.0.0.1:9000/v1", settings)
    assert not validate_generic_endpoint_allowed("https://evil.example/x", settings)


def test_whitelist_does_not_override_https_rule():
    """白名单里写了明文公网端点也不能放行：规则在名单之上。"""
    settings = _settings(GENERIC_ALLOWED_ENDPOINTS="http://api.example.com/v1")
    assert not validate_generic_endpoint_allowed("http://api.example.com/v1", settings)


def test_loopback_with_whitelist_miss_rejected():
    settings = _settings(GENERIC_ALLOWED_ENDPOINTS="http://127.0.0.1:9000/v1")
    assert not validate_generic_endpoint_allowed("http://127.0.0.1:9001/v1", settings)


def test_unparsable_http_endpoint_rejected():
    """`urlsplit` 抛 ValueError（未闭合 IPv6）时不放行。"""
    assert not validate_generic_endpoint_allowed("http://[::1", _settings())