"""CodeArts（华为云码道 / snap-access 盘古引擎）SSE → 中立事件映射。

上游用**逐行 `data:` JSON + `data:[DONE]` 结束**（标准 OpenAI 流式形状），
不能用 `engine.sse` 的 `FrameAssembler`——那个要等空行才成帧，且不会识别
`[DONE]` 语义。本模块自带逐行读取器 `iter_data_lines`（与 SSE 帧的不同点：
本引擎的 `data:` 行之间**有空行**，逐行读取天然把它当空行跳过）。

帧语义（2026-09-30 抓真实流核实）：

* **v2 `/api/v2/chat/completions`（实际走的路径）是标准 OpenAI chunk**：
  `{"choices":[{"delta":{"content":..,"reasoning_content":..},"finish_reason":..}]}`，
  增量在 `delta`（**不是**累计全文），收尾帧带 `usage`，最后 `data:[DONE]`。
* 旧形状（legacy §4 或早期记录）：`{"text":"<当前完整文本>"}` 是**累计全文**，
  用 `TextSnapshot` 转成增量（替换语义）；结束帧 `{"text":"[DONE]","error_code":"0"}`。
  解析器两种形状同时兼容，按字段是否存在分派。
* 错误帧：HTTP 仍是 200，错误内嵌在流里（`error_code` 形如
  `ChatAgent.00001001` / `TM.00001041`），上游排队/TPM/未注册模型都走这条路。

CodeArts 的业务码是**字符串**（`ChatAgent.00001001`），而中立 `Event.error_code`
只有 int 槽位，故原始码进 `error_message`，分类进 `error_kind`。
"""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ...provider.base import (
    ErrKind,
    Event,
    EventKind,
    UpstreamProtocolViolation,
    Usage,
    business_codes,
)

# 上游技术常量（逆向记录 §2/§3/§7，勿改）
SNAP_ENGINE_HOST = "https://snap-access.cn-north-4.myhuaweicloud.com"
SNAP_MANAGER_PREFIX = "/snap-manager"
STS_HOST = "https://sts.cn-north-4.myhuaweicloud.com"
PORTAL_HOST = "https://codearts.huaweicloud.com/portal"
BENEFIT_HOST = "https://opengw.developer.huaweicloud.com"
# refresh_token 与 client_id 绑定：换值刷新会被 STS 拒（STS5.1806）。
CLIENT_ID = "codearts-agent"

EP_CHAT_V2 = "/api/v2/chat/completions"
EP_CHAT = "/v1/chat/chat"
EP_MODEL_BUILTIN = "/v1/model/builtin"
EP_BENEFIT_CONFIG = "/api/v1/gateway/config"
EP_BENEFIT_CLAIM = "/api/v1/benefit/claim"
EP_TOKEN_BALANCE = "/api/v1/user/tokens/balance"
EP_LOGIN_TICKET = "/v1/login/ticket"
EP_OAUTH_TOKENS = "/v1/oauth2/tokens"
EP_CURRENT_USER = "/v1/current/user"
EP_CALLER_IDENTITY = "/v5/caller-identity"

# 福利模型路由头（逆向记录 §7：无此头报 InferHub.002002009.404 未注册）。
HEADER_MAAS_TYPE = "maas_type"
MAAS_BENEFIT = "benefit"
# 内置模型接口要求（逆向记录 §7）。
AGENT_TYPE_PROMPT_CENTER = "PromptCenter"

DONE_TEXT = "[DONE]"



# 该账号用不了这个模型（重试无意义）→ (账号, 模型) 负缓存。
# `InferHub.002002009.404 model is not registered` / `InferHub.4004.200
# benefit not found` 均来自逆向记录 §7 实测原文。
_MODEL_ABSENT_MARKERS = ("002002009", "not registered", "4004.200", "benefit not found")
# 模型级限流：上游点名当前模型/会话（`TM.00001041`、TPM、并发会话）。
_MODEL_THROTTLE_MARKERS = ("00001041", "tpm", "429", "并发会话", "rate limit", "throttl")
# HTTP 状态码侧的限流/并发标记。与流内业务码不同，这里判的是**响应体文本**：
# 上游把并发超限放在 HTTP 400（不是 429），命中即应算可重试的模型级限流。
_STATUS_THROTTLE_MARKERS = ("00001041", "tpm", "并发", "rate limit", "throttl")
# 结构性噪声行（心跳、被截断的裸括号）：整行只有空白与 `[]:,`，无 JSON 语义。
# `_data_payload` 会放行 `{`/`[` 开头的行，实测上游偶发只发一个括号的心跳。
_SSE_NOISE_RE = re.compile(r"^[\s{}\[\]:,]*$")


