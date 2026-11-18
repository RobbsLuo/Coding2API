"""通用 OpenAI 兼容上游（自行部署 / 第三方官方 API）。

十渠道都是**协议私有**的订阅池，加一家要改八九处代码；但 Command Code、Kimi
Code、MiniMax、智谱、自建 vLLM / LiteLLM 这些**本身就是标准 OpenAI 端点**，
不需要逆向。这个渠道把它们接进来，一个部署配置一条（`GENERIC_API_ENDPOINT`）。

与 Zen / Kilo 的区别：**有真实凭证**（Bearer API key，进加密凭证池，可多把
轮换），所以 401 是**凭据真的坏了**（判 DEAD 硬禁用），而 Zen/Kilo 的 401 只是
「这个模型要付费 key」（判 INVALID）。这是两套完全相反的处置，不能共用分类。

**一个部署只配一条**：本渠道 id 固定 `generic`，模型表、统计、凭证池都按渠道
聚合。要同时接两家官方 API，拆成两个部署（或等后续把 `KNOWN_PROVIDERS`
动态化，见 `docs/oss-feature-migration-scan-2026-11-18.md` §P1-1）。
"""

from __future__ import annotations

import copy
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...engine.sse import iter_frames
from ...provider import base
from ...provider.base import ErrKind, Event, EventKind, Model, Quota, UpstreamProtocolViolation
from ...provider.openai_chunk import openai_events, payload_of, usage_from_openai
from ...tasks.pacer import paced
from ..lazy_clients import LazyClients

EP_CHAT = "/chat/completions"
EP_MODELS = "/models"


def prepare_body(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """上游请求体：深拷 messages、强制 stream、写死 model。

    与 Kilo / Zen 同一条纪律：下游可能原地改写 messages，不能穿透回引擎持有的
    `request.raw`（会话粘性指纹与续写都读它）。非流式由引擎聚合，故一律
    `stream=true`。
    """
    body = dict(payload)
    body["messages"] = copy.deepcopy(payload.get("messages"))
    body["model"] = model
    body["stream"] = True
    return body


class UpstreamHTTPError(base.UpstreamHTTPError):
    """通用上游非 2xx。分类见 `classify_status`。"""

    classify_status = staticmethod(lambda status, body: classify_status(status, body))


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """标准 OpenAI 端点 + 真实 API key 的状态码分类。

    与 Zen / Kilo 的**无凭证**渠道相反：这里的 401/403 就是「这把 key 废了」，
    判 DEAD 让刷新任务/人工 revive 处理；归 INVALID 会让坏 key 一直留在池里
    参与轮换，每次请求白撞一次。

    * 401/403 → DEAD（key 失效 / 无权限）
    * 402 → CREDIT（余额不足）
    * 404/400/422 → INVALID（模型不存在 / 请求写错）
    * 429 → SOFT（限流；标准端点通常不点名模型，故账号级）
    * 5xx → OTHER（累计后冷却）
    """
    if status in (401, 403):
        return ErrKind.DEAD
    if status == 402:
        return ErrKind.CREDIT
    if status in (400, 404, 422):
        return ErrKind.INVALID
    if status == 429:
        return ErrKind.SOFT
    return ErrKind.OTHER


def _error_event(error: dict[str, Any]) -> Event:
    """流内 `{"error": {...}}` 信封（标准 OpenAI 形状）。"""
    raw_code = error.get("code")
    code = (raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool)
            else None)
    message = error.get("message")
    return Event(
        kind=EventKind.ERROR,
        error_code=code,
        error_message=str(message) if message is not None else "",
        # 流内错误信封没有 HTTP 状态码：保留消息供归因，分类交给引擎按 OTHER
        # 处理（不猜成账号级冷却）。
    )


def parse_all_events(frame) -> list[Event]:
    """标准 OpenAI chunk → 中立事件（含 reasoning_content）。"""
    return openai_events(frame, "通用上游", error_event=_error_event,
                         usage=usage_from_openai)


