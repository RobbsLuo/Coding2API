"""执行引擎：选号 → 上游流 → 轮换重试 → 统计（Q12=B 轮换 ≤3 次）。"""

from __future__ import annotations

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

    async def stream(self, request: ChatRequest, *, username: str = "unknown"
                     ) -> AsyncIterator[bytes]:
        """流式执行；上游错误按分类冷却并换号，最多 3 次。"""
        target = self.resolve_target(request)
        tried: set[str] = set()
        last_error: Exception | None = None
        last_kind: ErrKind | None = None
        translator = StreamTranslator(target.model)
        started = time.monotonic()
        # 轮换耗尽后再次选号会失败，此时统计仍应归到实际尝试过的上游
        last_provider = "-"
        last_credential: str | None = None

        while True:
            pick = self._pick(target, tried)
            if pick is None:
                if last_kind is ErrKind.INVALID:
                    # 所有候选上游都拒绝了该模型：400 语义而非 503
                    yield _error_frame(
                        _reject_message(target.model, last_error,
                                        self._suggestions(target.model)),
                        "invalid_request")
                    return
                self._deps.record(
                    username=username, provider=last_provider, credential_id=last_credential,
                    model=target.model, ok=False, error_type="no_healthy_credential",
                    latency_ms=int((time.monotonic() - started) * 1000))
                yield _unavailable_frame(last_error)
                return
            credential_id, credential_data = pick
            provider_id = self._deps.credentials.provider_of(credential_id)
            last_provider, last_credential = provider_id or "-", credential_id
            tried.add(credential_id)
            try:
                async for event in self._deps.providers[provider_id].stream_chat(
                    credential_data, request.raw, self._upstream_model(provider_id, target.model)
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
                            self._record_invalid(username, provider_id, credential_id,
                                                 target.model, started, event)
                            last_error = UpstreamStreamError(event)
                            last_kind = ErrKind.INVALID
                            self._skip_provider(provider_id, tried)
                            break
                        logger.warning(
                            "上游 %s 流内错误（凭证 %s，kind=%s）: code=%s %s",
                            provider_id, credential_id, kind,
                            event.error_code, event.error_message)
                        outcome = self._deps.scheduler.note_error(
                            self._candidate(credential_id), kind, int(time.time()))
                        self._deps.credentials.save_error(credential_id, outcome)
                        last_error = UpstreamStreamError(event)
                        break
                    for frame in translator.translate(event):
                        yield frame
                else:
                    self._deps.credentials.save_success(credential_id)
                    self._deps.record(
                        username=username,
                        provider=self._deps.credentials.provider_of(credential_id) or "-",
                        credential_id=credential_id, model=target.model, ok=True,
                        input_tokens=_usage_field(translator.usage, "input_tokens"),
                        output_tokens=_usage_field(translator.usage, "output_tokens"),
                        reasoning_tokens=_usage_field(translator.usage, "reasoning_tokens"),
                        cached_tokens=_usage_field(translator.usage, "cached_tokens"),
                        credit=_usage_field(translator.usage, "credit"),
                        latency_ms=int((time.monotonic() - started) * 1000))
                    for frame in translator.finish():
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
                    self._record_invalid(username, provider_id, credential_id,
                                         target.model, started, error)
                    last_error, last_kind = error, ErrKind.INVALID
                    self._skip_provider(provider_id, tried)
                else:
                    logger.warning("上游 %s 错误（凭证 %s，kind=%s）: %s",
                                   provider_id, credential_id, kind, error)
                    outcome = self._deps.scheduler.note_error(
                        self._candidate(credential_id), kind, int(time.time()))
                    self._deps.credentials.save_error(credential_id, outcome)
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
                    username=username, provider=provider_id,
                    credential_id=credential_id, model=target.model, ok=False,
                    error_type=_error_type_for(kind) if kind else "upstream_protocol",
                    latency_ms=int((time.monotonic() - started) * 1000))
                yield _unavailable_frame(last_error)
                return

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

        while True:
            pick = self._pick(target, tried)
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
                async for event in self._deps.providers[provider_id].stream_chat(
                    credential_data, request.raw, self._upstream_model(provider_id, target.model)
                ):
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
                    self._record_error(credential_id, kind)
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
                        self._record_error(credential_id, kind)
                        last_error = error
                else:
                    self._deps.credentials.save_success(credential_id)
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

    def _pick(self, target: ModelTarget, tried: set[str]):
        # 区分两种情况：模型所属 provider 完全没注册（400）vs 注册了但没有可用凭证（503）
        registered = [pid for pid in self._narrow_providers(target)
                      if pid in self._deps.providers]
        if not registered:
            raise NoProviderForModel(f"no provider registered for model {target.model!r}")
        candidates = self._deps.credentials.candidates(registered)
        if not candidates:
            return None
        credential_id = self._deps.scheduler.select(candidates, tried, int(time.time()))
        if credential_id is None:
            return None
        credential_data = self._deps.credentials.credential_data(credential_id)
        if credential_data is None:  # 并发删除
            tried.add(credential_id)
            return self._pick(target, tried)
        return credential_id, credential_data

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

    def _record_error(self, credential_id: str, kind: ErrKind) -> None:
        outcome = self._deps.scheduler.note_error(
            self._candidate(credential_id), kind, int(time.time()))
        self._deps.credentials.save_error(credential_id, outcome)


def _event_kind(event: Event) -> ErrKind:
    if event.error_code == 1005:
        return ErrKind.PLAN
    if event.error_code == 4001:
        # TRAE 流内 4001 = 参数/模型不可用：换凭证也没用，跳过该上游
        return ErrKind.INVALID
    return ErrKind.OTHER


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
    if kind is ErrKind.PLAN:
        return "rate_limit"
    if kind is ErrKind.DEAD:
        return "credential_unavailable"
    if kind is ErrKind.INVALID:
        return "invalid_request"
    return "upstream_error"


def _error_frame(message: str, code: str) -> bytes:
    from ..engine.sse import SSE_DONE, format_openai_frame

    payload = {"error": {"message": message, "type": "api_error", "code": code}}
    return format_openai_frame(json.dumps(payload, ensure_ascii=False)) + SSE_DONE


