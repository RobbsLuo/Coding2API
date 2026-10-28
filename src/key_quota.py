"""Key 级配额：解析/校验（纯逻辑）+ 准入判定 + 进程内滑动窗口。

配额存在 `api_keys.quota`（JSON 文本，`''` = 不限制），六项互相独立：

- `daily_requests` / `daily_tokens` / `daily_cost_usd`：**当日**累计上限，口径
  与凭证限额护栏一致（北京时间自然日，见 `engine/limits.py::day_start`），
  用量从明细表 `usage_events` 按 `api_key_id` 聚合（B1 采集的那一列）。
- `rpm` / `tpm` / `max_concurrency`：**瞬时**速率上限，用进程内滑动窗口与
  在途计数判定——**不落库、重启清零**。单进程 SQLite 部署下这是精确的；
  多进程/多副本部署时每个进程各算各的，实际放行量会成倍于配置值（需要跨
  进程精确限速时应改读共享存储，本项目不做）。

0 或缺键 = 该项关闭。写入路径（`parse_key_quota`）严格校验；读取路径
（`load_key_quota`）宽容——一行写坏不能把出口锁死，退化为「不限制」。

为什么判定失败要放行而不是拒绝：配额是**旁路治理**，不是安全边界（安全边界
是鉴权与 IP 白名单，那边 fail closed）。配额表脏数据/聚合查询异常时若拒绝，
用户会看到与真实原因无关的 429 且无法自救；放行 + warning 才是可运维的选择。
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .engine.limits import day_start

logger = logging.getLogger(__name__)

# 整数项（拒绝小数）；其余为浮点项（成本是美元，需要小数）。
INT_FIELDS = ("daily_requests", "daily_tokens", "rpm", "tpm", "max_concurrency")
FLOAT_FIELDS = ("daily_cost_usd",)
QUOTA_FIELDS = INT_FIELDS + FLOAT_FIELDS

# 滑动窗口长度：RPM/TPM 都是「每分钟」，固定 60 秒（不做可配置——配置项越多
# 越容易与文档漂移，而这一项没有业务上的第二种合理取值）。
WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class KeyUsage:
    """一把 Key 在某个时间窗内的累计用量（由 db 层聚合注入）。"""

    requests: int = 0
    ok: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class KeyQuota:
    """一把 Key 的配额配置；每一项 ≤0 / 缺省 = 关闭。"""

    daily_requests: int = 0
    daily_tokens: int = 0
    daily_cost_usd: float = 0.0
    rpm: int = 0
    tpm: int = 0
    max_concurrency: int = 0

    def is_empty(self) -> bool:
        """六项全关：该 Key 不受配额约束（热路径据此跳过所有判定与查库）。"""
        return not any(getattr(self, name) for name in QUOTA_FIELDS)

    def as_dict(self) -> dict[str, float]:
        """对外的可 JSON 序列化形态（管理台回显 / `/v1/usage` 的 limits）。"""
        return {name: getattr(self, name) for name in QUOTA_FIELDS}


def _positive_number(name: str, value: object) -> float:
    """校验单个配额项：必须是有限的非负数字（bool 不算数字）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"quota.{name} 必须是数字")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"quota.{name} 必须是有限数字")
    if numeric < 0:
        raise ValueError(f"quota.{name} 不能为负数")
    return numeric


def parse_key_quota(raw: object) -> KeyQuota:
    """解析并**严格校验**配额配置；不合法抛 ValueError（消息可直接回给用户）。

    接受三种输入：空值（None/`''`/`` → 全关）、JSON 文本（DB 列）、dict
    （管理台提交）。未知键一律报错而不是静默忽略——写错的键名若被吞掉，
    用户会以为配额生效了。
    """
    if raw is None or raw == "" or raw == {}:
        return KeyQuota()
    data: object = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise ValueError("quota 必须是 JSON 对象") from error
    if not isinstance(data, dict):
        raise ValueError("quota 必须是 JSON 对象")
    unknown = sorted(str(key) for key in data if key not in QUOTA_FIELDS)
    if unknown:
        raise ValueError(f"quota 含未知配额项: {', '.join(unknown)}")
    values: dict[str, float] = {}
    for name in QUOTA_FIELDS:
        if name not in data:
            continue
        numeric = _positive_number(name, data[name])
        if name in INT_FIELDS:
            if numeric != int(numeric):
                raise ValueError(f"quota.{name} 必须是整数")
            values[name] = int(numeric)
        else:
            values[name] = numeric
    return KeyQuota(**values)


