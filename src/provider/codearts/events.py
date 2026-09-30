"""CodeArts（华为云码道 / snap-access 盘古引擎）SSE → 中立事件映射。

上游 SSE 与其它渠道都不一样：**逐行 `data:` JSON，没有空行分隔**（逆向记录 §5）。
因此不能用 `engine.sse` 的 `FrameAssembler`——那个要等空行才成帧，CodeArts 的流
会一直缓冲到连接结束才吐出唯一一帧，等于没有流式。本模块自带逐行读取器
`iter_data_lines`。

帧语义（逆向记录 §5，实测）：

* 快照帧 `{"text":"<当前完整文本>","prompt_tokens":..,"completion_tokens":..}`：
  `text` 是**累计全文**，不是增量 → 用 `TextSnapshot` 转成增量（替换语义）。
* 增量帧 `{"delta":{"content":...,"reasoning_content":...}}`（is_delta_response）。
* 结束帧 `{"text":"[DONE]","error_code":"0"}`。
* 错误帧 `{"text":"[DONE]","error_code":"ChatAgent.00001001","error_msg":...}`：
  HTTP 仍是 200，错误内嵌在流里（上游排队/TPM/未注册模型都走这条路）。

CodeArts 的业务码是**字符串**（`ChatAgent.00001001`），而中立 `Event.error_code`
只有 int 槽位，故原始码进 `error_message`，分类进 `error_kind`。
"""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ...provider.base import ErrKind, Event, EventKind, Usage, business_codes

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


class UpstreamProtocolViolation(ValueError):
    """上游违反可映射的结构约束（不静默吞掉）。"""


# 该账号用不了这个模型（重试无意义）→ (账号, 模型) 负缓存。
# `InferHub.002002009.404 model is not registered` / `InferHub.4004.200
# benefit not found` 均来自逆向记录 §7 实测原文。
_MODEL_ABSENT_MARKERS = ("002002009", "not registered", "4004.200", "benefit not found")
# 模型级限流：上游点名当前模型/会话（`TM.00001041`、TPM、并发会话）。
_MODEL_THROTTLE_MARKERS = ("00001041", "tpm", "429", "并发会话", "rate limit", "throttl")


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
    """字节流 → 逐行 `data:` 载荷（无空行分隔，不能复用 SSEFrame 状态机）。

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
    """一行 → JSON 载荷；无下游语义的行（事件名/编号/注释/空行）返回空串。"""
    text = line.strip()
    if text.startswith("data:"):
        return text[5:].strip()
    if text.startswith("{"):
        return text                      # 实测部分行没有 `data:` 前缀
    return ""


def parse_line(line: str, snapshot: TextSnapshot) -> list[Event]:
    """一行 JSON 载荷 → 中立事件列表（同帧可能同时带全文与 usage）。"""
    try:
        payload = json.loads(line)
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
    text = payload.get("text")
    if isinstance(text, str):
        if text == DONE_TEXT:
            events.append(Event(kind=EventKind.FINISH, finish_reason="stop"))
        else:
            delta = snapshot.delta(text)
            if delta:
                events.append(Event(kind=EventKind.CONTENT, content=delta))

    delta_payload = payload.get("delta")
    if isinstance(delta_payload, dict):
        content = delta_payload.get("content")
        if isinstance(content, str) and content:
            events.append(Event(kind=EventKind.CONTENT, content=content))
        reasoning = delta_payload.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            events.append(Event(kind=EventKind.REASONING, content=reasoning))

    usage = _usage(payload)
    if usage is not None:
        events.append(Event(kind=EventKind.USAGE, usage=usage))
    return events


def _usage(payload: dict[str, Any]) -> Usage | None:
    input_tokens = _as_int(payload.get("prompt_tokens"))
    output_tokens = _as_int(payload.get("completion_tokens"))
    if input_tokens is None and output_tokens is None:
        return None
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


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
    if status == 429:
        return ErrKind.MODEL if any(
            m in lowered for m in ("00001041", "tpm", "并发")) else ErrKind.SOFT
    if status == 400:
        return ErrKind.INVALID
    return ErrKind.OTHER
