"""凭证与 API Key 的持久化访问（手写 SQL）。

凭证内容以 Fernet 密文入库；解密只发生在引擎侧，provider 不接触数据库。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable, Iterable
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
from ..sorting import sort_rows, sql_order
from ..tags import tags_text, tags_value


def _now(now: int | None) -> int:
    """调用方缺省时取当前 epoch 秒；显式传 0 时保留 0（不被当成缺省）。"""
    return int(now if now is not None else time.time())


def _clear_model_cooldowns(conn: Any, credential_id: str) -> None:
    """清掉该凭证的全部 (凭证, 模型) 冷却行。

    模型级冷却没有外键级联，所以删除凭证 / 硬禁用 / 账号级冷却 / 显式恢复
    都必须显式清理——否则重建同 id 凭证会继承旧的模型冷却，残留行还会拖慢
    留存清理。
    """
    conn.execute("DELETE FROM credential_model_cooldowns WHERE credential_id = ?",
                 (credential_id,))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# API Key `last_used_at` 的写库节流窗口（秒）：见 `ApiKeyRepository.authenticate`。
_LAST_USED_THROTTLE_SECONDS = 60


def _ladder_text(ladder: list[tuple[int, float]] | None) -> str | None:
    """到期阶梯落库：[[epoch, 剩余积分], ...]；无到期信息的渠道写 NULL。"""
    if not ladder:
        return None
    return json.dumps([[end, remaining] for end, remaining in ladder])


def _ladder_value(text: str | None) -> list[tuple[int, float]] | None:
    """读回到期阶梯。缺失、迁移前的空值或历史脏数据一律当「无到期信息」，
    不能让一行坏数据把整个选号流程拖崩。"""
    if not text:
        return None
    try:
        items = json.loads(text)
    except ValueError:
        return None
    # 合法 JSON 但不合形状（`"5"` / `null` / `[["x",5]]`）同样当脏数据：
    # 迭代非列表会抛 TypeError、int(item[0]) 会抛 ValueError，两者都必须
    # 归一为 None，否则 candidates()/pool_counts()/list_all() 一起崩。
    if not isinstance(items, list):
        return None
    ladder: list[tuple[int, float]] = []
    for item in items:
        if not (isinstance(item, list) and len(item) == 2):
            continue
        try:
            ladder.append((int(item[0]), float(item[1])))
        except (TypeError, ValueError):
            continue
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


# 列表排序白名单：API 键 → 取值函数（`None` 值统一排最后，见 src/sorting.py）。
# 凭证列含派生字段（quota_expiring_credits 等），故在 Python 侧排序而非 SQL。
CREDENTIAL_SORT_KEYS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "nickname": lambda row: row["nickname"] or "",
    "provider": lambda row: row["provider"],
    "health": lambda row: row["health"],
    "enabled": lambda row: row["enabled"],
    "pinned": lambda row: row["pinned"],
    "quota_remaining": lambda row: row["quota_remaining"],
    "quota_total": lambda row: row["quota_total"],
    "quota_expiring_credits": lambda row: row["quota_expiring_credits"],
    # 0 = 上游未给到期信息，视为缺失（排最后），不当成「最早过期」
    "token_expires_at": lambda row: row["token_expires_at"] or None,
    "growth_last_run_at": lambda row: row["growth_last_run_at"] or None,
    "created_at": lambda row: row["created_at"],
}

# 纯 SQL 列表的白名单：API 键 → 列名（受控常量，用户输入永不进 SQL）。
API_KEY_SORT_COLUMNS: dict[str, str] = {
    "name": "name",
    "provider_binding": "provider_binding",
    "created_at": "created_at",
    "last_used_at": "last_used_at",
    "expires_at": "expires_at",
}

USER_SORT_COLUMNS: dict[str, str] = {
    "username": "username",
    "role": "role",
    "enabled": "enabled",
    "created_by": "created_by",
    "created_at": "created_at",
    "updated_at": "updated_at",
}

AUDIT_SORT_COLUMNS: dict[str, str] = {
    "ts": "ts",
    "actor": "actor",
    "action": "action",
    "target": "target",
    "detail": "detail",
    "ip": "ip",
    "ok": "ok",
}

ALERT_SORT_COLUMNS: dict[str, str] = {
    "ts": "ts",
    "severity": "severity",
    "rule": "rule",
    "scope": "scope",
    "message": "message",
    "delivered": "delivered",
}

CREDIT_EVENT_SORT_COLUMNS: dict[str, str] = {
    "ts": "ts",
    "window_start": "window_start",
    "delta": "delta",
    "source": "source",
}

# 变动流水的账本白名单：pool＝套餐/每日池余额（CodeArts 之外的渠道）/
# credits＝CodeArts 积分（该渠道唯一的额度来源，见 `quota_ledger`）。
# 查询侧只认这两个取值，其余回落 pool。
CREDIT_EVENT_LEDGERS: frozenset[str] = frozenset({"pool", "credits"})


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


def quota_ledger(provider: str) -> str:
    """该渠道的额度记在哪本账（`credit_events.ledger`）。

    CodeArts 的额度来自积分接口（`/snap-manager/v1/statistics/plugin`，内置模型
    按积分计费），记 `credits`；其余渠道是套餐 / 每日池口径的余额，记 `pool`。
    老库里 CodeArts 的 `pool` 行是已下线的每日 token 池口径，不再展示（量级与
    语义都和积分不同，混读会被当成同一本账的连续读数）。
    """
    return "credits" if provider == "codearts" else "pool"


class CredentialRepository:
    def __init__(self, db, cipher: CredentialCipher) -> None:
        self._db = db
        self._cipher = cipher
        # 凭证级编辑锁：refresh / 账号切换都是「解密取整份 data → await
        # 上游 → 整份覆写 data_enc」，两个写者在 await 点交错时，后写的会用自己
        # 读到的旧快照覆盖先写的新值（典型：刷新把刚切好的账号覆盖回去）。
        # 锁只按凭证 id 分键，不同凭证互不阻塞。
        self._edit_locks: dict[str, asyncio.Lock] = {}

    def edit_lock(self, credential_id: str) -> asyncio.Lock:
        """取该凭证的编辑锁；调用方把「读 → await → 覆写」整段包在 `async with`。

        单进程事件循环内即可互斥；凭证数有限，锁表不回收（每个 id 一个空锁）。
        """
        lock = self._edit_locks.get(credential_id)
        if lock is None:
            lock = asyncio.Lock()
            self._edit_locks[credential_id] = lock
        return lock

    # ------------------------------------------------------------- 写入

    def add(self, *, provider: str, credential_data: dict, nickname: str = "",
            added_by: str = "", now: int | None = None,
            tags: list[str] | None = None) -> str:
        credential_id = _new_id("cred")
        payload = json.dumps(credential_data, ensure_ascii=False).encode("utf-8")
        created = _now(now)
        issued_at, expires_at = credential_token_times(credential_data)
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO credentials (id, provider, nickname, data_enc, created_at, added_by, "
                "token_expires_at, token_issued_at, tags) VALUES (?,?,?,?,?,?,?,?,?)",
                (credential_id, provider, nickname, self._cipher.encrypt(payload),
                 created, added_by, expires_at, issued_at, tags_text(tags or [])),
            )
        return credential_id

    def set_tags(self, credential_id: str, tags: list[str]) -> bool:
        """整体替换一条凭证的标签（空列表 = 清空）。返回是否命中该凭证。"""
        with self._db.transaction() as conn:
            cursor = conn.execute("UPDATE credentials SET tags = ? WHERE id = ?",
                                  (tags_text(tags), credential_id))
        return cursor.rowcount > 0

    def delete(self, credential_id: str) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM credentials WHERE id = ?", (credential_id,))
            # 模型级冷却没有外键级联，凭证删除后必须显式清理，
            # 否则重建同 id 凭证会继承旧的模型冷却（残留行还会拖慢留存清理）
            _clear_model_cooldowns(conn, credential_id)
        return cursor.rowcount > 0

    def set_enabled(self, credential_id: str, enabled: bool) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE credentials SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, credential_id))
        return cursor.rowcount > 0

    def set_pinned(self, credential_id: str | None) -> bool:
        """指定唯一被 pin 的凭证（`None` = 取消指定）。

        全库至多一条 pinned：先清全部再置目标行。

        返回值 = 指定是否生效。`credential_id` 非空但库里没有这一行时返回
        False，**且不改动任何既有 pin**：否则管理员会以为「已指定」生效了，
        实际把别人的指定解除了，界面却显示成功。

        查存在性与两次写入放在同一事务里：`BEGIN IMMEDIATE` 的写锁保证
        「查到存在」到「清 pin + 置 pin」之间不被并发删除打断（否则目标行
        在清完之后被删，pin 就落空了）。
        """
        with self._db.transaction() as conn:
            if credential_id is not None:
                exists = conn.execute(
                    "SELECT 1 FROM credentials WHERE id = ?",
                    (credential_id,)).fetchone()
                if exists is None:
                    return False
            conn.execute("UPDATE credentials SET pinned = 0")
            if credential_id is not None:
                conn.execute(
                    "UPDATE credentials SET pinned = 1 WHERE id = ?",
                    (credential_id,))
            return True

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
            _clear_model_cooldowns(conn, credential_id)
        return cursor.rowcount > 0

    def mark_relogin_required(self, credential_id: str, reason: str) -> None:
        """标记「需重新登录」：硬禁用 + 可操作原因，停止一切自动重试。

        与 `save_error` 的 `session dead` 分支分开：那条是聊天路径按
        ErrKind.DEAD 打的通用标记，而这里由预刷新任务在确认「续期凭据已被上游
        作废」后写入**可操作**的原因（管理台直接显示在 disabled_reason 上）。

        硬禁用是必需的：RefreshTask 会跳过 disabled 的凭证，调度选号与额度
        探测同样跳过（`candidates(selectable_only=True)`），否则一条已经死了的
        凭证会继续每轮被挑中并刷屏，而唯一能救它的刷新任务反倒不再尝试。
        """
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE credentials SET disabled = 1, disabled_reason = ?, err_count = 0, "
                "cooling_until = NULL WHERE id = ?", (reason, credential_id))
            _clear_model_cooldowns(conn, credential_id)

    def save_error(self, credential_id: str, outcome: ErrorOutcome) -> None:
        """落库一次错误结果。

        `outcome.model_cooldowns` 非空表示模型级冷却：只写 (凭证, 模型) 表，
        不动账号级 cooling_until；反之账号级冷却会清空该凭证的模型级条目
        （账号级限流不允许被「切模型」绕过）。

        硬禁用（DEAD）优先写 `outcome.disabled_reason`，为空才回落历史的
        "session dead"：401/403 里有「账号被风控限制」这类重登解决不了的原因，
        管理台直接显示该字段，写错会把人引向错误的修复动作。
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
                    "cooling_until = NULL WHERE id = ?",
                    (outcome.disabled_reason or "session dead", credential_id))
            else:
                conn.execute(
                    "UPDATE credentials SET cooling_until = ?, err_count = ? WHERE id = ?",
                    (outcome.cooling_until, outcome.err_count, credential_id))
            if outcome.cooling_until is not None:
                _clear_model_cooldowns(conn, credential_id)

    def cool_provider(self, provider: str, *, until: int) -> int:
        """渠道级冷却：把该渠道所有凭证的账号级冷却一起推后，返回受影响行数。

        用于「整批同源账号被上游风控限制」（CodeBuddy 11140）：逐个号冷却挡不住
        下一个请求换同渠道另一个号继续撞，每次都在上游多留一条风控记录。只写
        `cooling_until`，不动 `disabled`——禁用是不可逆的人工状态，而这类限制
        会由上游自行解除，到期必须能自动回到候选。

        只在比现有冷却更晚时覆盖（`cooling_until < until`）：渠道级暂停不该
        缩短某个号已经更晚的冷却。账号级冷却出现要清掉模型级条目，否则模型级
        豁免会泄漏成「换模型绕过账号级限制」。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE credentials SET cooling_until = ?, err_count = 0 "
                "WHERE provider = ? AND (cooling_until IS NULL OR cooling_until < ?)",
                (until, provider, until))
            conn.execute(
                "DELETE FROM credential_model_cooldowns WHERE credential_id IN "
                "(SELECT id FROM credentials WHERE provider = ?)", (provider,))
        return cursor.rowcount

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
                (_now(now),))
        return cursor.rowcount

    def purge_expired_account_cooldowns(self, now: int | None = None) -> int:
        """清掉已过期的账号级 `cooling_until`（留存任务每轮调用）。

        选号本身用 `cooling_until > now` 判断，过期值不会挡住凭证，但它会
        长期留在库里：管理台列表下发的是原始值，一条几周前的陈旧冷却会让
        该行在「冷却」维度上排序失真，也让「谁在冷却」这类人工排查看到
        幽灵记录。与模型级条目分开计数，便于区分两类回收。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE credentials SET cooling_until = NULL "
                "WHERE cooling_until IS NOT NULL AND cooling_until <= ?",
                (_now(now),))
        return cursor.rowcount

    def model_cooldowns_for(self, credential_id: str,
                            now: int | None = None) -> dict[str, int]:
        """该凭证仍在生效的模型级冷却（model → 截止 epoch），供管理台展示。"""
        rows = self._db.connect().execute(
            "SELECT model, cooling_until FROM credential_model_cooldowns "
            "WHERE credential_id = ? AND cooling_until > ?",
            (credential_id, _now(now))).fetchall()
        return {row["model"]: row["cooling_until"] for row in rows}

    def blocked_cooldowns(self, now: int | None = None) -> list[dict[str, Any]]:
        """仍在生效的 `reason='blocked'` 负缓存行（附渠道），供主动探活。

        只取 blocked：`model`（6004 模型限流）有自己的重置墙钟，探活成功一次
        不代表限流解除——那是调度器与上游时钟的事，不该被探测提前解除。
        """
        rows = self._db.connect().execute(
            "SELECT c.credential_id, c.model, c.cooling_until, c.hits, "
            "       cred.provider AS provider "
            "FROM credential_model_cooldowns c "
            "JOIN credentials cred ON cred.id = c.credential_id "
            "WHERE c.reason = 'blocked' AND c.cooling_until > ? "
            "ORDER BY c.cooling_until", (_now(now),)
        ).fetchall()
        return [dict(row) for row in rows]

    def clear_blocked(self, *, provider: str, model: str) -> int:
        """清掉某 (渠道, 模型) 的全部 blocked 负缓存行（人工恢复 / 探活成功）。

        按渠道而不是按凭证清：11102「该后端无此模型」通常随上游模型表变化，
        只清探活用的那一个号，其余号会各等一个 6h 起步的 TTL 才恢复。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM credential_model_cooldowns WHERE model = ? "
                "AND reason = 'blocked' AND credential_id IN "
                "(SELECT id FROM credentials WHERE provider = ?)", (model, provider))
        return cursor.rowcount

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
        """写回额度，并顺带记一条变动流水。

        流水必须在同一事务里与前值比较：分两次读改写会与并发探测交错，
        记出「before 是别人写过的值」的错行。因此这里先取旧余额，
        再在同一个事务里 UPDATE + INSERT。账本由凭证自己的渠道决定，见
        `quota_ledger`。
        """
        with self._db.transaction() as conn:
            previous = conn.execute(
                "SELECT provider, quota_remaining, quota_probed_at "
                "FROM credentials WHERE id = ?",
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
            if previous is not None:
                self._record_credit_event(
                    conn, credential_id, ledger=quota_ledger(previous["provider"]),
                    before=previous["quota_remaining"], after=quota.remaining,
                    probed_at=quota.probed_at, window_start=previous["quota_probed_at"])

    @staticmethod
    def _record_credit_event(conn, credential_id: str, *, ledger: str, before, after,
                             probed_at: int, window_start: int | None) -> None:
        """比对一本账的前后余额写一条 credit_events；无变化或无法量化时不写。

        `ledger` 标明这是哪本账（pool＝套餐/每日池余额 / credits＝CodeArts 积分，
