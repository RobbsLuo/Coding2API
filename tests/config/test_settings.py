"""Settings 默认值、必填项与端点白名单。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings, trusted_proxies_problem, validate_endpoint_allowed
from src.engine.scheduler import EXPIRY_WINDOW_SECONDS, SECONDARY_EXPIRY_WINDOW_SECONDS
from tests.helpers.constants import SECRET


def test_settings_defaults_and_admin_set():
    s = Settings(_env_file=None, APP_SECRET=SECRET, ADMIN_USERNAMES="alice, bob ,")
    assert s.admin_set == frozenset({"alice", "bob"})
    assert s.default_model == "deepseek-v4.1-flash"
    assert s.quota_expiry_window_seconds == EXPIRY_WINDOW_SECONDS
    assert s.quota_expiry_secondary_window_seconds == SECONDARY_EXPIRY_WINDOW_SECONDS
    assert s.is_admin("alice") and not s.is_admin("carol")

def test_settings_requires_app_secret():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None)

def test_allowed_endpoints_whitelist():
    s = Settings(_env_file=None, APP_SECRET=SECRET)
    assert validate_endpoint_allowed("https://copilot.tencent.com", s)
    assert validate_endpoint_allowed("https://www.codebuddy.ai", s)
    assert not validate_endpoint_allowed("https://evil.example", s)

def test_db_path_joins_data_dir():
    s = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR="/tmp/cbd")
    assert s.db_path == "/tmp/cbd/coding2api.sqlite3"


# --------------------------------------------------------- PUBLIC_BASE_URL 形态


@pytest.mark.parametrize("value", [
    "http://127.0.0.1:8000",
    "https://ai.example.com",
    "https://ai.example.com:8443",
    "https://ai.example.com/callback/x",
    "http://[::1]:8000",
])
def test_public_base_url_accepts_legal_shapes(value):
    s = Settings(_env_file=None, APP_SECRET=SECRET, PUBLIC_BASE_URL=value)
    assert s.public_base_url == value


@pytest.mark.parametrize("value", [
    "127.0.0.1:8000",       # 缺 scheme → 回调变相对路径
    "",                      # 空串
    "ftp://ai.example.com",  # 非 http(s)
    "https://",              # 无主机
    "http:///oops",          # 有 scheme 无主机
    "http://[::1",           # 未闭合 IPv6 → urlsplit 抛 ValueError
])
def test_public_base_url_rejects_illegal_shapes(value):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, APP_SECRET=SECRET, PUBLIC_BASE_URL=value)
    assert "PUBLIC_BASE_URL" in str(caught.value)


# ------------------------------------------- TRUST_PROXY / TRUSTED_PROXIES 组合


def test_require_trusted_proxies_defaults_off():
    """默认 false：不改变存量部署的行为（空名单照旧采信任意对端）。"""
    s = Settings(_env_file=None, APP_SECRET=SECRET)
    assert s.require_trusted_proxies is False
    assert s.trust_proxy is False
    assert s.trusted_proxies == ""


def test_require_trusted_proxies_reads_env_name():
    s = Settings(_env_file=None, APP_SECRET=SECRET, REQUIRE_TRUSTED_PROXIES="true")
    assert s.require_trusted_proxies is True


def test_trusted_proxies_problem_ignored_when_not_required():
    """未强制时不判：空名单是存量部署的合法形态。"""
    assert trusted_proxies_problem(
        trust_proxy=True, trusted_proxies="", require_trusted_proxies=False) is None


def test_trusted_proxies_problem_ignored_without_trust_proxy():
    """不采信 XFF 就没有这条风险面，名单留空也不该拦。"""
    assert trusted_proxies_problem(
        trust_proxy=False, trusted_proxies="", require_trusted_proxies=True) is None


def test_trusted_proxies_problem_flags_empty_list():
    problem = trusted_proxies_problem(
        trust_proxy=True, trusted_proxies="   ", require_trusted_proxies=True)
    assert problem is not None
    assert "TRUSTED_PROXIES" in problem and "allowed_ips" in problem
    assert "关闭 TRUST_PROXY" in problem


def test_trusted_proxies_problem_flags_wholly_unparsable_list():
    """名单非空但一条都解析不出：运行期 fail closed，运维却以为已生效。"""
    problem = trusted_proxies_problem(
        trust_proxy=True, trusted_proxies="not-an-ip", require_trusted_proxies=True)
    assert problem is not None
    assert "not-an-ip" in problem and "fail closed" in problem


def test_trusted_proxies_problem_accepts_valid_list():
    """解析口径与运行期一致：`10.0.0.1/24` 这类 strict=False 写法也算合法。"""
    assert trusted_proxies_problem(
        trust_proxy=True, trusted_proxies="10.0.0.1/24, 2001:db8::/32",
        require_trusted_proxies=True) is None


# ------------------------------------------------------ OTLP 指标出口配置


def test_otlp_defaults_are_off_with_sixty_second_period():
    s = Settings(_env_file=None, APP_SECRET=SECRET)
    assert s.otlp_endpoint == ""
    assert s.otlp_export_seconds == 60
    assert s.otlp_headers == ""


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:4318",
    "http://localhost:4318",
    "https://collector.example.com:4318",
])
def test_otlp_endpoint_accepts_legitimate_shapes(endpoint):
    s = Settings(_env_file=None, APP_SECRET=SECRET, OTLP_ENDPOINT=endpoint)
    assert s.otlp_endpoint == endpoint


@pytest.mark.parametrize(("endpoint", "fragment"), [
    ("file:///etc/passwd", "只接受 http/https"),
    ("collector.example.com:4318", "只接受 http/https"),
    ("https://", "缺少主机名"),
    # 明文 http 打公网 = 把 OTLP_HEADERS 里的令牌挂到链路上
    ("http://collector.example.com:4318", "必须使用 https"),
])
def test_otlp_endpoint_rejects_dangerous_shapes_at_startup(endpoint, fragment):
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, APP_SECRET=SECRET, OTLP_ENDPOINT=endpoint)
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize("seconds", [15, 60, 3600])
def test_otlp_export_seconds_accepts_values_at_or_above_floor(seconds):
    s = Settings(_env_file=None, APP_SECRET=SECRET, OTLP_EXPORT_SECONDS=seconds)
    assert s.otlp_export_seconds == seconds


@pytest.mark.parametrize("seconds", [0, -1, 14])
def test_otlp_export_seconds_rejects_busy_loop_values(seconds):
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, APP_SECRET=SECRET, OTLP_EXPORT_SECONDS=seconds)
    assert "不能小于 15" in str(excinfo.value)
