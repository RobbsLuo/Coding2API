"""Anthropic Messages 请求 → 内部 ChatRequest 的入站映射。

形状取自 Anthropic Messages API（anthropic-python 官方 SDK 类型定义），只覆盖
Claude Code / Anthropic SDK 会用的子集，其余显式 400（`InvalidRequest`），
**不静默降级**。本服务只实现 chat 子集，与 `/v1/chat/completions` 共用同一
执行引擎（选号 / 冷却 / 轮换 / 统计 / 会话粘性全部不动）。

映射要点：
- `system`（字符串或 text 块数组）→ 开头补一条 `system` 消息（空串忽略）
- `messages`：
  - `user` 的 text 块 → user 消息；`tool_result` 块 → `role:"tool"` 消息
    （同一条 user 里 text 与 tool_result 混排时按出现顺序拆分）
  - `assistant` 的 text 块 → assistant 消息正文；`tool_use` 块 → chat
    `tool_calls`（`input` 对象序列化成 `arguments` JSON 串）；`thinking`
    块 → assistant 的 `reasoning_content`
  - `image` 块 → chat 的 `image_url` 内容块（`source.type=base64` 拼成
    data URL，`source.type=url` 直传）；`document` 块仍不支持 → 400
  - 纯文本内容折成字符串（与旧行为逐字节一致），含图片时保留内容块数组
- `tools` 的 `{name, description, input_schema}` → chat
  `{type:"function", function:{name, description, parameters}}`
- `tool_choice`：`auto`→`auto`、`any`→`required`、`tool`→指定函数
- `max_tokens` → chat `max_tokens`（可选；count_tokens 端点不带）
- `stop_sequences` → `stop`；`top_k` 无 chat 等价物，丢弃
"""

from __future__ import annotations

import json
from typing import Any

from ...engine.reasoning import output_config_effort, thinking_effort
from ..openai.request import (
    MAX_OUTPUT_TOKENS,
    MAX_TEMPERATURE,
    ChatRequest,
    InvalidRequest,
    require_finite_number,
)


def _cache_control(block: dict[str, Any], where: str) -> dict[str, Any] | None:
    """取 Anthropic 内容块上的 `cache_control` 标记（缺省 None）。

    只做**结构**校验，取值原样透传：Anthropic 目前只有 `{"type": "ephemeral",
    "ttl"?: "5m"|"1h"}`，但它属于「缓存提示」而非本网关要解释的语义——本网关是
    管道，未知取值交给上游裁决（显式 400 会把上游将来新增的取值挡在门外）。
    结构不合法（非对象 / 无 type / ttl 非字符串）仍显式 400：静默丢弃会把
    客户端的意图悄悄吃掉，与本模块「不静默降级」一致。

    透传落点（2026-10-17 实测七渠道全部接受，见 `scripts/probe_cache_control.py`
    与 `docs/competitor-comparison.md` §3.1）：system/user/assistant/tool 消息的
    内容块、`tool_calls` 元素顶层、`tools` 元素顶层。
    """
    marker = block.get("cache_control")
    if marker is None:
        return None
    if not isinstance(marker, dict):
        raise InvalidRequest(f"{where}.cache_control must be an object")
    if not isinstance(marker.get("type"), str) or not marker["type"]:
        raise InvalidRequest(f"{where}.cache_control.type must be a non-empty string")
    ttl = marker.get("ttl")
    if ttl is not None and not isinstance(ttl, str):
        raise InvalidRequest(f"{where}.cache_control.ttl must be a string")
    return marker


