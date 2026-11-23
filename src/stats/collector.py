"""脱敏统计采集与查询。

纪律（PROPOSAL §6）：不存提示词、回答、请求头、Token、工具参数、原始错误体、会话 ID。
credit 为上游可选字段，两边都经常为 None。
成本（cost_usd / cost_cny）是**估算**：OpenRouter 刊例价 × 写入时的汇率，不是上游
真实扣费；两列同生同灭，匹配不到定价时为 None（界面显示 —）。
"""

from __future__ import annotations

import atexit
import logging
import math
import threading
import time
import uuid
import weakref
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..pricing import PriceTable, estimate_cost_usd, to_cny
from ..request_context import get_api_key_id, get_request_id
from .query import row_speed

logger = logging.getLogger(__name__)

#: 单次事务最多合并的明细行数（P2-15 写放大）。
#:
#: 一条聊天请求原本是「`BEGIN IMMEDIATE` + INSERT + UPSERT + 提交」一整轮，
#: 20 个并发请求就是 20 轮（20 次取写锁、20 次提交）。合并成一次事务后取锁
#: 与提交次数按并发度摊薄，`RetentionTask` 持写锁期间的 `busy_timeout` 撞车
#: 概率同步下降。20 是「一条语句的边际成本」与「队列里最长等待」的折中。
BATCH_MAX_EVENTS = 20

#: 待写队列硬上限。**刻意不用 `deque(maxlen=...)`**：满了静默丢最旧的行，正是
#: 本项要修的「静默丢用量」。到上限时新到的一行改为自己直接写（同样「写成功
#: 才返回」），既保证内存有界，也不丢行。
BATCH_MAX_PENDING = 1000

#: 进程内所有活着的采集器：`atexit` 兜底把它们队列里未落库的明细写完。
#: 用弱引用集合，测试里成百上千个临时实例不会在这里积累。
_live_collectors: weakref.WeakSet[StatsCollector] = weakref.WeakSet()


def _flush_all_collectors() -> None:
    """进程退出兜底：把各采集器队列里剩下的明细落库（P2-15）。

    为什么不是「起一个后台线程按 T 毫秒刷」：`record()` 必须保证
    **read-after-write**（统计页、日配额、实测扣费快照、以及大量测试都是
    「写完立刻读」），所以它是「入队后等本行提交」而不是 fire-and-forget。
    这样一来队列在稳定状态下基本是空的，剩下的唯一窗口是进程退出——`atexit`
    正好覆盖它（`SIGKILL` / 断电覆盖不了，那是 `synchronous=NORMAL` 自己承担
    的同一量级风险）。
    """
    for collector in list(_live_collectors):
        try:
            collector.flush()
        except Exception as error:  # noqa: BLE001 - 退出钩子绝不向外抛
            logger.warning("退出时统计落库失败: %s", error)


atexit.register(_flush_all_collectors)

CONTROLLED_ERROR_TYPES = frozenset({
    # 实际会写入的取值（与 executor / api 层的 error_type 一一对应）：
    #   client_disconnect  — 客户端中途断开（executor.stream）
    #   credential_unavailable — session 失效（_error_type_for(DEAD)）
    #   account_restricted — 账号被上游风控限制（_error_type_for(RESTRICTED)）
    #   invalid_request    — 请求无效（_record_invalid）
    #   model_not_found   — 上游明确说「没有这个模型」（_error_type_for(BLOCKED)）
    #   no_healthy_credential — 无可用凭证（executor 耗尽轮换）
    #   model_unavailable     — 耗尽轮换但候选全部只因**该模型**的模型级
    #                            冷却不可用（_exhaust_error_type；换个模型即可用）
    #   rate_limit         — 权益/额度耗尽（_error_type_for(PLAN/CREDIT/MODEL/
    #                        CONCURRENCY/QUEUE)）或上游限流（SOFT）
    #   context_overflow   — 上下文超限（11115 prompt too long）
    #   model_capability   — 模型不支持该请求形态（CodeArts 001001020 not multimodal）
    #   upstream_timeout   — 链路层瞬态（连接超时 / 读超时 / TLS 中断；只看异常类型）
    #   network_error      — 本机 DNS/连接失败（ConnectError/gaierror，连上游之前）
    #   upstream_error / upstream_protocol — 其它上游失败
    #   upstream_first_event_timeout — 出帧前上游不吐首个数据事件（A1）
    #   upstream_idle_timeout        — 已出帧后上游停止吐数据（A1）
    #   truncated_stream             — 流正常关闭但没走到终态（不冷却凭证，只留痕）
    "client_disconnect", "credential_unavailable",
    "account_restricted",
    "invalid_request", "model_not_found", "no_healthy_credential",
    "model_unavailable", "context_overflow", "model_capability",
    "rate_limit", "upstream_error", "upstream_protocol", "upstream_timeout",
    "network_error",
    "upstream_first_event_timeout", "upstream_idle_timeout",
    "truncated_stream",
})
MAX_MODEL_LENGTH = 64


