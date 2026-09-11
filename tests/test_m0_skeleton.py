"""M0 骨架测试：配置、加密、用户、会话、API Key、调度器（100% 目标）。"""

from __future__ import annotations

import base64

import pytest

from src.auth import apikey
from src.auth import session as session_mod
from src.auth.rbac import ForbiddenError, Principal, require_admin
from src.auth.users import (
    UsersFileError,
    UsersFileStore,
    create_password_hash,
    verify_password,
)
from src.config import Settings, validate_endpoint_allowed
from src.db.crypto import CredentialCipher, CredentialDecryptError, derive_key
from src.engine.scheduler import (
    PLAN_COOLDOWN_SECONDS,
    SOFT_COOLDOWN_SECONDS,
    Candidate,
    Scheduler,
)
from src.provider.base import (
    EXHAUSTED,
    ErrKind,
    Event,
    EventKind,
    Quota,
    health_score,
)

# --------------------------------------------------------------------- 配置

BASE_ENV = {"APP_SECRET": "test-secret"}


def test_settings_defaults_and_admin_set():
    s = Settings(_env_file=None, APP_SECRET="s", ADMIN_USERNAMES="alice, bob ,")
    assert s.admin_set == frozenset({"alice", "bob"})
    assert s.default_model == "glm-5.2"
    assert s.checkin_hour == 9
    assert s.is_admin("alice") and not s.is_admin("carol")


def test_settings_requires_app_secret():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_allowed_endpoints_whitelist():
    s = Settings(_env_file=None, APP_SECRET="s")
    assert validate_endpoint_allowed("https://copilot.tencent.com", s)
    assert validate_endpoint_allowed("https://www.codebuddy.ai", s)
    assert not validate_endpoint_allowed("https://evil.example", s)


def test_db_path_joins_data_dir():
    s = Settings(_env_file=None, APP_SECRET="s", DATA_DIR="/tmp/cbd")
    assert s.db_path == "/tmp/cbd/coding2api.sqlite3"


# --------------------------------------------------------------------- 加密

def test_cipher_roundtrip():
    cipher = CredentialCipher("secret-a")
    token = cipher.encrypt(b'{"uid": "1"}')
    assert cipher.decrypt(token) == b'{"uid": "1"}'


def test_cipher_wrong_secret_raises():
    token = CredentialCipher("secret-a").encrypt(b"x")
    with pytest.raises(CredentialDecryptError):
        CredentialCipher("secret-b").decrypt(token)


def test_cipher_rejects_empty_secret():
    with pytest.raises(ValueError):
        CredentialCipher("")


def test_derive_key_is_stable_urlsafe_base64():
    key = derive_key("abc")
    assert key == derive_key("abc")
    assert key != derive_key("abd")
    assert len(base64.urlsafe_b64decode(key)) == 32


# --------------------------------------------------------------------- 用户

def test_password_hash_roundtrip():
    h = create_password_hash("p@ss", iterations=600_000)
    assert verify_password("p@ss", h)
    assert not verify_password("wrong", h)


def test_password_hash_rejects_bad_iterations():
    with pytest.raises(ValueError):
        create_password_hash("p", iterations=100)


@pytest.mark.parametrize("bad", ["", "plain", "md5$1$a$b", "pbkdf2_sha256$x$a$b",
                                 "pbkdf2_sha256$1000$a$b", "pbkdf2_sha256$600000!!!a$b"])
def test_verify_password_rejects_malformed(bad):
    assert verify_password("p", bad) is False


def test_users_file_store(tmp_path):
    path = tmp_path / "users.txt"
    path.write_text(
        "# comment\n"
        f"alice:{create_password_hash('pw1', iterations=600_000)}\n"
        f"\ncarol:{create_password_hash('pw2', iterations=600_000)}\n",
        encoding="utf-8",
    )
    store = UsersFileStore(path)
    assert store.verify("alice", "pw1")
    assert not store.verify("alice", "nope")
    assert not store.verify("bob", "pw1")
    assert store.has("carol") and not store.has("bob")
    assert set(store.list_usernames()) == {"alice", "carol"}
    store.validate()


def test_users_file_missing(tmp_path):
    with pytest.raises(UsersFileError):
        UsersFileStore(tmp_path / "nope.txt").validate()


def test_users_file_empty(tmp_path):
    path = tmp_path / "users.txt"
    path.write_text("# only comment\n", encoding="utf-8")
    with pytest.raises(UsersFileError):
        UsersFileStore(path).validate()


def test_users_file_invalid_line(tmp_path):
    path = tmp_path / "users.txt"
    path.write_text("nocolon\n", encoding="utf-8")
    with pytest.raises(UsersFileError):
        UsersFileStore(path).validate()


# ------------------------------------------------------------------- 会话

def test_session_roundtrip_and_expiry():
    token = session_mod.create_session_token("alice", "sec", issued_at=1000, ttl_seconds=60)
    assert session_mod.verify_session_token(token, "sec", now=1030) == "alice"
    assert session_mod.verify_session_token(token, "sec", now=2000) is None
    assert session_mod.verify_session_token(token, "wrong", now=1030) is None


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "notbase64!!.x"])
def test_session_rejects_malformed(bad):
    assert session_mod.verify_session_token(bad, "sec", now=1) is None


# ----------------------------------------------------------------- API Key

def test_api_key_generate_digest_preview():
    key = apikey.generate_api_key()
    assert key.startswith("sk-")
    assert apikey.digest_api_key(key) == apikey.digest_api_key(key)
    assert apikey.matches(key, apikey.digest_api_key(key))
    assert not apikey.matches(apikey.generate_api_key(), apikey.digest_api_key(key))
    assert apikey.preview_api_key(key).startswith("sk-…")


