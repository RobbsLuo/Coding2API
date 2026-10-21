"""officeAce 上游协议：常量、SSE 解析、错误分类、订阅响应解析。

上游两套域（实测，2026-10-21）：

* **控制面** `agentarts.{region}.myhuaweicloud.com`：`POST /v1/claw/auth/state`
  取 state、`GET /v1/claw/auth/code` 轮询授权码、
  `GET /v1/claw/client-permission-validate` 取模型网关与 app key。
  签名用 `SDK-HMAC-SHA256`（见 `signer.build_v4_headers`）。
* **模型网关** `modelgw-XXXX.officeace.{region}.huaweicloud-agentarts.com`：
  聊天 `/v2/chat/completions`（**标准 OpenAI**），订阅/积分/费率
  `/v1/subscription*`、`/v1/model-token-rates`（**V11 签名**）。

错误分类的口径来自实测（见 docs/officeace-orcaterm-feasibility.md）：

| 上游码 | 含义 | 归属 |
|---|---|---|
| `ModelArts.81004` | 这个模型该账号无权限 | **模型** |
| `ModelArts.81009` | 模型名上游不认 | **模型** |
| `ModelArts.81111/81114` | 该模型 TPM 限流 | 模型（短冷却） |
| `ModelArts.81113` | 请求体过大 | 请求 |
| `APIG.1009` / `AppKey or AppSecret is invalid` | 凭据真坏了 | **账号** |
| `APIG.0303` | app 不存在（把 STS 当 Basic 用） | **账号**（配置错） |

`81004`/`81009` 走 HTTP 403/404，若按状态码直接判 DEAD 会把整条凭据打
死——而它们其实只说明「这个号没这个模型」。故业务码判定必须**先于**状态码。

**限流走伪 200**：华为网关限流时会 HTTP 200 + `data:{"error":...}`，
首帧正常、第二帧才是错误也出现过。解析层因此不能假设「2xx 就是好流」，
错误帧必须照常产出 `EventKind.ERROR` 交给引擎分类。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ...engine.sse import SSEFrame
from ...provider.base import (
    ErrKind,
    Event,
    EventKind,
    UpstreamProtocolViolation,
    Usage,
    business_codes,
)
from ..openai_chunk import first_choice, is_blank_tool_call

# ---------------------------------------------------------------- 域与端点

#: 控制面（OAuth 与开通校验）。region 可在凭证里覆盖。
AGENTARTS_HOST = "https://agentarts.cn-southwest-2.myhuaweicloud.com"
#: 默认模型网关。真实网关由 `client-permission-validate` 的
#: `model_info.model_api_url_base` 下发，此处仅作兜底与测试默认。
DEFAULT_GATEWAY_HOST = (
    "https://modelgw-0004.officeace.cn-southwest-2.huaweicloud-agentarts.com")
#: 华为云 STS（refresh_token 换临时 AK/SK），与 CodeArts 同域。
STS_HOST = "https://sts.cn-north-4.myhuaweicloud.com"
#: 华为云统一登录页（PKCE 授权入口）。
AUTH_HOST = "https://auth.huaweicloud.com"

#: officeAce 的 OAuth client_id。**与 CodeArts 的 `codearts-agent` 不通用**：
#: refresh_token 绑定 client_id，混用回 `STS5.1806 invalid client id`（实测）。
CLIENT_ID = "pdp5_for_agentarts"
#: 签名域（V11 的 CredentialScope 服务名，APIG 固定 apic）。
SIGN_REGION = "cn-southwest-2"
#: 授权回调地址由云端自己承接（`/v1/claw/auth/callback`），本服务不监听。
REDIRECT_PATH = "/v1/claw/auth/callback"

EP_OAUTH_TOKENS = "/v1/oauth2/tokens"
EP_AUTHORIZE = "/authui/v1/oauth2/authorize"
EP_LOGIN_PAGE = "/authui/login.html"
EP_LOGOUT = "/authui/logout"
EP_CLIENT_PERMISSION = "/v1/claw/client-permission-validate"
EP_AUTH_STATE = "/v1/claw/auth/state"
EP_AUTH_CODE = "/v1/claw/auth/code"

EP_SUBSCRIPTION = "/v1/subscription"
EP_SUBSCRIPTION_USAGE = "/v1/subscription/usage"
EP_BONUS_CLAIM = "/v1/subscription/bonus/claim"
EP_TOKEN_RATES = "/v1/model-token-rates"
EP_CHAT = "/v2/chat/completions"
EP_MODELS = "/v2/models"

#: 积分 SKU 标识（套餐额度与奖励额度同名）。
POINTS_SKU = "officeace_points"

# ---------------------------------------------------------------- 错误分类

#: 模型级拒绝：这个号没这个模型 / 上游不认这个名字。**不是**账号问题。
_MODEL_REJECT_MARKERS: tuple[str, ...] = ("81004", "81009")
#: 模型级限流（TPM）。只冷却「凭据 × 模型」，其它模型不受影响。
_MODEL_THROTTLE_MARKERS: tuple[str, ...] = ("81111", "81112", "81114")
#: 凭据级故障：app key 坏了 / 网关分片换了。
_AUTH_DEAD_MARKERS: tuple[str, ...] = (
    "apig.1009",
    "appkey or appsecret is invalid",
    "apig.0303",
)
#: 请求体过大（81113 带 `^C` 前缀）。
_REQUEST_TOO_LARGE_MARKERS: tuple[str, ...] = ("81113",)
#: 参数非法（81001：max_tokens 越界等）→ 请求问题，换号也没用。
_BAD_PARAM_MARKERS: tuple[str, ...] = ("81001",)
#: 积分耗尽（上游明确说余额不足时）。
_CREDIT_MARKERS: tuple[str, ...] = ("insufficient balance", "余额不足", "points exhausted")

#: refresh_token 已不可自愈（一次性票被消费 / 绑定项不符）。
_RELOGIN_MARKERS: tuple[str, ...] = (
    "invalid refresh token",
    "the refresh token has been used",
    "invalid client id",
    "invaliddpopheader",
    "invalid_grant",
    # 华为 STS 一次性票被消费时的另一种写法（实测 CodeArts 同域）
    "sts5.1806",
)

#: 授权码还没就绪（云端 202）：不是错误，继续轮询。
CODE_NOT_READY = 202
#: 轮询间隔（云端建议 2.5s；与 OfficeAce 客户端一致）。
POLL_INTERVAL_SECONDS = 2.5


def relogin_required(body: bytes) -> str:
    """令牌端点响应体 → 该凭证是否已**不可自愈**，需重新登录。

    只认上游点名的那几类；可重试的（网络 / 5xx / 限流）返回空串。判错的
    代价是「一次偶发故障永久废掉一条好凭证」，故宁可漏判不可误判。
    """
    text = body.decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in _RELOGIN_MARKERS):
        return "刷新令牌已失效，需重新登录"
    return ""


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码 + body 业务码分类（判定顺序即优先级）。

    关键点：`81004`/`81009` 走 403/404，但必须先于状态码判定拦下来——
    否则 `status == 403 → DEAD` 会把整条凭据硬禁用，而真实原因只是
    「这个号没这个模型」。
    """
    text = body.decode("utf-8", errors="replace")
    lowered = text.lower()
    codes = business_codes(text)

    if any(m in lowered for m in _MODEL_REJECT_MARKERS):
        return ErrKind.BLOCKED
    if any(m in lowered for m in _MODEL_THROTTLE_MARKERS):
        return ErrKind.MODEL
    if any(m in lowered for m in _REQUEST_TOO_LARGE_MARKERS):
        return ErrKind.REQUEST
    if any(m in lowered for m in _BAD_PARAM_MARKERS):
        return ErrKind.INVALID
    if any(m in lowered for m in _AUTH_DEAD_MARKERS):
        return ErrKind.DEAD
    if any(m in lowered for m in _CREDIT_MARKERS) or 14018 in codes:
        return ErrKind.CREDIT
    if status == 402:
        return ErrKind.CREDIT
    if status == 429:
        return ErrKind.MODEL
    if status in (401, 403):
        return ErrKind.DEAD
    if status in (400, 404, 422):
        return ErrKind.INVALID
    return ErrKind.OTHER


