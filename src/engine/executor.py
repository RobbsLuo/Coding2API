"""执行引擎：选号 → 上游流 → 轮换重试 → 统计（轮换 ≤3 次）。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Any, Protocol

import httpx

from ..compat.openai.errors import UpstreamStreamError, stream_error_frame
from ..compat.openai.request import ChatRequest, InvalidRequest
from ..compat.openai.response import (
    StreamTranslator,
    aggregate,
)
from ..config import live
from ..db.repo import CredentialRepository
from ..provider.base import (
    QUEUE_DEFAULT_WAIT_SECONDS,
    QUEUE_MAX_ROUNDS,
    QUEUE_MAX_WAIT_SECONDS,
    ErrKind,
    Event,
    EventKind,
    Usage,
    aclose_stream,
    event_error_kind,
)
from .continuation import ContinuationStream
from .model_resolver import (
    ModelTarget,
    UnknownModelError,
    ordered_fallback_chain,
    resolve,
)
from .reasoning import map_effort, parse_effort_map
from .scheduler import Scheduler

logger = logging.getLogger(__name__)


class StreamSink(Protocol):
    """出口形状的注入缝：把中立 `Event` 翻成某个协议的 SSE 帧。

    默认实现是 chat 出口的 `StreamTranslator`；`/v1/responses` 注入自己的
    实现，从而复用同一套选号 / 轮换 / 记账（见 TECHNICAL §3.7）。
    """

    usage: Usage | None
    done_sent: bool

    def translate(self, event: Event) -> Iterator[bytes]: ...

    def finish(self) -> Iterator[bytes]: ...

    def error_frame(self, message: str, code: str) -> bytes: ...

    def keepalive(self) -> bytes: ...


class NoHealthyCredential(Exception):
    pass


class NoProviderForModel(Exception):
    pass


class _ModelExhausted(Exception):
    """流式当前模型在**产出任何响应帧之前**就用尽（回退链内部信号，不对外）。

    带上收尾文案与错误码，供整条回退链都失败时用最后一个链项的 translator
    产出终帧；避免在还有回退项时就把错误帧写给客户端。
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class _AggregateOverflow(Exception):
    """非流式聚合事件数超上限：上游持续吐 chunk 而不结束。

    内部信号，按瞬态错误处理（换号重试），不对外暴露。
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"aggregate exceeded {limit} events")
        self.limit = limit


class _Queued(Exception):
    """上游排队中（`ErrKind.QUEUE`）：内部信号，等 `wait` 秒后原号重开。

    用异常而不是 `break`+标志位，是因为非流式的读流包在 `try/except/else` 里，
    `break` 出来会落到 `else` 分支把**半截** events 拿去聚合。异常能干净地
    跳过 `else`，走 finally 释放租约后再等待。

    携带完整的 `plan`（而不只是秒数）：计数与 pinned 在抛出点已经算好，
    `except` 分支直接落账，避免两处各算一遍而漂移。
    """

    def __init__(self, plan: _QueueRestart) -> None:
        super().__init__(f"upstream queued {plan.wait}s")
        self.plan = plan


@dataclass(slots=True)
class _QueueRestart:
    """一次排队重开的计划（四个入口共用，见 `_plan_queue_restart`）。"""

    wait: float
    rounds: int
    attempts: int
    pinned: tuple[str, dict[str, Any]]


class _StreamTimeout(Exception):
    """流式取下一事件的计时超时（A1）。

    `phase` 区分两种语义完全不同的超时：

    - `first_event`：还没向客户端出过帧。此时切换凭证是安全的（客户端没
      收到任何内容），按 SOFT 冷却后继续换号。
    - `idle`：已经出过帧。半截输出不可回滚，换号会把两个账号的内容拼进
      同一条回复，只能以错误帧收尾。

    `kind()` 让通用的 `_classify` 也能识别（SOFT：短冷却、不累计），
    `error_code` 供 `_error_summary` 生成受控的对外标识。
    """

    def __init__(self, seconds: float, phase: str) -> None:
        super().__init__(f"upstream {phase} timed out after {seconds:g}s")
        self.seconds = seconds
        self.phase = phase
        # 统计口径（collector 白名单）与对外受控标识（`_error_summary`）
        self.error_code = f"upstream_{phase}_timeout"
        self.summary = f"upstream {phase.replace('_', '-')} timeout"

    def kind(self) -> ErrKind:
        return ErrKind.SOFT


class _TimedReader:
    """带超时地取下一个上游事件：**可复用 Task**，超时绝不取消上游。

    不能用 `asyncio.wait_for(anext(gen))`：超时会 cancel 掉 `anext` 协程，
    取消穿透进上游生成器并把它关闭——计时超时反而变成断流（`with_keepalive`
    的 docstring 已踩过这个坑）。这里用 `asyncio.wait` 与超时竞争，超时后
    `_pending` 原样保留，下一次 `next()` 继续等同一个 Task。
    """

    def __init__(self, iterator: Any) -> None:
        self._iterator = iterator
        self._pending: asyncio.Task[Event] | None = None

    async def next(self, timeout: float | None) -> Event:
        """取下一个事件；`timeout=None` 表示不设上限（关闭超时）。"""
        if self._pending is None:
            self._pending = asyncio.ensure_future(anext(self._iterator))
        if timeout is not None:
            done, _ = await asyncio.wait({self._pending}, timeout=timeout)
            if not done:
                # 保留 pending：上游可能只是慢，稍后还会吐；取消它就断流了
                raise TimeoutError
        try:
            return await self._pending
        finally:
            self._pending = None

    async def aclose(self) -> None:
        """关闭前清掉在途任务（避免 "Task was destroyed" 告警）。"""
        pending, self._pending = self._pending, None
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@dataclass(slots=True)
class ExecutorDeps:
    """注入点：provider 客户端、仓储、调度器、统计采集。"""

    providers: dict[str, Any]           # provider_id → 带 stream_chat 的客户端适配器
    credentials: CredentialRepository
    scheduler: Scheduler
    default_model: str = "glm-5.2"
    stats: Any | None = None            # StatsCollector；None 表示不采集（测试可用）
    upstream_model_name: Any | None = None  # (provider_id, 归一名) → 上游原始名；None 则原样传
    model_suggestions: Any | None = None    # (模型名) → 相近可用模型列表；None 则不给建议
    # provider → {小写模型名: 上游原始 id}；api/models.list_models 拉取后就地更新。
    # 用于把独有模型的候选上游收窄到真正登记了它的上游，避免白打一次请求
    model_aliases: dict[str, dict[str, str]] | None = None
    # provider → {小写原始 id: Model}：模型目录原始表（api/models 的进程内缓存）。
    # 与 model_aliases 一起构成「对外 id → 渠道 → 倍率」查询，供调度器的
    # 免费优先档（候选中存在 x0 渠道时优先选它）注入 credit_rate；None / 空
    # 表示目录未就绪，倍率未知，调度退回原排序（向后兼容）。
    model_list_cache: dict[str, dict[str, Any]] | None = None
    # 会话粘性（ConversationAffinity）；None 表示关闭。对话进行中固定用原
    # 凭证，出错才轮换，成功后重新粘定实际服务的凭证
    affinity: Any | None = None
    # 截断续写上限：上游 finish_reason=length 时同凭证续写，最多该次数；
    # 0 关闭。续写在事件流包装器内完成，不参与凭证轮换
    max_auto_continues: int = 0
    # 非流式聚合整体超时（秒）：流式路径有心跳与 read=None 兜底，非流式
    # 聚合没有——上游连接半开停滞会让请求无限悬挂并占住凭证。超时按瞬态
    # 错误（SOFT：短冷却不累计）换号重试；≤0 关闭
    complete_timeout_seconds: float = 600
    # 非流式聚合的事件条数上限：上游若持续吐 chunk（超长回答 / 死循环
    # 输出）而不结束，`events` 会无界增长直到 OOM——超时只兜住「慢」，兜不住
    # 「快而无穷」。超过即按瞬态错误换号重试；≤0 关闭
    complete_max_events: int = 200_000
    # 流式首事件超时（秒）：接了连接却迟迟不吐第一个**数据事件**时，在出帧前
    # 换号（此时客户端还没收到任何内容，切换安全）。注释帧/心跳不算数据，
    # 不重置计时。≤0 关闭（默认）。标量或零参 callable（热更），见 config.live
    first_event_timeout_seconds: Any = 0
    # 流式空闲超时（秒）：**已出帧**后上游长时间不再吐数据，只能以错误帧收尾
    # （半截输出不可回滚）。≤0 关闭（默认）。标量或零参 callable（热更）
    stream_idle_timeout_seconds: Any = 0
    # 短冷却「等待续上」（秒）：全池都在短冷却、没有任何立即可用候选时，若
    # 最短的剩余冷却 ≤ 该值则等它到期后重取一次（而不是直接 503）。等待期间
    # 不占在途租约，也不消耗轮换预算。≤0 关闭（默认）。标量或零参 callable
    short_cooldown_wait_seconds: Any = 0
    # 跨渠道 fallback 兼容组：配置文本 → 组的解析值（dict）；None 表示
    # 关闭。零参 callable 时每次请求现读（热更）。空 dict 与 None 等价（无组）。
    fallback_groups: Any | None = None
    # 上下文压缩（修订）：选号后按**实际服务渠道**的输入窗口压缩请求体。
    # (provider_id, 归一名, payload) → payload；None 表示不压缩。契约：实现
    # 必须返回新对象或原样返回，不得原地改入参——同一请求可能在轮换/回退里
    # 对不同渠道各压一次，且 affinity 指纹读的是未压缩原文
    context_compress: Any | None = None
    # 凭证级限额护栏（src/engine/limits.py 的 LimitGuard）：选号前过滤掉
    # 「当日用量/余额触顶」的候选；None 表示不启用（向后兼容）。
    limits: Any | None = None
    # 凭证在途租约（src/engine/inflight.py 的 InflightGuard）：选号前过滤掉
    # 「并发已占满」的候选，选中后登记、请求结束（含断开/异常）释放；
    # 在途计数同时注入候选快照供调度器做「最少连接」负载均衡。None 表示
    # 不启用（向后兼容，此时无过滤也无负载键）。
    inflight: Any | None = None
    # 出站剥离思考内容：真值 → 丢掉 `EventKind.REASONING` 事件（三个出口
    # 一致生效，流式/非流式都覆盖）。**只过滤响应、不改请求体**：上游仍照常
    # 思考（reasoning_effort 原样透传），只是不交给客户端。零参 callable 时
    # 每次请求现读（热更）；默认关。
    force_disable_thinking: Any = False
    # 出站 `reasoning_effort` 档位映射：配置文本 → `{渠道: {源: 目标}}`。
    # 只改写客户端**显式**给的档位（渠道自身的 setdefault 补值不动）；目标
    # `none` 表示删除该键。None / 空文本 = 原样透传。零参 callable 时每次
    # 请求现读（热更）。解析见 src/engine/reasoning.py。
    reasoning_effort_map: Any | None = None

    def record(self, **fields: Any) -> None:
        """统计写入失败绝不能影响聊天响应。"""
        if self.stats is None:
            return
        try:
            self.stats.record(**fields)
        except Exception as error:  # noqa: BLE001
            logger.warning("usage stats write failed: %s", error)


class Executor:
    def __init__(self, deps: ExecutorDeps) -> None:
        self._deps = deps
        # 流式超时的时钟：默认真实单调钟，测试可换成假时钟以精确断言
        # 「只有数据事件重置计时」而不必真的等满窗口（见 test_executor_stream_timeout）
        self._clock = time.monotonic

    def _record_invalid(self, username: str, provider_id: str, credential_id: str,
                        model: str, started: float, error: object) -> None:
        """请求无效：只记统计（invalid_request），绝不冷却/禁用凭证。"""
        self._deps.record(
            username=username, provider=provider_id, credential_id=credential_id,
            model=model, ok=False, error_type="invalid_request",
            latency_ms=_elapsed_ms(started))

    def _suggestions(self, model: str) -> list[str] | None:
        """400 时给用户的相近模型建议（未注入或无候选则 None）。"""
        if self._deps.model_suggestions is None:
            return None
        try:
            return self._deps.model_suggestions(model) or None
        except Exception:  # noqa: BLE001 - 建议失败不影响主错误
            return None

    def _unavailable_text(self, target: ModelTarget, last_error: Exception | None) -> str:
        """耗尽轮换后的 503 文案。

        若该模型在所有候选凭证上都处于**模型级**冷却（上游限流或上游节点
        故障，如 Qoder 的 `[FAIL]node:…Execution failed`），给出「模型暂时
        不可用」的文案而不是误导性的「凭证不可用」——账号本身是好的，换个
        模型立即可用。
        """
        if self._all_model_cooled(target):
            return _model_cooled_message(target.model, last_error,
                                         self._suggestions(target.model))
        return _unavailable_message(last_error)

    def _exhaust_error_type(self, target: ModelTarget) -> str:
        """轮换耗尽时落统计的受控 error_type（与 503 文案分流同源）。

        所有候选都只因**该模型的模型级冷却**被排除时（`_all_model_cooled`，
        与 `_unavailable_text` 同一判据）记 `model_unavailable`，否则记
        `no_healthy_credential`——统计页此前一律显示「无可用凭证」，把
        「换个模型就好」的上游限流误读成整池凭证挂了（实测 2026-10-13
        `ling-3.1-flash`：kilo 唯一凭证被 Novita 429 写模型级冷却后，
        明细每条都标「无可用凭证」）。对外错误码不变（仍
        `no_healthy_credential`，兼容层映射依赖它）。
        """
        return ("model_unavailable" if self._all_model_cooled(target)
                else "no_healthy_credential")

    def _model_cooled_only(self, providers: Iterable[str],
                           target: ModelTarget, now: int) -> bool:
        """这些上游的凭证是否**全部**只因该模型的模型级冷却而不可选。

        只认模型级冷却：账号级冷却 / 硬禁用 / 暂停属于「凭证不可用」，不能
        冒充模型故障——那种情况下换个模型也未必能用，文案不能指向「换个
        模型即可」。候选集为空（该上游一条凭证都没有）同样不算：那是
        目录 / 凭证侧的问题，不是上游在限流。仅用于耗尽时的 503 文案
        分流（见 `_unavailable_text`）。
        """
        candidates = self._deps.credentials.candidates(providers)
        if not candidates:
            return False
        for candidate in candidates:
            scope = self._model_scope(candidate.provider, target.model)
            if candidate.is_selectable(now, scope):
                return False                        # 还有可用候选（只是都试过了）
            if not candidate.is_selectable(now):    # 账号级原因 → 非模型故障
                return False
        return True

    def _all_model_cooled(self, target: ModelTarget) -> bool:
        """候选凭证是否**全部**因该模型的模型级冷却而不可选（账号本身可选）。

        只认模型级冷却：账号级冷却/禁用属于「凭证不可用」，不能冒充模型故障。
        用于把两种耗尽的文案区分开（见 `_unavailable_text`），也用于决定
        「收窄后无可用候选」时**是否**该放宽重试（见 `_select`）。

        调用点在 `_pick` 返回 None 之后，此时 `_select` 已保证至少注册了一个
        上游（全无注册时它会先抛 NoProviderForModel），故无需再判空上游。
        """
        return self._model_cooled_only(self._registered(target), target,
                                       int(time.time()))

    def _skip_provider(self, provider_id: str, tried: set[str]) -> None:
        """INVALID 后跳过该上游：把它的全部凭证都标记为已试。"""
        for candidate in self._deps.credentials.candidates([provider_id]):
            tried.add(candidate.credential_id)

    def _upstream_model(self, provider_id: str, model: str) -> str:
        """归一模型名 → 该上游注册的原始 id（大小写变体映射，未知则原样）。"""
        if self._deps.upstream_model_name is None:
            return model
        return self._deps.upstream_model_name(provider_id, model)

    def resolve_target(self, request: ChatRequest,
                       provider_binding: str | None = None) -> ModelTarget:
        target = resolve(request.model, live(self._deps.default_model)())
        return self._apply_binding(target, provider_binding)

    def _fallback_chain(self, target: ModelTarget) -> tuple[ModelTarget, ...]:
        """请求模型的回退链；未配置或未命中任何组时只有它自己。

        兼容组 `fast=glm-4.6,glm-5`：组名只是入口别名（不进链），成员
        `glm-4.6`/`glm-5` 才是候选模型。`@渠道` 强制指定与 API Key 渠道绑定
        都表示「用户已把渠道钉死」，不参与跨渠道回退，直接返回原目标。

        目录过滤（「兼容组白名单」，避免给不存在的模型白打一次上游）：目录可用
        时剔除不在任何候选渠道登记的回退成员；目录未就绪时全部放行，交给执行层
        兜底候选（与 `_apply_binding` 同方针）。链首若就是用户请求的那个模型，
        无论目录是否认识都保留（那是明确的用户意图）。成员按名去重。
        """
        if target.forced:
            return (target,)
        raw = self._deps.fallback_groups
        groups = live(raw)() if raw is not None else {}
        if not groups:
            return (target,)
        chain = ordered_fallback_chain(target.model, groups)
        if not chain:
            return (target,)
        resolved: list[ModelTarget] = []
        seen: set[str] = set()
        for name in chain:
            # 链首若是原请求模型，沿用已解析的 target
            if name.lower() == target.model.lower():
                member = target
            else:
                try:
                    member = resolve(name, name)
                except UnknownModelError:
                    logger.warning("兼容组成员 %r 含未知渠道，已跳过", name)
                    continue
            key = member.model.lower()
            if key in seen:
                continue
            seen.add(key)
            resolved.append(member)
        if not resolved:
            return (target,)
        head_is_requested = resolved[0].model.lower() == target.model.lower()
        head = resolved[0] if head_is_requested else None
        rest = resolved[1:] if head_is_requested else resolved
        filtered = [member for member in rest if self._fallback_allowed(member)]
        if head is not None:
            return (head, *filtered)
        return tuple(filtered) or (target,)

    @staticmethod
    def _with_model(request: ChatRequest, model: str) -> ChatRequest:
        """同一请求换模型（回退用）：模型名两处都要改——顶层字段与 raw。"""
        if request.raw.get("model") == model and request.model == model:
            return request
        raw = dict(request.raw)
        raw["model"] = model
        return ChatRequest(model=model, messages=request.messages,
                           stream=request.stream, raw=raw)

    def _fallback_allowed(self, target: ModelTarget) -> bool:
        """回退成员是否允许尝试：目录能确认它挂在候选渠道上，或目录未就绪。

        目录未就绪（冷启动快照缺失 / 拉取失败）时**放行**交给执行层兜底候选，
        与 `_apply_binding` 的保守策略一致；目录可用但该模型不在任何候选渠道
        时跳过，避免对不存在的模型白打一次上游（这正是「兼容组白名单」的含义）。
        """
        aliases = self._deps.model_aliases
        if not aliases:
            return True
        lower = target.model.lower()
        return any(lower in aliases.get(pid, {}) for pid in target.providers)

    def _apply_binding(self, target: ModelTarget,
                       binding: str | None) -> ModelTarget:
        """按 API Key 的渠道绑定收窄候选上游。

        绑定是 Key 的策略，不是请求参数：它把候选固定到该渠道，并把
        `forced` 置真（跳过模型目录收窄，因为归属已在此校验判断过）。

        模型确实属于别的渠道时给 400 而不是让它落到「无可用凭证」503——
        前者是「你请求错了」，后者读起来像服务坏了。目录未就绪（拉取失败）
        时不做归属判断，保守放行给下游选号，避免用缓存外的信息误拒。
        """
        if not binding:
            return target
        if target.forced and target.providers != (binding,):
            raise InvalidRequest(
                f"model {target.model!r} is bound to provider {target.providers[0]!r} "
                f"but this api key is restricted to {binding!r}")
        aliases = self._deps.model_aliases
        if aliases:
            lower = target.model.lower()
            known = tuple(pid for pid in aliases if lower in aliases.get(pid, {}))
            if known and binding not in known:
                raise InvalidRequest(
                    f"model {target.model!r} is not available on provider {binding!r} "
                    f"(this api key is bound to {binding!r}; provided by "
                    f"{', '.join(sorted(known))})")
        return ModelTarget(model=target.model, providers=(binding,), forced=True)

    def preflight(self, request: ChatRequest,
                  provider_binding: str | None = None) -> ModelTarget:
        """流式路由建立 StreamingResponse 之前的前置校验。

        生成器体内的异常发生在响应头（200）已发出之后，只能表现为连接被
        截断——客户端拿到空 body，误以为请求成功。凡是"请求本身不可能成功"
        的错误（未知 provider、模型不属于任何已注册上游）必须在返回
        StreamingResponse 之前抛给异常处理器，才能得到正确的 400。

        回退链启用时按整条链判断：链首无注册上游但有回退项可用时放行，交给
        `stream` 逐链项尝试。
        """
        target = self.resolve_target(request, provider_binding)
        for attempt in self._fallback_chain(target):
            if self._registered(attempt):
                return target
        raise NoProviderForModel(f"no provider registered for model {target.model!r}")

    async def stream_guarded(self, request: ChatRequest, *, username: str = "unknown",
                             translator: StreamSink | None = None,
                             provider_binding: str | None = None) -> AsyncIterator[bytes]:
        """流式出口的最终兜底：任何逃逸异常都转成 SSE 错误帧。

        没有这一层时，未预期的异常（如凭证在轮换中途被删除）会让连接
        静默断开，客户端无法区分"空回复"与"服务出错"。
        """
        inner = self.stream(request, username=username,
                            translator=translator,
                            provider_binding=provider_binding)
        try:
            async for frame in inner:
                yield frame
        except (GeneratorExit, asyncio.CancelledError):
            raise                          # 客户端断开：已由 stream() 记账
        except Exception as error:  # noqa: BLE001 - 流已开始，只能以错误帧收尾
            logger.exception("流式响应失败: %s", error)
            yield _stream_error_frame(translator, "internal server error", "internal_error")
        finally:
            # 同 stream()：break / 客户端断开都不会关闭内层流，必须显式关闭，
            # 否则上游流的节流名额只能等 GC 回收
            await aclose_stream(inner)

    async def stream(self, request: ChatRequest, *, username: str = "unknown",
                     translator: StreamSink | None = None,
                     provider_binding: str | None = None) -> AsyncIterator[bytes]:
        """流式执行；上游错误按分类冷却并换号，最多 3 次。

        启用兼容组回退链时，**只有在当前链项还没产出任何响应帧之前**
        才允许切到下一链项——已出帧后换模型会让客户端看到两个模型的混合输出。
        每链项内部仍是完整的选号 / 冷却 / 轮换 / 粘性 / 统计路径；整条链都用尽
        时用最后一个链项的 translator 产出终帧。

        客户端中途断开时（生成器被关闭 / 任务被取消）把已产生的用量
        记入统计，标记 client_disconnect：否则统计里的用量低于真实消耗，
        而断开是长回复场景下的常态。

        例外：完整响应（[DONE]）已产出后的断开不算中途——客户端拿到
        回调后立即关闭连接是正常收尾（SSE 流在 [DONE] 发出到结束帧
        more_body=False 之间有一拍竞态，框架会把它当断开），按成功记账。
        """
        target = self.resolve_target(request, provider_binding)
        chain = self._fallback_chain(target)
        state = _StreamState(translator=translator or StreamTranslator(target.model),
                             started=time.monotonic(), username=username)
        if self._deps.affinity is not None:
            state.affinity_id = self._deps.affinity.pin_for(request.raw, username)
        exhausted: _ModelExhausted | None = None
        for attempt in chain:
            if translator is None:
                # 每链项自成一路出口（默认 chat 出口）：模型名随链项走。
                # 注入的 translator（responses / anthropic）由调用方持有，
                # 回退只在未出帧前发生，复用同一实例不会有半截状态。
                state.translator = StreamTranslator(attempt.model)
            loop_source = self._stream_loop(
                self._with_model(request, attempt.model), attempt, state)
            try:
                async for frame in loop_source:
                    yield frame
            except _ModelExhausted as error:
                exhausted = error
                continue
            except (GeneratorExit, asyncio.CancelledError):
                if not state.recorded and (state.translator.usage is not None
                                           or state.ttfb_ms() is not None):
                    if state.translator.done_sent:
                        # [DONE] 已产出：客户端收尾断开，按成功记账（tokens 如实记录）
                        if state.credential_id is not None:
                            self._deps.credentials.save_success(state.credential_id)
                        self._record_success(attempt, state, state.provider,
                                             state.credential_id or "-")
                    else:
                        self._record_disconnect(attempt, state)
                raise
            finally:
                # 客户端断开（GeneratorExit / CancelledError）时上面的 async for
                # 不会关闭 _stream_loop，不显式关闭的话上游流的节流名额只能等
                # GC 回收——生产路径靠 with_keepalive 取消 task 侥幸及时，换个调用
                # 方就会攒满 max_concurrency 后永久阻塞
                await aclose_stream(loop_source)
            return
        # 链至少一项，循环里每项要么正常 return、要么抛 _ModelExhausted；能走到
        # 这里说明整条链都在**未出帧前**用尽——用最后链项的 translator 收尾。
        if exhausted is None:  # pragma: no cover - 按构造不可达（链非空且必抛）
            raise RuntimeError("fallback chain produced no terminal error")
        yield state.translator.error_frame(exhausted.message, exhausted.code)

    async def _stream_loop(self, request: ChatRequest, target: ModelTarget,
                           state: _StreamState) -> AsyncIterator[bytes]:
        tried: set[str] = set()
        # 真正打过上游的次数（与 tried 分开）：INVALID 的 _skip_provider 会把
        # 被跳过的上游全部凭证塞进 tried，但那些都没被尝试过，不能计入轮换预算
        # ——否则一个不认模型的上游就能占满 max_rotate，永试不到别的上游（H2）。
        attempts = 0
        last_error: Exception | None = None
        last_kind: ErrKind | None = None
        # 本链项是否已落过尝试级明细（DEAD）：是则选号耗尽时不再落收尾行，
        # 否则同一请求在 requests 里算两次、Key 配额扣两次。
        attributed = False
        # 本链项是否已向客户端出过响应帧：出了就绝不再换模型**或换号**
        # （半截输出不可回滚，换号会让两个凭证的内容拼进同一条回复）
        emitted = False
        # 出站剥离思考内容：每次请求现读（热更）。放在这里（executor 消费
        # 上游事件的循环）而不是 `_stream_source` 里，是因为续写包装器在
        # `_stream_source` 内层、需要 reasoning 原文来拼续写上下文；剥在
        # 外层既保住续写质量，又对三个出口（chat/Anthropic/Responses）一致。
        hide_reasoning = self._thinking_disabled()

        # 排队重开（F3）：上游回「排队中 + retryAfterSeconds」时，等它点名的
        # 秒数后在**原号**上重开，不冷却、不换号、不占轮换预算。
        # `pinned` 是被点名的凭证（下一轮跳过 `_pick` 直接用），
        # `queued_wait` 是本轮要等的秒数（等完 `continue`）。
        queue_rounds = 0
        pinned: tuple[str, dict[str, Any]] | None = None

        def terminal(message: str, code: str) -> bytes:
            """终帧：本项未出帧时抛信号让外层试下一链项；已出帧则写错误帧收尾。"""
            if not emitted:
                raise _ModelExhausted(message, code)
            return state.translator.error_frame(message, code)

        while True:
            if pinned is not None:
                # 排队重开：复用被点名的凭证，不走 `_pick`（否则可能选到别的号，
                # 排队语义就丢了）。凭证可能已被并发删除，删了就退回正常选号。
                # **不计入 attempts**：排队不是一次「换号尝试」，吃掉轮换预算
                # 会让三次排队就把整个请求打成 503。
                credential_id, credential_data = pinned
                pinned = None
                if self._deps.credentials.credential_data(credential_id) is None:
                    continue
                provider_id = self._deps.credentials.provider_of(credential_id)
                state.provider, state.credential_id = provider_id or "-", credential_id
            else:
                pick = await self._pick(target, tried, state.affinity_id)
                if pick is None:
                    # 走到这里 emitted 必为 False：已出帧的路径在上面 `if emitted or
                    # not should_rotate` 就 return 了，所以 `terminal()` 在这里**必定**
                    # 抛 `_ModelExhausted`（交给回退链下一项）。直接抛，不留永远
                    # 走不到的 `yield`+`return`。
                    if last_kind is ErrKind.INVALID:
                        # 所有候选上游都拒绝了该模型：400 语义而非 503
                        raise _ModelExhausted(
                            _reject_message(target.model, last_error,
                                            self._suggestions(target.model)),
                            "invalid_request")
                    if not attributed:
                        self._deps.record(
                            username=state.username, provider=state.provider,
                            credential_id=state.credential_id, model=target.model, ok=False,
                            error_type=self._exhaust_error_type(target),
                            latency_ms=_elapsed_ms(state.started))
                    raise _ModelExhausted(
                        self._unavailable_text(target, last_error),
                        "no_healthy_credential")
                credential_id, credential_data = pick
                provider_id = self._deps.credentials.provider_of(credential_id)
                state.provider, state.credential_id = provider_id or "-", credential_id
                tried.add(credential_id)
                attempts += 1
            # 按实际服务渠道压缩（未启用/未知窗口/未超限时原样返回）：窗口
            # 取该渠道自己的上限，不再被更小窗口的兄弟渠道拖累误裁
            payload = self._compress_payload(request, provider_id, target.model)
            # 档位映射放在选号之后：目标档位依赖**实际服务渠道**（同名模型
            # 跨渠道接受的档位不同）。返回值可能是新 dict，不改 `request.raw`
            payload = self._map_effort(provider_id, payload)
            # 显式持有迭代器并在 finally 里关闭：下面两处 `break`（流内错误）
            # 只跳出 `async for`，不会关闭上游 async generator，provider 的
            # 节流名额归还（pacer.release 在其 finally 里）会推迟到 GC。
            # 轮换重试每次都要重新 wait_turn，泄漏累积到 max_concurrency 后
            # 新请求永久阻塞——「用了三次就限制」。见 aclose_stream 注释。
            iterator = self._stream_source(
                provider_id, credential_data, payload, target.model).__aiter__()
            # 在途登记放在创建迭代器之后、`try` 之前：选号到此全同步，不会与
            # 并发请求交错超额；释放统一走下面的 finally，覆盖成功/轮换/断开。
            self._lease(credential_id)
            reader = _TimedReader(iterator)
            state.deadline = None
            broken = False
            queued_wait: float | None = None
            try:
                while True:
                    event = await self._next_event(reader, state, emitted)
                    if event is None:
                        break                       # 上游正常结束
                    if hide_reasoning and event.kind is EventKind.REASONING:
                        # 出站剥离思考：上游仍照常生成，只是不交给客户端
                        continue
                    if event.kind is EventKind.ERROR:
                        kind = _event_kind(event)
                        if kind is ErrKind.QUEUE:
                            # 排队：上游点名「等 N 秒再来」。未出帧时在原号上
                            # 重开，不冷却、不换号、不占轮换预算；已出帧则
                            # 无法重开（半截输出不可回滚），按 SOFT 收尾。
                            plan = self._plan_queue_restart(
                                event.retry_after_seconds, emitted=emitted,
                                rounds=queue_rounds, attempts=attempts,
                                provider_id=provider_id, credential_id=credential_id,
                                credential_data=credential_data)
                            if plan is not None:
                                queue_rounds = plan.rounds
                                attempts = plan.attempts
                                pinned = plan.pinned
                                queued_wait = plan.wait
                                broken = True
                                break
                            kind = ErrKind.SOFT
                        if kind is ErrKind.INVALID:
                            # 流内 4001 等参数/模型错误：换凭证没用，
                            # 跳过该上游继续试其他上游；全部拒绝才以 400 结束
                            logger.warning(
                                "上游 %s 流内拒绝模型 %s（凭证 %s 跳过）: code=%s %s",
                                provider_id, target.model, credential_id,
                                event.error_code, event.error_message)
                            self._record_invalid(state.username, provider_id, credential_id,
                                                 target.model, state.started, event)
                            last_error = UpstreamStreamError(event)
                            last_kind = ErrKind.INVALID
                            self._skip_provider(provider_id, tried)
                            broken = True
                            break
                        logger.warning(
                            "上游 %s 流内错误（凭证 %s，kind=%s）: code=%s %s",
                            provider_id, credential_id, kind,
                            event.error_code, event.error_message)
                        attributed = self._note_upstream_error(
                            credential_id, kind, provider_id, target.model, event,
                            username=state.username, started=state.started) or attributed
                        last_error = UpstreamStreamError(event)
                        broken = True
                        break
                    for frame in state.translator.translate(event):
                        state.mark_first_byte()
                        emitted = True
                        yield frame
                if not broken:
                    # 上游正常结束：清错误累计、重新粘定、按成功记账并收尾
                    self._deps.credentials.save_success(
                        credential_id, model=self._model_scope(provider_id, target.model))
                    self._remember(request, state.username, credential_id)
                    self._record_success(target, state, provider_id, credential_id)
                    for frame in state.translator.finish():
                        emitted = True
                        yield frame
                    return
            except Exception as error:  # noqa: BLE001 - 统一转为冷却或上抛
                kind = _classify(error)
                if kind is None:
                    raise
                if kind is ErrKind.QUEUE:
                    # 排队也可能以 HTTP 403 + 排队 body 的形式抛出（见
                    # qoder.events.classify_status）：同样在原号上等后重开。
                    plan = self._plan_queue_restart(
                        getattr(error, "retry_after_seconds", None),
                        emitted=emitted, rounds=queue_rounds, attempts=attempts,
                        provider_id=provider_id, credential_id=credential_id,
                        credential_data=credential_data)
                    if plan is not None:
                        queue_rounds = plan.rounds
                        attempts = plan.attempts
                        pinned = plan.pinned
                        queued_wait = plan.wait
                    else:
                        kind = ErrKind.SOFT
                        logger.warning("上游 %s 排队但无法重开（凭证 %s）: %s",
                                       provider_id, credential_id, _error_detail(error))
                        self._note_upstream_error(credential_id, kind, provider_id,
                                                  target.model, error,
                                                  username=state.username,
                                                  started=state.started)
                        last_error = error
                elif kind is ErrKind.INVALID:
                    # 请求无效（如该上游不认识模型）：不冷却凭证，
                    # 跳过该上游继续试其他上游；全部拒绝才以 400 结束
                    logger.warning("上游 %s 拒绝模型 %s（凭证 %s 跳过）: %s",
                                   provider_id, target.model, credential_id,
                                   _error_detail(error))
                    self._record_invalid(state.username, provider_id, credential_id,
                                         target.model, state.started, error)
                    last_error, last_kind = error, ErrKind.INVALID
                    self._skip_provider(provider_id, tried)
                else:
                    logger.warning("上游 %s 错误（凭证 %s，kind=%s）: %s",
                                   provider_id, credential_id, kind,
                                   _error_detail(error))
                    attributed = self._note_upstream_error(
                        credential_id, kind, provider_id, target.model,
                        error, username=state.username,
                        started=state.started) or attributed
                    last_error = error
            finally:
                # break / 异常 / 客户端断开都走到这里：先清掉在途的取帧任务
                # （超时后它可能还在等上游），再关闭上游流归还节流名额。
                # 顺序不能反：生成器正在被 anext 驱动时 aclose 会抛
                # "asynchronous generator is already running"。
                await reader.aclose()
                await aclose_stream(iterator)
                self._release(credential_id)
            if queued_wait is not None:
                # 在租约与上游流都释放**之后**才等：等待期间不占并发名额，
                # 也不让上游连接白挂着。等完回到 while 顶部用 `pinned` 重开。
                await asyncio.sleep(queued_wait)
                continue
            if not self._deps.scheduler.should_rotate(attempts) or emitted:
                if last_kind is ErrKind.INVALID:
                    # 流已开始（200 已发出），以 invalid_request 错误帧结束
                    yield terminal(
                        _reject_message(target.model, last_error,
                                        self._suggestions(target.model)),
                        "invalid_request")
                    return
                # 已出帧后收尾：上游可能已生成内容（真实计费），按已知用量记账，
                # 否则这次尝试的 token 全部漏记（与断开路径同一理由）
                usage = state.translator.usage
                self._deps.record(
                    username=state.username, provider=provider_id,
                    credential_id=credential_id, model=target.model, ok=False,
                    error_type=_stream_error_type(last_error),
                    input_tokens=_usage_field(usage, "input_tokens"),
                    output_tokens=_usage_field(usage, "output_tokens"),
                    latency_ms=_elapsed_ms(state.started))
                if isinstance(last_error, _StreamTimeout):
                    # 空闲超时（已出帧）：半截输出不可回滚，不换号，直接以错误帧
                    # 收尾；对外文案带上受控标识（A1 验收口径）
                    yield state.translator.error_frame(
                        _unavailable_message(last_error), "no_healthy_credential")
                    return
                yield terminal(self._unavailable_text(target, last_error),
                               "no_healthy_credential")
                return

    async def _next_event(self, reader: _TimedReader, state: _StreamState,
                          emitted: bool) -> Event | None:
        """取下一个上游事件，按出帧状态施加首事件/空闲超时（A1）。

        返回 None 表示上游流正常结束；超时抛 `_StreamTimeout`，由外层
        `_stream_loop` 统一按 SOFT 冷却（未出帧 → 换号；已出帧 → 错误帧收尾）。

        计时口径：

        - 未出帧 → `first_event_timeout_seconds`；已出帧 → `stream_idle_timeout_seconds`
        - **只有数据事件重置计时**：注释帧在更外层（`with_keepalive`）且不经过
          这里，空事件（无内容/无工具调用/无 usage/finish）直接 continue 重取，
          都不刷新 deadline——否则一个只会发心跳或空 delta 的上游永远不超时。
        - ≤0 关闭对应计时（行为与历史版本一致）。

        截止时刻存进 `state`：跨尝试保留（换号后重新计时的语义是「新尝试
        重新计首事件」，故每次进入新凭证时由 `_stream_loop` 重置）。
        """
        first = float(live(self._deps.first_event_timeout_seconds)() or 0)
        idle = float(live(self._deps.stream_idle_timeout_seconds)() or 0)
        timeout = idle if emitted else first
        phase = "idle" if emitted else "first_event"
        remaining = None
        if timeout > 0:
            deadline = state.deadline
            now = self._clock()
            if deadline is None:
                deadline = now + timeout
                state.deadline = deadline
            # 可能为负（时钟跳过窗口，如进程被挂起）：负超时让等待立刻返回
            # 「未完成」，等价于「窗口已耗尽」，不必单独判一次
            remaining = deadline - now
        try:
            event = await reader.next(remaining)
        except StopAsyncIteration:
            return None
        except TimeoutError:
            raise _StreamTimeout(timeout, phase) from None
        if _is_data_event(event):
            state.deadline = None
        return event

    def _compress_payload(self, request: ChatRequest, provider_id: str | None,
                          model: str) -> dict[str, Any]:
        """按实际服务渠道压缩请求体；未装配 / 未启用 / 未知窗口时原样返回。

        放在选号之后（而不是 API 入口）是为了用**该渠道自己的**输入上限：
        同名模型跨渠道窗口不同（180K vs 1M），路由前只能取 min，会把大窗口
        渠道的会话误裁到前缀不稳定、击穿上游前缀缓存。读 `request.raw` 原文
        而非上一轮压缩结果：换号/回退可能落到不同窗口的渠道，必须每轮从
        原文重压（实现契约禁止原地修改，见 ExecutorDeps.context_compress）。
        """
        if self._deps.context_compress is None:
            return request.raw
        return self._deps.context_compress(provider_id or "-", model, request.raw)

    def _thinking_disabled(self) -> bool:
        """出站是否剥离思考内容（热更）。未装配 / 标量 False 时即关闭。"""
        return bool(live(self._deps.force_disable_thinking)())

    def _map_effort(self, provider_id: str | None,
                    payload: dict[str, Any]) -> dict[str, Any]:
        """按实际服务渠道改写客户端显式给的 `reasoning_effort`。

        只动客户端**显式**给的值：渠道各自的 `setdefault` 补默认档发生在
        provider 侧、晚于这里，故不受影响（见 ExecutorDeps.reasoning_effort_map）。
        需要改写时返回**新 dict**——`payload` 可能正是 `request.raw`，原地改会
        污染会话粘性指纹、也会把改写泄漏到下一轮轮换。
        """
        if self._deps.reasoning_effort_map is None:
            return payload
        raw = live(self._deps.reasoning_effort_map)()
        if not raw:
            return payload
        effort = payload.get("reasoning_effort")
        if not isinstance(effort, str) or not effort:
            return payload
        mapped = map_effort(parse_effort_map(str(raw)), provider_id, effort)
        if mapped == effort:
            return payload
        updated = dict(payload)
        if mapped is None:
            updated.pop("reasoning_effort", None)
        else:
            updated["reasoning_effort"] = mapped
        return updated

    def _stream_source(self, provider_id: str, credential_data: dict[str, Any],
                       payload: dict[str, Any], model: str) -> AsyncIterator[Event]:
        """上游事件流入口；开启截断续写时包一层。

        续写在包装器内固定用同一凭证续发请求，因此凭证轮换/记账/统计
        逻辑完全不需要感知它——executor 只会看到一条更长的流。
        """
        upstream_model = self._upstream_model(provider_id, model)
        stream = self._deps.providers[provider_id].stream_chat(
            credential_data, payload, upstream_model)
        # 续写上限可热更：0 表示关闭，每次请求读当前值
        max_continues = int(live(self._deps.max_auto_continues)())
        if max_continues <= 0:
            return stream
        return ContinuationStream(
            self._deps.providers[provider_id], credential_data, payload,
            upstream_model, max_continues=max_continues)

    def _record_success(self, target: ModelTarget, state: _StreamState,
                        provider_id: str, credential_id: str) -> None:
        state.recorded = True
        usage = state.translator.usage
        self._deps.record(
            username=state.username, provider=provider_id, credential_id=credential_id,
            model=target.model, ok=True,
            input_tokens=_usage_field(usage, "input_tokens"),
            output_tokens=_usage_field(usage, "output_tokens"),
            reasoning_tokens=_usage_field(usage, "reasoning_tokens"),
            cached_tokens=_usage_field(usage, "cached_tokens"),
            credit=_usage_field(usage, "credit"),
            credit_estimated=bool(_usage_field(usage, "credit_estimated")),
            ttfb_ms=state.ttfb_ms(), latency_ms=_elapsed_ms(state.started))

    def _record_disconnect(self, target: ModelTarget, state: _StreamState) -> None:
        """客户端断开：按已知用量记账，标记 client_disconnect。"""
        state.recorded = True
        usage = state.translator.usage
        self._deps.record(
            username=state.username, provider=state.provider,
            credential_id=state.credential_id, model=target.model, ok=False,
            error_type="client_disconnect",
            input_tokens=_usage_field(usage, "input_tokens"),
            output_tokens=_usage_field(usage, "output_tokens"),
            ttfb_ms=state.ttfb_ms(), latency_ms=_elapsed_ms(state.started))

    async def complete(self, request: ChatRequest, *, username: str = "unknown",
                       provider_binding: str | None = None) -> dict[str, Any]:
        """非流式：按兼容组回退链依次尝试，逐链项走完整的换号重试。

        回退只在**整条链**都用尽时以最后一个链项的 503/400 收尾；单链项内部
        的选号 / 冷却 / 轮换 / 会话粘性 / 统计全部沿用既有路径。
        """
        target = self.resolve_target(request, provider_binding)
        chain = self._fallback_chain(target)
        last_error: Exception | None = None
        for attempt in chain:
            try:
                return await self._complete_model(
                    self._with_model(request, attempt.model), attempt, username)
            except (NoHealthyCredential, InvalidRequest) as error:
                last_error = error
        if last_error is None:  # pragma: no cover - 按构造不可达（链非空且必抛）
            raise NoHealthyCredential(self._unavailable_text(chain[0], None))
        raise last_error

    async def _complete_model(self, request: ChatRequest, target: ModelTarget,
                              username: str) -> dict[str, Any]:
        """单个模型的非流式执行（原 complete 主体）。"""
        tried: set[str] = set()
        # 真正打过的凭证数（与 tried 分开），理由同 _stream_loop：INVALID 的
        # _skip_provider 把上游全部凭证塞进 tried 只为排除候选，不计轮换预算（H2）。
        attempts = 0
        last_error: Exception | None = None
        last_kind: ErrKind | None = None
        # 本次请求是否已落过尝试级明细（DEAD）：是则选号耗尽时不再落收尾行，
        # 否则同一请求在 requests 里算两次、Key 配额扣两次。
        attributed = False
        started = time.monotonic()
        last_provider = "-"
        last_credential: str | None = None
        # 首事件时刻：上游响应的第一个事件（TTFE，非流式的首字延迟）；跨重试只记最早一次
        first_event_at: float | None = None
        # 会话粘性：对话上一轮用过哪个凭证，本轮优先复用
        affinity_id = (self._deps.affinity.pin_for(request.raw, username)
                       if self._deps.affinity is not None else None)
        # 出站剥离思考内容：与 _stream_loop 同口径，每次请求现读（热更）。
        # 在**聚合前**丢弃 reasoning 事件，`aggregate()` 便不会写出
        # `reasoning_content`；三个出口共用 complete 路径，故一致生效。
        hide_reasoning = self._thinking_disabled()

        # 排队重开：与 `_stream_loop` 同一套语义（不冷却、不换号、不占轮换预算）。
        # 非流式同样要处理：排队信号在上游流里，聚合前不拦就会被 `aggregate`
        # 当成流内错误，直接冷却换号。
        queue_rounds = 0
        pinned: tuple[str, dict[str, Any]] | None = None

        while True:
            if pinned is not None:
                credential_id, credential_data = pinned
                pinned = None
                if self._deps.credentials.credential_data(credential_id) is None:
                    continue
                provider_id = self._deps.credentials.provider_of(credential_id)
                last_provider, last_credential = provider_id or "-", credential_id
            else:
                pick = await self._pick(target, tried, affinity_id)
                if pick is None:
                    if last_kind is ErrKind.INVALID:
                        # 所有候选上游都拒绝了该模型：400 而非 503
                        raise InvalidRequest(_reject_message(
                            target.model, last_error, self._suggestions(target.model)))
                    if not attributed:
                        self._deps.record(
                            username=username, provider=last_provider,
                            credential_id=last_credential,
                            model=target.model, ok=False,
                            error_type=self._exhaust_error_type(target),
                            latency_ms=_elapsed_ms(started))
                    raise NoHealthyCredential(
                        self._unavailable_text(target, last_error))
                credential_id, credential_data = pick
                tried.add(credential_id)
                attempts += 1
                provider_id = self._deps.credentials.provider_of(credential_id)
                last_provider, last_credential = provider_id or "-", credential_id
            # 与 _stream_loop 同理：按实际服务渠道压缩（对同一请求可能压多次，
            # 但实现必须无副作用——每轮都从原文出发），再按实际服务渠道映射档位
            payload = self._compress_payload(request, provider_id, target.model)
            payload = self._map_effort(provider_id, payload)
            events: list[Event] = []
            # 与 _stream_loop 同理：显式关闭上游流，让节流名额在每次尝试
            # 结束时同步归还，不依赖 GC 的 asyncgen finalize
            iterator = self._stream_source(
                provider_id, credential_data, payload, target.model).__aiter__()
            # 与 _stream_loop 同理：在途登记 + finally 释放（含超时/断开）
            self._lease(credential_id)
            queued_wait: float | None = None
            try:
                # 聚合整体超时兜底（见 ExecutorDeps.complete_timeout_seconds）
                async with (asyncio.timeout(self._deps.complete_timeout_seconds)
                            if self._deps.complete_timeout_seconds > 0
                            else contextlib.nullcontext()):
                    async for event in iterator:
                        if first_event_at is None:
                            first_event_at = time.monotonic()
                        if hide_reasoning and event.kind is EventKind.REASONING:
                            # 出站剥离思考：上游仍照常生成，只是不交给客户端
                            continue
                        if (event.kind is EventKind.ERROR
                                and _event_kind(event) is ErrKind.QUEUE):
                            # 排队信号必须在入 events **之前**拦下：进了 events
                            # 就会被 `aggregate` 当成流内错误，直接冷却换号。
                            plan = self._plan_queue_restart(
                                event.retry_after_seconds, emitted=False,
                                rounds=queue_rounds, attempts=attempts,
                                provider_id=provider_id, credential_id=credential_id,
                                credential_data=credential_data)
                            if plan is not None:
                                raise _Queued(plan)
                            # 轮次用尽：降级为普通瞬态错误，交给既有路径处理
                            event = replace(event, error_kind=ErrKind.SOFT)
                        events.append(event)
                        # 事件条数上限：超长/死循环输出下 events 会无界
                        # 增长直到 OOM，超时兜不住「快而无穷」。按瞬态错误换号。
                        if (self._deps.complete_max_events > 0
                                and len(events) > self._deps.complete_max_events):
                            raise _AggregateOverflow(
                                self._deps.complete_max_events)
            except _Queued as queued:
                queue_rounds = queued.plan.rounds
                attempts = queued.plan.attempts
                pinned = queued.plan.pinned
                queued_wait = queued.plan.wait
            except TimeoutError:
                logger.warning(
                    "上游 %s 非流式聚合超过 %ss（凭证 %s，按瞬态错误换号）",
                    provider_id, self._deps.complete_timeout_seconds, credential_id)
                self._note_upstream_error(credential_id, ErrKind.SOFT,
                                          provider_id, target.model,
                                          username=username, started=started)
                last_error = UpstreamStreamError(Event(
                    kind=EventKind.ERROR,
                    error_message=(f"upstream complete timed out after "
                                   f"{self._deps.complete_timeout_seconds}s")))
            except _AggregateOverflow as overflow:
                logger.warning(
                    "上游 %s 非流式聚合事件超过 %s 条（凭证 %s，按瞬态错误换号）",
                    provider_id, overflow.limit, credential_id)
                self._note_upstream_error(credential_id, ErrKind.SOFT,
                                          provider_id, target.model,
                                          username=username, started=started)
                last_error = UpstreamStreamError(Event(
                    kind=EventKind.ERROR,
                    error_message=(f"upstream complete exceeded "
                                   f"{overflow.limit} events")))
            except Exception as error:  # noqa: BLE001
                kind = _classify(error)
                if kind is None:
                    raise
                if kind is ErrKind.QUEUE:
                    # HTTP 级排队（403 + 排队 body）：与流内排队同一套语义
                    plan = self._plan_queue_restart(
                        getattr(error, "retry_after_seconds", None),
                        emitted=False, rounds=queue_rounds, attempts=attempts,
                        provider_id=provider_id, credential_id=credential_id,
                        credential_data=credential_data)
                    if plan is not None:
                        queue_rounds = plan.rounds
                        attempts = plan.attempts
                        pinned = plan.pinned
                        queued_wait = plan.wait
                    else:
                        logger.warning("上游 %s 排队但无法重开（凭证 %s）: %s",
                                       provider_id, credential_id, _error_detail(error))
                        self._note_upstream_error(credential_id, ErrKind.SOFT,
                                                  provider_id, target.model, error,
                                                  username=username, started=started)
                        last_error = error
                elif kind is ErrKind.INVALID:
                    # 请求无效（如该上游不认识模型）：不冷却凭证，
                    # 跳过该上游继续试其他上游；全部拒绝才以 400 结束
                    logger.warning("上游 %s 拒绝模型 %s（凭证 %s 跳过）: %s",
                                   provider_id, target.model, credential_id,
                                   _error_detail(error))
                    self._record_invalid(username, provider_id, credential_id,
                                         target.model, started, error)
                    last_error, last_kind = error, ErrKind.INVALID
                    self._skip_provider(provider_id, tried)
                else:
                    logger.warning("上游 %s 错误（凭证 %s，kind=%s）: %s",
                                   provider_id, credential_id, kind,
                                   _error_detail(error))
                    attributed = self._note_upstream_error(
                        credential_id, kind, provider_id, target.model,
                        error, username=username, started=started) or attributed
                    last_error = error
            else:
                try:
                    result = aggregate(events, target.model)
                except UpstreamStreamError as error:
                    kind = _event_kind(error.event)
                    if kind is ErrKind.INVALID:
                        # 流内参数/模型错误：跳过该上游继续轮换，不冷却
                        logger.warning(
                            "上游 %s 流内拒绝模型 %s（凭证 %s 跳过）: code=%s %s",
                            provider_id, target.model, credential_id,
                            error.event.error_code, error.event.error_message)
                        self._record_invalid(username, provider_id, credential_id,
                                             target.model, started, error)
                        last_error, last_kind = error, ErrKind.INVALID
                        self._skip_provider(provider_id, tried)
                    else:
                        attributed = self._note_upstream_error(
                            credential_id, kind, provider_id, target.model, error,
                            username=username, started=started) or attributed
                        last_error = error
                else:
                    self._deps.credentials.save_success(
                        credential_id, model=self._model_scope(provider_id, target.model))
                    self._remember(request, username, credential_id)
                    usage = result.get("usage") or {}
                    # credit 不在对外响应里（客户端不需要，见 compat 出口），
                    # 但统计要记：从 USAGE 事件取，非流式路径否则会整条漏掉。
                    # 取**最后一个**而非第一个：CodeArts v2 每个 chunk 都带 usage，
                    # 前面的帧是 0/0 或空占位、只有收尾帧是真值，取第一个会让积分
                    # 记成空/0。末尾优先与流式路径（translator.usage）和
                    # `aggregate()` 同语义，落库的 usage 与对外响应才同源。
                    usage_event = next(
                        (e.usage for e in reversed(events)
                         if e.kind is EventKind.USAGE and e.usage is not None), None)
                    self._deps.record(
                        username=username, provider=provider_id,
                        credential_id=credential_id, model=target.model, ok=True,
                        input_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                        reasoning_tokens=(usage.get("completion_tokens_details") or {})
                        .get("reasoning_tokens"),
                        cached_tokens=(usage.get("prompt_tokens_details") or {})
                        .get("cached_tokens"),
                        credit=(usage_event.credit if usage_event else None),
                        credit_estimated=bool(usage_event.credit_estimated)
                        if usage_event else False,
                        ttfb_ms=(int((first_event_at - started) * 1000)
                                 if first_event_at is not None else None),
                        latency_ms=_elapsed_ms(started))
                    return result
            finally:
                await aclose_stream(iterator)
                self._release(credential_id)
            if queued_wait is not None:
                # 租约与上游流都释放**之后**才等（同 _stream_loop）
                await asyncio.sleep(queued_wait)
                continue
            if not self._deps.scheduler.should_rotate(attempts):
                if last_kind is ErrKind.INVALID:
                    raise InvalidRequest(_reject_message(
                        target.model, last_error, self._suggestions(target.model)))
                kind = _classify(last_error) if last_error is not None else None
                self._deps.record(
                    username=username, provider=provider_id, credential_id=credential_id,
                    model=target.model, ok=False,
                    error_type=_error_type_for(kind) if kind else "upstream_protocol",
                    latency_ms=_elapsed_ms(started))
                raise NoHealthyCredential(
                    self._unavailable_text(target, last_error))

    # -------------------------------------------------------------- 内部

    def _remember(self, request: ChatRequest, username: str, credential_id: str) -> None:
        """成功后把本对话粘到实际服务的凭证（轮换降级后随之换粘）。"""
        if self._deps.affinity is not None:
            self._deps.affinity.remember(request.raw, username, credential_id)

    async def _pick(self, target: ModelTarget, tried: set[str],
                    affinity_id: str | None = None):
        """选一个「仓库里还在」的凭证；被并发删除就换下一个。

        循环而不是递归：候选池理论上无上限，`tried` 又只在删一个后加一项，
        极端情况下（大量凭证被并发删除）递归深度能顶到 Python 的递归上限。
        循环写法没有这个上限，也省掉每次递归的栈开销。

        短冷却「等待续上」（A4，`short_cooldown_wait_seconds`）：没有任何
        **立即可用**候选、但最短的剩余冷却不超过该上限时，等待到期后重取
        一次。目的：全池都在 60s 软冷却时，与其立刻 503 让客户端自己重试
        （重试往往更密集），不如在原连接上等几秒续上。

        约束（计划 A4 明确要求）：

        - 只在「一个立即可用候选都没有」时等待——有可用号就必须马上用，
          等冷却中的号等于白降吞吐。
        - 等待期间**不占在途租约**（租约在选中之后才 acquire），所以等待
          不会把并发堆成连接洪峰：N 个请求一起等，到期后仍按原规则分摊。
        - **不计入 `max_rotate`**：等待发生在选号阶段，`attempts` 由调用方
          按真正打过的凭证数计，这里不碰。
        - 只重取一次：到期后仍有别的请求抢走名额就照常返回 None（503），
          不做「等第二轮」，否则慢请求会在池子打满时层层排队。
        """
        waited = False
        while True:
            credential_id = self._select(target, tried, affinity_id)
            if credential_id is None:
                # 没有立即可用候选：短冷却「等待续上」只允许一次（见 docstring）
                if waited or not await self._wait_short_cooldown(target, tried):
                    return None
                waited = True
                continue
            credential_data = self._deps.credentials.credential_data(credential_id)
            if credential_data is not None:
                return credential_id, credential_data
            tried.add(credential_id)  # 并发删除：标记后重选

    async def _wait_short_cooldown(self, target: ModelTarget,
                                   tried: set[str]) -> bool:
        """没有立即可用候选时，若最短剩余冷却够短则等它到期；返回是否等待过。

        `remaining` 取**所有候选里最短的**剩余冷却（账号级与模型级取小）：
        池子里只要有一个号 N 秒后可用，等 N 秒就能续上；上限是热更的
        `short_cooldown_wait_seconds`（≤0 关闭，直接快速失败）。

        候选来源与 `_select` 完全一致（`_selectable` 过滤掉限额/在途之后），
        但这里刻意**不套 `_selectable`**：被在途占满的号不是「等几秒就好」
        （它要等一个完整请求跑完，可能几分钟），把它算进等待会放大排队。
        因此只统计「冷却中 / 启用中」的候选，并按 `tried` 排除本轮已试过的。
        """
        limit = float(live(self._deps.short_cooldown_wait_seconds)() or 0)
        if limit <= 0:
            return False
        registered = self._registered(target)
        if not registered:
            return False
        now = int(time.time())
        remaining: int | None = None
        for candidate in self._deps.credentials.candidates(registered):
            if candidate.credential_id in tried:
                continue
            if candidate.disabled or not candidate.enabled:
                continue
            if (self._deps.limits is not None
                    and self._deps.limits.blocked(candidate, target.model)):
                continue
            if (self._deps.inflight is not None
                    and self._deps.inflight.blocked(candidate.credential_id)):
                continue
            until = candidate.cooling_until
            model_until = candidate.model_cooling_until(
                self._model_scope(candidate.provider, target.model))
            # 账号级与模型级取**较晚**的那个：两个都过期才真的可用
            if model_until is not None:
                until = model_until if until is None else max(until, model_until)
            # 没有冷却、或冷却已经过期却仍不可选（被别的条件挡掉）：等几秒
            # 也不会变好，跳过它——只统计「真的在冷却」的候选
            if until is None or until <= now:
                continue
            wait = until - now
            if remaining is None or wait < remaining:
                remaining = wait
        if remaining is None or remaining > limit:
            return False
        logger.info("全部候选处于短冷却（最短剩余 %ss ≤ %.0fs），等待续上",
                    remaining, limit)
        await asyncio.sleep(remaining)
        return True

    def _sticky(self, candidates: list, tried: set[str],
                affinity_id: str | None) -> str | None:
        """会话粘性：指纹命中的凭证仍可选时直接复用，不参与排序。

        只校验「在候选池里且未冷却/禁用/本请求已轮换过」；其余情况
        （凭证被删、冷却中、用户强制了别的上游）回退常规调度，成功后
        重新粘定，对话不会因此永久失粘。

        手动 pin 优先于粘性（PROPOSAL §3.3「手动 pin 优先 → 过滤 healthy →
        到期积分」）：管理员显式指定了凭证时，粘性不得把它顶掉，否则"指定"
        在对话中途失效且无处可见。

        入参 candidates 已由 `_select` 按模型作用域过滤过，这里不再重查
        模型级冷却——传归一模型名进来会与按上游原始名登记的冷却表对不上。
        """
        if affinity_id is None or affinity_id in tried:
            return None
        now = int(time.time())
        pinned = [c for c in candidates
                  if c.pinned and c.credential_id not in tried and c.is_selectable(now)]
        if pinned:
            return None
        match = next((c for c in candidates if c.credential_id == affinity_id), None)
        if match is None or not match.is_selectable(now):
            return None
        return affinity_id

    def _registered(self, target: ModelTarget) -> list[str]:
        """收窄后的候选渠道 ∩ 已注册渠道（决定「这个模型能不能发出去」）。"""
        return [pid for pid in self._narrow_providers(target)
                if pid in self._deps.providers]

    def _narrow_providers(self, target: ModelTarget) -> tuple[str, ...]:
        """模型目录能证明归属时，把候选上游收窄到登记了该模型的上游。

        CodeBuddy 独有模型用扁平名请求时，若先选中 TRAE 凭证会真实打一次
        TRAE 上游：对侧账号留请求记录、统计多一条 invalid_request，随后才
        轮换到正确上游。强制指定（@provider）是用户明确意图，不过滤；
        目录未就绪或没有任何已注册上游登记该模型时保持原候选（保守：
        模型列表只是展示口径，直连指定不受黑名单影响，见 README）。
        """
        aliases = self._deps.model_aliases
        if target.forced or not aliases:
            return target.providers
        lower = target.model.lower()
        known = tuple(
            pid for pid in target.providers
            if pid in self._deps.providers and lower in aliases.get(pid, {}))
        return known or target.providers

    def _model_rates(self, target: ModelTarget) -> dict[str, float | None]:
        """候选渠道请求**当前模型**的消耗倍率（provider_id → credit_rate）。

        数据源是模型目录（`services.model_list_cache` 的原始表，渠道原 id 经
        别名表换回）。目录里查不到的渠道（该模型不在其登记表 / 目录未就绪 /
        上游没给倍率）记 None——调度端未知倍率不参与免费优先，与 0（确认
        免费）严格区分。免费渠道（zen / kilo）的条目显式标 0.0。
        """
        aliases = self._deps.model_aliases
        cache = self._deps.model_list_cache
        if not aliases or not cache:
            return {}
        lower = target.model.lower()
        rates: dict[str, float | None] = {}
        for pid in target.providers:
            raw = aliases.get(pid, {}).get(lower)
            if raw is None:
                continue
            model = cache.get(pid, {}).get(raw.lower())
            if model is not None:
                rates[pid] = model.credit_rate
        return rates

    def _candidate(self, credential_id: str):
        for candidate in self._deps.credentials.candidates():
            if candidate.credential_id == credential_id:
                return candidate
        raise NoHealthyCredential(f"credential {credential_id} disappeared")

    def _select(self, target: ModelTarget, tried: set[str],
                affinity_id: str | None = None) -> str | None:
        """按模型收窄候选 → 过滤该模型上被冷却的凭证 → 粘性/pin/排序选号。

        模型冷却按凭证所属上游的原始模型名登记（见 `_model_scope`），
        因此逐凭证查自己的那份名字：同一账号在不同上游的大小写可能不同。
        未命中冷却时查询恒为空，代价可忽略。

        **候选渠道只信模型目录**（`_narrow_providers` 的收窄结果，即模型
        对应缓存列表）：目录登记了该模型的渠道全部不可用时直接判「无可用
        凭证」，不再放宽到没登记它的渠道——那只是拿请求逐个渠道试错。
        实测（2026-10-10 `longcat-2.5-preview`）：目录里只有 zen 登记它，
        zen 匿名免费层 429 让虚拟凭证进 60s 账号级冷却后，旧逻辑每次请求都
        放宽扇出到 CB/TRAE/kilo/CodeArts，各回 11102/4001/401/404——客户端
        拿到误导性的 400 而非「该渠道暂时不可用」，还给 CodeBuddy 写下 6
        小时起步的 (凭证, 模型) 负缓存。目录证不了归属时（模型不在任何渠道
        的登记表里）`_narrow_providers` 本就放行全部候选，不存在「漏试真正
        持有它的新渠道」——模型列表 TTL 300s + 后台兜底刷新即新鲜度上限。
        """
        registered = self._registered(target)
        if not registered:
            raise NoProviderForModel(f"no provider registered for model {target.model!r}")
        now = int(time.time())
        usable = self._selectable(self._deps.credentials.candidates(registered),
                                  target, now)
        if not usable:
            return None
        return (self._sticky(usable, tried, affinity_id)
                or self._deps.scheduler.select(usable, tried, now))

    def _selectable(self, candidates: list, target: ModelTarget, now: int) -> list:
        """按「该凭证所属上游的原始模型名」过滤出当前可选的候选。

        顺手把**当前模型**的消耗倍率按渠道注进候选快照（free-tier 优先档的
        数据源，见 Scheduler.select）。候选集（就是 `_select` 按模型目录
        收窄后的那些渠道）在此一次注入。

        另按限额护栏二次过滤（`deps.limits`，未装配则跳过）：被限额停用的
        凭证**只剩它时**会走到「无可用凭证」503——与「全部因模型级冷却」区分，
        后者仍报 model_unavailable（限额是账号级原因，换个模型也未必能用）。

        再按在途租约过滤（`deps.inflight`，未装配则跳过）：并发已达上限的
        凭证本轮不参与选号，避免把单账号打进上游限流/封号。同一份在途计数
        顺手注入候选快照（`Candidate.inflight`），供调度器做「最少连接」负载
        均衡——未装配时恒为 0，排序退回原链。
        """
        rates = self._model_rates(target)
        guard = self._deps.limits
        inflight = self._deps.inflight
        selected = [
            c for c in candidates
            if c.is_selectable(now, self._model_scope(c.provider, target.model))
            and (guard is None or not guard.blocked(c, target.model))
            and (inflight is None or not inflight.blocked(c.credential_id))
        ]
        if inflight is not None:
            selected = [replace(c, inflight=inflight.count(c.credential_id))
                        for c in selected]
        if not rates:
            return selected
        return [
            replace(c, credit_rate=rates[c.provider]) if c.provider in rates else c
            for c in selected
        ]

    def _lease(self, credential_id: str) -> None:
        """请求开始：登记一个在途租约（未装配护栏则跳过）。"""
        if self._deps.inflight is not None:
            self._deps.inflight.acquire(credential_id)

    def _release(self, credential_id: str) -> None:
        """请求结束（成功/轮换/异常/断开）：释放一个在途租约（幂等）。"""
        if self._deps.inflight is not None:
            self._deps.inflight.release(credential_id)

    def _note_upstream_error(self, credential_id: str, kind: ErrKind,
                             provider_id: str, model: str,
                             error: Exception | None = None, *,
                             username: str, started: float) -> bool:
        """对一次上游错误作出反应：记账 + 落库（DEAD 另落一条统计），REQUEST 类零动作。

        返回值 = **本次是否落了尝试级明细**（仅 DEAD 为真）。调用方据此在轮换
        耗尽时跳过收尾行，避免同一个请求在 `requests` 里被算两次、Key 配额被
        扣两次。

        请求级错误（11101 请求体坏 / 11115 上下文超限 / 11128 渠道风控 /
        11135 图片无效）不是账号的问题：冷却或累计错误数都会在阈值处把健康
        凭证踢掉。
        也绝不落库——save_error 会把已有 cooling_until 写成 NULL，
        等于顺手解掉别的错误留下的冷却。调用方只换号重试。

        **DEAD 额外落一条 `credential_unavailable` 明细**：硬禁用只写
        `credentials.disabled`，归因卡片读的是 `usage_events`，不落明细就完全
        看不到（实测 2026-10-23 officeace 两次 401 `AppKey or AppSecret is
        invalid` 在统计页零痕迹）。与 `invalid_request` 同口径：**每次尝试**
        落一行，换号成功后由 `errors()` 的 `rescued` 标为「已救回」。其他分类
        （SOFT / CREDIT / PLAN / MODEL）不在此落行——它们每次请求可能触发多次，
        落行会把明细表灌满噪声；这些请求的失败仍由轮换耗尽时的收尾行覆盖。

        **换号耗尽时收尾行不再落**（调用方看返回值）：否则一次请求两行，
        `usage_hourly.requests` 虚高、成功率虚低，按 Key 计的日请求额度也会
        被扣两次。DEAD 尝试行已经是这次失败的完整归因。

        `error` 带 `retry_at` 时（上游 `Retry-After`，见 provider.base）交给
        调度器在**该错误分类的作用域**内覆盖冷却时长；缺失/过去时刻回落常量。
        """
        if kind is ErrKind.REQUEST:
            return False
        outcome = self._deps.scheduler.note_error(
            self._candidate(credential_id), kind, int(time.time()),
            model=self._model_scope(provider_id, model),
            retry_at=getattr(error, "retry_at", None))
        self._deps.credentials.save_error(credential_id, outcome)
        if kind is not ErrKind.DEAD:
            return False
        self._deps.record(
            username=username, provider=provider_id, credential_id=credential_id,
            model=model, ok=False, error_type=_error_type_for(kind),
            latency_ms=_elapsed_ms(started))
        return True

    def _queue_wait(self, retry_after: float | None, emitted: bool,
                    rounds: int) -> float | None:
        """排队信号 → 本轮等待秒数；不该重开时返回 None（调用方降级为 SOFT）。

        三条约束：

        - **已出帧不重开**：半截输出不可回滚，重开会把两个号的内容拼进同一条
          回复。此时返回 None，让调用方按 SOFT 收尾。
        - **轮次上限**：`QUEUE_MAX_ROUNDS` 用尽后返回 None，否则持续排队的
          上游会把客户端连接吊死到它自己的超时。
        - **单轮封顶**：上游可能点名「2 小时后再来」，照等等于把请求挂死；
          按 `QUEUE_MAX_WAIT_SECONDS` 封顶，超了先等一轮再说。
        """
        if emitted or rounds >= QUEUE_MAX_ROUNDS:
            return None
        wait = QUEUE_DEFAULT_WAIT_SECONDS if retry_after is None else float(retry_after)
        return min(max(wait, 0.0), QUEUE_MAX_WAIT_SECONDS)

    def _plan_queue_restart(
        self, retry_after: float | None, *, emitted: bool, rounds: int,
        attempts: int, provider_id: str, credential_id: str,
        credential_data: dict[str, Any],
    ) -> _QueueRestart | None:
        """排队信号 → 重开计划；不该重开时返回 None（调用方降级为 SOFT）。

        四个入口（流式事件 / 流式异常 / 非流式 `_Queued` / 非流式异常）原本各
        写一份「计数 + 记 pinned + 打日志」，收敛到这里保证四处的预算口径一致：

        - `attempts -= 1`：排队不是「换号尝试」。不归还的话三次排队就把
          `max_rotate` 吃满，请求直接 503。
        - `pinned` 记下被点名的凭证，下一轮跳过 `_pick` 直接用——否则可能选到
          别的号，排队语义就丢了。
        """
        wait = self._queue_wait(retry_after, emitted, rounds)
        if wait is None:
            return None
        rounds += 1
        logger.info("上游 %s 排队中（凭证 %s，第 %s/%s 轮），%.1fs 后原号重开",
                    provider_id, credential_id, rounds, QUEUE_MAX_ROUNDS, wait)
        return _QueueRestart(wait=wait, rounds=rounds, attempts=attempts - 1,
                             pinned=(credential_id, credential_data))

    def _model_scope(self, provider_id: str, model: str) -> str:
        """冷却表登记的模型名：用上游原始名（与 provider 实际调用的名字一致）。"""
        return self._upstream_model(provider_id, model)

@dataclass(slots=True)
class _StreamState:
    """流式轮换过程中需要跨尝试保留的状态（统计用）。"""

    translator: StreamSink
    started: float
    username: str
    provider: str = "-"
    credential_id: str | None = None
    affinity_id: str | None = None
    _first_byte_at: float | None = None
    # 已记账标记：断开分支防与正常成功路径重复记账
    recorded: bool = False
    # 流式超时的截止时刻（monotonic）：None = 下一次取事件时重新计时。
    # 只被数据事件重置（见 `_is_data_event`），跨尝试由 _stream_loop 显式清空
    deadline: float | None = None

    def mark_first_byte(self) -> None:
        if self._first_byte_at is None:
            self._first_byte_at = time.monotonic()

    def ttfb_ms(self) -> int | None:
        if self._first_byte_at is None:
            return None
        return int((self._first_byte_at - self.started) * 1000)


def _elapsed_ms(started: float, end: float | None = None) -> int:
    return int(((end if end is not None else time.monotonic()) - started) * 1000)


def _is_data_event(event: Event) -> bool:
    """该事件是否算「数据」（重置流式空闲计时）。

    只认真正推进响应的帧：正文 / 思考 / 工具调用 / usage / 结束。空事件
    （所有载荷字段都为空的 delta）与流内 error 都不算——否则上游只要每
    N 秒发一个空 delta，空闲计时就永远不触发，超时形同虚设。
    """
    if event.kind in (EventKind.CONTENT, EventKind.REASONING):
        return bool(event.content)
    if event.kind is EventKind.TOOL_CALLS:
        return bool(event.tool_calls)
    if event.kind is EventKind.USAGE:
        return event.usage is not None
    return event.kind is EventKind.FINISH


def _event_kind(event: Event) -> ErrKind:
    """流内 error 事件的业务分类：provider 解析时已定（Event.error_kind）。

    缺失（无码事件 / 测试桩未带分类）按 OTHER 处理。口径与
    `UpstreamStreamError.kind()` 共用 `event_error_kind`，避免两处兜底漂移。
    """
    return event_error_kind(event)


def _classify(error: Exception) -> ErrKind | None:
    """上游错误 → ErrKind；不认识的原样上抛。

    `kind()` 优先（provider 自己按上游错误码语义分类）。之后兜住
    `httpx.TransportError`：连接超时 / DNS 失败 / 写请求体超时 / 读超时
    都是链路瞬态，与具体账号无关，按 SOFT 短冷却换号重试。

    **为什么统一放在这里，而不是让每个 provider 各自包装**：httpx 的传输
    异常会从 `client.stream()` 的 `__aenter__`、`aiter_bytes()`、写请求体
    等多个位置抛出，逐点 `try/except` 必然漏掉其中某个；而且 6 个渠道各写
    一遍，新增渠道还会再漏一次。分类是「传输层失败」这一件事的公共语义，
    放在唯一的错误出口最省也最不容易漏。

    刻意**不**泛化到 `OSError` / `ConnectionError`：文件系统类错误（磁盘满、
    句柄耗尽）也是它们的子类，误判成上游故障会去冷却一个完全健康的凭证。
    `UpstreamTransportError` 自带 `kind()`，已被上面第一段覆盖。

    **`ssl.SSLError` 单独列出**：TLS 层的 record layer failure / 证书链握手
    失败在 anyio 的 `_call_sslobject_method` 里只有 `SSLEOFError` 被转成
    `BrokenResourceError`，其余 `SSLError` 原样上抛（见 anyio/streams/tls.py）。
    它是 `OSError` 子类但**不是** `httpx.TransportError`，落不进上面的判断——
    实测 2026-10-25 15:50 两次 codebuddy_intl 走 7897 代理时 `[SSL] record
    layer failure` 直接逃到 `stream_guarded`，表现为一帧 `internal_error`：
    不换号、不冷却、不记统计，失败归因面板里连痕迹都没有。TLS 中断与读中断
    同属链路瞬态，归 SOFT。仍然只认**具体类**，不放开到 `OSError` 整体。
    """
    kind_method = getattr(error, "kind", None)
    if callable(kind_method):
        return kind_method()
    if isinstance(error, (httpx.TransportError, ssl.SSLError)):
        return ErrKind.SOFT
    return None


def _error_detail(error: Exception | None) -> str:
    """服务端日志用的错误描述：`异常类名: 消息`。

    为什么不能只 `%s` 格式化异常：`httpx` 的传输异常经常 `str()` 为空
    （`Server disconnected without sending a response.` 之外的大量读超时 /
    写超时都是空串），日志只剩「kind=soft）: 」，排障时分不清是读断、写超时
    还是连接重置（实测 2026-10-23 codebuddy_intl 两次 158s/162s 的 SOFT 错误
    都是空正文）。类名本身不含用户数据与上游正文，与既有 `body_hint` 截断
    正文的口径一致，只进服务端日志。
    """
    if error is None:
        return "-"
    name = type(error).__name__
    text = str(error)
    return f"{name}: {text}" if text else name


def _error_summary(error: Exception | None) -> str:
    """对外错误文案里的错误标识：**只给受控分类，不回显上游正文**。

    上游错误体可能包含上游内部 schema、请求回显，甚至（若上游回显请求头）
    credential/token 片段；透传给任意 API 消费者等于信息泄露。完整正文只进
    服务端日志（见各 provider 的 warning）。这里给调用方可据以行动的短标识：
    HTTP 状态码 / 上游业务码 / 异常类名。
    """
    if error is None:
        return ""
    summary = getattr(error, "summary", None)
    if isinstance(summary, str) and summary:
        return summary
    status = getattr(error, "status", None)
    if isinstance(status, int) and status > 0:
        return f"upstream status {status}"
    code = getattr(error, "error_code", None)
    if code:
        return f"upstream code {code}"
    return type(error).__name__


def _unavailable_message(last_error: Exception | None) -> str:
    """503 文案：带上最后一次错误的**受控标识**便于排查（不回显上游正文）。"""
    message = "all credentials unavailable"
    summary = _error_summary(last_error)
    if summary:
        message += f": {summary}"
    return message


def _model_cooled_message(model: str, last_error: Exception | None,
                          suggestions: list[str] | None = None) -> str:
    """模型级冷却耗尽候选时的 503 文案（账号没问题，只是这个模型暂不可用）。

    与 `_unavailable_message`（凭证整体不可用）区分：上游把该模型限流或
    其执行节点故障时，冷却只锁这一个模型，换模型即可用——提示要指着模型。
    """
    message = (f"model {model!r} temporarily unavailable on upstream "
               f"(cooling down; retry later or use another model)")
    summary = _error_summary(last_error)
    if summary:
        message += f": {summary}"
    if suggestions:
        message += f" (similar available models: {', '.join(suggestions)})"
    return message


def _stream_error_frame(translator: StreamSink | None, message: str, code: str) -> bytes:
    """流已开始后的错误帧；形状由出口的 translator 决定（chat / Responses）。"""
    builder = getattr(translator, "error_frame", None)
    if callable(builder):
        return builder(message, code)
    return stream_error_frame(message, code)


def _usage_field(usage: object, name: str) -> object:
    """上游可能完全没有 usage 帧，统计字段要容忍缺失。"""
    return getattr(usage, name, None) if usage is not None else None


def _reject_message(model: str, last_error: Exception | None,
                    suggestions: list[str] | None = None) -> str:
    """所有上游都拒绝该模型时的 400 文案（只带受控标识，不回显上游正文）。"""
    summary = _error_summary(last_error)
    detail = f": {summary}" if summary else ""
    message = f"model {model!r} not available on any configured upstream{detail}"
    if suggestions:
        message += f" (similar available models: {', '.join(suggestions)})"
    return message


def _stream_error_type(error: Exception | None) -> str:
    """流已开始后的失败类型：流式超时单独成类，其余沿用通用映射。

    `_StreamTimeout.kind()` 是 SOFT（冷却口径），但统计页要把「上游首字超时」
    与「上游流中断」分开看（A1 验收），所以这里先按异常类型分流，再落到
    `_error_type_for`。三个取值都在 collector 的受控白名单里。
    """
    if isinstance(error, _StreamTimeout):
        return error.error_code
    kind = _classify(error) if error is not None else None
    return _error_type_for(kind) if kind else "upstream_protocol"


def _error_type_for(kind: ErrKind) -> str:
    """ErrKind → 受控的统计失败类型（web/src/api/display.ts 同步维护）。"""
    if kind in (ErrKind.PLAN, ErrKind.CREDIT, ErrKind.MODEL,
                ErrKind.CONCURRENCY, ErrKind.QUEUE):
        # MODEL / CONCURRENCY 都是模型级限流，展示口径同为额度类；
        # QUEUE 是「上游排队中」，同属容量不足，与额度类同档展示。
        # 冷却作用域与时长差异在 schema 层
        return "rate_limit"
    if kind is ErrKind.DEAD:
        return "credential_unavailable"
    if kind is ErrKind.BLOCKED:
        # 「该后端无此模型」（11102 / 81004 / 81009 / InferHub 002002009）：
        # **不是**请求无效。归到 `invalid_request` 会让归因面板把「上游没这个
        # 模型」读成「客户端参数写错了」（实测 2026-10-26 officeAce
        # `deepseek-v4.1-flash-1017` 404 `ModelArts.81009` 就是被标成
        # 「请求无效」）。独立成类才能让运维一眼看出是模型下线而非请求错。
        return "model_not_found"
    if kind in (ErrKind.INVALID, ErrKind.REQUEST):
        return "invalid_request"
    return "upstream_error"


