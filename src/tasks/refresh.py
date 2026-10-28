"""token 预刷新：只处理进入 REFRESH_SKEW_HOURS 窗口的凭证。

终态识别：一次性 refresh_token 被消费后，每轮重试用的都是同一张废票，
表现为「每小时刷一条一模一样的 warning」而凭证其实早就死了。上游明确说
票据作废时（`UpstreamReloginRequired`）改写 disabled_reason 并停手，把处置
交给人；其余失败照旧只记 warning，等下一轮。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..db.crypto import CredentialDecryptError
from ..db.repo import CredentialRepository
from ..provider.base import UpstreamReloginRequired
from ..request_context import credential_scope
from . import TaskReport
from .pacer import Pacer

logger = logging.getLogger(__name__)

# 落库的「需重新登录」原因：固定文案，绝不写上游原文（见 run_once 注释）。
RELOGIN_REASON = "刷新令牌已失效，需重新登录"


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
            # 硬禁用与用户软暂停（enabled=0）都跳过：暂停凭证不该被预刷新占用
            # 调度窗口，线上失败的也只会是早已停用的旧凭证（掩盖真实状态）。
            if candidate.disabled or not candidate.enabled:
                report.skipped += 1
                continue
            provider = self._providers.get(candidate.provider)
            # 解不开（APP_SECRET 换过 / 密文损坏）→ 跳过这条凭证：一条坏凭证
            # 不该让整轮预刷新失败（早先直接冒泡 → 整轮任务挂掉）。
            try:
                data = self._credentials.credential_data(candidate.credential_id)
            except CredentialDecryptError:
                logger.error("预刷新跳过 %s：凭证解密失败（APP_SECRET 是否被更换过？）",
                             candidate.credential_id)
                report.skipped += 1
                continue
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
            # 编辑锁：读 → await 上游 → 整份覆写必须与账号切换等写者互斥，
            # 否则两边都用自己读到的旧快照写回，后写者覆盖先写者。
            async with self._credentials.edit_lock(candidate.credential_id):
                # 持锁后重读：等锁期间可能已被别的写者改过（如刚切了账号），
                # 用旧 data 刷新会把那次改动覆盖掉。
                try:
                    data = self._credentials.credential_data(candidate.credential_id)
                except CredentialDecryptError:
                    logger.error("预刷新跳过 %s：凭证解密失败（APP_SECRET 是否被更换过？）",
                                 candidate.credential_id)
                    report.skipped += 1
                    continue
                if data is None:
                    report.skipped += 1
                    continue
                try:
                    with credential_scope(candidate.credential_id):
                        refreshed = await provider.refresh(data)
                except UpstreamReloginRequired as error:
                    # 续期凭据已作废：重试无意义，且重试只会继续污染凭证状态。
                    # 标记成需重新登录后本任务下轮直接跳过（candidates 的 disabled
                    # 分支），不再每小时刷一条同样的 warning。
                    logger.warning(
                        "refresh for %s needs re-login, disabling credential: %s",
                        candidate.credential_id, error)
                    # 落库文案固定，不写 `str(error)`：该值经 GET /api/credentials
                    # 回显管理台，固定文案可保证「将来有人往异常里塞上游原文」时
                    # 也不会把上游响应写进凭证状态。
                    self._credentials.mark_relogin_required(
                        candidate.credential_id, RELOGIN_REASON)
                    report.failed += 1
                    continue
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
