"""Qoder COSY 私有协议：自定义 Base64、设备指纹与签名。"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from src.provider.qoder import events as qoder_events
from src.provider.qoder.cosy import (
    COSY_VERSION,
    CosySession,
    CosySessionCache,
    aes_cbc_decrypt,
    cosy_signature,
    derive_id,
    derive_machine_token,
    derive_machine_type,
    derive_request_id,
    json_sorted_compact,
    qoder_decode,
    qoder_encode,
    sign_path,
)
from tests.provider.qoder._support import GATEWAY, cred


def test_qoder_encode_roundtrip_and_custom_alphabet():
    for plain in (b"", b"a", b"ab", b"abc", b"hello world!" * 7):
        encoded = qoder_encode(plain)
        assert qoder_decode(encoded) == plain
    # 标准字母表字符不出现在编码结果里，填充符换成 "$"
    encoded = qoder_encode(b"\x00")
    assert "+" not in encoded and "/" not in encoded and "=" not in encoded
    assert "$" in encoded

def test_qoder_encode_matches_documented_transform():
    plain = b"{\"a\":1}"
    std = base64.b64encode(plain).decode("ascii")
    size = len(std)
    head = size // 3
    rearranged = std[size - head:] + std[head:size - head] + std[:head]
    expected = rearranged.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
        "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!$"))
    assert qoder_encode(plain) == expected

def test_json_sorted_compact_none_becomes_empty_string():
    assert json_sorted_compact({"b": 1, "a": None}) == b'{"a":"","b":1}'
    assert json_sorted_compact({}) == b"{}"

def test_derive_helpers_are_stable_and_shaped():
    assert derive_id("u1", "machine") == derive_id("u1", "machine")
    assert derive_id("u1", "machine") != derive_id("u2", "machine")
    assert derive_id("", "machine") == derive_id("", "machine")   # anonymous 稳定
    machine_type = derive_machine_type("u1")
    assert len(machine_type) == 18 and "-" not in machine_type
    assert len(derive_machine_token("u1")) == 43
    assert derive_request_id("u1").startswith(derive_id("u1", "req") + "-")

def test_sign_path_strips_algo_prefix_and_query():
    assert sign_path(GATEWAY + qoder_events.EP_MODELS) == (
        "/api/v2/model/list")
    assert sign_path(GATEWAY + qoder_events.EP_CHAT) == (
        "/api/v2/service/pro/sse/agent_chat_generation")
    assert sign_path("https://gw.test/other") == "/other"
    assert sign_path("https://gw.test") == "/"

def test_session_headers_and_signature_vector():
    session = CosySession(uid="u1", access_token="tok", temp_key="0123456789abcdef")
    url = GATEWAY + qoder_events.EP_CHAT
    headers = session.headers(body="BODY", raw_url=url, model_key="qmodel_38max",
                              sse=True, date="1700000000", request_id="rid")
    payload = base64.b64encode(json_sorted_compact({
        "cosyVersion": COSY_VERSION, "ideVersion": "", "info": session.info,
        "requestId": "rid", "version": "v1"})).decode("ascii")
    digest = hashlib.md5("\n".join([
        payload, session.cosy_key, "1700000000", "BODY",
        "/api/v2/service/pro/sse/agent_chat_generation"]).encode()).hexdigest()
    assert headers["authorization"] == f"Bearer COSY.{payload}.{digest}"
    assert headers["cosy-date"] == "1700000000"
    assert headers["cosy-user"] == "u1"
    assert headers["x-model-key"] == "qmodel_38max"
    assert headers["x-model-source"] == "system"
    assert headers["cache-control"] == "no-cache"
    assert headers["cosy-key"] == session.cosy_key
    assert headers["cosy-machineid"] == derive_id("u1", "machine")

def test_session_without_sse_and_model_key_omits_optional_headers():
    session = CosySession(uid="", access_token="tok", temp_key="0123456789abcdef")
    headers = session.headers(body="x", raw_url=GATEWAY + qoder_events.EP_MODELS,
                              sse=False, accept="application/json")
    assert "cache-control" not in headers
    assert "x-model-key" not in headers
    assert headers["accept"] == "application/json"
    assert headers["cosy-user"] == ""

def test_session_info_is_decryptable_identity():
    session = CosySession(uid="u9", access_token="tok", nickname="N",
                          refresh_token="drt-9", temp_key="0123456789abcdef",
                          user_type="pro", org_id="org-1", org_name="Org")
    plain = aes_cbc_decrypt(base64.b64decode(session.info), b"0123456789abcdef",
                            b"0123456789abcdef")
    identity = json.loads(plain)
    assert identity["uid"] == "u9" and identity["name"] == "N"
    assert identity["security_oauth_token"] == "tok"
    assert identity["refresh_token"] == "drt-9"
    assert identity["organization_id"] == "org-1" and identity["yx_uid"] == ""

def test_session_rejects_empty_token_and_bad_temp_key():
    with pytest.raises(ValueError, match="empty access token"):
        CosySession(uid="u", access_token="")
    with pytest.raises(ValueError, match="temp key"):
        CosySession(uid="u", access_token="t", temp_key="short")

def test_cosy_signature_is_md5_of_parts():
    assert cosy_signature(payload_b64="p", cosy_key="k", date="d", body="b",
                          path="/x") == hashlib.md5(
        b"p\nk\nd\nb\n/x").hexdigest()

def test_session_cache_rebuilds_on_token_change():
    cache = CosySessionCache(temp_key_factory=lambda: "0123456789abcdef")
    first = cache.get(cred())
    assert cache.get(cred()) is first                       # 同 token 复用
    rotated = cache.get(cred(access_token="dt-token-2"))
    assert rotated is not first
    cache.invalidate("u-1")
    assert cache.get(cred()) is not first
    cache.clear()
    with pytest.raises(ValueError, match="empty access token"):
        cache.get(cred(access_token=""))

def test_session_cache_keys_by_token_when_uid_missing():
    cache = CosySessionCache(temp_key_factory=lambda: "0123456789abcdef")
    session = cache.get(cred(uid=""))
    assert session.uid == ""
    assert cache.get(cred(uid="")) is session

def test_cosy_session_cache_clear_and_invalidate_missing():
    cache = CosySessionCache(temp_key_factory=lambda: "0123456789abcdef")
    cache.invalidate("nobody")                                # 不存在也不抛
    cache.clear()


# ----------------------------------------------- x-model-key 进头的字符校验


@pytest.mark.parametrize("model_key", [
    "qmodel_38max",
    "kmodel_latest",
    "vendor/model:free",
    "model@qoder",                # `@` 渠道后缀
    "a" * 128,                    # 长度上界
])
def test_session_headers_accept_legal_model_key(model_key):
    session = CosySession(uid="u1", access_token="t",
                          temp_key="0123456789abcdef")
    headers = session.headers(body="B", raw_url=GATEWAY + qoder_events.EP_CHAT,
                              model_key=model_key, date="1700000000",
                              request_id="rid")
    assert headers["x-model-key"] == model_key
    assert headers["x-model-source"] == "system"


@pytest.mark.parametrize("model_key", [
    "模型-中文名",                 # 非 ASCII → httpx 抛 UnicodeEncodeError
    "evil\r\nX-Injected: 1",      # CRLF → httpx 抛 LocalProtocolError
    "with space",
    "a" * 129,                    # 超长
])
def test_session_headers_reject_illegal_model_key(model_key):
    """非法模型名在写头处转成 `InvalidModelName`（INVALID），不漏成 500。"""
    from src.provider.base import ErrKind, InvalidModelName

    session = CosySession(uid="u1", access_token="t",
                          temp_key="0123456789abcdef")
    with pytest.raises(InvalidModelName) as caught:
        session.headers(body="B", raw_url=GATEWAY + qoder_events.EP_CHAT,
                        model_key=model_key, date="1700000000", request_id="rid")
    assert caught.value.kind() is ErrKind.INVALID


def test_session_headers_omit_model_key_when_empty():
    """空 `model_key` 不写头（模型清单请求走这条），不必过白名单。"""
    session = CosySession(uid="u1", access_token="t",
                          temp_key="0123456789abcdef")
    headers = session.headers(body="B", raw_url=GATEWAY + qoder_events.EP_CHAT,
                              model_key="", date="1700000000", request_id="rid")
    assert "x-model-key" not in headers and "x-model-source" not in headers
