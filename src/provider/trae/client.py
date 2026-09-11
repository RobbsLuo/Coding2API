"""TRAE SOLO 上游客户端：payload 改写、SSE 流、凭证解析、模型/额度查询。

双 httpx 客户端（T-Q4）：流式无总超时防长流截断；短请求 30s 总超时防悬挂。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...engine.sse import iter_frames
from ...provider.base import ErrKind, Event, Model, Quota
from . import events as trae_events
from .callback import build_login_url, parse_callback_url
from .credential import TraeCredential, parse_credential
from .events import (
    AGENT_HOST,
    CLIENT_ID,
    EP_CHAT,
    EP_EXCHANGE,
    EP_MODELS,
    EP_USER_INFO,
    FUNCTION,
    OAUTH_HOST,
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


def auth_headers(credential: TraeCredential, *, stream: bool = True) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "x-cloudide-token": credential.access_token,
    }
    if credential.machine_id:
        headers["x-machine-id"] = credential.machine_id
    if credential.device_id:
        headers["x-device-id"] = credential.device_id
    return headers


def oauth_headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "Accept": "application/json"}


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
            "POST", url, json=body, headers=auth_headers(credential),
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
            f"{self.agent_host}{EP_MODELS}", body, auth_headers(credential, stream=False),
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
            auth_headers(credential, stream=False),
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
        headers = oauth_headers() | {"X-Cloudide-Token": credential.access_token}
        data = await self._post_json(f"{host}{EP_USER_INFO}", {"ReqSource": "IDE"}, headers)
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
        super().__init__(f"upstream http {status}")
        self.status = status
        self.body = body

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

    def list_models(self, credential_data: dict) -> list[Model]:
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

    def parse_login_url(self, raw_url: str) -> dict:
        """解析 TRAE 登录回调链接（Q17=C callback 轨道）。"""
        return parse_callback_url(raw_url)

    def build_login_url(self, callback_url: str, *, machine_id: str,
                        device_id: str) -> str:
        return build_login_url(callback_url, machine_id=machine_id, device_id=device_id)
