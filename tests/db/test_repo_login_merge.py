"""重新登录合并：按上游账号身份找到既有凭证并就地更新。"""

from __future__ import annotations

from src.db.crypto import CredentialCipher
from src.engine.scheduler import ErrorOutcome, ModelCooldown
from src.provider.login_identity import login_identities_for


def _identity(data: dict) -> frozenset[str]:
    uid = data.get("uid")
    return frozenset({str(uid)}) if uid else frozenset()


def _codearts_aliases(data: dict) -> frozenset[str]:
    return login_identities_for("codearts", data)


def _add(credentials, *, uid: str, disabled: int = 0, pinned: int = 0,
         nickname: str = "", token: str = "old") -> str:
    cid = credentials.add(provider="codearts",
                          credential_data={"uid": uid, "token": token},
                          nickname=nickname)
    if disabled or pinned:
        with credentials._db.transaction() as conn:   # noqa: SLF001 - 测试直写状态
            conn.execute("UPDATE credentials SET disabled = ?, pinned = ? WHERE id = ?",
                         (disabled, pinned, cid))
    return cid


def test_find_login_credential_matches_identity_and_skips_bad_rows(repo):
    """身份相同才命中；密文坏掉的行跳过，不能让一条坏数据挡住登录。"""
    credentials, db = repo
    good = _add(credentials, uid="u1")
    _add(credentials, uid="u2")
    # 一条解不开的凭证：换密钥加密，模拟 APP_SECRET 变更 / 密文损坏
    broken = _add(credentials, uid="u1", token="broken")
    other = CredentialCipher("a-completely-different-secret")
    with db.transaction() as conn:
        conn.execute("UPDATE credentials SET data_enc = ? WHERE id = ?",
                     (other.encrypt(b'{"uid": "u1"}'), broken))

    assert credentials.find_login_credential("codearts", _identity, {"u1"}) == good
    assert credentials.find_login_credential("codearts", _identity, {"u3"}) is None
    # 空目标 = 没有可合并的身份，直接不猜
    assert credentials.find_login_credential("codearts", _identity, frozenset()) is None
    # 别的渠道不参与匹配
    assert credentials.find_login_credential("officeace", _identity, {"u1"}) is None


def test_find_login_credential_matches_any_alias_of_the_same_account(repo):
    """老行只有 uid、新登录只有兜底键 → 仍命中（否则会新增重复凭证）。

    身份补全接口（caller-identity / current-user）尽力而为，网络失败只记日志，
    那次登录就只能给出 `domain_id|user_name`。单键比较必然错开，交集才合并。
    """
    credentials, _db = repo
    old = credentials.add(provider="codearts", credential_data={"uid": "u1"})
    aliases = {"u1", "d|n"}
    assert credentials.find_login_credential("codearts", _identity, aliases) == old

    # 反向：新登录只有兜底键，老行同时有两个别名
    newer = credentials.add(provider="codearts",
                            credential_data={"uid": "u2", "domain_id": "d",
                                             "user_name": "n"})
    assert credentials.find_login_credential(
        "codearts", _codearts_aliases, {"d|n"}) == newer


def test_find_login_credential_prefers_disabled_then_pinned_then_oldest(repo):
    """历史重复凭证的选取顺序：disabled（本次重登要救的）→ pinned → 最早。"""
    credentials, _db = repo
    oldest = _add(credentials, uid="u1")
    pinned = _add(credentials, uid="u1", pinned=1)
    disabled = _add(credentials, uid="u1", disabled=1)

    assert credentials.find_login_credential("codearts", _identity, {"u1"}) == disabled

    # 无 disabled → pinned
    credentials.revive(disabled)
    assert credentials.find_login_credential("codearts", _identity, {"u1"}) == pinned

    # 都普通 → 最早创建的一条（统计流水挂在这个 id 上）
    with credentials._db.transaction() as conn:   # noqa: SLF001 - 测试直写状态
        conn.execute("UPDATE credentials SET pinned = 0 WHERE id = ?", (pinned,))
    assert credentials.find_login_credential("codearts", _identity, {"u1"}) == oldest


