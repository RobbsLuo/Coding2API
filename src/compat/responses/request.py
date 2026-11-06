"""OpenAI Responses 请求 → 内部 ChatRequest 的入站映射（Codex CLI 出口）。

字段形状全部取自官方 openai-python 3.x 的类型定义（OpenAPI 生成，见
`openai/types/responses/*.py`），不凭记忆写；本模块只覆盖 Codex CLI 实际
会用到的子集，其余显式 400（`InvalidRequest`），**不静默降级**。

映射要点：
- `instructions`（字符串）→ 开头补一条 `system` 消息（空串忽略）
- `input` 字符串 → 单条 `user`；数组 → 逐项按 `type` 映射
  （`message` / `function_call` / `function_call_output` / `reasoning`；
   其余 item 类型属 Responses 私有语义，无法无损表达 → 400）
- content part：`input_text`/`text`/`output_text` → chat text；`input_image`
  → chat `image_url`（`image_url` 直传，上游已实测接受 data URL 与 http URL）；
  `input_file` 需服务端文件存储，本网关无此能力 → 400
- `tools` 的 `{type:"function", name, description, parameters}` → chat
  `{type:"function", function:{...}}`；**托管工具与非 function 类型一律跳过**
  （`web_search` / `tool_search` / `namespace` / `custom`，见 `_map_tools`）
- `reasoning.effort` → 顶层 `reasoning_effort`（出口侧字段名映射）
- `text.format` → `response_format`（`text`/`json_object` 直通，`json_schema`
  展开成 chat 的嵌套结构；无法表达的形状显式 400）
- `max_output_tokens` → `max_tokens`（CB 上游只认后者，见 TECHNICAL §3.4）
- `prompt_cache_key` → 透传，供会话粘性（§3.5）与上游使用
- `store=true` / `previous_response_id` / `background=true` / 未知 `include`
  → 400（本服务无状态，不假装支持）
- `include=["reasoning.encrypted_content"]` → **接受并忽略**：Codex CLI 每轮
  必带该值，400 会直接打死主客户端（见 TECHNICAL §3.7 实测表）
"""

from __future__ import annotations

import logging
from typing import Any

from ..openai.request import (
    MAX_OUTPUT_TOKENS,
    MAX_TEMPERATURE,
    ChatRequest,
    InvalidRequest,
    normalize_response_format,
    require_finite_number,
    require_positive_int,
)

logger = logging.getLogger(__name__)

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


def _content_part(part: dict[str, Any], where: str) -> dict[str, Any]:
    """单个 content part → 内部归一形态（text / image_url）。

    图片归一为 chat 的 `image_url` 内容块：`image_url` 直传（上游实测接受
    data URL 与 http(s) URL，见扫描报告 §4.15）。`file_id` 需要服务端文件
    存储，本网关无此能力 → 显式 400 而不是静默丢图。
    """
    kind = part.get("type")
    if kind in ("input_text", "text", "output_text"):
        text = part.get("text")
        if not isinstance(text, str):
            raise InvalidRequest(f"{where}.text must be a string")
        return {"type": "text", "text": text}
    if kind == "input_image":
        url = part.get("image_url")
        if not isinstance(url, str) or not url:
            raise InvalidRequest(f"{where}.image_url must be a non-empty string")
        return {"type": "image_url", "image_url": {"url": url}}
    if kind in ("input_file", "image_url", "refusal"):
        raise InvalidRequest(
            f"content part type {kind!r} is not supported by this gateway")
    raise InvalidRequest(f"unknown content part type {kind!r}")


def _content_value(value: Any) -> str | list[dict[str, Any]]:
    """Responses 的 content（字符串或 part 数组）→ chat content。

    纯文本折成字符串（与旧行为逐字节一致，保住会话粘性与上游前缀缓存）；
    含图片时保留内容块数组。
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise InvalidRequest("message content must be a string or an array")
    parts: list[dict[str, Any]] = []
    for index, part in enumerate(value):
        if not isinstance(part, dict):
            raise InvalidRequest(f"content[{index}] must be an object")
        parts.append(_content_part(part, f"content[{index}]"))
    if all(part["type"] == "text" for part in parts):
        return "".join(part["text"] for part in parts)
    return parts


def _text_content(value: Any) -> str:
    """纯文本 content：只接受 `input_text` / `text` / `output_text`。

    用于 `instructions` 这类只可能是文本的位置——出现图片 part 说明调用方
    理解有误，直接 400 而不是静默丢弃。
    """
    mapped = _content_value(value)
    if not isinstance(mapped, str):
        raise InvalidRequest("image content is not supported here")
    return mapped


def _arguments_to_json(value: Any) -> str:
    """chat 的 tool_calls.function.arguments 是 JSON 字符串。

    历史里的 function_call 只有 `arguments` 字符串（有就原样用）；**缺失与空串
    一视同仁，都回落成空串**（`responses` 流对无参数调用本就发 `""`，
    `compat/responses/response.py` 也用它做占位）。此前实现只认「缺失」，把显式
    `arguments: ""` 直接抛 InvalidRequest：同一语义（无参数）在「省略键」与
    「给空串」两种写法下结果不同，还会把本服务自己发出的历史回传时判为非法，
    带工具调用的对话无法续轮。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    raise InvalidRequest("function_call.arguments must be a string")