def load_key_quota(raw: object) -> KeyQuota:
    """读取路径的宽容解析：脏数据退化为「不限制」并记 warning。

    与 `ip_allowed` 的 fail closed 刻意相反：白名单是安全边界（读坏必须拒绝），
    配额是流量治理（读坏就放行，否则脏行会把 Key 永久锁死且用户无法自救）。
    """
    try:
        return parse_key_quota(raw)
    except ValueError as error:
        logger.warning("API Key 配额配置无法解析，本次按不限制处理: %s", error)
        return KeyQuota()


def quota_text(quota: KeyQuota) -> str:
    """落库形态：只存非零项，全关时存 `''`（列默认值 = 不受限）。"""
    data = {name: value for name, value in quota.as_dict().items() if value}
    return json.dumps(data, ensure_ascii=False, sort_keys=True) if data else ""


class QuotaExceeded(Exception):
    """本次请求超出 Key 配额；`retry_after` 供 429 的 `Retry-After` 头。"""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


@dataclass
class Lease:
    """一次准入登记；请求结束（含流式收尾）必须 `release()` 归还在途名额。"""

    guard: KeyQuotaGuard
    key_id: str
    counted: bool = True
    released: bool = False

    def release(self) -> None:
        """幂等释放：流式生成器的 finally 与异常路径可能都调到。"""
        if self.released:
            return
        self.released = True
        if self.counted:
            self.guard.release(self.key_id)