def _system_content(system: Any) -> str | list[dict[str, Any]]:
    """`system` 字符串或 text 块数组 → system 消息的 content。

    不带 `cache_control` 时折成一个字符串（**与旧行为逐字节一致**：会话粘性
    指纹与上游前缀缓存都依赖这个形状）。带标记时保留内容块数组，把标记挂在
    对应块上——折成字符串会丢掉「缓存在这里断点」的信息。
    """
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[dict[str, Any]] = []
        for index, block in enumerate(system):
            if not isinstance(block, dict):
                raise InvalidRequest(f"system[{index}] must be an object")
            kind = block.get("type")
            if kind != "text":
                raise InvalidRequest(
                    f"system block type {kind!r} is not supported by this gateway")
            text = block.get("text")
            if not isinstance(text, str):
                raise InvalidRequest(f"system[{index}].text must be a string")
            part: dict[str, Any] = {"type": "text", "text": text}
            marker = _cache_control(block, f"system[{index}]")
            if marker is not None:
                part["cache_control"] = marker
            parts.append(part)
        if any("cache_control" in part for part in parts):
            return parts
        return "".join(part["text"] for part in parts)
    if system is None:
        return ""
    raise InvalidRequest("system must be a string or an array")


def _block_text(block: dict[str, Any], where: str) -> str:
    text = block.get("text")
    if not isinstance(text, str):
        raise InvalidRequest(f"{where}.text must be a string")
    return text


def _image_part(block: dict[str, Any], where: str) -> dict[str, Any]:
    """Anthropic `image` 块 → chat 的 `image_url` 内容块。

    只做结构映射，不下载 / 不压缩 / 不改写图片字节（上游已实测接受 data URL
    与 http(s) URL，见扫描报告 §4.15 实测表）。
    """
    source = block.get("source")
    if not isinstance(source, dict):
        raise InvalidRequest(f"{where}.source must be an object")
    kind = source.get("type")
    if kind == "base64":
        media_type = source.get("media_type")
        data = source.get("data")
        if not isinstance(media_type, str) or not media_type:
            raise InvalidRequest(f"{where}.source.media_type must be a non-empty string")
        if not isinstance(data, str) or not data:
            raise InvalidRequest(f"{where}.source.data must be a non-empty string")
        part: dict[str, Any] = {"type": "image_url",
                                "image_url": {"url": f"data:{media_type};base64,{data}"}}
    elif kind == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise InvalidRequest(f"{where}.source.url must be a non-empty string")
        part = {"type": "image_url", "image_url": {"url": url}}
    else:
        raise InvalidRequest(
            f"{where}.source.type {kind!r} is not supported by this gateway")
    marker = _cache_control(block, where)
    if marker is not None:
        part["cache_control"] = marker
    return part


def _text_part(block: dict[str, Any], where: str) -> dict[str, Any]:
    part: dict[str, Any] = {"type": "text", "text": _block_text(block, where)}
    marker = _cache_control(block, where)
    if marker is not None:
        part["cache_control"] = marker
    return part