@dataclass(slots=True)
class TextSnapshot:
    """累计全文 → 增量（替换语义）。

    上游快照帧给的是「当前完整文本」而非 delta。直接转发全文会让客户端把
    已展示的内容再追加一遍（重复输出），因此必须做差：

    * 新文本以旧文本为前缀 → 只发新增后缀（常态）；
    * 否则（上游改写 / 回退 / 换了新回答）→ 无法做增量，整段重发。
      宁可重复一段，也不能丢内容。
    """

    _emitted: str = ""

    def delta(self, text: str) -> str:
        if text == self._emitted:
            return ""
        delta = (text[len(self._emitted):]
                 if text.startswith(self._emitted) else text)
        self._emitted = text
        return delta


async def iter_data_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """字节流 → 逐行 `data:` 载荷（也有空行分隔，逐行读取天然跳过它们）。

    增量 UTF-8 解码，避免多字节字符被块边界切断后变成替换字符。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer = ""
    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            payload = _data_payload(line)
            if payload:
                yield payload
    tail = _data_payload(buffer)          # 末行没有换行符
    if tail:
        yield tail


def _data_payload(line: str) -> str:
    """一行 → JSON 载荷；无下游语义的行（事件名/编号/注释/空行）返回空串。

    `data:[DONE]` 是流结束哨兵（非 JSON），原样保留给 `parse_line` 识别。
    """
    text = line.strip()
    if text.startswith("data:"):
        return text[5:].strip()
    if text.startswith("{"):
        return text                      # 实测部分行没有 `data:` 前缀
    return ""


def parse_line(line: str, snapshot: TextSnapshot) -> list[Event]:
    """一行载荷 → 中立事件列表（同帧可能同时带正文/思考/usage/finish）。"""
    text = line.strip()
    if text == DONE_TEXT:
        return [Event(kind=EventKind.FINISH, finish_reason="stop")]
    if _SSE_NOISE_RE.match(text):
        # 心跳 / 被截断的裸括号行：无可映射语义，跳过而不是让整条响应以
        # unparsable 失败（那会把一次心跳升级成 500，实测发生过）。
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation("unparsable CodeArts SSE data") from error
    if not isinstance(payload, dict):
        raise UpstreamProtocolViolation("CodeArts SSE data is not an object")
    return _events_from_payload(payload, snapshot)


def _events_from_payload(payload: dict[str, Any], snapshot: TextSnapshot) -> list[Event]:
    error_code = payload.get("error_code")
    if isinstance(error_code, str) and error_code not in ("", "0"):
        # 业务错误内嵌在 HTTP 200 的流里：分类在解析时定死（单一来源）。
        message = payload.get("error_msg")
        detail = f"{error_code} {message}".strip() if isinstance(message, str) else error_code
        return [Event(kind=EventKind.ERROR, error_code=None, error_message=detail,
                      error_kind=classify_error_code(error_code))]

    events: list[Event] = []
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        # 标准 OpenAI chunk（v2 端点实测形状）：增量在 choices[].delta。
        events.extend(_events_from_choices(choices))
    text = payload.get("text")
    if isinstance(text, str):
        # 旧形状：`text` 是累计全文 → 用快照做差。
        if text == DONE_TEXT:
            events.append(Event(kind=EventKind.FINISH, finish_reason="stop"))
        else:
            delta = snapshot.delta(text)
            if delta:
                events.append(Event(kind=EventKind.CONTENT, content=delta))
    delta_payload = payload.get("delta")
    if isinstance(delta_payload, dict):
        # 旧形状的独立 delta 帧。
        events.extend(_delta_events(delta_payload))
    # usage 可能在收尾帧（choices 为空）单独给出，故在顶层统一取。
    usage_object = payload.get("usage")
    if isinstance(usage_object, dict):
        events.append(Event(kind=EventKind.USAGE, usage=_usage_object(usage_object)))
    else:
        usage = _usage(payload)                      # 旧形状：token 数平铺在顶层
        if usage is not None:
            events.append(Event(kind=EventKind.USAGE, usage=usage))
    return events


def _events_from_choices(choices: list[Any]) -> list[Event]:
    events: list[Event] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            events.extend(_delta_events(delta))
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            events.append(Event(kind=EventKind.FINISH, finish_reason=finish_reason))
    return events


def _delta_events(delta: dict[str, Any]) -> list[Event]:
    """增量块 → 内容 / 思考 / 工具调用事件（空串与非字符串一律忽略）。"""
    events: list[Event] = []
    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list):
        kept = [call for call in tool_calls if isinstance(call, dict)]
        if kept:
            events.append(Event(kind=EventKind.TOOL_CALLS, tool_calls=kept))
    content = delta.get("content")
    if isinstance(content, str) and content:
        events.append(Event(kind=EventKind.CONTENT, content=content))
    reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        events.append(Event(kind=EventKind.REASONING, content=reasoning))
    return events


def _usage(payload: dict[str, Any]) -> Usage | None:
    """旧形状：token 数平铺在顶层。"""
    input_tokens = _as_int(payload.get("prompt_tokens"))
    output_tokens = _as_int(payload.get("completion_tokens"))
    if input_tokens is None and output_tokens is None:
        return None
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def _usage_object(usage: dict[str, Any]) -> Usage:
    """标准 OpenAI `usage` 对象（v2 收尾帧）。"""
    return Usage(input_tokens=_as_int(usage.get("prompt_tokens")),
                 output_tokens=_as_int(usage.get("completion_tokens")))


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def classify_error_code(code: str | None) -> ErrKind:
    """流内业务码（字符串）分类。

    只有 `002002009.404`（模型未注册）与 `4004.200`（福利未领取）是逆向记录里
    的实测原文，其余按语义就近映射；未识别的一律 OTHER（累计 3 次 → 10m
    整条渠道冷却），绝不猜测成更重的死禁。
    """
    text = (code or "").strip().lower()
    if not text or text == "0":
        return ErrKind.OTHER
    if any(marker in text for marker in _MODEL_ABSENT_MARKERS):
        return ErrKind.BLOCKED
    if any(marker in text for marker in _MODEL_THROTTLE_MARKERS):
        return ErrKind.MODEL
    if "1005" in text or "quota" in text or "insufficient" in text:
        return ErrKind.PLAN
    if "apig.0301" in text or "decrypt token" in text:
        return ErrKind.DEAD
    return ErrKind.OTHER


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码 + body 业务码分类（判定顺序即优先级）。

    与 CodeBuddy / TRAE 同序，只把 CodeArts 自己的标记点加进来：
    `APIG.0301 decrypt token fail` 实测是签名/临时凭证失效（会话失效）。
    """
    text = body.decode("utf-8", errors="replace")
    lowered = text.lower()
    codes = business_codes(text)
    if status == 402 or 14018 in codes:
        return ErrKind.CREDIT
    if 1005 in codes:
        return ErrKind.PLAN
    if status in (400, 404) and (
           11102 in codes or any(m in lowered for m in _MODEL_ABSENT_MARKERS)):
        return ErrKind.BLOCKED
    if status in (401, 403):
        return ErrKind.DEAD
    if status == 404:
        return ErrKind.SOFT
    # 限流/并发会话标记：HTTP 400 与 429 都可能承载（实测并发超限走 400 +
    # `TM.00001041`）。统一判为模型级限流，避免 400 落进 INVALID → 换号重试
    # 也没用、且不触发冷却（与流内 classify_error_code 的 TM.00001041 一致）。
    if status in (400, 429) and any(
            m in lowered for m in _STATUS_THROTTLE_MARKERS):
        return ErrKind.MODEL
    if status == 429:
        return ErrKind.SOFT
    if status == 400:
        return ErrKind.INVALID
    return ErrKind.OTHER
