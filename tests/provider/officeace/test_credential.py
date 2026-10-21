"""officeAce 凭证：两套凭据的解析、Basic 头、刷新轮转。"""

from __future__ import annotations

import base64
import json

import pytest

from src.provider.base import UpstreamProtocolViolation
from src.provider.officeace.credential import (
    OfficeAceCredential,
    _jwk_from_raw,
    merge_refreshed,
    parse_tokens,
)
from src.provider.officeace.events import CLIENT_ID
from tests.provider.officeace._support import MOMENT
from tests.provider.officeace._support import cred as _cred
from tests.provider.officeace._support import token_response as _token_response


def test_from_dict_accepts_camel_case():
    credential = OfficeAceCredential.from_dict({
        "accountId": "a", "principalId": "p", "userName": "u",
        "modelAppKey": "K", "modelAppSecret": "S",
        "modelBaseUrl": "https://gw.test/v2/",
        "accessKeyId": "AK", "secretAccessKey": "SK", "securityToken": "T",
        "projectId": "pid", "refreshToken": "rt", "clientId": "cid",
    })
    assert credential.account_id == "a" and credential.principal_id == "p"
    assert credential.user_name == "u"
    assert credential.model_app_key == "K" and credential.model_app_secret == "S"
    # 尾斜杠被归一：拼端点时不会出现 `//v2/chat/...`
    assert credential.model_base_url == "https://gw.test/v2"
    assert credential.access_key_id == "AK"
    assert credential.client_id == "cid"


def test_from_dict_rejects_non_object():
    with pytest.raises(UpstreamProtocolViolation):
        OfficeAceCredential.from_dict("nope")  # type: ignore[arg-type]


def test_from_dict_defaults_client_id():
    assert OfficeAceCredential.from_dict({}).client_id == CLIENT_ID


def test_to_dict_roundtrip():
    original = _cred()
    again = OfficeAceCredential.from_dict(original.to_dict())
    assert again == original
    # 私有 JWK 必须是副本：外部改动不能穿透到凭证
    dumped = original.to_dict()
    dumped["dpop_private_jwk"]["d"] = "tampered"
    assert original.dpop_private_jwk["d"] != "tampered"


def test_authorization_is_basic_base64():
    credential = _cred(model_app_key="ak", model_app_secret="sk")
    expected = base64.b64encode(b"ak:sk").decode()
    assert credential.authorization == "Basic " + expected


def test_authorization_empty_without_app_key():
    assert _cred(model_app_key="").authorization == ""


def test_has_chat_and_control_plane():
    assert _cred().has_chat and _cred().has_control_plane
    assert not _cred(model_app_secret="").has_chat
    assert not _cred(access_key_id="").has_control_plane
    assert not _cred(secret_access_key="").has_control_plane


# ------------------------------------------------------------------ 刷新窗口

def test_needs_refresh_within_lead():
    credential = _cred(expiration=MOMENT + 1000)
    assert credential.needs_refresh(1800, now=MOMENT) is True
    assert credential.needs_refresh(600, now=MOMENT) is False


def test_needs_refresh_skew_capped():
    """全局 skew 默认 24h ≫ 临时凭证 2h 寿命：不封顶会每轮都烧一张一次性票。"""
    credential = _cred(expiration=MOMENT + 7200)
    assert credential.needs_refresh(86400, now=MOMENT) is False
    assert credential.needs_refresh(86400, now=MOMENT + 7200 - 2700) is True


def test_needs_refresh_requires_everything():
    assert _cred(expiration=0).needs_refresh(10**6, now=MOMENT) is False
    assert _cred(refresh_token="").needs_refresh(10**6, now=MOMENT) is False
    assert _cred(dpop_private_jwk={}).needs_refresh(10**6, now=MOMENT) is False


def test_needs_refresh_uses_wall_clock_by_default():
    assert _cred(expiration=1).needs_refresh(60) is True


def test_token_expires_at_is_expiration():
    assert _cred(expiration=123).token_expires_at() == 123


# ------------------------------------------------------------------ 到期归一

@pytest.mark.parametrize("raw, want", [
    (True, 0), (1700000000, 1700000000), (1700000000.9, 1700000000),
    ("1700000000", 1700000000), ("2026-10-21T00:00:00Z", 1792540800),
    ("nope", 0), ("", 0), (None, 0),
])
def test_expiration_from_raw(raw, want):
    credential = OfficeAceCredential.from_dict({"expiration": raw})
    assert credential.expiration == want


def test_expiration_milliseconds_are_normalized():
    """华为 STS 有时给毫秒；不归一会让凭证「已过期 50000 天」。"""
    credential = OfficeAceCredential.from_dict({"expiration": 1792546252000})
    assert credential.expiration == 1792546252


# ------------------------------------------------------------------ JWK

