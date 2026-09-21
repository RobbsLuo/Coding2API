"""M0 骨架测试：配置、加密、用户、会话、API Key、调度器（100% 目标）。"""

from __future__ import annotations

import base64
import time

import pytest

from src.auth import api_key
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
    BLOCKED_BASE_SECONDS,
    BLOCKED_MAX_SECONDS,
    EXPIRY_WINDOW_SECONDS,
    MODEL_COOLDOWN_MAX_SECONDS,
    MODEL_COOLDOWN_SECONDS,
    PLAN_COOLDOWN_SECONDS,
    SECONDARY_EXPIRY_WINDOW_SECONDS,
    SOFT_COOLDOWN_SECONDS,
    Candidate,
    ModelCooldown,
    Scheduler,
    next_credit_reset,
)
from src.provider.base import (
    EXHAUSTED,
    ErrKind,
    Event,
    EventKind,
    Quota,
    health_score,
)
from tests.conftest import SECRET

# --------------------------------------------------------------------- 配置

BASE_ENV = {"APP_SECRET": "test-secret"}


def test_settings_defaults_and_admin_set():
    s = Settings(_env_file=None, APP_SECRET=SECRET, ADMIN_USERNAMES="alice, bob ,")
    assert s.admin_set == frozenset({"alice", "bob"})
    assert s.default_model == "glm-5.2"
    assert s.quota_expiry_window_seconds == EXPIRY_WINDOW_SECONDS
    assert s.quota_expiry_secondary_window_seconds == SECONDARY_EXPIRY_WINDOW_SECONDS
    assert s.is_admin("alice") and not s.is_admin("carol")


def test_settings_requires_app_secret():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_allowed_endpoints_whitelist():
    s = Settings(_env_file=None, APP_SECRET=SECRET)
    assert validate_endpoint_allowed("https://copilot.tencent.com", s)
    assert validate_endpoint_allowed("https://www.codebuddy.ai", s)
    assert not validate_endpoint_allowed("https://evil.example", s)


def test_db_path_joins_data_dir():
    s = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR="/tmp/cbd")
    assert s.db_path == "/tmp/cbd/coding2api.sqlite3"


# --------------------------------------------------------------------- 加密

def test_cipher_roundtrip():
    cipher = CredentialCipher("secret-a-0123456789")
    token = cipher.encrypt(b'{"uid": "1"}')
    assert cipher.decrypt(token) == b'{"uid": "1"}'


def test_cipher_wrong_secret_raises():
    token = CredentialCipher("secret-a-0123456789").encrypt(b"x")
    with pytest.raises(CredentialDecryptError):
        CredentialCipher("secret-b-0123456789").decrypt(token)


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
    key = api_key.generate_api_key()
    assert key.startswith("sk-")
    assert api_key.digest_api_key(key) == api_key.digest_api_key(key)
    assert api_key.preview_api_key(key).startswith("sk-…")


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


def test_select_prefers_more_expiring_credits_over_health():
    """到期积分多者优先：低健康度但快过期的号，胜过健康的高健康度号。"""
    steady = cand("steady", health=95)
    burning = cand("burn", health=10, expiry_ladder=[(NOW + 600, 100.0)])
    assert Scheduler().select([steady, burning], set(), NOW) == "burn"


def test_select_expiry_credits_desc_then_health():
    """到期积分降序；相同的再按健康度降序。"""
    pool = [cand("few", health=90, expiry_ladder=[(NOW + 1000, 50.0)]),
            cand("many", health=20, expiry_ladder=[(NOW + 1000, 100.0),
                                                    (NOW + 2000, 100.0)]),
            cand("tied", health=80, expiry_ladder=[(NOW + 2000, 150.0)])]
    assert Scheduler().select(pool, set(), NOW) == "many"
    assert Scheduler().select([p for p in pool if p.credential_id != "many"],
                              set(), NOW) == "tied"


