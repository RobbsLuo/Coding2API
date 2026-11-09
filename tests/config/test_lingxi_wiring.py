"""灵犀接线：Settings 字段、端点白名单、热更节流与注册表。"""

from __future__ import annotations

from src.config import validate_lingxi_endpoint_allowed
from src.engine.model_resolver import KNOWN_PROVIDERS
from src.provider.login_identity import (
    lingxi_login_identities,
    lingxi_login_identity,
)
from src.provider.tos import tos_for
from src.runtime_settings import HOT_BY_KEY, RuntimeSettings
from tests.helpers.auth import settings_with as _settings
from tests.helpers.stores import MemoryStore as _MemoryStore


def test_lingxi_defaults_and_allowed_parsing():
    settings = _settings()
    assert settings.lingxi_api_endpoint == "https://lingxi.wps.cn"
    assert settings.lingxi_allowed == ("https://lingxi.wps.cn",
                                       "https://lingxi.kdocs.cn")
    blank = _settings(lingxi_allowed_endpoints=" https://a.example.com/ , , ")
    assert blank.lingxi_allowed == ("https://a.example.com",)


def test_lingxi_endpoint_whitelist():
    settings = _settings()
    assert validate_lingxi_endpoint_allowed("https://lingxi.wps.cn", settings)
    assert validate_lingxi_endpoint_allowed("https://lingxi.wps.cn/", settings)
    assert validate_lingxi_endpoint_allowed("https://lingxi.kdocs.cn", settings)
    assert not validate_lingxi_endpoint_allowed("https://evil.example.com", settings)


def test_lingxi_chat_min_interval_is_hot():
    assert "lingxi_chat_min_interval" in HOT_BY_KEY
    runtime = RuntimeSettings(_settings(), _MemoryStore())
    assert runtime.lingxi_chat_min_interval == 0
    runtime.set("lingxi_chat_min_interval", 2)
    assert runtime.lingxi_chat_min_interval == 2


def test_lingxi_registered_everywhere():
    assert "lingxi" in KNOWN_PROVIDERS
    assert tos_for("lingxi").level == "gray"


def test_login_identity_prefers_uid_and_falls_back_to_sid_hash():
    assert lingxi_login_identity({"wps_sid": "abc", "uid": "42"}) == "42"
    hashed = lingxi_login_identity({"wps_sid": "abc"})
    assert hashed.startswith("sid-")
    assert lingxi_login_identities({"wps_sid": "abc", "uid": "42"}) == {
        "42", hashed}
    assert lingxi_login_identity({}) == ""
    assert lingxi_login_identities({}) == frozenset()