"""Anthropic Messages 兼容出口：`POST /v1/messages` + `/v1/messages/count_tokens`（P0-1）。

与 `/v1/chat/completions` / `/v1/responses` 共用同一 `executor`（选号 / 冷却 /
轮换 / 统计 / 会话粘性全部不动），只换入站映射与出口翻译：

- 入站：`compat/anthropic/request.py` 把 Anthropic 请求体映射成内部 chat 载荷
  （`ChatRequest.raw`），本服务只实现 chat 子集，不支持项显式 400。
- 出口：流式注入 `AnthropicStreamTranslator`（中立 Event → Anthropic SSE 帧）；
  非流式复用 `executor.complete` 后转换形状。

鉴权同时接受 `x-api-key`（Anthropic SDK 默认）与 `Authorization: Bearer`
（`ANTHROPIC_AUTH_TOKEN`）。Anthropic 协议没有 `[DONE]` 哨兵，终止事件
`message_stop` 本身就是流结束。
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.access import model_allowed
from ..compat.anthropic.request import parse_messages_request
from ..compat.anthropic.response import (
    AnthropicStreamTranslator,
    completion_to_message,
)
from ..compat.openai.request import InvalidRequest
from ..engine.compress import estimate_messages
from .context import apply_context_compression
from .deps import (
    ApiKeyPrincipal,
    Services,
    api_key_user_anthropic,
    read_json_body,
)
from .streaming import with_keepalive


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    executor = services.executor
    settings = services.settings

    @router.post("/v1/messages")
    async def messages(request: Request,
                       principal: ApiKeyPrincipal = Depends(api_key_user_anthropic)):
        body = await read_json_body(request)
        chat_request = parse_messages_request(body)
        if not model_allowed(chat_request.model, principal.allowed_models,
                             default_model=settings.default_model):
            raise InvalidRequest(
                f"model {chat_request.model!r} is not allowed for this api key")
        apply_context_compression(services, chat_request)
        binding = principal.provider_binding
        if chat_request.stream:
            # 与 chat 出口同样的前置校验：在 200 响应头发出前拒绝不可能成功的请求
            target = executor.preflight(chat_request, binding)
            translator = AnthropicStreamTranslator(target.model)
            return StreamingResponse(
                with_keepalive(executor.stream_guarded(chat_request,
                                                       username=principal.username,
                                                       translator=translator,
                                                       provider_binding=binding)),
                media_type="text/event-stream")
        completion = await executor.complete(chat_request, username=principal.username,
                                             provider_binding=binding)
        return JSONResponse(completion_to_message(completion))

    @router.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request,
                           principal: ApiKeyPrincipal = Depends(api_key_user_anthropic)):
        """本地估算输入 token（不转发上游）。

        上游没有统一的计费接口，这里用与上下文压缩同一套启发式估算，
        只保证量级正确（客户端用它决定是否裁剪，不需要精确值）。
        `max_tokens` 等字段对计数无关，映射时一并忽略。
        """
        body = await read_json_body(request)
        chat_request = parse_messages_request(body)
        if not model_allowed(chat_request.model, principal.allowed_models,
                             default_model=settings.default_model):
            raise InvalidRequest(
                f"model {chat_request.model!r} is not allowed for this api key")
        return {"input_tokens": math.ceil(estimate_messages(chat_request.messages))}

    return router