def classify_error_code(code: int | None, message: str = "") -> ErrKind:
    """流内错误 → 分类（错误码可能是 int，也可能只藏在 message 文本里）。"""
    text = (message or "").lower()
    if any(m in text for m in _MODEL_REJECT_MARKERS):
        return ErrKind.BLOCKED
    if any(m in text for m in _MODEL_THROTTLE_MARKERS):
        return ErrKind.MODEL
    if any(m in text for m in _REQUEST_TOO_LARGE_MARKERS):
        return ErrKind.REQUEST
    if any(m in text for m in _BAD_PARAM_MARKERS):
        return ErrKind.INVALID
    if any(m in text for m in _AUTH_DEAD_MARKERS):
        return ErrKind.DEAD
    if any(m in text for m in _CREDIT_MARKERS) or code == 14018:
        return ErrKind.CREDIT
    if code == 402:
        return ErrKind.CREDIT
    if code == 429:
        return ErrKind.MODEL
    if code in (401, 403):
        return ErrKind.DEAD
    if code in (400, 404, 422):
        return ErrKind.INVALID
    return ErrKind.OTHER


# ---------------------------------------------------------------- SSE 解析


def parse_frame(frame: SSEFrame) -> Event | None:
    """单帧 → 代表性事件；`[DONE]` 与无内容帧返回 None。"""
    payload = _payload(frame)
    if payload is None:
        return None
    return _event_from_payload(payload)


