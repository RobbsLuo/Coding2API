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
只影响列表展示；直连指定被滤模型不受影响。模式按**归一后的对外写法**匹配
（原代号、原代号的归一键、展示名、展示名的归一键四种写法任一命中即滤，见
`_block_names`），用户照列表里看到的归一键 / 展示名写也能生效。

**缓存存的是未过滤列表，过滤在每个出口现做**：MODEL_BLOCKLIST 是可热更项
（Q34「改完立即生效」），若把过滤结果存进缓存，改完黑名单要等 TTL（300s）
才反映到 Playground，且被滤掉的模型在 TTL 内会从「兜底缓存」里复活。
元数据（消耗倍率 / token 上限 / 支持性）随条目透传，双上游同名模型
逐字段补缺（先到先填，后到只补 None）。

**能力分（2026-10 起）**：每个条目可带可选 `benchmarks` 字段——Artificial
Analysis 的三项指数（智能 / 编程 / 智能体），经 OpenRouter 公开接口分发
（`src/benchmarks.py`）。匹配口径与价表共用 `model_match.lookup`：按对外 id
与展示名两路取候选键等值查表，**唯一命中才采用**，查不到或有歧义就不带该
字段（宁可不配也不错配）。渠道内部占位模型（`custom_model_*`、`*_subagent`）
本来就不该有分，匹配不到即无字段。拉取失败时整表为空，效果同样是「没有
字段」——模型列表本身不受影响。数据是第三方成绩，不是本服务实测，故随条目
透出 `source` 与 `source_model`（上游原始 id）供溯源。

**命名三字段（2026-10 起）**：六条渠道（codebuddy / trae / zen / kilo /
qoder / codearts）的每个模型统一成三个字段，规则见
`provider.naming`：

1. **raw_id**：渠道请求时真正发的 key，**永不改动**。各渠道内部代号互不相同
   （Qoder `kmodel_latest` = TRAE `kimi-k3-1`），也可能带厂商命名空间前缀
   （kilo 的 `kilo-auto/free`、`nvidia/nemotron-3-ultra-550b-a55b:free`）。
2. **归一键**（`normalize_model_key`）：剥掉噪声标记（免费尾缀 `-free`/`_free`/
   `:free`、路径段 `free`、括号词 `(free)`；新版标记 `-new` / `(new)`）与厂商
   命名空间前缀，再 slug 化——`kilo-auto`、`longcat-2.5-preview`。
   **合并键与全部条目的对外 id 都用它**。
3. **展示名**（`display_model_name`）：清洗后的可读文本 `LongCat 2.5
   Preview`。上游给了可读名就用它（Qoder `Qwen3.8-Max` → `Qwen3.8 Max`），
   没有就从 id 现派生（zen / kilo 多数如此）；品牌与缩写按表纠正大小写。

同渠道内归一键重复（CodeBuddy 的 `hy4-preview` / `hy4-preview-x` 都叫
「Hy4 Preview」）时冲突项用渠道限定键隔离（**不**跨渠道合并），对外 id 取
原代号的归一键；对外 id 真撞车时再依次退回（见 `_disambiguate`）。

**合并键**：统一按归一键（= 清洗后展示名再 slug 化）合并，六条渠道一视同仁
——原先 zen / kilo 被排除在按名合并之外，现在清洗规则一致后它们能与其它
渠道对齐（如 zen 的 `nemotron-3-ultra-free` 与 kilo 的
`nvidia/nemotron-3-ultra-550b-a55b:free` 并成一条）。哨兵名（`auto` /
`default`）是各上游自己的「自动路由 / 默认模型」占位，语义只在本渠道内成立，
故排除在跨渠道合并之外（见 `_SENTINEL_LABELS`），否则 kilo 的 `kilo-auto/free`
会与 Qoder 的 `auto` 误并、把请求路由到别的上游。除此之外仍要防同渠道重名
（CodeBuddy 的 `hy4-preview` / `hy4-preview-x`，见 `_merge_key`）。

