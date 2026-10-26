"""LobsterAI 接线：Settings 字段、端点白名单与热更最小间隔。"""

from __future__ import annotations

import pytest

from src.config import validate_lobsterai_endpoint_allowed
from src.runtime_settings import HOT_BY_KEY, RuntimeSettings
from tests.helpers.auth import settings_with as _settings
from tests.helpers.stores import MemoryStore as _MemoryStore

# --------------------------------------------------------------- Settings 字段

def test_lobsterai_defaults_and_allowed_parsing():
    settings = _settings()
    assert settings.lobsterai_api_endpoint == "https://lobsterai-server.youdao.com"
    assert settings.lobsterai_portal_endpoint == "https://lobsterai.youdao.com"
    assert settings.lobsterai_version_endpoint == (
        "https://api-overmind.youdao.com/openapi/get/luna/hardware/lobsterai/prod/update")
    # 白名单含三个主机：API 域、门户域、版本域
    assert settings.lobsterai_allowed == (
        "https://lobsterai-server.youdao.com",
        "https://lobsterai.youdao.com",
        "https://api-overmind.youdao.com/openapi/get/luna/hardware/lobsterai/prod/update",
    )


def test_lobsterai_allowed_parsing_ignores_blank_entries():
    settings = _settings(
        lobsterai_allowed_endpoints=" https://a.example.com/ , , ")
    assert settings.lobsterai_allowed == ("https://a.example.com",)


# --------------------------------------------------------------- 端点白名单

def test_lobsterai_endpoint_whitelist():
    settings = _settings()
    assert validate_lobsterai_endpoint_allowed(
        "https://lobsterai-server.youdao.com", settings)
    assert validate_lobsterai_endpoint_allowed(
        "https://lobsterai-server.youdao.com/", settings)
    assert validate_lobsterai_endpoint_allowed(
        "https://lobsterai.youdao.com", settings)
    assert not validate_lobsterai_endpoint_allowed("https://evil.example.com", settings)


def test_lobsterai_endpoints_reject_unallowlisted_host():
    from src.config import Settings
    from src.main import _lobsterai_endpoint
    from tests.helpers.constants import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        LOBSTERAI_API_ENDPOINT="https://evil.example.com")
    with pytest.raises(ValueError, match="LOBSTERAI_ALLOWED_ENDPOINTS"):
        _lobsterai_endpoint(settings, "lobsterai_api_endpoint",
                             "LOBSTERAI_API_ENDPOINT")


def test_lobsterai_endpoints_accept_defaults():
    from src.config import Settings
    from src.main import _lobsterai_endpoint
    from tests.helpers.constants import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET)
    assert _lobsterai_endpoint(
        settings, "lobsterai_api_endpoint", "LOBSTERAI_API_ENDPOINT") == (
        "https://lobsterai-server.youdao.com")
    assert _lobsterai_endpoint(
        settings, "lobsterai_portal_endpoint", "LOBSTERAI_PORTAL_ENDPOINT") == (
        "https://lobsterai.youdao.com")
    assert _lobsterai_endpoint(
        settings, "lobsterai_version_endpoint",
        "LOBSTERAI_VERSION_ENDPOINT").endswith("/prod/update")


# --------------------------------------------------------------- 热更最小间隔

def test_lobsterai_min_interval_is_hot():
    """LobsterAI 聊天最小间隔可热更（默认 0 = 不节流）。"""
    assert "lobsterai_chat_min_interval" in HOT_BY_KEY
    runtime = RuntimeSettings(_settings(), _MemoryStore())
    assert runtime.lobsterai_chat_min_interval == 0
    runtime.set("lobsterai_chat_min_interval", 2.5)
    assert runtime.lobsterai_chat_min_interval == 2.5


def test_lobsterai_is_a_known_provider():
    """新渠道必须进 KNOWN_PROVIDERS：否则 @lobsterai 路由与代理白名单都不认。"""
    from src.engine.model_resolver import KNOWN_PROVIDERS

    assert "lobsterai" in KNOWN_PROVIDERS
