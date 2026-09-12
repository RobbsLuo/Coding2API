"""每日签到：按「endpoint + X-User-Id」隔离，同账号多凭证共享一次。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..db.repo import CredentialRepository
from . import TaskReport

logger = logging.getLogger(__name__)


class CheckinTask:
    """每日签到：按「endpoint + X-User-Id」隔离，同账号多凭证共享一次。"""

    def __init__(self, credentials: CredentialRepository, providers: dict,
                 *, checkin_hour: int = 9, on_success: Callable[[str], None] | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self.checkin_hour = checkin_hour
        self._on_success = on_success
        self._done_scopes: set[str] = set()

    def due(self, *, now: time.struct_time | None = None) -> bool:
        """09:30 之后当日首次视为到期（服务器本地时区）。"""
        current = now or time.localtime()
        if current.tm_hour < self.checkin_hour:
            return False
        return self._day_key(current) not in self._done_scopes

    def _day_key(self, now: time.struct_time) -> str:
        return f"{now.tm_year:04d}-{now.tm_mon:02d}-{now.tm_mday:02d}"

    async def run_once(self, *, now: time.struct_time | None = None) -> TaskReport:
        current = now or time.localtime()
        report = TaskReport()
        seen: set[str] = set()
        for candidate in self._credentials.candidates():
            if candidate.disabled:
                report.skipped += 1
                continue
            provider = self._providers.get(candidate.provider)
            checkin_scope = getattr(provider, "checkin_scope", None)
            data = self._credentials.credential_data(candidate.credential_id)
            if provider is None or data is None or checkin_scope is None:
                report.skipped += 1
                continue
            scope = checkin_scope(data)
            if scope in seen:
                report.skipped += 1                      # 同上游账号只签一次
                continue
            seen.add(scope)
            report.attempted += 1
            try:
                result = await provider.checkin(data)
            except Exception as error:  # noqa: BLE001
                logger.warning("checkin failed for %s: %s", candidate.credential_id, error)
                report.failed += 1
                continue
            if result.ok:
                report.succeeded += 1
                if self._on_success is not None:
                    self._on_success(candidate.credential_id)
            else:
                report.failed += 1
                logger.warning("checkin failed for %s: %s",
                               candidate.credential_id, result.message or "未知原因")
        # 只有全部成功才当日封账；有失败时留着，下轮循环重试
        if report.failed == 0:
            self._done_scopes.add(self._day_key(current))
        return report
