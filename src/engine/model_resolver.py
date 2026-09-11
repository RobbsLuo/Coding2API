"""模型名解析（Q21=C 扁平 + Q27=A @provider 后缀）。"""

from __future__ import annotations

from dataclasses import dataclass

KNOWN_PROVIDERS = ("codebuddy", "trae")


class UnknownModelError(ValueError):
    pass


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