# -------------------------------------------------------------------- RBAC

def test_require_admin():
    assert require_admin(Principal("root", True)).username == "root"
    with pytest.raises(ForbiddenError):
        require_admin(Principal("u", False))


# ----------------------------------------------------------------- 健康度

def test_health_three_states():
    assert health_score(None) is None                                   # 未探测
    assert health_score(Quota(probe_failed=True)) is None               # 探测失败 ≠ 耗尽
    assert health_score(Quota(remaining=0.0, total=0.0)) == EXHAUSTED
    assert health_score(Quota(remaining=50.0, total=100.0)) == 50
    assert health_score(Quota(remaining=150.0, total=100.0)) == 100     # 夹紧


def test_event_defaults():
    e = Event(kind=EventKind.CONTENT, content="hi")
    assert e.usage is None and e.tool_calls is None


# ----------------------------------------------------------------- 调度器

NOW = 1_000_000


def cand(cid="c1", **kw):
    base = dict(credential_id=cid, provider="trae")
    base.update(kw)
    return Candidate(**base)


def test_select_prefers_pinned():
    s = Scheduler()
    pool = [cand("a", health=90), cand("b", health=10, pinned=True)]
    assert s.select(pool, set(), NOW) == "b"


def test_select_skips_pinned_when_not_selectable():
    s = Scheduler()
    pool = [cand("a", health=90), cand("b", health=10, pinned=True, disabled=True)]
    assert s.select(pool, set(), NOW) == "a"


def test_select_known_desc_then_unknown_then_exhausted():
    s = Scheduler()
    pool = [cand("unknown", health=None), cand("dead", health=EXHAUSTED),
            cand("low", health=10), cand("high", health=80)]
    assert s.select(pool, set(), NOW) == "high"
    assert s.select([c for c in pool if c.credential_id != "high"], set(), NOW) == "low"
    assert s.select([c for c in pool if c.credential_id not in ("high", "low")], set(),
                    NOW) == "unknown"
    assert s.select([cand("dead", health=EXHAUSTED)], set(), NOW) == "dead"


def test_select_excludes_tried_and_disabled_and_cooling():
    s = Scheduler()
    pool = [cand("a", health=5), cand("b", health=9),
            cand("c", health=99, cooling_until=NOW + 10),
            cand("d", health=98, enabled=False), cand("e", health=97, disabled=True)]
    assert s.select(pool, set(), NOW) == "b"
    assert s.select(pool, {"b"}, NOW) == "a"
    assert s.select(pool, {"a", "b"}, NOW) is None


def test_select_returns_none_when_empty():
    assert Scheduler().select([], set(), NOW) is None


def test_should_rotate_respects_max():
    s = Scheduler(max_rotate=3)
    assert s.should_rotate(set())
    assert s.should_rotate({"a", "b"})
    assert not s.should_rotate({"a", "b", "c"})


def test_note_error_dead_disables():
    out = Scheduler().note_error(cand(), ErrKind.DEAD, NOW)
    assert out.disabled and out.cooling_until is None and out.err_count == 0


def test_note_error_plan_long_cooldown():
    out = Scheduler().note_error(cand(), ErrKind.PLAN, NOW)
    assert out.cooling_until == NOW + PLAN_COOLDOWN_SECONDS and not out.disabled


def test_note_error_soft_short_cooldown_no_accumulate():
    s = Scheduler()
    out = s.note_error(cand(err_count=2), ErrKind.SOFT, NOW)
    assert out.cooling_until == NOW + SOFT_COOLDOWN_SECONDS
    assert out.err_count == 2          # 不累计（防雪崩）


def test_note_error_other_accumulates_then_cools():
    s = Scheduler(err_threshold=3)
    out1 = s.note_error(cand(err_count=0), ErrKind.OTHER, NOW)
    out2 = s.note_error(cand(err_count=1), ErrKind.OTHER, NOW)
    assert out1.err_count == 1 and out1.cooling_until is None
    assert out2.err_count == 2 and out2.cooling_until is None
    out3 = s.note_error(cand(err_count=2), ErrKind.OTHER, NOW)
    assert out3.err_count == 0 and out3.cooling_until == NOW + 10 * 60


def test_note_success_clears_errors():
    s = Scheduler()
    assert s.note_success(cand(err_count=2)).err_count == 0
    clean = cand(err_count=0)
    assert s.note_success(clean) is clean     # 无变化时返回原对象


def test_scheduler_end_to_end_rotation():
    """一次请求内：换号 → 冷却 → 再选不中已冷却的号。"""
    s = Scheduler()
    a, b = cand("a", health=80), cand("b", health=20)
    tried: set[str] = set()
    first = s.select([a, b], tried, NOW)
    tried.add(first)
    out = s.note_error(a if first == "a" else b, ErrKind.PLAN, NOW)
    cooled = a if first == "a" else b
    cooled = Candidate(**{**cooled.__dict__, "cooling_until": out.cooling_until})
    others = [c for c in (a, b) if c.credential_id != first]
    assert s.select(others, tried, NOW) == others[0].credential_id
    assert not cooled.is_selectable(NOW + 60)
    assert cooled.is_selectable(NOW + PLAN_COOLDOWN_SECONDS + 1)


def test_candidate_is_selectable_boundaries():
    c = cand(cooling_until=NOW + 1)
    assert not c.is_selectable(NOW)
    assert c.is_selectable(NOW + 2)
