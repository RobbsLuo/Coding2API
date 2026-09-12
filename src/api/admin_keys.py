"""API Key 管理：列出 / 创建 / 删除（明文只在创建时返回一次）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..compat.openai.request import InvalidRequest
from .deps import Services, csrf_protected, principal_from_request


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
        created = api_keys.create(principal.username, str(payload.get("name") or ""))
        return created          # 明文只在此返回一次

    @router.delete("/api/api-keys/{key_id}")
    async def delete_key(key_id: str,
                         _csrf: None = Depends(csrf_protected),
                         principal=Depends(principal_from_request)):
        if not api_keys.delete(key_id, principal.username):
            raise InvalidRequest("api key not found")
        return {"ok": True}

    return router
