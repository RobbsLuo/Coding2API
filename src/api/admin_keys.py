"""API Key 管理：列出 / 创建 / 删除（明文只在创建时返回一次）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth.access import normalize_allowed_ips
from ..compat.openai.request import InvalidRequest
from ..engine.model_resolver import KNOWN_PROVIDERS
from .deps import Services, csrf_protected, principal_from_request


def _parse_binding(raw: object) -> str:
    """校验渠道绑定：空 = 自动；否则必须是已注册渠道。"""
    binding = str(raw or "").strip().lower()
    if binding and binding not in KNOWN_PROVIDERS:
        raise InvalidRequest(
            f"provider_binding must be empty or one of {', '.join(KNOWN_PROVIDERS)}")
    return binding


def _parse_allowed_ips(raw: object) -> str:
    """校验并规范化来源 IP 白名单（写入时校验，读取路径只做匹配）。"""
    try:
        return normalize_allowed_ips(str(raw or ""))
    except ValueError as error:
        raise InvalidRequest(str(error)) from error


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    api_keys = services.api_keys

    @router.get("/api/api-keys")
    async def list_keys(principal=Depends(principal_from_request)):
        return {"api_keys": api_keys.list_for(principal.username)}

    @router.post("/api/api-keys")
    async def create_key(payload: dict,
                         _csrf: None = Depends(csrf_protected),
                         principal=Depends(principal_from_request)):
        created = api_keys.create(
            principal.username, str(payload.get("name") or ""),
            provider_binding=_parse_binding(payload.get("provider_binding")),
            allowed_ips=_parse_allowed_ips(payload.get("allowed_ips")))
        return created          # 明文只在此返回一次

    @router.delete("/api/api-keys/{key_id}")
    async def delete_key(key_id: str,
                         _csrf: None = Depends(csrf_protected),
                         principal=Depends(principal_from_request)):
        if not api_keys.delete(key_id, principal.username):
            raise InvalidRequest("api key not found")
        return {"ok": True}

    return router