**对外 id 一律是归一键**（去 free / 去厂商前缀的 slug）：`kimi-k3`、
`longcat-2.5-preview`、`kilo-auto`，六条渠道一个口径。原代号不在对外 id 里
露面，但**请求转发到某渠道时一律映射回该渠道自己登记的原代号**，映射表见
`services.model_aliases`，由 `executor.upstream_model_name` 消费；
`by_provider.{pid}.raw_id` 把每渠道的原代号一并透给前端。用户按原代号、
展示名、归一键三种写法都能直连命中。

**展示顺序**：CodeBuddy / TRAE 的模型排在前面（`_PROVIDER_RANK`：
codebuddy 0 → trae 1 → qoder 2 → codearts 3 → 其他 4），组内仍按模型名字典序；
多渠道模型按其最高优先级渠道归位（含 CB 即进第一段，含 TR 即进第二段）。
仅影响 `/v1/models` 与 Playground 的展示顺序，不影响调度选号。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from fnmatch import fnmatch
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..benchmarks import BenchmarkTable
from ..model_match import lookup
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

# 拉取串行锁：模块级单锁（一次只有一个进程内的列表刷新在跑）。调用方有 HTTP
# 出口与后台兜底刷新两处，不串行就会同时打同一条渠道的上游。
_refresh_lock = asyncio.Lock()

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
    把 free 标记、新版标记、命名空间前缀与分隔符统一掉，得到 `Qwen3.8 Max` /
    `LongCat 2.5 Preview` 这类可直接展示的文本。清洗后为空（上游名就是 `free`
    之类）返回空串，调用方回退原始 id。
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


def _block_names(model: Model) -> tuple[str, ...]:
    """一个模型可供黑名单匹配的全部写法。

    对外 id 改为归一键后，用户照列表里看到的**归一键 / 展示名**写黑名单
    （`kimi-k3`、`Kimi K3`）也必须命中；同时保留按上游**原代号**写的老规则
    （默认值里的 `custom_model_*` / `browser_use_*` 带下划线，归一键把下划线
    换成连字符后并不命中，只有原代号能匹配）。故四种写法都参与：

    * 原代号（`kmodel_latest`、`custom_model_claude`）；
    * 原代号的归一键（兜底/哨兵条目的对外 id，Kilo `kilo-auto/free` → `kilo-auto`）；
    * 展示名（`Kimi K3`）；
    * 展示名的归一键（正常条目的对外 id，`kimi-k3`）。
    """
    label = _model_label(model)
    names = {model.id, normalize_model_key(model.id), label,
             normalize_model_key(label)}
    names.discard("")   # 清洗后退化的空名不参与匹配（否则 `*` 会命中空串）
    return tuple(names)


def _blocked(model: Model, patterns: tuple[str, ...]) -> bool:
    """该模型是否命中黑名单：任一种对外写法命中任一 glob 即滤。

    两边都忽略大小写：用户照展示名写（`Kimi K3`）与照归一键写（`kimi-k3`）
    应等价命中，不能只把被匹配方转小写、模式原样比对（`Kimi-K3` 会漏）。
    """
    return any(fnmatch(name.lower(), pattern.lower())
               for name in _block_names(model)
               for pattern in patterns)


