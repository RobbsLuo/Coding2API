"""CodeArts（华为云码道）渠道测试：签名、DPoP、登录闭环、凭证、客户端、Provider。

覆盖硬门槛按 AGENTS.md：新增行/分支必须测到（不靠 pragma 达标）。全用
`httpx.MockTransport` 注入，不连真上游、不用真凭证；`Settings` 一律显式传值
（本文件不构造 Settings，因 Provider 层不依赖 config）。

四块关注点：
* `signer.py`：SDK-HMAC-SHA256 的逐项构造（固定向量可复现）。
* `dpop.py`：JWS 结构与**低 S 归一化**（华为 STS 的硬要求）。
* `auth.py` / `credential.py`：PKCE 登录闭环与凭证归一。
* `client.py` / `CodeArtsProvider`：SSE 逐行解析、模型/额度/刷新链路。
"""

from __future__ import annotations

import hashlib
import json
import time

import httpx
import pytest

from src.provider.base import ErrKind, Event, EventKind, Usage
from src.provider.codearts import CodeArtsProvider, credential_key, dpop, signer
from src.provider.codearts import auth as codearts_auth
from src.provider.codearts import events as codearts_events
from src.provider.codearts.client import (
    BENEFIT_SEED,
    CodeArtsClient,
    UpstreamHTTPError,
    _credit_rate,
    _fill_estimated_credit,
    _flatten_content,
    _legacy_blocks,
    _models_from_items,
    _parse_ratio,
    chat_headers,
    derive_chat_id,
    new_chat_id,
    parse_balance,
    prepare_body,
    request_path,
    sdk_date,
    short_headers,
)
from src.provider.codearts.credential import (
    CodeArtsCredential,
    _expiration_from_raw,
    _jwk_from_raw,
    merge_refreshed,
    parse_credentials,
)

# ------------------------------------------------------------------ 夹具/助手


def _cred(**over) -> CodeArtsCredential:
    base = {
        "uid": "u1", "user_name": "alice",
        "access_key_id": "AK", "secret_access_key": "SK", "security_token": "TOK",
        "expiration": int(time.time()) + 7200,
    }
    base.update(over)
    return CodeArtsCredential.from_dict(base)


def _jwk() -> dict[str, str]:
    return dpop.new_private_jwk()


def _client(handler, **kw) -> CodeArtsClient:
    transport = httpx.MockTransport(handler)
    kw.setdefault("endpoint", "https://snap.test")
    kw.setdefault("benefit_host", "https://gw.test")
    kw.setdefault("sts_host", "https://sts.test")
    return CodeArtsClient(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None), **kw)


def _sse(*payloads: object) -> str:
    """CodeArts SSE：逐行 `data:`（真实流每行后有空行，逐行读取两者皆可）。"""
    return "".join(f"data: {json.dumps(p)}\n" for p in payloads)


# ================================================================== signer.py


def test_signer_canonical_uri_encodes_segments_and_appends_slash():
    assert signer.canonical_uri("/v1/model/builtin") == "/v1/model/builtin/"
    # 已带尾斜杠：不重复补
    assert signer.canonical_uri("/v1/model/builtin/") == "/v1/model/builtin/"
    assert signer.canonical_uri("/") == "/"
    # 每段独立 percent-encode，分隔符自身不被编码
    assert signer.canonical_uri("/a b/c+d") == "/a%20b/c%2Bd/"


def test_signer_canonical_query_sorts_and_skips_blank():
    assert signer.canonical_query("") == ""
    assert signer.canonical_query("b=2&a=1") == "a=1&b=2"
    # 空段（连续 &&）跳过；同 key 多值也排序
    assert signer.canonical_query("a=2&&a=1") == "a=1&a=2"
    # key/value 都 quote
    assert signer.canonical_query("k=a b") == "k=a%20b"


def test_signer_canonical_headers_lowercases_and_sorts_all_headers():
    canonical, signed = signer.canonical_headers(
        {"X-B": " 2 ", "A": "1", "Content-Type": "application/json"})
    assert canonical == "a:1\ncontent-type:application/json\nx-b:2\n"
    assert signed == "a;content-type;x-b"


