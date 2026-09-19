"""异常 → HTTP 响应映射（TECHNICAL §6.5：稳定的机器可读错误码）。

管理端点返回的失败原因必须是稳定枚举，不能是 Python 异常类名——
类名是实现细节，用户既判断不出问题也不知道下一步做什么，重构时还会漂移。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..auth.csrf import CsrfRejectedError
from ..auth.rbac import ForbiddenError, UnauthorizedError
from ..auth.throttle import ThrottledError
from ..compat.openai.errors import error_payload
from ..compat.openai.request import InvalidRequest
from ..db.crypto import CredentialDecryptError
from ..engine.executor import NoHealthyCredential, NoProviderForModel
from ..engine.model_resolver import UnknownModelError
from ..provider.codebuddy.events import (
    UpstreamProtocolViolation as CodeBuddyProtocolViolation,
)
from ..provider.trae.events import UpstreamProtocolViolation

logger = logging.getLogger(__name__)

# 上游连接/超时失败：502（调用方可重试），与凭证健康度无关。
UPSTREAM_ERROR_STATUS = 502


def register_exception_handlers(app: FastAPI) -> None:
    """把全部异常处理器注册到 app（顺序无关，FastAPI 按类型匹配）。"""

    @app.exception_handler(UnauthorizedError)
    async def _unauthorized(_request, _error: UnauthorizedError):
        return JSONResponse(status_code=401,
                            content=error_payload("invalid authentication credentials",
                                                  "invalid_api_key", 401))

    @app.exception_handler(InvalidRequest)
    async def _invalid(_request, error: InvalidRequest):
        return JSONResponse(status_code=400,
                            content=error_payload(str(error), "invalid_request", 400))

    @app.exception_handler(UnknownModelError)
    async def _unknown_model(_request, error: UnknownModelError):
        return JSONResponse(status_code=400,
                            content=error_payload(str(error), "invalid_request", 400))

    @app.exception_handler(UpstreamProtocolViolation)
    async def _bad_credential(_request, error: UpstreamProtocolViolation):
        return JSONResponse(status_code=400,
                            content=error_payload(str(error), "invalid_credential", 400))

    @app.exception_handler(CodeBuddyProtocolViolation)
    async def _bad_codebuddy_credential(_request, error: CodeBuddyProtocolViolation):
        return JSONResponse(status_code=400,
                            content=error_payload(str(error), "invalid_credential", 400))

    @app.exception_handler(NoHealthyCredential)
    async def _no_health(_request, error: NoHealthyCredential):
        return JSONResponse(status_code=503,
                            content=error_payload(str(error), "no_healthy_credential", 503))

    @app.exception_handler(NoProviderForModel)
    async def _no_provider(_request, error: NoProviderForModel):
        return JSONResponse(status_code=400,
                            content=error_payload(str(error), "invalid_request", 400))

    @app.exception_handler(ForbiddenError)
    async def _forbidden(_request, _error: ForbiddenError):
        return JSONResponse(status_code=403,
                            content=error_payload("admin only", "forbidden", 403))

    @app.exception_handler(CsrfRejectedError)
    async def _csrf_rejected(_request, _error: CsrfRejectedError):
        return JSONResponse(status_code=403,
                            content=error_payload("cross-origin write rejected",
                                                  "forbidden", 403))

    @app.exception_handler(CredentialDecryptError)
    async def _decrypt_failed(_request, _error: CredentialDecryptError):
        """APP_SECRET 变更或密文损坏：必须给出可行动提示，而不是 500。"""
        logger.error("凭证解密失败：APP_SECRET 是否被更换过？")
        return JSONResponse(status_code=500,
                            content=error_payload(
                                "credential decryption failed; APP_SECRET may have changed",
                                "credential_decrypt_failed", 500))

    @app.exception_handler(httpx.TransportError)
    async def _transport_error(_request, error: httpx.TransportError):
        """上游连接/超时失败：502 而不是 500（调用方可重试）。

        httpx 的 TimeoutException/ConnectError 在引擎里不被 _classify 认识
        （没有 kind()），会直接冒泡——以前表现成 500，语义错误。
        """
        logger.warning("上游传输层失败: %s: %s", type(error).__name__, error)
        return JSONResponse(
            status_code=UPSTREAM_ERROR_STATUS,
            content=error_payload(f"upstream transport failed: {type(error).__name__}",
                                  "upstream_unavailable", UPSTREAM_ERROR_STATUS))

    @app.exception_handler(ThrottledError)
    async def _throttled(_request, _error: ThrottledError):
        response = JSONResponse(status_code=429,
                                content=error_payload(
                                    "too many login attempts, slow down",
                                    "rate_limited", 429))
        # OpenAI 客户端按 Retry-After 退避；缺失会立即重试加剧限流
        response.headers.setdefault("Retry-After", "60")
        return response
