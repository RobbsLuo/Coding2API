"""凭证与 API Key 的持久化访问（手写 SQL，T-Q2）。

凭证内容以 Fernet 密文入库；解密只发生在引擎侧，provider 不接触数据库。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable
from typing import Any

from ..auth.api_key import digest_api_key, generate_api_key, preview_api_key
from ..db.crypto import CredentialCipher
from ..engine.scheduler import Candidate, ErrorOutcome, expiring_credits
from ..provider.base import Quota, health_score


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _ladder_text(ladder: list[tuple[int, float]] | None) -> str | None:
    """到期阶梯落库：[[epoch, 剩余积分], ...]；无到期信息的渠道写 NULL。"""
    if not ladder:
        return None
    return json.dumps([[end, remaining] for end, remaining in ladder])


def _ladder_value(text: str | None) -> list[tuple[int, float]] | None:
    """读回到期阶梯。缺失、迁移前的空值或历史脏数据一律当「无周期概念」，
    不能让一行坏数据把整个选号流程拖崩。"""
    if not text:
        return None
    try:
        items = json.loads(text)
    except ValueError:
        return None
    ladder: list[tuple[int, float]] = []
    for item in items:
        if isinstance(item, list) and len(item) == 2:
            ladder.append((int(item[0]), float(item[1])))
    return ladder


class CredentialRepository:
    def __init__(self, db, cipher: CredentialCipher) -> None:
        self._db = db
        self._cipher = cipher

    # ------------------------------------------------------------- 写入

    def add(self, *, provider: str, credential_data: dict, nickname: str = "",
            added_by: str = "", now: int | None = None) -> str:
        credential_id = _new_id("cred")
        payload = json.dumps(credential_data, ensure_ascii=False).encode("utf-8")
        self._db.connect().execute(
            "INSERT INTO credentials (id, provider, nickname, data_enc, created_at, added_by) "
            "VALUES (?,?,?,?,?,?)",
            (credential_id, provider, nickname, self._cipher.encrypt(payload),
             int(now if now is not None else time.time()), added_by),
        )
        self._db.connect().commit()
        return credential_id

    def delete(self, credential_id: str) -> bool:
        cursor = self._db.connect().execute("DELETE FROM credentials WHERE id = ?",
                                           (credential_id,))
        self._db.connect().commit()
        return cursor.rowcount > 0

    def set_enabled(self, credential_id: str, enabled: bool) -> bool:
        cursor = self._db.connect().execute(
            "UPDATE credentials SET enabled = ? WHERE id = ?", (1 if enabled else 0, credential_id))
        self._db.connect().commit()
        return cursor.rowcount > 0

    def set_pinned(self, credential_id: str | None) -> None:
        conn = self._db.connect()
        conn.execute("UPDATE credentials SET pinned = 0")
        if credential_id is not None:
            conn.execute("UPDATE credentials SET pinned = 1 WHERE id = ?", (credential_id,))
        conn.commit()

    def revive(self, credential_id: str) -> bool:
        """解除硬禁用（session 死亡）与冷却，允许凭证重新参与调度。

        没有这个入口时，凭证一旦因 session 失效被硬禁用就只能删除重建，
        重新登录后也无法复用同一条记录。
        """
        cursor = self._db.connect().execute(
            "UPDATE credentials SET disabled = 0, disabled_reason = NULL, cooling_until = NULL, "
            "err_count = 0 WHERE id = ?", (credential_id,))
        self._db.connect().commit()
        return cursor.rowcount > 0

    def save_error(self, credential_id: str, outcome: ErrorOutcome) -> None:
        conn = self._db.connect()
        if outcome.disabled:
            conn.execute(
                "UPDATE credentials SET disabled = 1, disabled_reason = ?, err_count = 0, "
                "cooling_until = NULL WHERE id = ?", ("session dead", credential_id))
        else:
            conn.execute(
                "UPDATE credentials SET cooling_until = ?, err_count = ? WHERE id = ?",
                (outcome.cooling_until, outcome.err_count, credential_id))
        conn.commit()

    def save_success(self, credential_id: str) -> None:
        conn = self._db.connect()
        conn.execute("UPDATE credentials SET err_count = 0 WHERE id = ?", (credential_id,))
        conn.commit()

    def save_credential_data(self, credential_id: str, credential_data: dict) -> None:
        payload = json.dumps(credential_data, ensure_ascii=False).encode("utf-8")
        conn = self._db.connect()
        conn.execute("UPDATE credentials SET data_enc = ? WHERE id = ?",
                     (self._cipher.encrypt(payload), credential_id))
        conn.commit()

    def save_quota(self, credential_id: str, quota: Quota) -> None:
        conn = self._db.connect()
        conn.execute(
            "UPDATE credentials SET quota_remaining = ?, quota_total = ?, quota_cycle_end = ?, "
            "quota_expiry_ladder = ?, quota_probed_at = ?, health = ? WHERE id = ?",
            (quota.remaining, quota.total, quota.cycle_end,
             _ladder_text(quota.expiry_ladder), quota.probed_at,
             health_score(quota), credential_id),
        )
        conn.commit()

    def mark_probe_failed(self, credential_id: str, now: int | None = None) -> None:
        conn = self._db.connect()
        conn.execute("UPDATE credentials SET quota_probed_at = ?, health = NULL WHERE id = ?",
                     (int(now if now is not None else time.time()), credential_id))
        conn.commit()

    # ------------------------------------------------------------- 读取

    def candidates(self, providers: Iterable[str] | None = None,
                   *, selectable_only: bool = False) -> list[Candidate]:
        """列出候选凭证。

        selectable_only=True 时排除硬禁用（session 死亡）与用户软关闭的凭证：
        它们不会被调度器选中，用它们去拉模型列表/探测只会白白失败。
        默认 False：签到、刷新等任务需要看到全部凭证才能正确计数 skipped。
        """
        rows = self._db.connect().execute(
            "SELECT id, provider, health, cooling_until, disabled, enabled, err_count, pinned, "
            "quota_cycle_end, quota_expiry_ladder FROM credentials").fetchall()
        allowed = set(providers) if providers is not None else None
        result = [
            Candidate(
                credential_id=row["id"], provider=row["provider"], health=row["health"],
                cooling_until=row["cooling_until"], disabled=bool(row["disabled"]),
                enabled=bool(row["enabled"]), err_count=row["err_count"],
                pinned=bool(row["pinned"]), cycle_end=row["quota_cycle_end"],
                expiry_ladder=_ladder_value(row["quota_expiry_ladder"]),
            )
            for row in rows if allowed is None or row["provider"] in allowed
        ]
        if selectable_only:
            result = [c for c in result if c.enabled and not c.disabled]
        return result

    def provider_of(self, credential_id: str) -> str | None:
        row = self._db.connect().execute(
            "SELECT provider FROM credentials WHERE id = ?", (credential_id,)).fetchone()
        return row["provider"] if row else None

    def credential_data(self, credential_id: str) -> dict[str, Any] | None:
        row = self._db.connect().execute(
            "SELECT data_enc FROM credentials WHERE id = ?", (credential_id,)).fetchone()
        if row is None:
            return None
        plaintext = self._cipher.decrypt(row["data_enc"])
        return json.loads(plaintext.decode("utf-8"))

    def list_all(
        self, *, expiring_window: int = 0, now: int | None = None,
    ) -> list[dict[str, Any]]:
        """管理台列表：绝不返回明文凭证。

        附 `quota_expiring_credits`（窗口内即将到期的积分，与调度排序同源口径）；
        渠道无到期信息（TRAE）为 None，展示层据此隐藏该行。
        """
        now = int(now or time.time())
        rows = self._db.connect().execute(
            "SELECT id, provider, nickname, enabled, disabled, disabled_reason, pinned, "
            "health, cooling_until, err_count, quota_remaining, quota_total, quota_cycle_end, "
            "quota_probed_at, created_at, added_by, quota_expiry_ladder "
            "FROM credentials ORDER BY created_at").fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            rec = dict(row)
            ladder = _ladder_value(row["quota_expiry_ladder"])
            rec.pop("quota_expiry_ladder", None)
            rec["quota_expiring_credits"] = (
                None if ladder is None else expiring_credits(ladder, expiring_window, now))
            out.append(rec)
        return out


class ApiKeyRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, username: str, name: str = "", now: int | None = None) -> dict[str, Any]:
        plaintext = generate_api_key()
        key_id = _new_id("key")
        created_at = int(now if now is not None else time.time())
        self._db.connect().execute(
            "INSERT INTO api_keys (id, username, name, key_digest, preview, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (key_id, username, name, digest_api_key(plaintext), preview_api_key(plaintext),
             created_at),
        )
        self._db.connect().commit()
        return {"id": key_id, "username": username, "name": name, "api_key": plaintext,
                "preview": preview_api_key(plaintext), "created_at": created_at}

    def verify(self, api_key: str) -> str | None:
        digest = digest_api_key(api_key)
        row = self._db.connect().execute(
            "SELECT id, username FROM api_keys WHERE key_digest = ?", (digest,)).fetchone()
        if row is None:
            return None
        self._db.connect().execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                                   (int(time.time()), row["id"]))
        self._db.connect().commit()
        return row["username"]

    def list_for(self, username: str) -> list[dict[str, Any]]:
        rows = self._db.connect().execute(
            "SELECT id, username, name, preview, created_at, last_used_at FROM api_keys "
            "WHERE username = ? ORDER BY created_at", (username,)).fetchall()
        return [dict(row) for row in rows]

    def delete(self, key_id: str, username: str) -> bool:
        cursor = self._db.connect().execute(
            "DELETE FROM api_keys WHERE id = ? AND username = ?", (key_id, username))
        self._db.connect().commit()
        return cursor.rowcount > 0
