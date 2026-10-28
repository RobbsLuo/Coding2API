"""后台任务调度：把额度探测、签到、token 预刷新、明细清理接进应用生命周期。

设计要点（PROPOSAL §6 / TECHNICAL §6）：
- 两类任务共用一个全局随机间隔节流器（跨所有用户）
- 后台任务失败只记日志，绝不影响聊天请求
- 启动时先做一轮额度探测（不节流），避免调度器拿到全 unknown 的池
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from ..config import live
from .activity import ActivityTask
from .alerting import AlertTask
from .backup import BackupTask
from .checkin import CheckinTask
from .growth import GrowthTask
from .model_probe import ModelProbeTask
from .pacer import Pacer
from .quota_probe import QuotaProbeTask
from .refresh import RefreshTask
from .retention import RetentionTask
from .status import TASK_BY_KEY, TASK_SPECS, TaskStatusStore
from .upstream_status import UpstreamStatusTask

logger = logging.getLogger(__name__)


class UnknownTaskError(Exception):
    """请求手动运行一个不存在的任务 key（或该任务未装配）。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"unknown task {key!r}")
        self.key = key


class _LoopSpec(NamedTuple):
    """一条周期循环：任务 key + 一轮协程 + 当前生效间隔的取值器。

    `key` 同时是运行态（TaskStatusStore）与配置归属（HOT_SETTINGS.task）的
    连接键；展示名与描述从 `TASK_BY_KEY` 取，不在这里重复一份。
    """

    key: str
    runner: Callable[[], Awaitable[object]]
    interval: Callable[[], float]