def _message_item(item: dict[str, Any], role: str) -> dict[str, Any]:
    content = _content_value(item.get("content"))
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
    text = _function_call_output_value(item.get("output"))
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def _function_call_output_value(output: Any) -> str | list[dict[str, Any]]:
    """function_call_output.output 的四种线上形状：纯字符串、part 数组、
    `{content}`（Codex 的 `FunctionCallOutputPayload`）与 `{content_items}`。

    与 Anthropic 的 `tool_result.content` 一致：纯文本返回字符串（旧行为不变），
    含图片时返回内容块数组（chat 的 `tool` 消息带 `image_url` 已实测被上游接受）。
    """
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if isinstance(output, list):
        return _content_value(output)
    if isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, str):
            return content
        items = output.get("content_items")
        if items is None:
            return ""
        return _content_value(items)
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
    """Responses 工具表 → chat 工具表。

    **只翻译 chat 表达得了的 function**，其余类型跳过并记 debug（不报错）：

    * `function`：直译（历史上唯一被接受的类型）。
    * `namespace`：Codex 0.135+ 对自定义 responses provider **默认下发**
      （Responses-Lite 的 `functions` 组、`multi_agent_v1` 子代理组、MCP 组）。
      **只展平默认命名空间 `functions`**：它等价于无命名空间，成员就是普通
      function，模型调用时客户端按 `functions` 查到同一个工具。
      其余命名空间的成员不加前缀、直接丢弃——chat 出口只能给出扁平工具名，
      而客户端是按 `(namespace, name)` 二元组查登记表的（`registry.rs`
      `ToolName::new(namespace, name)`）：展平成 `{ns}__{name}` 在客户端看来
      是一个「默认命名空间里不存在的新工具」，模型一旦调用就是
      `unsupported call`，比不提供更糟。
    * 其余（`web_search` / `tool_search` / `image_generation` / `file_search` /
      `custom` …）：托管工具由**推理服务端**执行，本网关没有该能力；
      `custom` 是 freeform 工具（`apply_patch` 的 lark 语法），chat 的
      `function.parameters` 表达不了「整段自由文本」还不破坏 Codex 的
      `custom_tool_call` 配对。跳过是降级，但 400 会让**整轮请求失败**——
      Codex 默认配置必带 `web_search`，客户端也不会降级重试（实测 0.135–0.167）。
    """
    mapped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise InvalidRequest(f"tools[{index}] must be an object")
        kind = tool.get("type")
        if kind == "namespace":
            for name, function in _namespace_functions(tool, f"tools[{index}]"):
                if name in seen:
                    continue
                seen.add(name)
                mapped.append({"type": "function", "function": function})
            continue
        if kind != "function":
            logger.debug("responses: 跳过无法翻译的工具类型 %r（tools[%d]）",
                         kind, index)
            continue
        function = _function_tool(tool, f"tools[{index}]")
        if function["name"] in seen:
            continue
        seen.add(function["name"])
        mapped.append({"type": "function", "function": function})
    return mapped


def _function_tool(tool: dict[str, Any], where: str) -> dict[str, Any]:
    """`{type:"function", name, description, parameters}` → chat `function`。"""
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise InvalidRequest(f"{where}.name must be a non-empty string")
    function: dict[str, Any] = {"name": name}
    if isinstance(tool.get("description"), str):
        function["description"] = tool["description"]
    if isinstance(tool.get("parameters"), dict):
        function["parameters"] = tool["parameters"]
    return function


