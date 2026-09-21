"""TRAE SOLO 上游客户端：payload 改写、SSE 流、凭证解析、模型/额度查询。

双 httpx 客户端（T-Q4）：流式无总超时防长流截断；短请求 30s 总超时防悬挂。
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...engine.sse import iter_frames
from ...provider import base
from ...provider.base import (
    AuthSession,
    CheckinResult,
    ErrKind,
    Event,
    Model,
    Quota,
)
from ...provider.token_expiry import normalize_epoch
from . import events as trae_events
from .callback import (
    CallbackInfo,
    build_login_url,
    credential_from_callback,
    new_machine_identity,
    parse_callback_url,
)
from .credential import TraeCredential, new_checkin_device_id, parse_credential
from .events import (
    AGENT_HOST,
    APP_ID,
    CLIENT_ID,
    DEVICE_BRAND,
    EP_CHAT,
    EP_EXCHANGE,
    EP_MODELS,
    EP_USER_INFO,
    FUNCTION,
    IDE_VERSION,
    IDE_VERSION_CODE,
    OAUTH_HOST,
    OS_VERSION,
    UG_HOST,
    UpstreamProtocolViolation,
)

# 9074 时一轮内的尝试次数（每次换一个全新设备号）。不做更多：9074 无法在本轮内
# 穷尽解决，而后台任务每 10 分钟一轮，与上游的分钟级退避窗口自然错开。
CHECKIN_ATTEMPTS = 2
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
SHORT_TIMEOUT = httpx.Timeout(30.0)

# SOLO 静态模型表（继承 trae2api-web；动态拉取失败时回退）
STATIC_MODELS: tuple[str, ...] = (
    "Doubao-Seed-2.1-Pro", "seed-code-pro-0430", "Doubao-Seed-2.1-Turbo",
    "Doubao-Seed-2.0-Code", "DeepSeek-V4-Flash-Official", "browser_use_subagent",
    "glm-5.2", "glm-5-turbo", "glm-5", "DeepSeek-V4-Pro", "DeepSeek-V4-Flash",
    "kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "minimax-m3", "qwen-3.7-plus",
    "sagitta", "aquila", "custom_model_gemini", "custom_model_placeholder",
    "custom_model_1M_text", "custom_model_1M", "custom_model_kimi",
    "custom_model_claude", "custom_model_gpt-5", "custom_model_no-fc",
    "custom_model_deepseek_chat", "custom_model_deepseek_reasoner",
    "custom_model_deepseek_v4", "explore_sub_agent_v13", "explore_sub_agent_v2",
    "summary",
)


def prepare_body(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """OpenAI 请求体 → SOLO llm_utils_chat 请求体。

    - messages.content 字符串 → [{"type":"text","text":...}]
    - stream 强制 true（非流式由本服务聚合）
    - model → config_name + model；function 固定 solo_work_lite
    - tools/tool_choice 归一化
    """
    body: dict[str, Any] = dict(payload)
    body["stream"] = True
    body["function"] = FUNCTION
    body["config_name"] = model
    body["model"] = model

    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            # OpenAI developer 角色（PI 等客户端对 reasoning 模型使用）
            # TRAE 上游不认：静默返回空流（3003）→ 归一为 system
            if isinstance(message, dict) and message.get("role") == "developer":
                message["role"] = "system"
    if isinstance(messages, list):
        rewritten: list[Any] = []
        for message in messages:
            if not isinstance(message, dict):
                rewritten.append(message)
                continue
            item = dict(message)
            content = item.get("content")
            if isinstance(content, str):
                item["content"] = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                pass  # 已是数组 → 透传
            tool_calls = item.get("tool_calls")
            if isinstance(tool_calls, list):
                kept = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function")
                    if not isinstance(fn, dict):
                        continue
                    # OpenAI function{name,arguments} → SOLO function_call；
                    # 上游要求 FunctionCall.Name 必填，无 name 的剔除
                    if not str(fn.get("name") or "").strip():
                        continue
                    tc = dict(tc)
                    tc["function_call"] = fn
                    del tc["function"]
                    kept.append(tc)
                if kept:
                    item["tool_calls"] = kept
                else:
                    item.pop("tool_calls", None)
                    # 全部 tool_call 被剔（如历史中的空 name 脏数据）：
                    # content 为空的 assistant 占位消息也一并丢弃
                    if item.get("content") is None:
                        continue
            rewritten.append(item)
        body["messages"] = rewritten

    _normalize_tools(body)
    _drop_orphan_tool_results(body)
    return body


def _drop_orphan_tool_results(body: dict[str, Any]) -> None:
    """删除引用了已剔除 tool_call 的悬空 role=tool 消息（TRAE 空流对策）。"""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    known_call_ids = {
        tc.get("id")
        for m in messages if isinstance(m, dict) and m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)
    }
    body["messages"] = [
        m for m in messages
        if not (isinstance(m, dict) and m.get("role") == "tool"
                and m.get("tool_call_id") not in known_call_ids)
    ]


def _normalize_tools(body: dict[str, Any]) -> None:
    """tool_choice: "none" 删 tools；function 对象 → name 字符串。"""
    choice = body.get("tool_choice")
    if choice == "none" or (isinstance(choice, dict) and choice.get("type") == "none"):
        body.pop("tool_choice", None)
        body.pop("tools", None)
        body.pop("functions", None)
        return
    if isinstance(choice, dict):
        function = choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if choice.get("type") == "function" and isinstance(name, str) and name:
            body["tool_choice"] = name
        else:
            # 结构无法无损映射（缺 name 的 function、未知 type）→ 删除而非透传
            body.pop("tool_choice", None)
    if body.get("tools") is None:
        body.pop("tools", None)
        return
    _stringify_tool_parameters(body)


def _stringify_tool_parameters(body: dict[str, Any]) -> None:
    """OpenAI 的 function.parameters 是 object，TRAE（Go）要求 JSON 字符串。

    实测不转换 → 400 code=4001 "cannot unmarshal object into Go struct
    field FunctionDefinition.tools.function.parameters of type string"。
    """
    tools = body.get("tools")
    if not isinstance(tools, list):
        return
    normalized: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool = dict(tool)
        function = tool.get("function")
        if isinstance(function, dict):
            function = dict(function)
            params = function.get("parameters")
            if isinstance(params, (dict, list)):
                function["parameters"] = json.dumps(params, ensure_ascii=False)
            elif params is None:
                function["parameters"] = "{}"
            tool["function"] = function
        normalized.append(tool)
    body["tools"] = normalized


def solo_headers(credential: TraeCredential, *, stream: bool = True) -> dict[str, str]:
    """聊天/模型端点头（agent host）。逐项对照原实现 SOLOHeaders（实测必须）。"""
    token = credential.access_token
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": f"Trae/{IDE_VERSION}",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "X-Cloudide-Token": token,
        "X-Ide-Token": token,
        "X-App-Id": APP_ID,
        "X-App-Version": "default",
        "X-Ide-Version": IDE_VERSION,
        "X-Ide-Version-Code": IDE_VERSION_CODE,
        "X-App-Version-Code": IDE_VERSION_CODE,
        "X-Ide-Version-Type": "stable",
        "X-Device-Type": "windows",
        "X-OS-Version": OS_VERSION,
        "X-Device-Brand": DEVICE_BRAND,
        "Request-Traffic-Type": "prod",
    }
    if credential.uid:
        headers["X-Uid"] = credential.uid
    if credential.machine_id:
        headers["X-Machine-Id"] = credential.machine_id
    if credential.device_id:
        headers["X-Device-Id"] = credential.device_id
    return headers


def ug_headers(credential: TraeCredential, *, device_id: str = "") -> dict[str, str]:
    """签到/积分端点头（api.trae.cn）。

    设备头是签到 API 的隐藏必填项（论坛实测 topic/180147）：缺 X-Device-Id 时
    status 直接返回 9004（参数错误）。取值需要是数字串，且**不宜复用**——两者
    的观测依据见 credential.new_checkin_device_id 的注释。

    未显式传入时每次生成一个新的数字串；调用方需要同一设备号跨请求一致时
    （如 9074 后原样重试）才自行传入。传入非数字串时回落为新生成值：
    凭证自带的 deviceId 是登录 URL 用的 hex32，拿它调 claim 会得到 9074。
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Trae/{IDE_VERSION}",
        "Authorization": f"Cloud-IDE-JWT {credential.access_token}",
        "X-User-Region": "CN",
    }
    # machine_id 保持登录时那一对（它不是签到的校验项，换掉反而可能与登录态不匹配）
    if credential.machine_id:
        headers["X-Machine-Id"] = credential.machine_id
    effective = device_id if device_id.isdigit() else new_checkin_device_id()
    headers["X-Device-Id"] = effective
    if credential.uid:
        headers["X-Uid"] = credential.uid
    return headers


