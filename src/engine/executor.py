"""执行引擎：选号 → 上游流 → 轮换重试 → 统计（Q12=B 轮换 ≤3 次）。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from ..compat.openai.errors import UpstreamStreamError, stream_error_frame
from ..compat.openai.request import ChatRequest, InvalidRequest
from ..compat.openai.response import (
    StreamTranslator,
    aggregate,
)
from ..config import live
from ..db.repo import CredentialRepository
from ..provider.base import ErrKind, Event, EventKind, Usage, aclose_stream
from .continuation import ContinuationStream
from .model_resolver import (
    ModelTarget,
    UnknownModelError,
    ordered_fallback_chain,
    resolve,
)
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
    # 非流式聚合整体超时（秒）：流式路径有心跳与 read=None 兜底，非流式
    # 聚合没有——上游连接半开停滞会让请求无限悬挂并占住凭证。超时按瞬态
    # 错误（SOFT：短冷却不累计）换号重试；≤0 关闭
    complete_timeout_seconds: float = 600
    # 跨渠道 fallback 兼容组（P1-5）：配置文本 → 组的解析值（dict）；None 表示
    # 关闭。零参 callable 时每次请求现读（热更）。空 dict 与 None 等价（无组）。
    fallback_groups: Any | None = None

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

    def _all_model_cooled(self, target: ModelTarget) -> bool:
        """候选凭证是否**全部**因该模型的模型级冷却而不可选（账号本身可选）。

        只认模型级冷却：账号级冷却/禁用属于「凭证不可用」，不能冒充模型故障。
        用于把两种耗尽的文案区分开（见 `_unavailable_text`）。

        调用点在 `_pick` 返回 None 之后，此时 `_select` 已保证至少注册了一个
        上游（全无注册时它会先抛 NoProviderForModel），故无需再判空上游。
        """
        registered = [pid for pid in self._narrow_providers(target)
                      if pid in self._deps.providers]
        candidates = self._deps.credentials.candidates(registered)
        if not candidates:
            return False
        now = int(time.time())
        for candidate in candidates:
            scope = self._model_scope(candidate.provider, target.model)
            if candidate.is_selectable(now, scope):
                return False                        # 还有可用候选（只是都试过了）
            if not candidate.is_selectable(now):    # 账号级原因 → 非模型故障
                return False
        return True

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
        """请求模型的回退链（P1-5）；未配置或未命中任何组时只有它自己。

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
        """按 API Key 的渠道绑定收窄候选上游（B3.5）。

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
        `stream` 逐链项尝试（P1-5）。
        """
        target = self.resolve_target(request, provider_binding)
        for attempt in self._fallback_chain(target):
            if [pid for pid in self._narrow_providers(attempt)
                    if pid in self._deps.providers]:
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

        启用兼容组回退链（P1-5）时，**只有在当前链项还没产出任何响应帧之前**
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
        assert exhausted is not None
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
        # 本链项是否已向客户端出过响应帧：出了就绝不再换模型（半截输出不可回滚）
        emitted = False

        def terminal(message: str, code: str) -> bytes:
            """终帧：本项未出帧时抛信号让外层试下一链项；已出帧则写错误帧收尾。"""
            if not emitted:
                raise _ModelExhausted(message, code)
            return state.translator.error_frame(message, code)

        while True:
            pick = self._pick(target, tried, state.affinity_id)
            if pick is None:
                if last_kind is ErrKind.INVALID:
                    # 所有候选上游都拒绝了该模型：400 语义而非 503
                    yield terminal(
                        _reject_message(target.model, last_error,
                                        self._suggestions(target.model)),
                        "invalid_request")
                    return
                self._deps.record(
                    username=state.username, provider=state.provider,
                    credential_id=state.credential_id, model=target.model, ok=False,
                    error_type="no_healthy_credential",
                    latency_ms=_elapsed_ms(state.started))
                yield terminal(self._unavailable_text(target, last_error),
                               "no_healthy_credential")
                return
            credential_id, credential_data = pick
            provider_id = self._deps.credentials.provider_of(credential_id)
            state.provider, state.credential_id = provider_id or "-", credential_id
            tried.add(credential_id)
            attempts += 1
            # 显式持有迭代器并在 finally 里关闭：下面两处 `break`（流内错误）
            # 只跳出 `async for`，不会关闭上游 async generator，provider 的
            # 节流名额归还（pacer.release 在其 finally 里）会推迟到 GC。
            # 轮换重试每次都要重新 wait_turn，泄漏累积到 max_concurrency 后
            # 新请求永久阻塞——「用了三次就限制」。见 aclose_stream 注释。
            iterator = self._stream_source(
                provider_id, credential_data, request.raw, target.model).__aiter__()
            try:
                async for event in iterator:
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
                        emitted = True
                        yield frame
                else:
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
            finally:
                # break / 异常 / 客户端断开都走到这里：同步归还节流名额
                await aclose_stream(iterator)
            if not self._deps.scheduler.should_rotate(attempts):
                if last_kind is ErrKind.INVALID:
                    # 流已开始（200 已发出），以 invalid_request 错误帧结束
                    yield terminal(
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
                yield terminal(self._unavailable_text(target, last_error),
                               "no_healthy_credential")
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
        # 续写上限可热更（B3.2）：0 表示关闭，每次请求读当前值
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
        的选号 / 冷却 / 轮换 / 会话粘性 / 统计全部沿用既有路径（P1-5）。
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
        assert last_error is not None          # 链至少一项，且必然以异常收尾否则已 return
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
                    self._unavailable_text(target, last_error))
            credential_id, credential_data = pick
            tried.add(credential_id)
            attempts += 1
            provider_id = self._deps.credentials.provider_of(credential_id)
            last_provider, last_credential = provider_id or "-", credential_id
            events: list[Event] = []
            # 与 _stream_loop 同理：显式关闭上游流，让节流名额在每次尝试
            # 结束时同步归还，不依赖 GC 的 asyncgen finalize
            iterator = self._stream_source(
                provider_id, credential_data, request.raw, target.model).__aiter__()
            try:
                # 聚合整体超时兜底（见 ExecutorDeps.complete_timeout_seconds）
                async with (asyncio.timeout(self._deps.complete_timeout_seconds)
                            if self._deps.complete_timeout_seconds > 0
                            else contextlib.nullcontext()):
                    async for event in iterator:
                        if first_event_at is None:
                            first_event_at = time.monotonic()
                        events.append(event)
            except TimeoutError:
                logger.warning(
                    "上游 %s 非流式聚合超过 %ss（凭证 %s，按瞬态错误换号）",
                    provider_id, self._deps.complete_timeout_seconds, credential_id)
                self._note_upstream_error(credential_id, ErrKind.SOFT,
                                          provider_id, target.model)
                last_error = UpstreamStreamError(Event(
                    kind=EventKind.ERROR,
                    error_message=(f"upstream complete timed out after "
                                   f"{self._deps.complete_timeout_seconds}s")))
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
                        latency_ms=int((time.monotonic() - started) * 1000))
                    return result
            finally:
                await aclose_stream(iterator)
            if not self._deps.scheduler.should_rotate(attempts):
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
                    self._unavailable_text(target, last_error))

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
        now = int(time.time())
        usable = self._selectable(self._deps.credentials.candidates(registered),
                                  target, now)
        if not usable:
            # 收窄后的候选**全部不可用**，不能就此判「无可用渠道」：目录可能陈旧
            # 或降级——某渠道新增了该模型但别名表还没更新（TTL 内），或该渠道
            # 的模型列表回退了静态表而丢掉该模型。此时退回 target 的原始候选集
            # 再试一次，让真正持有该模型且有可用凭证的渠道兜底（CodeArts 无凭证
            # 时回落到 CodeBuddy/TRAE，而不是直接 503）。
            # 强制/@绑定 的 target 候选本就是单一渠道，`broader` 不会更宽，
            # 因此「强制指定出不回退」的语义不受影响。
            broader = [pid for pid in target.providers if pid in self._deps.providers]
            if len(broader) > len(registered):
                usable = self._selectable(self._deps.credentials.candidates(broader),
                                          target, now)
        if not usable:
            return None
        return (self._sticky(usable, tried, affinity_id)
                or self._deps.scheduler.select(usable, tried, now))

    def _selectable(self, candidates: list, target: ModelTarget, now: int) -> list:
        """按「该凭证所属上游的原始模型名」过滤出当前可选的候选。"""
        return [
            c for c in candidates
            if c.is_selectable(now, self._model_scope(c.provider, target.model))
        ]

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

    translator: StreamSink
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
    """流内 error 事件的业务分类：provider 解析时已定（Event.error_kind）。

    缺失（无码事件 / 测试桩未带分类）按 OTHER 处理。
    """
    return event.error_kind if event.error_kind is not None else ErrKind.OTHER


