"""TRAE SOLO 上游客户端：payload 改写、SSE 流、凭证解析、模型/额度查询。

双 httpx 客户端（T-Q4）：流式无总超时防长流截断；短请求 30s 总超时防悬挂。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...engine.sse import iter_frames
from ...provider.base import (
    AuthSession,
    CheckinResult,
    ErrKind,
    Event,
    Model,
    Quota,
    body_hint,
)
from . import events as trae_events
from .callback import (
    CallbackInfo,
    build_login_url,
    credential_from_callback,
    new_machine_identity,
    parse_callback_url,
)
from .credential import TraeCredential, parse_credential
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
                kept = [tc for tc in tool_calls if isinstance(tc, dict)]
                if kept:
                    item["tool_calls"] = kept
                else:
                    item.pop("tool_calls", None)
                item.pop("content", None) if not content else None
            rewritten.append(item)
        body["messages"] = rewritten

    _normalize_tools(body)
    return body


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


def ug_headers(credential: TraeCredential) -> dict[str, str]:
    """签到/积分端点头（api.trae.cn）。对照原实现 UgHeaders。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Trae/{IDE_VERSION}",
        "Authorization": f"Cloud-IDE-JWT {credential.access_token}",
        "X-User-Region": "CN",
    }
    if credential.device_id:
        headers["X-Device-Id"] = credential.device_id
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
        models = [Model(id=str(c["config_name"]),
                        name=str((c.get("display_config") or {}).get("display_name") or ""))
                  for c in configs
                  if isinstance(c, dict) and c.get("config_name")]
        if not models:
            raise UpstreamProtocolViolation("models api returned empty list")
        return models

    async def fetch_quota(self, credential: TraeCredential) -> Quota:
        """ide_user_ent_usage：remain = limit - used；多权益包求和。"""
        data = await self._post_json(
            f"{self.ug_host}/trae/api/v2/pay/ide_user_ent_usage", {},
            ug_headers(credential),
        )
        packs = data.get("user_entitlement_pack_list")
        if not isinstance(packs, list):
            raise UpstreamProtocolViolation("quota response missing pack list")
        limit = 0.0
        used = 0.0
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
        return Quota(remaining=max(0.0, limit - used), total=limit, probed_at=int(time.time()))

    async def fetch_checkin_status(self, credential: TraeCredential) -> dict[str, Any]:
        """checkin_credits/status：checked_in / credits / enable。"""
        data = await self._post_json(
            f"{self.ug_host}{trae_events.EP_CHECKIN_STATUS}", {}, ug_headers(credential))
        return {
            "checked_in": bool(data.get("checked_in")),
            "credits": data.get("credits"),
            "enable": bool(data.get("enable")),
        }

    async def claim_checkin(self, credential: TraeCredential) -> dict[str, Any] | None:
        """checkin_credits/claim：领取当日积分。"""
        return await self._post_json(
            f"{self.ug_host}{trae_events.EP_CHECKIN_CLAIM}", {}, ug_headers(credential))

    async def refresh_token(self, credential: TraeCredential) -> TraeCredential:
        """ExchangeToken；失败不改写原凭证字段。"""
        host = credential.api_host or self.oauth_host
        body = {"ClientID": CLIENT_ID, "RefreshToken": credential.refresh_token,
                "ClientSecret": "-", "UserID": ""}
        data = await self._post_json(f"{host}{EP_EXCHANGE}", body, oauth_headers())
        result = data.get("Result")
        if not isinstance(result, dict) or not result.get("Token"):
            raise UpstreamProtocolViolation("refresh_failed: no token in response")
        expires_raw = result.get("TokenExpireAt")
        expires_at = credential.expires_at
        if isinstance(expires_raw, (int, float)) and expires_raw > 0:
            expires_at = _normalize_epoch(int(expires_raw))
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
        host = credential.api_host or self.oauth_host
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


def _normalize_epoch(value: int) -> int:
    """上游可能返回毫秒；>1e12 视为毫秒。"""
    return value // 1000 if value > 1_000_000_000_000 else value


class UpstreamHTTPError(Exception):
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        super().__init__(f"upstream http {status}: {body_hint(body)}")

    def kind(self) -> ErrKind:
        return trae_events.classify_status(self.status, self.body)


@dataclass(slots=True)
class TraeProvider:
    """Provider 协议实现（细接口，Q16=A）。"""

    client: TraeClient = field(default_factory=TraeClient)

    id: str = "trae"

    def import_credential(self, raw: dict) -> dict:
        credential = parse_credential(raw)
        if not credential.uid:
            raise UpstreamProtocolViolation("credential missing uid")
        return credential.to_dict()

    def classify(self, status: int, body: bytes) -> ErrKind:
        return trae_events.classify_status(status, body)

    async def probe_quota(self, credential_data: dict) -> Quota:
        return await self.client.fetch_quota(TraeCredential.from_dict(credential_data))

    async def list_models(self, credential_data: dict) -> list[Model]:
        """优先动态拉取（需要凭证）；失败回退静态表。"""
        credential = TraeCredential.from_dict(credential_data)
        if credential.access_token:
            try:
                return await self.client.fetch_models(credential)
            except Exception as error:  # noqa: BLE001 - 回退不是静默：错误带上日志
                import logging

                logging.getLogger(__name__).warning(
                    "TRAE 动态模型拉取失败，回退静态表: %s", error)
        return [Model(id=mid) for mid in STATIC_MODELS]

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        """引擎调用入口：dict 凭证 → 上游流 → 中立事件。"""
        credential = TraeCredential.from_dict(credential_data)
        async for event in self.client.stream_chat(credential, payload, model):
            yield event

    async def refresh(self, credential_data: dict) -> dict:
        refreshed = await self.client.refresh_token(TraeCredential.from_dict(credential_data))
        return refreshed.to_dict()

    async def aclose(self) -> None:
        """释放内部 HTTP 连接池。"""
        await self.client.aclose()

    async def checkin(self, credential_data: dict) -> CheckinResult:
        """TRAE 签到：先查状态，未签且可签才领取。

        上游没有独立的「已签到」错误码，status.checked_in 就是已签语义。
        """
        credential = TraeCredential.from_dict(credential_data)
        status = await self.client.fetch_checkin_status(credential)
        if status["checked_in"]:
            return CheckinResult(ok=True, credit=None, message="今天已签到",
                                 already_checked_in=True)
        if not status["enable"]:
            return CheckinResult(ok=False, message="当前账号不可签到")
        await self.client.claim_checkin(credential)
        return CheckinResult(ok=True, credit=None)

    def checkin_scope(self, credential_data: dict) -> str:
        """同上游账号的多凭证共享一次签到。"""
        return f"trae|{credential_data.get('uid', '')}"

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
