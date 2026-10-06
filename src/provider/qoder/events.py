"""Qoder（阿里，COSY 私有协议）区域常量、错误分类与信封 SSE 解包。

Qoder 与 CodeBuddy / TRAE 的关键差异在这一层：

* **双区域**：国内 `openapi.qoder.com.cn` / `gateway.qoder.com.cn`，国际
  `openapi.qoder.sh` / `api1.qoder.sh`（官方客户端还有 api2/api3 故障切换）。
  区域由凭证域名推断，推理主机候选见 `gateway_candidates`。
* **信封式 SSE**：上游不是标准 OpenAI 流，每行是一个外层信封
  `data:{"headers":…,"body":"<内层 OpenAI chunk>","statusCodeValue":200}`；
  `body=="[DONE]"` 结束；`statusCodeValue != 200` 是**流内错误**（HTTP 可能
  仍是 200）。解包在 `decode_envelope` / `parse_inner_chunk`。
* **空噪声 delta**：上游会给 delta 塞 `extra_fields` / `refusal` /
  空 `reasoning_content` / 空 `tool_calls` 等占位，`clean_delta` 统一剔除，
  避免把「什么都没说」的帧当成内容转发给客户端。

错误分类沿用本项目中立层（`provider.base.ErrKind`）语义；Qoder 没有观测到
CB/TRAE 那套 `1005/6004/11102` 业务码，信封里的 `statusCodeValue` 本身就是
HTTP 状态语义，故按状态码分类。唯一例外：上游会把**自身节点执行故障**也包成
400（`[FAIL]node:… msg:Execution failed`），这类按 `NODE_FAILURE_MARKERS` 识别后
归模型级瞬时故障（`ErrKind.MODEL`），见 `classify_error_code`。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ...engine.sse import SSEFrame
from ...provider.base import (
    CheckinStatus,
    ErrKind,
    Event,
    EventKind,
    Model,
    Quota,
    UpstreamProtocolViolation,
    Usage,
)
from ..openai_chunk import first_choice
from ..token_expiry import normalize_epoch

# ---------------------------------------------------------------------------
# 区域常量（逆向自官方桌面/CLI 客户端；签名只覆盖 path，切主机不影响校验）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RealmConfig:
    """一个区域的上游地址与登录参数。"""

    name: str
    openapi: str
    gateway: str
    website: str
    client_id: str
    redirect_uri: str
    domain: str
    user_agent: str
    gateway_fallbacks: tuple[str, ...] = ()
    send_client_id: bool = True
    send_redirect_uri: bool = False
    nonce_dashed: bool = False


REALM_CONFIGS: dict[str, RealmConfig] = {
    "cn": RealmConfig(
        name="国内版 (China)",
        openapi="https://openapi.qoder.com.cn",
        gateway="https://gateway.qoder.com.cn",
        # website/client_id 取自官方 CN CLI @qodercn-ai/qoderclicn@1.1.32（issue #3
        # 取证：旧值 qoder.com.cn + 旧 client_id 会被授权页判「参数无效」）；
        # domain 保持 qoder.com.cn——它用于查找官方客户端 machine_id 落盘文件，勿改。
        website="https://qoder.cn",
        client_id="e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        redirect_uri="qoder-work-cn://",
        domain="qoder.com.cn",
        user_agent="QoderWork/1.1.64",
        # 官方 CN CLI 的授权链接不带 redirect_uri；带上会被授权页拒绝。
        send_redirect_uri=False,
        nonce_dashed=True,
    ),
    "intl": RealmConfig(
        name="国际版 (Global)",
        openapi="https://openapi.qoder.sh",
        gateway="https://api1.qoder.sh",
        website="https://qoder.com",
        client_id="e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        redirect_uri="qoder://aicoding.aicoding-agent/login-success",
        domain="qoder.com",
        user_agent="Qoder/1.1.64",
        gateway_fallbacks=("https://api2.qoder.sh", "https://api3.qoder.sh"),
        send_redirect_uri=False,
        nonce_dashed=False,
    ),
}

DEFAULT_REALM = "cn"

# openapi 业务端点（纯 Bearer，无 COSY 签名）
EP_DEVICE_POLL = "/api/v1/deviceToken/poll"
EP_DEVICE_REFRESH = "/api/v1/deviceToken/refresh"
EP_USERINFO = "/api/v1/userinfo"
EP_QUOTA = "/api/v2/quota/usage"
EP_PLAN = "/api/v2/user/plan"
EP_CHECKIN_STATUS = "/sash/api/v1/me/daily-check-in/status"
EP_CHECKIN_CLAIM = "/sash/api/v1/me/daily-check-in/claim"
# 活动制签到（2026-10 起）：上游把每日签到改造成通用 campaign，旧的
# daily-check-in 接口在当前国内版返回 `status: DISABLED`（等于永远不发奖）。
# 查询/领取都必须带 `Cosy-ClientType: 10`，否则上游**静默**返回空 campaign 列表
# （HTTP 200 `{"campaigns":[]}`，实测），看似「活动未开放」实则被降级。
EP_CAMPAIGNS = "/sash/api/v1/me/campaigns"
EP_CAMPAIGN_CLAIM = "/sash/api/v1/me/campaigns/{campaign_id}/claim"
COSY_CLIENT_TYPE = "10"

# 活动制签到的语义标记（实测国内版 2026-10）：
# `actionType` 区分活动种类（`CLAIM_BENEFIT` 才是可领奖励；`VIEW_DETAILS` 等
# 只是展示位，claimStatus 也会是 CLAIMABLE，必须过滤掉，否则会去领错活动）。
# `claimStatus` 为 CLAIMABLE/CLAIMED；`BLOCKED` + `SAME_PERSON_ALREADY_CLAIMED`
# 表示同一自然人（实名的不同账号）已领过，本账号今日无法再领。
ACTION_CLAIM_BENEFIT = "CLAIM_BENEFIT"
CLAIM_STATUS_CLAIMABLE = "CLAIMABLE"
CLAIM_STATUS_CLAIMED = "CLAIMED"
CLAIM_STATUS_BLOCKED = "BLOCKED"
CLAIM_BLOCKED_SAME_PERSON = "SAME_PERSON_ALREADY_CLAIMED"
CHECKIN_ACTIVITY_NAME = "Qoder 每日签到"

# 推理网关端点（COSY 签名；path 部分进签名，query 不进）
EP_CHAT_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
EP_CHAT = EP_CHAT_PATH + "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
EP_MODELS = "/algo/api/v2/model/list?Encode=1"

# 模型清单接口的**签名 body**：服务端校验签名与请求体一致，必须用同一个
# `qoder_encode("")` 参与签名；方法必须是 GET——带头 POST/PUT 会 400，不带头
# 裸 GET 会 403。
MODELS_SIGN_PLAIN = b""

CLIENT_UA = "Go-http-client/2.0"
DEFAULT_USER_TYPE = "personal_professional_trial"

# 上游主动吊销离线会话的标记：命中则刷新无意义，需要重新登录。
SESSION_DEAD_MARKERS = ("TOKEN_EXPIRE", "12153", "Offline user session not found")

# 上游把**自身节点故障**包装成 400 的标记。实测（2026-09-30）免费模型 qfmodel
# 被路由到 `oa_qwen-plus-main` 节点后持续返回
# `{"code":"400","message":"[FAIL]node:… msg:Execution failed: null"}`（HTTP 与
# 信封 statusCodeValue 都是 400）。这与「模型不存在 / 请求无效」的 400 语义
# 完全不同：换凭证不解决问题但会自愈，必须归为模型级瞬时冷却而非 INVALID。
NODE_FAILURE_MARKERS = ("[FAIL]node:", "Execution failed")

# 信封内层 delta 的空占位键（只在取值为空/假时才剔除，非空值一律保留）。
NOISE_KEYS = ("extra_fields", "refusal", "reasoning_content")



def get_realm_config(realm: str) -> RealmConfig:
    """未知区域回落国内版（宁可打错区域也不崩）。"""
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS[DEFAULT_REALM]


def detect_realm_from_domain(domain: str) -> str:
    """按域名推断区域：`*.qoder.sh` / `qoder.com`（非 `.com.cn`）为国际版。"""
    value = str(domain or "").lower()
    if "qoder.sh" in value:
        return "intl"
    if "qoder.com" in value and "qoder.com.cn" not in value:
        return "intl"
    return "cn"


def gateway_candidates(realm: str) -> list[str]:
    """该区域的推理主机候选（主选 + 官方故障切换域名，去重保序）。"""
    config = get_realm_config(realm)
    hosts = [config.gateway]
    for host in config.gateway_fallbacks:
        if host not in hosts:
            hosts.append(host)
    return hosts


def is_session_dead(text: str) -> bool:
    """响应体是否带「会话已失效」标记（TOKEN_EXPIRE / 12153 / Offline user session）。"""
    return any(marker in text for marker in SESSION_DEAD_MARKERS)


def is_node_failure(text: str) -> bool:
    """响应体是否为「上游节点故障」包装的 400（见 `NODE_FAILURE_MARKERS`）。"""
    return any(marker in text for marker in NODE_FAILURE_MARKERS)


def classify_error_code(code: int | None, body: bytes | str = b"") -> ErrKind:
    """状态/信封码 → ErrKind（401/403 的会话失效细节由 `classify_status` 补全）。

    * 402 → 余额不足（等签到恢复）
    * 429 → 模型级限流（上游频控点名具体模型，换模型立即可用）
    * 418/500/502/503/504 → SOFT：上游把自身故障包装成 418/5xx，属瞬时类，
      短冷却换号即可，不该累计成「连续 3 次 → 10m」
    * 400/404/422 → INVALID：请求/模型无效，换凭证没用，跳过该渠道
    * 400 且报文命中 `NODE_FAILURE_MARKERS` → MODEL：上游节点执行失败（如
      qfmodel 的 `[FAIL]node:… Execution failed`），属**模型级瞬时**故障，
      只冷却该模型、换模型立即可用，不能当成「模型不存在」直接 400
    """
    if code == 402:
        return ErrKind.CREDIT
    if code in (401, 403):
        return ErrKind.DEAD
    if code == 429:
        return ErrKind.MODEL
    if code in (418, 500, 502, 503, 504):
        return ErrKind.SOFT
    if code in (400, 404, 422):
        text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
        return ErrKind.MODEL if is_node_failure(text) else ErrKind.INVALID
    return ErrKind.OTHER


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码分类。

    401/403 只有**明确带会话失效标记**时才归 DEAD（硬禁用）；否则按 SOFT
    短冷却轮换——实测 Qoder 的 401/403 多数是上游瞬时/风控抖动，直接硬禁用
    会把可自愈的凭证打进冷宫。
    """
    if status in (401, 403) and not is_session_dead(body.decode("utf-8", errors="replace")):
        return ErrKind.SOFT
    return classify_error_code(status, body)