def test_select_expiry_window_boundaries():
    """恰好窗口内算到期；超过窗口或已过期（探测滞后）都不计入。"""
    steady = cand("steady", health=95)
    at_edge = cand("at_edge", health=5, expiry_ladder=[(NOW + EXPIRY_WINDOW_SECONDS, 100.0)])
    # 「超过主窗口」在 36h 外仍落在次窗口（7 天）内，所以按次窗口计分优先于健康度
    beyond = cand("beyond", health=5,
                  expiry_ladder=[(NOW + EXPIRY_WINDOW_SECONDS + 1, 100.0)])
    past_secondary = cand("far", health=5, expiry_ladder=[
        (NOW + SECONDARY_EXPIRY_WINDOW_SECONDS + 1, 100.0)])
    stale = cand("stale", health=5, expiry_ladder=[(NOW - 1, 100.0)])
    assert Scheduler().select([steady, at_edge], set(), NOW) == "at_edge"
    assert Scheduler().select([steady, beyond], set(), NOW) == "beyond"
    assert Scheduler().select([steady, past_secondary], set(), NOW) == "steady"
    assert Scheduler().select([steady, stale], set(), NOW) == "steady"
    # 只按落在窗口内的包累加，窗口外的包不计
    mixed = cand("mixed", health=5, expiry_ladder=[(NOW + 600, 100.0),
                                                    (NOW + 2 * EXPIRY_WINDOW_SECONDS, 900.0)])
    assert mixed.expiry_credits(NOW, EXPIRY_WINDOW_SECONDS) == 100.0


def test_select_without_expiry_ladder_keeps_health_order():
    """无周期信息（TRAE、企业版）计 0 分，退回健康度排序。"""
    assert Scheduler().select([cand("a", health=10), cand("b", health=90)],
                              set(), NOW) == "b"
    assert cand("empty", expiry_ladder=[]).expiry_credits(NOW, EXPIRY_WINDOW_SECONDS) == 0.0


def test_select_expiry_window_zero_disables_metric():
    s = Scheduler(expiry_window=0)
    assert s.select([cand("steady", health=95),
                     cand("burn", health=5,
                          expiry_ladder=[(NOW + 600, 100.0)])], set(), NOW) == "steady"
    assert cand("burn", expiry_ladder=[(NOW + 600, 100.0)]).expiry_credits(NOW, 0) == 0.0
    # 主窗口是总开关：关掉它时次窗口一并失效，不能偷偷用 7 天窗口排序
    assert s.secondary_expiry_window == 0
    week = cand("week", health=5, expiry_ladder=[(NOW + 3 * 86400, 100.0)])
    assert s.select([cand("steady", health=95), week], set(), NOW) == "steady"


def test_select_secondary_expiry_breaks_primary_tie():
    """两级字典序：36h 打平（都为 0）时，比 7 天内会过期的积分，多者优先。"""
    week = cand("week", health=50,
                expiry_ladder=[(NOW + 3 * 86400, 200.0)])       # 36h 外、7 天内
    steady = cand("steady", health=95)
    assert Scheduler().select([steady, week], set(), NOW) == "week"


def test_select_primary_expiry_wins_over_secondary():
    """主窗口优先于次窗口：36h 内有 1 分也胜过 7 天内 1000 分。"""
    soon = cand("soon", health=5, expiry_ladder=[(NOW + 600, 1.0)])
    week = cand("week", health=95, expiry_ladder=[(NOW + 3 * 86400, 1000.0)])
    assert Scheduler().select([week, soon], set(), NOW) == "soon"


