"""执行引擎：选号 → 上游流 → 轮换重试 → 统计（Q12=B 轮换 ≤3 次）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..compat.openai.errors import UpstreamStreamError
from ..compat.openai.request import ChatRequest, InvalidRequest
from ..compat.openai.response import (
    StreamTranslator,
    aggregate,
)
from ..db.repo import CredentialRepository
from ..provider.base import ErrKind, Event, EventKind
from .continuation import ContinuationStream
from .model_resolver import ModelTarget, resolve
from .scheduler import Scheduler

logger = logging.getLogger(__name__)


class NoHealthyCredential(Exception):
    pass


class NoProviderForModel(Exception):
    pass


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
    # 会话粘性（ConversationAffinity）；None 表示关闭。对话进行中固定用原
    # 凭证，出错才轮换，成功后重新粘定实际服务的凭证
    affinity: Any | None = None
    # 截断续写上限（B1.4）：上游 finish_reason=length 时同凭证续写，最多该次数；
    # 0 关闭。续写在事件流包装器内完成，不参与凭证轮换
    max_auto_continues: int = 0

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

    def _record_invalid(self, username: str, provider_id: str, credential_id: str,
                        model: str, started: float, error: object) -> None:
        """请求无效：只记统计（invalid_request），绝不冷却/禁用凭证。"""
        self._deps.record(
            username=username, provider=provider_id, credential_id=credential_id,
            model=model, ok=False, error_type="invalid_request",
            latency_ms=int((time.monotonic() - started) * 1000))

    def _suggestions(self, model: str) -> list[str] | None:
        """400 时给用户的相近模型建议（未注入或无候选则 None）。"""
        if self._deps.model_suggestions is None:
            return None
        try:
            return self._deps.model_suggestions(model) or None
        except Exception:  # noqa: BLE001 - 建议失败不影响主错误
            return None

    def _skip_provider(self, provider_id: str, tried: set[str]) -> None:
        """INVALID 后跳过该上游：把它的全部凭证都标记为已试。"""
        for candidate in self._deps.credentials.candidates([provider_id]):
            tried.add(candidate.credential_id)

    def _upstream_model(self, provider_id: str, model: str) -> str:
        """归一模型名 → 该上游注册的原始 id（大小写变体映射，未知则原样）。"""
        if self._deps.upstream_model_name is None:
            return model
        return self._deps.upstream_model_name(provider_id, model)

    def resolve_target(self, request: ChatRequest) -> ModelTarget:
        return resolve(request.model, self._deps.default_model)

    def preflight(self, request: ChatRequest) -> ModelTarget:
        """流式路由建立 StreamingResponse 之前的前置校验。

        生成器体内的异常发生在响应头（200）已发出之后，只能表现为连接被
        截断——客户端拿到空 body，误以为请求成功。凡是"请求本身不可能成功"
        的错误（未知 provider、模型不属于任何已注册上游）必须在返回
        StreamingResponse 之前抛给异常处理器，才能得到正确的 400。
        """
        target = self.resolve_target(request)
        if not [pid for pid in self._narrow_providers(target) if pid in self._deps.providers]:
            raise NoProviderForModel(f"no provider registered for model {target.model!r}")
        return target

    async def stream_guarded(self, request: ChatRequest, *, username: str = "unknown"
                             ) -> AsyncIterator[bytes]:
        """流式出口的最终兜底：任何逃逸异常都转成 SSE 错误帧。

        没有这一层时，未预期的异常（如凭证在轮换中途被删除）会让连接
        静默断开，客户端无法区分"空回复"与"服务出错"。
        """
        try:
            async for frame in self.stream(request, username=username):
                yield frame
        except (GeneratorExit, asyncio.CancelledError):
            raise                          # 客户端断开：已由 stream() 记账
        except Exception as error:  # noqa: BLE001 - 流已开始，只能以错误帧收尾
            logger.exception("流式响应失败: %s", error)
            yield _error_frame("internal server error", "internal_error")

    async def stream(self, request: ChatRequest, *, username: str = "unknown"
                     ) -> AsyncIterator[bytes]:
        """流式执行；上游错误按分类冷却并换号，最多 3 次。

        客户端中途断开时（生成器被关闭 / 任务被取消）把已产生的用量
        记入统计，标记 client_disconnect：否则统计里的用量低于真实消耗，
        而断开是长回复场景下的常态。

        例外：完整响应（[DONE]）已产出后的断开不算中途——客户端拿到
        回调后立即关闭连接是正常收尾（SSE 流在 [DONE] 发出到结束帧
        more_body=False 之间有一拍竞态，框架会把它当断开），按成功记账。
        """
        target = self.resolve_target(request)
        state = _StreamState(translator=StreamTranslator(target.model),
                             started=time.monotonic(), username=username)
        if self._deps.affinity is not None:
            state.affinity_id = self._deps.affinity.pin_for(request.raw, username)
        try:
            async for frame in self._stream_loop(request, target, state):
                yield frame
        except (GeneratorExit, asyncio.CancelledError):
            if not state.recorded and (state.translator.usage is not None
                                       or state._first_byte_at is not None):
                if state.translator.done_sent:
                    # [DONE] 已产出：客户端收尾断开，按成功记账（tokens 如实记录）
                    if state.credential_id is not None:
                        self._deps.credentials.save_success(state.credential_id)
                    self._record_success(target, state, state.provider,
                                         state.credential_id or "-")
                else:
                    self._record_disconnect(target, state)
            raise

    async def _stream_loop(self, request: ChatRequest, target: ModelTarget,
                           state: _StreamState) -> AsyncIterator[bytes]:
        tried: set[str] = set()
        last_error: Exception | None = None
        last_kind: ErrKind | None = None

        while True:
            pick = self._pick(target, tried, state.affinity_id)
            if pick is None:
                if last_kind is ErrKind.INVALID:
                    # 所有候选上游都拒绝了该模型：400 语义而非 503
                    yield _error_frame(
                        _reject_message(target.model, last_error,
                                        self._suggestions(target.model)),
                        "invalid_request")
                    return
                self._deps.record(
                    username=state.username, provider=state.provider,
                    credential_id=state.credential_id, model=target.model, ok=False,
                    error_type="no_healthy_credential",
                    latency_ms=_elapsed_ms(state.started))
                yield _unavailable_frame(last_error)
                return
            credential_id, credential_data = pick
            provider_id = self._deps.credentials.provider_of(credential_id)
            state.provider, state.credential_id = provider_id or "-", credential_id
            tried.add(credential_id)
            try:
                async for event in self._stream_source(
                    provider_id, credential_data, request.raw, target.model,
                ):
                    if event.kind is EventKind.ERROR:
                        kind = _event_kind(event)
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
                            break
                        logger.warning(
                            "上游 %s 流内错误（凭证 %s，kind=%s）: code=%s %s",
                            provider_id, credential_id, kind,
                            event.error_code, event.error_message)
                        self._note_upstream_error(credential_id, kind, provider_id, target.model)
                        last_error = UpstreamStreamError(event)
                        break
                    for frame in state.translator.translate(event):
                        state.mark_first_byte()
                        yield frame
                else:
                    self._deps.credentials.save_success(
                        credential_id, model=self._model_scope(provider_id, target.model))
                    self._remember(request, state.username, credential_id)
                    self._record_success(target, state, provider_id, credential_id)
                    for frame in state.translator.finish():
                        yield frame
                    return
            except Exception as error:  # noqa: BLE001 - 统一转为冷却或上抛
                kind = _classify(error)
                if kind is None:
                    raise
                if kind is ErrKind.INVALID:
                    # 请求无效（如该上游不认识模型）：不冷却凭证，
                    # 跳过该上游继续试其他上游；全部拒绝才以 400 结束
                    logger.warning("上游 %s 拒绝模型 %s（凭证 %s 跳过）: %s",
                                   provider_id, target.model, credential_id, error)
                    self._record_invalid(state.username, provider_id, credential_id,
                                         target.model, state.started, error)
                    last_error, last_kind = error, ErrKind.INVALID
                    self._skip_provider(provider_id, tried)
                else:
                    logger.warning("上游 %s 错误（凭证 %s，kind=%s）: %s",
                                   provider_id, credential_id, kind, error)
                    self._note_upstream_error(credential_id, kind, provider_id, target.model)
                    last_error = error
            if not self._deps.scheduler.should_rotate(tried):
                if last_kind is ErrKind.INVALID:
                    # 流已开始（200 已发出），以 invalid_request 错误帧结束
                    yield _error_frame(
                        _reject_message(target.model, last_error,
                                        self._suggestions(target.model)),
                        "invalid_request")
                    return
                kind = _classify(last_error) if last_error is not None else None
                self._deps.record(
                    username=state.username, provider=provider_id,
                    credential_id=credential_id, model=target.model, ok=False,
                    error_type=_error_type_for(kind) if kind else "upstream_protocol",
                    latency_ms=_elapsed_ms(state.started))
                yield _unavailable_frame(last_error)
                return

    def _stream_source(self, provider_id: str, credential_data: dict[str, Any],
                       payload: dict[str, Any], model: str) -> AsyncIterator[Event]:
        """上游事件流入口；开启截断续写时包一层（B1.4）。

        续写在包装器内固定用同一凭证续发请求，因此凭证轮换/记账/统计
        逻辑完全不需要感知它——executor 只会看到一条更长的流。
        """
        upstream_model = self._upstream_model(provider_id, model)
        stream = self._deps.providers[provider_id].stream_chat(
            credential_data, payload, upstream_model)
        if self._deps.max_auto_continues <= 0:
            return stream
        return ContinuationStream(
            self._deps.providers[provider_id], credential_data, payload,
            upstream_model, max_continues=self._deps.max_auto_continues)

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

    async def complete(self, request: ChatRequest, *, username: str = "unknown"
                       ) -> dict[str, Any]:
        """非流式：聚合同一执行路径的事件。流内错误会触发换号重试。"""
        target = self.resolve_target(request)
        tried: set[str] = set()
        last_error: Exception | None = None
        last_kind: ErrKind | None = None
        started = time.monotonic()
        last_provider = "-"
        last_credential: str | None = None
        # 首事件时刻：上游响应的第一个事件（TTFE，非流式的首字延迟）；跨重试只记最早一次
        first_event_at: float | None = None
        # 会话粘性：对话上一轮用过哪个凭证，本轮优先复用
        affinity_id = (self._deps.affinity.pin_for(request.raw, username)
                       if self._deps.affinity is not None else None)

        while True:
            pick = self._pick(target, tried, affinity_id)
            if pick is None:
                if last_kind is ErrKind.INVALID:
                    # 所有候选上游都拒绝了该模型：400 而非 503
                    raise InvalidRequest(_reject_message(
                        target.model, last_error, self._suggestions(target.model)))
                self._deps.record(
                    username=username, provider=last_provider, credential_id=last_credential,
                    model=target.model, ok=False, error_type="no_healthy_credential",
                    latency_ms=int((time.monotonic() - started) * 1000))
                raise NoHealthyCredential(
                    f"all credentials unavailable: {last_error}" if last_error
                    else "all credentials unavailable")
            credential_id, credential_data = pick
            tried.add(credential_id)
            provider_id = self._deps.credentials.provider_of(credential_id)
            last_provider, last_credential = provider_id or "-", credential_id
            events: list[Event] = []
            try:
                async for event in self._stream_source(
                    provider_id, credential_data, request.raw, target.model,
                ):
                    if first_event_at is None:
                        first_event_at = time.monotonic()
                    events.append(event)
            except Exception as error:  # noqa: BLE001
                kind = _classify(error)
                if kind is None:
                    raise
                if kind is ErrKind.INVALID:
                    # 请求无效（如该上游不认识模型）：不冷却凭证，
                    # 跳过该上游继续试其他上游；全部拒绝才以 400 结束
                    logger.warning("上游 %s 拒绝模型 %s（凭证 %s 跳过）: %s",
                                   provider_id, target.model, credential_id, error)
                    self._record_invalid(username, provider_id, credential_id,
                                         target.model, started, error)
                    last_error, last_kind = error, ErrKind.INVALID
                    self._skip_provider(provider_id, tried)
                else:
                    logger.warning("上游 %s 错误（凭证 %s，kind=%s）: %s",
                                   provider_id, credential_id, kind, error)
                    self._note_upstream_error(credential_id, kind, provider_id, target.model)
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
                        self._note_upstream_error(credential_id, kind, provider_id, target.model)
                        last_error = error
                else:
                    self._deps.credentials.save_success(
                        credential_id, model=self._model_scope(provider_id, target.model))
                    self._remember(request, username, credential_id)
                    usage = result.get("usage") or {}
                    self._deps.record(
                        username=username, provider=provider_id,
                        credential_id=credential_id, model=target.model, ok=True,
                        input_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                        reasoning_tokens=(usage.get("completion_tokens_details") or {})
                        .get("reasoning_tokens"),
                        cached_tokens=(usage.get("prompt_tokens_details") or {})
                        .get("cached_tokens"),
                        ttfb_ms=(int((first_event_at - started) * 1000)
                                 if first_event_at is not None else None),
                        latency_ms=int((time.monotonic() - started) * 1000))
                    return result
            if not self._deps.scheduler.should_rotate(tried):
                if last_kind is ErrKind.INVALID:
                    raise InvalidRequest(_reject_message(
                        target.model, last_error, self._suggestions(target.model)))
                kind = _classify(last_error) if last_error is not None else None
                self._deps.record(
                    username=username, provider=provider_id, credential_id=credential_id,
                    model=target.model, ok=False,
                    error_type=_error_type_for(kind) if kind else "upstream_protocol",
                    latency_ms=int((time.monotonic() - started) * 1000))
                raise NoHealthyCredential(
                    f"all credentials unavailable: {last_error}" if last_error
                    else "all credentials unavailable")

    # -------------------------------------------------------------- 内部

    def _remember(self, request: ChatRequest, username: str, credential_id: str) -> None:
        """成功后把本对话粘到实际服务的凭证（轮换降级后随之换粘）。"""
        if self._deps.affinity is not None:
            self._deps.affinity.remember(request.raw, username, credential_id)

    def _pick(self, target: ModelTarget, tried: set[str],
              affinity_id: str | None = None):
        credential_id = self._select(target, tried, affinity_id)
        if credential_id is None:
            return None
        credential_data = self._deps.credentials.credential_data(credential_id)
        if credential_data is None:  # 并发删除
            tried.add(credential_id)
            return self._pick(target, tried, affinity_id)
        return credential_id, credential_data

    def _sticky(self, candidates: list, tried: set[str],
                affinity_id: str | None) -> str | None:
        """会话粘性：指纹命中的凭证仍可选时直接复用，不参与排序。

        只校验「在候选池里且未冷却/禁用/本请求已轮换过」；其余情况
        （凭证被删、冷却中、用户强制了别的上游）回退常规调度，成功后
        重新粘定，对话不会因此永久失粘。

        手动 pin 优先于粘性（PROPOSAL §4.3「手动 pin 优先 → 过滤 healthy →
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
        """
        registered = [pid for pid in self._narrow_providers(target)
                      if pid in self._deps.providers]
        if not registered:
            raise NoProviderForModel(f"no provider registered for model {target.model!r}")
        candidates = self._deps.credentials.candidates(registered)
        if not candidates:
            return None
        now = int(time.time())
        usable = [
            c for c in candidates
            if c.is_selectable(now, self._model_scope(c.provider, target.model))
        ]
        if not usable:
            return None
        return (self._sticky(usable, tried, affinity_id)
                or self._deps.scheduler.select(usable, tried, now))

    def _note_upstream_error(self, credential_id: str, kind: ErrKind,
                             provider_id: str, model: str) -> None:
        """对一次上游错误作出反应：记账 + 落库，REQUEST 类零动作。

        请求级错误（11101 请求体坏 / 11115 上下文超限 / 11128 渠道风控 /
        11135 图片无效）不是账号的问题：冷却或累计错误数都会在阈值处把健康
        凭证踢掉。
        也绝不落库——save_error 会把已有 cooling_until 写成 NULL，
        等于顺手解掉别的错误留下的冷却。调用方只换号重试。
        """
        if kind is ErrKind.REQUEST:
            return
        outcome = self._deps.scheduler.note_error(
            self._candidate(credential_id), kind, int(time.time()),
            model=self._model_scope(provider_id, model))
        self._deps.credentials.save_error(credential_id, outcome)

    def _model_scope(self, provider_id: str, model: str) -> str:
        """冷却表登记的模型名：用上游原始名（与 provider 实际调用的名字一致）。"""
        return self._upstream_model(provider_id, model)


@dataclass(slots=True)
class _StreamState:
    """流式轮换过程中需要跨尝试保留的状态（统计用）。"""

    translator: StreamTranslator
    started: float
    username: str
    provider: str = "-"
    credential_id: str | None = None
    affinity_id: str | None = None
    _first_byte_at: float | None = None
    # 已记账标记：断开分支防与正常成功路径重复记账
    recorded: bool = False

    def mark_first_byte(self) -> None:
        if self._first_byte_at is None:
            self._first_byte_at = time.monotonic()

    def ttfb_ms(self) -> int | None:
        if self._first_byte_at is None:
            return None
        return int((self._first_byte_at - self.started) * 1000)


def _elapsed_ms(started: float, end: float | None = None) -> int:
    return int(((end if end is not None else time.monotonic()) - started) * 1000)


def _event_kind(event: Event) -> ErrKind:
    """流内 error 事件的业务码 → ErrKind。

    与 provider 的 `classify_error_code` 同源，但这里再走一遍是因为
    流内事件只带 code 不带 HTTP 状态；两侧映射必须保持一致。
    """
    return _PROVIDER_EVENT_KINDS.get(event.error_code, ErrKind.OTHER)


# 流内错误码 → ErrKind。INVALID（4001）来自 TRAE 实测；其余来自 CodeBuddy
# 业务码语义（见 codebuddy/events.py 的 classify_status 说明）。
_PROVIDER_EVENT_KINDS: dict[int, ErrKind] = {
    1005: ErrKind.PLAN,       # 权益耗尽
    14018: ErrKind.CREDIT,    # 余额不足 → 等签到
    4001: ErrKind.INVALID,    # TRAE 参数/模型不可用 → 跳过该上游
    6004: ErrKind.MODEL,      # 模型级限流 → 只冷却该模型
    11102: ErrKind.BLOCKED,   # 该账号无此模型 → 负缓存
    11101: ErrKind.REQUEST,   # 请求体坏 → 不罚号
    11115: ErrKind.REQUEST,   # 上下文超限 → 不罚号
    11128: ErrKind.REQUEST,   # 渠道风控（瞬时，窗口内自愈）→ 换号重试，不罚号
    11135: ErrKind.REQUEST,   # 图片无效 → 不罚号
}


def _classify(error: Exception) -> ErrKind | None:
    """上游 HTTP 错误 → ErrKind；不认识的原样上抛。"""
    kind_method = getattr(error, "kind", None)
    if callable(kind_method):
        return kind_method()
    return None


def _unavailable_frame(last_error: Exception | None) -> bytes:
    """503 帧：带上最后一次错误便于排查（Q21=C 扁平路由下这是唯一线索）。"""
    message = "all credentials unavailable"
    if last_error is not None:
        message += f": {last_error}"
    return _error_frame(message, "no_healthy_credential")


def _usage_field(usage: object, name: str) -> object:
    """上游可能完全没有 usage 帧，统计字段要容忍缺失。"""
    return getattr(usage, name, None) if usage is not None else None


def _reject_message(model: str, last_error: Exception | None,
                    suggestions: list[str] | None = None) -> str:
    """所有上游都拒绝该模型时的 400 文案。"""
    detail = f": {last_error}" if last_error is not None else ""
    message = f"model {model!r} not available on any configured upstream{detail}"
    if suggestions:
        message += f" (similar available models: {', '.join(suggestions)})"
    return message


def _error_type_for(kind: ErrKind) -> str:
    """ErrKind → 受控的统计失败类型（web/src/api/display.ts 同步维护）。"""
    if kind in (ErrKind.PLAN, ErrKind.CREDIT, ErrKind.MODEL):
        # MODEL 是模型级限流，展示口径同为额度类；冷却作用域差异在 schema 层
        return "rate_limit"
    if kind is ErrKind.DEAD:
        return "credential_unavailable"
    if kind in (ErrKind.INVALID, ErrKind.BLOCKED, ErrKind.REQUEST):
        return "invalid_request"
    return "upstream_error"


def _error_frame(message: str, code: str) -> bytes:
    from ..engine.sse import SSE_DONE, format_openai_frame

    payload = {"error": {"message": message, "type": "api_error", "code": code}}
    return format_openai_frame(json.dumps(payload, ensure_ascii=False)) + SSE_DONE


