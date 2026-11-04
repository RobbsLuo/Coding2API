"""会话令牌签发与校验。"""

from __future__ import annotations

import pytest

from src.auth import session as session_mod


def test_session_roundtrip_and_expiry():
    token = session_mod.create_session_token("alice", "sec", issued_at=1000, ttl_seconds=60)
    assert session_mod.verify_session_token(token, "sec", now=1030) == ("alice", 0, 1000)
    assert session_mod.verify_session_token(token, "sec", now=2000) is None
    assert session_mod.verify_session_token(token, "wrong", now=1030) is None

@pytest.mark.parametrize("bad", ["", "abc", "a.b", "notbase64!!.x"])
def test_session_rejects_malformed(bad):
    assert session_mod.verify_session_token(bad, "sec", now=1) is None


@pytest.mark.parametrize("bad", [
    "abc.\u00ff\u00fe",   # 签名段非 ASCII：compare_digest 抛 TypeError
    "\u00ff.\u00ff",      # payload 段非 ASCII：_sign 的 encode("ascii") 抛 UnicodeError
])
def test_session_rejects_non_ascii_cookie_without_raising(bad):
    """非 ASCII Cookie 由客户端任意构造，必须归一为「非法」而不是冒泡 500。"""
    assert session_mod.verify_session_token(bad, "sec", now=1) is None


@pytest.mark.parametrize("issued_at", ["1000", True, -1])
def test_session_rejects_invalid_issued_at(issued_at):
    """显式写了非法 `iat` 才拒绝；缺失（老 Cookie）走 None 由调用方跳过校验。"""
    import base64
    import json

    raw = json.dumps({"u": "alice", "ep": 0, "iat": issued_at, "exp": 2000},
                     separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    token = f"{payload_b64}.{session_mod._sign(payload_b64, 'sec')}"
    assert session_mod.verify_session_token(token, "sec", now=1500) is None


def test_session_without_issued_at_returns_none():
    """缺 `iat` 的老 Cookie：返回 None 让 `session_valid` 跳过创建时间校验。"""
    import base64
    import json

    raw = json.dumps({"u": "alice", "ep": 0, "exp": 2000},
                     separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    token = f"{payload_b64}.{session_mod._sign(payload_b64, 'sec')}"
    assert session_mod.verify_session_token(token, "sec", now=1500) == ("alice", 0, None)
