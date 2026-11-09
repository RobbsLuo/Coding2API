"""WPS 灵犀上游 SSE 帧 → 中立事件。

上游帧不是 OpenAI chunk，形状是（取证见 `docs/` 与 magpie / MiraiHub）：

```
{"code":"Success","usage":{…},"choices":[{"text":"…","reasoning_content":"…",
 "tool_calls":[…],"finish_reason":"stop"}]}
```

三个必须照抄、改了就坏的点：

1. **没有 `[DONE]`**：收尾靠 `finish_reason` 非空，由客户端补 FINISH；
2. **`usage.total_tokens` 是「当前上下文大小」**（含缓存命中），直接透传会
   让用量虚高几十倍 —— 必须自算 `prompt + completion`；
3. `cached_tokens` / `reasoning_tokens` 是**扁平字段**，方向与 OpenAI 相反
   （OpenAI 放在 `prompt_tokens_details` / `completion_tokens_details` 里）。

错误两种：HTTP 非 2xx（走 `classify_status`），以及 **HTTP 200 帧内**
`code != Success` / `result != ok`（走 `classify_code`）。后者最坑的是
`SQLNoRows` /「会话不存在」——run 过期，重建一次即可，不是账号故障。
"""

from __future__ import annotations

import re
from typing import Any

from ...provider.base import ErrKind, Event, EventKind, Usage

#: 审核拦截：上游把「需求被拦」也塞进 200 帧，且文案多变。
AUDIT_PATTERN = re.compile(
    r"\bAuditFailed\b|\baudit[_-]?blocked\b|暂时无法回答此问题|内容审核", re.I)
#: run / 会话失效（重建一次即可，不罚号）
_SESSION_EXPIRED = re.compile(
    r"SQLNoRows|会话不存在|session not found|数据不存在", re.I)
#: 余额/积分不足（走 CREDIT：冷却到次日，而不是换号重试）
_CREDIT_EXHAUSTED = re.compile(
    r"credit[s]?\s*(?:is\s*)?(?:not enough|insufficient|exhausted)|"
    r"insufficient[_ ]credits|积分不足|余额不足|智点不足", re.I)
#: 模型不存在 / 套餐不允许（负缓存，重试无意义）
_MODEL_MISSING = re.compile(
    r"model[^.]{0,40}(?:not found|not exist|not allowed|unsupported)|"
    r"模型不存在|无此模型|模型不可用", re.I)
#: 鉴权失效（cookie 过期 / 被踢下线）
_AUTH_FAILED = re.compile(
    r"unauthor|unauthenticated|login required|not logged in|"
    r"wps_sid (?:is )?(?:required|invalid|expired)|"
    r"登录(?:态)?(?:已)?失效", re.I)