def parse_all_events(frame: SSEFrame) -> list[Event]:
    """一帧拆成多个事件：同一 chunk 可同时携带正文与 finish_reason。

    必须基于代表性事件再补 usage/finish，否则「最后一段正文 + finish」
    这种合并帧会丢掉 finish（客户端永远收不到结束信号）。
    """
    payload = _payload(frame)
    if payload is None:
        return []
    events: list[Event] = [e for e in (_event_from_payload(payload),) if e is not None]
    has_usage = any(e.kind is EventKind.USAGE for e in events)
    has_finish = any(e.kind is EventKind.FINISH for e in events)

    usage = payload.get("usage")
    if isinstance(usage, dict) and not has_usage:
        events.append(Event(kind=EventKind.USAGE, usage=_usage(usage)))
    choice = first_choice(payload)
    finish_reason = choice.get("finish_reason") if choice else None
    if isinstance(finish_reason, str) and finish_reason and not has_finish:
        events.append(Event(kind=EventKind.FINISH, finish_reason=finish_reason))
    return events


def _event_from_payload(payload: dict[str, Any]) -> Event | None:
    """已解析 JSON → 代表性事件。

    优先级：tool_calls > 正文 > 思考 > 错误 > usage > finish。
    错误排在正文之后是有意的：伪 200 的第二帧常常「无正文 + error」，
    而带正文的帧不该被错误覆盖。
    """
    choice = first_choice(payload)
    delta = choice.get("delta") if choice else None
    delta = delta if isinstance(delta, dict) else {}
    finish_reason = choice.get("finish_reason") if choice else None

    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list):
        kept = [tc for tc in tool_calls
                if isinstance(tc, dict) and not is_blank_tool_call(tc)]
        if kept:
            return Event(kind=EventKind.TOOL_CALLS, tool_calls=kept)

    content = delta.get("content")
    if isinstance(content, str) and content:
        return Event(kind=EventKind.CONTENT, content=content)

    # 上游推理模型给 `reasoning_content`（OpenAI 兼容惯例）
    reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return Event(kind=EventKind.REASONING, content=reasoning)

    error = payload.get("error")
    if isinstance(error, dict):
        return _error_event(error)
    if isinstance(error, str) and error:
        return _error_event({"message": error})

    usage = payload.get("usage")
    if isinstance(usage, dict):
        return Event(kind=EventKind.USAGE, usage=_usage(usage))

    if isinstance(finish_reason, str) and finish_reason:
        return Event(kind=EventKind.FINISH, finish_reason=finish_reason)
    return None