class GenericClient(LazyClients):
    """标准 OpenAI 兼容客户端：`{base}/chat/completions` + `{base}/models`。"""

    def __init__(self, *, endpoint: str, api_key: str = "",
                 user_agent: str = "",
                 stream_client: httpx.AsyncClient | None = None,
                 short_client: httpx.AsyncClient | None = None,
                 proxy: str | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.user_agent = user_agent
        self._stream_client = stream_client
        self._short_client = short_client
        self._proxy = proxy

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        return headers

    async def stream_chat(self, payload: dict[str, Any], model: str,
                          api_key: str = "") -> AsyncIterator[Event]:
        """逐帧产出中立事件；非 2xx 抛 `UpstreamHTTPError`。"""
        key = api_key or self.api_key
        headers = self.headers()
        if key and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {key}"
        body = prepare_body(payload, model)
        async with self._stream.stream(
            "POST", f"{self.endpoint}{EP_CHAT}", json=body, headers=headers,
        ) as response:
            if response.status_code >= 400:
                raise UpstreamHTTPError(response.status_code,
                                        await base.read_body_bounded(response),
                                        **base.retry_hints(response.headers))
            async for frame in iter_frames(response.aiter_bytes()):
                for event in parse_all_events(frame):
                    yield event

    async def fetch_models(self, api_key: str = "") -> list[Model]:
        """`GET /models`；拿不到就抛，由上层用缓存兜底。"""
        headers = self.headers()
        key = api_key or self.api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        headers.pop("Accept", None)
        response = await self._short.get(f"{self.endpoint}{EP_MODELS}",
                                        headers=headers)
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON models response") from error
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise UpstreamProtocolViolation("models response missing data list")
        models: list[Model] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not (isinstance(model_id, str) and model_id):
                continue
            name = item.get("name")
            models.append(Model(id=model_id,
                                name=name if isinstance(name, str) else ""))
        if not models:
            raise UpstreamProtocolViolation("models api returned no models")
        return models


def split_models(raw: str) -> list[Model]:
    """`GENERIC_MODELS` 静态兜底表（上游没 `/models` 时用，逗号分隔）。"""
    return [Model(id=name.strip()) for name in raw.split(",") if name.strip()]


def parse_frame(frame) -> Event | None:  # pragma: no cover - 仅测试/诊断使用
    payload = payload_of(frame, "通用上游")
    if payload is None:
        return None
    events = parse_all_events(frame)
    return events[0] if events else None


@dataclass(slots=True)
class GenericProvider:
    """Provider 协议实现：标准 OpenAI 端点 + Bearer 凭证池。

    只实现对话 / 模型列表 / 额度三件事。`probe_quota` 恒 `probe_failed`
    （标准端点没有统一的额度 API），签到 / 成长 / 活跃上报 / token 刷新一律
    **不实现**——任务层用 `getattr` 探测，缺方法就跳过。
    """

    client: GenericClient = field(default_factory=lambda: GenericClient(endpoint=""))
    static_models: list[Model] = field(default_factory=list)
    pacer: Any | None = None

    id: str = "generic"

    def import_credential(self, raw: dict) -> dict:
        """导入：接受 `{"api_key": "sk-…"}` / `{"token": …}` / 裸字符串。

        base_url 来自部署配置（`GENERIC_API_ENDPOINT`），**不由凭证携带**：
        否则同一渠道的凭证指向不同上游，模型表与调度会互相污染。
        """
        if isinstance(raw, str):
            raw = {"api_key": raw}
        if not isinstance(raw, dict):
            return {}
        for key in ("api_key", "apiKey", "token", "access_token"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return {"api_key": value.strip()}
        return {}

    def classify(self, status: int, body: bytes) -> ErrKind:
        return classify_status(status, body)

    async def probe_quota(self, credential_data: dict) -> Quota:
        # 标准端点无额度接口：未知（probe_failed）而不是「已耗尽」
        return Quota(probe_failed=True, probed_at=int(time.time()))

    async def list_models(self, credential_data: dict) -> list[Model]:
        api_key = str(credential_data.get("api_key") or "")
        try:
            return await self.client.fetch_models(api_key)
        except (UpstreamHTTPError, UpstreamProtocolViolation):
            # 无 `/models` 的端点（部分自建网关）回落静态表；两者都没有则抛，
            # 让上层用上次缓存兜底
            if self.static_models:
                return list(self.static_models)
            raise

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        api_key = str(credential_data.get("api_key") or "")
        async with paced(self.pacer, "generic"):
            async for event in self.client.stream_chat(payload, model, api_key):
                yield event

    async def aclose(self) -> None:
        await self.client.aclose()
