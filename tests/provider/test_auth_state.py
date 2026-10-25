"""共享 OAuth pending 会话登记表。

四个渠道原先各写一份 `AuthStateStore`（owner/consume/cancel/cleanup 逐字相同），
现由基类承担；这里直接测基类语义，各渠道的 `begin` 包装由各自测试覆盖。
"""

from __future__ import annotations

import pytest

from src.provider.auth_state import AuthStateStore, pkce_pair

MOMENT = 1_700_000_000


def test_begin_registers_and_owner_checks_username():
    store: AuthStateStore[str] = AuthStateStore(600)
    state = store.begin("alice", "payload", now=MOMENT)
    assert store.owner(state, "alice", now=MOMENT) is True
    # 别人拿不到（归属校验是安全约束，不是风格）
    assert store.owner(state, "bob", now=MOMENT) is False
    assert store.payload(state, "bob", now=MOMENT) is None


def test_owner_is_false_for_unknown_state():
    store: AuthStateStore[str] = AuthStateStore(600)
    assert store.owner("nope", "alice", now=MOMENT) is False
    assert store.payload("nope", "alice", now=MOMENT) is None
    assert store.reservation("nope", "alice", now=MOMENT) is None


def test_owner_expires_lazily_and_drops_entry():
    """过期即判否并从表里删掉（惰性清理，避免字典堆积）。"""
    store: AuthStateStore[str] = AuthStateStore(10)
    state = store.begin("alice", "p", now=MOMENT)
    assert store.owner(state, "alice", now=MOMENT + 9) is True
    assert store.owner(state, "alice", now=MOMENT + 10) is False
    assert len(store) == 0


def test_consume_is_single_use_and_cancel_aliases_it():
    store: AuthStateStore[str] = AuthStateStore(600)
    state = store.begin("alice", "p", now=MOMENT)
    assert store.consume(state, "alice", now=MOMENT) is True
    assert store.consume(state, "alice", now=MOMENT) is False

    other = store.begin("alice", "p", now=MOMENT)
    assert store.cancel(other, "alice", now=MOMENT) is True
    assert store.cancel(other, "alice", now=MOMENT) is False


def test_cleanup_removes_only_expired():
    store: AuthStateStore[str] = AuthStateStore(10)
    old = store.begin("alice", "p", now=MOMENT)
    fresh = store.begin("alice", "p", now=MOMENT + 5)
    store.cleanup(now=MOMENT + 10)
    assert store.owner(old, "alice", now=MOMENT + 10) is False
    assert store.owner(fresh, "alice", now=MOMENT + 10) is True


def test_max_pending_evicts_oldest():
    """攒太多没登完的顶掉最老的（OfficeAce 的实测需求）。"""
    store: AuthStateStore[str] = AuthStateStore(600, max_pending=2)
    first = store.begin("alice", "a", now=MOMENT)
    second = store.begin("alice", "b", now=MOMENT + 1)
    third = store.begin("alice", "c", now=MOMENT + 2)
    assert store.owner(first, "alice", now=MOMENT + 2) is False
    assert store.owner(second, "alice", now=MOMENT + 2) is True
    assert store.owner(third, "alice", now=MOMENT + 2) is True
    assert len(store) == 2


def test_reservation_returns_owner_and_payload():
    store: AuthStateStore[str] = AuthStateStore(600)
    state = store.begin("alice", "payload", now=MOMENT)
    entry = store.reservation(state, "alice", now=MOMENT)
    assert entry is not None
    assert entry.username == "alice"
    assert entry.created_at == MOMENT
    assert entry.payload == "payload"


def test_pkce_pair_challenge_is_s256_of_verifier():
    import base64
    import hashlib

    verifier, challenge = pkce_pair()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_pkce_pair_accepts_explicit_verifier():
    """调用方自带 verifier（Qoder 的自造 64 位字母表）时不再随机。"""
    assert pkce_pair("fixed-verifier")[0] == "fixed-verifier"


@pytest.mark.parametrize("length", [1, 32, 64])
def test_pkce_pair_roundtrip_length(length):
    verifier, challenge = pkce_pair("v" * length)
    assert verifier == "v" * length
    assert "=" not in challenge
