"""OpenAI Responses 请求 → 内部 ChatRequest 的入站映射（B2.1，Codex CLI 出口）。

字段形状全部取自官方 openai-python 3.x 的类型定义（OpenAPI 生成，见
`openai/types/responses/*.py`），不凭记忆写；本模块只覆盖 Codex CLI 实际
会用到的子集，其余显式 400（`InvalidRequest`），**不静默降级**。

映射要点：
- `instructions`（字符串）→ 开头补一条 `system` 消息（空串忽略）
- `input` 字符串 → 单条 `user`；数组 → 逐项按 `type` 映射
  （`message` / `function_call` / `function_call_output` / `reasoning`；
   其余 item 类型属 Responses 私有语义，无法无损表达 → 400）
- `tools` 的 `{type:"function", name, description, parameters}` → chat
  `{type:"function", function:{...}}`；非 function 工具（web_search 等）→ 400
- `reasoning.effort` → 顶层 `reasoning_effort`（出口侧字段名映射）
- `max_output_tokens` → `max_tokens`（CB 上游只认后者，见 TECHNICAL §3.4）
- `prompt_cache_key` → 透传，供会话粘性（§3.5）与上游使用
- `store=true` / `previous_response_id` / `background=true` / 未知 `include`
  → 400（本服务无状态，不假装支持）
- `include=["reasoning.encrypted_content"]` → **接受并忽略**：Codex CLI 每轮
  必带该值，400 会直接打死主客户端（见 TECHNICAL §3.7 实测表）
"""

from __future__ import annotations

from typing import Any

from ..openai.request import ChatRequest, InvalidRequest

# Responses 里合法但本网关无法无损转成 chat 的 input item 类型。
# 依据 openai/codex（codex-rs/protocol/src/models.rs 的 ResponseItem 与
# codex-rs/app-server-protocol 的 TS 定义）实测：Codex CLI 实际会发
# `reasoning`（带 encrypted_content）与 `compaction` 两种“未服务端往返”
# 项；两者都没有 chat 等价物，只能丢弃——但**必须**显式声明，不能静默。
# 其余项（local_shell_call 等）属 Responses 私有工具族，chat 表达不了。
_UNSUPPORTED_ITEM_TYPES = (
    "local_shell_call", "local_shell_call_output", "tool_search_call",
    "tool_search_output", "custom_tool_call", "custom_tool_call_output",
    "agent_message", "additional_tools", "compaction_trigger",
    "configuration_update", "context_compaction", "other",
)

# 丢弃即无损 / 无副作用：加密推理内容与压缩检查点对 chat 上游没有意义
# （上游不认这个字段），丢掉不影响回答质量，只是不再回传给客户端。
_DROPPED_ITEM_TYPES = ("reasoning", "compaction")


