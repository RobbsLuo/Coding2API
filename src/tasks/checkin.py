"""每日签到：按「endpoint + X-User-Id」隔离，同账号多凭证共享一次。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..db.repo import CredentialRepository
from . import TaskReport
from .pacer import Pacer

logger = logging.getLogger(__name__)


class CheckinTask:
    """每日签到：按「endpoint + X-User-Id」隔离，同账号多凭证共享一次。"""

    def __init__(self, credentials: CredentialRepository, providers: dict,
                 *, on_success: Callable[[str], None] | None = None,
                 pacer: Pacer | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self._on_success = on_success
        self._pacer = pacer
        # done_key → 封账截止的 epoch 秒。原先是 set（只有「当天封/不封」），
        # 现在带截止时刻：渠道的签到周期不一定等于本地自然日（Qoder 每日 10:00
        # 才换活动），封到自然日结束会把新周期整段封掉。
        self._done_scopes: dict[str, int] = {}

    def due(self, *, now: time.struct_time | None = None) -> bool:
        """全天每 10 分钟一轮（由 runner 周期控制），不再受时刻限制。"""
        return True

    def _day_key(self, now: time.struct_time) -> str:
        return f"{now.tm_year:04d}-{now.tm_mon:02d}-{now.tm_mday:02d}"

    def _done_key(self, day: str, scope: str) -> str:
        return f"{day}:{scope}"

    def _day_end(self, now: time.struct_time) -> int:
        """本地当日 24:00 的 epoch 秒（默认封账截止 = 当天结束）。"""
        return int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday + 1,
                                0, 0, 0, 0, 1, -1)))

    def _prune(self, now: time.struct_time) -> None:
        """丢掉非当天的封账记录：done_key 以日期开头，跨天后不可能再命中，
        长驻进程否则每天每账号各攒一条（无上限）。"""
        day = self._day_key(now)
        stale = [key for key in self._done_scopes if not key.startswith(f"{day}:")]
        for key in stale:
            del self._done_scopes[key]

    def _sealed(self, done_key: str, now: time.struct_time) -> bool:
        """该作用域是否已封账且封账未到期。"""
        until = self._done_scopes.get(done_key)
        return until is not None and until > time.mktime(now)

    async def run_once(self, *, now: time.struct_time | None = None) -> TaskReport:
        """签到一轮：已成功（当日封账）的作用域直接跳过；失败的留到下轮重试。"""
        current = now or time.localtime()
        day = self._day_key(current)
        report = TaskReport()
        seen: set[str] = set()
        self._prune(current)
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
            # 身份未知（provider 返回空 scope）时绝不共享：共享会让第二个账号被
            # 永久跳过（空 scope 相同 → seen 去重命中），代价是漏签一个号且无任何
            # 报错。回落到 credential_id 最坏只是多签一次，上游签到幂等（返回 ALREADY）。
            scope = checkin_scope(data) or f"credential|{candidate.credential_id}"
            done_key = self._done_key(day, scope)
            if self._sealed(done_key, current):
                report.skipped += 1                      # 封账未到期，不再调上游
                continue
            if scope in seen:
                report.skipped += 1                      # 同一轮同上游账号只签一次
                continue
            seen.add(scope)
            report.attempted += 1
            # 签到一轮含 status+claim+回查三次上游调用，与 quota_probe/growth
            # 共用同一 Pacer，避免绕开全局频率风控对策
            if self._pacer is not None:
                await self._pacer.wait_turn()
            try:
                result = await provider.checkin(data)
            except Exception as error:  # noqa: BLE001
                logger.warning("checkin failed for %s: %s", candidate.credential_id, error)
                report.failed += 1
                continue
            if result.ok:
                report.succeeded += 1
                # 成功即封账该作用域：封账期内不再重试，失败凭证不受影响可继续
                # 重试。渠道给了 `seal_until` 就按它封（签到周期 ≠ 自然日，
                # 见 `CheckinResult.seal_until`），否则封到本地当日结束。
                self._done_scopes[done_key] = result.seal_until or self._day_end(current)
                if self._on_success is not None:
                    self._on_success(candidate.credential_id)
            else:
                report.failed += 1
                logger.warning("checkin failed for %s: %s",
                               candidate.credential_id, result.message or "未知原因")
        return report
