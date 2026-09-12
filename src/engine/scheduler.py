"""调度器：选号 + 冷却状态机 + pin（Q12=B，Q26 三态健康度）。

纯逻辑：操作 Candidate 数据类，不碰数据库；持久化由调用方完成。
这样 100% 覆盖可以用假数据达成（Q14=B）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..provider.base import ErrKind

PLAN_COOLDOWN_SECONDS = 12 * 3600
SOFT_COOLDOWN_SECONDS = 60
OTHER_COOLDOWN_SECONDS = 10 * 60
ERR_THRESHOLD = 3
MAX_ROTATE = 3


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

    def is_selectable(self, now: int) -> bool:
        if self.disabled or not self.enabled:
            return False
        return not (self.cooling_until is not None and self.cooling_until > now)


@dataclass(frozen=True)
class ErrorOutcome:
    """note_error 的结果：调用方据此持久化。"""

    disabled: bool = False
    cooling_until: int | None = None
    err_count: int = 0


class Scheduler:
    def __init__(
        self,
        *,
        max_rotate: int = MAX_ROTATE,
        err_threshold: int = ERR_THRESHOLD,
        plan_cooldown: int = PLAN_COOLDOWN_SECONDS,
        soft_cooldown: int = SOFT_COOLDOWN_SECONDS,
        other_cooldown: int = OTHER_COOLDOWN_SECONDS,
    ) -> None:
        self.max_rotate = max_rotate
        self.err_threshold = err_threshold
        self._cooldowns = {
            ErrKind.PLAN: plan_cooldown,
            ErrKind.SOFT: soft_cooldown,
            ErrKind.OTHER: other_cooldown,
        }

    # ---------------------------------------------------------------- 选号

    def select(self, candidates: Iterable[Candidate], tried: set[str], now: int) -> str | None:
        """返回应使用的 credential_id；无可用的返回 None。

        排序规则：pin 优先 → known 降序 → unknown → exhausted 垫底。
        """
        pool = [
            c for c in candidates
            if c.is_selectable(now) and c.credential_id not in tried
        ]
        if not pool:
            return None
        pinned = [c for c in pool if c.pinned]
        chosen = sorted(
            pinned or pool,
            key=lambda c: (_rank(c.health), -(_health_value(c.health)), c.credential_id),
        )
        return chosen[0].credential_id

    def should_rotate(self, tried: set[str]) -> bool:
        """还能继续换号吗（≤ max_rotate 次尝试）。"""
        return len(tried) < self.max_rotate

    # ------------------------------------------------------------ 结果反馈

    def note_error(self, candidate: Candidate, kind: ErrKind, now: int) -> ErrorOutcome:
        """按错误分类更新冷却/禁用状态。

        DEAD    → 硬禁用
        PLAN    → 长冷却，清空 err_count
        SOFT    → 短冷却，不累计 err_count（防雪崩）
        OTHER   → 累计，达到阈值 → 中冷却
        """
        if kind is ErrKind.DEAD:
            return ErrorOutcome(disabled=True, err_count=0)
        if kind is ErrKind.PLAN:
            return ErrorOutcome(cooling_until=now + self._cooldowns[ErrKind.PLAN], err_count=0)
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
