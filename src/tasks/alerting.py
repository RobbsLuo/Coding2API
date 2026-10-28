"""运维告警后台任务：四类规则 + webhook 推送 + 站内记录。

语义（与用户确认的范围一致）：

1. **凭证池耗尽**：池里有凭证但 `ready` 少于阈值——服务「活着但用不了」，
   `/health` 探针看不出来，只有池级视角能发现。severity=critical。
2. **后台任务连续失败**：某任务连续失败达到阈值（成功一轮清零）。
   severity=warning。
3. **token 临近到期**：某凭证的 access token 剩余时间落在窗口内。severity=warning。
4. **上游错误率骤升**：统计窗内失败占比超过阈值且样本数足够。severity=warning。
5. **单 Key 用量暴涨**：统计窗内某把 Key 的 token 总量越过阈值（泄露/共用信号）。
6. **非工作时段请求**：工作时间区间之外的请求数达到最小样本。
7. **预算分层**：当月成本达到预算的 50/80/95/100% 档；只报最高档，100% 为 critical。

投递（用户选的「Webhook + 站内」）：
- **站内**：每条命中落 `alert_events`，管理台「站内告警记录」回看（重启不丢）。
- **Webhook**：配置 `ALERT_WEBHOOK_URL` 时 POST JSON；留空只留站内记录。
  多个地址用逗号分隔，全部成功才算 delivered。

为什么要静默窗：评估每 N 分钟一轮，而池耗尽 / token 到期这类状态会持续存在。
没有静默窗就会每轮落一行、每轮推一次，把记录和群聊都刷屏。同一条
`(rule, scope)` 在静默窗内只落库一次；窗口过后若仍命中，再报一次（提醒还在）。

为什么要迟滞（`for_ticks` / `clear_ticks`）：单轮采样可能只是毛刺（一次探测
超时、一轮统计窗口内的偶发 5xx）。**连续 N 轮命中**才认为真的出事、才告警；
**连续 M 轮不再命中**才认为恢复、才解除并推一条恢复通知。迟滞状态是**进程内**
的（与签到去重、静默窗同范式）：重启后需要重新累积命中轮数，不编造重启前的
连续计数。

恢复通知：确认恢复时给该 `(rule, scope)` 最近一条告警写上 `resolved_at`（同一
行，不新落一行——「告警 + 恢复」是一个事件的两端），并外推一条 webhook
（body 带 `status: "resolved"`）。未配置 webhook 时只写库。

签名：webhook 请求带 `X-Coding2API-Signature: sha256=<HMAC-SHA256(APP_SECRET, body)>`，
**恒开**（不设开关——收端要么校验要么忽略，服务端多一个开关只会多一种「以为
开了其实没开」的静默状态）。签名算在**实际发送的字节**上，收端可原样复算。

纯函数 `evaluate_alerts` 与任务 `AlertTask` 分离：规则判定不碰 IO，测试可直接
喂快照；任务的 IO（读池/统计/落库/webhook）全部可注入，便于用假实现覆盖分支。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..db.repo import AlertRepository, CredentialRepository
from ..runtime_settings import blocked_webhook_host
from ..stats.collector import StatsCollector
from .status import TASK_BY_KEY, TaskStatusStore

logger = logging.getLogger(__name__)

# webhook 单次投递超时（秒）。远端挂掉时不能拖住后台循环。
_WEBHOOK_TIMEOUT = 10.0

# 一条告警的所有 webhook 地址共享的总投递预算（秒）：并发下最坏等单次超时，
# 这一层兜住「地址极多 / 自建 client 路径累积超时」把后台循环拖死的情况。
_WEBHOOK_TOTAL_BUDGET = 15.0

# 外发 webhook 的 detail 里需要脱敏的字段：`nickname` 是用户自定义标签，
# 实践中常填邮箱 / 手机号，站内记录留原文、外发一律打码（同源问题）。
_PII_DETAIL_KEYS = ("nickname",)
_PII_MASK = "***"

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"

# 迟滞默认值：连续 1 轮命中即告警（等价于不做进入迟滞，保持向后兼容的默认），
# 连续 2 轮不命中才确认恢复（避免单轮抖动就把告警撤掉）。
DEFAULT_FOR_TICKS = 1
DEFAULT_CLEAR_TICKS = 2

# webhook 签名头：`sha256=<hex(HMAC-SHA256(APP_SECRET, body))>`。
SIGNATURE_HEADER = "X-Coding2API-Signature"


def sign_webhook_body(secret: str, body: bytes) -> str:
    """对**实际发送的字节**做 HMAC-SHA256，返回 `sha256=<hex>`。

    收端用同一 secret 复算即可验真。secret 为空（未配置 APP_SECRET，仅测试
    可能出现）时返回空串，调用方据此不加签名头。
    """
    if not secret:
        return ""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _redact_webhook_url(url: str) -> str:
    """webhook URL → 只留 scheme://host（Slack/飞书的 path 即密钥）。"""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return "<webhook>"
    return f"{parts.scheme}://{parts.netloc}"


