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
import time
from fnmatch import fnmatch
from typing import Any

from fastapi import APIRouter, Depends

from ..provider.base import Model
from .deps import ApiKeyPrincipal, Services, api_key_user

logger = logging.getLogger(__name__)

# 模型列表 TTL：TTL 内直接复用上次结果。客户端（IDE/Playground）打开面板
# 就会调 /v1/models，无 TTL 时每次都会向上游真实发起请求。
MODEL_LIST_TTL_SECONDS = 300

# 响应透传的元数据字段（Model → OpenAI 额外字段）
_META_FIELDS = ("credit_rate", "max_input_tokens", "max_output_tokens",
                "supports_images", "supports_tool_call",
                "supports_reasoning", "default_effort")


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
    同时记录每渠道各自的元数据（provider_meta），供前端按渠道展示倍率。
    """
    provider_aliases = aliases.setdefault(provider_id, {})
    for lower, model in models_by_lower.items():
        provider_aliases[lower] = model.id
        entry = grouped.setdefault(lower, {"canonical": model.id, "providers": set(),
                                           "meta": dict.fromkeys(_META_FIELDS),
                                           "provider_meta": {}})
        # canonical 偏向全小写形式（与 OpenAI 惯例一致）
        if model.id == model.id.lower():
            entry["canonical"] = model.id
        entry["providers"].add(provider_id)
        meta = entry["meta"]
        provider_meta = {key: getattr(model, key) for key in _META_FIELDS}
        entry["provider_meta"][provider_id] = provider_meta
        for key in _META_FIELDS:
            if meta[key] is None:
                meta[key] = provider_meta[key]


def _entry_response(entry: dict[str, Any]) -> dict[str, Any]:
    """合并后的 grouped 条目 → OpenAI 兼容响应条目。

    多渠道模型额外给 by_provider.{pid}.credit_rate：双上游倍率不同，
    前端按渠道分别展示。
    """
    result: dict[str, Any] = {
        "id": entry["canonical"], "object": "model", "owned_by": "Coding2API",
        "providers": sorted(entry["providers"]),
        **{key: value for key, value in entry["meta"].items() if value is not None},
    }
    if len(entry["providers"]) > 1:
        by_provider = {pid: {"credit_rate": meta["credit_rate"]}
                       for pid, meta in entry["provider_meta"].items()
                       if meta.get("credit_rate") is not None}
        if by_provider:
            result["by_provider"] = by_provider
    return result


async def list_models(services: Services, *, force: bool = False) -> dict:
    """跨上游拉取并合并模型列表。

    CodeBuddy/TRAE 的动态列表里同一模型常只差大小写
    （如 deepseek-v4-flash vs DeepSeek-V4-Flash），此处按小写归一合并，
    providers 取并集；同时记录各上游的原始 id 供执行时映射。
    某上游拉取失败时用上次成功的缓存兜底，而不是让该上游从列表里消失。

    force=False（默认）时 TTL 内直接复用刚才的结果；启动预热传 force=True。
    """
    aliases: dict[str, dict[str, str]] = {}
    grouped: dict[str, dict[str, Any]] = {}   # 小写名 → {canonical, providers, meta}
    cache = services.model_list_cache
    now = time.monotonic()
    for provider_id, provider in services.registry.items():
        fetched_at = services.model_list_fetched_at.get(provider_id)
        fresh = (cache.get(provider_id)
                 and fetched_at is not None
                 and now - fetched_at < MODEL_LIST_TTL_SECONDS)
        if fresh and not force:
            _merge_provider(grouped, aliases, provider_id, cache[provider_id])
            continue
        # 用该上游的一个可用凭证拉取（凭证有归属，模型列表是账号级的）
        # selectable_only：硬禁用/用户关闭的凭证取不到数据，只会白失败
        candidates = services.credentials.candidates([provider_id], selectable_only=True)
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
        services.model_list_fetched_at[provider_id] = time.monotonic()
    # 就地更新（executor 的映射闭包引用同一个 dict 对象）
    services.model_aliases.clear()
    services.model_aliases.update(aliases)
    return {"object": "list", "data": [
        _entry_response(entry)
        for entry in sorted(grouped.values(), key=lambda e: e["canonical"])
    ]}


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_v1_models(_principal: ApiKeyPrincipal = Depends(api_key_user)):
        return await list_models(services)

    return router
