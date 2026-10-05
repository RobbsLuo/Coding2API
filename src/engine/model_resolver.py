"""模型名解析（Q21=C 扁平 + Q27=A @provider 后缀）。"""

from __future__ import annotations

from dataclasses import dataclass

KNOWN_PROVIDERS = ("codebuddy", "trae", "zen", "kilo", "qoder", "codearts")


class UnknownModelError(ValueError):
    pass


def parse_fallback_groups(raw: str) -> dict[str, tuple[str, ...]]:
    """兼容组配置文本 → `{组名: (成员, …)}`。

    格式（`MODEL_FALLBACK_GROUPS`）：`组名=成员1,成员2;组名2=成员3`。
    分隔：`;` 分组、`=` 分「组名 / 成员列表」、`,` 分成员。留空 = 无组。

    宽容解析：忽略空段、缺 `=`、空组名 / 空成员的段（一行坏配置不能让整个
    网关起不来，也不能因一个笔误把别的组合法组丢掉）。重复组名后者覆盖前者。
    组名与成员都去首尾空白；成员按出现顺序保留（顺序即回退顺序）。
    """
    groups: dict[str, tuple[str, ...]] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, members_raw = chunk.partition("=")
        name = name.strip()
        members = tuple(m.strip() for m in members_raw.split(",") if m.strip())
        if not name or not members:
            continue
        groups[name] = members
    return groups


def ordered_fallback_chain(model: str, groups: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """请求模型 → 回退链（含请求模型本身，首项即它）；不在任何组则空元组。

    组名只是**入口别名**，不是可路由的模型：`fast=glm-4.6,glm-5` 表示
    「请求 `fast`，或请求成员 `glm-4.6` / `glm-5`，都在这个兼容组里」。

    * 请求组名 `fast`      → `(glm-4.6, glm-5)`（按配置顺序）
    * 请求成员 `glm-5`     → `(glm-5, glm-4.6)`（请求模型置首，其余保持顺序）

    组名不进链，避免把别名当模型发上游；成员按配置顺序即回退顺序。大小写不
    敏感匹配；返回里保留请求模型的原始大小写。只做映射、不校验成员是否存在
    于模型目录（那属执行层，见 `Executor._fallback_chain`）。
    """
    lower = model.lower()
    for name, members in groups.items():
        if lower == name.lower():
            return tuple(members)
        if any(lower == member.lower() for member in members):
            return (model, *(m for m in members if m.lower() != lower))
    return ()


@dataclass(frozen=True, slots=True)
class ModelTarget:
    model: str                        # 归一化后的模型名（去掉 @provider）
    providers: tuple[str, ...]        # 候选 provider 顺序
    forced: bool = False              # 是否由 @provider 强制


def resolve(model: str | None, default_model: str) -> ModelTarget:
    """解析请求模型名。

    "" / "auto" / None → 默认模型，全部 provider 参与
    "glm-5.2"          → 扁平名，全部 provider 参与
    "glm-5.2@trae"     → 强制 trae；未知 provider → UnknownModelError
    """
    raw = (model or "").strip()
    if raw == "" or raw.lower() == "auto":
        raw = default_model
    if "@" in raw:
        name, _, provider = raw.rpartition("@")
        provider = provider.strip().lower()
        if provider not in KNOWN_PROVIDERS:
            raise UnknownModelError(f"unknown provider in model {raw!r}")
        if not name.strip():
            raise UnknownModelError(f"model name missing before @ in {raw!r}")
        return ModelTarget(model=name.strip(), providers=(provider,), forced=True)
    return ModelTarget(model=raw, providers=KNOWN_PROVIDERS, forced=False)