def _safe_model(model: Any) -> str:
    if not isinstance(model, str) or not model:
        return "unknown"
    if len(model) > MAX_MODEL_LENGTH:
        return "unknown"
    if any(character.isspace() or ord(character) < 0x20 for character in model):
        return "unknown"
    return model


def _normalize_error_type(value: Any) -> str | None:
    return value if isinstance(value, str) and value in CONTROLLED_ERROR_TYPES else None


@dataclass(slots=True)
class UsageEvent:
    id: str
    ts: int
    username: str
    provider: str
    credential_id: str | None
    model: str
    ok: bool
    error_type: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cached_tokens: int | None
    credit: float | None
    credit_estimated: bool
    cost_usd: float | None
    cost_cny: float | None
    latency_ms: int | None
    ttfb_ms: int | None
    request_id: str | None
    api_key_id: str | None


class StatsCollector:
    def __init__(self, db, *,
                 prices: Callable[[], Mapping[str, tuple[float, float, float]]] | None = None,
                 usd_cny_rate: Callable[[], float] | None = None,
                 on_record: Callable[[dict[str, Any]], None] | None = None,
                 on_write: Callable[[], None] | None = None) -> None:
        """`prices` / `usd_cny_rate` 是零参取值器（热更：每写一条现读当前价表
        与汇率）。两者都缺省为 None = 不估算成本（既有测试与老装配保持原行为，
        cost 两列恒为 NULL）。

        `on_record` 是「明细落库后」的回调（装配时接 `LiveEventHub.publish`，
        供 `GET /api/stats/events/stream` 的 SSE 实时流）。回调拿到的是**已脱敏**
        的行字典（与 `/api/stats/events` 同一字段集，另带 rowid），且异常被吞
        ——统计写入绝不能因为一个订阅者出问题而失败。

        `on_write` 是「任何一行落库后」的零参通知（装配时接
        `PaidModelRepository.invalidate`）：实测扣费校准的快照 TTL 是 600s，
        不失效的话「刚观测到上游真扣费」要等 10 分钟才影响选号，而这正是
        目录 x0 数据烧额度的窗口。异常同样吞掉。
        """
        self._db = db
        self._prices = prices
        self._usd_cny_rate = usd_cny_rate
        self._on_record = on_record
        self._on_write = on_write
        # 进程内「失败请求 × 原因」计数（供 /metrics 的 errors_total）。
        # 为什么不落库、不读明细表：① usage_hourly 没有 error_type 维度；
        # ② usage_events 只留 90 天，用它算 `_total` 会让计数器随裁剪**下降**
        #   （Prometheus 视作 reset）；③ scrape 时对明细全表 GROUP BY 正是
        #   /metrics 要避免的开销。进程内计数器随重启归零，Prometheus 原生
        #   把 counter 下降当作 reset 处理，语义自洽。
        self._error_totals: dict[tuple[str, str], int] = {}
        # P2-15 待写队列：`record()` 入队，由「抢到写锁」的那个调用者把队列
        # 一次性写空（并发请求自然合并进同一个事务）。队列项是完整的事件对象
        # （含 id/ts），重试时原样重放，不会重新取 `now` 或 contextvar。
        self._pending: deque[UsageEvent] = deque()
        self._pending_lock = threading.Lock()
        # 串行化「drain + 一个事务」：同一时刻只有一个写者。用 RLock 是因为
        # 提交后的回调（`on_record` / `on_write`）属于外部代码——将来若有订阅者
        # 在回调里再记一条统计，不可重入锁会自锁。
        self._write_lock = threading.RLock()
        # 写失败计数（**行数**，不是事务数）：失败的批会回填队列重试，重试成功
        # 前每次失败都计一次，故它度量「写入失败次数」而不是「丢失行数」。
        # 供 `/metrics` 接线（`write_failures()`）；进程内计数，重启归零。
        self._write_failures = 0
        _live_collectors.add(self)

    def _cost(self, model: str, *, input_tokens: int | None,
              output_tokens: int | None,
              cached_tokens: int | None) -> tuple[float | None, float | None]:
        """按写入时的价表与汇率估算 (cost_usd, cost_cny)；无法定价则 (None, None)。

        估算异常绝不能影响聊天统计：取值器/换算任何抛出都降级为无成本。
        """
        if self._prices is None or self._usd_cny_rate is None:
            return None, None
        try:
            table: PriceTable = dict(self._prices())
            usd = estimate_cost_usd(
                table, model, input_tokens=input_tokens,
                output_tokens=output_tokens, cached_tokens=cached_tokens)
            if usd is None:
                return None, None
            return usd, to_cny(usd, self._usd_cny_rate())
        except Exception as error:  # noqa: BLE001 - 定价失败不影响统计写入
            logger.warning("成本估算失败（model=%s）: %s", model, error)
            return None, None

    def record(
        self,
        *,
        username: str,
        provider: str,
        model: str,
        ok: bool,
        credential_id: str | None = None,
        error_type: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cached_tokens: int | None = None,
        credit: float | None = None,
        credit_estimated: bool = False,
        latency_ms: int | None = None,
        ttfb_ms: int | None = None,
        now: int | None = None,
    ) -> None:
        """写入单条脱敏明细。

        入队后**等本行提交**才返回（read-after-write）：统计页、日配额、实测
        扣费快照都依赖「写完立刻读得到」。并发请求在同一时刻入队时由抢到写锁
        的那个调用者合并成一次事务（见 `_write_pending`）。

        写失败照旧向上抛（调用方 `ExecutorDeps.record` 记录告警），同时累加
        `write_failures` —— 绝不静默丢用量。
        """
        event = self._build_event(
            username=username, provider=provider, model=model, ok=ok,
            credential_id=credential_id, error_type=error_type,
            input_tokens=input_tokens, output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens, cached_tokens=cached_tokens,
            credit=credit, credit_estimated=credit_estimated,
            latency_ms=latency_ms, ttfb_ms=ttfb_ms, now=now,
        )
        if not event.ok and event.error_type is not None:
            key = (event.provider, event.error_type)
            self._error_totals[key] = self._error_totals.get(key, 0) + 1
        self._submit(event)

    def _build_event(
        self,
        *,
        username: str,
        provider: str,
        model: str,
        ok: bool,
        credential_id: str | None = None,
        error_type: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cached_tokens: int | None = None,
        credit: float | None = None,
        credit_estimated: bool = False,
        latency_ms: int | None = None,
        ttfb_ms: int | None = None,
        now: int | None = None,
    ) -> UsageEvent:
        """把一次调用的入参规整成待写事件（脱敏/归一/定成本都在这里）。"""
        safe_model = _safe_model(model)
        cost_usd, cost_cny = self._cost(
            safe_model, input_tokens=_int_or_none(input_tokens),
            output_tokens=_int_or_none(output_tokens),
            cached_tokens=_int_or_none(cached_tokens))
        return UsageEvent(
            id=f"evt_{uuid.uuid4().hex[:16]}",
            ts=int(now if now is not None else time.time()),
            username=username or "unknown",
            provider=provider,
            credential_id=credential_id,
            model=safe_model,
            ok=bool(ok),
            error_type=_normalize_error_type(error_type),
            input_tokens=_int_or_none(input_tokens),
            output_tokens=_int_or_none(output_tokens),
            reasoning_tokens=_int_or_none(reasoning_tokens),
            cached_tokens=_int_or_none(cached_tokens),
            credit=_float_or_none(credit),
            # 只有确实记下 credit 时才可能标推算：credit 为 None 的推算标记无意义
            credit_estimated=bool(credit_estimated) and _float_or_none(credit) is not None,
            cost_usd=cost_usd,
            cost_cny=cost_cny,
            latency_ms=_int_or_none(latency_ms),
            ttfb_ms=_int_or_none(ttfb_ms),
            # 请求 ID 从 contextvar 现读（中间件在请求入口设置）：调用链跨引擎层，
            # 不为一个可观测性字段给所有中间层加参数。后台任务/脚本无请求上下文
            # → None（该列可空）。
            request_id=get_request_id(),
            # Key 归属同上：由鉴权层写 contextvar，后台任务/管理台内部请求
            # 无 Key 归属 → None（该列可空，语义=未归属）。
            api_key_id=get_api_key_id(),
        )

    def _submit(self, event: UsageEvent) -> None:
        """入队并等本行落库；队列到上限时自己直接写（不丢行）。"""
        with self._pending_lock:
            queued = len(self._pending) < BATCH_MAX_PENDING
            if queued:
                self._pending.append(event)
        if not queued:
            # 积压到硬上限：不再排进队列（否则内存无界），也不丢——自己开一个
            # 事务写这一行。这条路径只在写者长时间卡在 `busy_timeout` 时出现；
            # 仍走写锁（与正常批次互斥，避免两个线程同时抢 SQLite 写锁空转），
            # 失败同样计数、同样向上抛。
            self._write_inline([event])
            return
        self._settle()

    def _write_inline(self, batch: list[UsageEvent]) -> None:
        """兜底写一批（不经队列）：失败计数后原样抛出。"""
        with self._write_lock:
            try:
                self._write_batch(batch)
            except BaseException:
                with self._pending_lock:
                    self._write_failures += len(batch)
                raise

    def _settle(self) -> None:
        """等队列写空；本行可能由别的写者的批次一起提交。"""
        with self._write_lock:
            # 抢到写锁就意味着「此刻没有别的写者」，于是队列里剩下的（含本行）
            # 都由本次 `_write_pending` 写掉。若排队期间另一个写者已经把整批
            # 连同本行提交了，这里看到的是空队列，直接返回。
            self._write_pending()

    def _write_pending(self) -> int:
        """把队列里的行按 `BATCH_MAX_EVENTS` 分批写空，返回本次写入的行数。

        失败时把这一批**回填队首**并累加 `write_failures` 后原样抛出：回填让
        等待中的调用者在拿到写锁后重试（可能这次就成功了），失败本身向上抛给
        `ExecutorDeps.record` 记录，不会静默。
        """
        written = 0
        while True:
            with self._pending_lock:
                if not self._pending:
                    return written
                size = min(len(self._pending), BATCH_MAX_EVENTS)
                batch = [self._pending.popleft() for _ in range(size)]
            try:
                self._write_batch(batch)
            except BaseException:
                with self._pending_lock:
                    self._pending.extendleft(reversed(batch))
                    self._write_failures += len(batch)
                raise
            written += len(batch)

    def _write_batch(self, batch: list[UsageEvent]) -> None:
        """一个事务写入一批明细 + 增量小时汇总，提交后逐行广播。"""
        written: list[tuple[int | None, UsageEvent]] = []
        with self._db.transaction() as conn:
            for event in batch:
                cursor = conn.execute(
                    "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, "
                    "ok, error_type, input_tokens, output_tokens, reasoning_tokens, cached_tokens, "
                    "credit, credit_estimated, cost_usd, cost_cny, latency_ms, ttfb_ms, "
                    "request_id, api_key_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event.id, event.ts, event.username, event.provider, event.credential_id,
                     event.model, int(event.ok), event.error_type, event.input_tokens,
                     event.output_tokens, event.reasoning_tokens, event.cached_tokens,
                     event.credit, int(event.credit_estimated), event.cost_usd, event.cost_cny,
                     event.latency_ms, event.ttfb_ms, event.request_id, event.api_key_id),
                )
                # 当前小时增量累加：总览/图表都读小时表，不能等 5 分钟一轮的
                # retention rollup 才可见（否则刚发生的请求统计页面显示 0）。
                # 增量累加与全量重算等价：rollup 后续会把这一小时算成同样的值。
                self._bump_hourly(conn, event)
                # 事务内拿 rowid，提交后再广播：订阅者拿到的是「已落库」的行，
                # 与随后刷新 `/api/stats/events` 看到的是同一条（含 rowid 游标）。
                written.append((cursor.lastrowid, event))
        # 快照失效每批一次即可：它查询的是明细表，一批里任何一行都足以让
        # 「刚观测到的真扣费」立刻参与选号。
        self._invalidate_paid_snapshot()
        for rowid, event in written:
            self._notify(rowid, event)

    def flush(self) -> int:
        """把队列里尚未落库的明细写掉，返回本次写入的行数。

        稳定状态下队列是空的（`record()` 等本行提交），这个方法覆盖两个场景：
        进程退出（`atexit` 兜底）与测试里需要「把残留的批次刷干净」。
        写失败照旧抛给调用方（`atexit` 钩子自己吞并告警）。
        """
        with self._write_lock:
            return self._write_pending()

    def write_failures(self) -> int:
        """进程内统计写入失败的行数（供 `/metrics` 接线，只读）。

        只提供 getter 是因为 `/metrics` 的装配点不在本模块所有权内；主控接线时
        渲染成 `coding2api_stats_write_failures_total` 即可。语义与
        `error_totals()` 一致：进程内计数，Prometheus 把下降当 reset。
        """
        with self._pending_lock:
            return self._write_failures

    def _invalidate_paid_snapshot(self) -> None:
        """通知「实测扣费」快照作废（未装配 / 出问题都不影响写入）。

        放在事务提交之后：快照查询要读到这一行才有意义。
        """
        if self._on_write is None:
            return
        try:
            self._on_write()
        except Exception as error:  # noqa: BLE001 - 快照失效失败不能影响统计写入
            logger.warning("实测扣费快照失效失败: %s", error)

    def _notify(self, rowid: int | None, event: UsageEvent) -> None:
        """把刚写入的明细推给实时流订阅者（未装配 / 出问题都不影响写入）。

        广播在**事务提交之后**：订阅者收到就能立刻用 rowid 接着翻页，不会
        看到一条回滚掉的记录。回调异常一律吞掉——统计是旁路，绝不能让它
        把聊天响应搞失败。
        """
        if self._on_record is None:
            return
        # `tps` / `tps_visible` 是派生列（库里不存）：实时流行在广播点就地算，
        # 规则与 /api/stats/events 的 SQL 列同源（`row_speed`）。
        visible_tps, total_tps = row_speed(
            ok=event.ok, output_tokens=event.output_tokens,
            reasoning_tokens=event.reasoning_tokens,
            latency_ms=event.latency_ms, ttfb_ms=event.ttfb_ms)
        payload = {
            "rowid": rowid,
            "id": event.id,
            "ts": event.ts,
            "username": event.username,
            "provider": event.provider,
            "model": event.model,
            "credential_id": event.credential_id,
            "ok": int(event.ok),
            "error_type": event.error_type,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "reasoning_tokens": event.reasoning_tokens,
            "cached_tokens": event.cached_tokens,
            "credit": event.credit,
            "credit_estimated": int(event.credit_estimated),
            "cost_usd": event.cost_usd,
            "cost_cny": event.cost_cny,
            "latency_ms": event.latency_ms,
            "ttfb_ms": event.ttfb_ms,
            # 逐请求速度（派生列，见 `_notify` 开头）：与明细端点同规则
            "tps": total_tps,
            "tps_visible": visible_tps,
            "request_id": event.request_id,
        }
        try:
            self._on_record(payload)
        except Exception as error:  # noqa: BLE001 - 实时流失败不影响统计写入
            logger.warning("实时事件广播失败: %s", error)

    @staticmethod
    def _bump_hourly(conn, event: UsageEvent) -> None:
        """把一条明细增量累加进所属小时行（同 key 则累加，不存在则插入）。"""
        conn.execute(
            """
            INSERT INTO usage_hourly (hour_utc, username, provider, model, requests, ok_count,
                                      input_tokens, output_tokens, reasoning_tokens,
                                      cached_tokens, cached_known,
                                      credit_sum, credit_known, credit_estimated_known,
                                      cost_usd_sum, cost_cny_sum, cost_known,
                                      latency_sum, ttfb_sum)
            VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(hour_utc, username, provider, model) DO UPDATE SET
                requests = requests + 1,
                ok_count = ok_count + excluded.ok_count,
                input_tokens = input_tokens + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
                cached_tokens = cached_tokens + excluded.cached_tokens,
                cached_known = cached_known + excluded.cached_known,
                credit_sum = COALESCE(credit_sum, 0) + excluded.credit_sum,
                credit_known = credit_known + excluded.credit_known,
                credit_estimated_known = credit_estimated_known + excluded.credit_estimated_known,
                cost_usd_sum = COALESCE(cost_usd_sum, 0) + excluded.cost_usd_sum,
                cost_cny_sum = COALESCE(cost_cny_sum, 0) + excluded.cost_cny_sum,
                cost_known = cost_known + excluded.cost_known,
                latency_sum = latency_sum + excluded.latency_sum,
                ttfb_sum = ttfb_sum + excluded.ttfb_sum
            """,
            # NULL 一律折成 0 参与累加：列定义是 NOT NULL DEFAULT 0，
            # `x + NULL` 会交出 NULL，把整行污染掉
            ((event.ts // 3600) * 3600, event.username, event.provider, event.model,
             int(event.ok),
             event.input_tokens or 0, event.output_tokens or 0,
             event.reasoning_tokens or 0,
             event.cached_tokens or 0,
             0 if event.cached_tokens is None else 1,
             event.credit or 0.0,
             0 if event.credit is None else 1,
             1 if event.credit_estimated else 0,
             event.cost_usd or 0.0,
             event.cost_cny or 0.0,
             0 if event.cost_usd is None else 1,
             (event.latency_ms or 0) if event.ok else 0,
             (event.ttfb_ms or 0) if event.ok else 0),
        )

    def window_error_rate(self, *, since: int) -> tuple[int, int]:
        """近窗口内的 (请求数, 失败数)，供上游错误率告警。

        读明细表 `usage_events` 而不是小时汇总：告警窗是分钟级（默认 15 分钟），
        小时粒度要么整点才更新、要么跨小时口径错乱。明细只保留 90 天，而告警
        只看最近几十分钟，不受保留期影响。

        `failed` **排除** `client_disconnect`：那是客户端自己关掉页面/连接，
        与上游健康无关；算进去会让「用户随手关页面」触发上游错误率告警
        （文案明确写的是「上游错误率」）。比较用 `COALESCE(error_type,'')`
        而不是 `<>`：`<>` 对 NULL 求值为 NULL，成功行（error_type 为 NULL，
        但 ok=1）与「失败但无 error_type」的历史行都会被漏掉。

        分母仍是**全部请求**（含 client_disconnect）：告警问的是「这窗里有多少
        比例的请求因上游失败掉」，中断请求属于被观测的总体，把它从分母剔掉
        会让样本量凭空变小、更容易被少数上游错误顶过阈值。
        """
        row = self._db.connect().execute(
            "SELECT COUNT(*) AS requests, "
            "COALESCE(SUM(CASE WHEN ok = 0 "
            "AND COALESCE(error_type, '') <> 'client_disconnect' "
            "THEN 1 ELSE 0 END), 0) AS failed "
            "FROM usage_events WHERE ts >= ?", (since,)).fetchone()
        return int(row["requests"] or 0), int(row["failed"] or 0)

    def window_dead_channels(self, *, since: int,
                            min_requests: int) -> list[dict[str, Any]]:
        """近窗口内「有流量但一次都没成功」的渠道，供渠道死亡告警。

        判据是**零成功**而不是错误率：officeace 2026-11-08 积分耗尽后回的是
        流内 401，请求记录 `ok=0`；同类故障也可能整批走 fallback 而完全不落在
        本渠道的 `ok=1` 上。按错误率算会被 `failed/requests` 的高阈值稀释，
        而「这个渠道在当前窗口内一次都没成功」是一个不会误判的信号。

        `min_requests` 过滤掉样本过少的渠道（一次失败就报警等于噪声）。返回
        `[{provider, requests, last_ok}]`，`last_ok` 供文案说明「多久没成功」。
        """
        rows = self._db.connect().execute(
            "SELECT provider, COUNT(*) AS requests, "
            "COALESCE(SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END), 0) AS ok_count, "
            "MAX(CASE WHEN ok = 1 THEN ts END) AS last_ok "
            "FROM usage_events WHERE ts >= ? GROUP BY provider",
            (since,)).fetchall()
        return [
            {"provider": row["provider"], "requests": int(row["requests"] or 0),
             "last_ok": int(row["last_ok"]) if row["last_ok"] is not None else None}
            for row in rows
            if int(row["requests"] or 0) >= max(1, min_requests)
            and int(row["ok_count"] or 0) == 0
        ]

    def window_key_usage(self, *, since: int,
                         limit: int = 50) -> list[dict[str, Any]]:
        """近窗口内按 API Key 聚合的用量（token 降序），供「单 Key 暴涨」告警。

        只统计带 `api_key_id` 的明细：Cookie 会话（Playground）没有 Key，
        按 Key 告警对它们无意义。`tokens` 是输入+输出之和，缺字段按 0 计
        （上游未上报时不给假数据，0 就是 0）。`limit` 只捞最重的几个 Key——
        告警不需要全量列表，全量在按 Key 统计页。
        """
        rows = self._db.connect().execute(
            "SELECT api_key_id, username, COUNT(*) AS requests, "
            "COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0)"
            " AS tokens "
            "FROM usage_events WHERE ts >= ? AND api_key_id IS NOT NULL "
            "GROUP BY api_key_id, username ORDER BY tokens DESC LIMIT ?",
            (since, max(1, limit))).fetchall()
        return [{"api_key_id": row["api_key_id"], "username": row["username"],
                 "requests": int(row["requests"] or 0),
                 "tokens": int(row["tokens"] or 0)} for row in rows]

    def window_cost(self, *, since: int) -> tuple[float, int, int]:
        """近窗口成本（人民币）：`(已花费, 可定价小时数, 总小时数)`。

        读小时汇总（永久保留，与统计页同口径）。后两项给告警文案用：成本只
        覆盖「匹配到 OpenRouter 刊例价 + 当时汇率」的明细，未定价模型是 0 贡献，
        覆盖率低时预算告警会偏乐观——detail 里写明覆盖情况，不假装准确。
        """
        row = self._db.connect().execute(
            "SELECT COALESCE(SUM(cost_cny_sum), 0) AS spent, "
            "COALESCE(SUM(CASE WHEN cost_known > 0 THEN 1 ELSE 0 END), 0) AS known, "
            "COUNT(*) AS hours "
            "FROM usage_hourly WHERE hour_utc >= ?", (since,)).fetchone()
        return (float(row["spent"] or 0), int(row["known"] or 0),
                int(row["hours"] or 0))

    def error_totals(self) -> list[tuple[str, str, int]]:
        """进程内「渠道 × 失败原因」累计计数（稳定排序）。

        只统计**受控**错误类型（`CONTROLLED_ERROR_TYPES`，`error_type` 为
        None 的失败不计入）。返回 (provider, kind, count)，供 `/metrics`
        渲染 `coding2api_upstream_errors_total{provider,kind}`。
        """
        return sorted((provider, kind, count)
                      for (provider, kind), count in self._error_totals.items())

    def purge_expired(self, retention_days: int = 90, now: int | None = None) -> int:
        """明细保留 90 天；小时汇总永久（PROPOSAL §6）。

        切点向上对齐到小时边界：只删**整个小时都已过期**的明细。
        这不是保守取值而是正确性要求——`rollup_hourly` 对整行用 REPLACE
        语义，只有保证「仍有明细的小时保有全部明细」，重算才精确；
        若把边界小时只删一半，下一轮 rollup 会把汇总行覆盖为剩下的那一半，
        永久丢掉被删部分（已过期明细无法再从明细找回）。
        代价：明细最多多留 1 小时。
        """
        raw_cutoff = int(now if now is not None else time.time()) - retention_days * 86400
        cutoff = (raw_cutoff // 3600 + 1) * 3600
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM usage_events WHERE ts < ?", (cutoff,))
        return cursor.rowcount

    def rollup_hourly(self, since: int | None = None) -> int:
        """把明细汇总进小时表（幂等 upsert）。

        默认汇总全部明细：小时汇总要永久保留，不能因为「只看最近 N 小时」
        而丢掉历史数据。需要增量时由调用方显式传 since。

        整行 REPLACE 语义要求「有明细的小时保有全部明细」——
        `purge_expired` 按整小时删除来保这个前提（见其 docstring）。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO usage_hourly (hour_utc, username, provider, model, requests, ok_count,
                                          input_tokens, output_tokens, reasoning_tokens,
                                          cached_tokens, cached_known,
                                          credit_sum, credit_known, credit_estimated_known,
                                          cost_usd_sum, cost_cny_sum, cost_known,
                                          latency_sum, ttfb_sum)
                SELECT (ts / 3600) * 3600 AS hour_utc, username, provider, model,
                       COUNT(*), SUM(ok),
                       COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(reasoning_tokens), 0),
                       COALESCE(SUM(cached_tokens), 0),
                       SUM(CASE WHEN cached_tokens IS NULL THEN 0 ELSE 1 END),
                       COALESCE(SUM(credit), 0), SUM(CASE WHEN credit IS NULL THEN 0 ELSE 1 END),
                       SUM(CASE WHEN credit_estimated = 1 AND credit IS NOT NULL THEN 1 ELSE 0 END),
                       COALESCE(SUM(cost_usd), 0), COALESCE(SUM(cost_cny), 0),
                       SUM(CASE WHEN cost_usd IS NULL THEN 0 ELSE 1 END),
                       COALESCE(SUM(CASE WHEN ok = 1 THEN latency_ms END), 0),
                       COALESCE(SUM(CASE WHEN ok = 1 THEN ttfb_ms END), 0)
                FROM usage_events WHERE (? IS NULL OR ts >= ?)
                GROUP BY hour_utc, username, provider, model
                ON CONFLICT(hour_utc, username, provider, model) DO UPDATE SET
                    requests = excluded.requests,
                    ok_count = excluded.ok_count,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    reasoning_tokens = excluded.reasoning_tokens,
                    cached_tokens = excluded.cached_tokens,
                    cached_known = excluded.cached_known,
                    credit_sum = excluded.credit_sum,
                    credit_known = excluded.credit_known,
                    credit_estimated_known = excluded.credit_estimated_known,
                    cost_usd_sum = excluded.cost_usd_sum,
                    cost_cny_sum = excluded.cost_cny_sum,
                    cost_known = excluded.cost_known,
                    latency_sum = excluded.latency_sum,
                    ttfb_sum = excluded.ttfb_sum
                """, (since, since))
        return cursor.rowcount


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    # 拒绝 NaN/Inf：它们会进 SQLite 聚合，合法行一旦被污染就再也回不来（L1）。
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return numeric

