"""CodeArts 凭证解析与合并。"""

from __future__ import annotations

import time

import pytest

from src.provider.codearts import events as codearts_events
from src.provider.codearts.credential import (
    CodeArtsCredential,
    _expiration_from_raw,
    _jwk_from_raw,
    merge_refreshed,
    parse_credentials,
)
from tests.provider.codearts._support import cred as _cred
from tests.provider.codearts._support import jwk as _jwk
from tests.provider.codearts._support import token_response as _token_response


def test_credential_expiry_and_needs_refresh():
    assert _cred(expiration=0).token_expires_at() == 0
    assert _cred(expiration=0).needs_refresh(3600) is False       # 未知 → 不猜
    # expiration<=0 直接短路（不落到时钟比较）
    assert _cred(expiration=-5).needs_refresh(3600) is False
    now = int(time.time())
    # 进入窗口（expiration <= now + skew）→ True；远离窗口 → False
    assert _cred(expiration=now + 5).needs_refresh(10) is True
    assert _cred(expiration=now + 100).needs_refresh(10) is False
    # 显式传 now：不依赖真实时钟
    assert _cred(expiration=1000).needs_refresh(10, now=995) is True
    assert _cred(expiration=1000).needs_refresh(10, now=1) is False

def test_credential_needs_refresh_caps_skew_at_sts_lifetime():
    """24h 的全局 skew 会被本渠道封顶，否则每轮都烧一张一次性票。

    STS 临时凭证只有 2h 寿命，skew ≫ 寿命会让 needs_refresh 恒为真；
    封顶后只在「离到期 ≤ 45min」时才轮转。封顶值必须**严格大于**预刷新轮询
    周期（默认 30min，见 REFRESH_INTERVAL_MINUTES）：窗口 ≤ 周期时轮询点会
    整轮落在窗口外，凭证拖到到期才刷（实测封顶 30min + 周期 60min 即为此故障）。
    """
    cap = CodeArtsCredential.refresh_skew_cap_seconds
    assert cap == 2700
    assert cap > 30 * 60          # 严格宽于默认轮询周期，留出余量而非贴边
    # skew 24h（86400s）被压到 45min：now=1000 时离到期 2701s 不刷、2700s 就刷
    assert _cred(expiration=1000 + 2701).needs_refresh(86400, now=1000) is False
    assert _cred(expiration=1000 + 2700).needs_refresh(86400, now=1000) is True
    # 封顶不影响小 skew（调用方传的值更小时原样生效）
    assert _cred(expiration=1000 + 10).needs_refresh(10, now=1000) is True
    assert _cred(expiration=1000 + 11).needs_refresh(10, now=1000) is False

def test_credential_to_dict_roundtrip():
    jwk = _jwk()
    credential = _cred(dpop_private_jwk=jwk, refresh_token="rt", code_verifier="v",
                       domain="d", domain_id="did", nickname="nick")
    data = credential.to_dict()
    restored = CodeArtsCredential.from_dict(data)
    assert restored.to_dict() == data
    assert restored.dpop_private_jwk == jwk

def test_credential_from_dict_accepts_camel_case_and_junk():
    credential = CodeArtsCredential.from_dict({
        "userName": "camel", "accessKeyId": "AKX", "secretAccessKey": "SKX",
        "securityToken": "TOKX", "refreshToken": "rtx", "clientId": "cid",
        "codeVerifier": "cv", "domainId": "did",
        "dpop_private_jwk": '{"kty":"EC","d":"x","n":1}',   # JSON 串，非字符串字段被滤掉
    })
    assert credential.user_name == "camel" and credential.access_key_id == "AKX"
    assert credential.client_id == "cid" and credential.code_verifier == "cv"
    assert credential.dpop_private_jwk == {"kty": "EC", "d": "x"}
    # 缺 client_id 回落默认值
    assert CodeArtsCredential.from_dict({}).client_id == codearts_events.CLIENT_ID

def test_credential_expiration_from_raw_all_forms():
    assert _expiration_from_raw(True) == 0
    assert _expiration_from_raw(1700000000) == 1700000000
    assert _expiration_from_raw(1_700_000_000_000) == 1_700_000_000   # 毫秒
    assert _expiration_from_raw("1700000000") == 1700000000
    iso = _expiration_from_raw("2025-01-02T03:04:05Z")
    assert iso > 1_700_000_000
    assert _expiration_from_raw("not-a-date") == 0
    assert _expiration_from_raw("   ") == 0
    assert _expiration_from_raw(None) == 0

def test_credential_jwk_from_raw_forms():
    assert _jwk_from_raw(None) == {}
    assert _jwk_from_raw('{"a":"1"}') == {"a": "1"}
    assert _jwk_from_raw("{not json") == {}
    assert _jwk_from_raw({"a": "1", "b": 2}) == {"a": "1"}

def test_credential_parse_credentials_new_and_legacy_envelopes():
    modern = parse_credentials(_token_response())
    assert modern.access_key_id == "AK2" and modern.security_token == "TOK2"
    assert modern.expiration == 1999999999
    legacy = parse_credentials({"credential": {
        "access": "AKL", "secret": "SKL", "securitytoken": "TOKL",
        "expires_at": 1999999998}})
    assert legacy.access_key_id == "AKL" and legacy.security_token == "TOKL"
    assert legacy.expiration == 1999999998

def test_credential_parse_credentials_rejects_missing():
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="not an object"):
        parse_credentials("nope")            # type: ignore[arg-type]
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="missing"):
        parse_credentials({"credentials": {"access_key_id": "AK"}})

def test_credential_merge_refreshed_rewrites_rotating_fields():
    old = _cred(refresh_token="rt-old", dpop_private_jwk={"kty": "EC", "x": "1"},
                client_id="cid", code_verifier="cv", domain="d")
    merged = merge_refreshed(old, {
        "credentials": {"access_key_id": "AKN", "secret_access_key": "SKN",
                        "security_token": "TOKN"},
        "user_id": "u9", "user_name": "new", "refresh_token": "rt-new",
    })
    assert merged.uid == "u9" and merged.user_name == "new"
    assert merged.refresh_token == "rt-new"                # 一次性 token 必须轮转
    assert merged.access_key_id == "AKN"
    # 上游没回传的绑定项沿用旧值
    assert merged.client_id == "cid" and merged.dpop_private_jwk == {"kty": "EC", "x": "1"}
    assert merged.code_verifier == "cv" and merged.domain == "d"
    # 刷新回包无 expiration → 沿用旧的
    assert merged.expiration == old.expiration


def test_credential_web_cookie_roundtrip_and_aliases():
    assert _cred(web_cookie="sid=1").to_dict()["web_cookie"] == "sid=1"
    assert CodeArtsCredential.from_dict({"webCookie": "a=1"}).web_cookie == "a=1"
    assert CodeArtsCredential.from_dict({"cookie": "b=2"}).web_cookie == "b=2"
    assert CodeArtsCredential.from_dict({}).web_cookie == ""


def test_merge_refreshed_preserves_web_cookie():
    old = _cred(web_cookie="sid=1", refresh_token="rt", dpop_private_jwk=_jwk())
    assert merge_refreshed(old, _token_response()).web_cookie == "sid=1"