class TaskRunner:
    """周期任务循环。每类任务一个 asyncio 任务，异常互不影响。"""

    def __init__(
        self,
        *,
        quota_probe: QuotaProbeTask,
    checkin: CheckinTask,
    growth: GrowthTask | None = None,
    activity: ActivityTask | None = None,
    refresh: RefreshTask,
    retention: RetentionTask,
    # 渠道模型列表兜底刷新：注入「跑一轮」的协程而不是任务对象——真正的活是
    # api.models.list_models（要 services 与 provider registry），tasks 层
    # 不该反向依赖 api 层。由 main 装配时传入；None 表示不装配这条循环。
    model_catalog: Callable[[], Awaitable[object]] | None = None,
    # OpenRouter 模型目录刷新（价格 + 明细 + 能力分，一次抓取）：同样注入
    # 「跑一轮」的协程（真正的活是 benchmarks.fetch_openrouter_models + 落盘 +
    # 换入），tasks 层不依赖 benchmarks/httpx。None 表示不装配。
    openrouter_catalog: Callable[[], Awaitable[object]] | None = None,
    # 运维告警：None 表示不装配该循环（老调用方/测试保持原行为）。
    alert: AlertTask | None = None,
    # SQLite 备份：None 表示不装配该循环（老调用方/测试保持原行为）。
    backup: BackupTask | None = None,
    # 上游状态页聚合：None 表示不装配该循环（老调用方/测试保持原行为）。
    upstream_status: UpstreamStatusTask | None = None,
    # 模型可用性探活：None 表示不装配该循环。即使装配了，周期为 0（默认）
    # 时也由 `_sync_model_probe` 每轮判成 no-op——探活会真发上游请求。
    model_probe: ModelProbeTask | None = None,
    quota_probe_minutes: int | Callable[[], int] = 60,
    growth_interval_minutes: int | Callable[[], int] = 60,
    refresh_interval_minutes: int | Callable[[], int] = 60,
    retention_interval_minutes: int | Callable[[], int] = 5,
    model_catalog_minutes: int | Callable[[], int] = 30,
    model_probe_minutes: int | Callable[[], int] = 0,
    openrouter_catalog_minutes: int | Callable[[], int] = 1440,
    alert_interval_minutes: int | Callable[[], int] = 5,
    backup_interval_minutes: int | Callable[[], int] = 1440,
    upstream_status_minutes: int | Callable[[], int] = 5,
    activity_enabled: Callable[[], bool] | None = None,
    alert_enabled: Callable[[], bool] | None = None,
    upstream_status_enabled: Callable[[], bool] | None = None,
    status: TaskStatusStore | None = None,
) -> None:
        self._quota_probe = quota_probe
        self._checkin = checkin
        self._growth = growth
        self._activity = activity
        self._refresh = refresh
        self._retention = retention
        self._model_catalog = model_catalog
        self._model_probe = model_probe
        self._openrouter_catalog = openrouter_catalog
        self._alert = alert
        self._backup = backup
        self._upstream_status = upstream_status
        # 周期可热更：存取值器，每轮 sleep 前读当前值（否则改配置
        # 要等到下一次重启才生效）。下限与业务语义同前，不变。
        self._quota_probe_minutes = live(quota_probe_minutes)
        self._growth_minutes = live(growth_interval_minutes)
        self._refresh_minutes = live(refresh_interval_minutes)
        self._retention_minutes = live(retention_interval_minutes)
        self._model_catalog_minutes = live(model_catalog_minutes)
        self._model_probe_minutes = live(model_probe_minutes)
        self._openrouter_catalog_minutes = live(openrouter_catalog_minutes)
        self._alert_minutes = live(alert_interval_minutes)
        self._backup_minutes = live(backup_interval_minutes)
        self._upstream_status_minutes = live(upstream_status_minutes)
        # 活跃上报是否启用也可热更：装配时恒建对象（构造成本为零），
        # 每轮由 _sync_activity 问一次，关着时是 no-op。
        self._activity_enabled = activity_enabled or (lambda: activity is not None)
        # 运维告警同理由 AlertTask 自己问（关着时 run_once 返回 None）
        self._alert_enabled = alert_enabled or (lambda: alert is not None)
        # 上游状态页抓取同理（关着时 run_once 返回 None）
        self._upstream_status_enabled = (upstream_status_enabled
                                         or (lambda: upstream_status is not None))
        # 活跃上报：每 10 分钟醒一次看时点（due() 只在配置小时窗口内放行），
        # 而不是整点只醒一次——服务恰在整点重启会整天漏报
        self._activity_interval = 600
        self._tasks: list[asyncio.Task[None]] = []
        # 进程内运行态（管理台「任务与配置」页）：共享实例由 build_app 注入，
        # 未注入时自建（测试与一次性装配直接构造 TaskRunner）。
        self.status = status or TaskStatusStore()
        # 正在真跑的 key 集合：周期循环与手动触发互斥。手动触发**不排队**
        # ——任务已在跑时立刻返回「运行中」，而不是攒一堆重复请求在锁后等
        # （连点十次会打十轮上游）。asyncio 单线程下「查 + 加」之间没有 await，
        # 是原子的，不需要额外锁。
        self._running: set[str] = set()

    @property
    def _quota_interval(self) -> float:
        return max(60, int(self._quota_probe_minutes()) * 60)

    @property
    def _growth_interval(self) -> float:
        # 成长中心请求量比签到大一个量级（一轮 7 类领取），间隔下限设 5 分钟：
        # 比这更密只会撞上游风控，而 Buddy 旅行最快 1 小时才回来
        return max(300, int(self._growth_minutes()) * 60)

    @property
    def _refresh_interval(self) -> float:
        return max(60, int(self._refresh_minutes()) * 60)

    @property
    def _retention_interval(self) -> float:
        return max(60, int(self._retention_minutes()) * 60)

    @property
    def _model_catalog_interval(self) -> float:
        # 下限 5 分钟与热更项 minimum 对齐：更密只是白打各渠道的 /models，
        # 而 zen 的免费模型判活本身就有 30 分钟缓存，再密也不会多探一次。
        return max(300, int(self._model_catalog_minutes()) * 60)

    @property
    def _openrouter_catalog_interval(self) -> float:
        # 下限 60 分钟与热更项 minimum 对齐：价格与能力指数变动都很少，更密只是
        # 白拉一遍（一次抓取同时供三处用）。
        return max(3600, int(self._openrouter_catalog_minutes()) * 60)

    @property
    def _model_probe_interval(self) -> float:
        # 周期 0 = 关闭，故下限与热更项 minimum 对齐到 1 分钟（不再按 60 分钟
        # 兜底，否则「填 1」会静默变成 60）。负缓存本身 6h 起步，实际部署建议
        # ≥60 分钟——这条只防手滑填成 0 以外的小值。
        return max(60, int(self._model_probe_minutes()) * 60)

    @property
    def _alert_interval(self) -> float:
        # 下限 1 分钟：告警是「越早越好」的观测，但比 1 分钟更密只会让
        # 每轮读池/统计的开销白花，而池与错误率不会在秒级翻转。
        return max(60, int(self._alert_minutes()) * 60)

    @property
    def _backup_interval(self) -> float:
        # 下限 60 分钟与热更项 minimum 对齐：库不大，更密只是白占磁盘。
        return max(3600, int(self._backup_minutes()) * 60)

    @property
    def _upstream_status_interval(self) -> float:
        # 下限 1 分钟与热更项 minimum 对齐：官方状态页本身就是分钟级更新的，
        # 抓得比它更新只是白打别人的接口。
        return max(60, int(self._upstream_status_minutes()) * 60)

    @property
    def _checkin_interval(self) -> float:
        """签到循环的醒转周期：留空（旧行为）10 分钟，配了时点则 1 分钟。

        为什么必须短：`due()` 的窗口是 `[时点-抖动, 时点]`，抖动=0 时只有
        「恰好那一分钟」。10 分钟周期下，醒来时刻与配置时点几乎必然错开，
        结果是**一天一次都不签**——比旧的全天轮询更糟。1 分钟是 60 秒精度
        与「醒来仅比较几个整数」之间的折中。
        """
        return 60.0 if self._checkin.has_times() else 600.0

    async def start(self) -> None:
        """启动所有周期任务；首轮额度探测与 token 预刷新都立即执行（不节流）。

        首轮刷新必须抢先跑：循环体是先睡后跑，而 CodeArts 的一次性
        refresh_token 现在只由 RefreshTask 轮转（额度探测不再保活）。服务重启时
        若凭证已落在到期窗口，不抢先刷就会带病服务到下一轮（60 分钟），期间
        聊天 401 会被硬禁用。
        """
        await self._guarded(self._quota_probe.run_once(apply_pacing=False),
                            "启动额度探测", key="quota_probe")
        await self._guarded(self._refresh.run_once(),
                            "启动 token 预刷新", key="refresh")
        for spec in self._loop_specs():
            self._tasks.append(asyncio.create_task(
                self._loop(TASK_BY_KEY[spec.key].name, spec.runner, spec.interval,
                           key=spec.key)))

    def _loop_specs(self, *, manual: bool = False) -> list[_LoopSpec]:
        """当前装配的全部周期循环（key + 一轮协程 + 间隔取值器）。

        周期循环与手动触发**共用这一份清单**：`start()` 据此建循环，
        `run_task()` 据此解析手动请求。两处各写一份会让「手动能跑但定时没
        接线」（或反之）——加任务时只改一处，两处行为不会漂移。

        可选任务（growth / activity / model_catalog / …）未装配时整条跳过，
        与 `task_status()` 的过滤口径一致。

        `manual=True` 时**绕过时点窗口**（`due()`）：手动就是「立刻跑」，
        不再问「到点了没」。开关类（activity 的 `enabled`）不绕——关着时
        `run_once` 自身返回 None，记 no-op。
        """
        specs: list[_LoopSpec] = [
            self._spec("quota_probe", lambda: self._quota_probe.run_once()),
            self._spec("refresh", self._refresh.run_once),
            self._spec("retention", self._sync_retention),
            # 签到：留空时点 = 全天每 10 分钟一轮；有配置时点时仍按 10 分钟醒
            # 一次，由 `due()` 自己判定是否落在抖动窗口内。**不能**把周期改成
            # 「按最大时点间隔算」——服务恰在两个时点之间重启会直接漏签，而
            # 10 分钟空转的代价只是读一次配置（与 activity 的取舍同理）。
            self._spec("checkin",
                       ((lambda: self._sync_checkin(manual=True)) if manual
                        else self._sync_checkin)),
        ]
        if self._growth is not None:
            specs.append(self._spec("growth", self._sync_growth))
        if self._activity is not None:
            activity = ((lambda: self._sync_activity(manual=True)) if manual
                        else self._sync_activity)
            specs.append(self._spec("activity", activity))
        if self._model_catalog is not None:
            specs.append(self._spec("model_catalog", self._model_catalog))
        if self._model_probe is not None:
            specs.append(self._spec("model_probe", self._sync_model_probe))
        if self._openrouter_catalog is not None:
            specs.append(self._spec("openrouter_catalog", self._openrouter_catalog))
        if self._alert is not None:
            specs.append(self._spec("alert", self._sync_alert))
        if self._backup is not None:
            specs.append(self._spec("backup", self._sync_backup))
        if self._upstream_status is not None:
            specs.append(self._spec("upstream_status", self._sync_upstream_status))
        return specs

    def _spec(self, key: str,
              runner: Callable[[], Awaitable[object]]) -> _LoopSpec:
        """拼一条循环描述：周期取值器统一走 `_interval_seconds`（热更现读）。"""
        return _LoopSpec(key, runner, lambda: self._interval_seconds(key))

    async def run_task(self, key: str) -> dict[str, Any]:
        """手动运行一个后台任务一轮，返回该任务的最新运行态。

        语义（与既有 per-credential 手动动作 probe / checkin / growth 同口径）：

        - **绕过 due() / 时点窗口**：手动就是「立刻跑」，不再问「到点了没」。
        - **不绕开关**：activity / alert 关闭时其 `run_once` 自身返回 None，
          本轮记 `ran=False`（页面按钮同时按 `enabled` 禁用，两条路径一致）。
        - **不排队**：任务正在跑（周期循环或另一次手动）时立刻返回
          `ran=False, busy=True`，而不是攒一串重复请求在锁后等。
        - 等结果返回后再回响应：调用方（管理台）要能立刻看到 report / error。
        """
        spec = next((item for item in self._loop_specs(manual=True)
                     if item.key == key), None)
        if spec is None:
            raise UnknownTaskError(key)
        if key in self._running:
            return {**self._task_item(key), "ran": False, "busy": True}
        _ok, ran = await self._run_guarded(spec.runner, TASK_BY_KEY[key].name, key=key)
        return {**self._task_item(key), "ran": ran, "busy": False}

    @staticmethod
    def _require(task: object | None, label: str) -> Any:
        """取已装配的任务对象；未装配时显式报错（而不是静默 no-op）。

        这类失败只可能来自装配错误（可选任务未接线却出现在清单里），
        必须让 `_guarded` 记成失败而不是「未运行」。
        """
        if task is None:
            raise RuntimeError(f"{label} task not configured")
        return task

    async def _sync_growth(self) -> object:
        """成长中心一轮：领礼物 / 派 Buddy / 任务 / 补登 / 兑换 / 抽奖 / 盲盒。"""
        task = self._require(self._growth, "growth")
        return await task.run_once()

    async def _sync_activity(self, *, manual: bool = False) -> object:
        """活跃上报：仅启用且处于配置小时窗口内才执行，成功凭证当日封账。

        「是否启用」每轮现读：关掉再打开不需要重启，也不必重建循环。
        `manual=True`（管理台「立即运行」）绕过时点窗口：手动就是立刻跑。
        """
        task = self._require(self._activity, "activity")
        if not self._activity_enabled():
            return None
        if not manual and not self._activity.due():
            return None
        return await task.run_once()

    async def _sync_checkin(self, *, manual: bool = False) -> object:
        """签到一轮：有配置时点时只在窗口内跑，留空则全天每 10 分钟一轮。

        成功凭证当日封账（run_once 内跳过），失败凭证继续重试。
        `manual=True`（管理台「立即运行」）绕过时点窗口：手动就是立刻跑。
        """
        if not manual and not self._checkin.due():
            return None
        return await self._checkin.run_once()

    async def _sync_retention(self) -> object:
        """明细清理：整表 GROUP BY / DELETE 是同步 SQLite，放线程池跑。

        直接在事件循环里执行会把所有并发请求卡住（热更把
        `context_compress_min_keep_messages` 调大后可达秒级）。
        """
        return await asyncio.to_thread(self._retention.run_once)

    async def _sync_model_probe(self) -> object:
        """模型可用性探活一轮：周期 0（默认）时是 no-op。

        是否开启每轮现读：探活要真发上游请求、消耗额度，默认关闭；管理台把
        「模型探活周期」填上即生效，不必重启。关着时返回 None，`_guarded`
        不记运行态——页面显示「未运行」，而不是「刚跑过」。
        """
        task = self._require(self._model_probe, "model probe")
        if int(self._model_probe_minutes()) <= 0:
            return None
        return await task.run_once()

    async def _sync_alert(self) -> object:
        """运维告警一轮：评估四类规则、静默去重后落库 + 推送。

        是否启用每轮现读：AlertTask.run_once 关着时返回 None，
        `_guarded` 不记运行态——页面显示「未运行」，而不是「刚跑过」。
        """
        task = self._require(self._alert, "alert")
        return await task.run_once()

    async def _sync_backup(self) -> object:
        """数据库备份一轮：VACUUM INTO 是同步磁盘 IO，放线程池跑。

        直接在事件循环里执行会卡住所有并发请求（库越大越久）。
        """
        task = self._require(self._backup, "backup")
        return await asyncio.to_thread(task.run_once)

    async def _sync_upstream_status(self) -> object:
        """上游状态页一轮：并发抓全部源并原子替换内存快照。

        是否启用每轮现读：任务关着时 `run_once` 返回 None，`_guarded` 不记
        运行态——页面显示「未运行」，而不是「刚跑过」。
        """
        task = self._require(self._upstream_status, "upstream status")
        return await task.run_once()

    async def _loop(self, name: str, runner: Callable[[], Awaitable[object]],
                    interval: float | Callable[[], float],
                    key: str | None = None) -> None:
        """周期循环：间隔在每次 sleep 前重新求值（热更周期）。

        传标量等价于固定周期（测试与一次性任务仍这么用）。
        """
        delay = live(interval)
        while True:
            # 间隔取值本身也可能抛错（热更取值器读配置 / 解析失败）。它必须在
            # 守卫之外单独兜住：`_guarded` 只保护 runner 本体，取值若在循环里
            # 裸奔抛异常，整个任务循环就永久退出——一次配置读取失败会让该任务
            # 从此不再运行，且页面上只是「停止更新」。失败时退回 60 秒再试。
            try:
                seconds = delay()
            except Exception as error:  # noqa: BLE001 - 后台任务不能拖垮服务
                logger.warning("后台任务「%s」读取间隔失败: %s，60 秒后重试",
                               name, error)
                seconds = 60.0
            try:
                await asyncio.sleep(seconds)
            except asyncio.CancelledError:
                raise
            await self._run_guarded(runner, name, key=key)

    async def _run_guarded(self, runner: Callable[[], Awaitable[object]], name: str,
                           *, key: str | None = None) -> tuple[bool, bool]:
        """跑一轮并标记「运行中」，返回 `(是否成功, 是否真跑了一轮)`。

        `key` 非空时在真跑期间占住该 key：手动触发撞上周期循环（或反之）
        时立刻返回「运行中」，不并发打两轮上游。

        「是否真跑」以运行态计数是否增加判定（`_guarded` 对 no-op 不入账）：
        activity 关闭时 `run_once` 返回 None，本轮就不算跑过——页面显示
        「未运行」，与定时循环同一口径。
        """
        if key is None:
            ok = await self._guarded(runner(), name)
            return ok, ok
        if key in self._running:
            return False, False
        self._running.add(key)
        try:
            before = self.status.runs(key)
            ok = await self._guarded(runner(), name, key=key)
            return ok, self.status.runs(key) > before
        finally:
            self._running.discard(key)

    async def _guarded(self, awaitable: Awaitable[object], name: str,
                       key: str | None = None) -> bool:
        """跑一轮并吞掉异常（后台任务不能拖垮服务）。

        `key` 给出时把「真实执行」记进运行态：返回 None 表示本轮 no-op
        （签到未到点 / 活跃上报未启用），不覆盖上一次结果——否则页面会显示
        「签到刚刚跑过」，而当天其实一次都没签。
        """
        started = time.time()
        try:
            result = await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 后台任务不能拖垮服务
            logger.warning("后台任务「%s」失败: %s", name, error)
            if key is not None:
                self.status.record(key, started_at=started, ok=False,
                                   report=None, error=str(error))
            return False
        if key is not None and result is not None:
            self.status.record(key, started_at=started, ok=True,
                               report=_as_report(result), error=None)
        return True

    def task_status(self) -> list[dict[str, Any]]:
        """管理台「任务与配置」页：每个已装配任务的上次真跑 + 当前周期/开关。

        周期与开关是**当前生效值**（热更后立刻反映），不是装配时的快照。
        装配判断复用 `_loop_specs()`（与 `start()` / `run_task()` 同一份清单），
        顺序仍按 `TASK_SPECS`（前端 tab 顺序稳定）。
        """
        assembled = {spec.key for spec in self._loop_specs()}
        return [self._task_item(spec.key)
                for spec in TASK_SPECS if spec.key in assembled]

    def _task_item(self, key: str) -> dict[str, Any]:
        """一个任务的最新运行态（`task_status()` 与手动触发响应的共同形状）。"""
        spec = TASK_BY_KEY[key]
        run = self.status.get(key)
        return {
            "key": spec.key,
            "name": spec.name,
            "description": spec.description,
            "group": spec.group,
            "interval_seconds": self._interval_seconds(spec.key),
            "enabled": self._task_enabled(spec.key),
            "running": spec.key in self._running,
            "runs": self.status.runs(spec.key),
            "last_started_at": run.started_at if run is not None else None,
            "last_finished_at": run.finished_at if run is not None else None,
            "last_ok": run.ok if run is not None else None,
            "last_report": run.report if run is not None else None,
            "last_error": run.error if run is not None else None,
        }

    def _interval_seconds(self, key: str) -> float:
        """按 key 取当前生效周期（秒）。property 是唯一真源，这里只做派发。"""
        getter = self._interval_getters().get(key)
        return float(getter()) if getter is not None else self._retention_interval

    def _interval_getters(self) -> dict[str, Callable[[], float]]:
        """key → 周期 property 的派发表。

        与 `_task_enabled` 分开维护：周期是每个任务必有的一项，开关只有
        activity / alert / upstream_status 三个可关。用字典而非 if-chain，
        新增任务时漏接线会直接落进 `_interval_seconds` 的兜底分支（保留期），
        而不是静默返回别的任务的周期。
        """
        return {
            "quota_probe": lambda: self._quota_interval,
            "refresh": lambda: self._refresh_interval,
            "retention": lambda: self._retention_interval,
            "checkin": lambda: self._checkin_interval,
            "growth": lambda: self._growth_interval,
            "activity": lambda: self._activity_interval,
            "model_catalog": lambda: self._model_catalog_interval,
            "model_probe": lambda: self._model_probe_interval,
            "openrouter_catalog": lambda: self._openrouter_catalog_interval,
            "alert": lambda: self._alert_interval,
            "backup": lambda: self._backup_interval,
            "upstream_status": lambda: self._upstream_status_interval,
        }

    def _task_enabled(self, key: str) -> bool:
        """任务是否启用；只有三个可关的任务需要现读开关，其余恒 True。"""
        getter = {
            "activity": self._activity_enabled,
            "alert": self._alert_enabled,
            "upstream_status": self._upstream_status_enabled,
        }.get(key)
        return True if getter is None else bool(getter())

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()


