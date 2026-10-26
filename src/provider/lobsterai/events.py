"""LobsterAI 上游协议：端点常量、SSE 解析与错误分类。

上游两套域（实测，2026-10-26）：

* **API 域** `lobsterai-server.youdao.com`：登录交换 / 续期 / 对话代理 /
  模型目录 / 积分 / 签到活动。
* **门户域** `lobsterai.youdao.com`：登录页 SPA（`/portal#/login`）。
  两者同 IP 同 CNAME（网易广州 ALB），是同一套服务的不同路径。
* **版本域** `api-overmind.youdao.com`：客户端当前版本号（签到按版本门禁，
  旧版本号拿不到活动）。

**六条硬约束**（每一条都是踩过的坑，务必在改动时保持）：

1. **对话只接受流式**：`stream:false` 上游回 HTTP 500。请求体强制
   `stream=true`，非流式由本服务聚合（`compat/openai/response.aggregate`）。
2. **业务错误藏在 HTTP 200 的 SSE 流首**：额度不足（40201）、未知模型
   （40300）等以 `event:error` / `{"error":{...}}` 帧表达。不解析流内错误
   → 错误伪装成空回复，客户端收到「什么都没发生」。
3. **必须带 `X-LobsterAI-Client-Capabilities`**：声明两个能力，缺一不可——
   `kimi-k3-agentic-v1` 是模型目录的准入条件（不带则目录里没有 `kimi-k3`）；
   `thinking-level-control-v1` 是关闭思考（`reasoning_effort:"off"`）的前提，
   不带该能力时服务端直接回 500。
4. **签到按 `clientVersion` 门禁**：写死 `0.1.0` 会拿到 `slotState=empty`
   （签不到）。真实版本是日期式（`2026.10.23`），从版本域动态取。
5. **身份三元组随凭证持久化**：`uuid` / `firstKeyfrom` / `latestKeyfrom`
   缺一个就永远续不了期（见 `credential.py`）。
6. **`/api/user/quota` 只含 `freeCreditsTotal=300`**，不含活动与订阅积分；
   对账必须用 `/api/user/profile-summary` 的 `totalCreditsRemaining`。

**错误分类顺序即优先级**（见 `classify_status`）：额度关键词必须**先于**
4xx 判定——实测上游用 400 + 中文「积分不足」表达余额耗尽，只按状态码会把
它判成可重试的 client 错误，于是反复重试一个永远不会成功的账号。
会话死亡（40100/40101）必须**先于** 429 判定：会话已死时换号重试毫无
意义（该账号需重新登录）。
"""

from __future__ import annotations

import json
from typing import Any

from ...engine.sse import SSEFrame
from ..base import (
    ErrKind,
    Event,
    EventKind,
    UpstreamProtocolViolation,
    Usage,
    business_codes,
)
from ..openai_chunk import openai_event, openai_events, payload_of, usage_from_openai

# ---------------------------------------------------------------- 域与端点

#: API 域（对话 / 目录 / 积分 / 签到 / 令牌交换）。
API_BASE = "https://lobsterai-server.youdao.com"
#: 门户域（登录页 SPA）。与 API 域同 IP 同 CNAME，但**单独一个常量**：
#: 未来两者可能分离部署，且上游实现本身也把这两个地址分开。
PORTAL_BASE = "https://lobsterai.youdao.com"
#: 客户端版本号域（签到门禁用）。响应**不是**统一信封：载荷在 `data.value.version`。
VERSION_API = (
    "https://api-overmind.youdao.com/openapi/get/luna/hardware/lobsterai/prod/update"
)

EP_EXCHANGE = "/api/auth/exchange"
EP_REFRESH = "/api/auth/refresh"
EP_CHAT = "/api/proxy/v1/chat/completions"
EP_MODELS = "/api/models/available"
#: 模型目录·公开兜底（**无需鉴权**）。未登录或 Bearer 目录不可用时用它，
#: 保证模型清单不依赖登录态。
EP_PRICING_CATALOG = "/api/models/pricing-catalog"
EP_PROFILE_SUMMARY = "/api/user/profile-summary"
EP_ACTIVITY_SLOT = "/api/client-activities/slot"
EP_ACTIVITY_BASE = "/api/client-activities"