def _payload(frame: SSEFrame) -> dict[str, Any] | None:
    """帧 → JSON 对象；空帧 / `[DONE]` 返回 None，坏帧显式失败。"""
    if not frame.data:
        return None
    if frame.data.strip() == "[DONE]":
        return None
    try:
        payload = json.loads(frame.data)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation("unparsable officeAce SSE data") from error
    if not isinstance(payload, dict):
        raise UpstreamProtocolViolation("officeAce SSE data is not an object")
    return payload


def _error_event(error: dict[str, Any]) -> Event:
    """流内错误信封 → ERROR 事件。

    `code` 可能是 int，也可能是 `ModelArts.81004` 这种字符串（伪 200 常见
    形态）：两者都要并进参与分类的文本——上游给**int** 业务码时（`81111`
    限流、`81004` 无权限）只看 message 会漏掉分类依据，落成「其它上游失败」，
    白等一次账号级冷却而不是模型级。
    """
    raw_code = error.get("code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    message = error.get("message")
    text = str(message) if message is not None else ""
    if isinstance(raw_code, str) and raw_code:
        # 字符串码（`ModelArts.81004`）：进不了 int 槽位，只用于分类文本
        text = f"{raw_code} {text}".strip()
    hint = text if code is None else f"ModelArts.{code} {text}".strip()
    return Event(
        kind=EventKind.ERROR,
        error_code=code,
        error_message=text,
        error_kind=classify_error_code(code, hint),
    )


def _usage(raw: dict[str, Any]) -> Usage:
    def as_int(key: str) -> int | None:
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    details = raw.get("prompt_tokens_details")
    cached = (details or {}).get("cached_tokens") if isinstance(details, dict) else None
    if not isinstance(cached, int) or isinstance(cached, bool):
        cached = as_int("cached_tokens")
    completion_details = raw.get("completion_tokens_details")
    reasoning = (completion_details or {}).get("reasoning_tokens") \
        if isinstance(completion_details, dict) else None
    if not isinstance(reasoning, int) or isinstance(reasoning, bool):
        reasoning = as_int("reasoning_tokens")
    return Usage(
        input_tokens=as_int("prompt_tokens"),
        output_tokens=as_int("completion_tokens"),
        reasoning_tokens=reasoning,
        cached_tokens=cached,
    )


# ------------------------------------------------------------ 订阅 / 积分

#: 北京时间（上游的 `expired_time` 是 UTC，但「今天」按上游活动口径取 UTC+8）。
BEIJING_OFFSET_SECONDS = 8 * 3600


def _number(value: Any) -> float:
    """宽容取数：字符串数字（上游大量用 `"4000"`）与数字都认，坏值算 0。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def epoch_from_iso(value: Any) -> int:
    """ISO8601（带 `Z` 或偏移）→ epoch 秒；坏值 / 缺失返回 0。"""
    if not isinstance(value, str) or not value.strip():
        return 0
    text = value.strip().replace("Z", "+00:00")
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return 0


def beijing_day(epoch: int) -> str:
    """epoch 秒 → 北京时间的 `YYYY-MM-DD`（签到「今天」的判定键）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(epoch + BEIJING_OFFSET_SECONDS))