def test_signer_fixed_vector_canonical_request_and_string_to_sign():
    payload = b'{"a":1}'
    payload_hash = signer.sha256_hex(payload)
    assert payload_hash == (
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862")
    headers = {
        "Content-Type": "application/json", "X-Language": "zh-cn",
        "X-Sdk-Date": "20250102T030405Z", "X-Security-Token": "TOKEN123",
        "X-Sdk-Content-Sha256": payload_hash,
    }
    request, signed = signer.canonical_request(
        "POST", "/v1/model/builtin", "b=2&a=1", headers, payload_hash)
    assert signed == ("content-type;x-language;x-sdk-content-sha256;"
                      "x-sdk-date;x-security-token")
    assert request == (
        "POST\n"
        "/v1/model/builtin/\n"
        "a=1&b=2\n"
        "content-type:application/json\n"
        "x-language:zh-cn\n"
        f"x-sdk-content-sha256:{payload_hash}\n"
        "x-sdk-date:20250102T030405Z\n"
        "x-security-token:TOKEN123\n"
        "\n"
        f"{signed}\n"
        f"{payload_hash}")
    assert signer.string_to_sign(request, "20250102T030405Z") == (
        "SDK-HMAC-SHA256\n"
        "20250102T030405Z\n"
        + signer.sha256_hex(request.encode("utf-8")))


def test_signer_sign_headers_full_flow_with_token_and_trace():
    credential = signer.SignCredential("AKIDEXAMPLE", "SECRETEXAMPLE", "TOKEN123")
    source_headers = {"Content-Type": "application/json", "X-Language": "zh-cn"}
    out = signer.sign_headers(
        credential, method="POST", path="/v1/model/builtin", raw_query="b=2&a=1",
        headers=source_headers, payload=b'{"a":1}', x_sdk_date="20250102T030405Z",
        trace_id="trace-1")
    # 全部头都进 SignedHeaders（含 trace_id 与补入的签名头）
    assert out["X-Sdk-Content-Sha256"] == signer.sha256_hex(b'{"a":1}')
    assert out["X-Security-Token"] == "TOKEN123"
    assert out["x-snap-traceid"] == "trace-1"
    assert out["Authorization"].startswith("SDK-HMAC-SHA256 Access=AKIDEXAMPLE, ")
    signed = out["Authorization"].split("SignedHeaders=")[1].split(",")[0]
    assert signed == ("content-type;x-language;x-sdk-content-sha256;"
                      "x-sdk-date;x-security-token;x-snap-traceid")
    # 入参不被就地修改（返回合并后的新 dict）
    assert source_headers == {"Content-Type": "application/json",
                              "X-Language": "zh-cn"}


def test_signer_sign_headers_empty_security_token_omits_header():
    credential = signer.SignCredential("AK", "SK", "")
    out = signer.sign_headers(
        credential, method="GET", path="/x", headers={}, payload=b"",
        x_sdk_date="20250102T030405Z")
    assert "X-Security-Token" not in out
    assert "x-snap-traceid" not in out
    assert signer.hmac_sha256_hex("k", "m") == signer.hmac_sha256_hex("k", "m")


# =================================================================== dpop.py


def test_dpop_private_jwk_roundtrip_and_public_strip():
    jwk = _jwk()
    assert set(jwk) == {"kty", "crv", "x", "y", "d"}
    key = dpop.private_key_from_jwk(jwk)
    assert key.private_numbers().private_value == int.from_bytes(
        dpop._b64url_decode(jwk["d"]), "big")
    public = dpop.public_jwk(jwk)
    assert set(public) == {"kty", "crv", "x", "y"}
    assert public["x"] == jwk["x"]


def test_dpop_private_key_from_jwk_rejects_malformed():
    with pytest.raises(ValueError, match="curve"):
        dpop.private_key_from_jwk({"kty": "RSA", "crv": "P-256", "d": "AA"})
    with pytest.raises(ValueError, match="JWK d"):
        dpop.private_key_from_jwk({"kty": "EC", "crv": "P-256"})
    zero = dpop._b64url(b"\x00" * 32)
    with pytest.raises(ValueError, match="JWK d"):
        dpop.private_key_from_jwk({"kty": "EC", "crv": "P-256", "d": zero})


def test_dpop_normalize_low_s_both_branches():
    n = dpop.P256_ORDER
    half = n // 2
    assert dpop.normalize_low_s(1) == 1
    assert dpop.normalize_low_s(half) == half
    assert dpop.normalize_low_s(half + 1) == n - (half + 1)
    assert dpop.normalize_low_s(n - 1) == 1


def test_dpop_sign_proof_structure_and_random_defaults():
    jwk = _jwk()
    proof = dpop.sign_proof(jwk, "https://sts.test/v1/oauth2/tokens")
    parts = proof.split(".")
    assert len(parts) == 3
    header = dpop.decode_segment(parts[0])
    assert header["alg"] == "ES256" and header["typ"] == "dpop+jwt"
    assert header["jwk"] == dpop.public_jwk(jwk)          # 头里只有公钥
    payload = dpop.decode_segment(parts[1])
    assert payload["htm"] == "POST"
    assert payload["htu"] == "https://sts.test/v1/oauth2/tokens"
    assert payload["jti"] and len(payload["jti"]) == 32   # 默认随机 jti
    assert dpop.verify_proof(
        proof, dpop.public_key_from_jwk(jwk)) is True


def test_dpop_sign_proof_injectable_iat_and_jti():
    jwk = _jwk()
    proof = dpop.sign_proof(jwk, "https://x", htm="POST", issued_at=1234, jti="fixed")
    payload = dpop.decode_segment(proof.split(".")[1])
    assert payload["iat"] == 1234 and payload["jti"] == "fixed"


def test_dpop_sign_proof_always_low_s():
    jwk = _jwk()
    half = dpop.P256_ORDER // 2
    for _ in range(50):
        signature = dpop._b64url_decode(
            dpop.sign_proof(jwk, "https://x").split(".")[2])
        s = int.from_bytes(signature[32:], "big")
        assert s <= half


def test_dpop_sign_proof_forces_high_s_normalization(monkeypatch):
    """强制 ECDSA 返回 high-S，断言编码前被归一化（华为 STS 的硬要求）。"""
    jwk = _jwk()
    high_s = dpop.P256_ORDER - 5                    # > n/2，属 high-S
    monkeypatch.setattr(dpop, "decode_dss_signature", lambda der: (1234, high_s))
    proof = dpop.sign_proof(jwk, "https://x", issued_at=1, jti="fixed")
    signature = dpop._b64url_decode(proof.split(".")[2])
    assert int.from_bytes(signature[:32], "big") == 1234
    assert int.from_bytes(signature[32:], "big") == 5      # n - (n - 5)
    assert int.from_bytes(signature[32:], "big") <= dpop.P256_ORDER // 2


def test_dpop_verify_proof_rejects_malformed_and_tampered():
    jwk = _jwk()
    proof = dpop.sign_proof(jwk, "https://x", issued_at=1, jti="j")
    public = dpop.public_key_from_jwk(jwk)
    assert dpop.verify_proof("a.b", public) is False            # 段数不对
    # 段数为 3 但签名段 base64 非法（"a" 的字符数 mod 4 == 1）
    assert dpop.verify_proof("a.b.a", public) is False
    assert dpop.verify_proof("a.b." + dpop._b64url(b"1234"), public) is False
    head, payload, signature = proof.split(".")
    raw = bytearray(dpop._b64url_decode(signature))
    raw[-1] ^= 0xFF
    tampered = f"{head}.{payload}.{dpop._b64url(bytes(raw))}"
    assert dpop.verify_proof(tampered, public) is False


def test_dpop_decode_segment_rejects_non_object():
    with pytest.raises(ValueError, match="not an object"):
        dpop.decode_segment(dpop._b64url(json.dumps([1, 2]).encode()))


# =================================================================== auth.py


def test_auth_new_ticket_identity_is_hex32():
    ticket, secret = codearts_auth.new_ticket_identity()
    assert len(ticket) == 32 and len(secret) == 32
    int(ticket, 16) and int(secret, 16)          # 合法十六进制
    assert (ticket, secret) != codearts_auth.new_ticket_identity()


def test_auth_pkce_pair_generates_and_accepts_verifier():
    verifier, challenge = codearts_auth.pkce_pair()
    assert len(verifier) >= 43 and challenge
    fixed_verifier, fixed_challenge = codearts_auth.pkce_pair("abc")
    assert fixed_verifier == "abc"
    expected = codearts_auth.base64.urlsafe_b64encode(
        hashlib.sha256(b"abc").digest()).rstrip(b"=").decode()
    assert fixed_challenge == expected


def _login_config() -> codearts_auth.LoginConfig:
    return codearts_auth.LoginConfig(
        portal_host="https://portal.test",
        snap_manager="https://snap.test/snap-manager",
        sts_host="https://sts.test")


def test_auth_build_authorize_url_has_full_parameter_set():
    config = _login_config()
    url = codearts_auth.build_authorize_url(
        config, ticket_id="t" * 32, code_challenge="ch", port=8123)
    assert url.startswith("https://portal.test/authorize?")
    query = dict(httpx.URL(url).params)
    assert query == {
        "theme": "2", "locale": "zh-cn",
        "uri_scheme": config.client_id, "client_id": config.client_id,
        "port": "8123", "code_challenge": "ch",
        # portal 只认 SHA-256，"S256" 会被拒
        "code_challenge_method": "SHA-256", "ticket_id": "t" * 32,
        "plugin-name": codearts_auth.PLUGIN_NAME,
        "plugin-version": codearts_auth.PLUGIN_VERSION,
    }


def test_auth_new_login_session_bundles_state_and_dpop_key():
    session = codearts_auth.new_login_session(_login_config(), port=9000)
    assert len(session.ticket_id) == 32 and len(session.secret) == 32
    verifier, challenge = codearts_auth.pkce_pair(session.code_verifier)
    assert challenge == session.code_challenge
    assert session.auth_url.startswith("https://portal.test/authorize?")
    assert "ticket_id=" + session.ticket_id in session.auth_url
    # DPoP 私钥随会话生成，必须与 refresh_token 一起落盘
    assert session.dpop_private_jwk["kty"] == "EC"
    assert codearts_auth.redirect_uri(_login_config(), 9000) == (
        "http://127.0.0.1:9000/oauth/callback")


def _token_response(**over) -> dict:
    payload = {
        "credentials": {"access_key_id": "AK2", "secret_access_key": "SK2",
                        "security_token": "TOK2", "expiration": 1999999999},
        "refresh_token": "rt2", "user_id": "u2", "user_name": "bob",
    }
    payload.update(over)
    return payload


async def test_auth_exchange_code_reuses_or_generates_dpop_key():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == codearts_events.EP_OAUTH_TOKENS
        assert request.headers["DPoP"]           # 必带 DPoP 证明
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json=_token_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        jwk = _jwk()
        result = await codearts_auth.exchange_code(
            http, _login_config(), code="c", code_verifier="v", port=1234,
            dpop_private_jwk=jwk)
        assert result.dpop_private_jwk == jwk         # 复用注入的私钥
        assert seen[-1]["grant_type"] == "authorization_code"
        assert seen[-1]["redirect_uri"] == "http://127.0.0.1:1234/oauth/callback"

        generated = await codearts_auth.exchange_code(
            http, _login_config(), code="c", code_verifier="v", port=1234)
        assert generated.dpop_private_jwk["kty"] == "EC"  # 未提供则新生成


async def test_auth_post_form_error_and_non_json():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"bad request")

    transport = httpx.MockTransport(http_error)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(codearts_auth.TokenEndpointError) as caught:
            await codearts_auth.exchange_code(
                http, _login_config(), code="c", code_verifier="v", port=1)
        assert caught.value.status == 400 and caught.value.body == b"bad request"

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    transport = httpx.MockTransport(non_json)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(codearts_events.UpstreamProtocolViolation):
            await codearts_auth.exchange_code(
                http, _login_config(), code="c", code_verifier="v", port=1)


