"""跨源模型匹配：同一套候选键口径，价格目录与能力排行共用。

**为什么需要它**：本项目对外的模型 id 是**归一键**（`kimi-k3`、`glm-5.3`，
见 `provider.naming`），而两个公开目录用的是各自的原名：

* OpenRouter：`tencent/hy3`、`z-ai/glm-5.3-flash`（带厂商命名空间）；
* OpenRouter：`moonshotai/kimi-k3`、展示名 `MoonshotAI: Kimi K3`。

两边与本项目 id 的对应关系只靠一条清洗链路是对不上的（`doubao-seed-2.1-turbo`
vs `bytedance-seed/seed-2.1-turbo`、TRAE 的 `DeepSeek-V4-Pro-Official` 多一个
`-official`）。在 `provider.naming`（免费标记 / 命名空间 / 异名规范表）之上，
本模块补的是**匹配专用**的显式规则：

1. **渠道内部原代号**（`kmodel_latest`、`custom_model_claude`、`dfmodel`）本身
   不是可匹配的身份，但当它携带可读名时，其**清洗后文本**与真身一致
   （`Qwen3.8-Max` → `qwen3.8-max`），故候选键同时收 id 与名称两路。
2. **展示名里的厂商前缀**（`NVIDIA: Nemotron 3 Ultra`）由 `_strip_noise` 削掉，
   与上游 id 尾段同键。
3. **只做等值匹配，不做前缀 / 相似度**：`glm-5.3` 能匹配 `glm-5.3`，绝不匹配
   `glm-5.3-flash`。宁可漏（不配）也不错配（把 A 的价格/分数标到 B 上）。
4. **命中不唯一时不采用**：同一候选键在表里指向多个不同条目（不同厂商的
   `auto`、同名不同源），说明这个键没有判别力，返回 None。

规则全部是**显式登记**的等价关系（`_ALIAS_RULES`）：新增一条要附实测来源，
不做启发式推断——这是定价与分数两种都会被用户当作事实的数据共同的纪律。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from .provider.naming import normalize_model_key

# 显式等价规则：`(匹配前, 匹配后)`。**只在匹配链路上生效**，不参与
# `normalize_model_key`（那里是合并键，改了会影响跨渠道合并与别名表）。
# 每条对应一个实测过的写法差异：
#   `doubao ≡ seed`：字节两个模型族在 TRAE 叫 `Doubao-Seed-2.1-Turbo`，在
#     OpenRouter 叫 `bytedance-seed/seed-2.1-turbo`（2026-10-10 实测）。
#   `维 度 ≡ vision`：CodeArts 的 `Qwen3-VL-235B`，OpenRouter 不带 `-vl` 后缀
#     的 `qwen3-235b` 是不同规格，**不能**并入，故不登记该条（留作反例说明
#     为什么只登记实测过的）。
_ALIAS_RULES: tuple[tuple[str, str], ...] = (
    ("doubao-seed", "seed"),
)

# 需要整段剥掉的尾缀词：上游用它区分「同一模型的另一个分发口径」，不是模型
# 身份的一部分。`DeepSeek-V4-Pro-Official`（TRAE）与
# `deepseek-v4-pro`（CodeBuddy / OpenRouter）是同一个模型，多一个 `official`
# 就匹配不上（2026-10-10 实测）。
_DROP_SUFFIXES: frozenset[str] = frozenset({"official"})

_DROPPABLE_TAIL = re.compile(rf"-(?:{'|'.join(sorted(_DROP_SUFFIXES))})$")

# 版本分隔符备选写法：同一模型在不同源里版本号的点 / 连字符混用——TRAE 的
# `Doubao-Seed-2.1-Turbo` vs OpenRouter 的 `bytedance-seed/seed-2-1-turbo`
# （2026-10-10 实测；`glm-5.2` / `qwen3.8-max` 在两边也有两种写法）。点号在
# `provider.naming` 里**不是**分隔符（`glm-5.2` 压成 `glm-5-2` 就换了模型），
# 故这里只额外生成一枚互换键、不动归一键本身。
#
# 只互换**夹在数字之间**的那一枚分隔符：那是版本号的形状（`2.1`、`5.3`、
# `3.8`），其余位置的 `.`/`-` 是词间分隔（`kimi.k3` 不是 `kimi-k3` 的另一种
# 写法）。两边都生成互换键，建表与查询对称，不必关心哪一侧是点。
#
# 安全性：互换后若两个键在表里都存在且值不同，`lookup` 的歧义判定直接拒配
# （宁可没有分数也不错配）。实测对 OpenRouter 全表 458 个模型做互换，
# 冲突 0 例——上游自身没把 `x-5.2` 与 `x-5-2` 当两个模型登记。
_VERSION_SEP = re.compile(r"(?<=\d)[.-](?=\d)")


def _version_variants(key: str) -> set[str]:
    """版本分隔符互换键（点 ↔ 连字符）；没有版本段时返回空集。"""
    return ({_VERSION_SEP.sub("-", key), _VERSION_SEP.sub(".", key)} - {key})


def match_keys(*texts: str | None) -> set[str]:
    """任意写法（原代号 / 展示名 / 上游 id）→ 可匹配的候选键集合。

    空串与乱码写法返回空串键：调用方据此判断「拿不到干净键」。**从不抛异常**，
    上游给什么怪东西都不该让模型列表挂掉。
    """
    keys: set[str] = set()
    for text in texts:
        if not text:
            continue
        key = normalize_model_key(text)
        if key:
            keys.add(key)
        # 剥尾缀再入一枚：`deepseek-v4-pro-official` 与 `deepseek-v4-pro` 都要
        # 能查。
        for left, right in _ALIAS_RULES:
            if key.startswith(left):
                keys.add(right + key[len(left):])
        if _DROPPABLE_TAIL.search(key):
            keys.add(_DROPPABLE_TAIL.sub("", key))
        # 尾缀词本身可能出现在被剥规则之后（`seed-official`）：递归一次即可，
        # 不需要通用循环（登记过的规则都只叠一层）。
        for candidate in list(keys):
            if _DROPPABLE_TAIL.search(candidate):
                keys.add(_DROPPABLE_TAIL.sub("", candidate))
        # 版本号点 / 连字符互换键：`glm-5.2` 与 `glm-5-2`、`seed-2.1-turbo` 与
        # `seed-2-1-turbo` 是同一个模型在两种源里的写法。加在最后，与归一键并存。
        for candidate in list(keys):
            keys |= _version_variants(candidate)
    keys.discard("")
    return keys


def lookup(table: Mapping[str, object], *texts: str | None) -> object | None:
    """按候选键查表；**命中不唯一或查不到时返回 None**（不猜、不取其一）。

    唯一性判定的是「值是否同一个对象 / 等值」：价表与能力排行表都是
    `{键: 值}` 的单值表，同键多处登记只可能来自上游自身重名，那样这个键就
    没有判别力。
    """
    if not table:
        return None
    matched = [table[key] for key in match_keys(*texts) if key in table]
    if len(matched) < 1:
        return None
    first = matched[0]
    for other in matched[1:]:
        if other != first:
            return None
    return first


def build_table(entries: Iterable[tuple[Iterable[str], object]]) -> dict[str, object]:
    """`(若干写法, 值)` 序列 → 匹配表。

    多个值登记到同一键上时**后者不覆盖前者**（first wins）：同一来源里一个键
    指向两个值说明上游自身重名，保留先到的那条才有确定的结果（测试与线上一致）。
    """
    table: dict[str, object] = {}
    for texts, value in entries:
        for key in match_keys(*texts):
            table.setdefault(key, value)
    return table
