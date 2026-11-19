"""内置检索工具的对外出口：`POST /v1/tools/search` 与 `POST /v1/fetch`。

上游端点是 CodeBuddy 官方 CLI 真实在打的 `/agenttool/v1/search` /
`/agenttool/v1/webfetch`（见 `provider/codebuddy/tools.py` 的模块文档），
**不接任何第三方搜索 API**——零外部依赖、零额外费用。

为什么单独开两个端点而不是只走 Responses 的 `web_search` 工具：
1. Codex CLI / Claude Code 在 Responses 里下发的 `web_search` 是**托管工具**，
   由推理服务端执行；本仓现在只能丢弃（`compat/responses/request.py` 注释
   自认「跳过是降级」）。有独立端点后，后续可以把 `web_search` 映射到这里。
2. 非 Responses 客户端（普通 OpenAI SDK、脚本）也能直接用，不必经过一次推理。

鉴权与 `/v1/*` 一致（Bearer）；失败语义与聊天出口同一套受控码：

- 上游业务码拒绝 → 400 / 429 / 403（见 `ToolRejected.status`）
- 没有可用凭证 → 503 + `Retry-After`
- 上游 5xx → 502

**不落用量明细**：这两个端点不消耗 token，落 `usage_events` 会污染 token 与
成本口径。只写一行访问日志（中间件已有 `X-Request-ID`）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..compat.openai.errors import error_payload
from ..compat.openai.request import InvalidRequest
from ..engine.executor import NoHealthyCredential
from ..provider.codebuddy.tools import ToolRejected
from .deps import ApiKeyPrincipal, Services, api_key_user, read_json_body

# 搜索/抓取只在这些渠道上有实现（其它渠道缺 `search_web` / `fetch_url` 方法）
_TOOL_PROVIDERS = ("codebuddy", "codebuddy_intl")


def _require_str(body: dict[str, Any], key: str, *, maximum: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"{key} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum:
        raise InvalidRequest(f"{key} is too long (max {maximum} characters)")
    return value


def _optional_domains(body: dict[str, Any], key: str) -> list[str] | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidRequest(f"{key} must be an array of strings")
    cleaned = [item.strip() for item in value if item.strip()]
    return cleaned or None


def _rejection(error: ToolRejected) -> JSONResponse:
    code = ("invalid_request" if error.status == 400
            else "rate_limit_exceeded" if error.status == 429
            else "credential_rejected" if error.status == 403
            else "upstream_error")
    return JSONResponse(status_code=error.status,
                        content=error_payload(error.message, code, error.status))


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    executor = services.executor

    def _providers() -> list[Any]:
        """按渠道顺序取「实现了检索工具」的 provider（缺失即跳过）。"""
        found = []
        for provider_id in _TOOL_PROVIDERS:
            provider = services.registry.get(provider_id)
            if provider is not None and hasattr(provider, "search_web"):
                found.append((provider_id, provider))
        return found

    async def _call(call: Any) -> dict[str, Any]:
        """按健康度顺序尝试凭证：401/403 视为「这个号不可用」→ 换下一个。"""
        providers = _providers()
        if not providers:
            raise InvalidRequest("no provider supports built-in search/fetch")
        last_error: ToolRejected | None = None
        for provider_id, provider in providers:
            for _credential_id, credential_data in executor.pick_aux((provider_id,)):
                try:
                    return await call(provider, credential_data)
                except ToolRejected as error:
                    if error.status == 403:
                        last_error = error
                        continue          # 换下一个号
                    raise
        if last_error is not None:
            raise last_error
        raise NoHealthyCredential("no healthy credential for built-in tools")

    @router.post("/v1/tools/search")
    async def search_tools(request: Request,
                           principal: ApiKeyPrincipal = Depends(api_key_user)):
        """网页搜索：`{query, max_results?, allowed_domains?, blocked_domains?, freshness?}`。"""
        body = await read_json_body(request)
        if not isinstance(body, dict):
            raise InvalidRequest("request body must be a JSON object")
        query = _require_str(body, "query", maximum=1000)
        max_results = body.get("max_results", 5)
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise InvalidRequest("max_results must be an integer")
        max_results = max(1, min(max_results, 20))
        freshness = body.get("freshness")
        if freshness is not None and not isinstance(freshness, str):
            raise InvalidRequest("freshness must be a string")
        try:
            return await _call(lambda provider, data: provider.search_web(
                data, query=query, max_results=max_results,
                allowed_domains=_optional_domains(body, "allowed_domains"),
                blocked_domains=_optional_domains(body, "blocked_domains"),
                freshness=freshness))
        except ToolRejected as error:
            return _rejection(error)

    @router.post("/v1/fetch")
    async def fetch_url(request: Request,
                        principal: ApiKeyPrincipal = Depends(api_key_user)):
        """网页抓取：`{url, prompt?, max_length?}` → `{url, content, content_type}`。"""
        body = await read_json_body(request)
        if not isinstance(body, dict):
            raise InvalidRequest("request body must be a JSON object")
        url = _require_str(body, "url", maximum=4096)
        if not url.startswith(("http://", "https://")):
            raise InvalidRequest("url must be an http(s) URL")
        prompt = body.get("prompt") or ""
        if not isinstance(prompt, str):
            raise InvalidRequest("prompt must be a string")
        max_length = body.get("max_length")
        if max_length is not None and (isinstance(max_length, bool)
                                       or not isinstance(max_length, int)
                                       or max_length <= 0):
            raise InvalidRequest("max_length must be a positive integer")
        try:
            return await _call(lambda provider, data: provider.fetch_url(
                data, url=url, prompt=prompt, max_length=max_length))
        except ToolRejected as error:
            return _rejection(error)

    return router
