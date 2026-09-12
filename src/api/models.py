"""模型列表：动态拉取 + 归一合并，供 /v1/models 与 playground 共用。

带服务层缓存：某上游拉取成功后把归一结果写入 services.model_list_cache；
下次该上游拉取失败时用缓存兜底，保证 /v1/models 稳定返回完整列表
（冷启动无缓存时才退化为跳过该上游）。
另按 MODEL_BLOCKLIST（fnmatch glob）过滤非用户模型与老模型，
只影响列表展示；直连指定被滤模型不受影响。
元数据（消耗倍率 / token 上限 / 支持性）随条目透传，双上游同名模型
逐字段补缺（先到先填，后到只补 None）。
"""

from __future__ import annotations

import logging
from fnmatch import fnmatch
from typing import Any

from fastapi import APIRouter, Depends

from ..provider.base import Model
from .deps import Services, api_key_user

logger = logging.getLogger(__name__)

# 响应透传的元数据字段（Model → OpenAI 额外字段）
_META_FIELDS = ("credit_rate", "max_input_tokens", "max_output_tokens",
                "supports_images", "supports_tool_call")


def _blocked(model_id: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(model_id, pattern) or fnmatch(model_id.lower(), pattern)
               for pattern in patterns)


def _merge_provider(grouped: dict[str, dict[str, Any]],
                    aliases: dict[str, dict[str, str]],
                    provider_id: str,
                    models_by_lower: dict[str, Model]) -> None:
    """把单个上游的 {小写名: Model} 合并进 grouped / aliases。

    元数据逐字段补缺：先到的上游先填，后到的只补 None 字段，
    避免双上游同名模型互相覆盖已有信息。
    """
    provider_aliases = aliases.setdefault(provider_id, {})
    for lower, model in models_by_lower.items():
        provider_aliases[lower] = model.id
        entry = grouped.setdefault(lower, {"canonical": model.id, "providers": set(),
                                           "meta": dict.fromkeys(_META_FIELDS)})
        # canonical 偏向全小写形式（与 OpenAI 惯例一致）
        if model.id == model.id.lower():
            entry["canonical"] = model.id
        entry["providers"].add(provider_id)
        meta = entry["meta"]
        for key in _META_FIELDS:
            if meta[key] is None:
                meta[key] = getattr(model, key)


async def list_models(services: Services) -> dict:
    """跨上游拉取并合并模型列表。

    CodeBuddy/TRAE 的动态列表里同一模型常只差大小写
    （如 deepseek-v4-flash vs DeepSeek-V4-Flash），此处按小写归一合并，
    providers 取并集；同时记录各上游的原始 id 供执行时映射。
    某上游拉取失败时用上次成功的缓存兜底，而不是让该上游从列表里消失。
    """
    aliases: dict[str, dict[str, str]] = {}
    grouped: dict[str, dict[str, Any]] = {}   # 小写名 → {canonical, providers, meta}
    cache = services.model_list_cache
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
            cached = cache.get(provider_id)
            if cached:
                logger.warning("模型列表获取失败 %s，使用上次缓存: %s", provider_id, error)
                _merge_provider(grouped, aliases, provider_id, cached)
            else:
                logger.warning("模型列表获取失败 %s: %s", provider_id, error)
            continue
        patterns = services.settings.blocklist_patterns
        models_by_lower = {model.id.lower(): model for model in models
                           if not _blocked(model.id, patterns)}
        _merge_provider(grouped, aliases, provider_id, models_by_lower)
        # 成功 → 更新该上游缓存（下次失败时兜底）
        cache[provider_id] = models_by_lower
    # 就地更新（executor 的映射闭包引用同一个 dict 对象）
    services.model_aliases.clear()
    services.model_aliases.update(aliases)
    return {"object": "list", "data": [
        {**{"id": entry["canonical"], "object": "model", "owned_by": "coding2api",
            "providers": sorted(entry["providers"])},
         **{key: value for key, value in entry["meta"].items() if value is not None}}
        for entry in sorted(grouped.values(), key=lambda e: e["canonical"])
    ]}


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_v1_models(_user: str = Depends(api_key_user)):
        return await list_models(services)

    return router
