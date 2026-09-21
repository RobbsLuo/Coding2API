"""OpenAI Responses 兼容出口：POST /v1/responses（B2.1，Codex CLI）。

与 `/v1/chat/completions` 共用同一 `executor`（选号 / 冷却 / 轮换 / 统计 /
会话粘性全部不动），只换入站映射与出口翻译：

- 入站：`compat/responses/request.py` 把 Responses 请求体映射成内部 chat
  载荷（`ChatRequest.raw`），本服务只实现 chat 子集，不支持项显式 400。
- 出口：流式注入 `ResponsesStreamTranslator`（把中立 Event 直接翻成
  Responses SSE 帧）；非流式复用 `executor.complete` 后转换形状。

Responses 协议没有 `[DONE]` 哨兵，终止事件（`response.completed` /
`response.incomplete` / `response.failed`）本身就是流结束。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..compat.responses.request import parse_responses_request
from ..compat.responses.response import (
    ResponsesStreamTranslator,
    completion_to_response,
)
from .deps import Services, api_key_user, read_json_body
from .streaming import with_keepalive


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    executor = services.executor

    @router.post("/v1/responses")
    async def responses(request: Request, user: str = Depends(api_key_user)):
        body = await read_json_body(request)
        chat_request = parse_responses_request(body)
        if chat_request.stream:
            # 与 chat 出口同样的前置校验：在 200 响应头发出前拒绝不可能成功的请求
            target = executor.preflight(chat_request)
            translator = ResponsesStreamTranslator(target.model)
            return StreamingResponse(
                with_keepalive(executor.stream_guarded(chat_request, username=user,
                                                       translator=translator)),
                media_type="text/event-stream")
        completion = await executor.complete(chat_request, username=user)
        return JSONResponse(completion_to_response(completion,
                                                   created_at=int(time.time())))

    return router