def _namespace_functions(tool: dict[str, Any],
                         where: str) -> list[tuple[str, dict[str, Any]]]:
    """`namespace` 工具组 → `[(chat 函数名, chat function)]`。

    **只展开默认命名空间 `functions`**；其他命名空间返回空表（调用方据此
    记 debug 日志）。原因见 `_map_tools`：chat 只有扁平名字，客户端却按
    `(namespace, name)` 查登记表，展平名会被当成「不存在的工具」。

    组内只认 `function` 成员；`custom`（freeform）成员跳过。
    """
    namespace = tool.get("name")
    if not isinstance(namespace, str) or not namespace:
        raise InvalidRequest(f"{where}.name must be a non-empty string")
    members = tool.get("tools")
    if members is None:
        return []
    if not isinstance(members, list):
        raise InvalidRequest(f"{where}.tools must be an array")
    if namespace != "functions":
        logger.debug("responses: 跳过非默认命名空间 %r（%s 共 %d 个成员）",
                     namespace, where, len(members))
        return []
    entries: list[tuple[str, dict[str, Any]]] = []
    for index, member in enumerate(members):
        member_where = f"{where}.tools[{index}]"
        if not isinstance(member, dict):
            raise InvalidRequest(f"{member_where} must be an object")
        if member.get("type") != "function":
            logger.debug("responses: 跳过命名空间 %r 内的工具类型 %r",
                         namespace, member.get("type"))
            continue
        function = _function_tool(member, member_where)
        entries.append((function["name"], function))
    return entries


def _map_response_format(text_config: dict[str, Any]) -> dict[str, Any] | None:
    """Responses `text.format` → chat `response_format`。

    形状取自官方 openai-python 3.x：Responses 的 json_schema 是
    `{type, name, description?, schema, strict?}` 扁平结构，chat 是
    `{type, json_schema: {name, description?, schema, strict?}}` 嵌套结构。
    只有 verbosity、没有 format 时返回 None（不产生 response_format）。
    无法无损表达的形状显式 400（与模块「不静默降级」一致）。

    校验与归一委托给 `normalize_response_format`（chat 入站用的是同一个）：
    两条入站协议对 json_schema 的判定必须一致，否则同一个 schema 走
    `/v1/chat/completions` 被拒、走 `/v1/responses` 放行，纯属口径分叉。
    """
    format = text_config.get("format")
    if format is None:
        return None
    if not isinstance(format, dict):
        raise InvalidRequest("text.format must be an object")
    kind = format.get("type")
    if kind in ("text", "json_object"):
        return {"type": kind}
    if kind != "json_schema":
        raise InvalidRequest(
            f"text.format.type {kind!r} is not supported by this gateway")
    nested: dict[str, Any] = {"name": format.get("name")}
    if "schema" in format:
        nested["schema"] = format["schema"]
    if isinstance(format.get("description"), str):
        nested["description"] = format["description"]
    if isinstance(format.get("strict"), bool):
        nested["strict"] = format["strict"]
    return normalize_response_format(
        {"type": "json_schema", "json_schema": nested}, prefix="text.format")


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

    # `store` 只接受布尔：`"true"`（字符串）在旧实现里因 `is True` 判否而被静默
    # 当成 false 放行——客户端以为开了存储、网关其实什么都没存。显式报错。
    store = body.get("store")
    if store is not None and not isinstance(store, bool):
        raise InvalidRequest("store must be a boolean")
    if store is True:
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
            value = require_finite_number(body[key], key)
            if key == "temperature" and value > MAX_TEMPERATURE:
                raise InvalidRequest(f"temperature must be <= {MAX_TEMPERATURE}")
            upstream[key] = value
    if "tool_choice" in body:
        upstream["tool_choice"] = _map_tool_choice(body["tool_choice"])
    if isinstance(body.get("parallel_tool_calls"), bool):
        upstream["parallel_tool_calls"] = body["parallel_tool_calls"]
    if "max_output_tokens" in body and body["max_output_tokens"] is not None:
        # CB 上游只认 max_tokens（max_completion_tokens 被忽略，TECHNICAL §3.4）
        upstream["max_tokens"] = require_positive_int(
            body["max_output_tokens"], "max_output_tokens",
            maximum=MAX_OUTPUT_TOKENS)
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        upstream["reasoning_effort"] = reasoning["effort"]
    cache_key = body.get("prompt_cache_key")
    if isinstance(cache_key, str) and cache_key:
        # 与 chat 出站同理：显式会话标识能固定凭证，上游也认该字段
        upstream["prompt_cache_key"] = cache_key
    text_config = body.get("text")
    if isinstance(text_config, dict):
        if isinstance(text_config.get("verbosity"), str):
            upstream["verbosity"] = text_config["verbosity"]
        response_format = _map_response_format(text_config)
        if response_format is not None:
            upstream["response_format"] = response_format

    # ChatRequest.raw 供 executor 使用；Responses 元数据在出口侧重新生成
    return ChatRequest(model=model or "", messages=messages, stream=stream,
                       raw=upstream)
