"""OpenCode Zen 上游客户端：免费层门禁伪装 + 标准 OpenAI SSE。

免费层门禁（实测，2026-09-29 复现，上游会演进故集中在此）：

* `User-Agent` 形如 `opencode/<version>`，且 version ≥ 1.18.0
  （低于阈值 → 426；无版本号 → 403）；
* `x-opencode-session` 匹配 `^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$`；
* body `stream` 必须为 `true`（本项目只用流式，非流式由引擎聚合）；
* body `tools` 必须**同时**含 name 为 `bash` 与 `read` 的工具
  （只校验 name，参数留空壳即可）。

门禁要求 tools 含 bash/read，但这两个是**我们伪造的**、客户端从没声明过：
若模型真的调用了它们，回包里的这些 tool_call 必须被过滤掉，否则客户端会
收到自己没定义的函数调用。过滤只针对「本次由我们注入的名字」——用户自己
就带了 bash/read 时绝不误伤（见 `_InjectedToolFilter`）。
"""

from __future__ import annotations

import asyncio
import copy
import secrets
import string
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
    EventKind,
    Model,
    Quota,
)
from . import events as zen_events

# 上游端点（公开事实，非用户可配置输入；凭证/主机不随凭证 JSON 变化）
ZEN_HOST = "https://opencode.ai"
EP_CHAT = "/zen/v1/chat/completions"
EP_MODELS = "/zen/v1/models"
# 门禁要求的 UA 版本下限；实测 1.18.0 及以上通过，1.17.9 返回 426
MIN_OPENCODE_VERSION = "1.18.0"
# 门禁要求 tools 里必须出现的工具名（大小写敏感）
REQUIRED_TOOLS: tuple[str, ...] = ("bash", "read")

# 免费模型判定：上游 `/zen/v1/models` 返回**全部**模型（含付费）且不带任何
# 免费/付费标记（`owned_by` 恒为 `opencode`，无 cost 字段），换鉴权头 / query
# 也仍是全量；models.dev 之类的第三方目录又与上游实际可用集不同步。
# 唯一权威信号是**匿名可用性**：付费模型恒 401 AuthError（`Missing API key.`），
# 免费模型永不 401。
#
# 故「只显示 free 模型」分两步：
#   1. 后缀收窄候选——上游用 `-free` 命名免费模型（约定，非契约）；
#   2. 逐个探活——真发一次最小请求，只保留 2xx（真能用的）；已下线（400）、
#      区域限制（403）、上游故障（5xx）一并剔除。
# 清单每次现拉现探，无静态白名单：上游增删免费模型自动跟随。
FREE_MODEL_SUFFIX = "-free"
# 探活并发上限：后缀候选实测约 11 个，取 ≥ 候选数让它们单批跑完，避免
# 多批叠加超时；仍显式限定，免得未来后缀泛滥打爆上游。
PROBE_CONCURRENCY = 12
# 单候选探活上限（秒）：探活发生在 /v1/models 的冷路径与启动预热里，而启动
# 预热在应用开始服务 /health 之前，不能无限等首字（实测正常 1–12s，偶发 >100s）。
# 超时按不可用处理：宁可少显示一个慢模型，也不拖垮启动与健康检查。
PROBE_TIMEOUT = 20.0
# 探活用最小请求（真发一次流式对话；只看响应头，不读 body）。
PROBE_PROMPT = "hi"
# 探活结果缓存时长（秒）：探活是整条模型列表链路里最贵的一步——逐个真发一次
# 推理，首字最慢的模型可占十几秒，而总耗时等于最慢那个。免费模型的增删却很慢，
# 所以缓存判活结果，比服务层的 MODEL_LIST_TTL_SECONDS（300s）长一档：服务层每
# 5 分钟到期重拉一次，此处直接复用上次判活集，只有超过本 TTL 才真的重探。
# 代价：免费模型下线后，最多多留在列表里 30 分钟（选中会 400/401，由引擎按
# 无效请求处理，不会误冷却凭证）。
MODELS_CACHE_TTL_SECONDS = 1800.0

# 流式无总超时防长流截断；短请求 30s 防悬挂（与 TRAE 同策略）
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
SHORT_TIMEOUT = httpx.Timeout(30.0)

# session id 的 base62 字母表（12 位 hex + 14 位 base62，共 26 位）
_SESSION_ALPHABET = string.ascii_letters + string.digits


def new_session_id() -> str:
    """生成符合门禁正则的会话 ID：`ses_` + 12 位小写 hex + 14 位 base62。"""
    suffix = "".join(secrets.choice(_SESSION_ALPHABET) for _ in range(14))
    return f"ses_{secrets.token_hex(6)}{suffix}"