def _redact_webhook_error(error: Exception) -> str:
    """httpx 异常文本里可能内嵌整条 URL（含密钥 path/query）：只留异常类名。

    httpx 的 `RequestError.__str__` 会带上请求 URL，落库后经 `GET /api/alerts`
    回显给管理台即等于把 webhook 密钥写进审计记录。
    """
    return f"{type(error).__name__}: webhook delivery failed"


def _public_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """外发 webhook 的 detail：把 PII 字段打码，站内记录不受影响。

    `nickname` 是用户自填标签，实践中常是邮箱/手机号。站内 `alert_events`
    属本机管理面，保留原文便于定位；一旦外发到第三方（Slack/飞书群）就该
    最小化，非空值一律替换为掩码，空值保持空串以免多出噪声字段。
    """
    public = dict(detail)
    for key in _PII_DETAIL_KEYS:
        if public.get(key):
            public[key] = _PII_MASK
    return public


def _parse_detail(raw: Any) -> dict[str, Any]:
    """`alert_events.detail` 文本 → dict；非法或非对象一律当空（防御历史脏行）。"""
    try:
        detail = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return detail if isinstance(detail, dict) else {}


def _alert_from_row(row: dict[str, Any]) -> Alert:
    """`alert_events` 一行 → `Alert`（重启后重建「告警中」集合用）。

    `pii` 没落库：从 detail 里按 `_PII_DETAIL_KEYS` 反推。丢了这一步，恢复
    通知的 `public_message()` 就没有可打码的片段，会把昵称原文推给 webhook。
    """
    detail = _parse_detail(row["detail"])
    pii = tuple(str(detail[key]) for key in _PII_DETAIL_KEYS if detail.get(key))
    return Alert(rule=row["rule"], severity=row["severity"], scope=row["scope"],
                 message=row["message"], detail=detail, pii=pii)


@dataclass(frozen=True)
class Alert:
    """一条待投递的告警（规则判定结果，尚未落库/推送）。"""

    rule: str
    severity: str
    scope: str            # 具体对象：pool / 任务 key / 凭证 id
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    # 文案里内嵌的 PII 片段（如昵称）：站内记录留原文，外发 webhook 前打码。
    pii: tuple[str, ...] = ()

    @property
    def dedup_key(self) -> str:
        return f"{self.rule}:{self.scope}"

    def public_message(self) -> str:
        """外发文案：把 `pii` 里的片段替换为掩码。"""
        message = self.message
        for secret in self.pii:
            if secret:
                message = message.replace(secret, _PII_MASK)
        return message


def _humanize(seconds: int) -> str:
    """剩余秒数 → 人类可读（小时/分钟），用于告警文案。"""
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} 小时"
    if seconds >= 60:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def month_start(now: int) -> int:
    """本地时区当月起点（epoch 秒）：预算按自然月结算，与账单页同口径。"""
    tm = time.localtime(now)
    return int(time.mktime((tm.tm_year, tm.tm_mon, 1, 0, 0, 0, 0, 0, -1)))


def parse_levels(raw: str) -> tuple[int, ...]:
    """`"50,80,95,100"` → `(50, 80, 95, 100)`；非法项丢弃、去重、升序、限 1..100。

    预算分层是运营参数，写错不该让告警任务崩：解析失败按空处理（= 该规则
    不生效），由设置页的校验兜住正常入口。100 以上不设层（超支仍算 100%）。
    """
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            level = int(part)
        except ValueError:
            continue
        if 1 <= level <= 100:
            values.add(level)
    return tuple(sorted(values))


