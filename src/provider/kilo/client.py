"""Kilo Gateway 上游客户端：标准 OpenAI SSE + 免费模型过滤。

与 Zen 的关键差异（决定这里能不能简化）：

* **无门禁伪装**——不需要伪造 UA / session / tools，`prepare_body` 只做
  标准 OpenAI 请求体处理（深拷贝 messages、强制 stream）；
* **免费模型有权威标记**——`/models` 每个条目带 `isFree` 布尔（实测
  395 个模型中 17 个为 true）。故**不做探活**：探活会真发一次推理，白白
  消耗本就极小的免费配额（网关级 200 req/h/IP），且结果不稳定（上游池
  随 OpenRouter 波动，探活成功不代表下一分钟可用）。免费集直接由
  `isFree` 决定，上游增删自动跟随。

链路层：`/api/gateway/models` 与 `/api/gateway/chat/completions`，匿名即可
（免费模型无需 Authorization）。上游 429 自报限额来自 OpenRouter 共享池
（`limit_source: upstream_provider_shared_pool`）并**点名具体模型**，429 消退
后同一模型还会转 503 `no endpoints available`；两种情况下实测同一时刻其他
免费模型仍 200——故 429 与 502/503/504 都归模型级冷却，只锁触发模型，交给
引擎按 `MODEL` 处理，本包不自建熔断。
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
from ...provider.base import (
    ErrKind,
    Event,
    Model,
    Quota,
)
from . import events as kilo_events

# 上游端点（公开事实，非用户可配置输入）
KILO_HOST = "https://api.kilo.ai/api/gateway"
EP_CHAT = "/chat/completions"
EP_MODELS = "/models"

# 流式无总超时防长流截断；短请求 30s 防悬挂（与 Zen/TRAE 同策略）
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
SHORT_TIMEOUT = httpx.Timeout(30.0)


def prepare_body(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """上游请求体：深拷 messages、强制 stream、写死 model。

    deepcopy 的必要性与 TRAE/Zen 相同：下游可能原地改写 messages，不能穿透
    回引擎持有的 `request.raw`（会话粘性指纹与续写都读它）。`stream` 无条件
    置 true——上游非流式由引擎聚合，不占用非流式分支。
    """
    body = dict(payload)
    body["messages"] = copy.deepcopy(payload.get("messages"))
    body["model"] = model
    body["stream"] = True
    return body


def request_headers() -> dict[str, str]:
    """匿名请求头。

    **不带 `Authorization`**：免费模型匿名即可用（实测无鉴权头 200）。与 Zen
    相反——Zen 带 `Bearer public` 占位符更稳，而 Kilo 的网关把任何
    `Authorization` 头当作真实凭证去校验，带 `Bearer public` 反而回
    401 `INVALID_TOKEN`（实测 2026-09-30）。故此处只发最简头。
    """
    return {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


class UpstreamHTTPError(base.UpstreamHTTPError):
    """Kilo 上游非 2xx；kind() 走 Kilo 的状态码规则。"""

    classify_status = staticmethod(kilo_events.classify_status)


class KiloClient:
    """上游 HTTP 客户端。host 可覆盖，便于测试与灰度。"""

    def __init__(
        self,
        *,
        host: str = KILO_HOST,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.host = host.rstrip("/")
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

    async def stream_chat(self, payload: dict[str, Any], model: str) -> AsyncIterator[Event]:
        """POST chat/completions 并逐事件产出中立 Event。非 2xx 抛 UpstreamHTTPError。"""
        body = prepare_body(payload, model)
        async with self._stream().stream(
            "POST", f"{self.host}{EP_CHAT}", json=body, headers=request_headers(),
        ) as response:
            if response.status_code >= 400:
                raw = await response.aread()
                raise UpstreamHTTPError(response.status_code, raw)
            async for frame in iter_frames(response.aiter_bytes()):
                for event in kilo_events.parse_all_events(frame):
                    yield event

    async def fetch_models(self) -> list[Model]:
        """GET /models，只返回 `isFree=true` 的免费模型（不做探活）。

        `fetch_models` 的结果同时也是引擎登记模型归属的来源，过滤后扁平名
        请求不会再被路由到 Kilo 的付费模型上（否则 401 会误伤虚拟凭证）。

        无静态白名单：上游增删免费模型、改 `isFree` 自动跟随。候选全灭时
        抛出让上层用上次成功的缓存兜底，避免 Kilo 从模型列表里消失。
        """
        response = await self._short().get(
            f"{self.host}{EP_MODELS}", headers=request_headers())
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code, response.content)
        try:
            data = response.json()
        except ValueError as error:
            raise kilo_events.UpstreamProtocolViolation("non-JSON models response") from error
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise kilo_events.UpstreamProtocolViolation("models response missing data list")
        models: list[Model] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not (isinstance(model_id, str) and model_id):
                continue
            if item.get("isFree") is not True:
                continue
            models.append(_model_from_item(item, model_id))
        if not models:
            raise kilo_events.UpstreamProtocolViolation("models api returned no free models")
        return models


def _model_from_item(item: dict[str, Any], model_id: str) -> Model:
    """免费模型条目 → 中立 Model（元数据有则透传，无则留 None）。"""
    name = item.get("name")
    top = item.get("top_provider")
    top = top if isinstance(top, dict) else {}
    architecture = item.get("architecture")
    architecture = architecture if isinstance(architecture, dict) else {}
    modalities = architecture.get("input_modalities")
    modalities = modalities if isinstance(modalities, list) else []
    supported = item.get("supported_parameters")
    supported = supported if isinstance(supported, list) else []

    def as_int(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    context = as_int(item.get("context_length")) or as_int(top.get("context_length"))
    return Model(
        id=model_id,
        name=name if isinstance(name, str) else "",
        # 免费层不消耗额度：显式给 0（而非 None），列表 UI 显示 x0，
        # 排序时也天然排在最省的一档。
        credit_rate=0.0,
        max_input_tokens=context,
        max_output_tokens=as_int(top.get("max_completion_tokens")),
        supports_images=("image" in modalities) if modalities else None,
        supports_tool_call=("tools" in supported) if supported else None,
        supports_reasoning=("reasoning" in supported) if supported else None,
    )


@dataclass(slots=True)
class KiloProvider:
    """Provider 协议实现（细接口，Q16=A）。

    与 Zen 同为无凭证渠道：只实现对话与模型列表三条能力，其余任务靠
    「方法缺失」自然跳过（CheckinTask / GrowthTask / RefreshTask 都用
    getattr 探测，不需要为此改任务代码）。
    """

    client: KiloClient = field(default_factory=KiloClient)
    pacer: Any | None = None

    id: str = "kilo"

    def import_credential(self, raw: dict) -> dict:
        """无凭证渠道：忽略入参，落库空对象（凭证行只是调度占位）。"""
        return {}

    def classify(self, status: int, body: bytes) -> ErrKind:
        return kilo_events.classify_status(status, body)

    async def probe_quota(self, credential_data: dict) -> Quota:
        """无额度接口 → 未知（probe_failed），绝不返回 total=None 被判「已耗尽」。

        `health_score` 对 `probe_failed=True` 返回 None（unknown）：调度器按
        中性处理，不会把这条免费渠道错误地降级到「已耗尽」。
        """
        return Quota(probe_failed=True, probed_at=int(time.time()))

    async def list_models(self, credential_data: dict) -> list[Model]:
        return await self.client.fetch_models()

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        # 与 zen 同样单桶（同渠道共用一条匿名额度），并发模式下必须配对 release。
        if self.pacer is not None:
            await self.pacer.wait_turn("kilo")
        try:
            async for event in self.client.stream_chat(payload, model):
                yield event
        finally:
            if self.pacer is not None:
                self.pacer.release("kilo")

    async def aclose(self) -> None:
        await self.client.aclose()
