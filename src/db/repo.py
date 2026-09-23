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
        """写回额度，并顺带记一条积分变动流水（B3.4）。

        流水必须在同一事务里与前值比较：分两次读改写会与并发探测交错，
        记出「before 是别人写过的值」的错行。因此这里先取旧余额，
        再在同一个事务里 UPDATE + INSERT。
        """
        with self._db.transaction() as conn:
            previous = conn.execute(
                "SELECT quota_remaining, quota_probed_at FROM credentials WHERE id = ?",
                (credential_id,)).fetchone()
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
            self._record_credit_event(conn, credential_id, quota,
                                      previous=previous)

    @staticmethod
    def _record_credit_event(conn, credential_id: str, quota: Quota, *,
                             previous) -> None:
        """比对前后余额写一条 credit_events；无变化或无法量化时不写。

        只记「有信息量」的行：
        - 首次探测（没有 previous 行）→ source=sync，只是建立基线；
          此时 delta 为空，因为「从无到有」不是一次真实的积分变动。
        - 余额未变 → 不写。否则每轮探测都落一行 0，表会被噪声淹没。
        - 任一端为 NULL（探测失败后的未知）→ delta 记空但**仍记行**：
          「余额从 100 变成未知」本身就是值得追的异常。

        绝不写「签到 +5」这类归因：上游不打日志，diff 看不到分数是谁加的
        （见 schema.sql 的表注释）。
        """
        if previous is None:       # 凭证已被并发删除：不补记，避免悬挂行
            return
        before = previous["quota_remaining"]
        after = quota.remaining
        if before is None:
            # 没有对照基线：只记「基线已建立」，不算积分数。
            # 连本次值都没有（两端皆未知）→ 什么也没学到，不写。
            if after is None:
                return
            conn.execute(
                "INSERT INTO credit_events (id, credential_id, ts, window_start, "
                "before, after, delta, source) VALUES (?,?,?,?,?,?,?,'sync')",
                (_new_id("credit"), credential_id, quota.probed_at, None,
                 None, after, None))
            return
        if after is not None and float(before) == float(after):
            return
        # 浮点噪声护栏：余额来自上游 JSON 的浮点运算，两次「没变」也可能差
        # 1e-13 这种量级。不挡的话会记出一堆 delta≈0 的假变动行。
        epsilon = 1e-9
        if after is not None and abs(float(after) - float(before)) < epsilon:
            return
        # 落库前按 epsilon 量级收敛：7.229999999999563 这种值直接展示会给
        # 「这数怎么这么脏」的印象，而 1e-9 位上的差异本来就不是真变化。
        delta = None if after is None else round(float(after) - float(before), 6)
        conn.execute(
            "INSERT INTO credit_events (id, credential_id, ts, window_start, "
            "before, after, delta, source) VALUES (?,?,?,?,?,?,?,'observed')",
            (_new_id("credit"), credential_id, quota.probed_at,
             previous["quota_probed_at"], before, after, delta))

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

    def pool_counts(self, now: int | None = None) -> dict[str, int]:
        """池健康计数（/healthz）：四类互斥且合计等于 total。

        口径直接复用调度器的 `Candidate.is_selectable`（不带模型过滤 = 池级
        视角），而不是另写一份 SQL——两处口径一旦分叉，健康检查会报出与
        实际选号不一致的「可用数」。判定优先级：disabled → paused → cooling
        → ready，与调度器一致（禁用/暂停优先于冷却）。
        """
        moment = int(now if now is not None else time.time())
        counts = {"total": 0, "ready": 0, "cooling": 0, "paused": 0, "disabled": 0}
        for candidate in self.candidates():
            counts["total"] += 1
            if candidate.disabled:
                counts["disabled"] += 1
            elif not candidate.enabled:
                counts["paused"] += 1
            elif not candidate.is_selectable(moment):
                counts["cooling"] += 1
            else:
                counts["ready"] += 1
        return counts

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

    def create(self, username: str, name: str = "", now: int | None = None,
               *, provider_binding: str = "", allowed_ips: str = "") -> dict[str, Any]:
        plaintext = generate_api_key()
        key_id = _new_id("key")
        created_at = int(now if now is not None else time.time())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO api_keys (id, username, name, key_digest, preview, created_at, "
                "provider_binding, allowed_ips) VALUES (?,?,?,?,?,?,?,?)",
                (key_id, username, name, digest_api_key(plaintext), preview_api_key(plaintext),
                 created_at, provider_binding, allowed_ips),
            )
        return {"id": key_id, "username": username, "name": name, "api_key": plaintext,
                "preview": preview_api_key(plaintext), "created_at": created_at,
                "provider_binding": provider_binding, "allowed_ips": allowed_ips}

    def authenticate(self, api_key: str) -> dict[str, Any] | None:
        """校验 Key 并返回其策略行（含 provider_binding / allowed_ips）。

        命中即刷新 last_used_at：所有出口鉴权都走这里，避免再散落一处
        「用了 Key 但没记最后使用时间」。返回 None 表示 Key 不存在。
        """
        digest = digest_api_key(api_key)
        row = self._db.connect().execute(
            "SELECT id, username, provider_binding, allowed_ips FROM api_keys "
            "WHERE key_digest = ?", (digest,)).fetchone()
        if row is None:
            return None
        with self._db.transaction() as conn:
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                         (int(time.time()), row["id"]))
        return dict(row)

    def verify(self, api_key: str) -> str | None:
        row = self.authenticate(api_key)
        return row["username"] if row else None

    def list_for(self, username: str) -> list[dict[str, Any]]:
        rows = self._db.connect().execute(
            "SELECT id, username, name, preview, created_at, last_used_at, "
            "provider_binding, allowed_ips FROM api_keys "
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


class CreditEventRepository:
    """积分变动流水（credit_events）：只读查询，写入在 save_quota 事务内完成。

    读侧独立成仓储（与 GrowthRepository 同形），写入刻意不放在这里：
    流水必须与额度 UPDATE 同事务才能拿到正确的 before（见
    CredentialRepository._record_credit_event）。
    """

    def __init__(self, db) -> None:
        self._db = db

    def recent(self, credential_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """倒序返回变动记录；limit 收敛到 [1, 200] 防止一次拉爆前端。"""
        rows = self._db.connect().execute(
            "SELECT * FROM credit_events WHERE credential_id = ? "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (credential_id, max(1, min(200, limit)))).fetchall()
        return [dict(row) for row in rows]

    def prune(self, *, keep_days: int, now: int | None = None) -> int:
        """删除超过保留期的流水（与 usage_events 同一保留策略入口）。"""
        cutoff = int(now if now is not None else time.time()) - keep_days * 86400
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM credit_events WHERE ts < ?", (cutoff,))
        return cursor.rowcount


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


class UserRepository:
    """管理台用户账号（users，B5）。

    本仓储**返回整行**（含 password_hash / activation_digest），因为
    `DbUserStore` 需要它们做校验。对外 API 必须显式挑选字段——绝不要把这里的
    dict 直接塞进响应体（测试 `test_users_never_leak_hash` 守住这条）。

    角色取值不在这里校验：合法值属于 `src/auth/rbac.py`（权限语义），
    仓储只负责存取，与 RuntimeSettingsRepository 的分工一致。
    """

    def __init__(self, db) -> None:
        self._db = db

    def get(self, username: str) -> dict[str, Any] | None:
        row = self._db.connect().execute(
            "SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def list_all(self) -> list[dict[str, Any]]:
        """按用户名排序的完整行；展示层负责剔除敏感列。"""
        rows = self._db.connect().execute(
            "SELECT * FROM users ORDER BY username").fetchall()
        return [dict(row) for row in rows]

    def list_usernames(self) -> tuple[str, ...]:
        rows = self._db.connect().execute(
            "SELECT username FROM users ORDER BY username").fetchall()
        return tuple(row["username"] for row in rows)

    def create(self, username: str, password_hash: str, *, role: str = "viewer",
               enabled: bool = True, must_change_password: bool = False,
               created_by: str | None = None, activation_digest: str | None = None,
               activation_expires_at: int | None = None, now: int | None = None) -> None:
        timestamp = int(now if now is not None else time.time())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, enabled, "
                "must_change_password, session_epoch, activation_digest, "
                "activation_expires_at, created_at, updated_at, created_by) "
                "VALUES (?,?,?,?,?,0,?,?,?,?,?)",
                (username, password_hash, role, 1 if enabled else 0,
                 1 if must_change_password else 0, activation_digest,
                 activation_expires_at, timestamp, timestamp, created_by),
            )

    def upsert_imported(self, username: str, password_hash: str, *,
                        now: int | None = None) -> bool:
        """导入 users.txt 用：已存在的用户不动（不覆盖已改过的密码/角色）。

        返回 True 表示确实插入了新行。幂等——重复启动不会重复导入。
        """
        published = self.get(username)
        if published is not None:
            return False
        self.create(username, password_hash, role="viewer", created_by=None, now=now)
        return True

    def update_role(self, username: str, role: str, *, bump_epoch: bool = True,
                    now: int | None = None) -> bool:
        """改角色。默认 bump epoch：降级必须立刻踢掉旧会话。"""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET role = ?, session_epoch = session_epoch + ?, "
                "updated_at = ? WHERE username = ?",
                (role, 1 if bump_epoch else 0,
                 int(now if now is not None else time.time()), username),
            )
        return cursor.rowcount > 0

    def set_enabled(self, username: str, enabled: bool, *,
                    now: int | None = None) -> bool:
        """启用/禁用。禁用必须 bump epoch，否则已登录会话还能用到 Cookie 过期。"""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET enabled = ?, session_epoch = session_epoch + 1, "
                "updated_at = ? WHERE username = ?",
                (1 if enabled else 0,
                 int(now if now is not None else time.time()), username),
            )
        return cursor.rowcount > 0

    def set_password(self, username: str, password_hash: str, *,
                     must_change_password: bool = False, now: int | None = None) -> bool:
        """改密：bump epoch（踢掉其他会话）并清掉一次性激活令牌。"""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = ?, "
                "session_epoch = session_epoch + 1, activation_digest = NULL, "
                "activation_expires_at = NULL, updated_at = ? WHERE username = ?",
                (password_hash, 1 if must_change_password else 0,
                 int(now if now is not None else time.time()), username),
            )
        return cursor.rowcount > 0

    def set_activation_token(self, username: str, digest: str, expires_at: int, *,
                             now: int | None = None) -> bool:
        """挂一次性激活令牌（登录前使用，故**不** bump epoch）。"""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET activation_digest = ?, activation_expires_at = ?, "
                "updated_at = ? WHERE username = ?",
                (digest, expires_at, int(now if now is not None else time.time()), username),
            )
        return cursor.rowcount > 0

    def find_by_activation(self, digest: str) -> dict[str, Any] | None:
        """按令牌摘要定位用户（/activate 用）。不校验过期——调用方比 now。"""
        row = self._db.connect().execute(
            "SELECT * FROM users WHERE activation_digest = ?", (digest,)).fetchone()
        return dict(row) if row else None

    def delete(self, username: str) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        return cursor.rowcount > 0

    def count_active_admins(self) -> int:
        """活跃 admin 数：防锁死判定（0 即无权可依）。"""
        row = self._db.connect().execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND enabled = 1"
        ).fetchone()
        return int(row["n"])