def in_off_hours(hour: int, start: int, end: int) -> bool:
    """`hour` 是否落在「非工作时段」；`start == end` 表示该规则关闭。

    跨零点写法（如 22 → 7）合法：`hour >= start or hour < end`。区间是
    **半开** `[start, end)`，与项目里其他时间窗口径一致。
    """
    if start == end:
        return False
    if start < end:
        return not (start <= hour < end)
    return not (hour >= start or hour < end)


def evaluate_alerts(
    *,
    pool: dict[str, int],
    pool_ready_min: int,
    failing_tasks: list[tuple[str, str, int]],
    expiring: list[dict[str, Any]],
    error_counts: tuple[int, int],
    error_rate_threshold: float,
    error_rate_min_requests: int,
    key_usage: list[dict[str, Any]] | None = None,
    key_burst_tokens: int = 0,
    off_hours: bool = False,
    off_hours_min_requests: int = 0,
    budget_cny: float = 0.0,
    budget_spent: float = 0.0,
    budget_levels: tuple[int, ...] = (),
) -> list[Alert]:
    """按四类规则把信号快照判成告警列表（纯函数，不碰 IO）。

    各规则的开关统一用「阈值 ≤ 0 即关闭」表达，不另设布尔开关：少一个字段
    就少一处「开关开着但阈值没配」的静默状态。

    - `failing_tasks`：`(key, name, streak)`，调用方已按阈值过滤。
    - `expiring`：`[{id, nickname, token_expires_at, ...}]`，已按窗口过滤。
    - `error_counts`：`(requests, failed)`，窗口口径由调用方给定。
    - `key_usage`：`[{api_key_id, username, requests, tokens}]`，按 Key 聚合。
    - `off_hours`：调用方按本地时钟算好的「当前是否非工作时段」；纯函数不碰
      时钟，测试可以任意构造。
    - `budget_*`：当月成本预算与已花费（调用方按月起点聚合）。只报**已达成的
      最高层**——低层在高一层触发时自然不再命中，由迟滞状态机写成「已恢复」，
      避免 50/80/95/100 四条同时挂着。
    """
    alerts: list[Alert] = []

    total = pool.get("total", 0)
    ready = pool.get("ready", 0)
    if pool_ready_min > 0 and total > 0 and ready < pool_ready_min:
        alerts.append(Alert(
            rule="pool_empty", severity=SEVERITY_CRITICAL, scope="pool",
            message=(f"凭证池可用数为 {ready}（少于阈值 {pool_ready_min}）："
                     f"共 {total} 个凭证，冷却 {pool.get('cooling', 0)}、"
                     f"暂停 {pool.get('paused', 0)}、禁用 {pool.get('disabled', 0)}"),
            detail={"total": total, "ready": ready, "cooling": pool.get("cooling", 0),
                    "paused": pool.get("paused", 0), "disabled": pool.get("disabled", 0),
                    "threshold": pool_ready_min},
        ))

    for key, name, streak in failing_tasks:
        alerts.append(Alert(
            rule="task_failed", severity=SEVERITY_WARNING, scope=key,
            message=f"后台任务「{name}」连续失败 {streak} 次",
            detail={"task": key, "name": name, "streak": streak},
        ))

    for row in expiring:
        expires_at = int(row["token_expires_at"])
        nickname = row.get("nickname") or ""
        label = nickname or row["id"]
        alerts.append(Alert(
            rule="token_expiring", severity=SEVERITY_WARNING, scope=row["id"],
            message=(f"凭证「{label}」的 token 将在 {_humanize(int(row['remaining']))} 后"
                     f"到期（{time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at))}）"),
            detail={"credential_id": row["id"], "provider": row.get("provider", ""),
                    "nickname": nickname, "token_expires_at": expires_at,
                    "remaining": int(row["remaining"])},
            # 文案里的昵称片段外发时打码（detail 由 _public_detail 处理）。
            pii=(nickname,) if nickname else (),
        ))

    requests, failed = error_counts
    if (error_rate_threshold > 0 and requests >= error_rate_min_requests
            and requests > 0):
        rate = failed / requests
        if rate >= error_rate_threshold:
            alerts.append(Alert(
                rule="error_rate", severity=SEVERITY_WARNING, scope="pool",
                message=(f"近窗口上游错误率 {rate * 100:.0f}%"
                         f"（{failed}/{requests} 失败，阈值 {error_rate_threshold * 100:.0f}%）"),
                detail={"requests": requests, "failed": failed,
                        "rate": round(rate, 4), "threshold": error_rate_threshold},
            ))

    # 单 Key 用量暴涨：窗口内总 token 越过阈值。**不是**错误，是滥用/泄露信号
    # （Key 被贴到公共仓库、多人共用一把 Key）。阈值 0 关闭。
    if key_burst_tokens > 0:
        for row in key_usage or ():
            tokens = int(row.get("tokens") or 0)
            if tokens < key_burst_tokens:
                continue
            alerts.append(Alert(
                rule="key_burst", severity=SEVERITY_WARNING,
                scope=row.get("api_key_id") or "unknown",
                message=(f"API Key 在统计窗内消耗 {tokens} token"
                         f"（{row.get('requests', 0)} 次请求，用户 "
                         f"{row.get('username') or '-'}，阈值 {key_burst_tokens}）"),
                detail={"api_key_id": row.get("api_key_id"),
                        "username": row.get("username"),
                        "tokens": tokens,
                        "requests": int(row.get("requests") or 0),
                        "threshold": key_burst_tokens},
            ))

    # 非工作时段仍有请求：可能是定时任务/自动化，也可能是 Key 被外部使用。
    # 只报「窗口内请求数达到最小样本」的次数，避免个别重试就报警。
    requests_total, _failed = error_counts
    if off_hours and off_hours_min_requests > 0 and requests_total >= off_hours_min_requests:
        alerts.append(Alert(
            rule="off_hours", severity=SEVERITY_WARNING, scope="pool",
            message=(f"非工作时段仍有 {requests_total} 次请求"
                     f"（阈值 {off_hours_min_requests}）"),
            detail={"requests": requests_total,
                    "threshold": off_hours_min_requests},
        ))

    # 预算分层：50/80/95/100%。只报当前达到的最高层；100%（含超支）为 critical。
    if budget_cny > 0 and budget_levels and budget_spent > 0:
        percent = budget_spent / budget_cny * 100
        reached = [level for level in budget_levels if percent >= level]
        if reached:
            level = max(reached)
            alerts.append(Alert(
                rule="budget",
                severity=SEVERITY_CRITICAL if level >= 100 else SEVERITY_WARNING,
                scope=f"budget_{level}",
                message=(f"当月成本已达预算的 {percent:.0f}%"
                         f"（¥{budget_spent:.2f} / ¥{budget_cny:.2f}，"
                         f"触发 {level}% 档）"),
                detail={"level": level, "percent": round(percent, 2),
                        "spent_cny": round(budget_spent, 4),
                        "budget_cny": budget_cny},
            ))

    return alerts


