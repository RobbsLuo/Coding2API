"""模型列表的归一 / 合并 / 命名规则（纯函数，不依赖 Services）。

本模块只管「一组上游模型表 → 对外条目 + 别名表」的纯计算，缓存 / TTL /
拉取编排在 `api.models`。拆开是因为合并规则本身就占了大半篇幅（六条渠道的
命名对齐、重名退回、对外 id 消歧），与服务层的缓存生命周期无关。

**命名三字段**（2026-10 起）：六条渠道（codebuddy / trae / zen / kilo /
qoder / codearts）的每个模型统一成三个字段，规则见 `provider.naming`：

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

**能力分（2026-10 起）**：每个条目可带可选 `benchmarks` 字段——Artificial
Analysis 的三项指数（智能 / 编程 / 智能体），经 OpenRouter 公开接口分发
（`src/benchmarks.py`）。匹配口径与价表共用 `model_match.lookup`：按对外 id
与展示名两路取候选键等值查表，**唯一命中才采用**，查不到或有歧义就不带该
字段（宁可不配也不错配）。渠道内部占位模型（`custom_model_*`、`*_subagent`）
本来就不该有分，匹配不到即无字段。数据是第三方成绩，不是本服务实测，故随条目
透出 `source` 与 `source_model`（上游原始 id）供溯源。

**黑名单过滤在每个出口现做**：`_block_names` / `_blocked` 按**归一后的对外
写法**匹配（原代号、原代号的归一键、展示名、展示名的归一键四种写法任一命中即
滤），用户照列表里看到的归一键 / 展示名写也能生效。
"""

from __future__ import annotations

import logging
from fnmatch import fnmatch
from typing import Any

from ..benchmarks import BenchmarkTable
from ..engine.reasoning import DELETE_EFFORT, WILDCARD_CHANNEL
from ..model_alias import ModelAlias
from ..model_match import lookup
from ..pricing import PriceTable
from ..provider.base import Model
from ..provider.naming import display_model_name, normalize_model_key  # noqa: F401

logger = logging.getLogger(__name__)

# 响应透传的元数据字段（Model → OpenAI 额外字段）。`name` 放在最前：
# 上游人类可读名（Qoder 的 `Qwen3.8-Max`），前端优先展示它而非内部代号。
_META_FIELDS = ("name", "credit_rate", "max_input_tokens", "max_output_tokens",
                "supports_images", "supports_tool_call",
                "supports_reasoning", "default_effort")

# 展示排序权重：CodeBuddy / TRAE 优先，其余渠道（zen / kilo 等）在后；
# 多渠道模型取所有渠道里的最小权重（含 CB 即进第一段）。
_PROVIDER_RANK = {"codebuddy": 0, "codebuddy_intl": 1, "trae": 2, "qoder": 3,
                  "codearts": 4, "officeace": 5}
