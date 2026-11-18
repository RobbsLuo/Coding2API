"""Gemini `generateContent` 请求 → 内部 `ChatRequest` 的入站映射。

形状取自 Google Generative Language API（`v1beta/models/{model}:generateContent`）：

    {contents: [{role: "user"|"model", parts: [...]}],
     systemInstruction: {parts: [{text}]},
     generationConfig: {temperature, topP, maxOutputTokens, stopSequences, ...},
     tools: [{functionDeclarations: [{name, description, parameters}]}],
     toolConfig: {functionCallingConfig: {mode, allowedFunctionNames}}}

映射到内部 chat 载荷后交给 `parse_chat_request`，因此数值边界、模型白名单、
引擎行为与 `/v1/chat/completions` 完全同源。本服务只实现 chat 子集，不支持项
**显式 400**（`InvalidRequest`），不静默降级。

模型名由调用方从 URL 取（`models/gemini-2.0-flash` → `gemini-2.0-flash`），
`stream` 由路由决定（`:streamGenerateContent` = true）。

已知取舍（写进 README）：
- `thinkingConfig` / `topK` / `candidateCount` 以外的 Google 私有采样字段不透传；
  `candidateCount != 1` 显式 400（出口结构只产生单候选，与本服务 `n` 同口径）。
- 服务端工具（`googleSearch` / `codeExecution` / `retrieval` 等）没有 chat 等价物
  → 400，而不是悄悄丢掉。
- `functionCall` / `functionResponse` 没有调用 id，用 `call_{name}` 稳定配对；
  同一轮里同名函数并行调用会撞 id（Gemini 协议本身也靠 name 区分）。
"""

from __future__ import annotations

import json
from typing import Any

from ..openai.request import ChatRequest, InvalidRequest, parse_chat_request

# Gemini 的 `role`：`user` / `model`（`function` 是更老的写法，按 user 处理）。
_ROLE_MAP = {"user": "user", "model": "assistant", "function": "user"}


def _text_of(parts: Any, where: str) -> str:
    """`parts` 里的 text 片段拼成一个字符串（忽略非 text 片段）。"""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        raise InvalidRequest(f"{where} must be an array")
    chunks: list[str] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            raise InvalidRequest(f"{where}[{index}] must be an object")
        text = part.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _system_instruction(body: dict[str, Any]) -> str:
    """`systemInstruction`（两种拼写都认）→ 系统提示词文本。"""
    raw = body.get("systemInstruction")
    if raw is None:
        raw = body.get("system_instruction")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        raise InvalidRequest("systemInstruction must be an object")
    return _text_of(raw.get("parts"), "systemInstruction.parts")


def _image_block(part: dict[str, Any], where: str) -> dict[str, Any] | None:
    """`inlineData` / `fileData` → chat 的 `image_url` 内容块。"""
    inline = part["inlineData"] if "inlineData" in part else part.get("inline_data")
    if isinstance(inline, dict):
        mime = inline.get("mimeType") or inline.get("mime_type")
        data = inline.get("data")
        if not isinstance(mime, str) or not mime:
            raise InvalidRequest(f"{where}.inlineData.mimeType must be a string")
        if not isinstance(data, str) or not data:
            raise InvalidRequest(f"{where}.inlineData.data must be a base64 string")
        return {"type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"}}
    file_data = part["fileData"] if "fileData" in part else part.get("file_data")
    if isinstance(file_data, dict):
        uri = file_data.get("fileUri") or file_data.get("file_uri")
        if not isinstance(uri, str) or not uri:
            raise InvalidRequest(f"{where}.fileData.fileUri must be a string")
        # 上游渠道基本只认 data URL / http URL；原样透传，由上游裁决可达性
        return {"type": "image_url", "image_url": {"url": uri}}
    return None


def _tool_call_id(name: str) -> str:
    """Gemini 没有调用 id：用函数名稳定配对 `functionCall` 与 `functionResponse`。"""
    return f"call_{name}"


def _function_args(raw: Any, where: str) -> str:
    """`functionCall.args` 是对象 → chat 的 `arguments` JSON 串。"""
    if raw is None:
        return ""
    if not isinstance(raw, dict):
        raise InvalidRequest(f"{where}.args must be an object")
    return json.dumps(raw, ensure_ascii=False)


