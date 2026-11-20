"""每日签到：按「endpoint + X-User-Id」隔离，同账号多凭证共享一次。"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import live
from ..db.crypto import CredentialDecryptError
from ..db.repo import CredentialRepository
from ..error_text import error_text
from ..provider.base import CheckinResult, UpstreamReloginRequired
from ..request_context import credential_scope
from . import TaskReport
from .account_shape import is_enterprise_account
from .pacer import Pacer

logger = logging.getLogger(__name__)

# 签到日边界固定北京时间：上游（CodeBuddy / OfficeAce）的「每日一次」按北京时间
# 切，与本服务进程的 TZ 无关。此前用 `time.localtime()`，容器/宿主 TZ≠CN 时
# 日界会偏移数小时，出现「上游已重置但本服务认为今天已签」或反之的静默漏签。
# 与 `engine/limits.day_start`、`tasks/activity` 同一口径。
_CN_TZ = ZoneInfo("Asia/Shanghai")

#: 失败重试退避：首轮失败后 10 分钟再试，连续失败翻倍，封顶 1 小时。
#: 上游对「今日积分尚未发放」会一直回同一句（实测 2026-11-05 两个号从 00:29
#: 到 17:54 每 10 分钟锤一次 → 145 条 WARNING + 几千次无效上游调用）。
#: 退避后一天最多 ~7 次/账号，仍能在当天补签；`trigger="manual"` 不受限制。
_FAIL_BACKOFF_BASE = 10 * 60
_FAIL_BACKOFF_MAX = 60 * 60

#: `seal_until` 上界（48h）：签到周期可跨天（Qoder 10:00 → 次日 09:59），故不能
#: 钳到本地当日 24:00；上界只防上游给出超长值把该凭证长期封住、永久漏签。
_SEAL_UNTIL_MAX = 48 * 60 * 60

#: 落库的「需重新登录」原因：固定文案，绝不写上游原文（同 `tasks/refresh`）。
#: 该值经 `GET /api/credentials` 回显管理台。
RELOGIN_REASON = "自动登录被上游拒绝，需重新登录"


def _cn_now(now: int | None = None) -> time.struct_time:
    """当前时刻的北京时间 struct_time（测试可注入 epoch 秒）。

    统一入口：`due` 与 `run_once` 都用它，保证「该不该跑」和「算哪天」同源。
    """
    moment = datetime.fromtimestamp(int(now if now is not None else time.time()),
                                   _CN_TZ)
    return moment.timetuple()


def _cn_epoch(moment: time.struct_time) -> int:
    """北京时间 struct_time → epoch 秒。

    不能用 `time.mktime`：它按**进程本地时区**解释 struct_time，TZ≠CN 时
    算出来的封账截止会偏几小时（签到每天可能漏一次或签两次）。
    """
    return int(datetime(*moment[:6], tzinfo=_CN_TZ).timestamp())


def _parse_providers(text: str) -> frozenset[str]:
    """`raccoon, lingxi` → `{"raccoon", "lingxi"}`（小写、去空段）。

    宽容解析：写错一个渠道名不该让整轮签到停摆（`skip_providers` 本身是
    止损手段，配置写错最多等于没配）。
    """
    return frozenset(part.strip().lower() for part in text.split(",") if part.strip())


def parse_checkin_times(text: str) -> tuple[int, ...]:
    """`HH:MM,HH:MM` → 距 0 点的分钟数（升序去重）。坏段忽略 + warning。

    解析**宽容**（与 `engine.reasoning.parse_effort_map` 同风格）：一行坏配置
    不能让网关起不来，也不能因一个笔误丢掉别的合法时点。空段 / 缺冒号 /
    非数字 / 越界（`25:00`、`09:99`）都只丢该段。留空返回空元组 =
    「不限时点」（旧的全天轮询行为）。
    """
    minutes: set[int] = set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        hour_raw, sep, minute_raw = chunk.partition(":")
        if not sep:
            logger.warning("签到时点 %r 缺少冒号，已忽略", chunk)
            continue
        try:
            hour, minute = int(hour_raw.strip()), int(minute_raw.strip())
        except ValueError:
            logger.warning("签到时点 %r 不是 HH:MM 数字，已忽略", chunk)
            continue
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            logger.warning("签到时点 %r 超出 00:00-23:59，已忽略", chunk)
            continue
        minutes.add(hour * 60 + minute)
    return tuple(sorted(minutes))


class CheckinTask:
    """每日签到：按「endpoint + X-User-Id」隔离，同账号多凭证共享一次。"""

    def __init__(self, credentials: CredentialRepository, providers: dict,
                 *, on_success: Callable[[str], None] | None = None,
                 events=None,
                 pacer: Pacer | None = None,
                 times: str | Callable[[], str] = "",
                 jitter_minutes: int | Callable[[], int] = 0,
                 skip_providers: str | Callable[[], str] = "",
                 auto_login: bool | Callable[[], bool] = False,
                 rng: Callable[[int], int] | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self._on_success = on_success
        # 签到审计日志仓储（CheckinEventRepository）：None 时（老调用方/测试）
        # 不落库，只跑签到本身——生产路径总是传入。
        self._events = events
        self._pacer = pacer
        # 时点与抖动可热更：存取值器，due() 每次现读（不是装配快照）。
        self._times = live(times)
        self._jitter = live(jitter_minutes)
        # 跳过签到的渠道：每轮 run_once 现读。上游长期停发（raccoon 2026-11-05
        # 起 daily_grant 断供）时用它止损，不影响聊天与其它渠道。
        self._skip_providers = live(skip_providers)
        # 按需登录（默认关，热更）：当日签到失败且凭证带 phone/password 时，
        # 调 `provider.relogin` 换一份新会话再核一次。上游只在「新登录」时补发
        # 当天 300（见 provider/raccoon/activity.py），refresh 换不出新会话。
        self._auto_login = live(auto_login)
        # done_key → 已尝试登录的日期。每天每凭证最多一次：密码错了再试只会
        # 累积风控/锁号风险，而成功一次就已经补发了当天这笔。
        self._login_days: dict[str, str] = {}
        # 抖动偏移的随机源：`rng(上限)` 返回 0..上限。可注入，测试不依赖真随机。
        self._rng = rng or (lambda span: random.randint(0, span))
        # done_key → 封账截止的 epoch 秒。原先是 set（只有「当天封/不封」），
        # 现在带截止时刻：渠道的签到周期不一定等于本地自然日（Qoder 每日 10:00
        # 才换活动），封到自然日结束会把新周期整段封掉。
        # 注意与下面的「时点放行」是两套键：这里是每个凭证/G 作用域的封账，
        # 那边是「今天这个时点已经触发过一轮」，语义不能合并。
        self._done_scopes: dict[str, int] = {}
        # done_key → (连续失败次数, 下次允许重试的 epoch 秒)。失败退避，成功清零；
        # 与 `_done_scopes` 同键同生命周期，跨天 prune 时一起清。
        self._failures: dict[str, tuple[int, int]] = {}
        # 时点放行：键固定为「本地日期 + 时点分钟数」，抖动只改窗口、不改归属，
        # 所以执行时刻落在窗口里哪个分钟都不会让同一时点跑两次。
        self._slot_offsets: dict[str, int] = {}   # 当日该时点抽到的抖动偏移（分钟）
        self._fired_slots: set[str] = set()       # 当日已放行的时点

    def has_times(self) -> bool:
        """是否配置了签到时点（决定 runner 的醒转周期：配了就要 1 分钟精度）。"""
        return bool(parse_checkin_times(self._times()))

    def due(self, *, now: time.struct_time | int | None = None) -> bool:
        """是否该跑一轮签到。

        - `checkin_times` 留空 → 恒 True，保持「全天每 10 分钟一轮」的旧行为
          （老部署升级后不打任何补丁也不会改变签到节奏）。
        - 有配置 → 只在某个时点的抖动窗口 `[时点 - 偏移, 时点]` 内返回 True。
          偏移每个「日期 + 时点」只抽一次（0..`checkin_jitter_minutes`），
          抖动=0 时窗口退化成「恰好该分钟」；跨零点时截到当日 0 点。
        - 放行一次即记 `日期 + 时点` 键（不是实际执行时刻），同一时点当天
          不再放行；跨天记录由 `_prune_slots` 清掉。

        注意窗口宽度与 runner 的醒转周期：配了时点时 runner 改为 **每 60 秒**
        醒一次（见 `TaskRunner._checkin_interval`），故抖动=0 也能稳定命中那
        一分钟；留空时仍是 10 分钟一轮的旧节奏。抖动继续用于把多部署摊开。
        """
        times = parse_checkin_times(self._times())
        if not times:
            return True                      # 留空 = 旧行为
        current = now if isinstance(now, time.struct_time) else _cn_now(now)
        day = self._day_key(current)
        self._prune_slots(day)
        moment = current.tm_hour * 60 + current.tm_min
        jitter = max(0, int(self._jitter()))
        for slot in times:
            key = f"{day}:{slot}"
            if key in self._fired_slots:
                continue
            offset = self._slot_offsets.get(key)
            if offset is None:               # 当日首次见到该时点 → 抽一次偏移
                # 抖动最多提前到当日 0 点：偏移量不能超过时点本身，否则窗口会
                # 跨到前一天，而键是「今天 + 时点」，语义就乱了。
                span = min(jitter, slot)
                offset = min(max(0, int(self._rng(span))), span) if span else 0
                self._slot_offsets[key] = offset
            if slot - offset <= moment <= slot:
                self._fired_slots.add(key)
                return True
        return False

    def _prune_slots(self, day: str) -> None:
        """丢掉非当天的抖动计划 / 放行记录：键以日期开头，跨天后不可能再命中，
        长驻进程否则每天每时点各攒两条（无上限）。"""
        prefix = f"{day}:"
        for key in [key for key in self._slot_offsets if not key.startswith(prefix)]:
            del self._slot_offsets[key]
        self._fired_slots = {key for key in self._fired_slots
                             if key.startswith(prefix)}

    def _day_key(self, now: time.struct_time) -> str:
        return f"{now.tm_year:04d}-{now.tm_mon:02d}-{now.tm_mday:02d}"

    def _done_key(self, day: str, scope: str) -> str:
        return f"{day}:{scope}"

    def _day_end(self, now: time.struct_time) -> int:
        """北京时间当日 24:00 的 epoch 秒（默认封账截止 = 当天结束）。

        用 `datetime + timedelta` 而不是「tm_mday + 1」：后者在月末（31 号）
        构造出 32 号，`datetime` 不做溢出归一（`time.mktime` 才会），直接抛
        `ValueError: day is out of range for month`——每月最后一天签到全挂。
        """
        midnight = datetime(now.tm_year, now.tm_mon, now.tm_mday, tzinfo=_CN_TZ)
        return int((midnight + timedelta(days=1)).timestamp())

    def _seal_deadline(self, result: Any, now: time.struct_time) -> int:
        """成功封账截止时刻：渠道给了 `seal_until` 就用它，但不超过 48h 上界。

        默认封到本地当日 24:00；跨天周期（Qoder 10:00 换窗口）按渠道给的值封。
        上游若回一个远超现实的值，会让该作用域长期不再签到 → 只钳上界。
        """
        default = self._day_end(now)
        until = result.seal_until
        if not until:
            return default
        return min(int(until), _cn_epoch(now) + _SEAL_UNTIL_MAX)

    def _prune(self, now: time.struct_time) -> None:
        """丢掉非当天的封账记录：done_key 以日期开头，跨天后不可能再命中，
        长驻进程否则每天每账号各攒一条（无上限）。"""
        day = self._day_key(now)
        prefix = f"{day}:"
        stale = [key for key in self._done_scopes if not key.startswith(prefix)]
        for key in stale:
            del self._done_scopes[key]
        for key in [key for key in self._failures if not key.startswith(prefix)]:
            del self._failures[key]
        # 按需登录的「今天试过了」记录随之清空，跨天恢复可登录
        for key in [key for key in self._login_days if not key.startswith(prefix)]:
            del self._login_days[key]

    def _sealed(self, done_key: str, now: time.struct_time) -> bool:
        """该作用域是否已封账且封账未到期。"""
        until = self._done_scopes.get(done_key)
        return until is not None and until > _cn_epoch(now)

    def _backed_off(self, done_key: str, now: time.struct_time,
                    *, manual: bool) -> bool:
        """失败退避未到期 → 本轮跳过；管理台「立即运行」不受退避限制。"""
        if manual:
            return False
        state = self._failures.get(done_key)
        return state is not None and state[1] > _cn_epoch(now)

    def _note_failure(self, done_key: str, now: time.struct_time,
                      *, retry_at: int | None = None) -> None:
        """记一次失败并按连续失败次数翻倍退避（10m → 20m → 40m → 封顶 1h）。

        `retry_at` 是渠道给出的「最早再试时刻」（如小浣熊每日积分账单要等到
        01:30 才同步稳定）。与本地退避取较晚者：既保留指数退避，也绝不在
        渠道点名的时间之前空锤。
        """
        streak = self._failures.get(done_key, (0, 0))[0] + 1
        delay = min(_FAIL_BACKOFF_BASE * 2 ** min(streak - 1, 6), _FAIL_BACKOFF_MAX)
        until = _cn_epoch(now) + delay
        if retry_at is not None:
            until = max(until, retry_at)
        self._failures[done_key] = (streak, until)

    def _clear_failure(self, done_key: str) -> None:
        """成功即清零：下次再失败从 10 分钟重新起算，不继承旧的退避档位。"""
        self._failures.pop(done_key, None)

    def _can_autologin(self, provider: Any, data: dict, done_key: str,
                       day: str) -> bool:
        """该凭证这一轮能不能试一次按需登录。

        四个前提缺一不可：开关打开、渠道提供 `relogin`、凭证带手机号+密码、
        今天还没试过。**每天一次**是硬约束——密码错了再试只会累积风控甚至
        锁号，而一旦成功当天这笔就已补发，没有第二次的必要。
        """
        if not self._auto_login():
            return False
        if getattr(provider, "relogin", None) is None:
            return False
        if not data.get("phone") or not data.get("password"):
            return False
        return self._login_days.get(done_key) != day

    async def _try_autologin(self, provider: Any, candidate: Any, data: dict,
                             result: Any, done_key: str,
                             day: str) -> tuple[Any, dict]:
        """签到失败后按需登录一次并重核；返回 (结论, 最新凭证数据)。

        三种结局都要落在签到审计里，别让「登录失败」看起来像「签到没发」：

        * 登录成功 → 落库新会话（`save_credential_data`，与预刷新同一写口）后
          重新 `checkin` 一次，用第二次的结论；
        * 上游明确拒绝（账号密码错 / 格式不符）→ 标「需重新登录」并停手，
          不再每小时拿同一份错密码去撞；
        * 其他异常（网络/5xx）→ 保留原结论，按既有退避下轮再试（**不改凭证**）。
        """
        # 记账必须在 `wait_turn()` **之后**：Pacer 被取消/超时会抛 BaseException，
        # 提前记账会让重启后当天不再按需登录（静默漏签）。记账点后移到真正发起
        # 登录之前，保留「失败也不许重试」的原意。
        if self._pacer is not None:
            await self._pacer.wait_turn()
        self._login_days[done_key] = day            # 失败也不许重试
        with credential_scope(candidate.credential_id):
            try:
                refreshed = await provider.relogin(data)
            except UpstreamReloginRequired as error:
                # 上游原文只进日志；落库用固定文案（经 /api/credentials 回显管理台）
                logger.warning("autologin rejected for %s: %s",
                               candidate.credential_id, error_text(error))
                self._credentials.mark_relogin_required(candidate.credential_id,
                                                        RELOGIN_REASON)
                return CheckinResult(ok=False, message=RELOGIN_REASON), data
            except Exception as error:  # noqa: BLE001 - 登录故障不该打断整轮
                logger.warning("autologin failed for %s: %s",
                               candidate.credential_id, error_text(error))
                return result, data
        if refreshed is None:                       # 没带凭据（不该发生，兜底）
            return result, data
        # 与预刷新/账号切换共用同一把编辑锁：整份覆写 data_enc，不能与别的
        # 写者交错（否则后写者会用自己读到的旧快照覆盖新 token）。
        async with self._credentials.edit_lock(candidate.credential_id):
            self._credentials.save_credential_data(candidate.credential_id, refreshed)
        logger.info("autologin refreshed %s", candidate.credential_id)
        with credential_scope(candidate.credential_id):
            return await provider.checkin(refreshed), refreshed

    async def run_once(self, *, now: time.struct_time | int | None = None,
                       trigger: str = "auto") -> TaskReport:
        """签到一轮：已成功（当日封账）的作用域直接跳过；失败的留到下轮重试。

        失败按连续次数做指数退避（10m → 20m → 40m → 封顶 1h），避免上游
        一直回「积分尚未发放」时每 10 分钟空锤一次；`trigger="manual"`
        （管理台立即运行）绕过退避。失败不封账，成功才封账。

        `trigger` 只进审计日志（auto=定时 / manual=管理台「立即运行」），
        除绕过失败退避外不参与任何判定。
        """
        current = now if isinstance(now, time.struct_time) else _cn_now(now)
        day = self._day_key(current)
        report = TaskReport()
        seen: set[str] = set()
        manual = trigger == "manual"
        skipped_providers = _parse_providers(self._skip_providers())
        self._prune(current)
        for candidate in self._credentials.candidates():
            # `enabled=0`（用户软暂停）同样跳过，口径与调度器 is_selectable 一致。
            # 必须早于 seen.add：否则暂停凭证会白占上游账号的本轮名额，后面那条
            # 启用中的同账号凭证每轮都被 seen 挡下 → 该渠道签到永久静默失效。
            if candidate.disabled or not candidate.enabled:
                report.skipped += 1
                continue
            # 渠道级止损：上游长期停发时不必每轮空锤（仅跳过签到，聊天照常）。
            if candidate.provider in skipped_providers:
                report.skipped += 1
                continue
            provider = self._providers.get(candidate.provider)
            checkin_scope = getattr(provider, "checkin_scope", None)
            # 解不开（APP_SECRET 换过 / 密文损坏）→ 跳过这条凭证：一条坏凭证
            # 不该让整轮签到失败（早先直接冒泡 → 整轮任务挂掉）。
            try:
                data = self._credentials.credential_data(candidate.credential_id)
            except CredentialDecryptError:
                logger.error("签到跳过 %s：凭证解密失败（APP_SECRET 是否被更换过？）",
                             candidate.credential_id)
                report.skipped += 1
                continue
            if provider is None or data is None or checkin_scope is None:
                report.skipped += 1
                continue
            # 企业版账号没有个人版签到活动：硬跑必然失败，会把「今日全成功」
            # 涂黑并触发 checkin_failing 告警（见 `account_shape`）。
            if is_enterprise_account(data):
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
            if self._backed_off(done_key, current, manual=manual):
                report.skipped += 1                      # 退避未到期，不调上游
                continue
            report.attempted += 1
            # 签到一轮含 status+claim+回查三次上游调用，与 quota_probe/growth
            # 共用同一 Pacer，避免绕开全局频率风控对策
            if self._pacer is not None:
                await self._pacer.wait_turn()
            try:
                with credential_scope(candidate.credential_id):
                    result = await provider.checkin(data)
            except Exception as error:  # noqa: BLE001
                logger.warning("checkin failed for %s: %s", candidate.credential_id,
                               error_text(error))
                self._note_failure(done_key, current)
                report.failed += 1
                continue
            # 当天这笔没发时，若凭证带登录凭据就按需登录一次再核（默认关）：
            # 上游只在「新登录」补发当天 300，而 refresh 换不出新会话。
            if not result.ok and self._can_autologin(provider, data, done_key, day):
                result, data = await self._try_autologin(
                    provider, candidate, data, result, done_key, day)
            if result.ok:
                report.succeeded += 1
                self._clear_failure(done_key)
                # 成功即封账该作用域：封账期内不再重试，失败凭证不受影响可继续
                # 重试。渠道给了 `seal_until` 就按它封（签到周期 ≠ 自然日，
                # 见 `CheckinResult.seal_until`），否则封到本地当日结束。
                # 上游可能给出超长值把该凭证长期封住 → 钳到 48h 上界。
                self._done_scopes[done_key] = self._seal_deadline(result, current)
                if self._on_success is not None:
                    self._on_success(candidate.credential_id)
            else:
                report.failed += 1
                self._note_failure(done_key, current, retry_at=result.retry_at)
                logger.warning("checkin failed for %s: %s",
                               candidate.credential_id, result.message or "未知原因")
            # 审计日志：成功/失败/已签都记一行（上游异常没有 result 时不记）。
            # 失败也要记是刻意的——「这个号连续几天签到都失败」正是要回看的东西。
            if self._events is not None:
                self._events.record(credential_id=candidate.credential_id,
                                    result=result, trigger=trigger)
        return report