class KeyQuotaGuard:
    """准入判定：当日用量（注入的聚合源）+ 进程内滑动窗口/在途计数。

    `usage_source(key_id, *, since, until=None) -> KeyUsage` 由装配注入
    （生产是 `db.key_usage.KeyUsageRepository.for_key`）；缺省 None 表示
    「当日用量恒为 0」——只有当日类配额会因此失效，测试与纯内存场景够用。
    """

    def __init__(self, usage_source: Callable[..., KeyUsage] | None = None, *,
                 window_seconds: float = WINDOW_SECONDS,
                 now_fn: Callable[[], float] = time.time) -> None:
        self._usage = usage_source
        self._window = window_seconds
        self._now = now_fn
        # key_id → 窗口内已准入的 (时刻, 预估 token)。为什么把**在途**请求也
        # 计入：它已经占用了这一分钟的名额，等它结束再算等于允许瞬时超发。
        self._events: dict[str, deque[tuple[float, int]]] = {}
        # key_id → 在途请求数（并发上限用）。
        self._inflight: dict[str, int] = {}

    # ------------------------------------------------------------ 只读快照

    def window_usage(self, key_id: str) -> tuple[int, int]:
        """最近一个窗口内的 (请求数, token 估算和)；顺带清理过期事件。"""
        events = self._prune(key_id, self._now())
        return len(events), sum(tokens for _, tokens in events)

    def inflight(self, key_id: str) -> int:
        return self._inflight.get(key_id, 0)

    # ------------------------------------------------------------ 准入

    def admit(self, key_id: str, quota: KeyQuota, *,
              tokens: int = 0, subject: str = "key") -> Lease:
        """判定并登记一次请求；超限抛 `QuotaExceeded`，放行返回 `Lease`。

        `tokens` 是本次请求的**预估** token 数（输入估算 + 输出上限），因为
        真实用量要等响应结束才知道，而 TPM 必须在转发前判定。预估偏大时
        略保守、偏小时略宽松，都不影响「同一分钟内不超发」的语义。

        `subject` 只进 429 文案（`key` / `user`）：同一个 guard 同时服务
        Key 级配额与用户级配额（Playground），调用方能一眼看出超的是哪一层。

        判定过程中的任何异常（当日聚合查库失败、脏配置等）一律**放行**并记
        warning：配额是流量治理而非安全边界，判定挂了不能把出口锁死。
        """
        try:
            return self._admit(key_id, quota, tokens=max(0, tokens),
                               subject=subject)
        except QuotaExceeded:
            raise
        except Exception as error:  # noqa: BLE001 - 配额判定失败必须放行
            logger.warning("Key 配额判定失败，本次放行: %s", error)
            return Lease(self, key_id, counted=False)

    def _admit(self, key_id: str, quota: KeyQuota, *, tokens: int,
               subject: str) -> Lease:
        moment = self._now()
        if quota.is_empty():
            return Lease(self, key_id, counted=False)
        self._check_daily(key_id, quota, moment, subject)
        events = self._prune(key_id, moment)
        if quota.rpm and len(events) >= quota.rpm:
            raise QuotaExceeded(
                f"{subject} quota exceeded: {quota.rpm} requests per minute",
                _window_retry(events, moment, self._window))
        used_tokens = sum(count for _, count in events)
        if quota.tpm and used_tokens + tokens > quota.tpm:
            raise QuotaExceeded(
                f"{subject} quota exceeded: {quota.tpm} tokens per minute",
                _window_retry(events, moment, self._window))
        if quota.max_concurrency and self.inflight(key_id) >= quota.max_concurrency:
            # 并发名额随请求结束释放，正常几秒内就有空位；给 1 秒让客户端退避。
            raise QuotaExceeded(
                f"{subject} quota exceeded: {quota.max_concurrency} concurrent requests",
                1)
        self._events.setdefault(key_id, deque()).append((moment, tokens))
        self._inflight[key_id] = self.inflight(key_id) + 1
        return Lease(self, key_id)

    def release(self, key_id: str) -> None:
        count = self._inflight.get(key_id, 0)
        if count <= 1:
            self._inflight.pop(key_id, None)
        else:
            self._inflight[key_id] = count - 1

    # ------------------------------------------------------------ 内部

    def _check_daily(self, key_id: str, quota: KeyQuota, moment: float,
                     subject: str) -> None:
        """当日累计配额：查一次聚合（只有配了当日类限额才查）。"""
        if not (quota.daily_requests or quota.daily_tokens or quota.daily_cost_usd):
            return
        if self._usage is None:
            return
        usage = self._usage(key_id, since=day_start(int(moment)))
        retry = int(day_start(int(moment)) + 86400 - moment)
        if quota.daily_requests and usage.requests >= quota.daily_requests:
            raise QuotaExceeded(
                f"{subject} quota exceeded: {quota.daily_requests} requests per day", retry)
        if quota.daily_tokens and usage.tokens >= quota.daily_tokens:
            raise QuotaExceeded(
                f"{subject} quota exceeded: {quota.daily_tokens} tokens per day", retry)
        if quota.daily_cost_usd and usage.cost_usd >= quota.daily_cost_usd:
            raise QuotaExceeded(
                f"{subject} quota exceeded: ${quota.daily_cost_usd:g} per day", retry)

    def _prune(self, key_id: str, moment: float) -> deque[tuple[float, int]]:
        """丢弃窗口外的事件；窗口空了就把该 Key 的条目一起删掉（防无界增长）。"""
        events = self._events.get(key_id)
        if events is None:
            return deque()
        cutoff = moment - self._window
        while events and events[0][0] <= cutoff:
            events.popleft()
        if not events:
            self._events.pop(key_id, None)
        return events


def _window_retry(events: deque[tuple[float, int]], moment: float,
                  window: float) -> int:
    """等到最早一条事件滑出窗口的秒数（向上取整，至少 1）。"""
    if not events:
        return 1
    return max(1, math.ceil(events[0][0] + window - moment))