def _user_messages(parts: list[Any], where: str) -> list[dict[str, Any]]:
    """user 内容：文本/图片折成一条 user 消息，`functionResponse` 拆成 tool 消息。

    顺序保持：文本块先攒着，遇到 functionResponse 就先冲掉前面的 user 消息，
    再追加 tool 消息（chat 要求 tool 消息紧跟在带 tool_calls 的 assistant 后）。
    """
    messages: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    pending_text: list[str] = []

    def flush() -> None:
        if not pending_text and not pending:
            return
        if pending:
            content: Any = list(pending)
            if pending_text:
                content.insert(0, {"type": "text",
                                   "text": "".join(pending_text)})
        else:
            content = "".join(pending_text)
        messages.append({"role": "user", "content": content})
        pending.clear()
        pending_text.clear()

    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            raise InvalidRequest(f"{where}[{index}] must be an object")
        if "functionResponse" in part:
            call = part["functionResponse"]
        elif "function_response" in part:
            call = part["function_response"]
        else:
            call = None
        if isinstance(call, dict):
            flush()
            name = call.get("name")
            if not isinstance(name, str) or not name:
                raise InvalidRequest(f"{where}[{index}].functionResponse.name required")
            response = call.get("response")
            messages.append({
                "role": "tool",
                "tool_call_id": _tool_call_id(name),
                "content": json.dumps(response if response is not None else {},
                                       ensure_ascii=False),
            })
            continue
        image = _image_block(part, f"{where}[{index}]")
        if image is not None:
            pending.append(image)
            continue
        text = part.get("text")
        if isinstance(text, str):
            pending_text.append(text)
    flush()
    return messages


def _assistant_message(parts: list[Any], where: str) -> dict[str, Any] | None:
    """model 内容：文本 → content，`functionCall` → chat `tool_calls`。"""
    texts: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            raise InvalidRequest(f"{where}[{index}] must be an object")
        if "functionCall" in part:
            call = part["functionCall"]
        elif "function_call" in part:
            call = part["function_call"]
        else:
            call = None
        if isinstance(call, dict):
            name = call.get("name")
            if not isinstance(name, str) or not name:
                raise InvalidRequest(f"{where}[{index}].functionCall.name required")
            tool_calls.append({
                "id": _tool_call_id(name),
                "type": "function",
                "function": {"name": name,
                             "arguments": _function_args(
                                 call.get("args"), f"{where}[{index}].functionCall")},
            })
            continue
        text = part.get("text")
        if isinstance(text, str):
            if part.get("thought") is True:
                reasoning.append(text)
            else:
                texts.append(text)
    message: dict[str, Any] = {"role": "assistant"}
    content = "".join(texts)
    message["content"] = content or None
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = tool_calls
    if message["content"] is None and not tool_calls:
        return None
    return message


def _convert_tools(raw: Any) -> list[dict[str, Any]]:
    """`tools` → chat function 工具表；服务端工具显式 400。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InvalidRequest("tools must be an array")
    converted: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InvalidRequest(f"tools[{index}] must be an object")
        if "functionDeclarations" in item:
            declarations = item["functionDeclarations"]
        elif "function_declarations" in item:
            declarations = item["function_declarations"]
        else:
            declarations = None
        if declarations is None:
            names = ", ".join(sorted(item)) or "empty object"
            raise InvalidRequest(
                f"tools[{index}] has no functionDeclarations ({names}); "
                "server-side Gemini tools are not supported")
        if not isinstance(declarations, list):
            raise InvalidRequest(f"tools[{index}].functionDeclarations must be an array")
        for pos, declaration in enumerate(declarations):
            where = f"tools[{index}].functionDeclarations[{pos}]"
            if not isinstance(declaration, dict):
                raise InvalidRequest(f"{where} must be an object")
            name = declaration.get("name")
            if not isinstance(name, str) or not name:
                raise InvalidRequest(f"{where}.name must be a non-empty string")
            function: dict[str, Any] = {"name": name}
            description = declaration.get("description")
            if isinstance(description, str):
                function["description"] = description
            parameters = declaration.get("parameters")
            if parameters is not None:
                if not isinstance(parameters, dict):
                    raise InvalidRequest(f"{where}.parameters must be an object")
                function["parameters"] = parameters
            converted.append({"type": "function", "function": function})
    return converted


def _convert_tool_choice(raw: Any) -> Any:
    """`toolConfig.functionCallingConfig` → chat `tool_choice`。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InvalidRequest("toolConfig must be an object")
    config = raw.get("functionCallingConfig") or raw.get("function_calling_config")
    if config is None:
        return None
    if not isinstance(config, dict):
        raise InvalidRequest("toolConfig.functionCallingConfig must be an object")
    mode = config.get("mode")
    if mode is None:
        return None
    normalized = str(mode).upper()
    if normalized == "AUTO":
        return "auto"
    if normalized == "NONE":
        return "none"
    if normalized == "ANY":
        names = config.get("allowedFunctionNames") or config.get(
            "allowed_function_names")
        if isinstance(names, list) and len(names) == 1 and isinstance(names[0], str):
            return {"type": "function", "function": {"name": names[0]}}
        return "required"
    raise InvalidRequest(f"toolConfig.functionCallingConfig.mode unsupported: {mode}")