def _text_content(value: Any) -> str:
    """把 Responses 的 content（字符串或 part 数组）压成 chat 的字符串。

    只接受 `input_text` / `text` part；图片/文件 part 会被上游 chat 拒绝，
    转成占位符会静默丢信息 → 直接 400 让调用方知道。
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise InvalidRequest("message content must be a string or an array")
    parts: list[str] = []
    for index, part in enumerate(value):
        if not isinstance(part, dict):
            raise InvalidRequest(f"content[{index}] must be an object")
        kind = part.get("type")
        if kind in ("input_text", "text", "output_text"):
            text = part.get("text")
            if not isinstance(text, str):
                raise InvalidRequest(f"content[{index}].text must be a string")
            parts.append(text)
        elif kind in ("input_image", "input_file", "image_url", "refusal"):
            raise InvalidRequest(
                f"content part type {kind!r} is not supported by this gateway")
        else:
            raise InvalidRequest(f"unknown content part type {kind!r}")
    return "".join(parts)


def _arguments_to_json(value: Any) -> str:
    """chat 的 tool_calls.function.arguments 是 JSON 字符串。

    历史里的 function_call 只有 `arguments` 字符串（有就原样用），
    没有则回落成空对象字面量——上游要求该键存在。
    """
    if isinstance(value, str) and value:
        return value
    if value is None:
        return ""
    raise InvalidRequest("function_call.arguments must be a string")


def _message_item(item: dict[str, Any], role: str) -> dict[str, Any]:
    content = _text_content(item.get("content"))
    return {"role": role, "content": content}


def _function_call_item(item: dict[str, Any]) -> dict[str, Any]:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise InvalidRequest("function_call.name must be a non-empty string")
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": item.get("call_id") or item.get("id") or "call",
        "type": "function",
        "function": {"name": name,
                     "arguments": _arguments_to_json(item.get("arguments"))},
    }]}


def _function_call_output_item(item: dict[str, Any]) -> dict[str, Any]:
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise InvalidRequest("function_call_output.call_id must be a non-empty string")
    text = _function_call_output_text(item.get("output"))
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def _function_call_output_text(output: Any) -> str:
    """function_call_output.output 的四种线上形状：纯字符串、part 数组、
    `{content}`（Codex 的 `FunctionCallOutputPayload`）与 `{content_items}`。"""
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if isinstance(output, list):
        return _text_content(output)
    if isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, str):
            return content
        items = output.get("content_items")
        if items is None:
            return ""
        return _text_content(items)
    raise InvalidRequest("function_call_output.output must be a string, object or array")


def _reasoning_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """历史 reasoning item → assistant 消息上的 reasoning_content。

    Codex 会把 `encrypted_content`（及可选的 summary/content）回传。上游
    不认加密串，只透传明文部分；两者都为空时返回 None（不产生空消息）。
    """
    parts: list[str] = []
    for field in ("summary", "content"):
        blocks = item.get(field)
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    text = "".join(parts)
    if not text:
        return None
    return {"role": "assistant", "content": "", "reasoning_content": text}


def _map_input_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        raise InvalidRequest("input items must be objects")
    kind = item.get("type", "message")
    if kind in _UNSUPPORTED_ITEM_TYPES:
        raise InvalidRequest(f"input item type {kind!r} is not supported by this gateway")
    if kind in _DROPPED_ITEM_TYPES:
        # Codex CLI 持续回传 reasoning/compaction；chat 上游没有对应表达。
        # 这是有意的无损丢弃（见模块 docstring），不是静默降级。
        return _reasoning_item(item) if kind == "reasoning" else None
    if kind == "message":
        role = item.get("role")
        if role == "developer":
            role = "system"       # 腾讯后端不认 developer（11128，见 TECHNICAL §3.2）
        if role not in ("user", "assistant", "system"):
            raise InvalidRequest(f"message role {role!r} is not supported")
        return _message_item(item, role)
    if kind == "function_call":
        return _function_call_item(item)
    if kind == "function_call_output":
        return _function_call_output_item(item)
    raise InvalidRequest(f"unknown input item type {kind!r}")


def _map_tools(tools: Any) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise InvalidRequest(f"tools[{index}] must be an object")
        if tool.get("type") != "function":
            raise InvalidRequest(
                f"tool type {tool.get('type')!r} is not supported by this gateway")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidRequest(f"tools[{index}].name must be a non-empty string")
        function: dict[str, Any] = {"name": name}
        if isinstance(tool.get("description"), str):
            function["description"] = tool["description"]
        if isinstance(tool.get("parameters"), dict):
            function["parameters"] = tool["parameters"]
        mapped.append({"type": "function", "function": function})
    return mapped


def _map_tool_choice(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("type") == "function":
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidRequest("tool_choice.name must be a non-empty string")
        return {"type": "function", "function": {"name": name}}
    raise InvalidRequest("tool_choice form is not supported by this gateway")


def parse_responses_request(body: Any) -> ChatRequest:
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be a JSON object")

    if body.get("store") is True:
        raise InvalidRequest("store=true is not supported: this gateway is stateless")
    if "previous_response_id" in body and body["previous_response_id"] is not None:
        raise InvalidRequest(
            "previous_response_id is not supported: send the full input each turn")
    if body.get("background") is True:
        raise InvalidRequest("background=true is not supported by this gateway")
    include = body.get("include")
    if include:
        # Codex CLI 每轮都带 include=["reasoning.encrypted_content"]（实测
        # codex-rs/core/src/client.rs）。本网关不透传加密推理内容，但这项
        # 是“要求服务端额外返回”，不做也不算降级：静默忽略即可。
        if not isinstance(include, list):
            raise InvalidRequest("include must be an array")
        unknown = [item for item in include if item != "reasoning.encrypted_content"]
        if unknown:
            raise InvalidRequest(
                f"include {unknown!r} is not supported by this gateway")

    model = body.get("model")
    if model is not None and not isinstance(model, str):
        raise InvalidRequest("model must be a string")

    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if instructions is not None:
        system = _text_content(instructions)
        if system:
            messages.append({"role": "system", "content": system})

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            mapped = _map_input_item(item)
            if mapped is not None:
                messages.append(mapped)
    else:
        raise InvalidRequest("input must be a string or an array")

    if not messages:
        raise InvalidRequest("input must contain at least one message")

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise InvalidRequest("stream must be a boolean")

    # 上游载荷：只有 chat 认识的键才透传，Responses 私有键一律丢弃
    upstream: dict[str, Any] = {"model": model or "", "messages": messages,
                                "stream": stream}
    tools = body.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise InvalidRequest("tools must be an array")
        mapped = _map_tools(tools)
        if mapped:
            upstream["tools"] = mapped
    for key in ("temperature", "top_p"):
        if key in body:
            upstream[key] = body[key]
    if "tool_choice" in body:
        upstream["tool_choice"] = _map_tool_choice(body["tool_choice"])
    if isinstance(body.get("parallel_tool_calls"), bool):
        upstream["parallel_tool_calls"] = body["parallel_tool_calls"]
    if isinstance(body.get("max_output_tokens"), int):
        # CB 上游只认 max_tokens（max_completion_tokens 被忽略，TECHNICAL §3.4）
        upstream["max_tokens"] = body["max_output_tokens"]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        upstream["reasoning_effort"] = reasoning["effort"]
    cache_key = body.get("prompt_cache_key")
    if isinstance(cache_key, str) and cache_key:
        # 与 chat 出站同理：显式会话标识能固定凭证（B1.5），上游也认该字段
        upstream["prompt_cache_key"] = cache_key
    text_config = body.get("text")
    if isinstance(text_config, dict) and isinstance(text_config.get("verbosity"), str):
        upstream["verbosity"] = text_config["verbosity"]

    # ChatRequest.raw 供 executor 使用；Responses 元数据在出口侧重新生成
    return ChatRequest(model=model or "", messages=messages, stream=stream,
                       raw=upstream)
