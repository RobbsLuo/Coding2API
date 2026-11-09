"""灵犀凭证归一：整段 Cookie / 裸 sid / 引号 / 缺字段。"""

from __future__ import annotations

import hashlib

import pytest

from src.provider.base import UpstreamProtocolViolation
from src.provider.lingxi.credential import (
    DEVICE_ID,
    LingxiCredential,
    parse_cookie_input,
)


def test_parse_cookie_input_bare_sid():
    assert parse_cookie_input("  abc-123  ") == {"wps_sid": "abc-123"}


def test_parse_cookie_input_full_header_and_quotes():
    raw = "\"Cookie: wps_sid=abc; uid=42; copilot_server_branch=hb; lang=zh-cn\""
    assert parse_cookie_input(raw) == {
        "wps_sid": "abc", "uid": "42", "copilot_server_branch": "hb"}


def test_parse_cookie_input_skips_bad_segments():
    assert parse_cookie_input("lang=zh; wps_sid=abc; foo; =bar; uid=") == {
        "wps_sid": "abc"}


def test_parse_cookie_input_empty():
    assert parse_cookie_input("   ") == {}
    assert parse_cookie_input("Cookie:   ") == {}


def test_from_dict_accepts_cookie_field():
    credential = LingxiCredential.from_dict({"cookie": "wps_sid=abc; uid=7"})
    assert credential.wps_sid == "abc"
    assert credential.uid == "7"


def test_from_dict_prefers_explicit_uid_and_branch():
    credential = LingxiCredential.from_dict({
        "wps_sid": "wps_sid=abc", "uid": "explicit",
        "copilot_server_branch": "hb"})
    assert credential.wps_sid == "abc"
    assert credential.uid == "explicit"
    assert credential.branch == "hb"


def test_from_dict_keeps_branch_from_cookie():
    credential = LingxiCredential.from_dict({
        "raw": "wps_sid=abc; copilot_server_branch=hb"})
    assert credential.branch == "hb"


def test_from_dict_non_dict_is_empty():
    assert LingxiCredential.from_dict(None).wps_sid == ""
    assert LingxiCredential.from_dict("x").wps_sid == ""


def test_from_import_requires_object_and_sid():
    with pytest.raises(UpstreamProtocolViolation):
        LingxiCredential.from_import("wps_sid=abc")
    with pytest.raises(UpstreamProtocolViolation):
        LingxiCredential.from_import({"uid": "1"})
    credential = LingxiCredential.from_import({"wps_sid": "abc"})
    assert credential.wps_sid == "abc"


def test_to_dict_drops_empty_fields():
    assert LingxiCredential(wps_sid="abc").to_dict() == {"wps_sid": "abc"}
    full = LingxiCredential(wps_sid="abc", uid="1", branch="hb").to_dict()
    assert full == {"wps_sid": "abc", "uid": "1",
                    "copilot_server_branch": "hb"}


def test_cookie_header_order():
    assert LingxiCredential(wps_sid="abc").cookie_header() == "wps_sid=abc"
    assert LingxiCredential("abc", "1", "hb").cookie_header() == (
        "wps_sid=abc; uid=1; copilot_server_branch=hb")


def test_identity_prefers_uid():
    assert LingxiCredential("abc", "42").identity() == "42"


def test_identity_falls_back_to_sid_hash():
    digest = hashlib.sha256(b"abc").hexdigest()[:16]
    assert LingxiCredential("abc").identity() == f"sid-{digest}"


def test_device_id_is_stable_hex():
    assert len(DEVICE_ID) == 64
    assert int(DEVICE_ID, 16) >= 0