def summarize_subscription(payload: Any, *, now: int | None = None) -> dict[str, Any]:
    """`GET /v1/subscription` → `{remaining, total, cycle_end, packages, daily}`。

    口径与官方前端一致：**总额 − 已用**，且**过期条目不计入**。两本来源相加：

    * `skus[].quotas[]` 里 `sku_attr_code == officeace_points` 的套餐积分；
    * `bonus_skus[]` 的奖励积分（新用户福利 + 每日签到）。

    与 CodeArts 不同，这里**积分就是主账**（没有并行的 token 池），故
    `remaining`/`total` 直接就是积分，不挂 `credits_*`。
    """
    moment = int(now if now is not None else time.time())
    data = payload if isinstance(payload, dict) else {}
    skus = data.get("skus")
    skus = skus if isinstance(skus, list) else []
    bonuses = data.get("bonus_skus")
    bonuses = bonuses if isinstance(bonuses, list) else []

    total = 0.0
    used = 0.0
    packages: list[dict[str, Any]] = []
    ladder: list[tuple[int, float]] = []

    for sku in skus:
        if not isinstance(sku, dict):
            continue
        quotas = sku.get("quotas")
        quotas = quotas if isinstance(quotas, list) else []
        for quota in quotas:
            if not isinstance(quota, dict) or quota.get("sku_attr_code") != POINTS_SKU:
                continue
            end = epoch_from_iso(quota.get("expired_time"))
            if end and end <= moment:
                continue                       # 过期额度不计入（官方口径）
            sku_total = _number(quota.get("sku_value"))
            sku_used = _number(quota.get("current_value"))
            total += sku_total
            used += sku_used
            remaining = max(0.0, sku_total - sku_used)
            packages.append({"name": str(sku.get("sku_name") or "套餐积分"),
                             "total": sku_total, "used": sku_used, "end": end})
            if end and remaining > 0:
                ladder.append((end, remaining))

    for bonus in bonuses:
        if not isinstance(bonus, dict):
            continue
        end = epoch_from_iso(bonus.get("expired_time"))
        if end and end <= moment:
            continue
        bonus_total = _number(bonus.get("points"))
        bonus_used = _number(bonus.get("current_value"))
        total += bonus_total
        used += bonus_used
        remaining = max(0.0, bonus_total - bonus_used)
        packages.append({"name": str(bonus.get("activity_name") or "奖励积分"),
                         "total": bonus_total, "used": bonus_used, "end": end})
        if end and remaining > 0:
            ladder.append((end, remaining))

    remaining = max(0.0, total - used)
    cycle_end = min((end for end, _ in ladder), default=0) or None
    return {
        "remaining": remaining,
        "total": total,
        "cycle_end": cycle_end,
        "packages": packages,
        "expiry_ladder": sorted(ladder),
        "daily": _daily_activity(bonuses, moment),
        "subscribe_status": str(data.get("subscribe_status") or ""),
        "domain_id": str(data.get("domain_id") or ""),
    }


def _daily_activity(bonuses: list[Any], now: int) -> dict[str, Any] | None:
    """当日签到活动（`bonus-daily-checkin-*`）→ `{claimed, credit, expires_at}`。

    识别依据是活动本身带 `daily` 资源标记（`cbc_resource_id` 形如
    `bonus:...:daily`）。上游次日会换一条新的（`expired_time` = 当日 23:59:59
    UTC+8），故「今晚之后过期的 daily 条目」只有今天签到过才会存在——
    这也是 `checkin_status` 判断「今日已签」的依据。
    """
    today = beijing_day(now)
    for bonus in bonuses:
        if not isinstance(bonus, dict):
            continue
        resource = str(bonus.get("cbc_resource_id") or "")
        name = str(bonus.get("activity_name") or "")
        if "daily" not in resource.lower() and "签到" not in name and "每日" not in name:
            continue
        end = epoch_from_iso(bonus.get("expired_time"))
        if end and end <= now:
            continue                            # 昨天的签到条目已过期
        effective = epoch_from_iso(bonus.get("effective_time"))
        claimed_today = bool(effective) and beijing_day(effective) == today
        return {
            "activity_id": str(bonus.get("activity_id") or ""),
            "activity_name": name or "每日签到",
            "claimed": claimed_today,
            "credit": max(0.0, _number(bonus.get("points"))
                          - _number(bonus.get("current_value"))),
            "expires_at": end,
        }
    return None


