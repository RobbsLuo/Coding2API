"""Playground：会话鉴权的调试端点（无需 API Key，与 /v1 走同一执行引擎）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..compat.openai.request import parse_chat_request
from .deps import Services, csrf_protected, principal_from_request
from .models import list_models


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    executor = services.executor

    @router.get("/api/playground/models")
    async def playground_list_models(_principal=Depends(principal_from_request)):
        return await list_models(services)

    @router.post("/api/playground/chat/completions")
    async def playground_chat(request: Request,
                              _csrf: None = Depends(csrf_protected),
                              principal=Depends(principal_from_request)):
        """会话内直接调试：与外部 /v1 走同一执行引擎，用量记到当前用户。

        不走 API Key 鉴权——调试是管理台自带能力，不应强迫用户先造一个 Key。
        """
        body = await request.json()
        chat_request = parse_chat_request(body)
        if chat_request.stream:
            return StreamingResponse(
                executor.stream(chat_request, username=principal.username),
                media_type="text/event-stream")
        return JSONResponse(await executor.complete(chat_request, username=principal.username))

    return router
