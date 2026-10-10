"""上下文压缩接线（P0-2，Q60 修订：按实际服务渠道取窗口）。

`src/engine/compress.py` 是纯函数；本模块构造「按渠道查输入上限 → 压缩」的
闭包，注入执行引擎（`ExecutorDeps.context_compress`）。压缩点在**选号之后、
发上游之前**：同名模型挂多渠道时各自上限不同（qoder 180K vs codebuddy 1M），
路由前取最小值会让大窗口渠道的会话被小窗口渠道的阈值反复裁剪——估算器口径
偏高时每轮裁剪结果不稳定，上游前缀缓存只命中 system 段（实测命中率从 96%+
跌到 7-16%）。按实际落到的渠道取窗口后，未超限的会话零裁剪，前缀单调增长、
缓存可正常命中。

目录里查不到输入上限时**不压缩**：宁可不裁剪，也不拿猜出来的数字去砍用户
上下文；因此本功能对未知模型是零副作用的。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..engine.compress import compress_messages


def context_window_for(cache: dict[str, dict[str, Any]],
                       aliases: dict[str, dict[str, str]],
                       provider_id: str, model: str) -> int | None:
    """单渠道的模型输入上限：按归一名或别名原始 id 在该渠道目录取值。

    `model` 可带 `@provider` 后缀（防御：executor 传入的已是去掉后缀的
    归一名，这里再剥一层不伤直调方）。查不到 / 值非法（≤0、非 int、bool）
    视为未知，返回 None。
    """
    name = (model or "").strip()
    if "@" in name:
        name = name.rsplit("@", 1)[0].strip()
    key = name.lower()
    if not key:
        return None
    table = cache.get(provider_id) or {}
    candidates = {key}
    raw = (aliases.get(provider_id) or {}).get(key)
    if raw:
        candidates.add(raw.lower())
    for candidate in candidates:
        entry = table.get(candidate)
        value = getattr(entry, "max_input_tokens", None)
        # bool 是 int 子类，显式排除；<=0 视为未上报
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def build_context_compressor(
        cache: dict[str, dict[str, Any]],
        aliases: dict[str, dict[str, str]],
        settings: Any) -> Callable[[str, str, dict], dict]:
    """构造 `(provider_id, model, payload) → payload` 的压缩闭包。

    `settings` 传运行态覆盖层（`RuntimeSettings`）：开关与四个参数每轮
    请求现读，管理台热更即生效（B3.2）。装配侧（main.py）把它注入
    `ExecutorDeps.context_compress`；executor 在选号后按实际渠道调用。
    """
    def compress(provider_id: str, model: str, payload: dict) -> dict:
        if not settings.context_compress_enabled:
            return payload
        window = context_window_for(cache, aliases, provider_id, model)
        if window is None:
            return payload
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return payload
        compressed = compress_messages(
            messages,
            max_input_tokens=window,
            reserve_for_output=int(settings.context_compress_reserve_tokens),
            min_keep_messages=int(settings.context_compress_min_keep_messages),
            safety_ratio=float(settings.context_compress_safety_ratio),
        )
        if compressed is messages:
            return payload
        # 不原地改 payload：同一请求可能在轮换/回退里再压一次（另一个渠道、
        # 另一个窗口），必须始终从原文出发，且 affinity 指纹读的是原文
        return {**payload, "messages": compressed}
    return compress