def bonus_remaining(payload: Any) -> dict[str, float]:
    """奖励活动 → `{activity_id: 剩余积分}`（签到前后比对用）。"""
    data = payload if isinstance(payload, dict) else {}
    bonuses = data.get("bonus_skus")
    bonuses = bonuses if isinstance(bonuses, list) else []
    result: dict[str, float] = {}
    for bonus in bonuses:
        if not isinstance(bonus, dict):
            continue
        activity = str(bonus.get("activity_id") or "")
        if not activity:
            continue
        result[activity] = max(0.0, _number(bonus.get("points"))
                               - _number(bonus.get("current_value")))
    return result


# ------------------------------------------------------------ 模型 / 网关

#: 上游 `max_tokens` 硬上限（按模型族前缀，实测值；最长前缀优先）。
#: 只有真打上去才知道各家不同，故集中在此，未列出的按 DEFAULT 处理。
MAX_TOKENS_BY_MODEL: dict[str, int] = {
    "glm-5": 131072,
    "qwen3": 65536,
    "deepseek-v4.1-flash": 393216,
}
DEFAULT_MAX_TOKENS = 393216
#: 推理预算余量：上游把 `max_tokens` 当「推理 + 正文」总预算，9/11 个可用
#: 模型是推理模型，给小了正文直接为空。
REASONING_HEADROOM = 1024
#: 小于该值**原样透传**：这些是「只要一个 token」的精确请求（探活/测通道），
#: 放大后反而拿回来一整句。
TINY_MAX_TOKENS = 16


def max_tokens_cap(model: str) -> int:
    """该模型的上游 `max_tokens` 上限（最长前缀命中，缺省用默认值）。"""
    lowered = (model or "").lower()
    best = ""
    for prefix in MAX_TOKENS_BY_MODEL:
        if lowered.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return MAX_TOKENS_BY_MODEL[best] if best else DEFAULT_MAX_TOKENS


def clamp_max_tokens(value: int, model: str) -> int:
    """客户端要的 `max_tokens` → 实际发上游的值。

    规则（实测，见 docs：上游把 `max_tokens` 当推理+正文总预算）：

    * `< TINY_MAX_TOKENS` → **原样透传**（精确请求，不许放大）；
    * `>= TINY_MAX_TOKENS` → 追加 `REASONING_HEADROOM`；
    * 追加后超过该模型硬上限 → 夹回上限（否则确定性 400 `81001`）。
    """
    if value < TINY_MAX_TOKENS:
        return value
    return min(value + REASONING_HEADROOM, max_tokens_cap(model))