def test_select_secondary_window_boundaries_and_zero_disables():
    """次窗口边界：恰好 7 天计入，超过不计入，已过期不计入。"""
    steady = cand("steady", health=95)
    at_edge = cand("at_edge", health=5, expiry_ladder=[
        (NOW + SECONDARY_EXPIRY_WINDOW_SECONDS, 100.0)])
    beyond = cand("beyond", health=5, expiry_ladder=[
        (NOW + SECONDARY_EXPIRY_WINDOW_SECONDS + 1, 100.0)])
    stale = cand("stale", health=5, expiry_ladder=[(NOW - 1, 100.0)])
    assert Scheduler().select([steady, at_edge], set(), NOW) == "at_edge"
    assert Scheduler().select([steady, beyond], set(), NOW) == "steady"
    assert Scheduler().select([steady, stale], set(), NOW) == "steady"
    # 二级窗口同样可关闭（≤0）：关闭后退回健康度排序
    s = Scheduler(secondary_expiry_window=0)
    burning_week = cand("week", health=5, expiry_ladder=[(NOW + 3 * 86400, 100.0)])
    assert s.select([steady, burning_week], set(), NOW) == "steady"


def test_select_secondary_tie_falls_back_to_health():
    """两级都打平才比健康度，再过才按 credential_id 稳定。"""
    week_a = cand("a", health=10, expiry_ladder=[(NOW + 3 * 86400, 100.0)])
    week_b = cand("b", health=90, expiry_ladder=[(NOW + 3 * 86400, 100.0)])
    assert Scheduler().select([week_a, week_b], set(), NOW) == "b"


def test_select_pinned_still_wins_over_expiry_metric():
    """pin 是显式指定，优先级高于到期积分。"""
    pinned = cand("pinned", health=90, pinned=True)
    burning = cand("burn", health=5, expiry_ladder=[(NOW + 600, 100.0)])
    assert Scheduler().select([burning, pinned], set(), NOW) == "pinned"


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


# ------------------------------------------- 校验 1：模型级冷却与新增错误类

def _with_model_cooldown(candidate, model, *, cooling_until, hits=1, reason="model"):
    return Candidate(**{**candidate.__dict__,
                        "model_cooldowns": {model: ModelCooldown(
                            cooling_until=cooling_until, hits=hits, reason=reason)}})


def test_model_cooldown_only_blocks_that_model():
    """模型级冷却不影响同凭证的其他模型（6004 的核心语义）。"""
    cooled = _with_model_cooldown(cand(), "glm-5.2", cooling_until=NOW + 100)
    assert not cooled.is_selectable(NOW, "glm-5.2")
    assert cooled.is_selectable(NOW, "kimi-k2")
    assert cooled.is_selectable(NOW, None)          # 不传模型：只校验账号级
    # 冷却到期后可再用
    assert cooled.is_selectable(NOW + 101, "glm-5.2")


def test_model_cooling_until_lookup_edges():
    assert cand().model_cooling_until("glm-5.2") is None      # 无冷却表
    assert cand().model_cooling_until(None) is None           # 无模型名
    cooled = _with_model_cooldown(cand(), "glm-5.2", cooling_until=NOW + 5)
    assert cooled.model_cooling_until("glm-5.2") == NOW + 5
    assert cooled.model_cooling_until("other") is None        # 表里没有该模型


def test_model_cooldown_first_hit_uses_base_duration():
    s = Scheduler()
    out = s.note_error(cand(), ErrKind.MODEL, NOW, model="glm-5.2")
    entry = out.model_cooldowns["glm-5.2"]
    assert (entry.cooling_until, entry.hits, entry.reason) == (
        NOW + MODEL_COOLDOWN_SECONDS, 1, "model")
    # 模型级条目不得顺带写账号级冷却
    assert out.cooling_until is None and not out.disabled


def test_model_cooldown_escalates_and_caps():
    s = Scheduler()
    existing = _with_model_cooldown(cand(), "m", cooling_until=NOW, hits=1)
    out = s.note_error(existing, ErrKind.MODEL, NOW, model="m")
    assert out.model_cooldowns["m"].cooling_until == NOW + 2 * MODEL_COOLDOWN_SECONDS
    # hits 很大时封顶，不能无限翻倍
    huge = _with_model_cooldown(cand(), "m", cooling_until=NOW, hits=99)
    capped = s.note_error(huge, ErrKind.MODEL, NOW, model="m")
    assert capped.model_cooldowns["m"].cooling_until == NOW + MODEL_COOLDOWN_MAX_SECONDS


