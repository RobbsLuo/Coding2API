"""TRAE SOLO SSE → 中立事件映射。

上游事件序列（实测）：
    event:metadata → event:timing_cost → event:output ×N → event:extra_info
    → event:token_usage → event:done
    event:error 为流内业务错误（1005 = 权益不足）。
"""

from __future__ import annotations

import json

from ...engine.sse import SSEFrame
from ...provider.base import ErrKind, Event, EventKind, Usage

# 上游技术常量（来自逆向实测，勿改）
AGENT_HOST = "https://trae-api-cn.mchost.guru"
UG_HOST = "https://api.trae.cn"
OAUTH_HOST = "https://api.trae.com.cn"
CLIENT_ID = "en1oxy7wnw8j9n"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IDE_VERSION = "0.1.52"
IDE_VERSION_CODE = "20260811"
DEVICE_BRAND = "83DG"
OS_VERSION = "Windows 11 Pro"
FUNCTION = "solo_work_lite"
EP_CHAT = "/api/agent/v3/llm_utils_chat"
EP_MODELS = "/api/ide/v1/get_detail_param"
EP_EXCHANGE = "/cloudide/api/v3/trae/oauth/ExchangeToken"
EP_USER_INFO = "/cloudide/api/v3/trae/GetUserInfo"


class UpstreamProtocolViolation(ValueError):
    """上游事件违反可映射的结构约束（不静默吞掉）。"""


def parse_frame(frame: SSEFrame) -> Event | None:
    """单帧 → 中立事件；无内容的心跳/元数据帧返回 None。

    注意：output 帧可能同时携带 response 与 reasoning_content，此处按 content 优先，
    需要两者都保留时用 parse_all_events()。
    """
    name = frame.event.strip()
    if not frame.data:
        return None
    try:
        payload = json.loads(frame.data)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation(f"unparsable SSE data for event {name!r}") from error
    if not isinstance(payload, dict):
        raise UpstreamProtocolViolation(f"SSE data for {name!r} is not an object")

    if name == "output":
        return _parse_output(payload)
    if name == "token_usage":
        return Event(kind=EventKind.USAGE, usage=_parse_usage(payload))
    if name == "done":
        reason = payload.get("finish_reason")
        finish = reason if isinstance(reason, str) else None
        return Event(kind=EventKind.FINISH, finish_reason=finish)
    if name == "error":
        code = payload.get("code")
        message = payload.get("message")
        return Event(
            kind=EventKind.ERROR,
            error_code=code if isinstance(code, int) and not isinstance(code, bool) else None,
            error_message=message if isinstance(message, str) else None,
        )
    return None  # metadata / timing_cost / extra_info 等无下游语义


def _parse_output(payload: dict) -> Event | None:
    content = payload.get("response")
    reasoning = payload.get("reasoning_content")
    tool_calls = payload.get("tool_calls")

    has_content = isinstance(content, str) and content != ""
    has_reasoning = isinstance(reasoning, str) and reasoning != ""
    has_tools = isinstance(tool_calls, list) and len(tool_calls) > 0

    if has_tools:
        return Event(kind=EventKind.TOOL_CALLS, tool_calls=tool_calls)
    if has_content:
        return Event(kind=EventKind.CONTENT, content=content)
    if has_reasoning:
        return Event(kind=EventKind.REASONING, content=reasoning)
    return None


def _parse_usage(payload: dict) -> Usage:
    def as_int(key: str) -> int | None:
        value = payload.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return Usage(
        input_tokens=as_int("prompt_tokens"),
        output_tokens=as_int("completion_tokens"),
        reasoning_tokens=as_int("reasoning_tokens"),
    )


def parse_all_events(frame: SSEFrame) -> list[Event]:
    """完整映射：一帧拆成多个事件，content 与 reasoning 都不丢。

    TRAE 的 output 帧实测会同时给出 response 与 reasoning_content
    （参考实现 trae2api-web 也是两个都转发）。
    """
    name = frame.event.strip()
    if name != "output" or not frame.data:
        event = parse_frame(frame)
        return [] if event is None else [event]
    try:
        payload = json.loads(frame.data)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation(f"unparsable SSE data for event {name!r}") from error
    if not isinstance(payload, dict):
        raise UpstreamProtocolViolation(f"SSE data for {name!r} is not an object")

    events: list[Event] = []
    content = payload.get("response")
    reasoning = payload.get("reasoning_content")
    tool_calls = payload.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        events.append(Event(kind=EventKind.TOOL_CALLS, tool_calls=tool_calls))
    if isinstance(content, str) and content:
        events.append(Event(kind=EventKind.CONTENT, content=content))
    if isinstance(reasoning, str) and reasoning:
        events.append(Event(kind=EventKind.REASONING, content=reasoning))
    return events


def classify_error_code(code: int | None) -> ErrKind:
    """流内错误码分类：1005 = 权益不足 → PLAN。"""
    if code == 1005:
        return ErrKind.PLAN
    return ErrKind.OTHER


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码分类。1005 出现在 body 时同样按 PLAN 处理。"""
    text = body.decode("utf-8", errors="replace")
    if '"code":1005' in text.replace(" ", ""):
        return ErrKind.PLAN
    if status == 401:
        return ErrKind.DEAD
    if status in (404, 429):
        return ErrKind.SOFT
    if status >= 400:
        return ErrKind.OTHER
    return ErrKind.OTHER