def _apply_generation_config(raw: dict[str, Any], config: Any) -> None:
    """`generationConfig` → chat 采样键（未知键丢弃，不冒充支持）。"""
    if config is None:
        return
    if not isinstance(config, dict):
        raise InvalidRequest("generationConfig must be an object")
    count = config.get("candidateCount")
    if count is not None and (isinstance(count, bool) or count != 1):
        # 出口结构只产生单候选（与 chat 的 n 同口径），显式拒绝而不是静默只回一个
        raise InvalidRequest(
            "generationConfig.candidateCount must be 1: this gateway can only "
            "produce a single candidate")
    for source, target in (("temperature", "temperature"), ("topP", "top_p"),
                           ("presencePenalty", "presence_penalty"),
                           ("frequencyPenalty", "frequency_penalty"),
                           ("maxOutputTokens", "max_tokens")):
        if source in config:
            raw[target] = config[source]
    stops = config.get("stopSequences")
    if stops is not None:
        if not isinstance(stops, list) or not all(isinstance(item, str) for item in stops):
            raise InvalidRequest("generationConfig.stopSequences must be an array of strings")
        raw["stop"] = stops
    mime = config.get("responseMimeType")
    schema = config.get("responseSchema")
    if schema is not None:
        if not isinstance(schema, dict):
            raise InvalidRequest("generationConfig.responseSchema must be an object")
        raw["response_format"] = {"type": "json_schema",
                                  "json_schema": {"name": "gemini_response",
                                                  "schema": schema}}
    elif mime == "application/json":
        raw["response_format"] = {"type": "json_object"}
    elif mime is not None and mime != "text/plain":
        raise InvalidRequest(
            f"generationConfig.responseMimeType unsupported: {mime}")


def gemini_to_chat_body(body: Any) -> dict[str, Any]:
    """Gemini 请求体 → chat 请求体（不做数值校验，交给 parse_chat_request）。"""
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be a JSON object")
    contents = body.get("contents")
    if not isinstance(contents, list) or not contents:
        raise InvalidRequest("contents must be a non-empty array")

    messages: list[dict[str, Any]] = []
    system = _system_instruction(body)
    if system:
        messages.append({"role": "system", "content": system})

    for index, content in enumerate(contents):
        if not isinstance(content, dict):
            raise InvalidRequest(f"contents[{index}] must be an object")
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            raise InvalidRequest(f"contents[{index}].parts must be a non-empty array")
        role = content.get("role")
        if role is None:
            role = "user"
        if not isinstance(role, str) or role not in _ROLE_MAP:
            raise InvalidRequest(
                f"contents[{index}].role must be one of user/model/function")
        if role in ("user", "function"):
            messages.extend(_user_messages(parts, f"contents[{index}].parts"))
            continue
        message = _assistant_message(parts, f"contents[{index}].parts")
        if message is not None:
            messages.append(message)

    if not messages:
        raise InvalidRequest("contents produced no messages")

    chat: dict[str, Any] = {"messages": messages}
    tools = _convert_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
    choice = _convert_tool_choice(body.get("toolConfig"))
    if choice is not None:
        chat["tool_choice"] = choice
    _apply_generation_config(chat, body.get("generationConfig"))
    return chat


def parse_gemini_request(body: Any, *, model: str, stream: bool) -> ChatRequest:
    """Gemini 请求 → `ChatRequest`；`model` / `stream` 由路由提供。"""
    chat = gemini_to_chat_body(body)
    chat["model"] = model
    chat["stream"] = stream
    return parse_chat_request(chat)
