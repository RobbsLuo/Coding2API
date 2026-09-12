"""模型列表：动态拉取 + 归一合并，供 /v1/models 与 playground 共用。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from .deps import Services, api_key_user

logger = logging.getLogger(__name__)


async def list_models(services: Services) -> dict:
    """跨上游拉取并合并模型列表。

    CodeBuddy/TRAE 的动态列表里同一模型常只差大小写
    （如 deepseek-v4-flash vs DeepSeek-V4-Flash），此处按小写归一合并，
    providers 取并集；同时记录各上游的原始 id 供执行时映射。
    """
    aliases: dict[str, dict[str, str]] = {}
    grouped: dict[str, dict[str, Any]] = {}   # 小写名 → {canonical, providers}
    for provider_id, provider in services.registry.items():
        # 用该上游的一个可用凭证拉取（凭证有归属，模型列表是账号级的）
        candidates = services.credentials.candidates([provider_id])
        credential_data = (
            services.credentials.credential_data(candidates[0].credential_id)
            if candidates else {}
        )
        try:
            models = await provider.list_models(credential_data)
        except Exception as error:  # noqa: BLE001 - 单上游失败不影响其他
            logger.warning("模型列表获取失败 %s: %s", provider_id, error)
            continue
        provider_aliases = aliases.setdefault(provider_id, {})
        for model in models:
            provider_aliases[model.id.lower()] = model.id
            entry = grouped.setdefault(model.id.lower(),
                                       {"canonical": model.id, "providers": set()})
            # canonical 偏向全小写形式（与 OpenAI 惯例一致）
            if model.id == model.id.lower():
                entry["canonical"] = model.id
            entry["providers"].add(provider_id)
    # 就地更新（executor 的映射闭包引用同一个 dict 对象）
    services.model_aliases.clear()
    services.model_aliases.update(aliases)
    return {"object": "list", "data": [
        {"id": entry["canonical"], "object": "model", "owned_by": "coding2api",
         "providers": sorted(entry["providers"])}
        for entry in sorted(grouped.values(), key=lambda e: e["canonical"])
    ]}


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_v1_models(_user: str = Depends(api_key_user)):
        return await list_models(services)

    return router
