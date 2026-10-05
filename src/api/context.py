"""请求入口的上下文压缩接线（P0-2）。

`src/engine/compress.py` 是纯函数；本模块负责把「模型目录里的输入上限」接到
它上面，并在三个 /v1 出口（chat / responses / anthropic）与 Playground 的
入站处调用。放在 api/ 层是因为只有这里能同时拿到 `Services`（模型目录缓存 +
别名表）和 `ChatRequest`。

目录里查不到输入上限时**不压缩**：宁可不裁剪，也不拿一个猜出来的数字去砍
用户上下文。因此本功能对未知模型是零副作用的。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..compat.openai.request import ChatRequest
from ..engine.compress import compress_messages

if TYPE_CHECKING:
    from .deps import Services


def context_window_for(services: Services, model: str) -> int | None:
    """按模型名从模型目录取输入上限；查不到返回 None。

    同名模型可能挂在多个渠道（各自上限不同），取**最小值**：调度可能落到
    任何一个候选渠道，用最小上限裁剪才不会在最小的那个上撞 400。
    `model` 可带 `@provider` 后缀，后缀不参与匹配。
    """
    name = (model or "").strip()
    if "@" in name:
        name = name.rsplit("@", 1)[0].strip()
    key = name.lower()
    if not key:
        return None
    windows: list[int] = []
    for provider_id, table in services.model_list_cache.items():
        raw = services.model_aliases.get(provider_id, {}).get(key)
        candidates = {key}
        if raw:
            candidates.add(raw.lower())
        for candidate in candidates:
            entry = table.get(candidate)
            value = getattr(entry, "max_input_tokens", None)
            # bool 是 int 子类，显式排除；<=0 视为未上报
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                windows.append(value)
                break
    return min(windows) if windows else None


def apply_context_compression(services: Services, chat_request: ChatRequest) -> None:
    """按热更配置与模型上限就地压缩 `chat_request` 的消息列表。

    未启用、未知上限、未超限时都不改动（`compress_messages` 原样返回同一
    列表对象，据此判断是否发生压缩）。
    """
    settings = services.settings
    if not settings.context_compress_enabled:
        return
    window = context_window_for(services, chat_request.model)
    if window is None:
        return
    messages = chat_request.raw.get("messages")
    if not isinstance(messages, list):
        return
    compressed = compress_messages(
        messages,
        max_input_tokens=window,
        reserve_for_output=int(settings.context_compress_reserve_tokens),
        min_keep_messages=int(settings.context_compress_min_keep_messages),
        safety_ratio=float(settings.context_compress_safety_ratio),
    )
    if compressed is messages:
        return
    chat_request.raw["messages"] = compressed
    chat_request.messages = compressed