async def test_auth_refresh_tokens_requires_token_and_key():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(codearts_events.UpstreamProtocolViolation,
                           match="refresh_token"):
            await codearts_auth.refresh_tokens(
                http, _login_config(), refresh_token="", dpop_private_jwk=_jwk())
        with pytest.raises(codearts_events.UpstreamProtocolViolation,
                           match="dpop_private_jwk"):
            await codearts_auth.refresh_tokens(
                http, _login_config(), refresh_token="rt", dpop_private_jwk={})

        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json=_token_response())

        transport2 = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport2) as http:
        out = await codearts_auth.refresh_tokens(
            http, _login_config(), refresh_token="rt", dpop_private_jwk=_jwk(),
            code_verifier="v")
        assert out["refresh_token"] == "rt2"
        assert seen[-1]["grant_type"] == "refresh_token"


async def test_auth_poll_ticket_empty_and_full():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/snap-manager" + codearts_events.EP_LOGIN_TICKET
        ticket = request.url.params["ticket_id"]
        if ticket == "waiting":
            return httpx.Response(200, json={"status": "not_logged_in"})
        if ticket == "legacy":
            return httpx.Response(200, json={
                "credential": {"access": "AK", "secret": "SK",
                               "securitytoken": "TOKS"}})
        return httpx.Response(200, json=_token_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        assert await codearts_auth.poll_ticket(
            http, _login_config(), ticket_id="waiting", secret="s") == {}
        legacy = await codearts_auth.poll_ticket(
            http, _login_config(), ticket_id="legacy", secret="s")
        assert legacy["credential"]["securitytoken"] == "TOKS"
        full = await codearts_auth.poll_ticket(
            http, _login_config(), ticket_id="ok", secret="s")
        assert full["refresh_token"] == "rt2"


async def test_auth_poll_ticket_error_and_json_violations():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    transport = httpx.MockTransport(http_error)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(codearts_auth.TokenEndpointError):
            await codearts_auth.poll_ticket(
                http, _login_config(), ticket_id="t", secret="s")

    def non_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    transport = httpx.MockTransport(non_object)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(codearts_events.UpstreamProtocolViolation):
            await codearts_auth.poll_ticket(
                http, _login_config(), ticket_id="t", secret="s")


def test_auth_credential_data_from_tokens_and_dump_jwk():
    jwk = _jwk()
    data = codearts_auth.credential_data_from_tokens(_token_response(), jwk)
    assert data["access_key_id"] == "AK2" and data["refresh_token"] == "rt2"
    assert data["dpop_private_jwk"] == jwk
    # nickname 缺省回落 user_name
    assert data["nickname"] == "bob"
    named = codearts_auth.credential_data_from_tokens(
        _token_response(nickname="N"), jwk)
    assert named["nickname"] == "N"
    assert codearts_auth.dump_jwk({"b": "2", "a": "1"}) == '{"a":"1","b":"2"}'


# =============================================================== credential.py


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


# =================================================================== client.py


def test_client_sdk_date_and_chat_id():
    assert sdk_date(0) == "19700101T000000Z"
    assert len(sdk_date()) == 16 and sdk_date().endswith("Z")
    assert len(new_chat_id()) == 32
    int(new_chat_id(), 16)


def test_client_derive_chat_id_stable_and_fallback():
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    assert derive_chat_id(payload) == derive_chat_id(payload)
    assert len(derive_chat_id(payload)) == 32
    cyclic: list = []
    cyclic.append(cyclic)                              # 不可序列化 → 退回随机
    assert len(derive_chat_id({"messages": cyclic})) == 32


def test_client_request_path_and_headers():
    assert request_path("https://h/v1/model/builtin?b=2&a=1") == (
        "/v1/model/builtin", "b=2&a=1")
    assert request_path("") == ("/", "")
    assert chat_headers(_cred(), trace_id="t")["x-auth-token"] == "TOK"
    benefit = chat_headers(_cred(), trace_id="t", benefit=True)
    assert benefit[codearts_events.HEADER_MAAS_TYPE] == codearts_events.MAAS_BENEFIT
    assert codearts_events.HEADER_MAAS_TYPE not in chat_headers(_cred(), trace_id="t")
    assert short_headers("PromptCenter")["Agent-Type"] == "PromptCenter"
    assert "Agent-Type" not in short_headers()


def test_client_prepare_body_v2_and_legacy():
    messages = [{"role": "user", "content": "hi"}]
    body = prepare_body({"messages": messages, "stream": False, "temperature": 0.5,
                         "unknown": 1}, "m", chat_id="c")
    assert body["stream"] is True and body["model"] == "m"
    assert body["chat_id"] == "c" and body["prompt_cache_key"] == "c"
    assert body["tool_stream"] is True and body["temperature"] == 0.5
    assert "unknown" not in body
    body["messages"][0]["content"] = "changed"          # 深拷贝
    assert messages[0]["content"] == "hi"

    legacy = prepare_body({"messages": messages}, "m", chat_id="c", legacy=True,
                          user_id="u")
    assert legacy["task"] == "chat" and legacy["user_id"] == "u"
    assert legacy["messages"][0] == {"type": "text", "text": "hi"}
    assert "user_id" not in prepare_body({"messages": []}, "m", chat_id="c",
                                         legacy=True)


def test_client_legacy_blocks_and_flatten():
    assert _legacy_blocks("nope") == []
    blocks = _legacy_blocks([
        "junk",
        {"role": "assistant", "content": [{"text": "a"}, {"text": "b"}, "x"]},
        {"role": "user", "content": ""},
        {"role": "user", "content": "hi"},
    ])
    assert blocks == [{"type": "text", "text": "assistant: ab"},
                      {"type": "text", "text": "hi"}]
    assert _flatten_content("s") == "s"
    assert _flatten_content(1) == ""
    assert _flatten_content([None, {"text": 2}, {"text": "z"}]) == "z"
    assert _flatten_content([{"text": ""}]) == ""
    assert _flatten_content(None) == ""


def test_client_models_from_items_and_helpers():
    assert _models_from_items("nope", benefit=False) == []
    # 内置：credit[].ratio_display → credit_rate；福利：不带 credit → None。
    models = _models_from_items([
        "junk", {},
        {"model_id": "a", "model_name": "A", "contextWindow": 8000, "maxTokens": 100,
         "credit": [{"ratio_display": "0.7x", "ratio": "0.05"}]},
        {"modelId": "b", "display_name": "B", "max_input_tokens": 900},
    ], benefit=False)
    assert [m.id for m in models] == ["a", "b"]
    assert models[0].credit_rate == 0.7 and models[0].max_input_tokens == 8000
    assert models[0].max_output_tokens == 100
    assert models[1].credit_rate is None          # 无 credit → 不猜
    benefit_models = _models_from_items([
        {"model_id": "c", "name": "C", "credit": [{"ratio_display": "0.7x"}]}],
        benefit=True)
    assert benefit_models[0].credit_rate is None  # 福利走每日 token 池，不打倍率
    none_models = _models_from_items([
        {"model_id": "c", "name": 5, "context_window": True, "max_tokens": -1}],
        benefit=False)
    assert none_models[0].name == "c" and none_models[0].credit_rate is None
    assert none_models[0].max_input_tokens is None
    assert none_models[0].max_output_tokens is None


def test_client_credit_rate_and_ratio_parsing():
    # 多档取首条；缺 ratio_display 时继续找下一个可解析项
    assert _credit_rate([{"ratio": "0.05"}, {"ratio_display": "0.32x"}]) == 0.32
    assert _credit_rate([{"ratio_display": "0.7x"}]) == 0.7
    assert _credit_rate([1]) is None              # 非 dict 档位跳过
    assert _credit_rate("nope") is None
    assert _credit_rate([]) is None
    # 纯数字 / 无 x 后缀 / 布尔 / 非数字串 / 非法小数
    assert _parse_ratio("0.7") == 0.7
    assert _parse_ratio(0.5) == 0.5
    assert _parse_ratio("1.25x") == 1.25
    assert _parse_ratio(True) is None
    assert _parse_ratio("x0.7") is None
    assert _parse_ratio("0.7.1") is None
    assert _parse_ratio(None) is None


def test_client_fill_estimated_credit_benefit_and_guards():
    # 福利模型：输入 + 输出 token = 每日池消耗，标推算值（上游不给该字段）
    event = Event(kind=EventKind.USAGE, usage=Usage(input_tokens=32, output_tokens=694))
    _fill_estimated_credit(event, benefit=True)
    assert event.usage.credit == 726 and event.usage.credit_estimated is True
    # 上游将来真回传 credit → 不覆盖
    upstream = Event(kind=EventKind.USAGE,
                     usage=Usage(input_tokens=1, output_tokens=2, credit=9.5))
    _fill_estimated_credit(upstream, benefit=True)
    assert upstream.usage.credit == 9.5 and upstream.usage.credit_estimated is False
    # 内置模型不扣每日池 → 不补
    builtin = Event(kind=EventKind.USAGE, usage=Usage(input_tokens=1, output_tokens=2))
    _fill_estimated_credit(builtin, benefit=False)
    assert builtin.usage.credit is None and builtin.usage.credit_estimated is False
    # 非 USAGE 事件 / 无 usage / 两个 token 都缺 → 都不猜
    content = Event(kind=EventKind.CONTENT, content="x")
    _fill_estimated_credit(content, benefit=True)
    assert content.usage is None
    _fill_estimated_credit(Event(kind=EventKind.USAGE), benefit=True)
    empty = Event(kind=EventKind.USAGE, usage=Usage())
    _fill_estimated_credit(empty, benefit=True)
    assert empty.usage.credit is None
    # 只给一半 token 也照算（另一侧按 0）
    half = Event(kind=EventKind.USAGE, usage=Usage(output_tokens=5))
    _fill_estimated_credit(half, benefit=True)
    assert half.usage.credit == 5 and half.usage.credit_estimated is True


def test_client_parse_balance_variants():
    assert parse_balance({}).probe_failed is True
    direct = parse_balance({"total": 100, "used": 30})
    assert direct.remaining == 70 and direct.total == 100
    envelope = parse_balance({"result": {"remaining": 5, "total": 10,
                                         "cycle_end": 1700000000}})
    assert envelope.remaining == 5 and envelope.cycle_end == 1700000000
    # bool 不算数字；残缺键回落下一个候选
    tolerant = parse_balance({"remaining": True, "balance": 3, "cycle_end": -1,
                              "expire_time": 1700000001})
    assert tolerant.remaining == 3 and tolerant.cycle_end == 1700000001
    assert parse_balance({"quota": 0}).probe_failed is False
    # 真实每日池形状（2026-09-30 实测）：按 daily_token_limit 算当日剩余
    noon = 1_780_300_800  # 任意时刻；到期点断言不依赖具体时区
    daily = parse_balance({"result": {
        "total_quota": 10000000, "total_balance": 9998868, "used_amount": 1132,
        "daily_token_limit": 10000000, "daily_tokens_used": 1132,
        "monthly_token_limit": 0, "expire_time": 0}}, now=noon)
    assert daily.total == 10000000 and daily.remaining == 9998868
    assert daily.probe_failed is False
    assert daily.probed_at == noon
    # 到期点＝下一个本地 0 点，当日剩余进到期阶梯 → 调度器「快过期的先用」优先消耗
    midnight = time.localtime(daily.cycle_end)
    assert (midnight.tm_hour, midnight.tm_min, midnight.tm_sec) == (0, 0, 0)
    assert noon < daily.cycle_end <= noon + 86400
    assert daily.expiry_ladder == [(daily.cycle_end, 9998868)]
    # 当日用尽 → 剩余 0（不是负数），健康度会判「已耗尽」；空池不进到期排序
    drained = parse_balance({"daily_token_limit": 100,
                             "daily_tokens_used": 250}, now=noon)
    assert drained.remaining == 0 and drained.total == 100
    assert drained.cycle_end == daily.cycle_end and drained.expiry_ladder is None
    # daily_token_limit 为 0/缺失时退化到通用键（含新补的 total_balance/used_amount）
    generic = parse_balance({"total_quota": 500, "used_amount": 40}, now=noon)
    assert generic.total == 500 and generic.remaining == 460
    # 通用形状没有可靠的每日重置时间 → 不登记到期阶梯，也不伪造 cycle_end
    assert generic.expiry_ladder is None and generic.cycle_end is None
    fallback = parse_balance({"result": {"total_balance": 900, "total_quota": 1000}})
    assert fallback.total == 1000 and fallback.remaining == 900
    # daily_tokens_used 缺失时按 0 处理 → 剩余等于额度
    assert parse_balance({"daily_token_limit": 10},
                         now=noon).remaining == 10


def test_client_upstream_error_kind():
    error = UpstreamHTTPError(401, b"")
    assert error.kind() is ErrKind.DEAD


def test_client_defaults_and_chat_url():
    client = CodeArtsClient(endpoint="https://snap.test/", sts_host="https://sts.test/")
    assert client.endpoint == "https://snap.test"
    assert client.sts_host == "https://sts.test"
    assert client.chat_url() == "https://snap.test" + codearts_events.EP_CHAT_V2
    assert client.is_benefit_model(_cred(), "deepseek-v4-flash-0731") is True
    assert client.is_benefit_model(_cred(), "unknown-model") is False
    legacy = CodeArtsClient(endpoint="https://snap.test", legacy=True,
                            login=codearts_auth.LoginConfig())
    assert legacy.chat_url() == "https://snap.test" + codearts_events.EP_CHAT
    assert legacy.login.client_id == codearts_events.CLIENT_ID


async def test_client_lazy_pools_and_aclose():
    client = CodeArtsClient(endpoint="https://snap.test")
    assert client._stream() is client._stream()          # noqa: SLF001
    assert client._short() is client._short()            # noqa: SLF001
    await client.aclose()
    await CodeArtsClient().aclose()                      # 两侧都没创建 → 不报错


async def test_client_stream_chat_openai_chunks_and_done():
    """v2 实测形状：标准 OpenAI chunk + `data:[DONE]`。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == codearts_events.EP_CHAT_V2
        assert request.url.host == "snap.test"
        body = json.loads(request.content)
        assert body["stream"] is True and body["messages"][0]["content"] == "x"
        assert body["tool_stream"] is True
        assert request.headers["Authorization"].startswith("SDK-HMAC-SHA256 ")
        assert codearts_events.HEADER_MAAS_TYPE not in request.headers
        return httpx.Response(200, text=_sse(
            {"choices": [{"index": 0, "delta": {"role": "assistant",
                                                "reasoning_content": ""},
                           "finish_reason": None}], "usage": None},
            {"choices": [{"index": 0, "delta": {"content": "",
                                                "reasoning_content": "想"},
                           "finish_reason": None}], "usage": None},
            {"choices": [{"index": 0, "delta": {"content": "好"},
                           "finish_reason": None}], "usage": None},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": None},
            {"choices": [], "usage": {"prompt_tokens": 35, "completion_tokens": 45}},
        ) + "data:[DONE]\n")

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]}, "m")]
    assert [e.content for e in events if e.kind is EventKind.CONTENT] == ["好"]
    assert [e.content for e in events if e.kind is EventKind.REASONING] == ["想"]
    assert events[-2].kind is EventKind.USAGE
    assert events[-2].usage.input_tokens == 35
    assert events[-2].usage.output_tokens == 45


async def test_client_stream_chat_snapshot_semantics_and_done():
    """旧形状（累计 `text`）仍兼容：快照做差 + `{"text":"[DONE]"}` 结束。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            {"text": "He"},                                  # 增量 He
            {"text": "Hello"},                               # 前缀 → 增量 llo
            {"text": "Hello"},                               # 无新内容 → 不产事件
            {"text": "Hi"},                                  # 非前缀 → 整段重发
            {"text": "[DONE]", "error_code": "0"},           # 正常结束
        ))

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]}, "m")]
    assert [e.content for e in events if e.kind is EventKind.CONTENT] == [
        "He", "llo", "Hi"]
    assert [e.kind for e in events][-1] is EventKind.FINISH


