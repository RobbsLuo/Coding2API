"""模型列表：动态拉取 + 归一合并，供 /v1/models 与 playground 共用。

**按凭证加载**：只处理「当前有可用凭证（selectable，即未暂停且未硬禁用）」
的渠道——从未接入、全部暂停或会话失效的渠道**既不拉取也不展示**，避免把
根本打不通的上游模型混进列表。冷启动时只有 zen / kilo（各自带虚拟凭证）在列，等
用户在管理台接入 CodeBuddy / TRAE 后，下一次列表请求（该渠道无缓存）才会拉取。

带服务层缓存：某上游拉取成功后把归一结果写入 services.model_list_cache；
下次该上游拉取失败时用缓存兜底，保证 /v1/models 稳定返回完整列表
（冷启动无缓存时才退化为跳过该上游）。TTL 记的是**上次尝试**时间（成功或
失败都记）：失败不记时间戳的话，上游一次抖动就会让其后每次 /v1/models 都
重跑一遍拉取（zen 探活最慢可占十几秒），把列表请求打成一串超时。
另按 MODEL_BLOCKLIST（fnmatch glob）过滤非用户模型与老模型，
只影响列表展示；直连指定被滤模型不受影响。

**缓存存的是未过滤列表，过滤在每个出口现做**：MODEL_BLOCKLIST 是可热更项
（Q34「改完立即生效」），若把过滤结果存进缓存，改完黑名单要等 TTL（300s）
才反映到 Playground，且被滤掉的模型在 TTL 内会从「兜底缓存」里复活。
元数据（消耗倍率 / token 上限 / 支持性）随条目透传，双上游同名模型
逐字段补缺（先到先填，后到只补 None）。

**展示顺序**：CodeBuddy / TRAE 的模型排在前面（`_PROVIDER_RANK`：
codebuddy 0 → trae 1 → qoder 2 → codearts 3 → 其他 4），组内仍按模型名字典序；
多渠道模型按其最高优先级渠道归位（含 CB 即进第一段，含 TR 即进第二段）。
仅影响 `/v1/models` 与 Playground 的展示顺序，不影响调度选号。
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
# 时间戳记「上次尝试」（含失败）：失败也进 TTL，否则一次抖动之后每次请求
# 都会重试，zen 探活的十几秒会叠加成一串慢请求。
MODEL_LIST_TTL_SECONDS = 300

# 响应透传的元数据字段（Model → OpenAI 额外字段）
_META_FIELDS = ("credit_rate", "max_input_tokens", "max_output_tokens",
                "supports_images", "supports_tool_call",
                "supports_reasoning", "default_effort")

# 展示排序权重：CodeBuddy / TRAE 优先，其余渠道（zen / kilo 等）在后；
# 多渠道模型取所有渠道里的最小权重（含 CB 即进第一段）。
_PROVIDER_RANK = {"codebuddy": 0, "trae": 1, "qoder": 2, "codearts": 3}
_UNRANKED_PROVIDER = 4


def _sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    """展示排序键：渠道优先级升序，同级按模型名（canonical）字典序。

    合并后的条目至少有一个渠道（providers 由 `_merge_provider` 逐个 add），
    故 min() 不会作用于空集合。
    """
    rank = min(_PROVIDER_RANK.get(pid, _UNRANKED_PROVIDER)
               for pid in entry["providers"])
    return rank, entry["canonical"]


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


def _visible(models_by_lower: dict[str, Model],
             patterns: tuple[str, ...]) -> dict[str, Model]:
    """按当前黑名单过滤一个上游的模型表（缓存里存的是未过滤的原始表）。"""
    return {lower: model for lower, model in models_by_lower.items()
            if not _blocked(model.id, patterns)}


def credential_providers(services: Services) -> set[str]:
    """当前有可用凭证（selectable）的渠道集合。

    `list_models` 用它决定「哪些渠道该出现在模型列表里」：没有可用凭证的
    渠道既不值得拉模型（只会白打上游/回退静态表），也不该对外展示——
    用户没接入或自己暂停了，就不该在 Playground / `/v1/models` 里看到它。

    口径与调度器一致（`selectable_only=True`：未暂停、未硬禁用）；冷却中的
    凭证仍算「有凭证」——渠道接了只是暂时限流，模型列表不该跟着闪没。
    """
    return {row.provider
            for row in services.credentials.candidates(selectable_only=True)}


async def list_models(services: Services, *, force: bool = False) -> dict:
    """跨上游拉取并合并模型列表。

    CodeBuddy/TRAE 的动态列表里同一模型常只差大小写
    （如 deepseek-v4-flash vs DeepSeek-V4-Flash），此处按小写归一合并，
    providers 取并集；同时记录各上游的原始 id 供执行时映射。
    某上游拉取失败时用上次成功的缓存兜底，而不是让该上游从列表里消失。

    只遍历「当前有可用凭证」的渠道（见 `credential_providers`）：没接入 /
    全部暂停 / 会话失效的渠道不拉取也不展示，所以冷启动只有 zen / kilo（自带
    虚拟凭证），接入 CodeBuddy / TRAE 后下一次请求才把它们拉进来。

    force=False（默认）时 TTL 内直接复用刚才的结果；启动预热传 force=True。
    黑名单在每个出口现算（缓存不做过滤），因此改完黑名单下一次调用立即生效。
    输出顺序按 `_sort_key`：CB / TR 渠道的模型优先，其余渠道在后。
    """
    aliases: dict[str, dict[str, str]] = {}
    grouped: dict[str, dict[str, Any]] = {}   # 小写名 → {canonical, providers, meta}
    cache = services.model_list_cache
    patterns = services.settings.blocklist_patterns
    connected = credential_providers(services)
    now = time.monotonic()
    for provider_id, provider in services.registry.items():
        # 没凭证的渠道直接跳过：不拉取、不展示。已接入的渠道即使本次拉取失败，
        # 也会走下面的缓存兜底，不会因为一次抖动就从列表消失。
        if provider_id not in connected:
            continue
        fetched_at = services.model_list_fetched_at.get(provider_id)
        # TTL 按「上次尝试」计（失败也刷新）：有缓存就继续用缓存，没缓存就跳过，
        # 两种情况都不再打上游，避免上游抖动时每次请求都重跑一遍拉取。
        if (not force and fetched_at is not None
                and now - fetched_at < MODEL_LIST_TTL_SECONDS):
            cached = cache.get(provider_id)
            if cached:
                _merge_provider(grouped, aliases, provider_id,
                                _visible(cached, patterns))
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
            # 失败也记时间戳（负缓存）：TTL 内不再重试，有缓存用缓存、无缓存跳过。
            services.model_list_fetched_at[provider_id] = time.monotonic()
            cached = cache.get(provider_id)
            if cached:
                logger.warning("模型列表获取失败 %s，使用上次缓存: %s", provider_id, error)
                _merge_provider(grouped, aliases, provider_id,
                                _visible(cached, patterns))
            else:
                logger.warning("模型列表获取失败 %s: %s", provider_id, error)
            continue
        models_by_lower = {model.id.lower(): model for model in models}
        _merge_provider(grouped, aliases, provider_id,
                        _visible(models_by_lower, patterns))
        # 成功 → 更新该上游缓存（下次失败时兜底）。存未过滤表：过滤在出口现做，
        # 否则黑名单热更后缓存里仍是被滤前的旧结果（最长 TTL 才生效）。
        cache[provider_id] = models_by_lower
        services.model_list_fetched_at[provider_id] = time.monotonic()
    # 就地更新（executor 的映射闭包引用同一个 dict 对象）
    services.model_aliases.clear()
    services.model_aliases.update(aliases)
    return {"object": "list", "data": [
        _entry_response(entry)
        for entry in sorted(grouped.values(), key=_sort_key)
    ]}


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_v1_models(_principal: ApiKeyPrincipal = Depends(api_key_user)):
        return await list_models(services)

    return router
