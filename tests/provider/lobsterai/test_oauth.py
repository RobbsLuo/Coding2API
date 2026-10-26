"""LobsterAI 登录闭环：登录 URL 构造、回调解析、state 校验。"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from src.provider.base import UpstreamProtocolViolation
from src.provider.lobsterai.oauth import (
    CALLBACK_PATH,
    CALLBACK_PORT,
    build_login_url,
    new_session,
    parse_callback_url,
)


def test_new_session_generates_identity_triple():
    session = new_session()
    assert session.uuid
    assert session.first_keyfrom.isdigit()
    assert session.upstream_state


def test_build_login_url_carries_portal_params():
    """门户只接受回环 redirect_uri；state 必须原样回显。"""
    session = new_session()
    url = build_login_url(session, portal_base="https://portal.test")
    parsed = urlparse(url)
    assert parsed.netloc == "portal.test"
    assert parsed.path == "/portal"
    fragment = parsed.fragment
    assert fragment.startswith("/login?")
    params = parse_qs(fragment.split("?", 1)[1])
    assert params["source"] == ["electron"]
    assert params["state"] == [session.upstream_state]
    redirect = params["redirect_uri"][0]
    assert redirect == f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}"


def test_build_login_url_uses_given_portal_base():
    session = new_session()
    url = build_login_url(session, portal_base="https://portal.test/")
    assert url.startswith("https://portal.test/portal#/login?")


def test_parse_callback_url_reads_code_and_state():
    info = parse_callback_url(
        f"http://127.0.0.1:{CALLBACK_PORT}/auth/callback?code=ABC&state=XYZ")
    assert info.code == "ABC"
    assert info.state == "XYZ"


def test_parse_callback_url_accepts_bare_query():
    info = parse_callback_url("code=ABC&state=XYZ")
    assert info.code == "ABC"
    assert info.state == "XYZ"


def test_parse_callback_url_rejects_missing_code():
    with pytest.raises(UpstreamProtocolViolation):
        parse_callback_url("http://127.0.0.1/auth/callback?state=XYZ")
    with pytest.raises(UpstreamProtocolViolation):
        parse_callback_url("   ")