async def test_client_stream_chat_benefit_header_and_embedded_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers[codearts_events.HEADER_MAAS_TYPE] == (
            codearts_events.MAAS_BENEFIT)
        return httpx.Response(200, text=_sse(
            {"text": "[DONE]", "error_code": "ChatAgent.00001001",
             "error_msg": "busy"}))

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": []}, "deepseek-v4-flash-0731")]
    assert len(events) == 1 and events[0].kind is EventKind.ERROR
    assert events[0].error_message == "ChatAgent.00001001 busy"
    assert events[0].error_kind is ErrKind.OTHER


async def test_client_stream_chat_benefit_usage_records_pool_credit():
    """福利模型收尾帧的 usage → 补推算扣池额度（token 1:1），标 estimated。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers[codearts_events.HEADER_MAAS_TYPE] == (
            codearts_events.MAAS_BENEFIT)
        return httpx.Response(200, text=_sse(
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 32, "completion_tokens": 694}},
        ) + "data:[DONE]\n")

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]},
        "deepseek-v4-flash-0731")]
    usage = next(e for e in events if e.kind is EventKind.USAGE).usage
    assert usage.input_tokens == 32 and usage.output_tokens == 694
    assert usage.credit == 726 and usage.credit_estimated is True


async def test_client_stream_chat_legacy_and_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == codearts_events.EP_CHAT
        body = json.loads(request.content)
        assert body["task"] == "chat"
        assert body["user_id"] == "alice"
        assert codearts_events.HEADER_MAAS_TYPE not in request.headers
        return httpx.Response(200, text=_sse({"text": "[DONE]"}))

    events = [e async for e in _client(handler, legacy=True).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]}, "m")]
    assert events[-1].kind is EventKind.FINISH

    def error_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(error_handler).stream_chat(_cred(), {}, "m")]
    assert caught.value.kind() is ErrKind.OTHER


async def test_client_fetch_models_merges_builtin_and_benefit():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.host}{request.url.path}")
        if request.url.path == codearts_events.EP_BENEFIT_CLAIM:
            assert request.method == "POST"
            return httpx.Response(200, json={"result": "ok"})     # 幂等
        if request.url.path == codearts_events.EP_MODEL_BUILTIN:
            assert request.headers["Agent-Type"] == "PromptCenter"
            return httpx.Response(200, json={"builtinModels": [
                "junk",
                {"model_id": "m1", "model_name": "M1"},
                {"name": "m2"},
            ]})
        return httpx.Response(200, json={"result": {"models": [
            {"modelId": "b1", "name": "B1"},
            {"model_name": "m1"},                                  # 已转正 → 不进福利目录
            {"id": 7},                                             # 坏条目跳过
        ]}})

    client = _client(handler)
    models = await client.fetch_models(_cred())
    assert [m.id for m in models] == ["m1", "m2", "b1"]
    assert "gw.test" + codearts_events.EP_BENEFIT_CLAIM in calls
    # 目录只含未转正的福利模型：m1 有 benefit 头被摘掉，b1 需要
    assert client.is_benefit_model(_cred(), "b1") is True
    assert client.is_benefit_model(_cred(), "m1") is False
    assert client.is_benefit_model(_cred(), "deepseek-v4-pro-0813") is False


async def test_client_fetch_models_partial_failure_keeps_previous_catalog():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_MODEL_BUILTIN:
            return httpx.Response(200, json={"builtinModels": [{"id": "m1"}]})
        if request.url.path == codearts_events.EP_BENEFIT_CONFIG:
            return httpx.Response(500, content=b"gw down")
        return httpx.Response(200, json={})                        # claim

    client = _client(handler, benefit_auto_claim=False)
    models = await client.fetch_models(_cred())
    assert [m.id for m in models] == ["m1"]
    # 福利来源失败 → 目录保留（不误摘 benefit 头）
    assert client.is_benefit_model(_cred(), "glm-5.3-flash") is True


async def test_client_fetch_models_builtin_failure_uses_benefit_and_empty_catalog():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_MODEL_BUILTIN:
            return httpx.Response(500, content=b"builtin down")
        if request.url.path == codearts_events.EP_BENEFIT_CLAIM:
            return httpx.Response(500, content=b"claim down")       # 静默失败
        return httpx.Response(200, json={"result": {"models": [{"id": "b1"}]}})

    client = _client(handler)
    models = await client.fetch_models(_cred())
    assert [m.id for m in models] == ["b1"]
    # 福利来源成功但为空集合 → 整体替换（非种子模型不必带 benefit 头）
    assert client.is_benefit_model(_cred(), "deepseek-v4-flash-0731") is False


async def test_client_fetch_models_empty_raises_violation():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_BENEFIT_CLAIM:
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})                         # 两路都无模型

    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="empty list"):
        await _client(handler).fetch_models(_cred())


async def test_client_claim_benefit_and_quota_and_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_BENEFIT_CLAIM:
            return httpx.Response(200, json={"result": {"claimed": True}})
        if request.url.path == codearts_events.EP_TOKEN_BALANCE:
            return httpx.Response(200, json={"result": {"remaining": 42, "total": 100}})
        if request.url.path == codearts_events.EP_CALLER_IDENTITY:
            return httpx.Response(200, json={
                "principal_id": "pid", "account_id": "acc",
                "principal_urn": "urn:...:user:carol"})
        if request.url.path == "/snap-manager" + codearts_events.EP_CURRENT_USER:
            return httpx.Response(200, json={
                "user_id": "u", "user_name": "n", "domain_id": "d"})
        return httpx.Response(500, content=b"unexpected")

    client = _client(handler)
    assert (await client.claim_benefit(_cred()))["result"]["claimed"] is True
    quota = await client.probe_quota(_cred())
    assert quota.remaining == 42 and quota.total == 100
    assert await client.caller_identity(_cred()) == ("pid", "carol", "acc")
    assert await client.current_user(_cred()) == ("u", "n", "d")

    def urnless(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"principal_id": "p", "account_id": "a"})

    assert await _client(urnless).caller_identity(_cred()) == ("p", "", "a")


async def test_client_probe_quota_never_refreshes_even_in_window():
    """额度探测只读余额：即使凭证已在刷新窗口，也不得消费一次性 refresh_token。

    刷新唯一归 `RefreshTask`（先落库再同步）。这里若刷新，会与 RefreshTask 抢
    同一个一次性 token，后到的报 `the refresh token has been used` 且本处结果
    不落库——实测由此把渠道打成硬失效。
    """
    jwk = _jwk()
    sts_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sts_calls
        if request.url.host == "sts.test":
            sts_calls += 1
            return httpx.Response(200, json=_token_response())
        return httpx.Response(200, json={"remaining": 7})

    client = _client(handler)
    expiring = _cred(refresh_token="rt", dpop_private_jwk=jwk,
                     expiration=int(time.time()) + 5)
    quota = await client.probe_quota(expiring)
    assert sts_calls == 0 and quota.remaining == 7


async def test_client_refresh_token_rotates_via_sts():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "sts.test"
        assert request.headers["DPoP"]
        form = httpx.QueryParams(request.content.decode())
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "rt-old"
        return httpx.Response(200, json={
            "credentials": {"access_key_id": "AKN", "secret_access_key": "SKN",
                            "security_token": "TOKN"},
            "refresh_token": "rt-new", "user_id": "u1"})

    refreshed = await _client(handler).refresh_token(
        _cred(refresh_token="rt-old", dpop_private_jwk=_jwk()))
    assert refreshed.refresh_token == "rt-new"              # 必须轮转回写
    assert refreshed.access_key_id == "AKN"


async def test_client_request_json_error_paths():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"bad gateway")

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).caller_identity(_cred())

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="non-JSON"):
        await _client(non_json).caller_identity(_cred())

    def non_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="shape"):
        await _client(non_object).caller_identity(_cred())


# ============================================================ CodeArtsProvider

def test_provider_id_classify_credential_from_and_key():
    provider = CodeArtsProvider()
    assert provider.id == "codearts"
    assert provider.classify(401, b"") is ErrKind.DEAD
    credential = provider.credential_from({"access_key_id": "AK", "uid": "u"})
    assert credential.access_key_id == "AK"
    from src.tasks.pacer import stable_key
    assert credential_key(credential) == stable_key("codearts", "u")
    assert credential_key(_cred(uid="")) == stable_key("codearts", "AK")


def test_provider_import_credential_validates():
    provider = CodeArtsProvider()
    valid = provider.import_credential({"access_key_id": "AK",
                                        "secret_access_key": "SK"})
    assert valid["access_key_id"] == "AK"
    # dpop_private_jwk 可缺（手工凭证无从刷新）
    assert valid["dpop_private_jwk"] == {}
    with_jwk = provider.import_credential({
        "access_key_id": "AK", "secret_access_key": "SK",
        "dpop_private_jwk": _jwk()})
    assert with_jwk["dpop_private_jwk"]["kty"] == "EC"


def test_provider_import_credential_rejects_bad_input():
    provider = CodeArtsProvider()
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="not an object"):
        provider.import_credential("nope")               # type: ignore[arg-type]
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="missing"):
        provider.import_credential({"access_key_id": "AK"})
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="dpop_private_jwk"):
        provider.import_credential({"access_key_id": "AK", "secret_access_key": "SK",
                                    "dpop_private_jwk": {"kty": "RSA"}})


async def test_provider_list_models_refresh_and_probe_quota():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_MODEL_BUILTIN:
            return httpx.Response(200, json={"builtinModels": [{"id": "m1"}]})
        if request.url.path == codearts_events.EP_BENEFIT_CONFIG:
            return httpx.Response(200, json={"result": {"models": []}})
        if request.url.host == "sts.test":
            return httpx.Response(200, json=_token_response())
        return httpx.Response(200, json={"remaining": 9})

    provider = CodeArtsProvider(client=_client(handler))
    models = await provider.list_models({})
    assert [m.id for m in models] == ["m1"]
    refreshed = await provider.refresh({"access_key_id": "AK", "secret_access_key": "SK",
                                        "refresh_token": "rt", "dpop_private_jwk": _jwk()})
    assert refreshed["access_key_id"] == "AK2"
    assert (await provider.probe_quota({})).remaining == 9
    await provider.aclose()


async def test_provider_stream_chat_pacer_pairing_and_release_on_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse({"text": "hi"}, {"text": "[DONE]"}))

    class Pacer:
        def __init__(self) -> None:
            self.turns = 0
            self.released = 0

        async def wait_turn(self, key=None) -> None:
            self.turns += 1

        def release(self, key=None) -> None:
            self.released += 1

    pacer = Pacer()
    provider = CodeArtsProvider(client=_client(handler), pacer=pacer)
    events = [e async for e in provider.stream_chat(
        {"access_key_id": "AK", "secret_access_key": "SK"},
        {"messages": []}, "m")]
    assert any(e.kind is EventKind.CONTENT for e in events)
    assert pacer.turns == 1 and pacer.released == 1
    await provider.aclose()

    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    pacer2 = Pacer()
    error_provider = CodeArtsProvider(client=_client(boom), pacer=pacer2)
    with pytest.raises(UpstreamHTTPError):
        [e async for e in error_provider.stream_chat(
            {"access_key_id": "AK", "secret_access_key": "SK"}, {}, "m")]
    assert pacer2.released == 1                        # 出错也要归还名额
    await error_provider.aclose()

    plain = CodeArtsProvider(client=_client(handler))
    _ = [e async for e in plain.stream_chat(
        {"access_key_id": "AK", "secret_access_key": "SK"}, {}, "m")]
    await plain.aclose()


# ============================================================ 边界常量校验

def test_benefit_seed_is_lowercase_and_nonempty():
    assert BENEFIT_SEED
    assert all(model == model.lower() for model in BENEFIT_SEED)


# =================================================================== events.py


async def _lines(chunks: list[bytes]):
    async def gen():
        for chunk in chunks:
            yield chunk
    async for line in codearts_events.iter_data_lines(gen()):
        yield line


def test_events_text_snapshot_replace_semantics():
    snapshot = codearts_events.TextSnapshot()
    assert snapshot.delta("He") == "He"
    assert snapshot.delta("Hello") == "llo"
    assert snapshot.delta("Hello") == ""            # 无变化
    assert snapshot.delta("Hi") == "Hi"             # 修正/回退 → 整段重发


async def test_events_iter_data_lines_handles_prefixes_and_tail():
    chunks = [
        b"event: message\n",            # 无 data 语义 → 跳过
        b"data: {\"a\": 1}\n",
        b"\n",
        b"{\"b\": 2}\n",                # 实测部分行没有 data: 前缀
        b"data: ",                      # 空载荷 → 跳过
        b'{"c":3}',                     # 末行无换行
    ]
    lines = [line async for line in _lines(chunks)]
    assert lines == ['{"a": 1}', '{"b": 2}', '{"c":3}']


async def test_events_iter_data_lines_multibyte_split_across_chunks():
    """UTF-8 多字节字符被块边界切断不能变替换字符。"""
    raw = "data: {\"text\": \"中文\"}\n".encode()
    lines = [line async for line in _lines([raw[:12], raw[12:]])]
    assert lines == ['{"text": "中文"}']


def test_events_parse_line_content_finish_reasoning_usage():
    snapshot = codearts_events.TextSnapshot()
    assert codearts_events.parse_line('{"text": "Hi"}', snapshot)[0].content == "Hi"
    done = codearts_events.parse_line('{"text": "[DONE]"}', snapshot)
    assert done[0].kind is EventKind.FINISH
    delta = codearts_events.parse_line(
        '{"delta": {"content": "c", "reasoning_content": "r"}}', snapshot)
    assert [e.kind for e in delta] == [EventKind.CONTENT, EventKind.REASONING]
    # delta 里空串 / 非字符串：不产事件
    assert codearts_events.parse_line(
        '{"delta": {"content": "", "reasoning_content": 5}}', snapshot) == []
    usage = codearts_events.parse_line(
        '{"prompt_tokens": 3, "completion_tokens": 4}', snapshot)
    assert usage[0].kind is EventKind.USAGE and usage[0].usage.input_tokens == 3


def test_events_parse_line_openai_chunk_shape():
    """v2 端点实测：标准 OpenAI chunk（delta + finish_reason + usage）。"""
    snapshot = codearts_events.TextSnapshot()
    delta = codearts_events.parse_line(
        '{"choices":[{"index":0,"delta":{"reasoning_content":"r"},'
        '"finish_reason":null}],"usage":null}', snapshot)
    assert [e.kind for e in delta] == [EventKind.REASONING]
    assert delta[0].content == "r"

    merged = codearts_events.parse_line(
        '{"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}', snapshot)
    assert [e.kind for e in merged] == [EventKind.CONTENT, EventKind.FINISH]
    assert merged[0].content == "好" and merged[1].finish_reason == "stop"

    # 收尾帧 choices 为空、usage 单独给出 → 仍要保留 usage
    tail = codearts_events.parse_line(
        '{"choices":[],"usage":{"prompt_tokens":35,"completion_tokens":45}}', snapshot)
    assert [e.kind for e in tail] == [EventKind.USAGE]
    assert tail[0].usage.input_tokens == 35 and tail[0].usage.output_tokens == 45

    # 空 delta / 非对象 choice / 空 tool_calls：不产内容事件
    assert codearts_events.parse_line(
        '{"choices":[{"delta":{"content":""}}]}', snapshot) == []
    assert codearts_events.parse_line(
        '{"choices":[5,{"delta":null}]}', snapshot) == []
    assert codearts_events.parse_line(
        '{"choices":[{"delta":{"tool_calls":[]}}]}', snapshot) == []
    tool = codearts_events.parse_line(
        '{"choices":[{"delta":{"tool_calls":[{"id":"t"}]}}]}', snapshot)
    assert tool[0].kind is EventKind.TOOL_CALLS and tool[0].tool_calls == [{"id": "t"}]


def test_events_parse_line_done_sentinel():
    """`data:[DONE]` 是流结束哨兵（非 JSON），必须识别而不是当坏 JSON 抛错。"""
    snapshot = codearts_events.TextSnapshot()
    events = codearts_events.parse_line("[DONE]", snapshot)
    assert len(events) == 1
    assert events[0].kind is EventKind.FINISH and events[0].finish_reason == "stop"
    # `iter_data_lines` 要原样把它吐出来（不能被当成无载荷行丢掉）
    assert codearts_events._data_payload("data:[DONE]") == "[DONE]"  # noqa: SLF001


def test_events_parse_line_embedded_error_code():
    snapshot = codearts_events.TextSnapshot()
    events = codearts_events.parse_line(
        '{"text": "[DONE]", "error_code": "ChatAgent.00001001", "error_msg": "busy"}',
        snapshot)
    assert len(events) == 1 and events[0].kind is EventKind.ERROR
    assert events[0].error_message == "ChatAgent.00001001 busy"
    # 无 error_msg：只留原始码
    only_code = codearts_events.parse_line(
        '{"error_code": "ChatAgent.00001001"}', snapshot)
    assert only_code[0].error_message == "ChatAgent.00001001"


def test_events_parse_line_bad_payload_raises():
    snapshot = codearts_events.TextSnapshot()
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="unparsable"):
        codearts_events.parse_line("{not json", snapshot)
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="not an object"):
        codearts_events.parse_line("[1, 2]", snapshot)


def test_events_usage_tolerates_bad_types():
    snapshot = codearts_events.TextSnapshot()
    assert codearts_events.parse_line('{"prompt_tokens": true}', snapshot) == []
    usage = codearts_events.parse_line('{"prompt_tokens": 1}', snapshot)[0]
    assert usage.usage.input_tokens == 1 and usage.usage.output_tokens is None


def test_events_classify_error_code_branches():
    classify = codearts_events.classify_error_code
    assert classify(None) is ErrKind.OTHER
    assert classify("0") is ErrKind.OTHER
    assert classify("   ") is ErrKind.OTHER
    assert classify("InferHub.002002009.404") is ErrKind.BLOCKED
    assert classify("InferHub.4004.200 benefit not found") is ErrKind.BLOCKED
    assert classify("TM.00001041") is ErrKind.MODEL
    assert classify("tpm limit") is ErrKind.MODEL
    assert classify("1005 quota") is ErrKind.PLAN
    assert classify("APIG.0301 decrypt token fail") is ErrKind.DEAD
    assert classify("ChatAgent.99999999") is ErrKind.OTHER


def test_events_classify_status_branches():
    classify = codearts_events.classify_status
    assert classify(402, b"") is ErrKind.CREDIT
    assert classify(200, b'{"code": 14018}') is ErrKind.CREDIT
    assert classify(200, b'{"code": 1005}') is ErrKind.PLAN
    assert classify(400, b'{"code": 11102}') is ErrKind.BLOCKED
    assert classify(404, b"model is not registered") is ErrKind.BLOCKED
    assert classify(401, b"") is ErrKind.DEAD
    assert classify(404, b"") is ErrKind.SOFT
    assert classify(429, b"00001041") is ErrKind.MODEL
    assert classify(429, b"slow down") is ErrKind.SOFT
    # 并发会话超限实测走 HTTP 400（不是 429）：必须判成可重试的 MODEL，
    # 否则落 INVALID → 换号也没用、还不冷却（77% 失败率主因之一）。
    throttle_body = ('{"error_code":"TM.00001041",'
                     '"error_msg":"并发会话数已达上限(3个)，请关闭部分会话后重试。"}'
                     ).encode()
    assert classify(400, throttle_body) is ErrKind.MODEL
    assert classify(400, b"rate limit exceeded") is ErrKind.MODEL
    assert classify(400, b"bad request") is ErrKind.INVALID
    assert classify(500, b"boom") is ErrKind.OTHER


def test_events_parse_line_skips_structural_noise():
    """心跳 / 被截断的裸括号行跳过，而不是让整条响应以 unparsable 失败。"""
    snapshot = codearts_events.TextSnapshot()
    assert codearts_events.parse_line("{", snapshot) == []
    assert codearts_events.parse_line("  [  ", snapshot) == []
    assert codearts_events.parse_line("", snapshot) == []
    # 噪声跳过不吞真载荷
    assert codearts_events.parse_line('{"text": "hi"}', snapshot)[0].content == "hi"


async def test_events_iter_data_lines_skips_non_data_lines_only():
    async def gen():
        yield b"event: ping\n"
        yield b": comment\n"
    assert [line async for line in codearts_events.iter_data_lines(gen())] == []

