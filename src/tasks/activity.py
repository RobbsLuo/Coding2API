"""活跃上报后台任务（B1.7，可选，默认关闭）。

用途：给 CodeBuddy 账号补发一条对话事件，续上成长中心的连登天数 / 活跃地图。
与积分、调度无关；上游改版即失效，**不作为可靠性功能**。

隔离与幂等：
- 按「endpoint + userId」隔离（activity_scope）；同账号多凭证共享一次
- 成功即当日封账（内存态，重启重建），失败下轮重试——与 CheckinTask 同范式
- 结果不落库（无对应表）：README/TECHNICAL 说明其只写日志

风险：官方条款禁止脚本篡改活动数据（处罚为取消资格并追回礼品）。默认关闭，
开启前自行评估；本模块不做任何加重风险的动作（每号每天一条、不重试写操作）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ..db.repo import CredentialRepository, GrowthRepository
from ..provider.base import GrowthResult, GrowthStep, StepStatus
from . import TaskReport

logger = logging.getLogger(__name__)

# 上游活动按北京时间分日；「每天一次」的封账日也应本地日界，而非 UTC
_CN_TZ = ZoneInfo("Asia/Shanghai")


class ActivityTask:
    """活跃上报一轮：遍历支持该能力的凭证，每个上游账号当日最多上报一次。"""

    def __init__(self, credentials: CredentialRepository, providers: dict, *,
                 events: GrowthRepository | None = None,
                 hour: int = 10, now: datetime | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self._events = events
        self._hour = hour
        self._now = now
        self._done_scopes: set[str] = set()

    def _current(self) -> datetime:
        if self._now is not None:
            return self._now
        return datetime.now(_CN_TZ)

    def due(self) -> bool:
        """时点到点执行：只在配置小时的那一小时窗口内跑（带容错窗）。"""
        current = self._current()
        return current.hour == self._hour

    def _day_key(self) -> str:
        return self._current().strftime("%Y-%m-%d")

    async def run_once(self, *, trigger: str = "auto") -> TaskReport:
        report = TaskReport()
        seen: set[str] = set()
        for candidate in self._credentials.candidates():
            if candidate.disabled:
                report.skipped += 1
                continue
            provider = self._providers.get(candidate.provider)
            data = self._credentials.credential_data(candidate.credential_id)
            activity_scope = getattr(provider, "activity_scope", None)
            if provider is None or data is None or activity_scope is None:
                report.skipped += 1
                continue
            scope = activity_scope(data) or f"credential|{candidate.credential_id}"
            if f"{self._day_key()}:{scope}" in self._done_scopes:
                report.skipped += 1                  # 当日已成功，不再调上游
                continue
            if scope in seen:
                report.skipped += 1                  # 同一轮同上游账号只报一次
                continue
            seen.add(scope)
            report.attempted += 1
            try:
                result = await provider.activity(data)
            except Exception as error:  # noqa: BLE001 - 单号失败不拖累其余
                logger.warning("activity report failed for %s: %s",
                               candidate.credential_id, error)
                report.failed += 1
                continue
            if result.ok:
                report.succeeded += 1
                self._done_scopes.add(f"{self._day_key()}:{scope}")
                self._record(candidate.credential_id, result, trigger)
            else:
                report.failed += 1
                logger.warning("activity report failed for %s: %s",
                               candidate.credential_id, result.message or "未知原因")
        return report

    def _record(self, credential_id: str, result, trigger: str) -> None:
        """有记录目标时落一行 growth_events（复用表，不新增 schema）。"""
        if self._events is None:
            return
        detail = result.message or "已上报一条对话事件"
        self._events.record(
            credential_id=credential_id, trigger=trigger,
            result=GrowthResult(
                report=f"活跃上报：{detail}",
                steps=[GrowthStep("活跃上报", StepStatus.DONE, detail)]))