_UNRANKED_PROVIDER = 6

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
               duplicate_labels: set[str],
               provider_id_keys: set[str]) -> tuple[str, bool]:
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

    * 展示名清洗后为空（上游名就是 `free` 之类）；
    * 展示名是**哨兵名**（`auto` / `default` 等，见 `_SENTINEL_LABELS`）：
      这些是各上游自己的「自动路由 / 默认模型」占位，语义只在本渠道内成立，
      跨渠道合并会把请求错误地路由到别的上游（kilo 的 `kilo-auto/free` 与
      Qoder 的 `auto` 都归一成 `auto`，却完全不是同一个东西）。

    **渠道内展示名重名时，先退回 id 归一键再考虑限定键**（2026-10-12）。上游
    把多个不同模型共用一个展示名是常态：CodeBuddy 的 `hy4-preview` /
    `hy4-preview-x` 都叫「Hy4 preview」，`hy3` / `hy3-x` 都叫「Hy3」。原来
    一律退回渠道限定键，代价是这些模型**在本渠道内就断了跨渠道合并**——
    国际站的 `hy4-preview`（无 `-x` 变体）本该与国内站合成一条，却因撞上被
    国内的污染键而被迫独立成条，对外多出 `hy4-preview-codebuddy_intl`、
    `deepseek-v4.1-flash-codebuddy_intl` 这类后缀。

    改判据：展示名重名时，若本渠道内**存在某个 id 归一后等于该 label key 的
    模型**，就用这个归一键当合并键（并按「未按名入键」处理，不登记别名）——
    重名的那几个模型靠**不同的 id 归一值**自然分开，且其中 id 自洽的那个
    重新获得了跨渠道合并能力。实测只有 TRAE 的 `sagitta` / `aquila`（展示名
    相同且都没有同名 id）会退到限定键，行为与改动前一致。

    除上述外一律不退回原 id：原 id 是各渠道内部代号，跨渠道根本对不上，
    退回等于放弃合并（`kmodel_latest` 就永远无法与 `kimi-k3` 对齐了）。
    """
    label_key = normalize_model_key(_model_label(model))
    if not label_key or label_key in _SENTINEL_LABELS:
        return _scoped_key(provider_id, model.id), False
    if label_key in duplicate_labels and label_key not in provider_id_keys:
        return _scoped_key(provider_id, model.id), False
    # 展示名在本渠道内撞车：用 id 归一值当键（重名者 id 不同故自然分开），
    # 但不算「按名入键」——那个展示名有歧义，登记成别名会把请求路由到错的模型
    return (label_key if label_key not in duplicate_labels
            else normalize_model_key(model.id)), label_key not in duplicate_labels


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
    # 先数本渠道内的重名（按清洗后的展示名归一键），用于把冲突项退回 id 键。
    label_keys = [normalize_model_key(_model_label(model))
                  for model in models_by_lower.values()]
    label_counts: dict[str, int] = {}
    for label_key in label_keys:
        label_counts[label_key] = label_counts.get(label_key, 0) + 1
    duplicate_labels = {key for key, count in label_counts.items() if count > 1}
    # 本渠道全部 id 的归一值：展示名重名时用它判断「有没有哪个模型的 id 自洽」
    # （见 `_merge_key`）。用整个渠道的 id 集合而非只算重名组，因为 id 自洽的
    # 那个模型本身的 label 可能不重名——判据是**存在**即可，不必同组。
    provider_id_keys = {normalize_model_key(model.id)
                        for model in models_by_lower.values()}
    for model in models_by_lower.values():
        key, name_keyed = _merge_key(model, provider_id, duplicate_labels,
                                      provider_id_keys)
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


def _find_target_entry(entries: list[dict[str, Any]],
                       model: str) -> dict[str, Any] | None:
    """按对外 id / 展示名 / 任一渠道原 id 找一条已定稿条目（大小写不敏感）。

    与 `api.models._match_entry` 同口径：别名目标写成这三种任一形式都能命中。
    只匹配**真实**条目（自定义别名在全部真实条目解析完之后才注入，故不会形成
    别名链——链式别名会让「A→B→C」的失败模式变得不可解释）。
    """
    target = model.strip().lower()
    if not target:
        return None
    for entry in entries:
        if entry["id"].lower() == target:
            return entry
        name = entry["meta"].get("name")
        if isinstance(name, str) and name.lower() == target:
            return entry
        if any(str(raw).lower() == target for raw in entry["raw_ids"].values()):
            return entry
    return None


def _alias_entry(alias: ModelAlias, target: dict[str, Any]) -> dict[str, Any]:
    """把一条自定义别名按目标条目克隆成对外条目（id 换成别名）。

    克隆而非共享目标条目：别名要带自己的 id 与 `custom_alias` 标记，而
    `_entry_response` / `_disambiguate` 都按条目就地写字段。`raw_ids` /
    `provider_meta` 按已钉的渠道裁剪——别名钉了 `@渠道` 时列表只该显示那一
    个渠道，请求也只该路由到它。
    """
    providers = (sorted(target["providers"]) if alias.provider is None
                 else [alias.provider])
    raw_ids = {pid: target["raw_ids"][pid] for pid in providers}
    provider_meta = {pid: target["provider_meta"][pid] for pid in providers}
    meta = dict(target["meta"])
    # 展示名用别名本身：用户在列表里选到的值与实际请求值必须一致，否则
    # 「列表显示 A、请求 A 却 404」的错配又要靠翻服务端日志才发现。
    meta["name"] = alias.name
    return {
        "key": alias.name.lower(), "id": alias.name, "providers": set(providers),
        "meta": meta, "provider_meta": provider_meta, "raw_ids": raw_ids,
        "labels": set(), "name_keyed": set(), "scoped": False,
        "custom_alias": True, "alias_target": target["id"],
    }


def _apply_custom_aliases(entries: list[dict[str, Any]],
                          custom: dict[str, ModelAlias],
                          aliases: dict[str, dict[str, str]]) -> None:
    """把用户自定义别名注入条目表与别名表（**列表与执行侧同一份**）。

    必须在 `_register_aliases` 之后跑：撞车判据是「真实对外 id 或已存在的自动
    别名」，两者此时才齐。撞任何一项都**跳过该段并记明确错误**——静默覆盖真实
    id 会让用户在列表里选到的模型变成另一个，比配置写错本身危险得多。

    注入的是**条目**（不只是别名表）：`/v1/models` 与 `GET /v1/models/{id}`
    都从条目渲染，只写别名表会出现「请求能用、列表里没有」。别名表里给每个
    候选渠道登记「别名 → 该渠道原 id」，执行侧 `upstream_model_name` /
    `_narrow_providers` 据此路由到目标渠道。
    """
    if not custom:
        return
    # 只在**真实条目**里找目标：别名链（A→B→C）会让「改一处影响两条」静默
    # 发生，排障时无从判断最终落到哪个模型，故明确不支持。
    real_entries = list(entries)
    existing_ids = {entry["id"].lower() for entry in real_entries}
    for alias in custom.values():
        if "@" in alias.name:
            logger.error("自定义别名 %r 无效：对外名不能含 `@`（那是 @渠道 语法）",
                         alias.name)
            continue
        key = alias.name.lower()
        if key in existing_ids:
            logger.error("自定义别名 %r 与真实模型 id 撞车，已跳过", alias.name)
            continue
        if any(key in provider_aliases for provider_aliases in aliases.values()):
            logger.error("自定义别名 %r 与已有自动别名撞车，已跳过", alias.name)
            continue
        target = _find_target_entry(real_entries, alias.model)
        if target is None:
            logger.error("自定义别名 %r 的目标模型 %r 不存在，已跳过",
                         alias.name, alias.model)
            continue
        if alias.provider is not None and alias.provider not in target["providers"]:
            logger.error("自定义别名 %r 的目标模型 %r 不在渠道 %s 上，已跳过",
                         alias.name, alias.model, alias.provider)
            continue
        entry = _alias_entry(alias, target)
        entries.append(entry)
        existing_ids.add(key)
        for provider_id, raw_id in entry["raw_ids"].items():
            aliases.setdefault(provider_id, {})[key] = raw_id
    # 别名条目按渠道优先级归位，与真实条目同一展示口径（追加到末尾会让它
    # 孤零零落在列表最后，用户很难判断它到底挂在哪条渠道上）。
    entries.sort(key=_sort_key)


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


def _build_aliases(entries: list[dict[str, Any]],
                   custom: dict[str, ModelAlias] | None = None
                   ) -> dict[str, dict[str, str]]:
    """定稿全部分组并产出别名表（含用户自定义别名）。

    `custom` 是 `MODEL_ALIASES` 的解析结果（`model_alias.parse_model_aliases`）：
    在真实条目定稿、自动别名登记完之后注入，撞车判据才齐（见
    `_apply_custom_aliases`）。None / 空 = 与加别名功能前的行为完全一致。
    """
    for entry in entries:
        _finalize(entry)
    _disambiguate(entries)
    aliases: dict[str, dict[str, str]] = {}
    _register_aliases(entries, aliases)
    _apply_custom_aliases(entries, custom or {}, aliases)
    return aliases


def _cost_of(prices: PriceTable | None, entry: dict[str, Any]
             ) -> dict[str, float] | None:
    """条目 → `cost`（USD / 百万 token）；查不到价返回 None（不带该字段）。

    匹配口径与成本估算、能力分共用 `model_match.lookup`（对外 id + 展示名两路
    等值、唯一命中才采用），故「列表里显示的价格」与「统计里估算成本用的价」
    是同一份数据。我们只有 `input` / `output` / `cache_read` 三项——上游的
    `cache_write` 本服务不采集，**不编**。
    """
    price = lookup(prices or {}, entry["id"], entry["meta"].get("name"))
    if not isinstance(price, (list, tuple)) or len(price) != 3:
        return None
    return {"input": price[0], "output": price[1], "cache_read": price[2]}


def _reasoning_efforts(entry: dict[str, Any],
                       effort_map: dict[str, dict[str, str]] | None
                       ) -> list[str] | None:
    """条目 → 该渠道生效中的目标档位集合（去重、稳定排序）；未配置返回 None。

    取 `REASONING_EFFORT_MAP` 在**该条目各渠道**的目标值（渠道项优先、`*` 兜底，
    与 `engine.reasoning.map_effort` 同一解析），丢掉 `none`——那个目标表示
    「删除该键、走渠道默认」，不是一个可选档位。多渠道条目取并集：用户选这条
    id 时可能落到任一渠道，客户端需要知道所有可能接受的档位。
    """
    if not effort_map:
        return None
    values: set[str] = set()
    for provider_id in sorted(entry["providers"]):
        targets = effort_map.get(provider_id) or effort_map.get(WILDCARD_CHANNEL) or {}
        values.update(target for target in targets.values()
                      if target != DELETE_EFFORT)
    return sorted(values) or None


def _entry_response(entry: dict[str, Any],
                    benchmarks: BenchmarkTable | None = None,
                    prices: PriceTable | None = None,
                    efforts: dict[str, dict[str, str]] | None = None
                    ) -> dict[str, Any]:
    """合并后的 grouped 条目 → OpenAI 兼容响应条目。

    多渠道模型额外给 `by_provider.{pid}`：各渠道倍率不同时前端按渠道分别
    展示；`raw_id` 一并透出，让用户能直接看到「选这个模型实际会发什么
    key」（kilo 的 `kilo-auto/free`、Qoder 的 `kmodel_latest`），排障不必
    再翻服务端日志。

    `benchmarks` 非空时按对外 id 与展示名两路查能力分（唯一命中才带），
    查不到就不带该字段——调用方不传（默认 None）时行为与加该字段前完全一致。
    `prices` / `efforts` 同理：分别为刊例价（`cost`，USD / 百万 token）与生效
    中的档位映射（`reasoning_efforts`）。缺价 / 未配置**不带字段**，绝不填 0
    或空数组——那会被读成「免费」「没有档位」。

    用户自定义别名条目额外带 `custom_alias: true`（前端据此打「自定义别名」
    徽章）与 `alias_target`（别名指向的真实对外 id，排障用）。
    """
    result: dict[str, Any] = {
        "id": entry["id"], "object": "model", "owned_by": "Coding2API",
        "providers": sorted(entry["providers"]),
        # 空串按「无该字段」处理：`name`/`default_effort` 未提供时是 ""，
        # 透传空串会让前端显示空模型名或多余的 `default_effort: ""`。
        **{field: value for field, value in entry["meta"].items()
           if value is not None and value != ""},
    }
    if entry.get("custom_alias"):
        result["custom_alias"] = True
        result["alias_target"] = entry.get("alias_target", "")
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
    cost = _cost_of(prices, entry)
    if cost is not None:
        result["cost"] = cost
    reasoning_efforts = _reasoning_efforts(entry, efforts)
    if reasoning_efforts is not None:
        result["reasoning_efforts"] = reasoning_efforts
    return result


def _visible(models_by_lower: dict[str, Model],
             patterns: tuple[str, ...]) -> dict[str, Model]:
    """按当前黑名单过滤一个上游的模型表（缓存里存的是未过滤的原始表）。"""
    return {lower: model for lower, model in models_by_lower.items()
            if not _blocked(model, patterns)}