def gate_headers(*, version: str = MIN_OPENCODE_VERSION,
                 session_id: str | None = None) -> dict[str, str]:
    """免费层门禁头。每次请求新生成 session（门禁只校验格式，不校验归属）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": f"opencode/{version}",
        "x-opencode-session": session_id or new_session_id(),
        # 匿名也通；带公开占位符比完全省略更稳（避免网关把无 Authorization
        # 的请求路由到别的鉴权分支）
        "Authorization": "Bearer public",
    }


def _empty_tool(name: str) -> dict[str, Any]:
    """门禁空壳工具：只要求 name 存在，参数留最小合法 schema。"""
    return {"type": "function",
            "function": {"name": name, "parameters": {"type": "object"}}}


def _tool_names(tools: list[Any]) -> set[str]:
    """收集已声明工具的名字（只认 `function.name` 字符串）。"""
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


def ensure_gate_tools(body: dict[str, Any]) -> frozenset[str]:
    """确保 body.tools 同时含 `bash`/`read`，返回**本次注入**的名字集合。

    只补缺、不覆盖：用户自带同名工具时视为真实工具，不注入也不过滤。
    """
    existing = body.get("tools")
    tools = list(existing) if isinstance(existing, list) else []
    present = _tool_names(tools)
    injected: set[str] = set()
    for name in REQUIRED_TOOLS:
        if name not in present:
            tools.append(_empty_tool(name))
            injected.add(name)
    body["tools"] = tools
    return frozenset(injected)


def prepare_body(payload: dict[str, Any], model: str,
                 ) -> tuple[dict[str, Any], frozenset[str]]:
    """上游请求体：深拷 messages、强制 stream、注入门禁工具。

    返回 `(body, injected_names)`。deepcopy 的必要性与 TRAE 相同：下游可能
    原地改写 messages，不能穿透回引擎持有的 `request.raw`（会话粘性指纹
    与续写都读它）。`stream` 无条件置 true——上游非流式会被门禁 403。
    """
    body = dict(payload)
    body["messages"] = copy.deepcopy(payload.get("messages"))
    body["model"] = model
    body["stream"] = True
    injected = ensure_gate_tools(body)
    return body, injected


class _InjectedToolFilter:
    """过滤模型对「我们伪造的 bash/read」的调用。

    OpenAI 流式 tool_call 分片：首个分片带 `id`/`function.name`，后续分片
    只有 `index` 与 `function.arguments`。因此按 `index` 记录已丢弃的调用，
    后续同名 index 的分片一并丢弃，避免残留半截 arguments。
    """

    def __init__(self, injected: frozenset[str]) -> None:
        self._injected = injected
        self._dropped_indexes: set[int] = set()

    def keep(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for call in tool_calls:
            index = call.get("index")
            index = index if isinstance(index, int) and not isinstance(index, bool) else None
            if index is not None and index in self._dropped_indexes:
                continue
            function = call.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and name in self._injected:
                if index is not None:
                    self._dropped_indexes.add(index)
                continue
            kept.append(call)
        return kept


class UpstreamHTTPError(base.UpstreamHTTPError):
    """Zen 上游非 2xx；kind() 走 Zen 的状态码规则。"""

    classify_status = staticmethod(zen_events.classify_status)


class ZenClient:
    """上游 HTTP 客户端。host / version 可覆盖，便于测试与灰度。"""

    def __init__(
        self,
        *,
        host: str = ZEN_HOST,
        version: str = MIN_OPENCODE_VERSION,
        free_suffix: str = FREE_MODEL_SUFFIX,
        models_cache_ttl: float = MODELS_CACHE_TTL_SECONDS,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.version = version
        self.free_suffix = free_suffix
        self.models_cache_ttl = models_cache_ttl
        # 上次判活的 (monotonic 时间, 模型表)；TTL 内直接复用，不再重探。
        self._models_cache: tuple[float, list[Model]] | None = None
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
        body, injected = prepare_body(payload, model)
        tool_filter = _InjectedToolFilter(injected)
        # 有实际 tool_call 被保留时才允许 finish_reason=tool_calls；
        # 全是伪工具调用时收敛为 stop，避免客户端收到空的 tool_calls 收尾。
        kept_tool_calls = False
        async with self._stream().stream(
            "POST", f"{self.host}{EP_CHAT}", json=body,
            headers=gate_headers(version=self.version),
        ) as response:
            if response.status_code >= 400:
                raw = await response.aread()
                raise UpstreamHTTPError(response.status_code, raw)
            async for frame in iter_frames(response.aiter_bytes()):
                for event in zen_events.parse_all_events(frame):
                    if event.kind is EventKind.TOOL_CALLS and event.tool_calls:
                        kept = tool_filter.keep(event.tool_calls)
                        if not kept:
                            continue
                        kept_tool_calls = True
                        event = Event(kind=EventKind.TOOL_CALLS, tool_calls=kept)
                    elif (event.kind is EventKind.FINISH
                          and event.finish_reason == "tool_calls" and not kept_tool_calls):
                        event = Event(kind=EventKind.FINISH, finish_reason="stop")
                    yield event

    async def fetch_models(self) -> list[Model]:
        """GET /zen/v1/models，只返回**探活可用**的免费模型。

        上游清单含全部（含付费）模型且无任何免费标记，直接透传会把 70 多个
        选不得的付费模型塞进 Playground（选中即 401）。此处按后缀收窄候选，
        再逐个探活，只有真正匿名可用的才对下游可见：
        `fetch_models` 的结果同时也是引擎登记模型归属的来源，过滤后扁平名
        请求不会再被路由到 zen 的付费模型上（否则 401 会误伤虚拟凭证）。

        判活集按 `models_cache_ttl` 缓存：服务层每 300s 到期重拉一次列表，此处
        直接复用上次判活结果，避免每 5 分钟就把十几个免费候选重探一遍。
        """
        if self._models_cache is not None:
            cached_at, cached_models = self._models_cache
            if time.monotonic() - cached_at < self.models_cache_ttl:
                return list(cached_models)
        response = await self._short().get(
            f"{self.host}{EP_MODELS}", headers=gate_headers(version=self.version))
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code, response.content)
        try:
            data = response.json()
        except ValueError as error:
            raise zen_events.UpstreamProtocolViolation("non-JSON models response") from error
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise zen_events.UpstreamProtocolViolation("models response missing data list")
        candidates: list[Model] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if (isinstance(model_id, str) and model_id
                    and model_id.lower().endswith(self.free_suffix)):
                owner = item.get("owned_by")
                # 免费层不消耗额度：显式给 0（而非 None），让列表 UI 显示 x0，
                # 排序时也天然排在最省的一档。
                candidates.append(Model(id=model_id,
                                        name=owner if isinstance(owner, str) else "",
                                        credit_rate=0.0))
        if not candidates:
            raise zen_events.UpstreamProtocolViolation("models api returned no free candidates")
        alive = await self._probe_alive([model.id for model in candidates])
        models = [model for model in candidates if model.id in alive]
        if not models:
            # 全部探活失败：多半是上游整体故障而非真的没有免费模型，
            # 抛出让上层用上次成功的缓存兜底，避免 zen 从列表里消失。
            raise zen_events.UpstreamProtocolViolation("no free model passed liveness probe")
        self._models_cache = (time.monotonic(), list(models))
        return models

    async def _probe_alive(self, model_ids: list[str]) -> set[str]:
        """并发探活：只留下**匿名请求返回 2xx** 的模型。

        付费模型恒 401、已下线 400、区域限制 403、上游故障 5xx，一律不算
        可用；连接/超时按不可用处理。用于模型列表展示，不触达业务统计。
        """
        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def one(model_id: str) -> str | None:
            async with semaphore:
                if await self._probe_one(model_id):
                    return model_id
                return None

        results = await asyncio.gather(*(one(model_id) for model_id in model_ids))
        return {model_id for model_id in results if model_id is not None}

    async def _probe_one(self, model_id: str) -> bool:
        """单模型探活：真发一次最小流式请求，2xx 即算可用。

        只看状态行、**不读 body**：进 context 即拿到响应头，随即关闭连接，
        不会等整段生成跑完（付费 401 / 下线 400 都在响应头就定性）。
        整体套 `asyncio.timeout`：首字过慢按不可用，不拖垮启动与 /health。
        """
        body, _ = prepare_body(
            {"messages": [{"role": "user", "content": PROBE_PROMPT}]}, model_id)
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                async with self._short().stream(
                    "POST", f"{self.host}{EP_CHAT}", json=body,
                    headers=gate_headers(version=self.version),
                ) as response:
                    return response.status_code < 300
        except (httpx.HTTPError, TimeoutError):
            return False


@dataclass(slots=True)
class ZenProvider:
    """Provider 协议实现（细接口，Q16=A）。

    Zen 免费层无凭证、无额度、无签到/成长：只实现对话与模型列表三条能力，
    其余任务靠「方法缺失」自然跳过（CheckinTask / GrowthTask / RefreshTask
    都用 getattr 探测，不需要为此改任务代码）。
    """

    client: ZenClient = field(default_factory=ZenClient)
    pacer: Any | None = None

    id: str = "zen"

    def import_credential(self, raw: dict) -> dict:
        """无凭证渠道：忽略入参，落库空对象（凭证行只是调度占位）。"""
        return {}

    def classify(self, status: int, body: bytes) -> ErrKind:
        return zen_events.classify_status(status, body)

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
        if self.pacer is not None:
            await self.pacer.wait_turn()
        async for event in self.client.stream_chat(payload, model):
            yield event

    async def aclose(self) -> None:
        await self.client.aclose()