见 `quota_ledger`），两本账的数值量级与语义都不同，混读会被当成同一本账的连续读数。

        只记「有信息量」的行：
        - 首次探测（没有对照基线）→ source=sync，只是建立基线；
          此时 delta 为空，因为「从无到有」不是一次真实的余额变动。
        - 余额未变 → 不写。否则每轮探测都落一行 0，表会被噪声淹没。
        - 任一端为 NULL（探测失败后的未知）→ delta 记空但**仍记行**：
          「余额从 100 变成未知」本身就是值得追的异常。

        绝不写「签到 +5」这类归因：上游不打日志，diff 看不到分数是谁加的
        （见 schema.sql 的表注释）。
        """
        if before is None:
            # 没有对照基线：只记「基线已建立」，不算积分数。
            # 连本次值都没有（两端皆未知）→ 什么也没学到，不写。
            if after is None:
                return
            conn.execute(
                "INSERT INTO credit_events (id, credential_id, ts, window_start, "
                "before, after, delta, source, ledger) VALUES (?,?,?,?,?,?,?,'sync',?)",
                (_new_id("credit"), credential_id, probed_at, None,
                 None, after, None, ledger))
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
            "before, after, delta, source, ledger) VALUES (?,?,?,?,?,?,?,'observed',?)",
            (_new_id("credit"), credential_id, probed_at,
             window_start, before, after, delta, ledger))

    def mark_probe_failed(self, credential_id: str, now: int | None = None) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE credentials SET quota_probed_at = ?, health = NULL WHERE id = ?",
                         (_now(now), credential_id))

    def save_growth_result(self, credential_id: str, report: str,
                           now: int | None = None) -> None:
        """记下成长中心最近一轮的汇报（列表页直接显示，不查 events 表）。"""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE credentials SET growth_last_run_at = ?, growth_last_result = ? "
                "WHERE id = ?",
                (_now(now), report, credential_id))

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
            "quota_remaining, quota_cycle_end, quota_expiry_ladder FROM credentials").fetchall()
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
                quota_remaining=row["quota_remaining"],
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
        moment = _now(now)
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

    def attention_counts(self) -> dict[str, int]:
        """侧栏角标：**需人工介入**的凭证数（`disabled` + `exhausted`）。

        - `disabled` = session 死亡硬禁用，需**重新登录**（最硬的可行动信号）。
        - `exhausted` = `health < 0`（额度耗尽）。排除 `enabled=0`（用户有意
          暂停）与「只有冷却」的号——冷却到点自愈，计入就变成永远亮着的噪声。

        ⚠️ 与 `pool_counts()` 口径**不同**：那边复用
        `Candidate.is_selectable`，只看 disabled / enabled / cooling、**不看
        health**，所以额度耗尽的号在 `/healthz` 里仍算 `ready`。本方法是
        「待办角标」的信号，不是可用数的另一种算法，两者不一致是刻意的。
        """
        row = self._db.connect().execute(
            "SELECT COALESCE(SUM(disabled = 1), 0) AS disabled, "
            "COALESCE(SUM(disabled = 0 AND enabled = 1 AND health < 0), 0) AS exhausted "
            "FROM credentials").fetchone()
        return {"disabled": int(row["disabled"]), "exhausted": int(row["exhausted"])}

    def credential_data(self, credential_id: str) -> dict[str, Any] | None:
        """取凭证明文；不存在返回 None。

        解密失败**照旧抛** `CredentialDecryptError`：它表示 APP_SECRET 换过
        或密文损坏，是需要人处理的状态（见 `webapp.handlers` 的可行动 500
        映射）。「一条坏凭证不该连累整个请求」由**调用方**处理：引擎选号
        与模型列表会捕获它并跳过该凭证（见 `executor._safe_credential_data`
        / `api.models`），而单条探测端点把它如实报给管理员。
        """
        row = self._db.connect().execute(
            "SELECT data_enc FROM credentials WHERE id = ?", (credential_id,)).fetchone()
        if row is None:
            return None
        plaintext = self._cipher.decrypt(row["data_enc"])
        return json.loads(plaintext.decode("utf-8"))

    def nickname_of(self, credential_id: str) -> str | None:
        """单条凭证昵称（供实时流按需补名字，避免每事件拉整份列表）。

        昵称来自全局共享池（常含邮箱/手机），调用方必须自己保证「只对
        admin/operator 解析」，与 `/api/stats/events` 同口径。凭证已删除 /
        无昵称时返回 None（前端回落到 ID 前缀）。
        """
        row = self._db.connect().execute(
            "SELECT nickname FROM credentials WHERE id = ?", (credential_id,)).fetchone()
        if row is None or not row["nickname"]:
            return None
        return str(row["nickname"])

    def list_all(
        self, *, expiring_window: int = 0, expiring_secondary_window: int = 0,
        now: int | None = None, sort: str | None = None, order: str | None = None,
        include_nickname: bool = True,
    ) -> list[dict[str, Any]]:
        """管理台列表：绝不返回明文凭证。

        `sort` / `order`：列表排序（白名单见 `CREDENTIAL_SORT_KEYS`）；未知键 /
        非法方向回落 `created_at` 升序。派生字段（到期额度等）在此可见，故排序
        放在组装之后，而不是 SQL。

        `include_nickname=False`：昵称来自全局共享池（常含邮箱/手机），只对
        admin/operator 返回，与 `/api/stats/events`、`by-credential` 口径一致；
        viewer 只看得到自己的记录，昵称对它没有用途。

        附 `quota_expiring_credits`（窗口内即将到期的积分，与调度排序第一级同源）；
        附 `quota_expiring_credits_secondary`（次窗口，主窗口打平时才参与排序的第二级）；
        附 `quota_expiry_ladder`（套餐到期阶梯，[[epoch, 剩余积分]]，按包独立到期的渠道）；
        渠道无到期信息时为 None，展示层据此隐藏。
        附 `model_cooldowns`（model → 截止 epoch），只在模型级限流/负缓存时非空。
        附 `token_expires_at` / `token_issued_at`（token 到期展示）：只下发
        绝对 epoch，剩余时间与预警阈值判定交给展示层用同一个时钟现算——否则
        服务端算好的「剩余秒数」不会随页面 tick 更新，两处口径还会漂移。
        到期时间未知时为 0（展示层隐藏，绝不当成已过期）。
        """
        # 注意：这里用 `or` 而非 `is not None`——list_all 的调用方从不传 0，
        # 而 `now=0`（epoch 起点）对它等同于「取当前时刻」是可接受的。
        now = int(now or time.time())
        rows = self._db.connect().execute(
            "SELECT id, provider, nickname, enabled, disabled, disabled_reason, pinned, "
            "health, cooling_until, err_count, quota_remaining, quota_total, quota_cycle_end, "
            "quota_probed_at, growth_last_run_at, growth_last_result, created_at, added_by, "
            "quota_expiry_ladder, quota_packages, data_enc, "
            "token_expires_at, token_issued_at, "
            "tags "
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
            if not include_nickname:
                rec["nickname"] = ""
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
            # 标签以列表下发（库里是逗号分隔文本）：展示层不必知道存储形态，
            # 脏数据由 tags_value 读路径容错滤掉。
            rec["tags"] = tags_value(row["tags"])
            primary, secondary = expiry_windows(expiring_window, expiring_secondary_window)
            rec["quota_expiring_credits"] = (
                None if ladder is None else expiring_credits(ladder, primary, now))
            rec["quota_expiring_credits_secondary"] = (
                None if ladder is None else expiring_credits(ladder, secondary, now))
            rec["model_cooldowns"] = cooling.get(row["id"], [])
            out.append(rec)
        # 排序在组装后做：额度占比 / 到期额度是派生字段，SQL 排不了。
        # 默认 created_at 升序，与历史行为一致。
        return sort_rows(out, sort, order, CREDENTIAL_SORT_KEYS,
                         default_key="created_at", default_desc=False)

    def expiring_tokens(self, *, within_seconds: int, now: int | None = None,
                        ) -> list[dict[str, Any]]:
        """列出 access token 将在窗口内到期的凭证（告警）。

        只收「到期时间已知且落在 (moment, moment+within]」的行：
        - `token_expires_at` 列 NULL（老库未写回）→ 从密文按需派生，与列表页
          同源；派生不出（0）视为未知，不算「即将到期」；
        - 0 = 上游确实没给到期信息，不当成已过期；
        - 硬禁用（session 死亡）的凭证排除：它们本就要求重新登录，再报 token
          到期没有额外价值；
        - **寿命本就短于窗口**的凭证排除：CodeArts 的 STS 临时凭证只有 2h，
          而默认窗口 24h，于是无论续期成功与否它都永远「即将到期」，每个静默窗
          报一次——恒真的告警只会把真告警淹掉。这类凭证真出问题时预刷新任务会
          失败并把它标成需重新登录（进而触发池空告警），所以这里主动静音。
          签发时间未知（0）时不猜，按原语义照报。
        """
        if within_seconds <= 0:
            return []
        moment = _now(now)
        deadline = moment + within_seconds
        rows = self._db.connect().execute(
            "SELECT id, provider, nickname, data_enc, token_expires_at, token_issued_at "
            "FROM credentials WHERE disabled = 0").fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if row["token_expires_at"] is None:
                issued_at, expires_at = _token_times_from_blob(row["data_enc"], self._cipher)
            else:
                issued_at, expires_at = row["token_issued_at"] or 0, row["token_expires_at"]
            lifetime = expires_at - issued_at if issued_at > 0 else 0
            if lifetime and lifetime <= within_seconds:
                continue
            if expires_at and moment < expires_at <= deadline:
                out.append({"id": row["id"], "provider": row["provider"],
                            "nickname": row["nickname"],
                            "token_expires_at": expires_at})
        return out


class ApiKeyRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, username: str, name: str = "", now: int | None = None,
               *, provider_binding: str = "", allowed_ips: str = "",
               allowed_models: str = "", expires_at: int | None = None,
               quota: str = "") -> dict[str, Any]:
        plaintext = generate_api_key()
        key_id = _new_id("key")
        created_at = _now(now)
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO api_keys (id, username, name, key_digest, preview, created_at, "
                "provider_binding, allowed_ips, allowed_models, expires_at, quota) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (key_id, username, name, digest_api_key(plaintext), preview_api_key(plaintext),
                 created_at, provider_binding, allowed_ips, allowed_models, expires_at, quota),
            )
        return {"id": key_id, "username": username, "name": name, "api_key": plaintext,
                "preview": preview_api_key(plaintext), "created_at": created_at,
                "provider_binding": provider_binding, "allowed_ips": allowed_ips,
                "allowed_models": allowed_models, "expires_at": expires_at,
                "quota": quota}

    def set_quota(self, key_id: str, username: str, quota: str) -> bool:
        """整体替换一把 Key 的配额配置（空串 = 清除配额）。

        带上 username 条件：管理台只能改自己名下的 Key，否则任何登录用户都能
        改别人的配额（越权写）。返回 False 表示 Key 不存在或不属于该用户。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE api_keys SET quota = ? WHERE id = ? AND username = ?",
                (quota, key_id, username))
        return cursor.rowcount > 0

    def authenticate(self, api_key: str) -> dict[str, Any] | None:
        """校验 Key 并返回其策略行（含 provider_binding / allowed_ips / 策略）。

        命中即刷新 last_used_at：所有出口鉴权都走这里，避免再散落一处
        「用了 Key 但没记最后使用时间」。返回 None 表示 Key 不存在。

        last_used_at 写库节流到分钟级：每次请求都写会给热路径加一次写事务
        （`BEGIN IMMEDIATE` 后还会与留存汇总争写锁），而该字段展示精度只到
        分钟。距上次记录不足 `_LAST_USED_THROTTLE_SECONDS` 就不写。
        """
        digest = digest_api_key(api_key)
        row = self._db.connect().execute(
            "SELECT id, username, provider_binding, allowed_ips, allowed_models, "
            "expires_at, last_used_at, quota FROM api_keys WHERE key_digest = ?",
            (digest,)).fetchone()
        if row is None:
            return None
        now = int(time.time())
        last_used = row["last_used_at"]
        if last_used is None or now - last_used >= _LAST_USED_THROTTLE_SECONDS:
            with self._db.transaction() as conn:
                conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                             (now, row["id"]))
        # 返回值形状与旧实现一致：last_used_at 只用于上面的节流判定
        return {key: row[key] for key in
                ("id", "username", "provider_binding", "allowed_ips",
                 "allowed_models", "expires_at", "quota")}

    def verify(self, api_key: str) -> str | None:
        row = self.authenticate(api_key)
        return row["username"] if row else None

    def list_for(self, username: str, sort: str | None = None,
                 order: str | None = None, limit: int | None = None,
                 offset: int = 0) -> list[dict[str, Any]]:
        """该用户名下的 Key；默认创建时间升序。

        `limit` / `offset`：分页。`limit=None`（默认）= 全量，与旧行为一致。
        """
        clause = sql_order(sort, order, API_KEY_SORT_COLUMNS,
                           default_key="created_at", default_desc=False, tiebreak="id")
        if limit is None:
            rows = self._db.connect().execute(
                "SELECT id, username, name, preview, created_at, last_used_at, "
                "provider_binding, allowed_ips, allowed_models, expires_at, quota "
                "FROM api_keys "
                f"WHERE username = ? ORDER BY {clause}", (username,)).fetchall()
        else:
            rows = self._db.connect().execute(
                "SELECT id, username, name, preview, created_at, last_used_at, "
                "provider_binding, allowed_ips, allowed_models, expires_at, quota "
                "FROM api_keys "
                f"WHERE username = ? ORDER BY {clause} LIMIT ? OFFSET ?",
                (username, max(1, min(500, limit)), max(0, offset))).fetchall()
        return [dict(row) for row in rows]

    def count_for(self, username: str) -> int:
        """该用户名下的 Key 总数（分页端点算页数用）。"""
        row = self._db.connect().execute(
            "SELECT COUNT(*) AS n FROM api_keys WHERE username = ?", (username,)).fetchone()
        return int(row["n"])

    def count_expiring(self, username: str, before: int) -> int:
        """该用户名下**已过期或即将过期**的 Key 数（`expires_at <= before`）。

        `expires_at IS NULL` = 永不过期，不计；`<=` 让已过期的也计入——那同样
        是「该续期或该删」的待办，不是「暂时没问题」。
        """
        row = self._db.connect().execute(
            "SELECT COUNT(*) AS n FROM api_keys "
            "WHERE username = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            (username, int(before))).fetchone()
        return int(row["n"])

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
                (event_id, credential_id, _now(now),
                 1 if result.ok else 0, 1 if result.session_dead else 0,
                 result.report, result.credit, result.energy, result.streak_days, trigger),
            )
        return event_id

    def prune(self, *, keep_days: int, now: int | None = None) -> int:
        """删除超过保留期的运行记录（与 usage_events 同一保留策略入口）。

        每轮探测都可能落一行，不回收会无界增长（读侧有 LIMIT，但表本身会
        一直变大）；保留期与请求明细一致，避免「成长中心记录比请求明细还久」。
        """
        cutoff = _now(now) - keep_days * 86400
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM growth_events WHERE ts < ?", (cutoff,))
        return cursor.rowcount

    def latest_for(self, credential_id: str) -> dict[str, Any] | None:
        row = self._db.connect().execute(
            "SELECT * FROM growth_events WHERE credential_id = ? ORDER BY ts DESC LIMIT 1",
            (credential_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, credential_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.connect().execute(
            "SELECT * FROM growth_events WHERE credential_id = ? ORDER BY ts DESC LIMIT ?",
            (credential_id, max(1, min(200, limit)))).fetchall()
        return [dict(row) for row in rows]


class CreditEventRepository:
    """积分变动流水（credit_events）：只读查询，写入在 save_quota 事务内完成。

    读侧独立成仓储（与 GrowthRepository 同形），写入刻意不放在这里：
    流水必须与额度 UPDATE 同事务才能拿到正确的 before（见
    CredentialRepository._record_credit_event）。
    """

    def __init__(self, db) -> None:
        self._db = db

    def recent(self, credential_id: str, limit: int = 20, sort: str | None = None,
               order: str | None = None, ledger: str = "pool",
               ) -> list[dict[str, Any]]:
        """默认倒序返回变动记录；limit 收敛到 [1, 200] 防止一次拉爆前端。

        `ledger`：读哪一本账的流水（pool＝套餐/每日池余额 / credits＝CodeArts 积分）。
        **必须按账本过滤**：两本账的数值量级与语义都不同，混读会被当成同一本
        账的连续读数（CodeArts 老库里 pool 行是已下线的每日池口径，与积分差一个
        数量级）。未知取值回落 `pool`——与老库默认列值一致，拼错也不会把两本账
        混在一起。

        `sort` / `order`：列头排序白名单见 `CREDIT_EVENT_SORT_COLUMNS`。
        """
        ledger = ledger if ledger in CREDIT_EVENT_LEDGERS else "pool"
        clause = sql_order(sort, order, CREDIT_EVENT_SORT_COLUMNS,
                           default_key="ts", default_desc=True, tiebreak="id")
        rows = self._db.connect().execute(
            f"SELECT * FROM credit_events WHERE credential_id = ? AND ledger = ? "
            f"ORDER BY {clause} LIMIT ?",
            (credential_id, ledger, max(1, min(200, limit)))).fetchall()
        return [dict(row) for row in rows]

    def prune(self, *, keep_days: int, now: int | None = None) -> int:
        """删除超过保留期的流水（与 usage_events 同一保留策略入口）。"""
        cutoff = _now(now) - keep_days * 86400
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM credit_events WHERE ts < ?", (cutoff,))
        return cursor.rowcount


class AlertRepository:
    """运维告警事件（alert_events）：落库 + 去重静默查询。

    为什么独立仓储：告警是「管理台要回看的历史」，与凭证/统计无关；写入与
    读取都由后台 AlertTask 与 /api/alerts 端点使用。静默去重靠
    `last_ts(rule, scope)`——同一条告警在静默窗内不重复落库，避免周期性
    评估把表刷屏。
    """

    def __init__(self, db) -> None:
        self._db = db

    def record(self, *, rule: str, severity: str, scope: str, message: str,
               detail: str = "", delivered: bool = False,
               delivery_error: str | None = None,
               now: int | None = None) -> str:
        alert_id = _new_id("alert")
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO alert_events (id, ts, rule, severity, scope, message, "
                "detail, delivered, delivery_error) VALUES (?,?,?,?,?,?,?,?,?)",
                (alert_id, _now(now), rule,
                 severity, scope, message, detail, 1 if delivered else 0,
                 delivery_error),
            )
        return alert_id

    def recent(self, limit: int = 50, sort: str | None = None,
               order: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """默认倒序返回最近告警；limit 收敛到 [1, 200] 防止一次拉爆前端。

        `sort` / `order`：列头排序白名单见 `ALERT_SORT_COLUMNS`。
        `offset`：分页偏移（非负）。
        """
        clause = sql_order(sort, order, ALERT_SORT_COLUMNS,
                           default_key="ts", default_desc=True, tiebreak="id")
        rows = self._db.connect().execute(
            f"SELECT * FROM alert_events ORDER BY {clause} LIMIT ? OFFSET ?",
            (max(1, min(200, limit)), max(0, offset))).fetchall()
        return [dict(row) for row in rows]

    def count_all(self) -> int:
        """告警总数（分页端点算页数用）。"""
        row = self._db.connect().execute(
            "SELECT COUNT(*) AS n FROM alert_events").fetchone()
        return int(row["n"])

    def count_unresolved(self) -> int:
        """**仍在告警中**的条数（`resolved_at IS NULL`）——侧栏角标用。

        与 `count_all()` 不是一回事：那个含已恢复的历史行，是分页总数。
        角标只该数「现在还没好」的，否则恢复后角标永远不灭。
        """
        row = self._db.connect().execute(
            "SELECT COUNT(*) AS n FROM alert_events WHERE resolved_at IS NULL").fetchone()
        return int(row["n"])

    def unresolved(self) -> list[dict[str, Any]]:
        """仍在告警中的记录（`resolved_at IS NULL`），按时间升序。

        供 `AlertTask` 首轮重建进程内「告警中」集合：迟滞状态机不落库、重启
        即失；不重建的话，重启前落库的告警永远等不到恢复——规则早已不再命中，
        却没有任何一轮评估会去写 `resolved_at`（恢复分支只遍历进程内的
        `_active`），角标与「已恢复」标记就永久卡住。

        同一 `(rule, scope)` 可能有多行（静默窗过后仍命中会再记一条提醒）：
        调用方按键去重、以最后一行为准。
        """
        rows = self._db.connect().execute(
            "SELECT rule, scope, severity, message, detail FROM alert_events "
            "WHERE resolved_at IS NULL ORDER BY ts, id").fetchall()
        return [dict(row) for row in rows]

    def last_ts(self, rule: str, scope: str) -> int | None:
        """该 (规则, 对象) 最近一次落库时刻（静默去重用）；无记录返回 None。"""
        row = self._db.connect().execute(
            "SELECT MAX(ts) AS ts FROM alert_events WHERE rule = ? AND scope = ?",
            (rule, scope)).fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    def resolve_by_id(self, alert_id: str, now: int | None = None) -> int:
        """按 id 手动标记一条告警已恢复，返回受影响行数（0 = 不存在或已恢复）。

        给运维一个人工出口：规则已下线 / 误报 / 告警任务被关掉时，状态机不会
        再有机会写 `resolved_at`，那行就会一直挂在「未恢复」里、角标永远亮。
        只动这一行——不做「按 (rule, scope) 批量」，免得手滑一次把仍在告警的
        同组记录一起收掉。
        """
        resolved_at = _now(now)
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE alert_events SET resolved_at = ? "
                "WHERE id = ? AND resolved_at IS NULL", (resolved_at, alert_id))
        return cursor.rowcount

    def resolve(self, rule: str, scope: str, now: int | None = None) -> int:
        """给该 (规则, 对象) **全部尚未恢复**的告警写 `resolved_at`，返回标记行数。

        一次故障期间可能落多条：静默窗过后仍命中会再记一条「提醒」，它们同属
        一次未恢复的故障，恢复时自然该一起标记。只标最近一条会让更早的提醒
        永远停在「未恢复」——角标数的是 `resolved_at IS NULL`，于是永远清不
        干净，看起来像「故障一直没好」。

        返回受影响行数（0 = 没有待恢复的行，例如告警已被清理）。
        """
        resolved_at = _now(now)
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE alert_events SET resolved_at = ? "
                "WHERE rule = ? AND scope = ? AND resolved_at IS NULL",
                (resolved_at, rule, scope))
        return cursor.rowcount

    def prune(self, *, keep_days: int, now: int | None = None) -> int:
        """删除超过保留期的告警（与 usage_events / 审计同一保留策略入口）。"""
        cutoff = _now(now) - keep_days * 86400
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM alert_events WHERE ts < ?", (cutoff,))
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
                (key, value, _now(now)),
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
    """管理台用户账号（users）。

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

    def list_all(self, sort: str | None = None,
                 order: str | None = None,
                 limit: int | None = None,
                 offset: int = 0) -> list[dict[str, Any]]:
        """按用户名排序的完整行；展示层负责剔除敏感列。

        `sort` / `order`：排序白名单见 `USER_SORT_COLUMNS`；默认用户名升序。

        `limit` / `offset`：分页。`limit=None`（默认）= 不分页返回全量，与旧行为
        一致（多处内部调用点依赖全量）。传入时收敛到 [1, 500]，`offset` 非负。
        """
        clause = sql_order(sort, order, USER_SORT_COLUMNS,
                           default_key="username", default_desc=False, tiebreak="username")
        if limit is None:
            rows = self._db.connect().execute(
                f"SELECT * FROM users ORDER BY {clause}").fetchall()
        else:
            rows = self._db.connect().execute(
                f"SELECT * FROM users ORDER BY {clause} LIMIT ? OFFSET ?",
                (max(1, min(500, limit)), max(0, offset))).fetchall()
        return [dict(row) for row in rows]

    def count_all(self) -> int:
        """用户总数（分页端点算页数用）。"""
        row = self._db.connect().execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])

    def count_pending_activation(self) -> int:
        """**待激活**账号数（`activation_digest IS NOT NULL`）——侧栏角标用。

        到期令牌也算在内：库里有令牌但已过期时用户其实激活不了，需要管理员
        重新签发（`src/api/activate.py` 会在过期时拒绝），所以它是待办而非噪声。
        """
        row = self._db.connect().execute(
            "SELECT COUNT(*) AS n FROM users "
            "WHERE activation_digest IS NOT NULL").fetchone()
        return int(row["n"])

    def list_usernames(self) -> tuple[str, ...]:
        rows = self._db.connect().execute(
            "SELECT username FROM users ORDER BY username").fetchall()
        return tuple(row["username"] for row in rows)

    def create(self, username: str, password_hash: str, *, role: str = "viewer",
               enabled: bool = True, must_change_password: bool = False,
               created_by: str | None = None, activation_digest: str | None = None,
               activation_expires_at: int | None = None, now: int | None = None) -> None:
        timestamp = _now(now)
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
                 _now(now), username),
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
                 _now(now), username),
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
                 _now(now), username),
            )
        return cursor.rowcount > 0

    def set_activation_token(self, username: str, digest: str, expires_at: int, *,
                             now: int | None = None) -> bool:
        """挂一次性激活令牌（登录前使用，故**不** bump epoch）。"""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET activation_digest = ?, activation_expires_at = ?, "
                "updated_at = ? WHERE username = ?",
                (digest, expires_at, _now(now), username),
            )
        return cursor.rowcount > 0

    def set_policy(self, username: str, *, allowed_models: str, quota: str,
                   now: int | None = None) -> bool:
        """整体替换用户级策略（模型白名单 + 配额），空串 = 不限制。

        只影响该用户在管理台 Playground 的会话直连；其 API Key 的策略在
        `api_keys` 行上，是另一套（Key 可单独设白名单/配额，两者互不覆盖）。
        返回 False 表示用户不存在。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET allowed_models = ?, quota = ?, updated_at = ? "
                "WHERE username = ?",
                (allowed_models, quota, _now(now), username),
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
    """审计流水（audit_events）。

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
                (event_id, _now(now), actor, action,
                 target, detail, ip, 1 if ok else 0),
            )
        return event_id

    def query(self, *, actor: str | None = None, action: str | None = None,
              since: int | None = None, before: int | None = None,
              limit: int = 100, offset: int = 0,
              sort: str | None = None, order: str | None = None) -> list[dict[str, Any]]:
        """默认按 ts 倒序；limit 收敛到 [1, 500]，offset 非负。

        `sort` / `order`：列头排序白名单见 `AUDIT_SORT_COLUMNS`。
        """
        where, params = self._filters(actor=actor, action=action, since=since, before=before)
        clause = sql_order(sort, order, AUDIT_SORT_COLUMNS,
                           default_key="ts", default_desc=True, tiebreak="id")
        rows = self._db.connect().execute(
            f"SELECT * FROM audit_events {where} ORDER BY {clause} LIMIT ? OFFSET ?",
            (*params, max(1, min(500, limit)), max(0, offset))).fetchall()
        return [dict(row) for row in rows]

    def count(self, *, actor: str | None = None, action: str | None = None,
              since: int | None = None, before: int | None = None) -> int:
        """与 `query` 同一套过滤条件下的总条数（分页端点算页数用）。

        过滤条件必须与 `query` 完全一致，否则页数会与实际结果对不上。
        """
        where, params = self._filters(actor=actor, action=action, since=since, before=before)
        row = self._db.connect().execute(
            f"SELECT COUNT(*) AS n FROM audit_events {where}", params).fetchone()
        return int(row["n"])

    @staticmethod
    def _filters(*, actor: str | None, action: str | None,
                 since: int | None, before: int | None) -> tuple[str, list[Any]]:
        """`query` / `count` 共用的 WHERE 片段与参数（两处口径必须同源）。"""
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
        return where, params

    def prune(self, *, keep_days: int, now: int | None = None) -> int:
        cutoff = _now(now) - keep_days * 86400
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM audit_events WHERE ts < ?", (cutoff,))
        return cursor.rowcount

class ModelProbeRepository:
    """主动探活结论（`model_probes`）：管理台展示 + 「探活成功清负缓存」+
    判死期间从模型列表摘除。

    与 `credential_model_cooldowns` 严格分工：那张表是调度**权威**状态（真实
    请求的失败驱动，直接决定选号），本表只是观测记录。把探测结论写进冷却表
    会让一次探测直接改变选号结果，而「探测失败」未必是模型本身的问题（额度
    耗尽、限流、网络抖动都会失败）。唯一的出口影响是 `failed()`：判死的
    (渠道, 模型) 从模型列表与别名表里整对抽掉，**不看负缓存是否仍在**——
    上游目录撒谎（officeAce 的 `/v2/models` 挂着推理端点 404 的模型）时，
    判死往往没有负缓存兜底，旧口径下这些模型会一直留在 Playground 里。

    隐藏**没有到期时间**：恢复入口只有「再探一次成功」（`failed()` 的行由
    `ModelProbeTask.run_once` 每轮复探，人工也可点「立即探活」）。到期口径
    若挂在负缓存上，负缓存过期后没人再探，隐藏就变成了永久隐藏——现在两者
    解耦：判死即隐藏，复探即恢复。
    """

    def __init__(self, db) -> None:
        self._db = db

    _COLUMNS = ("provider, model, state, failures, last_probe_at, last_ok_at, "
                "last_error, manual, updated_at")
    # `record` 不写 manual（接管标记只由 set_manual 改）：INSERT 的列必须与
    # 占位符一一对应，否则会撞「Incorrect number of bindings」。
    _RECORD_COLUMNS = ("provider, model, state, failures, last_probe_at, last_ok_at, "
                       "last_error, updated_at")

    def all(self) -> dict[tuple[str, str], dict[str, Any]]:
        """整表读回 `{(渠道, 模型): 行}`：表很小，只在探过的模型上才有行。"""
        rows = self._db.connect().execute(
            f"SELECT {self._COLUMNS} FROM model_probes").fetchall()
        return {(row["provider"], row["model"]): dict(row) for row in rows}

    def get(self, provider: str, model: str) -> dict[str, Any] | None:
        row = self._db.connect().execute(
            f"SELECT {self._COLUMNS} FROM model_probes "
            "WHERE provider = ? AND model = ?", (provider, model)).fetchone()
        return dict(row) if row is not None else None

    def record(self, *, provider: str, model: str, state: str,
               error: str = "", now: int | None = None) -> None:
        """落一条探活结论。

        `failures` 只在 `failed` 时累加、`ok`/`inconclusive` 时归零。归零是
        刻意的：额度耗尽、限流、网络抖动都不构成「模型本身有问题」的连续
        证据，继续累计会让一串无关故障凑成「连续失败」，把好模型判死。
        `last_ok_at` 只在 `ok` 时前移，其余状态保留上一次成功时刻。
        """
        stamp = _now(now)
        with self._db.transaction() as conn:
            conn.execute(
                f"INSERT INTO model_probes ({self._RECORD_COLUMNS}) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider, model) DO UPDATE SET "
                "state = excluded.state, "
                "failures = CASE WHEN excluded.state = 'failed' "
                "                THEN model_probes.failures + 1 ELSE 0 END, "
                "last_probe_at = excluded.last_probe_at, "
                "last_ok_at = CASE WHEN excluded.state = 'ok' "
                "                  THEN excluded.last_probe_at "
                "                  ELSE model_probes.last_ok_at END, "
                "last_error = excluded.last_error, "
                "updated_at = excluded.updated_at",
                (provider, model, state, 1 if state == "failed" else 0,
                 stamp, stamp if state == "ok" else 0, error, stamp))

    def set_manual(self, *, provider: str, model: str, manual: str,
                   now: int | None = None) -> None:
        """人工接管标记（`''` = 交还自动探活）。行不存在时先建一条空结论行。"""
        stamp = _now(now)
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO model_probes (provider, model, manual, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(provider, model) DO UPDATE SET "
                "manual = excluded.manual, updated_at = excluded.updated_at",
                (provider, model, manual, stamp))

    def failed(self) -> set[tuple[str, str]]:
        """「探活判死」的 `(渠道, 模型小写原名)`：模型列表据此整对抽掉。

        判据只有 `state='failed'`（上游明确 `BLOCKED` / `INVALID`）。**不再**
        要求同时挂着 blocked 负缓存：人工「立即探活」判死时通常没有负缓存
        （负缓存只由真实请求写入），旧口径下这些模型会一直留在 Playground /
        `/v1/models` 里——上游目录撒谎（officeAce `ModelArts.81009`）时尤其
        明显。隐藏的到期由复探负责：`ModelProbeTask.run_once` 会把这些行一起
        探，探活成功（`state='ok'`）即恢复展示。

        模型名输出统一小写以匹配模型列表缓存的键（`model_probes.model` 存的是
        上游原名）。
        """
        rows = self._db.connect().execute(
            "SELECT DISTINCT provider, LOWER(model) AS model "
            "FROM model_probes WHERE state = 'failed'").fetchall()
        return {(row["provider"], row["model"]) for row in rows}

    def failed_rows(self) -> list[dict[str, Any]]:
        """判死行的 `(渠道, 模型原名)`：供探活任务复探（探活成功即恢复展示）。

        与 `failed()` 同一判据、不同用途：那个给小写集合做展示过滤，这个给
        探活任务回放上游原名（上游只认原名，小写可能打不通）。
        """
        rows = self._db.connect().execute(
            "SELECT provider, model FROM model_probes WHERE state = 'failed' "
            "ORDER BY provider, model").fetchall()
        return [dict(row) for row in rows]

    def manual_marked(self) -> set[tuple[str, str]]:
        """被人工接管的 (渠道, 模型)：自动探活整行跳过。"""
        rows = self._db.connect().execute(
            "SELECT provider, model FROM model_probes WHERE manual != ''").fetchall()
        return {(row["provider"], row["model"]) for row in rows}