def model_id_from_item(item: dict[str, Any]) -> str:
    """模型条目 → id（`id` 优先，OpenAI 兼容网关两种写法都见过）。"""
    for key in ("id", "model", "model_name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_models(payload: Any) -> list[dict[str, Any]]:
    """`GET /v2/models` → 条目列表；结构非法显式失败。"""
    data = payload if isinstance(payload, dict) else {}
    items = data.get("data")
    if items is None:
        items = data.get("models")
    if not isinstance(items, list):
        raise UpstreamProtocolViolation("models response missing data list")
    return [item for item in items if isinstance(item, dict) and model_id_from_item(item)]


def parse_rates(payload: Any) -> dict[str, float]:
    """`GET /v1/model-token-rates` → `{模型名: cost_coefficient}`。

    上游名带过尾随换行（实测 `"kimi-k3\\n"`），故统一 strip 后小写成键。
    """
    data = payload if isinstance(payload, dict) else {}
    models = data.get("models")
    models = models if isinstance(models, list) else []
    rates: dict[str, float] = {}
    for item in models:
        if not isinstance(item, dict):
            continue
        name = item.get("model_name")
        if not isinstance(name, str) or not name.strip():
            continue
        coefficient = item.get("cost_coefficient")
        if isinstance(coefficient, bool) or not isinstance(coefficient, (int, float)):
            continue
        rates[name.strip().lower()] = float(coefficient)
    return rates


def parse_model_info(payload: Any) -> dict[str, str]:
    """`client-permission-validate` → `{account_id, principal_id, app_key, app_secret, base_url}`。

    未开通的账号这里 `subscription` 为 `null`（实测三账号全如此），此时
    `app_key` 为空——调用方据此给出「需要先在客户端兑换邀请码」的明确提示，
    而不是让用户去猜「为什么登上了却用不了」。
    """
    data = payload if isinstance(payload, dict) else {}
    subscription = data.get("subscription")
    subscription = subscription if isinstance(subscription, dict) else {}
    model_info: dict[str, Any] = {}
    for candidate in (data.get("model_info"), subscription.get("model_info")):
        if isinstance(candidate, dict) and candidate:
            model_info = candidate
            break
    auth_info = model_info.get("model_auth_info")
    auth_info = auth_info if isinstance(auth_info, dict) else {}
    base = str(model_info.get("model_api_url_base") or "").strip()
    base = base.rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    return {
        "account_id": str(data.get("account_id") or ""),
        "principal_id": str(data.get("principal_id") or ""),
        "user_name": _user_name_of(data),
        "app_key": str(auth_info.get("model_app_key") or ""),
        "app_secret": str(auth_info.get("model_app_secret") or ""),
        "base_url": base,
    }


def _user_name_of(data: dict[str, Any]) -> str:
    """从 `principal_urn` 末尾取用户名：`iam::<account>:user:<name>`。

    该接口**没有**独立的用户名字段（实测 2026-10-21），用户名只出现在 URN
    里。取不到就留空——调用方会退到账号 ID 当昵称，不能让登录因此失败。
    """
    explicit = str(data.get("user_name") or data.get("userName") or "").strip()
    if explicit:
        return explicit
    urn = str(data.get("principal_urn") or "")
    marker = ":user:"
    if marker not in urn:
        return ""
    return urn.split(marker, 1)[1].strip()


def build_authorize_url(*, auth_base: str, client_id: str, state: str,
                        code_challenge: str, redirect_uri: str) -> str:
    """拼华为云授权地址（参数集与 OfficeAce 客户端逐项对齐，勿增删）。

    外层 `login.html?service=` 是必要的：直接开 `authorize` 在没有会话时
    只会 302 到登录页而丢掉返回地址，套一层登录页才会登完回到授权流程。
    """
    from urllib.parse import quote, urlencode

    query = urlencode({
        "client_id": client_id,
        "code_challenge": code_challenge,
        "code_challenge_method": "SHA-256",
        "state": state,
        "scope": "openid",
        "redirect_uri": redirect_uri,
        "response_type": "code",
    })
    inner = f"{auth_base}{EP_AUTHORIZE}?{query}"
    return f"{auth_base}{EP_LOGIN_PAGE}?service={quote(inner, safe='')}"


def build_switch_account_url(*, auth_base: str, authorize_url: str) -> str:
    """「换一个账号」：先登出华为云，再回授权流程。

    不加这层时，浏览器里只要还有华为云会话，云端会**直接放行**、拿当前
    账号再登一遍——用户点「新增账号」却看不到登录页。登出地址会把浏览器
    送回 `authorize_url`（`service` 参数），故授权链路完全不变。
    """
    from urllib.parse import quote

    return f"{auth_base}{EP_LOGOUT}?service={quote(authorize_url, safe='')}"
