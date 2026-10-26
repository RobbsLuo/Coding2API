"""LobsterAI 凭证类型：解析、到期判定、uid 四级回落。"""

from __future__ import annotations

import pytest

from src.provider.base import UpstreamProtocolViolation
from src.provider.lobsterai.credential import (
    LobsteraiCredential,
    resolve_uid,
)
from tests.provider.lobsterai._support import MOMENT


def test_from_dict_accepts_camel_and_snake():
    camel = LobsteraiCredential.from_dict({
        "accessToken": "AT", "refreshToken": "RT", "expiresAt": 100,
        "uuid": "U", "firstKeyfrom": "F", "latestKeyfrom": "L",
        "userId": "UID", "nickname": "bob", "uid": "U1",
    })
    snake = LobsteraiCredential.from_dict({
        "access_token": "AT", "refresh_token": "RT", "expires_at": 100,
        "uuid": "U", "first_keyfrom": "F", "latest_keyfrom": "L",
        "user_id": "UID", "nickname": "bob", "uid": "U1",
    })
    assert camel.to_dict() == snake.to_dict()
    assert camel.access_token == "AT" and camel.uuid == "U"


def test_from_dict_normalizes_millisecond_expiry():
    """毫秒时间戳归一到秒（否则到期判断会晚三千年）。"""
    credential = LobsteraiCredential.from_dict({
        "accessToken": "AT", "expiresAt": 1_700_000_000_000})
    assert credential.expires_at == 1_700_000_000


def test_from_dict_rejects_non_object():
    with pytest.raises(UpstreamProtocolViolation):
        LobsteraiCredential.from_dict("nope")  # type: ignore[arg-type]


def test_token_expires_at_prefers_explicit_value():
    credential = LobsteraiCredential.from_dict({
        "accessToken": "AT", "expiresAt": 12345})
    assert credential.token_expires_at() == 12345


def test_token_expires_at_falls_back_to_jwt_exp():
    """显式值缺失时回落 JWT `exp`（上游签发的权威到期时间）。"""
    import base64
    import json

    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": 1_800_000_000}).encode()).decode().rstrip("=")
    token = f"header.{payload}.sig"
    credential = LobsteraiCredential.from_dict({"accessToken": token})
    assert credential.token_expires_at() == 1_800_000_000


def test_token_expires_at_unknown_is_zero():
    """两者都拿不到时返回 0（未知），不猜本地 TTL。"""
    assert LobsteraiCredential.from_dict({"accessToken": "opaque"}).token_expires_at() == 0


def test_needs_refresh_true_before_window():
    credential = LobsteraiCredential.from_dict({
        "accessToken": "AT", "expiresAt": MOMENT + 100})
    assert credential.needs_refresh(200, now=MOMENT) is True
    assert credential.needs_refresh(50, now=MOMENT) is False


def test_needs_refresh_true_when_expiry_unknown():
    """到期未知 → 保守判 True：宁可多刷一次，也不要静默过期。"""
    assert LobsteraiCredential.from_dict(
        {"accessToken": "opaque"}).needs_refresh(60, now=MOMENT) is True


def test_resolve_uid_prefers_user_id():
    assert resolve_uid({"user": {"id": "u1"}}, "AT") == "u1"
    assert resolve_uid({"user": {"userId": "u2", "id": "u1"}}, "AT") == "u1"
    assert resolve_uid({"user": {"yid": "y3"}}, "AT") == "y3"


def test_resolve_uid_falls_back_to_token_hash():
    """三个 user 字段皆空时用 token 的 sha256 前 16 位（稳定且不泄露明文）。"""
    uid = resolve_uid({"user": {}}, "AT")
    assert len(uid) == 16
    assert uid == resolve_uid({}, "AT")
    assert uid != resolve_uid({}, "OTHER")


def test_to_dict_round_trips():
    credential = LobsteraiCredential.from_dict({
        "accessToken": "AT", "refreshToken": "RT", "uuid": "U",
        "firstKeyfrom": "F", "latestKeyfrom": "L"})
    assert LobsteraiCredential.from_dict(credential.to_dict()).to_dict() \
        == credential.to_dict()
