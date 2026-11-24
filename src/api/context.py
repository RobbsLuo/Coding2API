"""上下文压缩接线（按实际服务渠道取窗口）。

`src/engine/compress.py` 是纯函数；本模块构造「按渠道查输入上限 → 压缩」的
闭包，注入执行引擎（`ExecutorDeps.context_compress`）。压缩点在**选号之后、
发上游之前**：同名模型挂多渠道时各自上限不同（qoder 180K vs codebuddy 1M），
路由前取最小值会让大窗口渠道的会话被小窗口渠道的阈值反复裁剪——估算器口径
偏高时每轮裁剪结果不稳定，上游前缀缓存只命中 system 段（实测命中率从 96%+
跌到 7-16%）。按实际落到的渠道取窗口后，未超限的会话零裁剪，前缀单调增长、
缓存可正常命中。

目录里查不到输入上限时**不压缩**：宁可不裁剪，也不拿猜出来的数字去砍用户
上下文；因此本功能对未知模型是零副作用的。

**声明值 ≠ 实际可用量**：上游目录里的 `max_input_tokens` 是模型自报的声明值，
个别渠道严重低估（TRAE 声明 200K/232K，实测可吃约 1M token）。压缩预算用
`_CHANNEL_WINDOW_FLOOR` 抬到实测下限，但 `/v1/models` 对外宣告仍用声明值——
客户端的保守认知与我们的实际裁剪阈值分开，既不虚假承诺，也不主动制造有损压缩。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..engine.compress import compress_messages, estimate_messages
from ..engine.prefix_debug import PrefixDebugTracker
from ..engine.sticky_trim import StickyTrimmer, conversation_key
from ..engine.tool_output import compress_tool_outputs
from ..persistence.compress_buckets import (
    COUNTER_FIELDS,
    HOUR_SECONDS,
    PERSIST_INTERVAL_SECONDS,
    RETENTION_DAYS,
    VERSION,
    load_buckets,
    save_buckets,
)

logger = logging.getLogger(__name__)

#: 字符 → token 的**下界**代理系数（纯 ASCII 按 `compress.estimate_tokens` 的
#: `_OTHER_WEIGHT`）。压缩收益只用于「该开还是该关」的量级判断，不需要精确：
#: 逐字符跑 `estimate_tokens` 在 16MB 请求体上是千万级 Python 迭代，会把事件
#: 循环卡住（与 `engine/compress.py` 的图片 part 估算同一取舍）。
_TOKENS_PER_CHAR = 0.25


@dataclass
class CompressStats:
    """压缩收益的进程内计数器（随 `/metrics` 输出 `coding2api_compress_*`）。

    为什么只存进程内：收益是「这次部署开没开、开了省多少」的运维判断，不是
    需要长期回溯的账单；落库会引入新表与保留期清理。默认实例的语义与
    Prometheus counter reset 相容；装配侧给 `persist_path` 后按小时桶落盘
    续接（见 `persistence/compress_buckets.py`）。

    `unchanged` 是「调用过压缩、但载荷逐字节没动」的次数——它才是「开关开着
    却毫无收益」的证据；只看 `triggers` 会把「读了开关但没命中」也算成收益。

    上方标量是**终身累计**（`/metrics` 的 counter 口径，不随小时桶裁剪倒退）；
    `buckets` 是**按小时分桶**的同名计数，供统计页按所选时间窗求和——否则
    面板的「省下 token」永远是「自本次启动以来」，与同屏按窗口计算的缓存
    命中率口径错位。

    `persist_path` 非空时按 `persist_interval` 节流写盘、启动经 `load()` 读回：
    重启不再丢整段历史。落盘是可选的**续接**手段，纯内存实例（测试/单次装配）
    语义不变。
    """

    triggers: int = 0            # 压缩实际改写了载荷的次数（tool + 上下文合计）
    tool_triggers: int = 0       # 其中由 tool 输出压缩触发的
    context_trims: int = 0       # 其中由上下文裁剪触发的
    chars_saved: int = 0         # 省下的字符数（压缩前后 content 长度差）
    unchanged: int = 0           # 调用了压缩但载荷未变的次数
    #: hour_utc（桶起点秒）→ 同名字段计数。超过保留期的桶在写入时丢弃。
    buckets: dict[int, dict[str, int]] = field(default_factory=dict)
    #: 时钟可注入：测试按整点定位桶，不必 sleep。
    clock: Callable[[], float] = time.time
    #: 上次裁剪到的小时（同一小时内不重复扫描；不参与比较/展示）。
    _pruned_hour: int | None = field(default=None, repr=False, compare=False)
    #: 落盘快照路径；None = 纯内存（测试与单次装配默认）。
    persist_path: str | None = None
    #: 两次落盘的最小间隔（秒）：长会话里压缩每轮都触发，逐次写盘不划算。
    persist_interval: float = PERSIST_INTERVAL_SECONDS
    #: 上次成功落盘的时钟；None = 本次启动还没写过。
    _last_persist_at: float | None = field(default=None, repr=False, compare=False)
    #: 是否有未落盘的变化：关机补写与失败重试都靠它。
    _dirty: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def load(cls, path: str, *,
             clock: Callable[[], float] = time.time) -> CompressStats:
        """从落盘快照恢复小时桶与终身累计；缺失/损坏时回到全新计数器。"""
        buckets, lifetime = load_buckets(path, now=clock())
        stats = cls(clock=clock, persist_path=path, **lifetime)
        stats.buckets = buckets
        return stats

    def as_dict(self, *, since: int | None = None,
                until: int | None = None) -> dict[str, Any]:
        """快照；给 `since`/`until` 就只汇总落在该窗口内的小时桶。

        窗口两端与 `usage_hourly.hour_utc` 的过滤同口径（整点对齐、闭下界
        开上界 `[since, until)`）：这样「省下的 token」与同屏的缓存命中率
        覆盖同一批小时。两端都不给 → 终身累计（「全部」与 `/metrics`）。
        """
        if since is None and until is None:
            values = {name: getattr(self, name) for name in COUNTER_FIELDS}
        else:
            values = dict.fromkeys(COUNTER_FIELDS, 0)
            for hour, bucket in self.buckets.items():
                if since is not None and hour < since:
                    continue
                if until is not None and hour >= until:
                    continue
                for name in COUNTER_FIELDS:
                    values[name] += bucket[name]
        return {
            **values,
            "tokens_saved": int(values["chars_saved"] * _TOKENS_PER_CHAR),
        }

    def _bump(self, **deltas: int) -> None:
        """把增量同时累加到终身标量与当前小时桶，并按需裁剪过期桶。"""
        hour = int(self.clock() // HOUR_SECONDS) * HOUR_SECONDS
        bucket = self.buckets.setdefault(hour, dict.fromkeys(COUNTER_FIELDS, 0))
        for name, delta in deltas.items():
            setattr(self, name, getattr(self, name) + delta)
            bucket[name] += delta
        if self._pruned_hour != hour:
            # 只在跨小时时扫一次：一条长会话里压缩是逐轮调用的常态，
            # 每次请求都遍历全部桶是纯浪费。
            self._pruned_hour = hour
            cutoff = hour - RETENTION_DAYS * 24 * HOUR_SECONDS
            for key in [key for key in self.buckets if key < cutoff]:
                del self.buckets[key]
        self._dirty = True
        self._maybe_persist()

    def _payload(self) -> dict[str, Any]:
        """落盘载荷：终身累计与小时桶分开存，加载时只有桶会按保留期裁剪。"""
        return {
            "version": VERSION,
            "saved_at": self.clock(),
            "lifetime": {name: getattr(self, name) for name in COUNTER_FIELDS},
            "buckets": {str(hour): dict(bucket)
                        for hour, bucket in self.buckets.items()},
        }

    def _maybe_persist(self) -> None:
        """节流落盘：首次变化立即写，之后至少隔 `persist_interval` 秒。"""
        if self.persist_path is None:
            return
        moment = self.clock()
        if (self._last_persist_at is not None
                and moment - self._last_persist_at < self.persist_interval):
            return
        self.flush()

    def flush(self) -> None:
        """无条件补写未落盘的变化（关机 / 节流窗口外）；无变化时是空操作。"""
        if self.persist_path is None or not self._dirty:
            return
        if save_buckets(self.persist_path, self._payload()):
            self._dirty = False
            self._last_persist_at = self.clock()

    @property
    def tokens_saved(self) -> int:
        """省下的 token 估算（字符数 × 下界系数，见 `_TOKENS_PER_CHAR`）。"""
        return int(self.chars_saved * _TOKENS_PER_CHAR)


#: 进程级计数器：装配侧（`main.py`）的压缩闭包与 `/metrics` 读同一个实例，
#: 因此仪器化对调用方零改动。测试直接传自己的 `CompressStats()` 即可隔离。
COMPRESS_STATS = CompressStats()


def _string_content_chars(messages: Any) -> int:
    """消息列表里字符串 content 的字符总数（压缩收益的统一口径）。"""
    if not isinstance(messages, list):
        return 0
    return sum(len(message["content"]) for message in messages
               if isinstance(message, dict)
               and isinstance(message.get("content"), str))


# 渠道实际窗口下限（只用于**压缩预算**，不影响 `/v1/models` 对外宣告）。
#
# 上游模型目录里的 `max_input_tokens` 是「声明值」，可能与实际可接受量差很远。
# TRAE 实测：声明 232768/200000，实际 1,450,000 字符（约 1,035,544 token）仍能
# 正确读到开头内容，1,470,000 字符才报 4022/4026（针在开头、问题在结尾、
# 中间填随机 ASCII，答对暗号即证明窗口横跨整段；复现见
# scripts/probe_trae_context_limit.py）。按声明值算，压缩预算只有
# 217K token，会在用户发 30 万 token 时把上游明明能吃的内容静默压掉。
#
# 只抬压缩预算、不改宣告值：客户端看到的仍是保守上限，不会主动发超限请求；
# 我们自己也不再制造有损压缩。取值 1,048,576（2^20）与实测硬边界吻合，且经
# `safety_ratio` 后实际预算约 992K，低于实测通过点。
_CHANNEL_WINDOW_FLOOR: dict[str, int] = {"trae": 1_048_576}


def context_window_for(cache: dict[str, dict[str, Any]],
                       aliases: dict[str, dict[str, str]],
                       provider_id: str, model: str) -> int | None:
    """单渠道的模型输入上限：按归一名或别名原始 id 在该渠道目录取值。

    `model` 可带 `@provider` 后缀（防御：executor 传入的已是去掉后缀的
    归一名，这里再剥一层不伤直调方）。查不到 / 值非法（≤0、非 int、bool）
    视为未知，返回 None。

    查到后按 `_CHANNEL_WINDOW_FLOOR` 抬到渠道实测下限：该表只描述「压缩时
    敢用多大预算」，不影响对外宣告。
    """
    name = (model or "").strip()
    if "@" in name:
        name = name.rsplit("@", 1)[0].strip()
    key = name.lower()
    if not key:
        return None
    table = cache.get(provider_id) or {}
    candidates = {key}
    raw = (aliases.get(provider_id) or {}).get(key)
    if raw:
        candidates.add(raw.lower())
    for candidate in candidates:
        entry = table.get(candidate)
        value = getattr(entry, "max_input_tokens", None)
        # bool 是 int 子类，显式排除；<=0 视为未上报
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return max(value, _CHANNEL_WINDOW_FLOOR.get(provider_id, 0))
    return None


def build_context_compressor(
        cache: dict[str, dict[str, Any]],
        aliases: dict[str, dict[str, str]],
        settings: Any,
        stats: CompressStats | None = None) -> Callable[[str, str, dict], dict]:
    """构造 `(provider_id, model, payload) → payload` 的压缩闭包。

    `settings` 传运行态覆盖层（`RuntimeSettings`）：开关与四个参数每轮
    请求现读，管理台热更即生效。装配侧（main.py）把它注入
    `ExecutorDeps.context_compress`；executor 在选号后按实际渠道调用。

    `stats` 是收益计数器（默认进程级 `COMPRESS_STATS`）：触发次数 / 省下的
    字符与 token / 原地不变次数，由 `/metrics` 与 `/api/stats/compression`
    同源读取。只计数、不改行为——开关关闭时载荷仍逐字节不变。
    """
    counters = stats if stats is not None else COMPRESS_STATS
    # 粘性裁剪的边界记忆（进程内，按会话+渠道）：压缩闭包每次请求都会调用，
    # 记忆必须挂在闭包外才能跨轮生效
    trimmer = StickyTrimmer()

    def compress(provider_id: str, model: str, payload: dict) -> dict:
        tool_enabled = bool(settings.tool_output_compress_enabled)
        context_enabled = bool(settings.context_compress_enabled)
        if not tool_enabled and not context_enabled:
            # 两个开关都关：**一个计数器都不动**。验收口径是「关 → 计数恒 0
            # 且请求体逐字节不变」，把这种未启用的情况算进 `unchanged` 会让
            # 面板在关闭时显示「白开 N 次」，与事实相反。
            return payload
        original_chars = _string_content_chars(payload.get("messages"))
        triggered = False
        if tool_enabled:
            # tool 输出压缩与窗口无关（省的是 token 不是长度），必须在
            # 「未超限就原样返回」之前做，否则永远等不到触发条件。
            messages = payload.get("messages")
            if isinstance(messages, list):
                squashed = compress_tool_outputs(
                    messages, max_chars=int(settings.tool_output_compress_max_chars))
                if squashed is not messages:
                    before = _string_content_chars(messages)
                    after = _string_content_chars(squashed)
                    # 只在真的压了才记。DEBUG 而非 INFO：长会话里这是**常态**
                    # 而非异常，实测 1 小时内 749 行、占 err.log 的 19%，把真
                    # WARNING 挤出视野；收益要数字时看 `/metrics` 的计数。
                    logger.debug(
                        "tool 输出压缩 provider=%s model=%s messages=%d 字符 %d→%d",
                        provider_id, model, len(messages), before, after)
                    counters._bump(triggers=1, tool_triggers=1)
                    triggered = True
                    payload = {**payload, "messages": squashed}
        window = (context_window_for(cache, aliases, provider_id, model)
                  if context_enabled else None)
        messages = payload.get("messages")
        if window is not None and isinstance(messages, list):
            options = {
                "max_input_tokens": window,
                "reserve_for_output": int(settings.context_compress_reserve_tokens),
                "min_keep_messages": int(settings.context_compress_min_keep_messages),
                "safety_ratio": float(settings.context_compress_safety_ratio),
            }
            if settings.context_compress_sticky_enabled:
                # 粘性裁剪：裁剪边界跨轮保持，前缀不变 → 上游缓存能命中（见
                # engine/sticky_trim.py）。关闭即退回「每轮都按当轮预算重算」的
                # 旧行为（客户端每轮重发完整历史时，命中率只有 system 段）
                outcome = trimmer.compress(
                    conversation_key(provider_id, model, payload), messages,
                    **options)
                compressed, trimmed = outcome.messages, outcome.trimmed
                sticky_reason = outcome.reason
            else:
                compressed = compress_messages(messages, **options)
                trimmed = compressed is not messages
                sticky_reason = "off"
            if compressed is not messages:
                counters._bump(triggers=1, context_trims=1)
                triggered = True
                # 每次裁剪都会让前缀边界前移一条消息 ⇒ 上游前缀缓存只剩
                # system 段。命中率异常时先看这里：同一条流连续多轮都在裁，
                # 就是它把命中打没了。
                if trimmed and settings.prefix_debug_enabled:
                    logger.info(
                        "前缀诊断·压缩 provider=%s model=%s window=%d messages=%d→%d "
                        "估算=%.0f→%.0f sticky=%s reason=%s",
                        provider_id, model, window, len(messages), len(compressed),
                        estimate_messages(messages), estimate_messages(compressed),
                        settings.context_compress_sticky_enabled, sticky_reason)
                # 不原地改 payload：同一请求可能在轮换/回退里再压一次（另一个
                # 渠道、另一个窗口），必须始终从原文出发，且 affinity 指纹读
                # 的是原文
                payload = {**payload, "messages": compressed}
        if triggered:
            # 收益按「整份 messages 的字符串 content 长度差」记：tool 压缩与
            # 上下文裁剪可能叠加，逐段相加会把被裁掉的消息重复扣一次。
            counters._bump(chars_saved=max(
                original_chars - _string_content_chars(payload.get("messages")), 0))
        else:
            # 开关开着但载荷没动（未超阈值 / 未知模型没有窗口）：这是「开了
            # 却没收益」的证据，与 `triggers` 分开记，面板才能区分「省了」与
            # 「白开」。未启用的情况上面已早退，不会进这里。
            counters._bump(unchanged=1)
        return payload
    return compress


def build_prefix_observer(
        settings: Any,
        tracker: PrefixDebugTracker | None = None,
) -> Callable[[str, str, str, dict], None]:
    """构造 `(credential_id, provider_id, model, payload) → None` 诊断闭包。

    开关现读运行态覆盖层（`prefix_debug_enabled`，默认关）：关闭时每请求只
    多一次属性读取，不计算任何摘要。`tracker` 可注入（测试固定 LRU 容量）。
    """
    state = tracker if tracker is not None else PrefixDebugTracker()

    def observe(credential_id: str, provider_id: str, model: str,
                payload: dict) -> None:
        if not settings.prefix_debug_enabled:
            return
        # 上游缓存按账号（凭证）归属，模型不同则模板/参数不同，分开记基准
        report = state.observe(f"{credential_id}\x00{model}", payload)
        logger.info(
            "前缀诊断 provider=%s model=%s cred=%s msgs=%d system=%s tools=%s "
            "tools_changed=%s lcp=%s reusable=%s split=%s",
            provider_id, model, credential_id, report["messages"],
            report["system"], report["tools"], report["tools_changed"],
            report["lcp"], report["reusable"], report["split"])

    return observe
