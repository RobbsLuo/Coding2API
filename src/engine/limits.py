"""凭证级限额护栏：判定（纯逻辑）+ 选号前快照缓存。

与 `src/db/limits.py` 的分工：那边只管存取（`credential_limits` 表 + 当日
用量聚合），这里只做判定与缓存，**不碰数据库**——判定路径可用假数据达成
100% 覆盖。

护栏口径（四个，缺省全关；「当日」= 北京时间自然日，与签到同一时区）：

- `keep_credits`：账户剩余积分 ≤ 该值时整号停用（给号留底，不把额度跑空）
- `daily_tokens`：当日 token（输入 + 输出）达到上限时整号停用
- `daily_credits`：当日 credit 达到上限时整号停用
- `per_model_tokens`：某模型当日 token 达到上限时**只停该模型**，其余模型可用

为什么在选号前过滤而不是写进 `Candidate.is_selectable`：限额判定依赖「当日
聚合用量」，那是每请求都变的动态数据；而 `is_selectable` 是纯函数（只吃
凭证自身的静态/冷却状态）。把动态用量塞进 Candidate 会让 repo/池健康计数
（`pool_counts`）也去查一遍用量，得不偿失。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

# 与签到、活跃上报同一时区：上游的「每日」按北京时间切，限额也照此对齐，
# 避免出现「上游刚重置、本服务还认为今天已用满」的半天错位。
_CN_TZ = ZoneInfo("Asia/Shanghai")


def day_start(now: int) -> int:
    """now 所在北京日的 0 点 epoch（当日用量计数的起点）。"""
    moment = datetime.fromtimestamp(now, _CN_TZ)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


@dataclass(frozen=True)
class CredentialLimits:
    """一条凭证的限额配置；未配置 / ≤0 的项 = 该护栏关闭。"""

    credential_id: str
    keep_credits: float | None = None
    daily_tokens: int | None = None
    daily_credits: float | None = None
    per_model_tokens: Mapping[str, int] = field(default_factory=dict)
    updated_at: int = 0
    updated_by: str | None = None

    def is_empty(self) -> bool:
        """四个护栏全关（且无有效的按模型项）：据此删除行而不是留一条空配置。

        按模型项里 ≤0 / 非数值的项视为关闭，不算「有配置」——否则
        `{"m": 0}` 会留下一条永远不生效的空配置行。
        """
        active_models = any(_enabled(cap) for cap in self.per_model_tokens.values())
        return not (
            _enabled(self.keep_credits) or _enabled(self.daily_tokens)
            or _enabled(self.daily_credits) or active_models)

    def as_dict(self) -> dict:
        """对外的可 JSON 序列化形态（管理台列表 / 写入响应共用）。"""
        return {
            "keep_credits": self.keep_credits,
            "daily_tokens": self.daily_tokens,
            "daily_credits": self.daily_credits,
            "per_model_tokens": dict(self.per_model_tokens),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


def _enabled(value: float | None) -> bool:
    """护栏是否开启：None / ≤0 表示关闭。"""
    return value is not None and value > 0


@dataclass(frozen=True)
class Usage:
    """某凭证自当日 0 点以来的用量（由 db 层聚合注入）。"""

    tokens: int = 0
    credits: float = 0.0
    per_model: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LimitState:
    """判定结果：账号级是否停用 + 哪些模型单独停用。"""

    blocked: bool = False
    blocked_models: frozenset[str] = frozenset()


def evaluate(limits: CredentialLimits | None, usage: Usage, *,
             quota_remaining: float | None) -> LimitState:
    """按限额判定是否应停用某凭证（以及哪些模型停用）。

    - 余额护栏优先：剩余积分已知且 ≤ `keep_credits` → 整号停用（与当日用量
      无关，这是「留底」语义；余额未知 / 渠道不上报时不猜，不因此停用）
    - 当日 token / credit 达到或超过上限 → 整号停用（`>=`：正好用满即停）
    - 某模型当日 token 达到上限 → 只停该模型
    """
    if limits is None:
        return LimitState()
    blocked_models = frozenset(
        model for model, cap in limits.per_model_tokens.items()
        if _enabled(cap) and usage.per_model.get(model, 0) >= cap)
    blocked = (
        (_enabled(limits.keep_credits) and quota_remaining is not None
         and quota_remaining <= limits.keep_credits)
        or (_enabled(limits.daily_tokens) and usage.tokens >= limits.daily_tokens)
        or (_enabled(limits.daily_credits) and usage.credits >= limits.daily_credits)
    )
    return LimitState(blocked=blocked, blocked_models=blocked_models)


# 快照 TTL：限额配置与当日用量都不需要秒级新鲜度，而每次选号查一次库（尤其
# 是按凭证聚合当日用量）会抵消选号的开销优势。管理员改完限额由 PUT 端点主动
# `invalidate()` 立即生效，所以 TTL 只兜底「别处写库」的场景。
SNAPSHOT_TTL_SECONDS = 15.0

# 当日用量聚合的复用时间桶（秒）。`_usage(since)` 是整日 `usage_events` GROUP BY，
# 行数上去后在事件循环上是一段可观的中断；按取整时间桶复用能把「同一桶内的重复
# 刷新」（TTL 刚过期时的并发选号、管理台改完限额后 invalidate 触发的第一批请求）
# 合并成一次查询，桶内命中不再查库。
#
# 刻意**不**改成 `asyncio.to_thread`：`blocked()` 是同步签名（选号热路径与既有
# 调用方都按同步使用），要异步化就得改整条调用链；而引入线程池又让快照缓存
# 变成跨线程共享，必须加锁才能避免读到半更新的 (limits, usage) 对，代价远大于
# 一次 15 秒一次的整日聚合。因此这里只限制查询频率并如实记录代价。
USAGE_BUCKET_SECONDS = 5


class LimitGuard:
    """选号前的限额过滤：进程内快照（限额表 + 当日用量）缓存。

    `limits_source.map()` 给出 `{credential_id: CredentialLimits}`，
    `usage_source(since)` 给出 `{credential_id: Usage}`。两者都可注入假实现，
    判定本身不依赖数据库。

    代价（业务方需知情）：用量聚合仍是**同步**查询，跑在调用方的线程/事件循环
    上——最坏情况每 `ttl` 秒（默认 15 秒）阻塞一次，单次耗时随 `usage_events`
    行数线性增长。`USAGE_BUCKET_SECONDS` 只把桶内重复刷新合并掉，不能消除这次
    阻塞；彻底消除需要异步化 `blocked()`，见上面常量处的取舍说明。
    """

    def __init__(self, limits_source, usage_source,
                 *, ttl: float = SNAPSHOT_TTL_SECONDS,
                 now_fn: Callable[[], float] = time.time) -> None:
        self._limits = limits_source
        self._usage = usage_source
        self._ttl = ttl
        self._now = now_fn
        # (载入时刻, 限额表, 当日用量)；用量只在存在限额时才查（省一次聚合）。
        self._snapshot: tuple[float, dict[str, CredentialLimits],
                              dict[str, Usage]] | None = None
        # (时间桶序号, 当日用量)：桶内重复刷新复用同一份聚合结果。
        self._usage_cache: tuple[int, dict[str, Usage]] | None = None

    def invalidate(self) -> None:
        """丢弃快照：管理台改完限额后调用，使下一次选号立刻用上新值。

        只丢限额快照不动用量缓存：同一时间桶内用量本来就只差几秒，重查一次
        整日聚合换不到有用精度（真需要立刻反映新用量时等下一个桶即可）。
        """
        self._snapshot = None

    def _load(self) -> tuple[dict[str, CredentialLimits], dict[str, Usage]]:
        now = self._now()
        snapshot = self._snapshot
        # 仅在缓存过期时才查：TTL 内连限额表也不重复读。
        if snapshot is not None and now - snapshot[0] < self._ttl:
            return snapshot[1], snapshot[2]
        limits_map = self._limits.map()
        usage_map = self._usage_map(limits_map, int(now))
        self._snapshot = (now, limits_map, usage_map)
        return limits_map, usage_map

    def _usage_map(self, limits_map: dict[str, CredentialLimits],
                   now: int) -> dict[str, Usage]:
        """取当日用量：无任何限额配置时不查；同一时间桶内复用上次结果。"""
        if not limits_map:
            return {}
        bucket = now // USAGE_BUCKET_SECONDS
        cached = self._usage_cache
        if cached is not None and cached[0] == bucket:
            return cached[1]
        usage_map = self._usage(day_start(now))
        self._usage_cache = (bucket, usage_map)
        return usage_map

    def blocked(self, candidate, model: str) -> bool:
        """该候选在请求 `model` 时是否因限额应被跳过。"""
        limits_map, usage_map = self._load()
        limits = limits_map.get(candidate.credential_id)
        if limits is None:
            return False
        state = evaluate(limits, usage_map.get(candidate.credential_id, Usage()),
                         quota_remaining=candidate.quota_remaining)
        return state.blocked or model in state.blocked_models
