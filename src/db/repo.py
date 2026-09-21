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
from ..db.crypto import CredentialCipher, CredentialDecryptError
from ..engine.scheduler import (
    Candidate,
    ErrorOutcome,
    ModelCooldown,
    expiring_credits,
    expiry_windows,
)
from ..provider.base import Quota, health_score
from ..provider.token_expiry import credential_token_times


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


def _packages_text(packages: list[dict[str, Any]] | None) -> str | None:
    """额度包明细落库；无明细写 NULL。

    与 _ladder_text 分开：阶梯是选号指标，这里是纯展示数据（带包名）。
    """
    if not packages:
        return None
    return json.dumps(packages, ensure_ascii=False)


def _packages_value(text: str | None) -> list[dict[str, Any]] | None:
    """读回额度包明细。脏数据/历史空值一律当「无明细」，不让展示层崩掉。"""
    if not text:
        return None
    try:
        items = json.loads(text)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, dict)]


def _token_times_from_blob(
    data_enc: bytes | None, cipher: CredentialCipher,
) -> tuple[int, int]:
    """从凭证密文派生 `(签发, 到期)`；解密/解析失败返回 (0, 0)。

    老库升级后两列都为 NULL：本列写回前不做一次性回填（解密全池会拖慢启动），
    改在列表读到时按需派生。写回后（导入 / 预刷新 / 账号切换）直接走列值，
    不再解密，避免每次列表都付一次密码学开销。
    """
    if not data_enc:
        return 0, 0
    try:
        data = json.loads(cipher.decrypt(data_enc).decode("utf-8"))
    except (CredentialDecryptError, ValueError, UnicodeDecodeError):
        return 0, 0
    return credential_token_times(data) if isinstance(data, dict) else (0, 0)


