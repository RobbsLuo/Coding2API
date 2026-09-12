"""周期额度探测：写回 credentials.quota_* 与 health。"""

from __future__ import annotations

import logging

from ..db.repo import CredentialRepository
from ..provider.base import Quota
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
            provider = self._providers.get(candidate.provider)
            if provider is None:
                report.skipped += 1
                continue
            data = self._credentials.credential_data(candidate.credential_id)
            if data is None:
                report.skipped += 1
                continue
            report.attempted += 1
            if apply_pacing and self._pacer is not None:
                await self._pacer.wait_turn()
            try:
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
