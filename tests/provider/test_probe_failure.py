"""额度探测失败 → 重新登录原因。"""

from __future__ import annotations

from src.provider.lingxi.client import UpstreamHTTPError as LingxiHTTPError
from src.provider.probe_failure import relogin_reason


def test_non_http_error_keeps_unknown():
    assert relogin_reason("lingxi", RuntimeError("boom")) == ""


def test_unbound_classify_status_keeps_unknown():
    """基类异常没绑定 classify_status（测试替身形态）→ 不升级处置。"""
    from src.provider.base import UpstreamHTTPError

    assert relogin_reason("lingxi", UpstreamHTTPError(401, b"")) == ""


def test_non_dead_http_error_keeps_unknown():
    # 灵犀把 500 归为 OTHER：只是一次探测失败，不能据此停号
    assert relogin_reason("lingxi", LingxiHTTPError(500, b"")) == ""


def test_lingxi_dead_explains_kicked_session():
    reason = relogin_reason("lingxi", LingxiHTTPError(401, b""))
    assert "WPS" in reason
    assert "登录新账号" in reason


def test_other_provider_dead_gets_generic_reason():
    reason = relogin_reason("codebuddy", LingxiHTTPError(401, b""))
    assert "登录态失效" in reason
    assert "WPS" not in reason