class AlertTask:
    """运维告警一轮：收集信号 → 判规则 → 迟滞确认 → 静默去重 → 落库 + 推送。

    全部依赖可注入（取值器用零参 callable，热更配置每轮现读）。`now` 与
    `client` 供测试注入固定时钟与 httpx.MockTransport。

    迟滞**计数**（连续命中/连续未命中）进程内维护：重启归零、重新累积，与
    签到去重同范式（不落库、不编造重启前的计数）。

    但「当前哪些告警仍未恢复」是**已落库的事实**（`resolved_at IS NULL`），
    不能随进程消失：构造时从库重建 `_active`。否则重启后规则早已不再命中的
    历史告警永远等不到 `resolve()`——恢复分支只遍历 `_active`——角标与
    「已恢复」标记会永久卡住（见 `AlertRepository.unresolved`）。
    """

    def __init__(
        self,
        credentials: CredentialRepository,
        alerts: AlertRepository,
        stats: StatsCollector,
        *,
        task_status: TaskStatusStore,
        enabled: Callable[[], bool] = lambda: True,
        webhook_url: Callable[[], str] = lambda: "",
        feishu_url: Callable[[], str] = lambda: "",
        slack_url: Callable[[], str] = lambda: "",
        pool_ready_min: Callable[[], int] = lambda: 1,
        task_failures: Callable[[], int] = lambda: 3,
        token_expiry_hours: Callable[[], int] = lambda: 24,
        error_rate_threshold: Callable[[], float] = lambda: 0.5,
        error_rate_min_requests: Callable[[], int] = lambda: 20,
        error_rate_window_minutes: Callable[[], int] = lambda: 15,
        key_burst_tokens: Callable[[], int] = lambda: 0,
        key_burst_window_minutes: Callable[[], int] = lambda: 15,
        off_hours_start: Callable[[], int] = lambda: 0,
        off_hours_end: Callable[[], int] = lambda: 0,
        off_hours_min_requests: Callable[[], int] = lambda: 0,
        budget_cny: Callable[[], float] = lambda: 0.0,
        budget_levels: Callable[[], str] = lambda: "50,80,95,100",
        silence_minutes: Callable[[], int] = lambda: 30,
        for_ticks: Callable[[], int] = lambda: DEFAULT_FOR_TICKS,
        clear_ticks: Callable[[], int] = lambda: DEFAULT_CLEAR_TICKS,
        app_secret: Callable[[], str] = lambda: "",
        client: httpx.AsyncClient | None = None,
        now: Callable[[], int] | None = None,
    ) -> None:
        self._credentials = credentials
        self._alerts = alerts
        self._stats = stats
        self._task_status = task_status
        self._enabled = enabled
        self._webhook_url = webhook_url
        self._feishu_url = feishu_url
        self._slack_url = slack_url
        self._pool_ready_min = pool_ready_min
        self._task_failures = task_failures
        self._token_expiry_hours = token_expiry_hours
        self._error_rate_threshold = error_rate_threshold
        self._error_rate_min_requests = error_rate_min_requests
        self._error_rate_window_minutes = error_rate_window_minutes
        self._key_burst_tokens = key_burst_tokens
        self._key_burst_window_minutes = key_burst_window_minutes
        self._off_hours_start = off_hours_start
        self._off_hours_end = off_hours_end
        self._off_hours_min_requests = off_hours_min_requests
        self._budget_cny = budget_cny
        self._budget_levels = budget_levels
        self._silence_minutes = silence_minutes
        self._for_ticks = for_ticks
        self._clear_ticks = clear_ticks
        self._app_secret = app_secret
        self._client = client
        self._now = now or (lambda: int(time.time()))
        # 迟滞状态机（进程内）：dedup_key → 连续命中轮数；
        # dedup_key → 连续未命中轮数（仅对「告警中」的 key 累积）；
        # dedup_key → 当前处于告警中的 Alert（含重建的历史行，供恢复通知取文案）。
        self._streak: dict[str, int] = {}
        self._clear_streak: dict[str, int] = {}
        # 从库重建「告警中」集合：同一 (rule, scope) 取最后一行为准（后写的
        # 提醒行文案更新），迟滞计数仍从 0 起——重启不编造连续计数。
        self._active: dict[str, Alert] = {
            row_alert.dedup_key: row_alert
            for row_alert in map(_alert_from_row, self._alerts.unresolved())
        }


    async def run_once(self) -> dict[str, Any] | None:
        """评估一轮；返回运行态摘要，关闭时返回 None（no-op，不覆盖上次结果）。"""
        if not self._enabled():
            return None
        now = int(self._now())
        threshold = self._task_failures()
        failing = [
            (key, TASK_BY_KEY[key].name if key in TASK_BY_KEY else key, streak)
            for key, streak in self._task_status.failing(threshold)
        ]
        window_minutes = max(0, self._error_rate_window_minutes())
        expiring_seconds = max(0, self._token_expiry_hours()) * 3600
        # 单 Key 暴涨用**自己的**窗口（与错误率窗口解耦：错误率看几分钟，
        # 用量暴涨看十几分钟到一小时更合理）。
        burst_minutes = max(0, self._key_burst_window_minutes())
        key_usage = (self._stats.window_key_usage(since=now - burst_minutes * 60)
                     if self._key_burst_tokens() > 0 and burst_minutes > 0 else [])
        start, end = self._off_hours_start(), self._off_hours_end()
        off_hours = in_off_hours(time.localtime(now).tm_hour, start, end)
        # 预算按自然月（本地）：月初成本归零 → 上月的预算告警自然恢复
        budget_cny = max(0.0, self._budget_cny())
        budget_spent = (self._stats.window_cost(since=month_start(now))[0]
                        if budget_cny > 0 else 0.0)
        alerts = evaluate_alerts(
            pool=self._credentials.pool_counts(now),
            pool_ready_min=self._pool_ready_min(),
            failing_tasks=failing,
            expiring=self._expiring(now, expiring_seconds),
            error_counts=self._stats.window_error_rate(
                since=now - window_minutes * 60),
            error_rate_threshold=self._error_rate_threshold(),
            error_rate_min_requests=self._error_rate_min_requests(),
            key_usage=key_usage,
            key_burst_tokens=self._key_burst_tokens(),
            off_hours=off_hours,
            off_hours_min_requests=self._off_hours_min_requests(),
            budget_cny=budget_cny,
            budget_spent=budget_spent,
            budget_levels=parse_levels(self._budget_levels()),
        )
        return await self._dispatch(alerts, now)

    def _expiring(self, now: int, within_seconds: int) -> list[dict[str, Any]]:
        """token 即将到期的凭证，补上 `remaining` 供文案与 detail 使用。"""
        if within_seconds <= 0:
            return []
        rows = self._credentials.expiring_tokens(within_seconds=within_seconds, now=now)
        return [{**row, "remaining": int(row["token_expires_at"]) - now} for row in rows]

    async def _dispatch(self, alerts: list[Alert], now: int) -> dict[str, Any]:
        """迟滞确认 → 静默去重 → 逐条落库 + 推送；返回本轮摘要。

        - **进入迟滞**：同一 `(rule, scope)` 连续命中 `for_ticks` 轮才真正告警，
          之前的轮次只累加计数（记 `pending`，不落库不推送）。
        - **静默窗**：已在告警中的条目在静默窗内不重复落库/推送。
        - **恢复迟滞**：告警中的条目连续 `clear_ticks` 轮未再命中才确认恢复，
          写 `resolved_at` 并外推一条 `status="resolved"` 通知。
        """
        silence = max(0, self._silence_minutes()) * 60
        for_ticks = max(1, self._for_ticks())
        clear_ticks = max(1, self._clear_ticks())
        fired = suppressed = pending = resolved = delivered = 0

        seen: set[str] = set()
        for alert in alerts:
            key = alert.dedup_key
            seen.add(key)
            self._clear_streak.pop(key, None)
            self._streak[key] = self._streak.get(key, 0) + 1
            if self._streak[key] < for_ticks:
                pending += 1
                continue
            if key in self._active:
                last = self._alerts.last_ts(alert.rule, alert.scope)
                if last is not None and silence > 0 and now - last < silence:
                    suppressed += 1
                    continue
            ok, error = await self._deliver(alert, now, status="firing")
            self._alerts.record(
                rule=alert.rule, severity=alert.severity, scope=alert.scope,
                message=alert.message,
                detail=json.dumps(alert.detail, ensure_ascii=False),
                delivered=ok, delivery_error=error, now=now,
            )
            self._active[key] = alert
            fired += 1
            if ok:
                delivered += 1

        # 告警中但本轮未命中 → 累积恢复轮数，确认后解除并外推恢复通知
        for key in list(self._active):
            if key in seen:
                continue
            self._streak.pop(key, None)
            self._clear_streak[key] = self._clear_streak.get(key, 0) + 1
            if self._clear_streak[key] < clear_ticks:
                continue
            alert = self._active.pop(key)
            self._clear_streak.pop(key, None)
            self._alerts.resolve(alert.rule, alert.scope, now)
            ok, _error = await self._deliver(alert, now, status="resolved")
            resolved += 1
            if ok:
                delivered += 1

        # 尚未确认告警、本轮又不再命中的 key：连击清零（下次重新累积）
        for key in list(self._streak):
            if key not in seen:
                self._streak.pop(key, None)

        return {"evaluated": len(alerts), "fired": fired, "suppressed": suppressed,
                "pending": pending, "resolved": resolved, "delivered": delivered}

    async def _deliver(self, alert: Alert, now: int, *,
                       status: str = "firing") -> tuple[bool, str | None]:
        """推送 webhook；未配置地址时视为「仅站内」，返回 (False, None)。

        `status` 为 `firing`（命中）或 `resolved`（恢复）：收端据此区分。

        投递失败只告警不抛错：一条 webhook 不能把后台任务判失败，否则
        「webhook 挂了」会被误报成「告警任务连续失败」，制造假信号。

        多个地址**并发**投递并套一层总预算：串行时 N 个地址最坏要等
        N × 单次超时，地址一多就能把后台循环拖到「连续失败」。并发下最坏
        只等单次超时；总预算再兜一层，防止 `_post` 的自建 client 路径
        （其 timeout 是每请求的）在地址很多时累积超时。
        """
        targets = self._targets(alert, status, now)
        if not targets:
            return False, None
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(
                    self._deliver_one(url, body,
                                      sign_webhook_body(self._app_secret(), body))
                    for url, body in targets
                )),
                timeout=_WEBHOOK_TOTAL_BUDGET,
            )
        except TimeoutError:
            logger.warning("告警 webhook 投递超过总预算 %.0f 秒，本轮放弃",
                           _WEBHOOK_TOTAL_BUDGET)
            return False, "TimeoutError: webhook delivery budget exceeded"
        errors = [error for error in results if error is not None]
        return (not errors), (errors[0] if errors else None)

    def _targets(self, alert: Alert, status: str, now: int) -> list[tuple[str, bytes]]:
        """本轮全部投递目标 → [(地址, 预序列化 body)]。

        通用 webhook 发完整 JSON（带签名，收端可复算）；飞书 / Slack 各自只认
        自己的格式，故按渠道单独构造。不同渠道 body 不同、签名逐 body 计算，
        签名仍覆盖实际发送的字节。未配置任何渠道时返回空列表（仅站内记录）。
        """
        resolved = status == "resolved"
        message = (f"已恢复：{alert.public_message()}" if resolved
                   else alert.public_message())
        targets: list[tuple[str, bytes]] = []
        for url in (item.strip() for item in self._webhook_url().split(",")):
            if not url:
                continue
            payload = {
                "service": "coding2api", "rule": alert.rule,
                "severity": alert.severity, "scope": alert.scope, "status": status,
                "message": message,
                "detail": _public_detail(alert.detail), "ts": now,
            }
            targets.append((url, json.dumps(payload, ensure_ascii=False).encode("utf-8")))
        feishu = self._feishu_url().strip()
        if feishu:
            # 飞书自定义机器人文本消息；不带 detail（可能含内部标识）
            targets.append((feishu, json.dumps(
                {"msg_type": "text", "content": {"text": f"Coding2API · {message}"}},
                ensure_ascii=False).encode("utf-8")))
        slack = self._slack_url().strip()
        if slack:
            targets.append((slack, json.dumps(
                {"text": f"Coding2API · {message}"}, ensure_ascii=False).encode("utf-8")))
        return targets

    async def _deliver_one(self, url: str, body: bytes, signature: str) -> str | None:
        """投递单个地址；失败只记日志并返回脱敏后的错误串，不抛出。

        webhook path/query 常即密钥（Slack/飞书），日志与落库都只留 host，
        绝不带上整条 URL 或 httpx 异常里内嵌的 URL。
        """
        if await asyncio.to_thread(blocked_webhook_host, url):
            # 写入期只能拦字面量 IP（域名当时解析不了），这里在**投递前**按
            # 实际解析结果复核一次，堵住「域名指向 127.0.0.1 / 169.254.169.254
            # / 内网 DNS 名」的 SSRF。解析失败按阻断处理（fail closed）。
            logger.warning("告警 webhook 解析到内网/保留地址，已跳过 %s",
                           _redact_webhook_url(url))
            return "BlockedWebhookHost: resolves to a private address"
        try:
            await self._post(url, body, signature)
        except Exception as exc:  # noqa: BLE001 - 单点失败不影响其余地址
            error = _redact_webhook_error(exc)
            logger.warning("告警 webhook 投递失败 %s: %s",
                           _redact_webhook_url(url), error)
            return error
        return None

    async def _post(self, url: str, body: bytes, signature: str) -> None:
        # 发送**预序列化字节**而不是 `json=`：签名必须覆盖实际发送的字节，
        # 收端才能原样复算验真（`json=` 会让 httpx 二次序列化，无法对账）。
        headers = {"Content-Type": "application/json"}
        if signature:
            headers[SIGNATURE_HEADER] = signature
        if self._client is not None:
            response = await self._client.post(url, content=body, headers=headers)
        else:
            # trust_env=False 与 provider/proxy.build_client 同口径：告警 JSON
            # 带池子健康详情，不能被宿主环境代理静默转手。
            async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT,
                                         trust_env=False) as client:
                response = await client.post(url, content=body, headers=headers)
        response.raise_for_status()