def _merge_key(model: Model,
               provider_id: str,
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

    以下情况用**渠道限定键**（`渠道\\0归一原 id`）隔离，绝不跨渠道合并：

    * 归一键在本渠道内重复（如 CodeBuddy 的 `hy4-preview` / `hy4-preview-x`
      都叫「Hy4 preview」）——否则其中一个会被同键覆盖而消失；
    * 展示名清洗后为空（上游名就是 `free` 之类）；
    * 展示名是**哨兵名**（`auto` / `default` 等，见 `_SENTINEL_LABELS`）：
      这些是各上游自己的「自动路由 / 默认模型」占位，语义只在本渠道内成立，
      跨渠道合并会把请求错误地路由到别的上游（kilo 的 `kilo-auto/free` 与
      Qoder 的 `auto` 都归一成 `auto`，却完全不是同一个东西）。

    除上述三种外一律不退回原 id：原 id 是各渠道内部代号，跨渠道根本对不上，
    退回等于放弃合并（`kmodel_latest` 就永远无法与 `kimi-k3` 对齐了）。
    """
    label_key = normalize_model_key(_model_label(model))
    if not label_key or label_key in duplicate_labels:
        return _scoped_key(provider_id, model.id), False
    if label_key in _SENTINEL_LABELS:
        return _scoped_key(provider_id, model.id), False
    return label_key, True


def _scoped_key(provider_id: str, raw_id: str) -> str:
    """渠道限定的合并键：只用于隔离，不作为对外 id（见 `_finalize`）。

    `\\0` 不会出现在任何上游 id 里，故限定键与普通归一键永不冲突。
    """
    return f"{provider_id}\x00{normalize_model_key(raw_id)}"


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
        key, name_keyed = _merge_key(model, provider_id, duplicate_labels)
        entry = grouped.setdefault(key, {"key": key, "providers": set(),
                                         "meta": dict.fromkeys(_META_FIELDS),
                                         "provider_meta": {}, "raw_ids": {},
                                         "labels": set(), "name_keyed": set(),
                                         "scoped": "\x00" in key})
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

    对外 id（`/v1/models` 的 `id`、前端选择器里的值）**一律是归一键**，
    去掉 free 标记与厂商前缀后的干净 slug——与前端展示一致，六条渠道一个口径：

    * 多渠道真正并到一起时取最短展示名的归一键（`Kimi K3` 优先于
      `Kimi K3 长尾`），如 `kimi-k3` / `qwen3.8-max`；
    * 单渠道条目取该条展示名的归一键（zen `longcat-2.5-preview-free` →
      `longcat-2.5-preview`、Qoder `kmodel_latest` → `kimi-k3`）；
    * **兜底/哨兵条目**（展示名重复、缺失，或 `auto`/`default` 这类只在本渠道
      内成立的占位）取**原代号的归一键**（kilo `kilo-auto/free` → `kilo-auto`），
      与其它渠道的同类条目天然区分。

    展示名同步为清洗后的文本：上游给的可读名优先（Qoder 的 `Qwen3.8-Max`
    → `Qwen3.8 Max`），没有就由 id 现派生。

    原代号不在对外 id 里露面，但**请求转发时一律经别名表换回**（见
    `_register_aliases`），用户按原代号直连仍能命中；`by_provider.{渠道}.raw_id`
    把每渠道原代号透给前端。
    """
    providers = sorted(entry["providers"])
    labels = entry["labels"]
    raw = entry["raw_ids"][providers[0]]
    if len(providers) > 1 and labels:
        representative = min(labels, key=lambda label: (len(label), label.lower()))
        entry["id"] = normalize_model_key(representative)
        entry["meta"]["name"] = representative
        return
    # 单渠道：展示名清洗后作为展示名；对外 id 走归一键。
    representative = min(labels, key=len) if labels else ""
    entry["meta"]["name"] = representative
    if entry["scoped"] or not representative:
        # 兜底/哨兵条目：展示名只在渠道内成立，用原代号的归一键当对外 id
        entry["id"] = normalize_model_key(raw)
    else:
        entry["id"] = normalize_model_key(representative)


def _disambiguate(entries: list[dict[str, Any]]) -> None:
    """保证对外 id 全局唯一：撞车时后者退回原代号的归一键。

    对外 id 都取归一键，多数情况天然唯一（同一模型的不同渠道写法会并成一条）；
    但仍可能撞：兜底/哨兵条目用**原代号的归一键**，可能恰好等于另一条正常
    条目的展示名归一键。撞车时两条 entry 共用一个 id 会让前端选择器的 `key`
    与别名表互相覆盖。

    保留先定稿的那个（同 `_sort_key` 序，靠前的渠道优先），把后来的换成
    `_fallback_ids` 里的第一个未占用候选；全都被占则追加渠道名，再不行加数字
    后缀，保证一定唯一。
    """
    taken: set[str] = set()
    for entry in entries:
        for candidate in (entry["id"], *_fallback_ids(entry)):
            if candidate and candidate.lower() not in taken:
                entry["id"] = candidate
                taken.add(candidate.lower())
                break
        else:
            entry["id"] = _unique_suffix(entry["id"], taken)
            taken.add(entry["id"].lower())


def _unique_suffix(base: str, taken: set[str]) -> str:
    """`base` 已被占用时追加数字后缀直到唯一（最后一道兜底）。"""
    index = 2
    while f"{base}-{index}".lower() in taken:
        index += 1
    return f"{base}-{index}"


def _fallback_ids(entry: dict[str, Any]) -> list[str]:
    """撞 id 时的候选替换值，按优先级排列。

    主候选是主渠道原代号的归一键（用户最熟悉的 key），其次再带上渠道名后缀
    （两个渠道的原代号归一键相同时区分）。
    """
    providers = sorted(entry["providers"])
    primary = entry["raw_ids"][providers[0]]
    normalized = normalize_model_key(primary)
    return [normalized, f"{normalized}-{providers[0]}"]


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


def _entry_response(entry: dict[str, Any],
                    benchmarks: BenchmarkTable | None = None) -> dict[str, Any]:
    """合并后的 grouped 条目 → OpenAI 兼容响应条目。

    多渠道模型额外给 `by_provider.{pid}`：各渠道倍率不同时前端按渠道分别
    展示；`raw_id` 一并透出，让用户能直接看到「选这个模型实际会发什么
    key」（kilo 的 `kilo-auto/free`、Qoder 的 `kmodel_latest`），排障不必
    再翻服务端日志。

    `benchmarks` 非空时按对外 id 与展示名两路查能力分（唯一命中才带），
    查不到就不带该字段——调用方不传（默认 None）时行为与加该字段前完全一致。
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
    score = lookup(benchmarks or {}, entry["id"], entry["meta"].get("name"))
    if score is not None:
        result["benchmarks"] = score
    return result


def _visible(models_by_lower: dict[str, Model],
             patterns: tuple[str, ...]) -> dict[str, Model]:
    """按当前黑名单过滤一个上游的模型表（缓存里存的是未过滤的原始表）。"""
    return {lower: model for lower, model in models_by_lower.items()
            if not _blocked(model, patterns)}


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


def _publish(services: Services, entries: list[dict[str, Any]]) -> None:
    """定稿好的条目 → 就地更新别名表（executor 的闭包引用同一个 dict 对象）。"""
    services.model_aliases.clear()
    services.model_aliases.update(_build_aliases(entries))


def publish_aliases(services: Services, connected: set[str]) -> None:
    """按当前缓存表重建别名表并就地 publish。

    **逐渠道增量调用**（见 `list_models`）：某渠道拉完就 publish，不必等最慢的
    zen 探活（十几秒）——那段时间里已拉好的渠道本该已经能收窄候选，否则扁平名
    请求会按「全部渠道」扇出，真实打一轮不认这个模型的上游。
    """
    _publish(services, merged_entries(services, connected))


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


def _needs_refresh(services: Services, provider_id: str, *, now: float) -> bool:
    """该渠道是否需要（重新）打上游。

    TTL 按「上次尝试」计（失败也刷新，restore 也播种）：TTL 内一律不打上游
    ——有缓存就用缓存、没缓存就跳过，避免上游抖动时每次 `/v1/models` 都重跑
    一遍拉取（zen 探活的十几秒会叠加成一串慢请求）。
    """
    fetched_at = services.model_list_fetched_at.get(provider_id)
    return fetched_at is None or now - fetched_at >= MODEL_LIST_TTL_SECONDS


def restore_model_catalog(services: Services) -> int:
    """把落盘目录灌回进程内缓存并**立即 publish 别名表**（同步、零上游请求）。

    启动时调用（`main.lifespan`，在后台预热之前）。解决的是「别名表为空的那段
    启动窗口」：这段时间里扁平名请求无法收窄候选，会真实打一轮不认这个模型的
    上游（CodeBuddy 11102 / TRAE 4001）。

    同时把快照年龄折进 `model_list_fetched_at`：快照不只是数据，还带新鲜度。
    不播种的话 TTL 时间戳是空的，启动预热（`_needs_refresh` 见无时间戳即刷新）
    会把刚恢复的表全量重拉一遍——kilo 实测 10–22s、zen 探活 12–15s，白花。

    返回恢复的渠道数（供日志/测试）。
    """
    connected = credential_providers(services)
    restored = 0
    now = time.time()
    for provider_id, (saved_at, table) in load_catalog(
            services.settings.data_dir, now=now).items():
        # 未注册的渠道（换过的装配）与没有可用凭证的渠道都不恢复：目录里的
        # 快照不该让「用户已暂停的渠道」重新出现在模型列表里。
        if provider_id not in services.registry or provider_id not in connected:
            continue
        services.model_list_cache[provider_id] = table
        services.model_list_fetched_at[provider_id] = (
            time.monotonic() - max(0.0, now - saved_at))
        restored += 1
    if restored:
        publish_aliases(services, connected)
        logger.info("已从落盘目录恢复模型目录: %s", ",".join(sorted(services.model_list_cache)))
    return restored


async def _refresh_providers(services: Services, connected: set[str]) -> None:
    """同步拉取所有「需要刷新」的渠道，每拉完一条就 publish 一次别名表。

    没凭证的渠道直接跳过：不拉取、不展示。已接入的渠道即使本次拉取失败，也会
    走缓存兜底，不会因为一次抖动就从列表里消失。
    **每拉完一条渠道就 publish**（`publish_aliases`），不等最慢的那条
    （zen 探活十几秒）——否则这段时间里别名表空着，扁平名请求会扇出到不认该
    模型的上游。整段串行化（`_refresh_lock`）：HTTP 出口与后台兜底刷新两处都
    可能进来，无锁时会同时对同一条渠道打上游（zen 那次是十几秒真推理），后到
    的拿到的还是同一份数据，纯属白打。
    """
    async with _refresh_lock:
        now = time.monotonic()
        for provider_id, provider in services.registry.items():
            if provider_id not in connected:
                continue
            if not _needs_refresh(services, provider_id, now=now):
                continue
            await _refresh_provider(services, provider_id, provider)
            publish_aliases(services, connected)


def _schedule_refresh(services: Services, connected: set[str],
                      stale: set[str]) -> None:
    """把「过期渠道」的刷新丢到后台（去重 + 完成后自动摘除）。

    HTTP 出口专用：**有缓存就直接返回旧列表**，把 zen 探活的十几秒从请求路径
    挪走——此前 TTL 一过就同步重拉全部渠道，Playground 每次卡十几到三十秒。
    代价是列表最多滞后一个 TTL：TTL 到期后的第一次请求仍回旧数据，后台刷新
    完成、下一次请求才转新鲜。

    去重：已有刷新在途就不再排新的（`model_refreshing`）；`_refresh_providers`
    里还有 `_refresh_lock`，但那是「多请求串行化」，这里再挡一层避免同一渠道被
    反复排队。任务句柄进 `model_refresh_tasks`，lifespan 关闭时统一取消。
    """
    if services.model_refreshing:
        return
    services.model_refreshing.update(stale)

    async def run() -> None:
        try:
            await _refresh_providers(services, connected)
        except Exception as error:  # noqa: BLE001 - 后台刷新失败只记日志
            logger.warning("后台模型列表刷新失败: %s", error)
        finally:
            services.model_refreshing.difference_update(stale)

    task = asyncio.create_task(run())
    services.pending_model_refreshes.append(task)
    services.model_refresh_tasks.add(task)

    def _done(_task: asyncio.Task) -> None:
        services.model_refresh_tasks.discard(_task)
        with contextlib.suppress(ValueError):
            services.pending_model_refreshes.remove(_task)

    task.add_done_callback(_done)


def _build_response(services: Services, connected: set[str],
                    allowed_models: str = "",
                    benchmarks: BenchmarkTable | None = None) -> dict:
    """按当前缓存合并出响应（不碰上游），并就地 publish 别名表。

    `allowed_models` 非空时按该 Key 的模型白名单过滤展示（P0-3）：白名单是
    Key 级策略，执行时的权威判定在出口（`model_allowed`），这里只是让
    `/v1/models` 与 Key 实际能用的模型一致。

    `benchmarks` 为能力分表（`app.state.model_benchmarks`）；None/空表时条目
    不带 `benchmarks` 字段，与加该字段前的行为一致。
    """
    entries = merged_entries(services, connected)
    _publish(services, entries)
    if allowed_models:
        from ..auth.access import model_allowed
        entries = [entry for entry in entries
                   if model_allowed(entry["id"], allowed_models)]
    return {"object": "list", "data": [_entry_response(entry, benchmarks)
                                       for entry in entries]}


async def serve_models(services: Services, allowed_models: str = "",
                       request: Request | None = None) -> dict:
    """HTTP 出口（`/v1/models`、Playground）：先回旧列表，过期渠道后台刷。

    stale-while-revalidate：TTL 到期不再把 zen 探活的十几秒压在请求上。只有某
    渠道**一条缓存都没有**时（冷启动无落盘快照 / 新接入渠道）才同步等它一次
    ——否则列表会缺一条渠道的模型。落盘快照恢复后几乎所有渠道都有缓存，故稳态
    请求不再阻塞；代价是列表最多滞后一个 TTL（到期后第一次仍回旧数据，后台刷
    新完成后下一次才转新鲜）。

    后台刷新任务收敛在 `services.model_refresh_tasks` / `pending_model_refreshes`，
    `model_refreshing` 去重，lifespan 关闭时统一取消。

    `request` 非空时从 `request.app.state.model_benchmarks` 现读能力分表并随
    条目透出（与 `models_dev_catalog` 同一形态：运行数据挂 app.state，不是
    Services 依赖）。不传（测试 / 内部调用）时条目不带 `benchmarks` 字段。
    """
    connected = credential_providers(services)
    stale = {provider_id for provider_id in connected
             if _needs_refresh(services, provider_id, now=time.monotonic())}
    if any(not services.model_list_cache.get(provider_id) for provider_id in stale):
        # 无可回退模型（冷启动无快照 / 新接入渠道）：必须同步拉一次
        await _refresh_providers(services, connected)
    elif stale:
        _schedule_refresh(services, connected, stale)
    benchmarks = getattr(request.app.state, "model_benchmarks", None) if request else None
    return _build_response(services, connected, allowed_models, benchmarks)


async def list_models(services: Services,
                      benchmarks: BenchmarkTable | None = None) -> dict:
    """同步拉取并合并模型列表（供后台预热 / 兜底刷新循环）。

    同一模型在各渠道的内部代号互不相同（Qoder `kmodel_latest` = TRAE
    `kimi-k3-1` = CodeBuddy `kimi-k3`），大小写、连字符、free 后缀、命名空间
    前缀的差异又各不相同；归一规则见 `provider.naming`：按清洗后的展示名归
    一键合并，providers 取并集，同时记录各上游的原始 id 供执行时映射回去。
    某上游拉取失败时用上次成功的缓存兜底，而不是让该上游从列表里消失。

    只遍历「当前有可用凭证」的渠道（见 `credential_providers`）：没接入 /
    全部暂停 / 会话失效的渠道不拉取也不展示，所以冷启动只有 zen / kilo（自带
    虚拟凭证），接入 CodeBuddy / TRAE 后下一次请求才把它们拉进来。

    TTL 内直接复用缓存（`model_list_fetched_at`，落盘恢复时按快照年龄播种）；
    **每拉完一条渠道就 publish 一次别名表**（`publish_aliases`），不等最慢的
    那条（zen 探活十几秒）——否则这段时间里别名表空着，扁平名请求会扇出到
    不认该模型的上游。黑名单在每个出口现算（缓存不做过滤），因此改完黑名单
    下一次调用立即生效。输出顺序按 `_sort_key`：CB / TR 渠道的模型优先。

    整段串行化（`_refresh_lock`，见 `_refresh_providers`）。后台任务要的是
    「真的刷新」，所以这条保持同步；HTTP 出口走 `serve_models`（SWR）。
    """
    connected = credential_providers(services)
    await _refresh_providers(services, connected)
    return _build_response(services, connected, "", benchmarks)


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_v1_models(request: Request,
                             principal: ApiKeyPrincipal = Depends(api_key_user)):
        return await serve_models(services, principal.allowed_models, request)

    return router