def _as_report(result: object) -> dict[str, Any]:
    """任务返回值 → JSON 可序列化摘要。

    `TaskReport` 有 `as_dict()`；清理任务直接返回 dict；其它形态（含 None 之外
    的自定义对象）只记 `{"result": repr}`，保证运行态一定能落成 JSON。
    """
    as_dict = getattr(result, "as_dict", None)
    if callable(as_dict):
        report = as_dict()
        if isinstance(report, dict):
            return report
    if isinstance(result, dict):
        return result
    return {"result": repr(result)}


def build_runner(credentials, providers: dict, stats_collector, config,
                 growth_events=None, credit_events=None, audit=None,
                 model_catalog: Callable[[], Awaitable[object]] | None = None,
                 openrouter_catalog: Callable[[], Awaitable[object]] | None = None,
                 openrouter_catalog_minutes: int | Callable[[], int] = 1440,
                 alerts=None,
                 backup: BackupTask | None = None,
                 upstream_status: UpstreamStatusTask | None = None,
                 model_probe: ModelProbeTask | None = None,
                 model_probe_minutes: int | Callable[[], int] = 0,
                 status: TaskStatusStore | None = None) -> TaskRunner:
    """按配置装配后台任务（Pacer 由两个 provider 共享）。

    growth_events 为 None 时（老调用方/测试）不装配成长中心任务：没有落库目标
    就跑起来只会把结果丢掉；生产路径（main.py）总是传入。

    credit_events 同理：为 None 时保留流水清理不启用（表仍会随探测增长，
    但不影响功能；生产路径总是传入）。

    audit 同理：为 None 时不清理审计流水（表仍会随登录/写操作增长；
    生产路径总是传入）。

    model_catalog 同理：None 时不装配渠道模型列表刷新循环（老调用方与测试保持
    原行为）；生产路径传入「跑一轮 list_models」的协程。

    openrouter_catalog 同理：None 时不装配 OpenRouter 模型目录刷新循环；生产路径
    传入「拉 OpenRouter 模型列表 + 落盘 + 就地把价表/明细/能力分三张表换入」的
    协程（价格 + 明细 + 能力分一次抓完）。

    alerts 同理：None 时不装配运维告警循环、也不清理告警记录（老调用方与测试
    保持原行为；生产路径传入 AlertRepository）。

    backup 同理：None 时不装配数据库备份循环（老调用方与测试保持原行为）；
    生产路径传入 BackupTask（需要 config.db_path，装配期在 main.py 构造）。

    upstream_status 同理：None 时不装配上游状态页抓取循环（老调用方与测试保持
    原行为）；生产路径传入 UpstreamStatusTask（快照挂在 app.state 供路由读取）。

    model_probe 同理：None 时不装配模型探活循环。周期默认 0（关闭）——探活
    会真发上游请求、消耗额度，开不开由部署者按余量决定；管理台把「模型探活
    周期」填上即生效（热更，不必重启）。

    热更：`config` 既可以是启动期快照 `Settings`，也可以是
    `RuntimeSettings` 覆盖层。装配时所有「可热更项」必须传**零参 lambda**，
    后台循环每轮读当前值——否则改周期/开关都要等重启，热更就名存实亡。
    注意不能写成 `live(config.x)`：那会立刻求值一次再包成常量，
    对 `Settings` 无害、对覆盖层则等于没热更。
    """
    pacer = Pacer(lambda: config.pacer_min_seconds,
                  lambda: config.pacer_max_seconds)
    growth = None
    if growth_events is not None:
        growth = GrowthTask(credentials, providers, growth_events,
                            allow_irreversible=lambda: config.growth_irreversible_actions,
                            pacer=pacer)
    # 活跃上报恒建对象（构造成本为零），是否真的跑由每条循环现读的开关决定：
    # 只有对象先存在，管理台才能把默认关闭的它热开到不需要重启。
    activity = ActivityTask(credentials, providers, events=growth_events,
                            hour=lambda: config.activity_report_hour)
    # 运维告警与运行态共享同一个 TaskStatusStore：告警要读各任务的连续失败
    # 计数，若自建 store 会永远读到 0（「任务连续失败」规则静默失效）。
    status_store = status or TaskStatusStore()
    alert = None
    if alerts is not None:
        alert = AlertTask(credentials, alerts, stats_collector,
                          task_status=status_store,
                          enabled=lambda: config.alert_enabled,
                          webhook_url=lambda: config.alert_webhook_url,
                          pool_ready_min=lambda: config.alert_pool_ready_min,
                          task_failures=lambda: config.alert_task_failures,
                          token_expiry_hours=lambda: config.alert_token_expiry_hours,
                          error_rate_threshold=lambda: config.alert_error_rate_threshold,
                          error_rate_min_requests=lambda: config.alert_error_rate_min_requests,
                          error_rate_window_minutes=lambda: config.alert_error_rate_window_minutes,
                          key_burst_tokens=lambda: config.alert_key_burst_tokens,
                          key_burst_window_minutes=lambda: config.alert_key_burst_window_minutes,
                          off_hours_start=lambda: config.alert_off_hours_start,
                          off_hours_end=lambda: config.alert_off_hours_end,
                          off_hours_min_requests=lambda: config.alert_off_hours_min_requests,
                          budget_cny=lambda: config.alert_budget_cny,
                          budget_levels=lambda: config.alert_budget_levels,
                          silence_minutes=lambda: config.alert_silence_minutes,
                          for_ticks=lambda: config.alert_for_ticks,
                          clear_ticks=lambda: config.alert_clear_ticks,
                          app_secret=lambda: config.app_secret)
    return TaskRunner(
        quota_probe=QuotaProbeTask(credentials, providers, pacer),
        checkin=CheckinTask(credentials, providers, pacer=pacer,
                            times=lambda: config.checkin_times,
                            jitter_minutes=lambda: config.checkin_jitter_minutes),
        growth=growth,
        activity=activity,
        refresh=RefreshTask(credentials, providers, skew_seconds=config.refresh_skew_hours * 3600,
                            now=lambda: int(time.time()), pacer=pacer),
        retention=RetentionTask(stats_collector, credentials=credentials,
                                credit_events=credit_events, audit=audit,
                                alerts=alerts, growth_events=growth_events),
        quota_probe_minutes=lambda: config.quota_probe_minutes,
        refresh_interval_minutes=lambda: config.refresh_interval_minutes,
        growth_interval_minutes=lambda: config.growth_interval_minutes,
        model_catalog=model_catalog,
        model_catalog_minutes=lambda: config.model_catalog_minutes,
        openrouter_catalog=openrouter_catalog,
        openrouter_catalog_minutes=lambda: config.openrouter_catalog_minutes,
        alert=alert,
        alert_interval_minutes=lambda: config.alert_interval_minutes,
        backup=backup,
        backup_interval_minutes=lambda: config.backup_interval_minutes,
        upstream_status=upstream_status,
        upstream_status_minutes=lambda: config.upstream_status_minutes,
        model_probe=model_probe,
        model_probe_minutes=lambda: config.model_probe_minutes,
        activity_enabled=lambda: config.activity_report_enabled,
        alert_enabled=lambda: config.alert_enabled,
        upstream_status_enabled=lambda: config.upstream_status_enabled,
        status=status_store,
    )
