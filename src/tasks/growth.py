"""成长中心后台任务（仅 CodeBuddy 有该活动）。

与 CheckinTask 的差别（不是重复实现）：
- 签到是「当日一次、失败重试」；成长中心是「一轮多类领取，有收获才值得记」
- 隔离键沿用 credential_id + 上游 scope：同账号多凭证共享一轮，避免重复领取
- 不可逆动作（抽奖/兑换/开盲盒/补登卡）由配置总开关控制，逐轮读取，改配置即生效
"""

from __future__ import annotations

import logging

from ..config import live
from ..db.repo import CredentialRepository, GrowthRepository
from . import TaskReport
from .pacer import Pacer

logger = logging.getLogger(__name__)


class GrowthTask:
    """一轮成长中心：遍历有该能力的凭证，逐个跑一遍并落库。"""

    def __init__(self, credentials: CredentialRepository, providers: dict,
                 events: GrowthRepository, *, allow_irreversible: bool = True,
                 pacer: Pacer | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self._events = events
        # 不可逆动作开关可热更（B3.2）：每轮读当前值，改配置当轮生效
        self._allow_source = live(allow_irreversible)
        self._pacer = pacer

    @property
    def _allow_irreversible(self) -> bool:
        """生效的开关；每轮现读，改运行时配置当轮生效。"""
        return bool(self._allow_source())

    @property
    def allow_irreversible(self) -> bool:
        """不可逆动作开关的当前生效值（每轮读取，故改名保留公开入口）。"""
        return self._allow_irreversible

    async def run_once(self, *, trigger: str = "auto") -> TaskReport:
        report = TaskReport()
        seen: set[str] = set()
        for candidate in self._credentials.candidates():
            provider = self._providers.get(candidate.provider)
            growth = getattr(provider, "growth", None)
            if growth is None:
                report.skipped += 1                 # 该渠道没有成长中心（TRAE）
                continue
            if candidate.disabled:
                report.skipped += 1
                continue
            data = self._credentials.credential_data(candidate.credential_id)
            if data is None:
                report.skipped += 1
                continue
            # 同上游账号多凭证共享一轮：重复领会被上游拒绝，但要额外打一串请求
            checkin_scope = getattr(provider, "checkin_scope", None)
            scope = (checkin_scope(data) or candidate.credential_id) if checkin_scope else (
                candidate.credential_id)
            if scope in seen:
                report.skipped += 1
                continue
            seen.add(scope)
            report.attempted += 1
            if self._pacer is not None:
                await self._pacer.wait_turn()
            try:
                result = await growth(data, allow_irreversible=self.allow_irreversible)
            except Exception as error:  # noqa: BLE001 - 一个凭证失败不拖累其余
                logger.warning("growth failed for %s: %s", candidate.credential_id, error)
                report.failed += 1
                continue
            if result.ok and not result.session_dead:
                report.succeeded += 1
            else:
                report.failed += 1
            self._credentials.save_growth_result(candidate.credential_id, result.report)
            self._events.record(credential_id=candidate.credential_id, result=result,
                                trigger=trigger)
        return report

    def due(self) -> bool:
        """全天周期执行（节奏由 runner 的间隔控制）。"""
        return True
