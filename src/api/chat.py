"""OpenAI 兼容出口：POST /v1/chat/completions（流式 + 非流式）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..compat.openai.request import parse_chat_request
from .deps import Services, api_key_user


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    executor = services.executor
    settings = services.settings

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request, user: str = Depends(api_key_user)):
        body = await request.json()
        if settings.dump_request_bodies:
            dump_dir = Path(settings.data_dir) / "dumps"
            dump_dir.mkdir(parents=True, exist_ok=True)
            (dump_dir / f"{int(time.time() * 1000)}.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        chat_request = parse_chat_request(body)
        if chat_request.stream:
            return StreamingResponse(executor.stream(chat_request, username=user),
                                     media_type="text/event-stream")
        return JSONResponse(await executor.complete(chat_request, username=user))

    return router