def test_jwk_accepts_dict_and_json_string():
    jwk = {"d": "x", "x": "y", "kty": "EC", "n": 1}
    assert _jwk_from_raw(jwk) == {"d": "x", "x": "y", "kty": "EC"}
    assert _jwk_from_raw(json.dumps({"d": "x"})) == {"d": "x"}


def test_jwk_tolerates_garbage():
    assert _jwk_from_raw(None) == {}
    assert _jwk_from_raw("{not json") == {}
    assert _jwk_from_raw([1]) == {}


# ------------------------------------------------------------------ 令牌响应

def test_parse_tokens_reads_nested_credentials():
    credential = parse_tokens(_token_response())
    assert credential.access_key_id == "AK2"
    assert credential.secret_access_key == "SK2"
    assert credential.security_token == "TOK2"
    assert credential.project_id == "pid2"
    assert credential.expiration == 1999999999
    assert credential.refresh_token == "rt2"
    assert credential.principal_id == "prin-2"


def test_parse_tokens_rejects_garbage():
    with pytest.raises(UpstreamProtocolViolation):
        parse_tokens("x")  # type: ignore[arg-type]
    with pytest.raises(UpstreamProtocolViolation):
        parse_tokens({"credentials": {"access_key_id": "AK"}})
    with pytest.raises(UpstreamProtocolViolation):
        parse_tokens({"credentials": "x"})


def test_parse_tokens_project_id_falls_back_to_root():
    credential = parse_tokens({
        "credentials": {"access_key_id": "AK", "secret_access_key": "SK"},
        "project_id": "root-pid",
    })
    assert credential.project_id == "root-pid"


def test_parse_tokens_reads_username_from_id_token():
    payload = base64.urlsafe_b64encode(
        json.dumps({"preferred_username": "alice"}).encode()).decode().rstrip("=")
    credential = parse_tokens({
        "credentials": {"access_key_id": "AK", "secret_access_key": "SK"},
        "id_token": f"h.{payload}.s",
    })
    assert credential.user_name == "alice"


def test_parse_tokens_keeps_user_name_from_response():
    """响应里已有 user_name 时不再从 id_token 覆盖（显式字段优先）。"""
    payload = base64.urlsafe_b64encode(
        json.dumps({"preferred_username": "from-token"}).encode()).decode().rstrip("=")
    credential = parse_tokens({
        "credentials": {"access_key_id": "AK", "secret_access_key": "SK"},
        "user_name": "explicit", "id_token": f"h.{payload}.s",
    })
    assert credential.user_name == "explicit"


def test_id_token_name_falls_back_to_name_claim():
    payload = base64.urlsafe_b64encode(
        json.dumps({"name": "Bob"}).encode()).decode().rstrip("=")
    assert parse_tokens({
        "credentials": {"access_key_id": "AK", "secret_access_key": "SK"},
        "id_token": f"h.{payload}.s",
    }).user_name == "Bob"


@pytest.mark.parametrize("token", [
    "", "nodots", "h.!!!.s",
    "h." + base64.urlsafe_b64encode(b'{"sub":"x"}').decode().rstrip("=") + ".s",
    "h." + base64.urlsafe_b64encode(b"[1]").decode().rstrip("=") + ".s",
])
def test_id_token_name_tolerates_bad_tokens(token):
    credential = parse_tokens({
        "credentials": {"access_key_id": "AK", "secret_access_key": "SK"},
        "id_token": token,
    })
    assert credential.user_name == ""


# ------------------------------------------------------------------ 刷新合并

def test_merge_refreshed_keeps_chat_credentials():
    """Basic app key 不过期：刷新绝不能把它换掉/清空。"""
    old = _cred()
    new = merge_refreshed(old, _token_response())
    assert new.model_app_key == old.model_app_key
    assert new.model_app_secret == old.model_app_secret
    assert new.model_base_url == old.model_base_url


def test_merge_refreshed_rotates_refresh_token():
    old = _cred(refresh_token="old-rt")
    new = merge_refreshed(old, _token_response())
    assert new.refresh_token == "rt2"


def test_merge_refreshed_keeps_binding_fields():
    """client_id 与 DPoP 私钥是 refresh_token 的绑定项，必须沿用旧值。"""
    old = _cred(client_id="cid")
    new = merge_refreshed(old, _token_response())
    assert new.client_id == "cid"
    assert new.dpop_private_jwk == old.dpop_private_jwk


def test_merge_refreshed_falls_back_to_old_when_missing():
    old = _cred()
    new = merge_refreshed(old, {"credentials": {
        "access_key_id": "AK3", "secret_access_key": "SK3"}})
    assert new.refresh_token == old.refresh_token
    assert new.project_id == old.project_id
    assert new.expiration == old.expiration
    assert new.account_id == old.account_id
    assert new.nickname == old.nickname


def test_merge_refreshed_takes_new_identity_when_present():
    old = _cred(account_id="old", user_name="")
    new = merge_refreshed(old, dict(_token_response(), user_id="prin-2"))
    assert new.principal_id == "prin-2"
    assert new.account_id == "old"
