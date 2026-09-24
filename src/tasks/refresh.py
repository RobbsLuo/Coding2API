"""token 预刷新：只处理进入 REFRESH_SKEW_HOURS 窗口的凭证。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..db.repo import CredentialRepository
from . import TaskReport
from .pacer import Pacer

logger = logging.getLogger(__name__)


class RefreshTask:
    """token 预刷新：只处理进入 refresh_skew 窗口的凭证。"""

    def __init__(self, credentials: CredentialRepository, providers: dict,
                 *, skew_seconds: int, now: Callable[[], int] | None = None,
                 pacer: Pacer | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self.skew_seconds = skew_seconds
        self._now = now or (lambda: int(time.time()))
        self._pacer = pacer

    async def run_once(self) -> TaskReport:
        report = TaskReport()
        current = self._now()
        for candidate in self._credentials.candidates():
            if candidate.disabled:
                report.skipped += 1
                continue
            provider = self._providers.get(candidate.provider)
            data = self._credentials.credential_data(candidate.credential_id)
            if provider is None or data is None:
                report.skipped += 1
                continue
            if not _needs_refresh(provider, data, self.skew_seconds, current):
                report.skipped += 1
                continue
            report.attempted += 1
            # 与 quota_probe/growth 共用同一 Pacer：ExchangeToken 同样是
            # 打上游的写请求，不过节流会绕开全局频率风控对策
            if self._pacer is not None:
                await self._pacer.wait_turn()
            try:
                refreshed = await provider.refresh(data)
            except Exception as error:  # noqa: BLE001
                logger.warning("refresh failed for %s: %s", candidate.credential_id, error)
                report.failed += 1
                continue
            # 先持久化轮换后的 token，再同步账号（AGENTS.md 顺序约束）
            self._credentials.save_credential_data(candidate.credential_id, refreshed)
            report.succeeded += 1
        return report


def _needs_refresh(provider, data: dict, skew_seconds: int, now: int) -> bool:
    builder = getattr(provider, "credential_from", None)
    if builder is None:
        return False
    credential = builder(data)
    needs = getattr(credential, "needs_refresh", None)
    return bool(needs(skew_seconds, now)) if callable(needs) else False