# ---------------------------------------------------------------------------
# 信封式 SSE 解包
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Envelope:
    """外层信封：`statusCodeValue`（缺省按 200）+ 内层 `body` 字符串。"""

    status: int
    body: str


def _to_status(value: Any) -> int:
    """信封 statusCodeValue → int（可能是 int 或数字字符串；异常回 502）。"""
    if value is None:
        return 200
    if isinstance(value, bool):
        return 502
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 502
    return 502


def decode_envelope(frame: SSEFrame) -> Envelope | None:
    """单帧 → Envelope；空帧/无 body 的正常心跳返回 None。

    顶层 `[DONE]`（部分网关不带信封直接发）也归一成 `Envelope(200, "[DONE]")`，
    由调用方负责收尾，避免走到 JSON 解析报协议违规。
    """
    data = frame.data.strip()
    if not data:
        return None
    if data == "[DONE]":
        return Envelope(status=200, body="[DONE]")
    try:
        outer = json.loads(data)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation("unparsable Qoder SSE envelope") from error
    if not isinstance(outer, dict):
        raise UpstreamProtocolViolation("Qoder SSE envelope is not an object")
    status = _to_status(outer.get("statusCodeValue"))
    body = outer.get("body")
    if not isinstance(body, str):
        if status == 200:
            return None
        return Envelope(status=status, body=json.dumps(outer, ensure_ascii=False))
    return Envelope(status=status, body=body)


