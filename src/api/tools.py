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

但**配额准入照判**（Key + 用户两层）：RPM/TPM/并发是请求维度的，不落明细也
不污染成本；缺这一步时一把 0 配额 Key 就能无限消耗共享凭证池的检索额度。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..compat.openai.errors import error_payload
from ..compat.openai.request import InvalidRequest
from ..engine.executor import NoHealthyCredential
from ..key_quota import Lease, QuotaExceeded
from ..provider.codebuddy.tools import ToolRejected
from ..runtime_settings import blocked_webhook_host
from .deps import ApiKeyPrincipal, Services, api_key_user, read_json_body
from .endpoints import _CombinedLease, quota_exceeded_response

logger = logging.getLogger(__name__)

# 搜索/抓取只在这些渠道上有实现（其它渠道缺 `search_web` / `fetch_url` 方法）
_TOOL_PROVIDERS = ("codebuddy", "codebuddy_intl")

# 工具调用不产生模型 token，给 TPM 一个固定小预估即可：只为让速率判定有输入。
_TOOL_TOKENS = 64


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
    """上游拒绝 → 受控文案：只给分类，不把上游正文回给 API 消费者。

    上游 body 可能含内部 schema / 请求回显（进而含 query 或 credential 片段），
    透传给任意调用方即信息泄露；原文只进服务端日志。
    """
    if error.status == 400:
        code, message = "invalid_request", "upstream rejected the request"
    elif error.status == 429:
        code, message = "rate_limit_exceeded", "upstream rate limited the request"
    elif error.status == 403:
        code, message = "credential_rejected", "upstream rejected credential"
    else:
        code, message = "upstream_error", f"upstream error {error.status}"
    logger.warning("工具上游拒绝 status=%s: %s", error.status, error.message)
    return JSONResponse(status_code=error.status,
                        content=error_payload(message, code, error.status))


def _admit_quota(services: Services, principal: ApiKeyPrincipal) -> Lease:
    """与聊天同一套两层配额（Key + 用户）；工具调用没有请求体可估 token。

    先 Key 后用户，用户级拒绝时归还 Key 级名额；与 `endpoints._admit_quota`
    复用 `_CombinedLease`，避免两层归还口径分叉。
    """
    lease = services.key_quota.admit(
        principal.key_id, principal.quota, tokens=_TOOL_TOKENS)
    try:
        user_lease = services.user_quota.admit(
            principal.username, principal.user_quota, tokens=_TOOL_TOKENS,
            subject="user")
    except QuotaExceeded:
        lease.release()
        raise
    return _CombinedLease(lease, user_lease)


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
            lease = _admit_quota(services, principal)
        except QuotaExceeded as error:
            return quota_exceeded_response(error)
        try:
            return await _call(lambda provider, data: provider.search_web(
                data, query=query, max_results=max_results,
                allowed_domains=_optional_domains(body, "allowed_domains"),
                blocked_domains=_optional_domains(body, "blocked_domains"),
                freshness=freshness))
        except ToolRejected as error:
            return _rejection(error)
        finally:
            lease.release()

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
        # 信任边界：URL 由调用方控制，而抓取走共享凭证，内网/保留地址必须拦。
        if await asyncio.to_thread(blocked_webhook_host, url):
            raise InvalidRequest(
                "url must not point to a private, loopback or reserved address")
        prompt = body.get("prompt") or ""
        if not isinstance(prompt, str):
            raise InvalidRequest("prompt must be a string")
        max_length = body.get("max_length")
        if max_length is not None and (isinstance(max_length, bool)
                                       or not isinstance(max_length, int)
                                       or max_length <= 0):
            raise InvalidRequest("max_length must be a positive integer")
        try:
            lease = _admit_quota(services, principal)
        except QuotaExceeded as error:
            return quota_exceeded_response(error)
        try:
            return await _call(lambda provider, data: provider.fetch_url(
                data, url=url, prompt=prompt, max_length=max_length))
        except ToolRejected as error:
            return _rejection(error)
        finally:
            lease.release()

    return router
