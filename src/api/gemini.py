"""Gemini 兼容出口：`POST /v1beta/models/{model}:generateContent|:streamGenerateContent`。

与另外三个出口共用同一 `executor`（选号 / 冷却 / 轮换 / 统计 / 会话粘性全不动），
只换入站映射与出口翻译：

- 入站：`compat/gemini/request.py` 把 Gemini 请求体映射成内部 chat 载荷；
- 出口：流式注入 `GeminiStreamTranslator`，非流式复用 `executor.complete` 后
  转成 `GenerateContentResponse`。

顺带提供 Gemini SDK 会调的两个元端点：`GET /v1beta/models`（列出本服务可用
模型）与 `POST /v1beta/models/{model}:countTokens`（本地估算，不转发上游）。

鉴权接受 `x-goog-api-key`、`?key=`、`Authorization: Bearer` 三种（前两种是
Gemini SDK / REST 的官方形态）。
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Request

from ..compat.gemini.request import parse_gemini_request
from ..compat.gemini.response import GeminiStreamTranslator, completion_to_gemini
from ..compat.openai.request import InvalidRequest
from ..engine.compress import estimate_messages
from .deps import (
    ApiKeyPrincipal,
    Services,
    api_key_user_gemini,
    read_json_body,
)
from .endpoints import dispatch_compat, ensure_model_allowed

# 支持的动作（Gemini 的路径级 RPC 名）。
_ACTIONS = {"generateContent", "streamGenerateContent", "countTokens"}


def split_model_path(raw: str) -> tuple[str, str]:
    """`models/gemini-2.0-flash:streamGenerateContent` → (model, action)。

    Vertex 风格会带更长的路径（`publishers/google/models/gemini-2.0-flash:...`）：
    取最后一个 `models/` 之后的段，兼容两种写法。
    """
    head, _, action = raw.rpartition(":")
    if not head or not action:
        raise InvalidRequest("path must end with :generateContent, "
                             ":streamGenerateContent or :countTokens")
    if action not in _ACTIONS:
        raise InvalidRequest(f"unsupported method: {action}")
    marker = head.rfind("models/")
    model = head[marker + len("models/"):] if marker >= 0 else head
    if not model:
        raise InvalidRequest("model is required")
    return model, action


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    executor = services.executor
    settings = services.settings

    @router.get("/v1beta/models")
    async def list_models(request: Request,
                          principal: ApiKeyPrincipal = Depends(api_key_user_gemini)):
        """Gemini 形状的模型列表（SDK 启动时会调）。

        只列当前有可用凭证的渠道，与 `/v1/models` 同源（`serve_models` 的
        过滤/白名单/缓存全复用），仅换外层形状。`name` 带 `models/` 前缀
        （Gemini 约定），`displayName` 用本服务的展示名。
        """
        from .models import serve_models

        payload = await serve_models(services, principal.allowed_models, request,
                                     user_allowed_models=principal.user_allowed_models)
        return {
            "models": [
                {
                    "name": f"models/{item['id']}",
                    "displayName": item.get("name") or item["id"],
                    "description": "Coding2API aggregated model",
                    "supportedGenerationMethods": [
                        "generateContent", "streamGenerateContent", "countTokens"],
                }
                for item in payload.get("data", [])
            ]
        }

    @router.post("/v1beta/models/{raw_path:path}")
    async def generate(raw_path: str, request: Request,
                       principal: ApiKeyPrincipal = Depends(api_key_user_gemini)):
        model, action = split_model_path(raw_path)
        if action == "countTokens":
            return await _count_tokens(request, principal, model, settings)
        return await dispatch_compat(
            executor, settings, request, principal,
            parse=lambda body: parse_gemini_request(
                body, model=model, stream=action == "streamGenerateContent"),
            translator_for=GeminiStreamTranslator,
            convert=completion_to_gemini)

    return router


async def _count_tokens(request: Request, principal: ApiKeyPrincipal,
                        model: str, settings: object) -> dict[str, int]:
    """本地估算输入 token（Gemini `:countTokens` 形状：`{"totalTokens": n}`）。

    与 Anthropic `count_tokens` 同一套启发式：量级正确即可，不转发上游。
    """
    chat_request = parse_gemini_request(
        await read_json_body(request), model=model, stream=False)
    ensure_model_allowed(chat_request, principal, settings)
    total = math.ceil(estimate_messages(chat_request.messages))
    return {
        "totalTokens": total,
        "promptTokensDetails": [{"modality": "TEXT", "tokenCount": total}],
    }
