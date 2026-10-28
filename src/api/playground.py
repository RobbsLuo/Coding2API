"""Playground：会话鉴权的调试端点（无需 API Key，与 /v1 走同一执行引擎）。

**按人再判一次出站策略**：会话 Cookie 只回答「你是谁」，不回答「你能用哪些
模型、一天能用多少」。缺这一步时任何登录用户（含 viewer）都能直调任意模型，
把 Key 的模型白名单与配额整体绕过——管理台是「人能进门」的地方，进门之后的
出站策略必须落在人身上（`users.allowed_models` / `users.quota`）。

判定与 `/v1` 共用同一份实现与口径：白名单走
`endpoints.ensure_model_allowed_for`（与 Key 白名单同一判据，含「省略模型名
即默认模型」的绕过口子），配额走独立的 `services.user_quota`（用量按**用户名**
聚合，该用户经任何入口的流量都算）。顺序也与 `/v1` 一致：先白名单（400），
再配额（429，不消耗任何上游调用）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..compat.openai.request import parse_chat_request
from ..key_quota import Lease, QuotaExceeded, load_key_quota
from .deps import Services, csrf_protected, principal_from_request, read_json_body
from .endpoints import (
    ensure_model_allowed_for,
    estimated_tokens,
    quota_exceeded_response,
)
from .models import serve_models
from .streaming import with_keepalive


async def _release_when_done(frames, lease: Lease):
    """流式收尾归还并发名额：正常结束 / 客户端断开 / 异常都要释放。"""
    try:
        async for frame in frames:
            yield frame
    finally:
        lease.release()


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    executor = services.executor

    @router.get("/api/playground/models")
    async def playground_list_models(request: Request,
                                     _principal=Depends(principal_from_request)):
        # 管理台入口用 stale-while-revalidate：先回缓存的旧列表，过期渠道后台刷，
        # 不把 zen 探活的十几秒压在打开页面上。request 用于把能力分表
        # （app.state.model_benchmarks）随条目透出，与 /v1/models 同一口径。
        return await serve_models(services, request=request)

    @router.post("/api/playground/chat/completions")
    async def playground_chat(request: Request,
                              _csrf: None = Depends(csrf_protected),
                              principal=Depends(principal_from_request)):
        """会话内直接调试：与外部 /v1 走同一执行引擎，用量记到当前用户。

        不走 API Key 鉴权——调试是管理台自带能力，不应强迫用户先造一个 Key；
        但**策略**照 Key 那套判：模型白名单不通过 → 400，配额超限 → 429。
        用户被删/被禁用不在这里判：`principal_from_request` 已按 DB 现读校验。
        取不到策略行时按「不限制」放行，与 Key 配额的宽容读路径一致——脏数据
        不该把调试入口锁死。
        """
        body = await read_json_body(request)
        chat_request = parse_chat_request(body)
        record = services.user_repo.get(principal.username) or {}
        ensure_model_allowed_for(chat_request,
                                 str(record.get("allowed_models") or ""),
                                 services.settings, subject="this user")
        try:
            lease = services.user_quota.admit(
                principal.username, load_key_quota(record.get("quota")),
                tokens=estimated_tokens(chat_request), subject="user")
        except QuotaExceeded as error:
            return quota_exceeded_response(error)
        if chat_request.stream:
            # 前置校验：在 200 响应头发出前拒绝不可能成功的请求（否则只能断流）
            try:
                executor.preflight(chat_request)
            except Exception:
                lease.release()      # 400 不该占着并发名额
                raise
            return StreamingResponse(
                with_keepalive(_release_when_done(
                    executor.stream_guarded(chat_request,
                                            username=principal.username), lease)),
                media_type="text/event-stream")
        try:
            completion = await executor.complete(chat_request,
                                                 username=principal.username)
        finally:
            lease.release()
        return JSONResponse(completion)

    return router