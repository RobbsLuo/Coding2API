"""模型列表：动态拉取 + 归一合并，供 /v1/models 与 playground 共用。

**按凭证加载**：只处理「当前有可用凭证（selectable，即未暂停且未硬禁用）」
的渠道——从未接入、全部暂停或会话失效的渠道**既不拉取也不展示**，避免把
根本打不通的上游模型混进列表。冷启动时只有 zen / kilo（各自带虚拟凭证）在列，等
用户在管理台接入 CodeBuddy / TRAE 后，下一次列表请求（该渠道无缓存）才会拉取。

带服务层缓存：某上游拉取成功后把归一结果写入 services.model_list_cache；
下次该上游拉取失败时用缓存兜底，保证 /v1/models 稳定返回完整列表
（冷启动无缓存时才退化为跳过该上游）。缓存另有一份**落盘快照**
（`model_catalog.json`，见 `api/model_catalog.py`），启动时同步读回并立即
publish 别名表——否则启动预热跑完之前别名表是空的，扁平名请求无法收窄候选，
会真实打一轮不认该模型的上游。TTL 记的是**上次尝试**时间（成功或
失败都记）：失败不记时间戳的话，上游一次抖动就会让其后每次 /v1/models 都
重跑一遍拉取（zen 探活最慢可占十几秒），把列表请求打成一串超时。
另按 MODEL_BLOCKLIST（fnmatch glob）过滤非用户模型与老模型，
只影响列表展示；直连指定被滤模型不受影响。

**缓存存的是未过滤列表，过滤在每个出口现做**：MODEL_BLOCKLIST 是可热更项
（Q34「改完立即生效」），若把过滤结果存进缓存，改完黑名单要等 TTL（300s）
才反映到 Playground，且被滤掉的模型在 TTL 内会从「兜底缓存」里复活。
元数据（消耗倍率 / token 上限 / 支持性）随条目透传，双上游同名模型
逐字段补缺（先到先填，后到只补 None）。

**命名三字段（2026-10 起）**：六条渠道（codebuddy / trae / zen / kilo /
qoder / codearts）的每个模型统一成三个字段，规则见
`provider.naming`：

1. **raw_id**：渠道请求时真正发的 key，**永不改动**。各渠道内部代号互不相同
   （Qoder `kmodel_latest` = TRAE `kimi-k3-1`），也可能带命名空间前缀
   （kilo 的 `kilo-auto/free`、`stealth/space-bunny-alpha`）。
2. **归一键**（`normalize_model_key`）：剥掉上游的可用性噪声（免费档的
   `-free` / 路径段 `free`）与命名空间前缀，再 slug 化——`kilo-auto`、
   `longcat-2.5-preview`。**合并键**与多渠道条目对外 id 都用它。
3. **展示名**（`display_model_name`）：清洗后的可读文本 `LongCat 2.5
   Preview`。上游给了可读名就用它（Qoder `Qwen3.8-Max` → `Qwen3.8 Max`），
   没有就从 id 现派生（zen / kilo 多数如此）；品牌与缩写按表纠正大小写。

同渠道内归一键重复（CodeBuddy 的 `hy4-preview` / `hy4-preview-x` 都叫
「Hy4 Preview」）时冲突项退回原 id 的归一键，否则其中一个会被同键覆盖
而消失；对外 id 撞车时同理退回原 id（见 `_disambiguate`）。

**合并键**：统一按归一键（= 清洗后展示名再 slug 化）合并，六条渠道一视同仁
——原先 zen / kilo 被排除在按名合并之外，现在清洗规则一致后它们能与其它
渠道对齐（如 zen 的 `nemotron-3-ultra-free` 与 kilo 的
`nvidia/nemotron-3-ultra-550b-a55b:free` 并成一条）。哨兵名（`auto` /
`default`）是各上游自己的「自动路由 / 默认模型」占位，语义只在本渠道内成立，
故排除在跨渠道合并之外（见 `_SENTINEL_LABELS`），否则 kilo 的 `kilo-auto/free`
会与 Qoder 的 `auto` 误并、把请求路由到别的上游。除此之外仍要防同渠道重名
（CodeBuddy 的 `hy4-preview` / `hy4-preview-x`，见 `_merge_key`）。

**单渠道条目仍用上游原始 id**（Qoder `kmodel_latest`、kilo
`kilo-auto/free` 原样），只在多条渠道真正并到一起时才改用归一键。无论对外
id 是什么，**请求转发到某渠道时一律映射回该渠道自己登记的原 id**，映射表见
`services.model_aliases`，由 `executor.upstream_model_name` 消费。
`by_provider.{pid}.raw_id` 把每渠道的原 id 一并透给前端。

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
from ..provider.naming import display_model_name, normalize_model_key
from .deps import ApiKeyPrincipal, Services, api_key_user
from .model_catalog import load_catalog, save_catalog

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

# 哨兵名：各上游自己的「自动路由 / 默认模型」占位，语义只在本渠道内成立，不跨
# 渠道合并。kilo 的 `kilo-auto/free`（上游名 `Auto Free`）与 Qoder 的 `auto`
# 都归一成 `auto`，却是完全不同的东西；合并会把请求错误路由到别的上游。
_SENTINEL_LABELS = frozenset({"auto", "default"})

def _model_label(model: Model) -> str:
    """模型 → 展示名（清洗后）。

    上游给了可读名就用它（Qoder 的 `Qwen3.8-Max`、TRAE 的 `Kimi-K3`），没有就
    从 id 现派生（zen / kilo 多数如此）。两条路径都过 `display_model_name`，
    把 free 标记、命名空间前缀与分隔符统一掉，得到 `Qwen3.8 Max` / `LongCat
    2.5 Preview` 这类可直接展示的文本。清洗后为空（上游名就是 `free` 之类）
    返回空串，调用方回退原始 id。
    """
    return display_model_name(model.name or model.id)


def _sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    """定稿前的排序键：渠道优先级升序，同级按归一键字典序。

    归一键（`entry["key"]`）而非对外 id：`_sort_key` 要在 `_finalize` 之前
    跑（多渠道条目的对外 id 要等所有渠道到齐才能定），而 `_disambiguate` 需要
    一个确定的先后顺序来决定撞 id 时谁保留——按渠道优先级排，「靠前的渠道
    优先」与列表展示口径一致。

    合并后的条目至少有一个渠道（providers 由 `_merge_provider` 逐个 add），
    故 min() 不会作用于空集合。
    """
    rank = min(_PROVIDER_RANK.get(pid, _UNRANKED_PROVIDER)
               for pid in entry["providers"])
    return rank, entry["key"]


def _blocked(model_id: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(model_id, pattern) or fnmatch(model_id.lower(), pattern)
               for pattern in patterns)


def _merge_key(model: Model,
               duplicate_labels: set[str]) -> tuple[str, bool]:
    """一个模型计入哪个合并键，以及是否按展示名入的键。

    返回 `(键, 是否按名入键)`：第二个值必须由本函数判定，不能事后拿
    `键 == 归一展示名` 反推——重名退回时两者可能碰巧相等（CodeBuddy 的
    `hy4-preview` 与其展示名「Hy4 Preview」归一后同键），反推会把它误当成
    「按名入键」，进而把歧义的名字登记成别名。

    统一按**清洗后的展示名归一键**合并，六条渠道一视同仁：同一模型在各渠道
    的内部代号互不相同（Qoder `kmodel_latest` = TRAE `kimi-k3-1` = zen 的
    `kimi-k2.7-code-free`），连字符/大小写/free 后缀的差异都由归一吃掉，只
    有展示名能对齐。

    归一键是展示名再 slug 化（`Kimi-K3` → `kimi-k3`），因此两条路径永远同源，
    不会出现「名字对不上但 id 相同」的漏合并。

    以下情况退回上游 id 的归一键：

    * 归一键在本渠道内重复（如 CodeBuddy 的 `hy4-preview` / `hy4-preview-x`
      都叫「Hy4 preview」）——否则其中一个会被同键覆盖而消失；
    * 展示名清洗后为空（上游名就是 `free` 之类）；
    * 展示名是**哨兵名**（`auto` / `default` 等，见 `_SENTINEL_LABELS`）：
      这些是各上游自己的「自动路由 / 默认模型」占位，语义只在本渠道内成立，
      跨渠道合并会把请求错误地路由到别的上游（kilo 的 `kilo-auto/free` 与
      Qoder 的 `auto` 都归一成 `auto`，却完全不是同一个东西）。

    除哨兵外一律不退回原 id：原 id 是各渠道内部代号，跨渠道根本对不上，退回
    等于放弃合并（`kmodel_latest` 就永远无法与 `kimi-k3` 对齐了）。
    """
    label_key = normalize_model_key(_model_label(model))
    if not label_key or label_key in duplicate_labels:
        return normalize_model_key(model.id), False
    if label_key in _SENTINEL_LABELS:
        return normalize_model_key(model.id), False
    return label_key, True


def _merge_provider(grouped: dict[str, dict[str, Any]],
                    provider_id: str,
                    models_by_lower: dict[str, Model]) -> None:
    """把单个上游的 {小写 id: Model} 合并进 grouped。

    元数据逐字段补缺：先到的上游先填，后到的只补 None 字段，
    避免双上游同名模型互相覆盖已有信息。
    同时记录每渠道各自的元数据（provider_meta）与**上游原始 id**
    （raw_ids，请求转发时按渠道映射回去），供前端按渠道展示倍率。
    合并键见 `_merge_key`，别名表在定稿阶段统一登记（见 `_register_aliases`）。
    """
    # 先数本渠道内的重名（按清洗后的展示名归一键），用于把冲突项退回上游 id。
    label_keys = [normalize_model_key(_model_label(model))
                  for model in models_by_lower.values()]
    label_counts: dict[str, int] = {}
    for label_key in label_keys:
        label_counts[label_key] = label_counts.get(label_key, 0) + 1
    duplicate_labels = {key for key, count in label_counts.items() if count > 1}
    for model in models_by_lower.values():
        key, name_keyed = _merge_key(model, duplicate_labels)
        entry = grouped.setdefault(key, {"key": key, "providers": set(),
                                         "meta": dict.fromkeys(_META_FIELDS),
                                         "provider_meta": {}, "raw_ids": {},
                                         "labels": set(), "name_keyed": set()})
        entry["providers"].add(provider_id)
        entry["raw_ids"][provider_id] = model.id
        # 展示名按渠道各自记一份清洗后的文本：不同渠道给的名字详略不同
        # （TRAE 的 `Kimi-K3` vs 上游的 `Kimi-K3-长尾`），`_finalize` 取最短的
        # 那份当对外 id 与展示名。
        label = _model_label(model)
        if label:
            entry["labels"].add(label)
        # 本渠道这条确实按展示名入的键（而非重名退回的 id）：只有这时展示名
        # 才与该渠道的模型一一对应，`_finalize` 才敢把它登记成别名。
        if name_keyed:
            entry["name_keyed"].add(provider_id)
        meta = entry["meta"]
        provider_meta = {field: getattr(model, field) for field in _META_FIELDS}
        entry["provider_meta"][provider_id] = provider_meta
        for field in _META_FIELDS:
            # 空串按「未填」处理（`name`/`default_effort` 缺省为 ""）：先到的
            # 上游没名字时，后续有名字的上游仍能补上，而不是被空串占住。
            if meta[field] is None or meta[field] == "":
                meta[field] = provider_meta[field]


def _finalize(entry: dict[str, Any]) -> None:
    """定稿一个合并组：算出对外 id 与展示名。

    对外 id（`_sort_key` 与前端选择器里的值）：

    * 多渠道真正并到一起时用**展示名的归一键**（`kimi-k3` / `qwen3.8-max`），
      与前端展示一致；取最短的名字（`Kimi K3` 优先于 `Kimi K3 长尾`）；
    * 单渠道条目仍用**上游原始 id**（Qoder `kmodel_latest` 原样，kilo 的
      `kilo-auto/free` 原样），用户按原 id 直连、与历史记录一致。

    展示名同步为清洗后的文本：上游给的可读名优先（Qoder 的 `Qwen3.8-Max`
    → `Qwen3.8 Max`），没有就由 id 现派生。六条渠道一个口径，前端不再需要
    按渠道猜怎么美化。
    """
    providers = sorted(entry["providers"])
    labels = entry["labels"]
    if len(providers) > 1 and labels:
        representative = min(labels, key=lambda label: (len(label), label.lower()))
        entry["id"] = normalize_model_key(representative)
        entry["meta"]["name"] = representative
    else:
        entry["id"] = entry["raw_ids"][providers[0]]
        # 单渠道也统一给清洗后的展示名；连清洗结果都为空（上游名就是 `free`
        # 之类）才留空，由前端回退 id。
        entry["meta"]["name"] = min(labels, key=len) if labels else ""


def _disambiguate(entries: list[dict[str, Any]]) -> None:
    """把对外 id 撞车的那组退回原 id，保证对外 id 全局唯一。

    对外 id 有两个来源：多渠道条目取展示名归一键、单渠道条目取原 id，二者
    可能撞上（TRAE 的 `glm-5` 归一键 `glm-5`，而另一渠道恰好有个原 id 就叫
    `glm-5` 的单渠道模型）。撞车时两条 entry 会共用一个 id，前端选择器的
    `key` 与别名表都会互相覆盖。

    保留先定稿的那个（同 `_sort_key` 序，靠前的渠道优先），把后来的换成
    `_fallback_ids` 里的第一个未占用候选：主渠道原 id → 该 id 的归一形式 →
    归一键（grouped 的字典键，按构造唯一，最后一道兜底）。
    """
    taken: set[str] = set()
    for entry in entries:
        for candidate in (entry["id"], *_fallback_ids(entry)):
            if candidate and candidate.lower() not in taken:
                entry["id"] = candidate
                taken.add(candidate.lower())
                break
        else:
            # 三个候选全被占：归一键（grouped 的字典键）按构造唯一，作为最后
            # 一道兜底，保证对外 id 唯一。
            entry["id"] = entry["key"]
            taken.add(entry["key"])


def _fallback_ids(entry: dict[str, Any]) -> list[str]:
    """撞 id 时的候选替换值，按优先级排列。

    先退回该组的主渠道原 id（用户最熟悉的那个 key），再退回归一后的展示名
    （组内 key 本身唯一，仅当它已作为别的组的对外 id 用掉时才轮到）。
    """
    primary = entry["raw_ids"][sorted(entry["providers"])[0]]
    return [primary, normalize_model_key(primary), entry["key"]]


def _register_aliases(entries: list[dict[str, Any]],
                      aliases: dict[str, dict[str, str]]) -> None:
    """把「对外 id → 各渠道原 id」写进别名表。

    别名表按渠道分别登记（`kimi-k3` → qoder 的 `kmodel_latest`）：
    `executor.upstream_model_name` 在真正转发时据此换回各渠道能识别的原 id，
    **raw key 到此为止不再改动**。每渠道登记两条键——对外 id（用户从列表里
    选到的值）与原 id（手写 `kmodel_latest` 直连）；「按展示名入键」的渠道
    额外登记展示名键，用户按 `Kimi K3` 这类名字也能落到该渠道，但不登记
    同渠道重名项（CB 的 `hy4-preview` / `hy4-preview-x` 都叫「Hy4 Preview」，
    登记任一个都会指向错误模型）。
    """
    for entry in entries:
        for provider_id in sorted(entry["providers"]):
            provider_aliases = aliases.setdefault(provider_id, {})
            raw_id = entry["raw_ids"][provider_id]
            provider_aliases[entry["id"].lower()] = raw_id
            provider_aliases[raw_id.lower()] = raw_id
            # 「按展示名入键」的渠道额外登记展示名键：用户按 `Kimi K3` 或
            # `kimi-k3` 直连都能落到该渠道。不登记同渠道重名项（CB 的
            # `hy4-preview` / `hy4-preview-x` 都叫「Hy4 Preview」，登记任一个
            # 都会指向错误模型）。
            display = entry["meta"].get("name")
            if provider_id in entry["name_keyed"] and display:
                provider_aliases[str(display).lower()] = raw_id
                label_key = normalize_model_key(str(display))
                if label_key != str(display).lower():
                    provider_aliases[label_key] = raw_id


def _build_aliases(entries: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """定稿全部分组并产出别名表。"""
    for entry in entries:
        _finalize(entry)
    _disambiguate(entries)
    aliases: dict[str, dict[str, str]] = {}
    _register_aliases(entries, aliases)
    return aliases


def _entry_response(entry: dict[str, Any]) -> dict[str, Any]:
    """合并后的 grouped 条目 → OpenAI 兼容响应条目。

    多渠道模型额外给 `by_provider.{pid}`：各渠道倍率不同时前端按渠道分别
    展示；`raw_id` 一并透出，让用户能直接看到「选这个模型实际会发什么
    key」（kilo 的 `kilo-auto/free`、Qoder 的 `kmodel_latest`），排障不必
    再翻服务端日志。
    """
    result: dict[str, Any] = {
        "id": entry["id"], "object": "model", "owned_by": "Coding2API",
        "providers": sorted(entry["providers"]),
        # 空串按「无该字段」处理：`name`/`default_effort` 未提供时是 ""，
        # 透传空串会让前端显示空模型名或多余的 `default_effort: ""`。
        **{field: value for field, value in entry["meta"].items()
           if value is not None and value != ""},
    }
    by_provider: dict[str, dict[str, Any]] = {}
    for pid, meta in entry["provider_meta"].items():
        detail: dict[str, Any] = {"raw_id": entry["raw_ids"][pid]}
        if meta["credit_rate"] is not None:
            detail["credit_rate"] = meta["credit_rate"]
        by_provider[pid] = detail
    if len(entry["providers"]) > 1 and by_provider:
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


def merged_entries(services: Services, connected: set[str]) -> list[dict[str, Any]]:
    """按当前缓存表合并出全部条目（归一键 → 定稿后按 `_sort_key` 排序）。

    只看 `services.model_list_cache`，不看「本次拉到了什么」：这样缓存兜底、
    TTL 复用、落盘恢复三条路径共用同一段合并逻辑，行为一致。渠道过滤用
    `connected`（有可用凭证），与 `list_models` 的拉取范围保持同口径。
    """
    patterns = services.settings.blocklist_patterns
    grouped: dict[str, dict[str, Any]] = {}   # 归一键 → {providers, meta, raw_ids, ...}
    for provider_id, table in services.model_list_cache.items():
        if provider_id in connected and table:
            _merge_provider(grouped, provider_id, _visible(table, patterns))
    # 先按渠道优先级 + 归一键排序（对外 id 尚未算出），再定稿：`_disambiguate`
    # 要靠这个顺序决定对外 id 撞车时谁保留。定稿后顺序不再变化，输出即此序。
    return sorted(grouped.values(), key=_sort_key)


def publish_aliases(services: Services, connected: set[str]) -> None:
    """按当前缓存表重建别名表并就地 publish（executor 的闭包引用同一个 dict）。

    **逐渠道增量调用**（见 `list_models`）：某渠道拉完就 publish，不必等最慢的
    zen 探活（十几秒）——那段时间里已拉好的渠道本该已经能收窄候选，否则扁平名
    请求会按「全部渠道」扇出，真实打一轮不认这个模型的上游。
    """
    # 就地更新（executor 的映射闭包引用同一个 dict 对象）
    services.model_aliases.clear()
    services.model_aliases.update(_build_aliases(merged_entries(services, connected)))


async def _refresh_provider(services: Services, provider_id: str, provider: Any) -> None:
    """拉一条渠道的模型表：成功更新缓存并落盘，失败保留旧缓存（兜底）。

    缓存里存**未过滤**的原始表：过滤在每个出口现做，否则黑名单热更后缓存里
    仍是被滤前的旧结果（最长 TTL 才生效）。
    """
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
        if services.model_list_cache.get(provider_id):
            logger.warning("模型列表获取失败 %s，使用上次缓存: %s", provider_id, error)
        else:
            logger.warning("模型列表获取失败 %s: %s", provider_id, error)
        return
    services.model_list_cache[provider_id] = {model.id.lower(): model for model in models}
    services.model_list_fetched_at[provider_id] = time.monotonic()
    save_catalog(services.settings.data_dir, services.model_list_cache)


def _needs_refresh(services: Services, provider_id: str, *, force: bool,
                   now: float) -> bool:
    """该渠道是否需要（重新）打上游。

    TTL 按「上次尝试」计（失败也刷新）：TTL 内一律不打上游——有缓存就用缓存、
    没缓存就跳过，避免上游抖动时每次 `/v1/models` 都重跑一遍拉取（zen 探活的
    十几秒会叠加成一串慢请求）。
    """
    if force:
        return True
    fetched_at = services.model_list_fetched_at.get(provider_id)
    return fetched_at is None or now - fetched_at >= MODEL_LIST_TTL_SECONDS


def restore_model_catalog(services: Services) -> int:
    """把落盘目录灌回进程内缓存并**立即 publish 别名表**（同步、零上游请求）。

    启动时调用（`main.lifespan`，在后台预热之前）。解决的是「别名表为空的那段
    启动窗口」：这段时间里扁平名请求无法收窄候选，会真实打一轮不认这个模型的
    上游（CodeBuddy 11102 / TRAE 4001）。

    恢复出来的 zen 免费集顺带回填客户端的判活缓存（`seed_models_cache`），
    否则重启后预热仍要逐个真发探活（12–15s）——而目录里已经有这个答案了。

    返回恢复的渠道数（供日志/测试）。
    """
    connected = credential_providers(services)
    restored = 0
    for provider_id, table in load_catalog(services.settings.data_dir).items():
        # 未注册的渠道（换过的装配）与没有可用凭证的渠道都不恢复：目录里的
        # 快照不该让「用户已暂停的渠道」重新出现在模型列表里。
        if provider_id not in services.registry or provider_id not in connected:
            continue
        services.model_list_cache[provider_id] = table
        seeder = getattr(getattr(services.registry[provider_id], "client", None),
                         "seed_models_cache", None)
        if callable(seeder):
            seeder(list(table.values()))
        restored += 1
    if restored:
        publish_aliases(services, connected)
        logger.info("已从落盘目录恢复模型目录: %s", ",".join(sorted(services.model_list_cache)))
    return restored


async def list_models(services: Services, *, force: bool = False) -> dict:
    """跨上游拉取并合并模型列表。

    同一模型在各渠道的内部代号互不相同（Qoder `kmodel_latest` = TRAE
    `kimi-k3-1` = CodeBuddy `kimi-k3`），大小写、连字符、free 后缀、命名空间
    前缀的差异又各不相同；归一规则见 `provider.naming`：按清洗后的展示名归
    一键合并，providers 取并集，同时记录各上游的原始 id 供执行时映射回去。
    某上游拉取失败时用上次成功的缓存兜底，而不是让该上游从列表里消失。

    只遍历「当前有可用凭证」的渠道（见 `credential_providers`）：没接入 /
    全部暂停 / 会话失效的渠道不拉取也不展示，所以冷启动只有 zen / kilo（自带
    虚拟凭证），接入 CodeBuddy / TRAE 后下一次请求才把它们拉进来。

    force=False（默认）时 TTL 内直接复用缓存；启动预热传 force=True。
    **每拉完一条渠道就 publish 一次别名表**（`publish_aliases`），不等最慢的
    那条（zen 探活十几秒）——否则这段时间里别名表空着，扁平名请求会扇出到
    不认该模型的上游。
    黑名单在每个出口现算（缓存不做过滤），因此改完黑名单下一次调用立即生效。
    输出顺序按 `_sort_key`：CB / TR 渠道的模型优先，其余渠道在后。
    """
    connected = credential_providers(services)
    now = time.monotonic()
    for provider_id, provider in services.registry.items():
        # 没凭证的渠道直接跳过：不拉取、不展示。已接入的渠道即使本次拉取失败，
        # 也会走缓存兜底，不会因为一次抖动就从列表里消失。
        if provider_id not in connected:
            continue
        if not _needs_refresh(services, provider_id, force=force, now=now):
            continue
        await _refresh_provider(services, provider_id, provider)
        publish_aliases(services, connected)
    entries = merged_entries(services, connected)
    aliases = _build_aliases(entries)
    # 就地更新（executor 的映射闭包引用同一个 dict 对象）
    services.model_aliases.clear()
    services.model_aliases.update(aliases)
    return {"object": "list", "data": [_entry_response(entry)
                                      for entry in entries]}


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_v1_models(_principal: ApiKeyPrincipal = Depends(api_key_user)):
        return await list_models(services)

    return router