def test_find_login_credential_identity_raising_row_is_skipped(repo):
    """身份解析抛错的行跳过，不让一条畸形数据挡住整次登录。"""
    credentials, _db = repo

    def identity(data: dict) -> frozenset[str]:
        if data.get("bad"):
            raise ValueError("malformed credential")
        uid = data.get("uid")
        return frozenset({str(uid)}) if uid else frozenset()

    credentials.add(provider="codearts", credential_data={"bad": True})
    credentials.add(provider="codearts", credential_data={"uid": "u1"})
    assert credentials.find_login_credential("codearts", identity, {"u1"}) is not None


def test_merge_login_credential_replaces_data_and_clears_failure_state(repo):
    """合并只换凭证内容 + 解除失败状态；运维字段与用户暂停状态必须保留。"""
    credentials, db = repo
    cid = credentials.add(provider="codearts",
                          credential_data={"uid": "u1", "token": "old"},
                          nickname="old-name", added_by="root",
                          tags=["keep"])
    credentials.set_enabled(cid, False)
    credentials.mark_relogin_required(cid, "refresh token revoked")
    credentials.save_error(cid, ErrorOutcome(cooling_until=9999, cooling_kind="other"))
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=9999, hits=1, reason="model")}))
    with db.transaction() as conn:
        conn.execute("UPDATE credentials SET err_count = 3 WHERE id = ?", (cid,))

    assert credentials.merge_login_credential(
        cid, {"uid": "u1", "token": "new"}, "new-name") is True

    assert credentials.credential_data(cid) == {"uid": "u1", "token": "new"}
    row = db.connect().execute(
        "SELECT nickname, enabled, disabled, disabled_reason, cooling_until, "
        "cooling_kind, err_count, added_by, tags FROM credentials WHERE id = ?",
        (cid,)).fetchone()
    assert row["nickname"] == "new-name"
    assert row["enabled"] == 0                     # 用户主动暂停保留
    assert row["disabled"] == 0 and row["disabled_reason"] is None
    # 本地 OTHER 冷却（非上游点名）照旧清除，与 revive(force=False) 同口径
    assert row["cooling_until"] is None and row["cooling_kind"] is None
    assert row["err_count"] == 0
    assert row["added_by"] == "root" and row["tags"] == "keep"
    assert credentials.model_cooldowns_for(cid, now=1000) == {}

    # 昵称为空时保留旧值，不能把展示名清空
    assert credentials.merge_login_credential(
        cid, {"uid": "u1", "token": "newer"}, "") is True
    assert credentials.credential_data(cid)["token"] == "newer"
    assert db.connect().execute(
        "SELECT nickname FROM credentials WHERE id = ?", (cid,)).fetchone()[0] == "new-name"


def test_merge_login_credential_keeps_upstream_named_cooldown(repo):
    """上游点名的冷却（429/余额/权益/风控）重登不清除。

    重登换的是凭证，不是配额：2026-11-03 实测「恢复顺手解掉刚写入的 429 冷却
    → 下一个请求立刻再撞同一个 429」，重复重登本身就是上游封号的自动化信号。
    `revive(force=False)` 已经保留这几类，重登必须同口径。
    """
    credentials, db = repo
    cid = credentials.add(provider="codearts", credential_data={"uid": "u1"})

    credentials.save_error(cid, ErrorOutcome(cooling_until=9999, cooling_kind="soft"))
    assert credentials.merge_login_credential(cid, {"uid": "u1"}, "n") is True
    row = db.connect().execute(
        "SELECT cooling_until, cooling_kind FROM credentials WHERE id = ?",
        (cid,)).fetchone()
    assert row["cooling_until"] == 9999 and row["cooling_kind"] == "soft"
    # 硬禁用仍然解除：冷却留着，但「需重新登录」这一条已经解决了
    assert db.connect().execute(
        "SELECT disabled FROM credentials WHERE id = ?", (cid,)).fetchone()[0] == 0


def test_merge_login_credential_missing_row_returns_false(repo):
    credentials, _db = repo
    assert credentials.merge_login_credential(
        "cred_missing", {"uid": "u1"}, "n") is False