def oauth_headers() -> dict[str, str]:
    """兑换/用户信息头：无签名，仅 UA（对照原实现 OAuthHeaders）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Trae/{IDE_VERSION}",
    }


class TraeClient:
    """上游 HTTP 客户端。host 可覆盖，便于测试。"""

    def __init__(
        self,
        *,
        agent_host: str = AGENT_HOST,
        ug_host: str = UG_HOST,
        oauth_host: str = OAUTH_HOST,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.agent_host = agent_host
        self.ug_host = ug_host
        self.oauth_host = oauth_host
        self._stream_client = stream_client
        self._short_client = short_client

    def _stream(self) -> httpx.AsyncClient:
        if self._stream_client is None:
            self._stream_client = httpx.AsyncClient(timeout=STREAM_TIMEOUT, trust_env=False)
        return self._stream_client

    def _short(self) -> httpx.AsyncClient:
        if self._short_client is None:
            self._short_client = httpx.AsyncClient(timeout=SHORT_TIMEOUT, trust_env=False)
        return self._short_client

    async def aclose(self) -> None:
        for client in (self._stream_client, self._short_client):
            if client is not None:
                await client.aclose()

    # ------------------------------------------------------------- 聊天流

    async def stream_chat(
        self, credential: TraeCredential, payload: dict[str, Any], model: str,
    ) -> AsyncIterator[Event]:
        """POST llm_utils_chat 并逐事件产出中立 Event。非 2xx 抛 UpstreamHTTPError。"""
        body = prepare_body(payload, model)
        url = f"{self.agent_host}{EP_CHAT}"
        async with self._stream().stream(
            "POST", url, json=body, headers=solo_headers(credential),
        ) as response:
            if response.status_code >= 400:
                raw = await response.aread()
                raise UpstreamHTTPError(response.status_code, raw)
            async for frame in iter_frames(response.aiter_bytes()):
                for event in trae_events.parse_all_events(frame):
                    yield event

    # ---------------------------------------------------------- 短请求接口

    async def fetch_models(self, credential: TraeCredential) -> list[Model]:
        body = {
            "function": FUNCTION, "config_names": None, "need_prompt": False,
            "current_config_info": None, "poly_prompt": True,
            "mode_type": None, "agent_type": None,
        }
        data = await self._post_json(
            f"{self.agent_host}{EP_MODELS}", body, solo_headers(credential, stream=False),
        )
        configs = data.get("config_info_list")
        if not isinstance(configs, list):
            raise UpstreamProtocolViolation("models response missing config_info_list")
        models = [self._to_model(c) for c in configs
                  if isinstance(c, dict) and c.get("config_name")]
        models = [m for m in models if m is not None]
        if not models:
            raise UpstreamProtocolViolation("models api returned empty list")
        return models

    @staticmethod
    def _to_model(config: dict) -> Model | None:
        """config 条目 → Model：解析消耗倍率与上下文窗口（缺失留空）。

        倍率在 display_contact_config（JSON 字符串）里：
        consumption_rate.enable 且有 data.rate 时才可信。
        """
        display = config.get("display_config")
        if not isinstance(display, dict):
            display = {}
        credit_rate: float | None = None
        contact = config.get("display_contact_config")
        if isinstance(contact, str) and contact:
            try:
                parsed = json.loads(contact)
            except ValueError:
                parsed = None
            rate_info = (parsed or {}).get("consumption_rate") or {}
            if rate_info.get("enable") and isinstance(rate_info.get("data"), dict):
                rate = rate_info["data"].get("rate")
                if isinstance(rate, (int, float)) and not isinstance(rate, bool):
                    credit_rate = float(rate)
        context_window = config.get("context_window_tokens") or {}
        max_input = context_window.get("dev") if isinstance(context_window, dict) else None
        if not isinstance(max_input, int) or isinstance(max_input, bool):
            max_input = None
        return Model(
            id=str(config["config_name"]),
            name=str(display.get("display_name") or ""),
            credit_rate=credit_rate,
            max_input_tokens=max_input,
            supports_reasoning=_trae_supports_reasoning(config),
            # TRAE 的 reasoning_effort_config 只有 support_thinking 布尔，
            # 无档位信息 → default_effort 保持 None（透传 None 优于编造）
            default_effort=None,
        )

    async def fetch_quota(self, credential: TraeCredential) -> Quota:
        """ide_user_ent_usage：remain = limit - used；多权益包求和。

        同时保留每个权益包的名称/额度/已用/到期，供管理台展示奖励积分明细
        （它们各自独立到期，汇总数字看不出是哪些包、什么时候过期）。
        注意只填充展示用的 packages，不填 expiry_ladder：后者是选号排序
        指标，TRAE 按设计无周期概念（保持 None 不变）。
        """
        data = await self._post_json(
            f"{self.ug_host}/trae/api/v2/pay/ide_user_ent_usage", {},
            ug_headers(credential),
        )
        packs = data.get("user_entitlement_pack_list")
        if not isinstance(packs, list):
            raise UpstreamProtocolViolation("quota response missing pack list")
        limit = 0.0
        used = 0.0
        packages: list[dict[str, Any]] = []
        for pack in packs:
            if not isinstance(pack, dict):
                continue
            base = (pack.get("entitlement_base_info") or {}).get("quota") or {}
            pack_limit = base.get("credits_limit")
            if not isinstance(pack_limit, (int, float)) or pack_limit <= 0:
                continue
            pack_used = (pack.get("usage") or {}).get("credits_amount")
            limit += float(pack_limit)
            used += float(pack_used) if isinstance(pack_used, (int, float)) else 0.0
            packages.append({
                "name": _pack_name(pack),
                "total": float(pack_limit),
                "used": float(pack_used) if isinstance(pack_used, (int, float)) else 0.0,
                "end": _pack_end(pack),
            })
        return Quota(remaining=max(0.0, limit - used), total=limit,
                     packages=packages or None, probed_at=int(time.time()))

    async def fetch_checkin_status(self, credential: TraeCredential,
                                   *, device_id: str = "") -> dict[str, Any]:
        """checkin_credits/status：checked_in / credits / enable。"""
        data = await self._post_json(
            f"{self.ug_host}{trae_events.EP_CHECKIN_STATUS}", {},
            ug_headers(credential, device_id=device_id))
        return {
            "checked_in": bool(data.get("checked_in")),
            "credits": data.get("credits"),
            "enable": bool(data.get("enable")),
        }

    async def claim_checkin(self, credential: TraeCredential,
                            *, device_id: str = "") -> dict[str, Any] | None:
        """checkin_credits/claim：领取当日积分。"""
        return await self._post_json(
            f"{self.ug_host}{trae_events.EP_CHECKIN_CLAIM}", {},
            ug_headers(credential, device_id=device_id))

    async def refresh_token(self, credential: TraeCredential) -> TraeCredential:
        """ExchangeToken；失败不改写原凭证字段。"""
        host = resolve_oauth_host(credential, self.oauth_host)
        body = {"ClientID": CLIENT_ID, "RefreshToken": credential.refresh_token,
                "ClientSecret": "-", "UserID": ""}
        data = await self._post_json(f"{host}{EP_EXCHANGE}", body, oauth_headers())
        result = data.get("Result")
        if not isinstance(result, dict) or not result.get("Token"):
            raise UpstreamProtocolViolation("refresh_failed: no token in response")
        expires_raw = result.get("TokenExpireAt")
        expires_at = credential.expires_at
        if isinstance(expires_raw, (int, float)) and expires_raw > 0:
            expires_at = normalize_epoch(int(expires_raw))
        elif isinstance(result.get("TokenExpireDuration"), (int, float)):
            expires_at = int(time.time()) + int(result["TokenExpireDuration"])
        return TraeCredential(
            uid=credential.uid, access_token=str(result["Token"]),
            refresh_token=str(result.get("RefreshToken") or credential.refresh_token),
            expires_at=expires_at, domain=credential.domain, api_host=credential.api_host,
            machine_id=credential.machine_id, device_id=credential.device_id,
            enterprise_id=credential.enterprise_id, nickname=credential.nickname,
        )

    async def get_user_info(self, credential: TraeCredential) -> tuple[str, str]:
        """返回 (uid, nickname)；失败不影响主流程。"""
        host = resolve_oauth_host(credential, self.oauth_host)
        data = await self._post_json(
            f"{host}{EP_USER_INFO}", {"ReqSource": "IDE"},
            oauth_headers() | {"X-Cloudide-Token": credential.access_token},
        )
        result = data.get("Result")
        if not isinstance(result, dict):
            raise UpstreamProtocolViolation("userinfo response missing Result")
        return str(result.get("UserID") or ""), str(result.get("ScreenName") or "")

    async def _post_json(self, url: str, body: dict[str, Any],
                         headers: dict[str, str]) -> dict[str, Any]:
        response = await self._short().post(url, json=body, headers=headers)
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code, response.content)
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation(f"non-JSON response from {url}") from error
        if not isinstance(data, dict):
            raise UpstreamProtocolViolation(f"unexpected response shape from {url}")
        return data


def _trae_supports_reasoning(config: dict[str, Any]) -> bool | None:
    """TRAE 侧推理能力判定（B1.6，实测两处上游字段）。

    优先 `display_config.model_capability == "reasoning_model"`；缺失时回落
    `reasoning_effort_config.support_thinking`。两者都不可信时返回 None。
    """
    display = config.get("display_config")
    if isinstance(display, dict):
        capability = display.get("model_capability")
        if isinstance(capability, str) and capability:
            return capability == "reasoning_model"
    effort = config.get("reasoning_effort_config")
    if isinstance(effort, dict) and isinstance(effort.get("support_thinking"), bool):
        return effort["support_thinking"]
    return None


def _pack_name(pack: dict[str, Any]) -> str:
    """权益包名称：优先 package_extra 的具体包名，逐级回落到描述。

    实测三种都有：福利积分（package_name）/ 每月登录积分（group_name）/
    老用户福利（display_desc）。全部缺失时给空串，展示层会跳过名称列。
    """
    base = pack.get("entitlement_base_info") or {}
    extra = (base.get("product_extra") or {}).get("package_extra") or {}
    for candidate in (extra.get("package_name"), pack.get("group_name"),
                      pack.get("display_desc")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _pack_end(pack: dict[str, Any]) -> int | None:
    """权益包到期时间（epoch）。取不到返回 None，展示层显示「—」。"""
    base = pack.get("entitlement_base_info") or {}
    for candidate in (base.get("end_time"), pack.get("expire_time")):
        if isinstance(candidate, (int, float)) and candidate > 0:
            return normalize_epoch(int(candidate))
    return None


class UpstreamHTTPError(base.UpstreamHTTPError):
    """TRAE 上游非 2xx；kind() 走 TRAE 的 1005/400 规则。"""

    classify_status = staticmethod(trae_events.classify_status)


# 上游 OAuth 地址白名单：凭证 JSON 里的 apiHost 是用户可控输入，
# 一旦被伪造，ExchangeToken 会把真实 refreshToken 发往任意主机
# （PROPOSAL §8「真实 Token 绝不转发到未授权站点」）。
ALLOWED_OAUTH_HOSTS: frozenset[str] = frozenset(
    {AGENT_HOST, UG_HOST, OAUTH_HOST, "https://api.trae.cn", "https://api.trae.com.cn"}
)


def resolve_oauth_host(credential: TraeCredential, oauth_host: str) -> str:
    """校验凭证自带 apiHost；不在白名单时退回已核实的官方地址。

    只做一次校验、不抛异常：落库的旧凭证可能带任意 apiHost，
    抛错会让这些凭证永远无法刷新，退回官方地址既保住可用性又不泄 Token。
    """
    host = (credential.api_host or "").strip().rstrip("/")
    return host if host in ALLOWED_OAUTH_HOSTS else oauth_host


@dataclass(slots=True)
class TraeProvider:
    """Provider 协议实现（细接口，Q16=A）。"""

    client: TraeClient = field(default_factory=TraeClient)
    pacer: Any | None = None

    id: str = "trae"

    # 动态模型拉取失败负缓存：失败后 N 秒内不再打上游（静态表兜底）。
    # 上游 /v1/models 每次都要拉取，故障期会反复失败；
    # 负缓存把无效请求压到 5 分钟一次（PROPOSAL §4.4 约定）。
    _dynamic_models_blocked_until: float | None = field(default=None, init=False)
    # 上次成功动态拉取的元数据（lower 名 → 字段表），静态表兜底时填倍率不丢
    _last_dynamic_meta: dict[str, dict] = field(default_factory=dict, init=False)

    def import_credential(self, raw: dict) -> dict:
        credential = parse_credential(raw)
        if not credential.uid:
            raise UpstreamProtocolViolation("credential missing uid")
        host = credential.api_host.strip().rstrip("/")
        if host and host not in ALLOWED_OAUTH_HOSTS:
            # 导入即拒绝：让用户立刻看到，而不是等下一次刷新时静默走官方地址
            raise UpstreamProtocolViolation(
                f"apiHost {host!r} is not in the TRAE allowed upstream hosts")
        return credential.to_dict()

    def credential_from(self, credential_data: dict) -> TraeCredential:
        """供 RefreshTask 判断是否进入刷新窗口（Q12 预刷新）。

        缺少这个方法时 `_needs_refresh` 对 TRAE 恒返回 False，
        凭证只能等 access token 过期后被动失效，永远不预刷新。
        """
        return TraeCredential.from_dict(credential_data)

    def classify(self, status: int, body: bytes) -> ErrKind:
        return trae_events.classify_status(status, body)

    async def probe_quota(self, credential_data: dict) -> Quota:
        return await self.client.fetch_quota(TraeCredential.from_dict(credential_data))

    async def list_models(self, credential_data: dict) -> list[Model]:
        """动态拉取 + 静态表合并：静态表的实测大小写优先，元数据继承动态结果。

        TRAE 上游列表数据与实际行为不一致（实测小写名 4001、驼峰名成功），
        因此重名条目的 id 用静态表的实测大小写，但保留动态条目的
        倍率 / token 上限等元数据。动态拉取失败（负缓存期）时，
        静态表条目用上次成功动态拉取的元数据填充，倍率不丢。
        """
        by_lower: dict[str, Model] = {}
        credential = TraeCredential.from_dict(credential_data)
        blocked_until = self._dynamic_models_blocked_until
        if credential.access_token and (blocked_until is None
                                        or time.monotonic() >= blocked_until):
            try:
                for model in await self.client.fetch_models(credential):
                    by_lower.setdefault(model.id.lower(), model)
                self._dynamic_models_blocked_until = None   # 成功即清除负缓存
                self._last_dynamic_meta = {
                    lower: {"name": m.name, "credit_rate": m.credit_rate,
                            "max_input_tokens": m.max_input_tokens,
                            "max_output_tokens": m.max_output_tokens,
                            "supports_images": m.supports_images,
                            "supports_tool_call": m.supports_tool_call}
                    for lower, m in by_lower.items()}
            except Exception as error:  # noqa: BLE001 - 回退不是静默：错误带上日志
                import logging

                self._dynamic_models_blocked_until = time.monotonic() + 300
                logging.getLogger(__name__).warning(
                    "TRAE 动态模型拉取失败，回退静态表 5 分钟: %s", error)
        # 静态表：重名时 id 用实测大小写，元数据优先取动态结果，
        # 其次取上次成功的动态拉取（负缓存期倍率不丢）
        merged: list[Model] = []
        for mid in STATIC_MODELS:
            lower = mid.lower()
            existing = by_lower.get(lower)
            if existing is not None:
                merged.append(Model(id=mid, name=existing.name,
                                    credit_rate=existing.credit_rate,
                                    max_input_tokens=existing.max_input_tokens,
                                    max_output_tokens=existing.max_output_tokens,
                                    supports_images=existing.supports_images,
                                    supports_tool_call=existing.supports_tool_call))
                continue
            meta = self._last_dynamic_meta.get(lower)
            if meta is not None and any(value is not None for value in meta.values()):
                merged.append(Model(id=mid, **meta))
            else:
                merged.append(Model(id=mid))
        # 动态独有的模型（静态表没有的新模型）附在后面
        known = {m.id.lower() for m in merged}
        for lower, model in by_lower.items():
            if lower not in known:
                merged.append(model)
        return merged

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        """引擎调用入口：dict 凭证 → 上游流 → 中立事件。"""
        credential = TraeCredential.from_dict(credential_data)
        if self.pacer is not None:
            await self.pacer.wait_turn()
        async for event in self.client.stream_chat(credential, payload, model):
            yield event

    async def refresh(self, credential_data: dict) -> dict:
        refreshed = await self.client.refresh_token(TraeCredential.from_dict(credential_data))
        return refreshed.to_dict()

    async def aclose(self) -> None:
        """释放内部 HTTP 连接池。"""
        await self.client.aclose()

    def checkin_scope(self, credential_data: dict) -> str:
        """同上游账号的多凭证共享一次签到。"""
        return f"trae|{credential_data.get('uid', '')}"

    async def checkin(self, credential_data: dict) -> CheckinResult:
        """TRAE 签到：查状态 → 未签才领 → **回查确认积分到账**。

        判定纪律（吃过亏，勿简化）：claim 返回 `code:0` **不能**当作领取成功。
        当天已签过的账号，任何 device_id 的 claim 都返回 `code:0`（幂等），
        仅凭它判断会把「什么都没发生」报成成功。唯一可信的确认方式是回查
        `status.credits`：它来自上游的余额字段，只有它变了才发现得了。

        因此成功的判定分两种，都很明确：
        - 调用前就 `checked_in=True` → 今日已签（`already_checked_in`）
        - claim 后回查 `checked_in=True` 且 **`credits` 比 claim 前增加**
          → 真正领到（`credit` 为本次增量）
        claim 说成功但回查没有增量、也没有 `checked_in` → 如实报失败，不伪装。

        9074「当前参与用户太多」按限流处理（观测数据见 credential 模块注释）；
        一轮内最多试 `CHECKIN_ATTEMPTS` 次，每次换一个全新设备号（设备号复用是
        9074 的可疑诱因），仍失败则返回软失败——后台任务 10 分钟一轮，
        与上游的分钟级退避窗口自然错开。
        """
        credential = TraeCredential.from_dict(credential_data)
        last: CheckinResult | None = None
        for _ in range(CHECKIN_ATTEMPTS):
            # 同一个设备号贯穿本轮的 status 与 claim：两者是配对的校验参数
            device_id = new_checkin_device_id()
            before = await self.client.fetch_checkin_status(credential, device_id=device_id)
            if before["checked_in"]:
                return CheckinResult(ok=True, credit=None, message="今天已签到",
                                     already_checked_in=True)
            if not before["enable"]:
                return CheckinResult(ok=False, message="当前账号不可签到")
            claim = await self.client.claim_checkin(credential, device_id=device_id)
            code = claim.get("code")
            if code not in (0, None):
                # 软失败：不抛异常（后台任务按失败计数，当日不封账，下轮重试）
                last = CheckinResult(ok=False, code=int(code),
                                     message=str(claim.get("message") or "claim 失败"))
                continue
            # claim 说成功 → 必须回查确认，不能直接采信
            after = await self.client.fetch_checkin_status(credential, device_id=device_id)
            gained = _credit_delta(before["credits"], after["credits"])
            if after["checked_in"] and gained is not None:
                return CheckinResult(ok=True, credit=after["credits"], code=0,
                                     message=f"签到成功 +{gained}")
            last = CheckinResult(
                ok=False, code=0,
                message=("领取接口返回成功，但回查未确认积分到账"
                         f"（{before['credits']} → {after['credits']}）"))
        # 所有尝试都失败：返回最后一次的真实原因（不伪装成功）
        return last if last is not None else CheckinResult(  # pragma: no cover
            ok=False, message="签到未完成")

    # ------------------------------------------------- callback 轨道（Q17=C）

    def parse_login_url(self, raw_url: str) -> CallbackInfo:
        """解析 TRAE 登录回调链接。"""
        return parse_callback_url(raw_url)

    def build_login_url(self, callback_url: str, *, machine_id: str,
                        device_id: str) -> str:
        return build_login_url(callback_url, machine_id=machine_id, device_id=device_id)

    def start_auth(self, callback_url: str) -> AuthSession:
        """生成登录 URL。machine/device id 由调用方保管，落盘凭证必须复用同一对。"""
        machine_id, device_id = new_machine_identity()
        return AuthSession(
            flow="callback", state=f"{machine_id}:{device_id}",
            callback_url=callback_url,
            auth_url=build_login_url(callback_url, machine_id=machine_id,
                                     device_id=device_id),
        )

    async def complete_callback(self, raw_url: str, state: str) -> dict:
        """回调链接 → ExchangeToken → 归一化凭证。

        state 形如 ``machine_id:device_id``，用于保证落盘凭证与登录时用的
        设备标识一致（原实现里这两者不一致会导致登录态与凭证不匹配）。
        """
        info = parse_callback_url(raw_url)
        machine_id, _, device_id = state.partition(":")
        if not machine_id or not device_id:
            raise UpstreamProtocolViolation("auth state missing machine/device id")

        # 回调只给 refreshToken，accessToken 必须由 ExchangeToken 换出来。
        # 换失败就没有可用凭证，绝不能用 refreshToken 充当 accessToken 混进池子。
        credential = credential_from_callback(
            info, "", machine_id=machine_id, device_id=device_id)
        try:
            credential = await self.client.refresh_token(credential)
        except UpstreamHTTPError as error:
            # 上游拒绝兑换（refreshToken 过期/失效）→ 归一为协议违规，
            # 让调用方按「凭证无效」处理，而不是把 HTTP 细节漏到 API 层
            raise UpstreamProtocolViolation(
                f"token exchange rejected: {error.status}") from error
        if not credential.uid:
            uid, nickname = await self.client.get_user_info(credential)
            credential = TraeCredential(
                uid=uid or credential.uid, access_token=credential.access_token,
                refresh_token=credential.refresh_token, expires_at=credential.expires_at,
                domain=credential.domain, api_host=credential.api_host,
                machine_id=credential.machine_id, device_id=credential.device_id,
                enterprise_id=credential.enterprise_id,
                nickname=nickname or credential.nickname)
        return credential.to_dict()

def _credit_delta(before: Any, after: Any) -> float | None:
    """积分增量：两个值都是有限数字时返回 after - before，否则 None。

    上游这两种情况都会出现：字段缺失、以及用 0 表示「不知道」。任一侧不可用就
    返回 None，由调用方按「未确认到账」处理——宁可报需要人看一眼，也不要把
    不确定当成成功。
    """
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return None
    if isinstance(before, bool) or isinstance(after, bool):
        return None
    left, right = float(before), float(after)
    if not (math.isfinite(left) and math.isfinite(right)):
        return None
    return right - left