class AuditRepository:
    """审计流水（audit_events，B5）。

    写入刻意不在这里做「业务校验」：调用方负责保证 detail 不含密码/令牌。
    `prune` 与 usage_events 同保留策略入口，由 RetentionTask 调用。
    """

    def __init__(self, db) -> None:
        self._db = db

    def record(self, *, actor: str, action: str, target: str | None = None,
               detail: str = "", ip: str | None = None, ok: bool = True,
               now: int | None = None) -> str:
        event_id = _new_id("audit")
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_events (id, ts, actor, action, target, detail, ip, ok) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (event_id, int(now if now is not None else time.time()), actor, action,
                 target, detail, ip, 1 if ok else 0),
            )
        return event_id

    def query(self, *, actor: str | None = None, action: str | None = None,
              since: int | None = None, before: int | None = None,
              limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """按 ts 倒序；limit 收敛到 [1, 500]，offset 非负。"""
        clauses: list[str] = []
        params: list[Any] = []
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(int(since))
        if before is not None:
            clauses.append("ts < ?")
            params.append(int(before))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend((max(1, min(500, limit)), max(0, offset)))
        rows = self._db.connect().execute(
            f"SELECT * FROM audit_events {where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def prune(self, *, keep_days: int, now: int | None = None) -> int:
        cutoff = int(now if now is not None else time.time()) - keep_days * 86400
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM audit_events WHERE ts < ?", (cutoff,))
        return cursor.rowcount
