"""调度器：选号 + 冷却状态机 + pin（Q12=B，Q26 三态健康度）。

纯逻辑：操作 Candidate 数据类，不碰数据库；持久化由调用方完成。
这样 100% 覆盖可以用假数据达成（Q14=B）。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..config import live
from ..provider.base import MODEL_SCOPED_KINDS, ErrKind

PLAN_COOLDOWN_SECONDS = 12 * 3600
SOFT_COOLDOWN_SECONDS = 60
OTHER_COOLDOWN_SECONDS = 10 * 60
ERR_THRESHOLD = 3
MAX_ROTATE = 3
# 模型级限流（6004）软冷却基数与封顶（有界指数退避）
MODEL_COOLDOWN_SECONDS = 600
MODEL_COOLDOWN_MAX_SECONDS = 2 * 3600
# 并发会话打满（CodeArts TM.00001041）固定短冷却：它是「在途满了」的瞬态信号，
# 不是额度耗尽——在途请求排空即恢复（实测 CodeArts 平均 9s、最大 38s），60s
# 覆盖排空加余量。刻意**不翻倍**：重复打满说明持续高并发，重 armed 60s 即可；
# 套 MODEL 的 600s 起步、翻倍到 2h 会把一次瞬态打满变成 10min 起的长时间不可用。
CONCURRENCY_COOLDOWN_SECONDS = 60
# 「该后端无此模型」（11102）负缓存：6h 起，翻倍封顶 24h
BLOCKED_BASE_SECONDS = 6 * 3600
BLOCKED_MAX_SECONDS = 24 * 3600
BLOCKED_SHIFT_MAX = 2
# 「余额不足」（402 / 14018）硬冷却的目标时刻：次日 04:00（签到任务全天恢复）
CREDIT_RESET_HOUR = 4
# 主到期排序窗口：把「距到期 ≤ 该时长」的额度加总，作为选号第一排序指标（多者先用）
# CodeBuddy 是每日 100 积分 × N 的小包，36h 覆盖今天与后天的到期点；
# CodeArts 的每日积分池（1000 万 token 折 1000 积分）0 点清零，到期点始终落在该窗口内
# （故只要有额度就先烧它）
EXPIRY_WINDOW_SECONDS = 36 * 3600
# 次要到期排序窗口：仅当主指标打平（最常见的是都为 0）时才启用，避免只看 36h
# 而漏掉一周内仍会过期的额度。7 天覆盖 CodeBuddy 一个完整的小包到期周期
SECONDARY_EXPIRY_WINDOW_SECONDS = 7 * 86400


def expiring_credits(
    ladder: list[tuple[int, float]] | None,
    window_seconds: int,
    now: int,
) -> float:
    """窗口内即将到期的额度：`now < 到期 <= now + window` 的各包剩余之和。

    窗口 ≤0 或无阶梯（渠道无到期信息）为 0。调度排序与管理台展示共用此口径。
    单位统一为积分（CodeArts 的上游 token 已在解析层折成积分），排序只比数值。
    """
    if not ladder or window_seconds <= 0:
        return 0
    return sum(
        amount for expiring_at, amount in ladder
        if now < expiring_at <= now + window_seconds
    )


def expiry_windows(primary: int, secondary: int) -> tuple[int, int]:
    """生效的主/次到期窗口：主窗口 ≤0 视为关闭整套到期排序，次窗口一并归零。

    两级是同一条排序链的两档，主窗口是总开关；若不联动，只关主窗口时
    次窗口仍会单独排序，「关闭到期指标」的既有语义就被悄悄改掉了。
    选号（Scheduler）与管理台展示（list_all）共用此函数，界面数字与
    选号顺序不会漂移。
    """
    if primary <= 0:
        return 0, 0
    return primary, secondary


@dataclass(frozen=True)
class ModelCooldown:
    """一条 (凭证, 模型) 冷却记录（含退避次数，供指数升级）。

    `reason` 区分「模型级限流」与「该后端无此模型」负缓存：两者冷却时长
    与清除条件不同——限流对齐上游重置墙钟（成功后也不解除），负缓存
    一旦该模型在成功响应里出现就说明该账号其实有这个模型，立即清掉。
    """

    cooling_until: int = 0
    hits: int = 0
    reason: str = "model"


def next_credit_reset(now: int) -> int:
    """距现在最近的下一个本地 04:00（余额耗尽等签到恢复）。

    凌晨 00:00–04:00 之间返回当天 04:00（此时签到尚未跑，等当天签到即可恢复）；
    04:00 及之后返回次日 04:00。用本地时区（与签到任务的日边界一致）。
    """
    local = time.localtime(now)
    reset = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                         CREDIT_RESET_HOUR, 0, 0, 0, 0, -1))
    if local.tm_hour >= CREDIT_RESET_HOUR:
        reset += 86400
    return int(reset)


def model_cooldown_duration(hits: int) -> int:
    """模型级软冷却时长：基数起按命中次数翻倍，封顶 MODEL_COOLDOWN_MAX_SECONDS。"""
    if hits <= 1:
        return MODEL_COOLDOWN_SECONDS
    return min(MODEL_COOLDOWN_SECONDS << (hits - 1), MODEL_COOLDOWN_MAX_SECONDS)


def blocked_backoff_duration(hits: int) -> int:
    """负缓存 TTL：BLOCKED_BASE 起按 `1 << min(hits-1, BLOCKED_SHIFT_MAX)` 放大，封顶。"""
    shift = min(max(hits - 1, 0), BLOCKED_SHIFT_MAX)
    return min(BLOCKED_BASE_SECONDS << shift, BLOCKED_MAX_SECONDS)


@dataclass(frozen=True)
class Candidate:
    """一条可调度凭证的调度相关快照。"""

    credential_id: str
    provider: str
    health: int | None = None          # None=unknown；0-100=known；-1=exhausted
    cooling_until: int | None = None
    disabled: bool = False
    enabled: bool = True
    err_count: int = 0
    pinned: bool = False
    quota_remaining: float | None = None   # 账户剩余积分；同健康度平级时多者优先
    cycle_end: int | None = None       # 额度最早到期（epoch）；无到期信息的渠道为 None
    expiry_ladder: list[tuple[int, float]] | None = None  # [(到期 epoch, 该包剩余额度)]
    # 本凭证请求**当前模型**的消耗倍率（credit_rate）：executor 按模型目录现查
    # 注入。0 = 免费渠道，参与「免费优先」排序档（见 Scheduler.select）；
    # None = 未知 / 该模型无倍率数据（如 CodeArts 福利模型），不参与。
    credit_rate: float | None = None
    # (凭证, 模型) 冷却表：model → ModelCooldown。模型级限流只写这里，
    # 不写 cooling_until，因此同账号的其他模型仍然可选
    model_cooldowns: Mapping[str, ModelCooldown] | None = None

    def expiry_credits(self, now: int, window: int) -> float:
        """窗口内即将到期的积分总量（不含已过期与已用完的包）。

        窗口 ≤ 0 或渠道无到期信息时恒为 0，选号退回健康度排序。
        """
        return expiring_credits(self.expiry_ladder, window, now)

    def model_cooling_until(self, model: str | None) -> int | None:
        """该模型在本凭证上的冷却截止时刻；无模型级冷却返回 None。"""
        if not model or not self.model_cooldowns:
            return None
        entry = self.model_cooldowns.get(model)
        return entry.cooling_until if entry is not None else None

    def is_selectable(self, now: int, model: str | None = None) -> bool:
        if self.disabled or not self.enabled:
            return False
        if self.cooling_until is not None and self.cooling_until > now:
            return False
        model_until = self.model_cooling_until(model)
        return not (model_until is not None and model_until > now)


@dataclass(frozen=True)
class ErrorOutcome:
    """note_error 的结果：调用方据此持久化。"""

    disabled: bool = False
    cooling_until: int | None = None
    err_count: int = 0
    # 模型级条目：model → ModelCooldown（仅 MODEL/BLOCKED 产生），
    # 由调用方按该映射写库；账号级冷却出现时这里恒为空（互斥）
    model_cooldowns: Mapping[str, ModelCooldown] | None = None


class Scheduler:
    def __init__(
        self,
        *,
        max_rotate: int = MAX_ROTATE,
        err_threshold: int = ERR_THRESHOLD,
        plan_cooldown: int = PLAN_COOLDOWN_SECONDS,
        soft_cooldown: int = SOFT_COOLDOWN_SECONDS,
        other_cooldown: int = OTHER_COOLDOWN_SECONDS,
        expiry_window: int = EXPIRY_WINDOW_SECONDS,
        secondary_expiry_window: int = SECONDARY_EXPIRY_WINDOW_SECONDS,
    ) -> None:
        self.max_rotate = max_rotate
        self.err_threshold = err_threshold
        # 到期窗口可热更（B3.2）：存「取值器」而不是快照，每次选号读当前值。
        # expiry_windows 的归一化（主窗口 ≤0 → 次窗口一并归零）必须同步在
        # 读取时做，否则热更把主窗口改成 0、次窗口仍会单独参与排序。
        self._expiry_window = live(expiry_window)
        self._secondary_expiry_window = live(secondary_expiry_window)
        self._cooldowns = {
            ErrKind.PLAN: plan_cooldown,
            ErrKind.SOFT: soft_cooldown,
            ErrKind.OTHER: other_cooldown,
        }

    @property
    def expiry_window(self) -> int:
        """生效的主窗口（含归一化）；只读，供展示与测试断言。"""
        return expiry_windows(self._expiry_window(), self._secondary_expiry_window())[0]

    @property
    def secondary_expiry_window(self) -> int:
        """生效的次窗口；主窗口 ≤0 时恒为 0（与 expiry_windows 同口径）。"""
        return expiry_windows(self._expiry_window(), self._secondary_expiry_window())[1]

    # ---------------------------------------------------------------- 选号

    def select(self, candidates: Iterable[Candidate], tried: set[str],
               now: int) -> str | None:
        """返回应使用的 credential_id；无可用的返回 None。

        排序规则：**免费优先**（候选中存在 x0 免费渠道时，免费渠道整体排前；
        仅当本次候选带有倍率数据——executor 注入的 credit_rate——才生效）→
        pin 优先 → 主窗口（36h）内即将到期额度多者优先 → 次窗口（7 天）内
        即将到期额度多者优先 → known 降序 → unknown → exhausted 垫底 →
        健康度打平时**账户剩余积分多者优先**。到期额度优先于健康度：快过期
        的先用掉，避免白丢；但免费优先又压过到期额度——省下的真金白银比
        「几百积分快过期先用」值钱（CodeBuddy 36h 窗口到期积分实测峰值
        ~400，而免费渠道跑一条中等回复就省几十积分、长会话累计破百）。
        两级窗口按字典序比较，主窗口打平（含都为 0）时才轮到次窗口，再打平
        才比健康度。剩余积分只作健康度的**打平键**，不会越级把低健康度的
        高余额号顶上去（健康度是「剩余/总量」比例，101% 也只是 100，同比例
        下多留些余额以备后用）。主窗口 ≤0 视为关闭整套到期排序（次窗口一并
        归零，见 `expiry_windows`），此时退回纯健康度排序。

        模型级冷却的过滤由调用方在候选集上完成（executor._select）：
        每个候选要按**自己所属上游**的原始模型名查冷却表，选号器不掌握
        provider→模型名 的映射，硬塞进来只会造成两处各查一半。
        """
        pool = [
            c for c in candidates
            if c.is_selectable(now) and c.credential_id not in tried
        ]
        if not pool:
            return None
        pinned = [c for c in pool if c.pinned]
        free_present = any(c.credit_rate == 0 for c in pool)
        primary, secondary = expiry_windows(
            self._expiry_window(), self._secondary_expiry_window())
        chosen = sorted(
            pinned or pool,
            key=lambda c: (_free_first_rank(c, free_present),
                           -c.expiry_credits(now, primary),
                           -c.expiry_credits(now, secondary),
                           _rank(c.health), -(_health_value(c.health)),
                           -_remaining_value(c.quota_remaining), c.credential_id),
        )
        return chosen[0].credential_id

    def should_rotate(self, attempts: int) -> bool:
        """还能继续换号吗（已真正尝试 < max_rotate 次）。

        入参是**实际打过的凭证数**，不是 `tried` 集合大小：INVALID 时
        `_skip_provider` 会把该上游全部凭证塞进 `tried`（只用于排除候选），
        用集合大小当预算会让「一个不认模型的上游占满 3 个名额」，从而永远
        试不到真正持有该模型的上游（H2）。
        """
        return attempts < self.max_rotate

    # ------------------------------------------------------------ 结果反馈

    def note_error(self, candidate: Candidate, kind: ErrKind, now: int, *,
                   model: str | None = None) -> ErrorOutcome:
        """按错误分类更新冷却/禁用状态。

        DEAD    → 硬禁用
        PLAN    → 12h 长冷却，清空 err_count（权益耗尽）
        CREDIT  → 冷却到次日 04:00（余额不足，等签到恢复）
        SOFT    → 短冷却，不累计 err_count（防雪崩）
        OTHER   → 累计，达到阈值 → 中冷却
        MODEL / BLOCKED / CONCURRENCY → 只写 (凭证, 模型) 条目，不动账号级状态；
                           其中 CONCURRENCY 用固定短冷却，不翻倍
                          账号级冷却出现时清空模型级条目（防豁免泄漏）
        REQUEST → 零动作：不是账号的问题，换号但绝不惩罚凭证
        """
        if kind is ErrKind.REQUEST:
            return ErrorOutcome(err_count=candidate.err_count)
        if kind is ErrKind.DEAD:
            return ErrorOutcome(disabled=True, err_count=0)
        if kind is ErrKind.CREDIT:
            return ErrorOutcome(cooling_until=next_credit_reset(now), err_count=0)
        if kind is ErrKind.PLAN:
            return ErrorOutcome(cooling_until=now + self._cooldowns[ErrKind.PLAN],
                                err_count=0)
        if kind in MODEL_SCOPED_KINDS:
            if not model:
                # 无模型名可归因（流内错误事件未带 model 等）：退化为账号级软冷却，
                # 宁可保守也不要写一条影响不到任何选号的孤儿记录
                return ErrorOutcome(cooling_until=now + self._cooldowns[ErrKind.SOFT],
                                    err_count=candidate.err_count)
            if kind is ErrKind.BLOCKED:
                reason = "blocked"
            elif kind is ErrKind.CONCURRENCY:
                reason = "concurrency"
            else:
                reason = "model"
            existing = (candidate.model_cooldowns or {}).get(model)
            # 换了原因就重新计数：限流与「无此模型」的退避基数/封顶完全不同，
            # 沿用对方的 hits 会得到既非 600s 也非 6h 的第三种时长
            hits = (existing.hits + 1
                    if existing is not None and existing.reason == reason else 1)
            # 并发打满固定短冷却、不翻倍：在途排空即恢复，重复打满重 armed 即可
            if kind is ErrKind.CONCURRENCY:
                duration = CONCURRENCY_COOLDOWN_SECONDS
            else:
                duration = (blocked_backoff_duration(hits) if kind is ErrKind.BLOCKED
                            else model_cooldown_duration(hits))
            return ErrorOutcome(err_count=candidate.err_count,
                                model_cooldowns={model: ModelCooldown(
                                    cooling_until=now + duration, hits=hits,
                                    reason=reason)})
        if kind is ErrKind.SOFT:
            return ErrorOutcome(cooling_until=now + self._cooldowns[ErrKind.SOFT],
                                err_count=candidate.err_count)
        count = candidate.err_count + 1
        if count >= self.err_threshold:
            return ErrorOutcome(cooling_until=now + self._cooldowns[ErrKind.OTHER], err_count=0)
        return ErrorOutcome(err_count=count)


def _rank(health: int | None) -> int:
    """known=0，unknown=1，exhausted=2（升序，越小越优先）。"""
    if health is None:
        return 1
    if health < 0:
        return 2
    return 0


def _health_value(health: int | None) -> int:
    """用于降序排：unknown/exhausted 归一为最低（0）。"""
    if health is None or health < 0:
        return 0
    return health


def _remaining_value(remaining: float | None) -> float:
    """健康度打平键：账户剩余积分，未知（None）归零排最后。

    只比数值不比单位——到期排序已把 CodeArts 的 token 折成积分
    （见 `expiring_credits`），此处沿用同一口径。
    """
    return remaining if remaining is not None else 0.0


def _free_first_rank(candidate: Candidate, free_present: bool) -> int:
    """免费优先档：0 = 免费渠道，1 = 其余。

    候选里存在 x0 渠道（credit_rate == 0.0）时，免费渠道整体排到所有付费渠道
    之前——省下的真金白银优先于「快过期先用」（到期积分排序）。0.08 与 0.29
    这类付费倍率不参与此档的内部先后（那仍交给到期/健康度排序）；模型倍率
    未知（None）的渠道与付费渠道同档，不因数据缺失免费优先。
    """
    if not free_present:
        return 0
    return 0 if candidate.credit_rate == 0 else 1