def _merge_parts(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """内容块数组 → chat content：纯文本折成字符串，含图片/缓存标记时保留数组。

    纯文本折成字符串是为了与旧行为**逐字节一致**——历史请求（不含图片、不带
    `cache_control`）经此出口映射后必须与改动前完全相同，否则会话粘性指纹与
    上游前缀缓存都会失效。带缓存标记时不能折：标记挂在块上，折成字符串就丢了。
    """
    if all(part.get("type") == "text" for part in parts) and not any(
            "cache_control" in part for part in parts):
        return "".join(part["text"] for part in parts)
    # 必须返回副本：调用方紧接着会 clear() 自己的 pending 列表，直接返回同一
    # 对象会把已存入消息的 content 一并清空（数组 part 场景下表现为 content=[]）。
    return list(parts)


def _placeholder_text(kind: str, block: dict[str, Any]) -> str:
    """本网关无法转成 chat 表达的内容块 → 一段中性占位文本。

    Claude Code 2.1.31x 起会在会话里回灌两类块：

    * `tool_reference`（服务端 tool_search 命中的工具名，`{type, tool_name}`）：
      它只是「这个工具可用」的提示，chat 没有对应语义，丢掉也不影响模型已
      看到的工具表。
    * `document`（PDF / 纯文本附件）：chat 只有 `image_url`，转不了文档。

    两者此前都显式 400，而客户端遇到 400 会**整轮重发一次**并永久关掉该特性
    （`tool_reference` 实测 28/30 次 400 都出自它）。改成占位文本后模型至少
    知道「这里原本有个附件/工具引用」，比白打一轮往返好。
    """
    if kind == "tool_reference":
        name = block.get("tool_name")
        if isinstance(name, str) and name:
            return f"[tool available: {name}]"
        return "[tool available]"
    title = block.get("title")
    label = title if isinstance(title, str) and title else kind
    return f"[{label} omitted: this gateway cannot forward {kind} content]"


def _tool_result_text(content: Any) -> str | list[dict[str, Any]]:
    """`tool_result.content`：字符串或 text/image 块数组。

    `is_error` 是纯客户端语义，chat 上游没有对应字段，按普通文本回灌。
    纯文本结果仍返回字符串（旧行为不变）；含图片时返回内容块数组——chat 的
    `tool` 消息带 `image_url` 数组已实测被上游接受（2026-10-15）。
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                raise InvalidRequest(f"tool_result.content[{index}] must be an object")
            where = f"tool_result.content[{index}]"
            kind = block.get("type")
            if kind == "text":
                parts.append(_text_part(block, where))
            elif kind == "image":
                parts.append(_image_part(block, where))
            elif kind in ("document", "tool_reference"):
                parts.append({"type": "text",
                              "text": _placeholder_text(kind, block)})
            else:
                raise InvalidRequest(
                    f"unknown tool_result.content type {kind!r}")
        return _merge_parts(parts)
    raise InvalidRequest("tool_result.content must be a string or an array")


def _input_arguments(value: Any) -> str:
    """`tool_use.input`（对象）→ chat `arguments` JSON 字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        # 少数客户端把已序列化的字符串塞进来：原样透传
        return value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    raise InvalidRequest("tool_use.input must be an object")


def _user_messages(blocks: list[Any]) -> list[dict[str, Any]]:
    """user 内容块 → 按顺序拆成 user / tool 消息。

    同一条 user 里 text 与图片相邻时合并进同一个 user 消息（保持图文顺序）；
    遇到 `tool_result` 才 flush——它必须另起一条 `tool` 消息。
    """
    messages: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        # 非空即产出：pending 里可能是纯图片（`_merge_parts` 返回数组，非空）
        if pending:
            messages.append({"role": "user", "content": _merge_parts(pending)})
            pending.clear()

    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise InvalidRequest(f"messages.content[{index}] must be an object")
        where = f"messages.content[{index}]"
        kind = block.get("type")
        if kind == "text":
            pending.append(_text_part(block, where))
        elif kind == "image":
            pending.append(_image_part(block, where))
        elif kind == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                raise InvalidRequest(
                    "tool_result.tool_use_id must be a non-empty string")
            flush()
            tool_message: dict[str, Any] = {
                "role": "tool", "tool_call_id": tool_use_id,
                "content": _tool_result_text(block.get("content")),
            }
            marker = _cache_control(block, where)
            if marker is not None:
                # 挂在消息顶层而不是内容块里：字符串 content 塞不下标记，包成
                # 数组又要多造一个空 text 块（会进模型上下文）。实测七渠道接受
                # 消息顶层标记（scripts/probe_cache_control.py）。
                tool_message["cache_control"] = marker
            messages.append(tool_message)
        elif kind == "document":
            # user 消息里的 document（附件）：chat 只有 image_url，见
            # `_placeholder_text`。此前显式 400 会打死整轮。
            pending.append({"type": "text", "text": _placeholder_text(kind, block)})
        elif kind == "tool_reference":
            pending.append({"type": "text", "text": _placeholder_text(kind, block)})
        else:
            raise InvalidRequest(f"unknown content block type {kind!r}")
    flush()
    return messages


def _assistant_message(blocks: list[Any]) -> dict[str, Any] | None:
    """assistant 内容块 → 单条 chat assistant 消息（空则 None）。

    正文默认折成字符串（与旧行为逐字节一致）；任一块带 `cache_control` 时保留
    内容块数组，标记挂在对应块上。`tool_use` 的标记挂在 `tool_calls` 元素顶层
    ——chat 的 `function` 对象只认 name/arguments，多塞字段属于伪造 schema。
    """
    content_parts: list[dict[str, Any]] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise InvalidRequest(f"messages.content[{index}] must be an object")
        where = f"messages.content[{index}]"
        kind = block.get("type")
        if kind == "text":
            content_parts.append(_text_part(block, where))
        elif kind == "thinking":
            thinking = block.get("thinking")
            if not isinstance(thinking, str):
                raise InvalidRequest("thinking.thinking must be a string")
            reasoning_parts.append(thinking)
        elif kind == "redacted_thinking":
            # 加密思考内容对 chat 上游无意义，丢弃（无损：上游不认）
            continue
        elif kind == "tool_use":
            name = block.get("name")
            if not isinstance(name, str) or not name:
                raise InvalidRequest("tool_use.name must be a non-empty string")
            tool_call: dict[str, Any] = {
                "id": block.get("id") or "call",
                "type": "function",
                "function": {"name": name,
                             "arguments": _input_arguments(block.get("input"))},
            }
            marker = _cache_control(block, where)
            if marker is not None:
                tool_call["cache_control"] = marker
            tool_calls.append(tool_call)
        elif kind == "tool_reference":
            # 服务端 tool_search 的「该工具可用」提示：chat 无对应语义，
            # assistant 侧直接丢（不占上下文，也不影响下一轮调用）
            continue
        else:
            raise InvalidRequest(f"unknown content block type {kind!r}")
    if not content_parts and not reasoning_parts and not tool_calls:
        return None
    marked = any("cache_control" in part for part in content_parts)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content_parts if marked else "".join(
            str(part["text"]) for part in content_parts),
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _map_messages(raw_messages: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index, item in enumerate(raw_messages):
        if not isinstance(item, dict):
            raise InvalidRequest(f"messages[{index}] must be an object")
        role = item.get("role")
        content = item.get("content")
        if role == "system":
            # Claude Code 2.1.318 起在新会话首轮发 mid-conversation system
            # 消息（`mid-conversation-system-2026-04-07` 特性）。chat 只在
            # 开头认 system，但**整轮 400 会让客户端白打一次往返**——它收到
            # 400 会去掉该消息重发并永久关掉特性。这里就地转成 system 消息
            # 按原位置插入：上游对此宽容，`role` 顺序本就是客户端的意图。
            messages.append({"role": "system",
                             "content": _system_block_content(content, index)})
            continue
        if isinstance(content, str):
            if role not in ("user", "assistant"):
                raise InvalidRequest(f"message role {role!r} is not supported")
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise InvalidRequest(
                f"messages[{index}].content must be a string or an array")
        if role == "user":
            messages.extend(_user_messages(content))
        elif role == "assistant":
            mapped = _assistant_message(content)
            if mapped is not None:
                messages.append(mapped)
        else:
            raise InvalidRequest(f"message role {role!r} is not supported")
    return messages


def _system_block_content(content: Any, index: int) -> str:
    """会话中途 system 消息的 content → 纯文本。

    只接受字符串与 text 块数组（与顶层 `system` 同形）；其他块类型属于
    客户端把非文本塞进 system 的畸形输入，显式 400 而不是猜。
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        raise InvalidRequest(
            f"messages[{index}].content must be a string or an array")
    parts: list[str] = []
    for block_index, block in enumerate(content):
        if not isinstance(block, dict):
            raise InvalidRequest(
                f"messages[{index}].content[{block_index}] must be an object")
        kind = block.get("type")
        if kind != "text":
            raise InvalidRequest(
                f"messages[{index}].content[{block_index}] type {kind!r} is "
                f"not supported in a system message")
        text = block.get("text")
        if not isinstance(text, str):
            raise InvalidRequest(
                f"messages[{index}].content[{block_index}].text must be a string")
        parts.append(text)
    return "".join(parts)


def _map_tools(tools: Any) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise InvalidRequest(f"tools[{index}] must be an object")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidRequest(f"tools[{index}].name must be a non-empty string")
        function: dict[str, Any] = {"name": name}
        if isinstance(tool.get("description"), str):
            function["description"] = tool["description"]
        # Anthropic 用 input_schema，chat 用 parameters（同一份 JSON Schema）
        schema = tool.get("input_schema")
        if isinstance(schema, dict):
            function["parameters"] = schema
        entry: dict[str, Any] = {"type": "function", "function": function}
        marker = _cache_control(tool, f"tools[{index}]")
        if marker is not None:
            # 挂在工具对象顶层（`function` 内部只认 name/description/parameters，
            # 塞进去属于伪造 schema）。Claude Code 会把标记打在最后一个工具上，
            # 表示「工具定义到此为止可缓存」。
            entry["cache_control"] = marker
        mapped.append(entry)
    return mapped


def _map_tool_choice(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidRequest("tool_choice must be an object")
    kind = value.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidRequest("tool_choice.name must be a non-empty string")
        return {"type": "function", "function": {"name": name}}
    raise InvalidRequest(f"tool_choice type {kind!r} is not supported")


def parse_messages_request(body: Any, *, thinking_efforts: str = "") -> ChatRequest:
    """Anthropic 请求体 → `ChatRequest`。

    `thinking_efforts` 是「`thinking.budget_tokens` → `reasoning_effort`」的
    热更映射文本（见 `engine/reasoning.py`）；**留空 = 完全不映射**，
    `thinking` 照旧被忽略（现状），不写任何上游字段。
    """
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be a JSON object")
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        raise InvalidRequest("model must be a string")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise InvalidRequest("messages must be a non-empty array")

    messages: list[dict[str, Any]] = []
    system = _system_content(body.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(_map_messages(raw_messages))
    if not messages:
        raise InvalidRequest("messages must contain at least one message")

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise InvalidRequest("stream must be a boolean")

    upstream: dict[str, Any] = {"model": model or "", "messages": messages,
                                "stream": stream}
    max_tokens = body.get("max_tokens")
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
            raise InvalidRequest("max_tokens must be an integer")
        if max_tokens <= 0 or max_tokens > MAX_OUTPUT_TOKENS:
            raise InvalidRequest(
                f"max_tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
        upstream["max_tokens"] = max_tokens
    for key in ("temperature", "top_p"):
        if key in body:
            value = require_finite_number(body[key], key)
            if key == "temperature" and value > MAX_TEMPERATURE:
                raise InvalidRequest(f"temperature must be <= {MAX_TEMPERATURE}")
            upstream[key] = value
    stop_sequences = body.get("stop_sequences")
    if stop_sequences is not None:
        if not isinstance(stop_sequences, list):
            raise InvalidRequest("stop_sequences must be an array")
        if not all(isinstance(item, str) for item in stop_sequences):
            raise InvalidRequest("stop_sequences must be an array of strings")
        upstream["stop"] = stop_sequences
    tools = body.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise InvalidRequest("tools must be an array")
        mapped = _map_tools(tools)
        if mapped:
            upstream["tools"] = mapped
    choice = _map_tool_choice(body.get("tool_choice"))
    if choice is not None:
        upstream["tool_choice"] = choice
    # 档位来源优先级：
    # 1. `output_config.effort`（Claude Code 2.1.318 起 `/effort` 的真实落点，
    #    与 `thinking:{type:"adaptive"}` 同时出现；此前整块被丢弃 → 用户选的
    #    档位静默失效，且不报错）
    # 2. `thinking.budget_tokens` → 阈值表（默认不映射）：Anthropic 只规定
    #    budget_tokens ≥1024、没给「预算 ↔ 档位」对应表，阈值只能由使用者显式
    #    配置；留空时一个字段都不写，`thinking` 与现状一样被忽略
    effort = output_config_effort(body.get("output_config"))
    if effort is None:
        effort = thinking_effort(thinking_efforts, body.get("thinking"))
    if effort is not None:
        upstream["reasoning_effort"] = effort
    return ChatRequest(model=model or "", messages=messages, stream=stream,
                       raw=upstream)