def _load_body(body: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise UpstreamProtocolViolation("unparsable Qoder inner chunk") from error
    if not isinstance(payload, dict):
        raise UpstreamProtocolViolation("Qoder inner chunk is not an object")
    return payload


def clean_delta(delta: dict[str, Any]) -> dict[str, Any]:
    """剔除 delta 里的空噪声字段（只删假值，非空值一律保留）。

    `reasoning_content` 是真实思考内容（DeepSeek 族多轮一致性依赖它），
    只有在为空时才删——它不是无条件噪声。
    """
    cleaned = dict(delta)
    for key in NOISE_KEYS:
        if key in cleaned and not cleaned[key]:
            cleaned.pop(key)
    calls = cleaned.get("tool_calls")
    if isinstance(calls, list) and not calls:
        cleaned.pop("tool_calls")
    function_call = cleaned.get("function_call")
    if function_call is not None and _is_blank_function_call(function_call):
        cleaned.pop("function_call")
    return cleaned


def _is_blank_function_call(function_call: Any) -> bool:
    """无 name 且 arguments 为空的噪声调用（有实际参数的续片必须保留）。"""
    if not isinstance(function_call, dict):
        return not function_call
    if str(function_call.get("name") or "").strip():
        return False
    return function_call.get("arguments") in (None, "", "", {})


def _is_blank_tool_call(call: dict[str, Any]) -> bool:
    function = call.get("function")
    return _is_blank_function_call(function if isinstance(function, dict) else function)


def _tool_calls(delta: dict[str, Any]) -> list[dict[str, Any]]:
    """delta 里的工具调用：OpenAI `tool_calls` 数组 + 旧式单数 `function_call`。"""
    calls: list[dict[str, Any]] = []
    raw = delta.get("tool_calls")
    if isinstance(raw, list):
        calls.extend(call for call in raw
                     if isinstance(call, dict) and not _is_blank_tool_call(call))
    function_call = delta.get("function_call")
    if isinstance(function_call, dict) and not _is_blank_function_call(function_call):
        calls.append({"type": "function", "function": function_call})
    return calls


def parse_inner_chunk(body: str) -> list[Event]:
    """内层 OpenAI chunk → 中立事件（一帧可同时给正文/思考/工具/usage/finish）。"""
    payload = _load_body(body)
    events: list[Event] = []
    choice = first_choice(payload)
    delta: dict[str, Any] = {}
    finish_reason: str | None = None
    if choice is not None:
        raw_delta = choice.get("delta")
        delta = clean_delta(raw_delta if isinstance(raw_delta, dict) else {})
        raw_finish = choice.get("finish_reason")
        finish_reason = raw_finish if isinstance(raw_finish, str) and raw_finish else None

    calls = _tool_calls(delta)
    if calls:
        events.append(Event(kind=EventKind.TOOL_CALLS, tool_calls=calls))
    content = delta.get("content")
    if isinstance(content, str) and content:
        events.append(Event(kind=EventKind.CONTENT, content=content))
    reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        events.append(Event(kind=EventKind.REASONING, content=reasoning))
    usage = payload.get("usage")
    if isinstance(usage, dict):
        events.append(Event(kind=EventKind.USAGE, usage=_usage(usage)))
    if finish_reason is not None:
        events.append(Event(kind=EventKind.FINISH, finish_reason=finish_reason))
    return events


def _usage(raw: dict[str, Any]) -> Usage:
    def as_int(key: str) -> int | None:
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    details = raw.get("prompt_tokens_details")
    cached = (details or {}).get("cached_tokens") if isinstance(details, dict) else None
    if not isinstance(cached, int) or isinstance(cached, bool):
        cached = as_int("cached_tokens")
    return Usage(
        input_tokens=as_int("prompt_tokens"),
        output_tokens=as_int("completion_tokens"),
        reasoning_tokens=as_int("reasoning_tokens"),
        cached_tokens=cached,
    )


# ---------------------------------------------------------------------------
# 模型清单 / 额度 / 签到解析
# ---------------------------------------------------------------------------


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _opt_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(
        value, bool) else None


def _opt_epoch(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    epoch = normalize_epoch(int(value))
    return epoch if epoch > 0 else None


def parse_models(payload: dict[str, Any]) -> list[Model]:
    """模型清单 → 中立 Model：`payload["chat"]` 每项 `key`/`display_name`。

    元数据（视觉/工具/推理/上下文/倍率）上游给了就透传，缺失留 None（不编造）。
    `price_factor` 即官方文档的「Credit 消耗倍率」，与全站 `credit_rate` 同义
    （docs.qoder.com/zh/cli/model：「表中倍率来自当前服务端模型列表的
    price_factor」），故直接映射——免费模型上游给 0.0，正好显示「免费」。
    """
    chat = payload.get("chat")
    if not isinstance(chat, list):
        raise UpstreamProtocolViolation("models response missing chat list")
    models: list[Model] = []
    for item in chat:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            continue
        display = item.get("display_name")
        models.append(Model(
            id=key,
            name=display if isinstance(display, str) else "",
            credit_rate=_opt_float(item.get("price_factor")),
            max_input_tokens=_opt_int(item.get("max_input_tokens")),
            supports_images=_opt_bool(item.get("is_vl")),
            supports_tool_call=_opt_bool(item.get("supportsToolCall")),
            supports_reasoning=_opt_bool(item.get("is_reasoning")),
        ))
    if not models:
        raise UpstreamProtocolViolation("models api returned no usable models")
    return models


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(source: dict[str, Any], key: str) -> float:
    value = source.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def parse_quota(payload: dict[str, Any], *, now: int) -> Quota:
    """`/api/v2/quota/usage` → Quota：基础额度 + 赠送/签到额度求和。

    明细包只作展示（各自独立到期，汇总数字看不出是哪些包），`cycle_end`
    取 `expiresAt`（毫秒自动归一）。
    """
    base = _obj(payload.get("userQuota"))
    addon = _obj(payload.get("addOnQuota"))
    remaining = _num(base, "remaining") + _num(addon, "remaining")
    total = _num(base, "total") + _num(addon, "total")
    end = _opt_epoch(payload.get("expiresAt"))
    packages = [
        {"name": "基础额度", "total": _num(base, "total"),
         "used": _num(base, "used"), "end": end},
        {"name": "赠送额度", "total": _num(addon, "total"),
         "used": _num(addon, "used"), "end": end},
    ]
    return Quota(remaining=max(0.0, remaining), total=total, cycle_end=end,
                 packages=packages, probed_at=now)


def _claimed_today(value: Any, now: int) -> bool:
    """lastClaimedAt（epoch，毫秒自动归一）是否落在服务器本地「今天」。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    epoch = normalize_epoch(int(value))
    if epoch <= 0:
        return False
    return (time.strftime("%Y-%m-%d", time.localtime(epoch))
            == time.strftime("%Y-%m-%d", time.localtime(now)))


def checkin_status_from(payload: dict[str, Any], *, now: int) -> CheckinStatus:
    """签到状态接口 → 中立 CheckinStatus（字段缺失一律按默认，不报错）。

    `today_checked_in` 必须同时满足 status==CLAIMED **且** lastClaimedAt 是
    今天：status 会停留在 CLAIMED，单看它会把昨天签过的号误判成今天已签。

    旧协议回退用。当前国内版此接口返回 `status: DISABLED`（活动已迁移，
    见 `checkin_status_from_campaigns`），故只在活动制接口不可用时才走这里。
    """
    status = str(payload.get("status") or "")
    return CheckinStatus(
        active=status in ("CLAIMABLE", "CLAIMED"),
        today_checked_in=status == "CLAIMED" and _claimed_today(
            payload.get("lastClaimedAt"), now),
        streak_days=_opt_int(payload.get("currentStreakDays")),
        today_credit=_opt_float(payload.get("rewardCredits")),
        total_credits=_opt_float(payload.get("totalRewardCredits")),
        activity_name=CHECKIN_ACTIVITY_NAME,
    )


def checkin_campaign(payload: dict[str, Any]) -> dict[str, Any] | None:
    """`GET /me/campaigns` → 签到活动的可领项（无则 None）。

    只认 `actionType == CLAIM_BENEFIT` 且 `claimStatus == CLAIMABLE` 的活动：
    `VIEW_DETAILS` 之类展示位 claimStatus 也是 CLAIMABLE，不筛 actionType 会去
    领错活动。`campaignId` 必须是非空字符串，否则无法构造 claim URL。
    """
    campaigns = payload.get("campaigns")
    if not isinstance(campaigns, list):
        return None
    for item in campaigns:
        if not isinstance(item, dict):
            continue
        if str(item.get("actionType") or "") != ACTION_CLAIM_BENEFIT:
            continue
        if str(item.get("claimStatus") or "") != CLAIM_STATUS_CLAIMABLE:
            continue
        campaign_id = item.get("campaignId")
        if isinstance(campaign_id, str) and campaign_id:
            return item
    return None


def checkin_status_from_campaigns(payload: dict[str, Any]) -> CheckinStatus:
    """活动制签到状态 → 中立 CheckinStatus。

    新协议**不提供**连续天数，`streak_days` 恒为 None（前端据此不显示），
    不编造 0 或沿用旧接口的残留值。

    `today_checked_in`：列表里存在签到活动且 `claimStatus == CLAIMED` 即当今日
    已领。新协议的 CLAIMED 只在**当日**窗口内出现（campaignId/startAt/endAt
    每日轮换，实测次日换新 id 并回到 CLAIMABLE），因此不需要像旧接口那样再按
    `lastClaimedAt` 比对日期。若同时存在 CLAIMABLE 的签到活动，说明今日可领，
    即使另有历史 CLAIMED 项也不判已签。
    """
    campaigns = payload.get("campaigns")
    items = [item for item in campaigns if isinstance(item, dict)] \
        if isinstance(campaigns, list) else []
    checkin_items = [item for item in items
                     if str(item.get("actionType") or "") == ACTION_CLAIM_BENEFIT]
    if not checkin_items:
        # 没有签到类活动：可能未登录/未开放。empty 列表与 campaigns 缺失同义，
        # `active=False` 会被 CheckinTask 当成「不重试」（见 client.checkin）。
        return CheckinStatus(active=False, activity_name=CHECKIN_ACTIVITY_NAME)
    claimed = any(str(item.get("claimStatus") or "") == CLAIM_STATUS_CLAIMED
                  for item in checkin_items)
    claimable = any(str(item.get("claimStatus") or "") == CLAIM_STATUS_CLAIMABLE
                    for item in checkin_items)
    credit = next((_campaign_amount(item) for item in checkin_items
                   if _campaign_amount(item) is not None), None)
    return CheckinStatus(
        active=True,
        today_checked_in=claimed and not claimable,
        streak_days=None,
        today_credit=credit,
        activity_name=CHECKIN_ACTIVITY_NAME,
    )


def _campaign_amount(item: dict[str, Any]) -> float | None:
    """活动 `benefit.amount`（CREDITS 类）→ float；非 CREDITS/非数值返回 None。"""
    return _benefit_credit(_obj(item.get("benefit")))


def campaign_claim_credit(payload: dict[str, Any]) -> float | None:
    """claim 响应 → 发放积分数（`benefit.amount`）；非 CREDITS/缺失返回 None。"""
    return _benefit_credit(_obj(payload.get("benefit")))


def _benefit_credit(benefit: dict[str, Any]) -> float | None:
    """`benefit` → 积分数量。

    `kind` 缺失时按 CREDITS 处理（claim 响应的 `benefit` 实测只含 `amount`，
    不带 `kind`）；`kind` 明确为非 CREDITS 时返回 None，不把其它奖励当积分。
    """
    kind = benefit.get("kind")
    if isinstance(kind, str) and kind and kind != "CREDITS":
        return None
    return _opt_float(benefit.get("amount"))


def claim_already_done(payload: dict[str, Any]) -> bool:
    """claim 响应是否表示「今日已领」。

    * `replayed: true`：重复领取（幂等重放），等价已领；
    * `status == BLOCKED` 且 `failureCode == SAME_PERSON_ALREADY_CLAIMED`：
      同一自然人（实名下不同账号）已领，本账号今日无法再领——归一为已领而
      非失败，否则 CheckinTask 每 10 分钟重试一次、永远失败。
    """
    if payload.get("replayed") is True:
        return True
    if str(payload.get("status") or "") == CLAIM_STATUS_BLOCKED:
        return str(payload.get("failureCode") or "") == CLAIM_BLOCKED_SAME_PERSON
    return False
