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
逐字段补缺（先到先填，后到只补 None）。`name`（上游人类可读名，如
Qoder 的 `Qwen3.8-Max`）同样按「先到的非空名」透传，供前端展示——否则
只能显示 `qmodel_38max` 这类内部代号。

**合并键（2026-09 起）**：CodeBuddy / TRAE / Qoder / CodeArts 这四条渠道
**按人类可读名（小写）合并**，而不是按上游 id——同一模型在各渠道的内部
代号互不相同（Qoder `kmodel_latest` = TRAE `kimi-k3` = CodeBuddy
`kimi-k3-1`），只有名字能对齐。zen / kilo 的 `name` 不是模型名（zen 恒为
`opencode`、kilo 是长标题），仍按 id 合并，避免把无关模型误并。同渠道内
重名（CodeBuddy 的 `hy4-preview` / `hy4-preview-x` 都叫「Hy4 preview」）
时冲突项退回上游 id，否则其中一个会被同键覆盖而消失。

合并键即**该条目的对外 id**（`/v1/models` 的 `id`、选择器里的值），如
`kimi-k3` / `qwen3.7-max`；**单渠道条目仍用上游原始 id**（只在多条渠道
真正并到一起时才改用可读名）。无论对外 id 是什么，**请求转发到某渠道时
一律映射回该渠道自己登记的原 id**（`kmodel_latest`），映射表见
`services.model_aliases`，由 `executor.upstream_model_name` 消费。

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

# 响应透传的元数据字段（Model → OpenAI 额外字段）。`name` 放在最前：
# 上游人类可读名（Qoder 的 `Qwen3.8-Max`），前端优先展示它而非内部代号。
_META_FIELDS = ("name", "credit_rate", "max_input_tokens", "max_output_tokens",
                "supports_images", "supports_tool_call",
                "supports_reasoning", "default_effort")

# 展示排序权重：CodeBuddy / TRAE 优先，其余渠道（zen / kilo 等）在后；
# 多渠道模型取所有渠道里的最小权重（含 CB 即进第一段）。
_PROVIDER_RANK = {"codebuddy": 0, "trae": 1, "qoder": 2, "codearts": 3}
_UNRANKED_PROVIDER = 4

# 按「人类可读名（小写）」合并的渠道：这几条的 id 是各渠道内部代号
# （Qoder `kmodel_latest` / TRAE `kimi-k3` / CB `kimi-k3-1`），只有 name
# 能对齐。zen / kilo 排除在外——它们 name 不是模型名（zen 恒 `opencode`、
# kilo 是长标题），按名合并会把无关模型误并成一条。
_NAME_KEY_PROVIDERS = frozenset({"codebuddy", "trae", "qoder", "codearts"})


def _sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    """展示排序键：渠道优先级升序，同级按对外 id 字典序。

    合并后的条目至少有一个渠道（providers 由 `_merge_provider` 逐个 add），
    故 min() 不会作用于空集合。
    """
    rank = min(_PROVIDER_RANK.get(pid, _UNRANKED_PROVIDER)
               for pid in entry["providers"])
    return rank, entry["id"]


def _blocked(model_id: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(model_id, pattern) or fnmatch(model_id.lower(), pattern)
               for pattern in patterns)


def _merge_key(model: Model, provider_id: str,
               duplicate_names: set[str]) -> str:
    """一个模型计入哪个合并键。

    CodeBuddy / TRAE / Qoder / CodeArts **按人类可读名（小写）合并**——这些
    渠道同一模型的内部代号互不相同（Qoder `kmodel_latest` = TRAE `kimi-k3`
    = CodeBuddy `kimi-k3-1`），只有名字能对齐。以下情况退回上游 id：

    * 渠道不在名合并集合（zen / kilo 的 `name` 不是模型名：zen 恒为
      `opencode`、kilo 是长标题，按名合并会把无关模型误并）；
    * 该模型没有名字；
    * 名字在本渠道内重复（如 CodeBuddy 的 `hy4-preview` / `hy4-preview-x`
      都叫「Hy4 preview」）——否则其中一个会被同键覆盖而消失。
    """
    if provider_id not in _NAME_KEY_PROVIDERS or not model.name:
        return model.id.lower()
    name_key = model.name.lower()
    return model.id.lower() if name_key in duplicate_names else name_key


