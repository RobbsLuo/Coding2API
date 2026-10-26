"""OpenAI 请求校验（宽松：只校验本服务依赖的字段）。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


class InvalidRequest(ValueError):
    pass


# 采样参数与输出上限的宽松但有限的边界：本服务只做透传，上界只为拦住
# 明显畸形的输入（NaN/Inf、负数、超大值），真正语义由上游裁决。
MAX_TEMPERATURE = 1000.0
MAX_OUTPUT_TOKENS = 4_000_000


def require_finite_number(value: Any, name: str) -> float:
    """数值字段校验：拒绝 bool、非数、NaN/Inf。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequest(f"{name} must be a number")
    if not math.isfinite(value):
        raise InvalidRequest(f"{name} must be a finite number")
    return float(value)


def require_positive_int(value: Any, name: str, *, maximum: int) -> int:
    """正整数校验：拒绝 bool、非整数、≤0、超过上界。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequest(f"{name} must be an integer")
    if value <= 0 or value > maximum:
        raise InvalidRequest(f"{name} must be between 1 and {maximum}")
    return value


# 采样参数：OpenAI 语义是 [-2, 2]，这里只做有限值 + 该范围校验。
SAMPLING_PENALTY_KEYS = ("presence_penalty", "frequency_penalty")
MAX_PENALTY_ABS = 2.0
# `n`（生成候选数）：本网关出口**结构上只能产生单个 choice**
# （`provider/openai_chunk.py::first_choice` 只取 choices[0]，
# `compat/openai/response.py` 恒单 choice），所以只接受 1。此前允许 1..8
# 属于「假装支持」——客户端要多候选，网关静默只回一个，比显式 400 更难排查。
# 这是**破坏性语义变更**（README 兼容性表已标注）。
MAX_CHOICES = 1


def validate_n(raw: dict[str, Any]) -> None:
    """`n != 1` 显式 400：出口只能产生单个 choice，不接受多候选。

    与 `require_positive_int` 分开：那里的报错是「超出范围 1..N」，而这里
    要说清「不是范围问题，是结构不支持」——否则用户会以为调小到 2 就好。
    """
    if "n" not in raw:
        return
    value = raw["n"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequest("n must be an integer")
    if value != MAX_CHOICES:
        raise InvalidRequest(
            "n must be 1: this gateway's response structure can only produce "
            f"a single choice (got n={value})")


def validate_numeric_fields(raw: dict[str, Any]) -> None:
    """校验全部数值型顶层字段，拒绝 NaN/±Inf 与畸形值。

    已知键（temperature / top_p / 采样惩罚 / seed / logit_bias）的范围与
    类型校验在这一层做：它们有业务边界（如 `temperature <= 1000`），不只是
    「是不是有限数」。

    **JSON 溢出成 inf 的 `1e999` 由更外层拦**（`api/deps.py::read_json_body`
    的 `parse_float`）：那里是解析入口，三个出口共用，一处生效全生效；
    逐字段补齐要各写一遍，且 chat 出口会把整个请求体透传给上游，未知键里的
    inf 会一路带到 httpx 序列化那一步炸成 500。
    """
    for key in ("temperature", "top_p"):
        if key in raw:
            value = require_finite_number(raw[key], key)
            if key == "temperature" and value > MAX_TEMPERATURE:
                raise InvalidRequest(f"temperature must be <= {MAX_TEMPERATURE}")
            raw[key] = value
    for key in SAMPLING_PENALTY_KEYS:
        if key in raw:
            value = require_finite_number(raw[key], key)
            if abs(value) > MAX_PENALTY_ABS:
                raise InvalidRequest(f"{key} must be between -2 and 2")
            raw[key] = value
    if "seed" in raw:
        # seed 是任意整数（可为负）；只拒绝 bool/非整数/NaN。
        value = raw["seed"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidRequest("seed must be a number")
        if not math.isfinite(value):
            raise InvalidRequest("seed must be a finite number")
        raw["seed"] = int(value)
    # 无条件调用：缺省 `n` 也要走一遍校验入口，避免「调用点漏判」时静默放行
    validate_n(raw)
    if "logit_bias" in raw:
        bias = raw["logit_bias"]
        if not isinstance(bias, dict):
            raise InvalidRequest("logit_bias must be an object")
        for token, value in bias.items():
            if not isinstance(token, str):
                raise InvalidRequest("logit_bias keys must be strings")
            require_finite_number(value, f"logit_bias[{token!r}]")


def validate_content_parts(messages: list[Any]) -> None:
    """校验 content 数组里的 `image_url` 内容块形状。

    只校验本服务要读取的字段（`image_url.url`）：畸形图片是**客户端**错误，
    放行会让上游回 400，而本服务按上游 4xx 冷却凭证——等于让一个写错的
    客户端把健康账号打凉。其余 part 类型一律放行（宽松透传，语义交上游裁决）。
    """
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image = part.get("image_url")
            if not isinstance(image, dict):
                raise InvalidRequest(
                    f"messages[{index}].content[{part_index}].image_url must be an object")
            url = image.get("url")
            if not isinstance(url, str) or not url:
                raise InvalidRequest(
                    f"messages[{index}].content[{part_index}].image_url.url "
                    "must be a non-empty string")


def normalize_response_format(value: Any, *,
                              prefix: str = "response_format") -> dict[str, Any]:
    """`response_format` 语义收口：只放行 OpenAI 定义的三种形状。

    此前 chat 入站 `raw=dict(body)` 整包透传，`response_format` 既不校验也不
    归一：畸形的 json_schema（缺 name/schema）会一路打到上游、按 4xx 冷却凭证，
    未知 type（`{"type": "audio"}` 之类）则被静默当成「上游会懂」。这里在入口
    显式收口；Responses 出口（`text.format` → 同一形状）复用本函数，两条入站
    协议不会各写一份而漂移。

    归一后的形状（与官方 openai-python 3.x 的 chat 类型一致）：

    - `{"type": "text"}`
    - `{"type": "json_object"}`
    - `{"type": "json_schema", "json_schema": {"name": str, "schema": Any,
      "description"?: str, "strict"?: bool}}`

    未知 type 显式 400（不静默降级）；`json_schema` 内的未知字段丢弃——
    它们既非 chat 规范、也无上游认，留着只是把客户端笔误透传出去。

    `prefix` 只影响报错文案里的字段路径（Responses 侧传 `text.format`），
    校验与输出形状完全一致。
    """
    if not isinstance(value, dict):
        raise InvalidRequest(f"{prefix} must be an object")
    kind = value.get("type")
    if kind in ("text", "json_object"):
        return {"type": kind}
    if kind != "json_schema":
        raise InvalidRequest(
            f"{prefix}.type {kind!r} is not supported by this gateway")
    schema = value.get("json_schema")
    if not isinstance(schema, dict):
        raise InvalidRequest(
            f"{prefix}.json_schema must be an object for json_schema")
    name = schema.get("name")
    if not isinstance(name, str) or not name:
        raise InvalidRequest(f"{prefix}.name must be a non-empty string")
    if "schema" not in schema:
        raise InvalidRequest(f"{prefix}.schema is required for json_schema")
    normalized: dict[str, Any] = {"name": name, "schema": schema["schema"]}
    if isinstance(schema.get("description"), str):
        normalized["description"] = schema["description"]
    if isinstance(schema.get("strict"), bool):
        normalized["strict"] = schema["strict"]
    return {"type": "json_schema", "json_schema": normalized}


@dataclass(slots=True)
class ChatRequest:
    model: str
    messages: list[dict[str, Any]]
    stream: bool
    raw: dict[str, Any]


def parse_chat_request(body: Any) -> ChatRequest:
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be a JSON object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidRequest("messages must be a non-empty array")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise InvalidRequest(f"messages[{index}] must be an object")
        if not isinstance(message.get("role"), str):
            raise InvalidRequest(f"messages[{index}].role must be a string")
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        raise InvalidRequest("model must be a string")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise InvalidRequest("stream must be a boolean")
    # 数值字段校验：NaN/Inf 由 read_json_body 拦在 JSON 层，但 `-1`/`true`
    # 这类合法 JSON 仍会透传到上游、污染凭证健康度与统计；与 Anthropic /
    # Responses 出口保持同一套边界。在 raw 副本上归一，不改动调用方 body。
    raw = dict(body)
    validate_numeric_fields(raw)
    validate_content_parts(messages)
    for key in ("max_tokens", "max_completion_tokens"):
        if key in raw and raw[key] is not None:
            require_positive_int(raw[key], key, maximum=MAX_OUTPUT_TOKENS)
    if raw.get("response_format") is not None:
        # 收口而非整包透传：畸形/未知形状在这里 400，别让它打到上游把凭证打凉
        raw["response_format"] = normalize_response_format(raw["response_format"])
    return ChatRequest(model=model or "", messages=messages, stream=stream, raw=raw)