#: 客户端能力声明（两个都必须，理由见模块头约束 3）。
CLIENT_CAPABILITIES = "kimi-k3-agentic-v1,thinking-level-control-v1"
#: User-Agent（照抄上游客户端形态）。
USER_AGENT = "LobsterAI/0.1.0"
#: 版本号兜底常量：版本域取不到时用它。宁可版本略旧（签到活动可能拿不到），
#: 也不要让整个对话链路因为一次版本查询失败而不可用。
FALLBACK_CLIENT_VERSION = "2026.10.23"

#: 签到活动槽位的三个固定 query 参数。`platform` 是**伪装客户端形态**
#: （`win32`），与本服务跑在什么系统上无关，改了可能拿不到活动。
SLOT_PLACEMENT = "desktop_sidebar"
SLOT_CONTAINER_API_VERSION = "2"
SLOT_PLATFORM = "win32"

#: 每日签到奖励积分（上游 `rewardCredits` 实测值）。上游每日发放一次，
#: 积分**当天有效**（过期作废）。
CHECKIN_REWARD_CREDITS = 100.0

# ---------------------------------------------------------------- 错误分类

#: 额度不足关键词（中英双通道：同一后端在不同场景会回中文或英文文案，
#: 只匹配一种会漏判）。「额度已用完」与「升级套餐」是实测的**实际文案**
#: ——早期表里只有「额度用尽」「积分用完」，字面不同，于是最主要的失败模式
#: 被判成「不换号」，用户看到「一个账号用完出错但不切换」。
_CREDIT_MARKERS: tuple[str, ...] = (
    "insufficient credit", "no credit", "credit exhausted", "out of credit",
    "quota exceeded", "quota exhaust", "payment required", "credit not enough",
    "not enough credit", "freecreditsused", "free credits used",
    "free quota", "quota used up", "upgrade your plan", "upgrade to continue",
    "积分不足", "额度不足", "余额不足", "积分用完", "额度用尽", "没有积分",
    "积分耗尽", "额度已用完", "升级套餐",
)

#: 会话终止标记：刷新被拒（40100/40101），终态、只能重新登录。
_SESSION_DEAD_MARKERS: tuple[str, ...] = (
    "40100", "40101", "token rejected", "refresh token was rejected",
)

#: 请求侧错误（模型不存在 / 参数非法）：换号没用，直接回 400。
_REQUEST_MARKERS: tuple[str, ...] = (
    "invalid model", "model not found", "unknown model",
)