def _merge_provider(grouped: dict[str, dict[str, Any]],
                    aliases: dict[str, dict[str, str]],
                    provider_id: str,
                    models_by_lower: dict[str, Model]) -> None:
    """把单个上游的 {小写名: Model} 合并进 grouped / aliases。

    元数据逐字段补缺：先到的上游先填，后到的只补 None 字段，
    避免双上游同名模型互相覆盖已有信息。
    同时记录每渠道各自的元数据（provider_meta）与**上游原始 id**
    （raw_ids，请求转发时按渠道映射回去），供前端按渠道展示倍率。
    合并键见 `_merge_key`。
    """
    provider_aliases = aliases.setdefault(provider_id, {})
    # 先数本渠道内的重名（只数非空名），用于把冲突项退回上游 id。
    name_counts: dict[str, int] = {}
    for model in models_by_lower.values():
        if model.name:
            name_counts[model.name.lower()] = name_counts.get(
                model.name.lower(), 0) + 1
    duplicate_names = {name for name, count in name_counts.items() if count > 1}
    for lower, model in models_by_lower.items():
        # 别名表按上游 id 小写索引：请求原 id（Qoder `kmodel_latest`）时
        # 能定位到本渠道；请求可读名时的映射在 `_finalize` 里补。
        provider_aliases[lower] = model.id
        key = _merge_key(model, provider_id, duplicate_names)
        entry = grouped.setdefault(key, {"providers": set(),
                                         "meta": dict.fromkeys(_META_FIELDS),
                                         "provider_meta": {}, "raw_ids": {},
                                         "names": set(), "name_keyed": set()})
        entry["providers"].add(provider_id)
        entry["raw_ids"][provider_id] = model.id
        if model.name:
            entry["names"].add(model.name)
        # 本渠道这条确实按「可读名」入的键（而非重名退回的 id）：只有这时
        # 可读名才与该渠道的模型一一对应，`_finalize` 才敢把它登记成别名。
        if provider_id in _NAME_KEY_PROVIDERS and model.name and key == model.name.lower():
            entry["name_keyed"].add(provider_id)
        meta = entry["meta"]
        provider_meta = {field: getattr(model, field) for field in _META_FIELDS}
        entry["provider_meta"][provider_id] = provider_meta
        for field in _META_FIELDS:
            # 空串按「未填」处理（`name`/`default_effort` 缺省为 ""）：先到的
            # 上游没名字时，后续有名字的上游仍能补上，而不是被空串占住。
            if meta[field] is None or meta[field] == "":
                meta[field] = provider_meta[field]


def _finalize(entry: dict[str, Any],
              aliases: dict[str, dict[str, str]]) -> None:
    """定稿一个合并组：算出对外 id，并把「对外 id → 各渠道原 id」写进别名表。

    对外 id：

    * 多渠道真正并到一起时用**可读名小写**（`kimi-k3` / `qwen3.7-max`），
      与前端展示一致；取最短的名字（如 `Kimi-K3` 优先于 `Kimi-K3-长尾`）；
    * 单渠道条目仍用**上游原始 id**（Qoder `kmodel_latest` 保持原样展示），
      用户按原 id 直连、与历史记录一致。

    别名表按渠道分别登记 `对外 id → 该渠道原 id`（`kimi-k3` → qoder 的
    `kmodel_latest`）：`executor.upstream_model_name` 在真正转发时据此
    换回各渠道能识别的原 id。单渠道时二者相同（不退化为无映射）。
    """
    providers = sorted(entry["providers"])
    names = sorted(entry["names"])
    if len(providers) > 1 and names:
        representative = min(names, key=lambda name: (len(name), name.lower()))
        entry["id"] = representative.lower()
        entry["meta"]["name"] = representative    # 展示名取同一代表名
    else:
        entry["id"] = entry["raw_ids"][providers[0]]
    for provider_id in providers:
        provider_aliases = aliases.setdefault(provider_id, {})
        raw_id = entry["raw_ids"][provider_id]
        # 两条键都登记：对外 id（合并后的可读名，用户从列表里选到的值）
        # 与原 id（用户手写 `kmodel_latest` 直连时仍能定位到本渠道）。
        provider_aliases[entry["id"].lower()] = raw_id
        provider_aliases[raw_id.lower()] = raw_id
        # 单渠道条目对外 id 是原 id，可读名不是 id：额外登记名键，
        # 用户按 `Kimi-K3` 这类可读名直连也能落在本渠道。仅限「按名入键」
        # 的渠道，避免同渠道重名（CB 的 `hy4-preview` / `hy4-preview-x` 都叫
        # 「Hy4 preview」）或 zen 的 `opencode` 等非模型名互相覆盖。
        display = entry["meta"].get("name")
        if provider_id in entry["name_keyed"] and display:
            provider_aliases[str(display).lower()] = raw_id


def _entry_response(entry: dict[str, Any]) -> dict[str, Any]:
    """合并后的 grouped 条目 → OpenAI 兼容响应条目。

    多渠道模型额外给 by_provider.{pid}.credit_rate：双上游倍率不同，
    前端按渠道分别展示。
    """
    result: dict[str, Any] = {
        "id": entry["id"], "object": "model", "owned_by": "Coding2API",
        "providers": sorted(entry["providers"]),
        # 空串按「无该字段」处理：`name`/`default_effort` 未提供时是 ""，
        # 透传空串会让前端显示空模型名或多余的 `default_effort: ""`。
        **{field: value for field, value in entry["meta"].items()
           if value is not None and value != ""},
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
    # 定稿：算出每个合并组的对外 id，并把「对外 id → 各渠道原 id」写进别名表。
    # 必须在合并全部完成后做——对外 id（多用可读名）要等所有渠道到齐才能定，
    # 且单渠道/多渠道的算法不同（见 `_finalize`）。
    for entry in grouped.values():
        _finalize(entry, aliases)
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
