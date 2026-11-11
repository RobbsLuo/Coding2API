"""Qoder 凭证解析、领域判定与刷新。"""

from __future__ import annotations

import base64
import json
import time

import pytest

from src.provider.qoder import credential_key
from src.provider.qoder import events as qoder_events
from src.provider.qoder.credential import (
    QoderCredential,
    credential_from_device,
    expires_from_device,
    merge_refreshed,
    parse_credential,
)
from src.provider.qoder.events import UpstreamProtocolViolation
from tests.provider.qoder._support import cred


def test_credential_roundtrip_and_realm_detection():
    raw = {"access_token": "t", "uid": "u", "realm": "intl",
           "refresh_token": "r", "expires_at": 123, "auth_source": "oauth",
           "user_type": "pro", "organization_id": "o", "organization_name": "O",
           "nickname": "N", "domain": "qoder.com"}
    restored = QoderCredential.from_dict(QoderCredential.from_dict(raw).to_dict())
    assert restored == QoderCredential.from_dict(raw)
    # 无 realm 时按 domain 推断
    assert QoderCredential.from_dict({"domain": "qoder.sh"}).realm == "intl"
    assert QoderCredential.from_dict({"domain": "qoder.com.cn"}).realm == "cn"
    assert QoderCredential.from_dict({}).realm == "cn"
    # 非法 auth_source 保持 unknown
    assert QoderCredential.from_dict({"auth_source": "hacked"}).auth_source == "unknown"
    # 驼峰键名兼容
    camel = QoderCredential.from_dict({"accessToken": "t", "userId": "u2",
                                       "refreshToken": "r2", "userType": "pro"})
    assert (camel.access_token, camel.uid, camel.refresh_token) == ("t", "u2", "r2")
    assert camel.user_type == "pro"
    assert QoderCredential.from_dict({"access_token": "t"}).user_type == (
        qoder_events.DEFAULT_USER_TYPE)

def test_credential_realm_config_and_needs_refresh():
    credential = cred(expires_at=int(time.time()) + 100)
    assert credential.realm_config().openapi.endswith("qoder.com.cn")
    assert credential.needs_refresh(3600) is True
    assert credential.needs_refresh(0) is False
    assert credential.needs_refresh(3600, now=int(time.time()) - 100000) is False

def test_needs_refresh_requires_oauth_and_refresh_token():
    assert cred(auth_source="manual").needs_refresh(10**9) is False
    assert cred(refresh_token="").needs_refresh(10**9) is False
    assert cred(expires_at=0, access_token="not-a-jwt").needs_refresh(10**9) is False

def test_token_expires_at_falls_back_to_jwt_exp():
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": 2000000000}).encode()).decode().rstrip("=")
    jwt = f"h.{payload}.s"
    assert QoderCredential(access_token=jwt).token_expires_at() == 2000000000
    # 到期时刻本身即进入刷新窗口（now == exp）；刷新只对 OAuth 凭证生效
    oauth = QoderCredential(access_token=jwt, auth_source="oauth",
                            refresh_token="drt")
    assert oauth.needs_refresh(0, now=2000000000) is True

def test_parse_credential_accepts_bytes_and_rejects_bad_input():
    parsed = parse_credential(b'{"token": "t", "user_id": "u"}')
    assert parsed.access_token == "t" and parsed.uid == "u"
    assert parsed.auth_source == "manual"
    oauth = parse_credential({"access_token": "t"}, auth_source="oauth")
    assert oauth.auth_source == "oauth"
    # 显式非法来源不被入口参数覆盖
    assert parse_credential({"access_token": "t",
                             "auth_source": "nope"}).auth_source == "unknown"
    with pytest.raises(UpstreamProtocolViolation, match="not valid JSON"):
        parse_credential(b"{not json")
    with pytest.raises(UpstreamProtocolViolation, match="not an object"):
        parse_credential(["nope"])  # type: ignore[arg-type]
    with pytest.raises(UpstreamProtocolViolation, match="missing access token"):
        parse_credential({"uid": "u"})

def test_device_expiry_forms():
    now = int(time.time())
    assert expires_from_device({"expires_in": 7_200_000}) == pytest.approx(now + 7200,
                                                                         abs=5)
    iso = expires_from_device({"expires_at": "2030-01-01T00:00:00Z"})
    assert iso > now
    # 带时区的 RFC3339 与小数秒
    assert expires_from_device({"expires_at": "2030-01-01T08:00:00+08:00"}) == iso
    assert expires_from_device({"expires_at": "not-a-date"}) == 0
    assert expires_from_device({}) == 0
    assert expires_from_device({"expires_in": 0}) == 0
    assert expires_from_device({"expires_at": 123}) == 0

def test_credential_from_device_prefers_userinfo_and_falls_back_uid():
    data = {"token": "dt-9", "refresh_token": "drt-9", "expires_in": 1000,
            "user_id": "from-device"}
    built = credential_from_device(data, realm="intl",
                                   userinfo={"id": "from-userinfo", "name": "N",
                                             "organization_id": "o"})
    assert built.uid == "from-userinfo" and built.nickname == "N"
    assert built.realm == "intl" and built.domain == "qoder.com"
    assert built.refresh_token == "drt-9" and built.expires_at > 0
    assert credential_from_device(data).uid == "from-device"
    assert credential_from_device({}).uid == ""
    assert credential_from_device({}, fallback_uid="fb").uid == "fb"

def test_merge_refreshed_keeps_identity():
    old = cred()
    merged = merge_refreshed(old, {"token": "dt-new"})
    assert merged.access_token == "dt-new"
    assert merged.uid == old.uid and merged.nickname == old.nickname
    assert merged.refresh_token == old.refresh_token
    assert merged.expires_at == old.expires_at
    # 响应不带 token 时保留旧值（不把凭证刷成空）
    kept = merge_refreshed(old, {"refresh_token": "drt-2"})
    assert kept.access_token == old.access_token
    assert kept.refresh_token == "drt-2"
    assert merge_refreshed(old, {"token": "x", "expires_in": 3_600_000}).expires_at > 0

def test_credential_key_prefers_uid():
    assert credential_key(cred()) == credential_key(cred(access_token="other"))
    assert credential_key(cred(uid="")) != credential_key(cred())

def test_credential_from_dict_rejects_non_dict():
    assert QoderCredential.from_dict(None).realm == "cn"      # type: ignore[arg-type]

def test_detect_realm_from_plain_qoder_com():
    assert qoder_events.detect_realm_from_domain("www.qoder.com") == "intl"


def test_web_cookie_roundtrip_and_aliases():
    """网页 Cookie 随凭证加密落库；导出/再导入与刷新都不丢它。"""
    raw = {"access_token": "t", "web_cookie": "qoder_session_cookie=abc"}
    assert QoderCredential.from_dict(raw).to_dict()["web_cookie"] == (
        "qoder_session_cookie=abc")
    # 兼容 cookie 别名
    assert QoderCredential.from_dict({"access_token": "t",
                                      "cookie": "sid=1"}).web_cookie == "sid=1"
    # 缺字段 = 空串（没有网页会话，额度探测走汇总接口）
    assert QoderCredential.from_dict({"access_token": "t"}).web_cookie == ""


def test_merge_refreshed_preserves_web_cookie():
    old = cred(web_cookie="qoder_session_cookie=abc")
    assert merge_refreshed(old, {"token": "dt-new"}).web_cookie == old.web_cookie
