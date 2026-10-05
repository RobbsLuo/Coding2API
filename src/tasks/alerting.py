"""运维告警后台任务（P1-7）：四类规则 + webhook 推送 + 站内记录。

语义（与用户确认的范围一致）：

1. **凭证池耗尽**：池里有凭证但 `ready` 少于阈值——服务「活着但用不了」，
   `/health` 探针看不出来，只有池级视角能发现。severity=critical。
2. **后台任务连续失败**：某任务连续失败达到阈值（成功一轮清零）。
   severity=warning。
3. **token 临近到期**：某凭证的 access token 剩余时间落在窗口内。severity=warning。
4. **上游错误率骤升**：统计窗内失败占比超过阈值且样本数足够。severity=warning。

投递（用户选的「Webhook + 站内」）：
- **站内**：每条命中落 `alert_events`，管理台「站内告警记录」回看（重启不丢）。
- **Webhook**：配置 `ALERT_WEBHOOK_URL` 时 POST JSON；留空只留站内记录。
  多个地址用逗号分隔，全部成功才算 delivered。

为什么要静默窗：评估每 N 分钟一轮，而池耗尽 / token 到期这类状态会持续存在。
没有静默窗就会每轮落一行、每轮推一次，把记录和群聊都刷屏。同一条
`(rule, scope)` 在静默窗内只落库一次；窗口过后若仍命中，再报一次（提醒还在）。

纯函数 `evaluate_alerts` 与任务 `AlertTask` 分离：规则判定不碰 IO，测试可直接
喂快照；任务的 IO（读池/统计/落库/webhook）全部可注入，便于用假实现覆盖分支。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..db.repo import AlertRepository, CredentialRepository
from ..stats.collector import StatsCollector
from .status import TASK_BY_KEY, TaskStatusStore

logger = logging.getLogger(__name__)

# webhook 单次投递超时（秒）。远端挂掉时不能拖住后台循环。
_WEBHOOK_TIMEOUT = 10.0

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Alert:
    """一条待投递的告警（规则判定结果，尚未落库/推送）。"""

    rule: str
    severity: str
    scope: str            # 具体对象：pool / 任务 key / 凭证 id
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        return f"{self.rule}:{self.scope}"


def _humanize(seconds: int) -> str:
    """剩余秒数 → 人类可读（小时/分钟），用于告警文案。"""
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} 小时"
    if seconds >= 60:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def evaluate_alerts(
    *,
    pool: dict[str, int],
    pool_ready_min: int,
    failing_tasks: list[tuple[str, str, int]],
    expiring: list[dict[str, Any]],
    error_counts: tuple[int, int],
    error_rate_threshold: float,
    error_rate_min_requests: int,
) -> list[Alert]:
    """按四类规则把信号快照判成告警列表（纯函数，不碰 IO）。

    各规则的开关统一用「阈值 ≤ 0 即关闭」表达，不另设布尔开关：少一个字段
    就少一处「开关开着但阈值没配」的静默状态。

    - `failing_tasks`：`(key, name, streak)`，调用方已按阈值过滤。
    - `expiring`：`[{id, nickname, token_expires_at, ...}]`，已按窗口过滤。
    - `error_counts`：`(requests, failed)`，窗口口径由调用方给定。
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
        label = row.get("nickname") or row["id"]
        alerts.append(Alert(
            rule="token_expiring", severity=SEVERITY_WARNING, scope=row["id"],
            message=(f"凭证「{label}」的 token 将在 {_humanize(int(row['remaining']))} 后"
                     f"到期（{time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at))}）"),
            detail={"credential_id": row["id"], "provider": row.get("provider", ""),
                    "nickname": row.get("nickname", ""), "token_expires_at": expires_at,
                    "remaining": int(row["remaining"])},
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

    return alerts


class AlertTask:
    """运维告警一轮：收集信号 → 判规则 → 静默去重 → 落库 + 推送。

    全部依赖可注入（取值器用零参 callable，热更配置每轮现读）。`now` 与
    `client` 供测试注入固定时钟与 httpx.MockTransport。
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
        pool_ready_min: Callable[[], int] = lambda: 1,
        task_failures: Callable[[], int] = lambda: 3,
        token_expiry_hours: Callable[[], int] = lambda: 24,
        error_rate_threshold: Callable[[], float] = lambda: 0.5,
        error_rate_min_requests: Callable[[], int] = lambda: 20,
        error_rate_window_minutes: Callable[[], int] = lambda: 15,
        silence_minutes: Callable[[], int] = lambda: 30,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], int] | None = None,
    ) -> None:
        self._credentials = credentials
        self._alerts = alerts
        self._stats = stats
        self._task_status = task_status
        self._enabled = enabled
        self._webhook_url = webhook_url
        self._pool_ready_min = pool_ready_min
        self._task_failures = task_failures
        self._token_expiry_hours = token_expiry_hours
        self._error_rate_threshold = error_rate_threshold
        self._error_rate_min_requests = error_rate_min_requests
        self._error_rate_window_minutes = error_rate_window_minutes
        self._silence_minutes = silence_minutes
        self._client = client
        self._now = now or (lambda: int(time.time()))

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
        alerts = evaluate_alerts(
            pool=self._credentials.pool_counts(now),
            pool_ready_min=self._pool_ready_min(),
            failing_tasks=failing,
            expiring=self._expiring(now, expiring_seconds),
            error_counts=self._stats.window_error_rate(
                since=now - window_minutes * 60),
            error_rate_threshold=self._error_rate_threshold(),
            error_rate_min_requests=self._error_rate_min_requests(),
        )
        return await self._dispatch(alerts, now)

    def _expiring(self, now: int, within_seconds: int) -> list[dict[str, Any]]:
        """token 即将到期的凭证，补上 `remaining` 供文案与 detail 使用。"""
        if within_seconds <= 0:
            return []
        rows = self._credentials.expiring_tokens(within_seconds=within_seconds, now=now)
        return [{**row, "remaining": int(row["token_expires_at"]) - now} for row in rows]

    async def _dispatch(self, alerts: list[Alert], now: int) -> dict[str, Any]:
        """静默去重后逐条落库 + 推送；返回本轮摘要。"""
        silence = max(0, self._silence_minutes()) * 60
        fired = suppressed = 0
        delivered = 0
        for alert in alerts:
            last = self._alerts.last_ts(alert.rule, alert.scope)
            if last is not None and silence > 0 and now - last < silence:
                suppressed += 1
                continue
            ok, error = await self._deliver(alert, now)
            self._alerts.record(
                rule=alert.rule, severity=alert.severity, scope=alert.scope,
                message=alert.message,
                detail=json.dumps(alert.detail, ensure_ascii=False),
                delivered=ok, delivery_error=error, now=now,
            )
            fired += 1
            if ok:
                delivered += 1
        return {"evaluated": len(alerts), "fired": fired,
                "suppressed": suppressed, "delivered": delivered}

    async def _deliver(self, alert: Alert, now: int) -> tuple[bool, str | None]:
        """推送 webhook；未配置地址时视为「仅站内」，返回 (False, None)。

        投递失败只告警不抛错：一条 webhook 不能把后台任务判失败，否则
        「webhook 挂了」会被误报成「告警任务连续失败」，制造假信号。
        """
        urls = [u.strip() for u in self._webhook_url().split(",") if u.strip()]
        if not urls:
            return False, None
        payload = {
            "service": "coding2api", "rule": alert.rule, "severity": alert.severity,
            "scope": alert.scope, "message": alert.message,
            "detail": alert.detail, "ts": now,
        }
        ok = True
        error: str | None = None
        for url in urls:
            try:
                await self._post(url, payload)
            except Exception as exc:  # noqa: BLE001 - 单点失败不影响其余地址
                ok = False
                if error is None:
                    error = str(exc)
                logger.warning("告警 webhook 投递失败 %s: %s", url, exc)
        return ok, error

    async def _post(self, url: str, payload: dict[str, Any]) -> None:
        if self._client is not None:
            response = await self._client.post(url, json=payload)
        else:
            async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT) as client:
                response = await client.post(url, json=payload)
        response.raise_for_status()
