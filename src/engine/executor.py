"""执行引擎：选号 → 上游流 → 轮换重试 → 统计（Q12=B 轮换 ≤3 次）。"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..compat.openai.request import ChatRequest
from ..compat.openai.response import (
    StreamTranslator,
    UpstreamStreamError,
    aggregate,
)
from ..db.repo import CredentialRepository
from ..provider.base import ErrKind, Event, EventKind
from .model_resolver import ModelTarget, resolve
from .scheduler import Scheduler


class NoHealthyCredential(Exception):
    pass


class NoProviderForModel(Exception):
    pass


@dataclass(slots=True)
class ExecutorDeps:
    """注入点：provider 客户端、仓储、调度器。"""

    providers: dict[str, Any]           # provider_id → 带 stream_chat 的客户端适配器
    credentials: CredentialRepository
    scheduler: Scheduler
    default_model: str = "glm-5.2"


class Executor:
    def __init__(self, deps: ExecutorDeps) -> None:
        self._deps = deps

    def resolve_target(self, request: ChatRequest) -> ModelTarget:
        return resolve(request.model, self._deps.default_model)

    async def stream(self, request: ChatRequest) -> AsyncIterator[bytes]:
        """流式执行；上游错误按分类冷却并换号，最多 3 次。"""
        target = self.resolve_target(request)
        tried: set[str] = set()
        last_error: Exception | None = None
        translator = StreamTranslator(target.model)

        while True:
            pick = self._pick(target, tried)
            if pick is None:
                yield _unavailable_frame(last_error)
                return
            credential_id, credential_data = pick
            tried.add(credential_id)
            try:
                async for event in self._deps.providers[
                    self._deps.credentials.provider_of(credential_id)
                ].stream_chat(credential_data, request.raw, target.model):
                    if event.kind is EventKind.ERROR:
                        outcome = self._deps.scheduler.note_error(
                            self._candidate(credential_id), _event_kind(event), int(time.time()))
                        self._deps.credentials.save_error(credential_id, outcome)
                        last_error = UpstreamStreamError(event)
                        break
                    for frame in translator.translate(event):
                        yield frame
                else:
                    self._deps.credentials.save_success(credential_id)
                    for frame in translator.finish():
                        yield frame
                    return
            except Exception as error:  # noqa: BLE001 - 统一转为冷却或上抛
                kind = _classify(error)
                if kind is None:
                    raise
                outcome = self._deps.scheduler.note_error(
                    self._candidate(credential_id), kind, int(time.time()))
                self._deps.credentials.save_error(credential_id, outcome)
                last_error = error
            if not self._deps.scheduler.should_rotate(tried):
                yield _unavailable_frame(last_error)
                return

    async def complete(self, request: ChatRequest) -> dict[str, Any]:
        """非流式：聚合同一执行路径的事件。流内错误会触发换号重试。"""
        target = self.resolve_target(request)
        tried: set[str] = set()
        last_error: Exception | None = None

        while True:
            pick = self._pick(target, tried)
            if pick is None:
                raise NoHealthyCredential(
                    f"all credentials unavailable: {last_error}" if last_error
                    else "all credentials unavailable")
            credential_id, credential_data = pick
            tried.add(credential_id)
            provider_id = self._deps.credentials.provider_of(credential_id)
            events: list[Event] = []
            try:
                async for event in self._deps.providers[provider_id].stream_chat(
                    credential_data, request.raw, target.model
                ):
                    events.append(event)
            except Exception as error:  # noqa: BLE001
                kind = _classify(error)
                if kind is None:
                    raise
                self._record_error(credential_id, kind)
                last_error = error
            else:
                try:
                    result = aggregate(events, target.model)
                except UpstreamStreamError as error:
                    kind = _event_kind(error.event)
                    self._record_error(credential_id, kind)
                    last_error = error
                else:
                    self._deps.credentials.save_success(credential_id)
                    return result
            if not self._deps.scheduler.should_rotate(tried):
                raise NoHealthyCredential(
                    f"all credentials unavailable: {last_error}" if last_error
                    else "all credentials unavailable")

    # -------------------------------------------------------------- 内部

    def _pick(self, target: ModelTarget, tried: set[str]):
        # 区分两种情况：模型所属 provider 完全没注册（400）vs 注册了但没有可用凭证（503）
        registered = [pid for pid in target.providers if pid in self._deps.providers]
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


def _error_frame(message: str, code: str) -> bytes:
    from ..engine.sse import SSE_DONE, format_openai_frame

    payload = {"error": {"message": message, "type": "api_error", "code": code}}
    return format_openai_frame(json.dumps(payload, ensure_ascii=False)) + SSE_DONE