def text_of(payload: Any) -> str:
    """从上游错误体里挖一条人类可读的话（顺序照抄参考实现）。"""
    if not isinstance(payload, dict):
        return str(payload or "")[:300]
    for key in ("hint", "msg", "message", "detail", "error_message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    data = payload.get("data")
    if isinstance(data, dict):
        inner = text_of(data)
        if inner:
            return inner
    return ""


def frame_error(frame: Any) -> tuple[str, str] | None:
    """帧内业务错误 → `(code, message)`；正常帧返回 None。"""
    if not isinstance(frame, dict):
        return None
    code = frame.get("code")
    if isinstance(code, str) and code.strip() and code.strip().lower() != "success":
        return code.strip(), text_of(frame) or f"灵犀上游返回 {code.strip()}"
    result = frame.get("result")
    if isinstance(result, str) and result.strip() and result.strip().lower() != "ok":
        return result.strip(), text_of(frame) or f"result={result.strip()}"
    return None


def is_session_expired(message: str) -> bool:
    """run / 会话失效（调用方据此丢弃缓存、重建一次）。"""
    return bool(_SESSION_EXPIRED.search(message or ""))


def is_audit_blocked(message: str) -> bool:
    """审核拦截：同一个请求重发还是被拦，故按请求级 400 处理。"""
    return bool(AUDIT_PATTERN.search(message or ""))


def classify_code(code: str | None, message: str = "") -> ErrKind:
    """帧内业务码/文案 → 错误分类（只按已取证语义，不猜）。

    * 审核拦截 → INVALID（换号无用，直接 400）；
    * 鉴权失效 → DEAD（需重新粘贴 Cookie）；
    * 额度不足 → CREDIT（冷却到次日签到恢复）；
    * 模型不存在 / 套餐不允许 → BLOCKED（(账号, 模型) 负缓存）；
    * 其余 → OTHER（4xx/5xx 里最保守的一档，累计 3 次冷却）。
    """
    text = f"{code or ''} {message or ''}"
    if _SESSION_EXPIRED.search(text):
        # run 过期：重建一次即可，不是账号故障（调用方在首帧前会重建）
        return ErrKind.SOFT
    if is_audit_blocked(text):
        return ErrKind.INVALID
    if _AUTH_FAILED.search(text):
        return ErrKind.DEAD
    if _CREDIT_EXHAUSTED.search(text):
        return ErrKind.CREDIT
    if _MODEL_MISSING.search(text):
        return ErrKind.BLOCKED
    return ErrKind.OTHER


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码分类。

    灵犀的拒绝几乎都来自**凭证或请求**，不是渠道级故障：

    * 401 → DEAD（登录态失效，只能重新粘贴 Cookie）；
    * 402 → CREDIT（余额/积分不足）；
    * 403 → SOFT：可能是风控抖动，也可能是接口权限；按可换号重试处理，
      不承认它是「账号被拉黑」（没有取证支持 RESTRICTED）；
    * 404 / 400 / 422 → 模型或参数问题（上游把「模型不可用」回 404）；
      400 归 REQUEST（不冷却、换号重试）——上游 400 多是请求形态问题；
    * 429 → MODEL（限流点名模型；同号其他模型仍可用，与 CB 的 6004 同型）；
    * 其余 → OTHER。
    """
    if status == 401:
        return ErrKind.DEAD
    if status == 402:
        return ErrKind.CREDIT
    if status == 403:
        return ErrKind.SOFT
    if status == 404:
        return ErrKind.INVALID
    if status in (400, 422):
        return ErrKind.REQUEST
    if status == 429:
        return ErrKind.MODEL
    return ErrKind.OTHER


def _usage(raw: Any) -> Usage | None:
    """上游 usage → 中立 Usage（total 自算，明细字段搬进 OpenAI 嵌套形状）。"""
    if not isinstance(raw, dict) or not raw:
        return None
    prompt = _int(raw.get("prompt_tokens"))
    completion = _int(raw.get("completion_tokens"))
    cached = _int(raw.get("cached_tokens"))
    if cached is None:
        cached = _int(_nested(raw, "prompt_tokens_details", "cached_tokens"))
    reasoning = _int(raw.get("reasoning_tokens"))
    if reasoning is None:
        reasoning = _int(_nested(raw, "completion_tokens_details",
                                 "reasoning_tokens"))
    return Usage(
        input_tokens=prompt if prompt is not None else 0,
        output_tokens=completion if completion is not None else 0,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
    )


def _nested(node: dict[str, Any], outer: str, inner: str) -> Any:
    value = node.get(outer)
    return value.get(inner) if isinstance(value, dict) else None


def _int(value: Any) -> int | None:
    """宽松取整数：bool 不算（`True` 会被 int() 变成 1，是脏数据）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _tool_calls(items: Any) -> list[dict] | None:
    """上游 tool_calls 列表 → 中立形状（保留 index/id/function）。"""
    if not isinstance(items, list):
        return None
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        call: dict[str, Any] = {}
        index = _int(item.get("index"))
        if index is not None:
            call["index"] = index
        if isinstance(item.get("id"), str) and item["id"]:
            call["id"] = item["id"]
            call["type"] = "function"
        function = item.get("function")
        if isinstance(function, dict):
            piece: dict[str, Any] = {}
            if isinstance(function.get("name"), str) and function["name"]:
                piece["name"] = function["name"]
            if isinstance(function.get("arguments"), str):
                piece["arguments"] = function["arguments"]
            call["function"] = piece
        if call:
            out.append(call)
    return out or None


def parse_frame(frame: Any) -> list[Event]:
    """一帧 → 若干中立事件（正文/思考/工具/用量/收尾可同帧）。

    结构非法（非对象）不抛错也不产出 —— 上游偶尔夹带心跳/空帧；
    但**业务错误绝不静默**：`code != Success` 一律产出 ERROR 事件。
    """
    if not isinstance(frame, dict):
        return []
    error = frame_error(frame)
    if error is not None:
        code, message = error
        return [Event(kind=EventKind.ERROR,
                      error_code=None,
                      error_message=message,
                      error_kind=classify_code(code, message))]
    events: list[Event] = []
    choices = frame.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if isinstance(choice, dict):
        text = choice.get("text")
        if isinstance(text, str) and text:
            events.append(Event(kind=EventKind.CONTENT, content=text))
        reasoning = choice.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            events.append(Event(kind=EventKind.REASONING, content=reasoning))
        calls = _tool_calls(choice.get("tool_calls"))
        if calls:
            events.append(Event(kind=EventKind.TOOL_CALLS, tool_calls=calls))
        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish.strip():
            events.append(Event(kind=EventKind.FINISH,
                                finish_reason=finish.strip()))
    usage = _usage(frame.get("usage"))
    if usage is not None:
        events.append(Event(kind=EventKind.USAGE, usage=usage))
    return events