def test_model_cooldown_without_model_degrades_to_soft():
    """流内错误没带模型名时不能写孤儿记录，退化为账号级软冷却。"""
    out = Scheduler().note_error(cand(), ErrKind.MODEL, NOW, model=None)
    assert out.cooling_until == NOW + SOFT_COOLDOWN_SECONDS
    assert out.model_cooldowns is None


def test_blocked_backoff_escalates_and_caps():
    s = Scheduler()
    first = s.note_error(cand(), ErrKind.BLOCKED, NOW, model="m")
    assert first.model_cooldowns["m"].cooling_until == NOW + BLOCKED_BASE_SECONDS
    assert first.model_cooldowns["m"].reason == "blocked"
    second = s.note_error(
        _with_model_cooldown(cand(), "m", cooling_until=NOW, hits=1, reason="blocked"),
        ErrKind.BLOCKED, NOW, model="m")
    assert second.model_cooldowns["m"].cooling_until == NOW + 2 * BLOCKED_BASE_SECONDS
    huge = s.note_error(
        _with_model_cooldown(cand(), "m", cooling_until=NOW, hits=99, reason="blocked"),
        ErrKind.BLOCKED, NOW, model="m")
    assert huge.model_cooldowns["m"].cooling_until == NOW + BLOCKED_MAX_SECONDS


def test_model_cooldown_switching_reason_restarts_hits():
    """限流 与 negative cache 的退避基数不同：换原因必须重新计数。"""
    s = Scheduler()
    limited = _with_model_cooldown(cand(), "m", cooling_until=NOW, hits=3, reason="model")
    out = s.note_error(limited, ErrKind.BLOCKED, NOW, model="m")
    assert out.model_cooldowns["m"] == ModelCooldown(
        cooling_until=NOW + BLOCKED_BASE_SECONDS, hits=1, reason="blocked")


def test_note_error_request_kind_is_zero_action():
    """请求级错误不冷却、不累计（累计到阈值同样会冷却，等于变相惩罚）。"""
    out = Scheduler().note_error(cand(err_count=2), ErrKind.REQUEST, NOW)
    assert (out.cooling_until, out.err_count, out.disabled) == (None, 2, False)
    assert out.model_cooldowns is None


def test_note_error_credit_cools_until_next_4am():
    """余额不足：冷却到次日签到时刻，而不是固定 12h。"""
    out = Scheduler().note_error(cand(), ErrKind.CREDIT, NOW)
    assert out.cooling_until == next_credit_reset(NOW)
    assert out.cooling_until > NOW and out.err_count == 0


@pytest.mark.parametrize(("hour", "expected_offset_days"), [
    (0, 0),     # 凌晨 00:00：等当天签到，不必跨天
    (3, 0),
    (4, 1),     # 04:00 起算次日
    (23, 1),
])
def test_next_credit_reset_boundaries(hour, expected_offset_days):
    local = time.localtime(NOW)
    today_4am = int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                 4, 0, 0, 0, 0, -1)))
    now_at_hour = today_4am + (hour - 4) * 3600
    expected = today_4am + expected_offset_days * 86400
    assert next_credit_reset(now_at_hour) == expected


def test_select_skips_model_cooled_credential_when_filtered():
    """端到端：调用方按模型过滤后，被冷却的凭证不再被选中，换模型仍最优。"""
    s = Scheduler()
    a, b = cand("a", health=90), cand("b", health=10)
    cooled_a = _with_model_cooldown(a, "glm-5.2", cooling_until=NOW + 100)
    usable = [c for c in (cooled_a, b) if c.is_selectable(NOW, "glm-5.2")]
    assert [c.credential_id for c in usable] == ["b"]
    assert s.select(usable, set(), NOW) == "b"
    # 同一份候选换模型：a 的模型级冷却不生效，健康度更高者胜出
    usable = [c for c in (cooled_a, b) if c.is_selectable(NOW, "kimi-k2")]
    assert s.select(usable, set(), NOW) == "a"
