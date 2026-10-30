"""周期额度探测：写回 credentials.quota_* 与 health。"""

from __future__ import annotations

import logging

from ..db.crypto import CredentialDecryptError
from ..db.repo import CredentialRepository
from ..provider.base import Quota
from ..request_context import credential_scope
from . import TaskReport
from .pacer import Pacer

logger = logging.getLogger(__name__)


class QuotaProbeTask:
    """周期额度探测 → 写回 credentials.quota_* 与 health。"""

    def __init__(self, credentials: CredentialRepository, providers: dict,
                 pacer: Pacer | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self._pacer = pacer

    async def run_once(self, *, apply_pacing: bool = True) -> TaskReport:
        report = TaskReport()
        for candidate in self._credentials.candidates():
            # 硬禁用与用户软暂停（enabled=0）都跳过，口径与 refresh / checkin /
            # growth / activity 一致：这些号一定探测失败，只会白打上游并把
            # 每小时一条 WARNING 灌进日志（实测容器实例对一条 10-15 就过期的
            # 号探了 160 次）。探测价值在「活号还有多少额度」，死号没有。
            if candidate.disabled or not candidate.enabled:
                report.skipped += 1
                continue
            provider = self._providers.get(candidate.provider)
            if provider is None:
                report.skipped += 1
                continue
            # 解不开（APP_SECRET 换过 / 密文损坏）→ 跳过这条凭证：一条坏凭证
            # 不该让整轮额度探测失败（早先直接冒泡 → 整轮任务挂掉）。
            try:
                data = self._credentials.credential_data(candidate.credential_id)
            except CredentialDecryptError:
                logger.error("额度探测跳过 %s：凭证解密失败（APP_SECRET 是否被更换过？）",
                             candidate.credential_id)
                report.skipped += 1
                continue
            if data is None:
                report.skipped += 1
                continue
            report.attempted += 1
            if apply_pacing and self._pacer is not None:
                await self._pacer.wait_turn()
            try:
                # 出口 IP 粘性：探活与推理必须走同一出口（见 provider/proxy.py）
                with credential_scope(candidate.credential_id):
                    quota = await provider.probe_quota(data)
            except Exception as error:  # noqa: BLE001 - 探测失败不能中断整批
                logger.warning("quota probe failed for %s: %s", candidate.credential_id, error)
                # 探测失败 → health 置 NULL（unknown），绝不当作 0 分
                self._credentials.mark_probe_failed(candidate.credential_id)
                report.failed += 1
                continue
            self._apply(candidate.credential_id, quota)
            report.succeeded += 1
        return report

    def _apply(self, credential_id: str, quota: Quota) -> None:
        self._credentials.save_quota(credential_id, quota)
