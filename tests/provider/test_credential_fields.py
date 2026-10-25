"""华为云系渠道共用的凭证字段解析。

两家（CodeArts / OfficeAce）落盘形状一致，解析规则曾各写一份且字节级相同。
这里直接测共享实现本身；各渠道仍保留同名薄包装，其行为由各自的
`test_credential.py` 覆盖。
"""

from __future__ import annotations

import json

import pytest

from src.provider.credential_fields import (
    epoch_from_iso,
    expiration_from_raw,
    jwk_from_raw,
    relogin_required,
    text_field,
)


def test_text_field_picks_first_non_empty_string():
    raw = {"a": "  ", "b": " x ", "c": "y"}
    assert text_field(raw, "a", "b", "c") == "x"
    assert text_field(raw, "a", "missing") == ""
    # 非字符串一律跳过（bool / 数字不是合法字段值）
    assert text_field({"a": 5, "b": True}, "a", "b") == ""


def test_epoch_from_iso_rejects_non_string_and_bad_value():
    assert epoch_from_iso(None) == 0
    assert epoch_from_iso("") == 0
    assert epoch_from_iso("   ") == 0
    assert epoch_from_iso(1234) == 0
    assert epoch_from_iso("not-a-date") == 0
    assert epoch_from_iso("2026-10-21T00:00:00Z") > 0


@pytest.mark.parametrize("raw,expected", [
    (True, 0),                       # bool 不是时间戳
    (0, 0),
    (1700000000, 1700000000),        # 秒
    (1700000000000, 1700000000),     # 毫秒归一到秒
    ("1700000000", 1700000000),      # 数字字符串
    ("1700000000000", 1700000000),
    ("2026-10-21T00:00:00Z", None),  # ISO8601（只断言非 0）
    ("bad", 0),
    (None, 0),
    (1.5, 1),
])
def test_expiration_from_raw(raw, expected):
    got = expiration_from_raw(raw)
    if expected is None:
        assert got > 0
    else:
        assert got == expected


def test_jwk_from_raw_accepts_string_and_dict():
    assert jwk_from_raw('{"kty": "EC", "d": "abc"}') == {"kty": "EC", "d": "abc"}
    assert jwk_from_raw({"kty": "EC", "n": 5}) == {"kty": "EC"}   # 非字符串值丢弃
    assert jwk_from_raw("{bad json") == {}
    assert jwk_from_raw(None) == {}
    assert jwk_from_raw(5) == {}


def test_relogin_required_matches_markers_only():
    markers = ("invalid refresh token", "sts5.1806")
    assert relogin_required(b'{"error":"invalid refresh token"}', markers) != ""
    assert relogin_required(b'{"code":"STS5.1806"}', markers) != ""     # 大小写不敏感
    assert relogin_required(b'{"error":"internal server error"}', markers) == ""
    # 可重试类失败绝不能判成终态（判错=永久废掉一条好凭证）
    assert relogin_required(b"", markers) == ""
    assert relogin_required(b"\xff\xfe invalid refresh token", markers) != ""


def test_jwk_from_raw_roundtrip_json_string():
    payload = {"kty": "EC", "crv": "P-256", "d": "s"}
    assert jwk_from_raw(json.dumps(payload)) == payload