def relogin_required(body: bytes) -> str:
    """续期响应体 → 该凭证是否已**不可自愈**，需要重新登录。

    可重试的失败（网络、5xx、限流）返回空串。判错的代价是「一次偶发故障
    永久废掉一条好凭证」，故只认上游点名的那几类。
    """
    text = body.decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in _SESSION_DEAD_MARKERS):
        return "刷新令牌已失效，需重新登录"
    return ""


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码 + body → 错误分类（判定顺序即优先级，勿重排）。"""
    text = body.decode("utf-8", errors="replace")
    lowered = text.lower()
    codes = business_codes(text)

    # ① 会话死亡先于一切：会话已死时任何换号重试都没意义。
    if any(marker in text for marker in _SESSION_DEAD_MARKERS):
        return ErrKind.DEAD
    # ② 额度关键词先于状态码：实测 400 + 中文「积分不足」是余额耗尽，
    #    只按状态码会判成可重试的 client 错误 → 反复重试同一个废号。
    if any(marker in lowered for marker in _CREDIT_MARKERS):
        return ErrKind.CREDIT
    # ③ 请求侧错误：模型不认识 / 参数非法 → 不冷却凭证，直接 400。
    if any(marker in lowered for marker in _REQUEST_MARKERS) or 40300 in codes:
        return ErrKind.INVALID
    if status == 402:
        return ErrKind.CREDIT
    if status == 429:
        return ErrKind.SOFT
    if status in (401, 403):
        return ErrKind.DEAD
    if status in (400, 404, 422):
        return ErrKind.INVALID
    return ErrKind.OTHER


def classify_error_code(code: int | None, message: str = "") -> ErrKind:
    """流内错误帧 → 分类（业务码可能是 int，也可能只藏在 message 文本里）。

    流内错误的 HTTP 状态是 **200**，故状态码分支全部失效，只剩关键词与
    业务码可用。**未命中任何判据时归 `OTHER` 而不是 `none`**：流内错误是
    明确的业务失败（该账号此刻没能服务这个请求），必须让引擎换号重试。
    """
    text = (message or "").lower()
    if any(marker in text for marker in _SESSION_DEAD_MARKERS):
        return ErrKind.DEAD
    if any(marker in text for marker in _CREDIT_MARKERS):
        return ErrKind.CREDIT
    if any(marker in text for marker in _REQUEST_MARKERS) or code == 40300:
        return ErrKind.INVALID
    if code in (402, 40201):
        return ErrKind.CREDIT
    if code == 429:
        return ErrKind.SOFT
    return ErrKind.OTHER


# ---------------------------------------------------------------- SSE 解析


def parse_frame(frame: SSEFrame) -> Event | None:
    """单帧 → 单个代表性事件；`[DONE]` 与无内容帧返回 None。"""
    payload = payload_of(frame, "LobsterAI")
    return None if payload is None else openai_event(
        payload, reasoning_keys=("reasoning_content",), error_event=_error_event,
        usage=_usage)


def parse_all_events(frame: SSEFrame) -> list[Event]:
    """一帧拆成多个事件：同一 chunk 可同时携带正文与 finish_reason。"""
    return openai_events(frame, "LobsterAI",
                         reasoning_keys=("reasoning_content",),
                         error_event=_error_event, usage=_usage)


def _error_event(error: dict[str, Any]) -> Event:
    """流内错误信封（`{"error": {...}}` 或 `{"type":"proxy_error",...}`）。

    帧内 `code` 是**业务码**（40201 额度耗尽 / 40300 未知模型），不是 HTTP
    状态；分类走 `classify_error_code`（关键词 + 业务码两条通道）。
    """
    raw_code = error.get("code")
    code = (raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool)
            else None)
    message = error.get("message")
    if not isinstance(message, str) or not message:
        message = error.get("type") if isinstance(error.get("type"), str) else ""
    return Event(
        kind=EventKind.ERROR,
        error_code=code,
        error_message=message,
        error_kind=classify_error_code(code, message),
    )


def _usage(raw: dict[str, Any]) -> Usage:
    return usage_from_openai(raw)


# ---------------------------------------------------------------- 信封解析


def parse_envelope(raw: bytes | str, label: str) -> Any:
    """上游统一信封 `{code, msg, data}` → `data`（**原样返回**）。

    `data` 的形状随端点而变：模型目录是**数组**，令牌 / 积分 / 签到是
    **对象**。故这里只解信封、不对 `data` 的类型做要求，由调用点按端点
    形状取用（`expect_object` 供需要对象的端点显式校验）。

    三条判定缺一不可（参考实现 `parseLobsteraiEnvelope`）：

    1. 响应体必须是对象；
    2. `code` 必须为 0；
    3. `data` 必须**存在**（可以是数组）。

    第 3 条尤其重要：上游在凭据失效时倾向于返回 `code: 0` 但 `data: null`，
    只看 code 会把这种情况当成成功，随后在解引用时崩在更远的地方。
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation(f"{label} response is not JSON") from error
    if not isinstance(payload, dict):
        raise UpstreamProtocolViolation(f"{label} response is not an object")
    code = payload.get("code")
    if isinstance(code, str) and code.strip().lstrip("-").isdigit():
        code = int(code.strip())
    if code != 0:
        message = payload.get("msg") or payload.get("message") or f"code={code}"
        raise UpstreamProtocolViolation(f"{label} rejected: {message}")
    if "data" not in payload or payload.get("data") is None:
        raise UpstreamProtocolViolation(f"{label} response has no data")
    return payload["data"]


def expect_object(data: Any, label: str) -> dict[str, Any]:
    """信封 `data` → 对象；不是对象即显式失败。"""
    if not isinstance(data, dict):
        raise UpstreamProtocolViolation(f"{label} response data is not an object")
    return data
