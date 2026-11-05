"""每日签到：按「endpoint + X-User-Id」隔离，同账号多凭证共享一次。"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import live
from ..db.crypto import CredentialDecryptError
from ..db.repo import CredentialRepository
from ..error_text import error_text
from ..request_context import credential_scope
from . import TaskReport
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

    def _note_failure(self, done_key: str, now: time.struct_time) -> None:
        """记一次失败并按连续失败次数翻倍退避（10m → 20m → 40m → 封顶 1h）。"""
        streak = self._failures.get(done_key, (0, 0))[0] + 1
        delay = min(_FAIL_BACKOFF_BASE * 2 ** min(streak - 1, 6), _FAIL_BACKOFF_MAX)
        self._failures[done_key] = (streak, _cn_epoch(now) + delay)

    def _clear_failure(self, done_key: str) -> None:
        """成功即清零：下次再失败从 10 分钟重新起算，不继承旧的退避档位。"""
        self._failures.pop(done_key, None)

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
        self._prune(current)
        for candidate in self._credentials.candidates():
            # `enabled=0`（用户软暂停）同样跳过，口径与调度器 is_selectable 一致。
            # 必须早于 seen.add：否则暂停凭证会白占上游账号的本轮名额，后面那条
            # 启用中的同账号凭证每轮都被 seen 挡下 → 该渠道签到永久静默失效。
            if candidate.disabled or not candidate.enabled:
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
            if result.ok:
                report.succeeded += 1
                self._clear_failure(done_key)
                # 成功即封账该作用域：封账期内不再重试，失败凭证不受影响可继续
                # 重试。渠道给了 `seal_until` 就按它封（签到周期 ≠ 自然日，
                # 见 `CheckinResult.seal_until`），否则封到本地当日结束。
                self._done_scopes[done_key] = result.seal_until or self._day_end(current)
                if self._on_success is not None:
                    self._on_success(candidate.credential_id)
            else:
                report.failed += 1
                self._note_failure(done_key, current)
                logger.warning("checkin failed for %s: %s",
                               candidate.credential_id, result.message or "未知原因")
            # 审计日志：成功/失败/已签都记一行（上游异常没有 result 时不记）。
            # 失败也要记是刻意的——「这个号连续几天签到都失败」正是要回看的东西。
            if self._events is not None:
                self._events.record(credential_id=candidate.credential_id,
                                    result=result, trigger=trigger)
        return report
