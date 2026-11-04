"""小浣熊凭证归一：JWT / 裸 token / 整段 cookie。"""

from __future__ import annotations

import base64
import json

from src.provider.raccoon.credential import (
    RaccoonCredential,
    jwt_claims,
    token_from_cookie,
)


def _jwt(claims: dict) -> str:
    def seg(value: dict) -> str:
        raw = json.dumps(value).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


BOX = _jwt({"sub": "pu-01a", "exp": 1796391693, "name": "Raccoon"})


def test_jwt_claims_parses_and_rejects_non_jwt():
    assert jwt_claims(BOX) == {"sub": "pu-01a", "exp": 1796391693,
                               "name": "Raccoon"}
    assert jwt_claims("not-a-jwt") == {}
    assert jwt_claims("a..c") == {}


def test_jwt_claims_handles_bad_base64_and_non_object():
    assert jwt_claims("a.!!!.c") == {}
    bad_json = base64.urlsafe_b64encode(b"\xff\xfe").decode("ascii").rstrip("=")
    assert jwt_claims(f"a.{bad_json}.c") == {}
    scalar = base64.urlsafe_b64encode(b"123").decode("ascii").rstrip("=")
    assert jwt_claims(f"a.{scalar}.c") == {}


def test_token_from_cookie():
    assert token_from_cookie("a=1; xhx_token=abc; b=2") == "abc"
    assert token_from_cookie("xhx_token=abc") == "abc"
    assert token_from_cookie("a=1; b=2") == ""
    # 没有 `=` 的段不应崩
    assert token_from_cookie("junk; xhx_token=v") == "v"


def test_from_dict_and_to_dict_round_trip():
    credential = RaccoonCredential.from_dict(
        {"access_token": "t", "uid": "u", "nickname": "n", "expires_at": 5})
    assert credential.to_dict() == {"access_token": "t", "uid": "u",
                                    "nickname": "n", "expires_at": 5}


def test_from_dict_treats_bad_expiry_as_zero():
    for value in (True, "5", None, 5.5):
        assert RaccoonCredential.from_dict(
            {"access_token": "t", "expires_at": value}).expires_at == 0


def test_from_import_accepts_raw_jwt_and_extracts_claims():
    credential = RaccoonCredential.from_import(BOX)
    assert credential.access_token == BOX
    assert credential.uid == "pu-01a"
    assert credential.nickname == "Raccoon"
    assert credential.expires_at == 1796391693


def test_from_import_accepts_all_token_keys():
    for key in ("access_token", "token", "jwt", "xhx_token"):
        credential = RaccoonCredential.from_import({key: BOX})
        assert credential.access_token == BOX


def test_from_import_accepts_cookie_string():
    credential = RaccoonCredential.from_import({"cookie": f"a=1; xhx_token={BOX}"})
    assert credential.access_token == BOX


def test_from_import_explicit_fields_win():
    credential = RaccoonCredential.from_import(
        {"access_token": BOX, "uid": "explicit", "nickname": "昵称"})
    assert credential.uid == "explicit"
    assert credential.nickname == "昵称"


def test_from_import_user_id_and_user_name_fallbacks():
    credential = RaccoonCredential.from_import(
        {"access_token": "plain", "user_id": "u2", "user_name": "名字"})
    assert (credential.uid, credential.nickname) == ("u2", "名字")


def test_from_import_rejects_empty_shapes():
    for raw in (None, 3, {}, {"cookie": "a=1"}, {"access_token": "   "}):
        assert RaccoonCredential.from_import(raw).access_token == ""


def test_from_token_handles_missing_claims():
    credential = RaccoonCredential.from_import({"access_token": "plain"})
    assert (credential.access_token, credential.uid, credential.nickname,
            credential.expires_at) == ("plain", "", "", 0)


def test_from_token_falls_back_to_uid_claim_and_nickname():
    token = _jwt({"uid": "u3", "nickname": "nick", "exp": "bad"})
    credential = RaccoonCredential.from_import({})
    assert credential.to_dict() == {"access_token": "", "uid": "", "nickname": "",
                                    "expires_at": 0}
    credential = RaccoonCredential.from_import({"token": token})
    assert credential.uid == "u3"
    assert credential.nickname == "nick"
    assert credential.expires_at == 0

def test_derive_device_id_is_stable_and_shaped_like_upstream():
    from src.provider.raccoon.credential import derive_device_id

    first = derive_device_id("pu-01a")
    assert first == derive_device_id("pu-01a")          # 同一账号永远同一台「设备」
    assert first != derive_device_id("pu-other")        # 不同账号不能撞设备
    # 上游实测形态：dev<32hex>-<uuid>
    assert first.startswith("dev")
    body = first[3:]
    hex_part, _, uuid_part = body.partition("-")
    assert len(hex_part) == 32 and all(c in "0123456789abcdef" for c in hex_part)
    assert len(uuid_part.split("-")) == 5
    # uid 缺失时用 anonymous 占位（可用但会被关联），不是随机值
    assert derive_device_id("") == derive_device_id("")