class CredentialRepository:
    def __init__(self, db, cipher: CredentialCipher) -> None:
        self._db = db
        self._cipher = cipher

    # ------------------------------------------------------------- 写入

    def add(self, *, provider: str, credential_data: dict, nickname: str = "",
            added_by: str = "", now: int | None = None) -> str:
        credential_id = _new_id("cred")
        payload = json.dumps(credential_data, ensure_ascii=False).encode("utf-8")
        created = int(now if now is not None else time.time())
        issued_at, expires_at = credential_token_times(credential_data)
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO credentials (id, provider, nickname, data_enc, created_at, added_by, "
                "token_expires_at, token_issued_at) VALUES (?,?,?,?,?,?,?,?)",
                (credential_id, provider, nickname, self._cipher.encrypt(payload),
                 created, added_by, expires_at, issued_at),
            )
        return credential_id

    def delete(self, credential_id: str) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM credentials WHERE id = ?", (credential_id,))
            # 模型级冷却没有外键级联，凭证删除后必须显式清理，
            # 否则重建同 id 凭证会继承旧的模型冷却（残留行还会拖慢留存清理）
            conn.execute("DELETE FROM credential_model_cooldowns WHERE credential_id = ?",
                         (credential_id,))
        return cursor.rowcount > 0

    def set_enabled(self, credential_id: str, enabled: bool) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE credentials SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, credential_id))
        return cursor.rowcount > 0

    def set_pinned(self, credential_id: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE credentials SET pinned = 0")
            if credential_id is not None:
                conn.execute("UPDATE credentials SET pinned = 1 WHERE id = ?", (credential_id,))

    def revive(self, credential_id: str) -> bool:
        """解除硬禁用（session 死亡）与冷却，允许凭证重新参与调度。

        没有这个入口时，凭证一旦因 session 失效被硬禁用就只能删除重建，
        重新登录后也无法复用同一条记录。模型级冷却一并清空：管理员显式
        「恢复」的意图包含该凭证的全部冷却状态。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE credentials SET disabled = 0, disabled_reason = NULL, "
                "cooling_until = NULL, err_count = 0 WHERE id = ?", (credential_id,))
            conn.execute("DELETE FROM credential_model_cooldowns WHERE credential_id = ?",
                         (credential_id,))
        return cursor.rowcount > 0

    def save_error(self, credential_id: str, outcome: ErrorOutcome) -> None:
        """落库一次错误结果。

        `outcome.model_cooldowns` 非空表示模型级冷却：只写 (凭证, 模型) 表，
        不动账号级 cooling_until；反之账号级冷却会清空该凭证的模型级条目
        （账号级限流不允许被「切模型」绕过）。
        """
        with self._db.transaction() as conn:
            if outcome.model_cooldowns:
                for model, entry in outcome.model_cooldowns.items():
                    conn.execute(
                        "INSERT INTO credential_model_cooldowns "
                        "(credential_id, model, cooling_until, hits, reason) "
                        "VALUES (?,?,?,?,?) "
                        "ON CONFLICT(credential_id, model) DO UPDATE SET "
                        "cooling_until = excluded.cooling_until, hits = excluded.hits, "
                        "reason = excluded.reason",
                        (credential_id, model, entry.cooling_until, entry.hits,
                         entry.reason))
                return
            if outcome.disabled:
                conn.execute(
                    "UPDATE credentials SET disabled = 1, disabled_reason = ?, err_count = 0, "
                    "cooling_until = NULL WHERE id = ?", ("session dead", credential_id))
            else:
                conn.execute(
                    "UPDATE credentials SET cooling_until = ?, err_count = ? WHERE id = ?",
                    (outcome.cooling_until, outcome.err_count, credential_id))
            if outcome.cooling_until is not None:
                conn.execute("DELETE FROM credential_model_cooldowns WHERE credential_id = ?",
                             (credential_id,))

    def save_success(self, credential_id: str, *, model: str | None = None) -> None:
        """成功：清账号级错误累计；`model` 命中负缓存条目时一并清除。

        模型级**限流**（6004）冷却不清除——它对齐上游的重置墙钟，成功一次
        不代表限流已解除；只有 11102 负缓存（reason 以 blocked 标记）才清。
        """
        with self._db.transaction() as conn:
            conn.execute("UPDATE credentials SET err_count = 0 WHERE id = ?", (credential_id,))
            if model:
                conn.execute(
                    "DELETE FROM credential_model_cooldowns "
                    "WHERE credential_id = ? AND model = ? AND reason = ?",
                    (credential_id, model, "blocked"))

    def purge_expired_model_cooldowns(self, now: int | None = None) -> int:
        """删除已过期的 (凭证, 模型) 冷却行（留存任务每轮调用）。"""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM credential_model_cooldowns WHERE cooling_until <= ?",
                (int(now if now is not None else time.time()),))
        return cursor.rowcount

    def model_cooldowns_for(self, credential_id: str,
                            now: int | None = None) -> dict[str, int]:
        """该凭证仍在生效的模型级冷却（model → 截止 epoch），供管理台展示。"""
        rows = self._db.connect().execute(
            "SELECT model, cooling_until FROM credential_model_cooldowns "
            "WHERE credential_id = ? AND cooling_until > ?",
            (credential_id, int(now if now is not None else time.time()))).fetchall()
        return {row["model"]: row["cooling_until"] for row in rows}

    def save_credential_data(self, credential_id: str, credential_data: dict) -> None:
        """写回凭证 JSON，并同步 access token 的签发/到期时间。

        两个值都由凭证 JSON 派生（显式 `expires_at` 优先，回落 JWT 的 `iat`/`exp`）：
        上游没给到期信息时写 0（未知），管理台据此隐藏进度条，而不是显示假到期。

        为什么必须同时给签发时间：剩余天数会被刷新拉满，单看「还剩几天」会把
        「刚续期」读成「永远不会过期」；而且进度条需要一个满量程，只有 token
        自己的寿命（`exp - iat`）才是对的量纲，拿固定值会把 50 天的 token 画成
        永远满格。签发时间取 JWT 的 `iat`（上游不会单独回传），拿不到就是 0。
        """
        payload = json.dumps(credential_data, ensure_ascii=False).encode("utf-8")
        issued_at, expires_at = credential_token_times(credential_data)
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE credentials SET data_enc = ?, token_expires_at = ?, "
                "token_issued_at = ? WHERE id = ?",
                (self._cipher.encrypt(payload), expires_at, issued_at, credential_id))

    def save_quota(self, credential_id: str, quota: Quota) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE credentials SET quota_remaining = ?, quota_total = ?, "
                "quota_cycle_end = ?, quota_expiry_ladder = ?, quota_packages = ?, "
                "quota_probed_at = ?, health = ? "
                "WHERE id = ?",
                (quota.remaining, quota.total, quota.cycle_end,
                 _ladder_text(quota.expiry_ladder), _packages_text(quota.packages),
                 quota.probed_at,
                 health_score(quota), credential_id),
            )

    def mark_probe_failed(self, credential_id: str, now: int | None = None) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE credentials SET quota_probed_at = ?, health = NULL WHERE id = ?",
                         (int(now if now is not None else time.time()), credential_id))

    def save_growth_result(self, credential_id: str, report: str,
                           now: int | None = None) -> None:
        """记下成长中心最近一轮的汇报（列表页直接显示，不查 events 表）。"""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE credentials SET growth_last_run_at = ?, growth_last_result = ? "
                "WHERE id = ?",
                (int(now if now is not None else time.time()), report, credential_id))

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
        # 模型级冷却整表读一次（表很小：只在模型限流/负缓存时才有行），
        # 按凭证聚合后挂到 Candidate 上；每选一次号查一次库会抵消选号的开销优势。
        # reason 必须一起读：note_error 靠它判断「换了原因就重新计数」，
        # 漏读会让 blocked 的 hits 每次从 1 重来（6h 退避永远不升级）。
        cooling_rows = self._db.connect().execute(
            "SELECT credential_id, model, cooling_until, hits, reason "
            "FROM credential_model_cooldowns"
        ).fetchall()
        by_credential: dict[str, dict[str, ModelCooldown]] = {}
        for cooling in cooling_rows:
            by_credential.setdefault(cooling["credential_id"], {})[cooling["model"]] = (
                ModelCooldown(cooling_until=cooling["cooling_until"], hits=cooling["hits"],
                              reason=cooling["reason"]))
        result = [
            Candidate(
                credential_id=row["id"], provider=row["provider"], health=row["health"],
                cooling_until=row["cooling_until"], disabled=bool(row["disabled"]),
                enabled=bool(row["enabled"]), err_count=row["err_count"],
                pinned=bool(row["pinned"]), cycle_end=row["quota_cycle_end"],
                expiry_ladder=_ladder_value(row["quota_expiry_ladder"]),
                model_cooldowns=by_credential.get(row["id"]),
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
        self, *, expiring_window: int = 0, expiring_secondary_window: int = 0,
        now: int | None = None,
    ) -> list[dict[str, Any]]:
        """管理台列表：绝不返回明文凭证。

        附 `quota_expiring_credits`（主窗口内即将到期的积分，与调度排序第一级同源）；
        附 `quota_expiring_credits_secondary`（次窗口，主窗口打平时才参与排序的第二级）；
        附 `quota_expiry_ladder`（套餐到期阶梯，[[epoch, 剩余积分]]，仅 CodeBuddy）；
        渠道无到期信息（TRAE）三者都为 None，展示层据此隐藏。
        附 `model_cooldowns`（model → 截止 epoch），只在模型级限流/负缓存时非空。
        附 `token_expires_at` / `token_issued_at`（B3.3 token 到期展示）：只下发
        绝对 epoch，剩余时间与预警阈值判定交给展示层用同一个时钟现算——否则
        服务端算好的「剩余秒数」不会随页面 tick 更新，两处口径还会漂移。
        到期时间未知时为 0（展示层隐藏，绝不当成已过期）。
        """
        now = int(now or time.time())
        rows = self._db.connect().execute(
            "SELECT id, provider, nickname, enabled, disabled, disabled_reason, pinned, "
            "health, cooling_until, err_count, quota_remaining, quota_total, quota_cycle_end, "
            "quota_probed_at, growth_last_run_at, growth_last_result, created_at, added_by, "
            "quota_expiry_ladder, quota_packages, data_enc, token_expires_at, token_issued_at "
            "FROM credentials ORDER BY created_at").fetchall()
        cooling_rows = self._db.connect().execute(
            "SELECT credential_id, model, cooling_until, hits, reason "
            "FROM credential_model_cooldowns WHERE cooling_until > ?", (now,)).fetchall()
        cooling: dict[str, list[dict[str, Any]]] = {}
        for item in cooling_rows:
            cooling.setdefault(item["credential_id"], []).append(
                {"model": item["model"], "cooling_until": item["cooling_until"],
                 "hits": item["hits"], "reason": item["reason"]})
        out: list[dict[str, Any]] = []
        for row in rows:
            rec = dict(row)
            # data_enc 只用于派生 token 时间，绝不进响应
            data_enc = rec.pop("data_enc", None)
            # NULL = 老库升级后从未写回（按需从密文派生）；0 = 已写回但确实未知，
            # 不重复解密（否则每刷新一次列表就为同一批无从得知的凭证白解一遍）。
            if row["token_expires_at"] is None:
                issued_at, expires_at = _token_times_from_blob(data_enc, self._cipher)
            else:
                issued_at, expires_at = row["token_issued_at"] or 0, row["token_expires_at"]
            rec["token_expires_at"] = expires_at
            rec["token_issued_at"] = issued_at
            ladder = _ladder_value(row["quota_expiry_ladder"])
            rec["quota_expiry_ladder"] = ladder
            rec["quota_packages"] = _packages_value(row["quota_packages"])
            primary, secondary = expiry_windows(expiring_window, expiring_secondary_window)
            rec["quota_expiring_credits"] = (
                None if ladder is None else expiring_credits(ladder, primary, now))
            rec["quota_expiring_credits_secondary"] = (
                None if ladder is None
                else expiring_credits(ladder, secondary, now))
            rec["model_cooldowns"] = cooling.get(row["id"], [])
            out.append(rec)
        return out


class ApiKeyRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, username: str, name: str = "", now: int | None = None) -> dict[str, Any]:
        plaintext = generate_api_key()
        key_id = _new_id("key")
        created_at = int(now if now is not None else time.time())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO api_keys (id, username, name, key_digest, preview, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (key_id, username, name, digest_api_key(plaintext), preview_api_key(plaintext),
                 created_at),
            )
        return {"id": key_id, "username": username, "name": name, "api_key": plaintext,
                "preview": preview_api_key(plaintext), "created_at": created_at}

    def verify(self, api_key: str) -> str | None:
        digest = digest_api_key(api_key)
        row = self._db.connect().execute(
            "SELECT id, username FROM api_keys WHERE key_digest = ?", (digest,)).fetchone()
        if row is None:
            return None
        with self._db.transaction() as conn:
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                         (int(time.time()), row["id"]))
        return row["username"]

    def list_for(self, username: str) -> list[dict[str, Any]]:
        rows = self._db.connect().execute(
            "SELECT id, username, name, preview, created_at, last_used_at FROM api_keys "
            "WHERE username = ? ORDER BY created_at", (username,)).fetchall()
        return [dict(row) for row in rows]

    def delete(self, key_id: str, username: str) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM api_keys WHERE id = ? AND username = ?", (key_id, username))
        return cursor.rowcount > 0


class GrowthRepository:
    """成长中心运行记录（growth_events）。

    只存汇总行，不存对话/奖励明细：一轮一行 report 文本足够回答
    「这个号昨天领到了什么」，也避免把活动内部数据结构固化进 schema。
    """

    def __init__(self, db) -> None:
        self._db = db

    def record(self, *, credential_id: str, result, trigger: str = "auto",
               now: int | None = None) -> str:
        event_id = _new_id("growth")
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO growth_events (id, credential_id, ts, ok, session_dead, "
                "report, credit, energy, streak_days, trigger) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (event_id, credential_id, int(now if now is not None else time.time()),
                 1 if result.ok else 0, 1 if result.session_dead else 0,
                 result.report, result.credit, result.energy, result.streak_days, trigger),
            )
        return event_id

    def latest_for(self, credential_id: str) -> dict[str, Any] | None:
        row = self._db.connect().execute(
            "SELECT * FROM growth_events WHERE credential_id = ? ORDER BY ts DESC LIMIT 1",
            (credential_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, credential_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.connect().execute(
            "SELECT * FROM growth_events WHERE credential_id = ? ORDER BY ts DESC LIMIT ?",
            (credential_id, max(1, limit))).fetchall()
        return [dict(row) for row in rows]


class RuntimeSettingsRepository:
    """运行时配置覆盖（runtime_settings）：只存管理台改过的 key。

    纯 key/value/updated_at 三列，不在这里做类型校验——白名单与取值范围
    属于 `src/runtime_settings.py`（配置语义），仓储只负责存取。这样新增一个
    可热更项不需要改 schema，与「表结构只加不改」的纪律一致。
    """

    def __init__(self, db) -> None:
        self._db = db

    def load(self) -> dict[str, str]:
        rows = self._db.connect().execute(
            "SELECT key, value FROM runtime_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set(self, key: str, value: str, now: int | None = None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO runtime_settings (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, int(now if now is not None else time.time())),
            )

    def delete(self, key: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM runtime_settings WHERE key = ?", (key,))

    def updated_at(self) -> dict[str, int]:
        """key → 最近一次修改时间（界面显示「何时改的」）。"""
        rows = self._db.connect().execute(
            "SELECT key, updated_at FROM runtime_settings").fetchall()
        return {row["key"]: row["updated_at"] for row in rows}