def _classify(error: Exception) -> ErrKind | None:
    """上游 HTTP 错误 → ErrKind；不认识的原样上抛。"""
    kind_method = getattr(error, "kind", None)
    if callable(kind_method):
        return kind_method()
    return None


def _error_summary(error: Exception | None) -> str:
    """对外错误文案里的错误标识：**只给受控分类，不回显上游正文**（M2）。

    上游错误体可能包含上游内部 schema、请求回显，甚至（若上游回显请求头）
    credential/token 片段；透传给任意 API 消费者等于信息泄露。完整正文只进
    服务端日志（见各 provider 的 warning）。这里给调用方可据以行动的短标识：
    HTTP 状态码 / 上游业务码 / 异常类名。
    """
    if error is None:
        return ""
    status = getattr(error, "status", None)
    if isinstance(status, int) and status > 0:
        return f"upstream status {status}"
    code = getattr(error, "error_code", None)
    if code:
        return f"upstream code {code}"
    return type(error).__name__


def _unavailable_message(last_error: Exception | None) -> str:
    """503 文案：带上最后一次错误的**受控标识**便于排查（M2：不回显上游正文）。"""
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
    """所有上游都拒绝该模型时的 400 文案（M2：只带受控标识，不回显上游正文）。"""
    summary = _error_summary(last_error)
    detail = f": {summary}" if summary else ""
    message = f"model {model!r} not available on any configured upstream{detail}"
    if suggestions:
        message += f" (similar available models: {', '.join(suggestions)})"
    return message


def _error_type_for(kind: ErrKind) -> str:
    """ErrKind → 受控的统计失败类型（web/src/api/display.ts 同步维护）。"""
    if kind in (ErrKind.PLAN, ErrKind.CREDIT, ErrKind.MODEL, ErrKind.CONCURRENCY):
        # MODEL / CONCURRENCY 都是模型级限流，展示口径同为额度类；
        # 冷却作用域与时长差异在 schema 层
        return "rate_limit"
    if kind is ErrKind.DEAD:
        return "credential_unavailable"
    if kind in (ErrKind.INVALID, ErrKind.BLOCKED, ErrKind.REQUEST):
        return "invalid_request"
    return "upstream_error"


