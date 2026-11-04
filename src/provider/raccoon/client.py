"""小浣熊上游客户端 + Provider 实现。

标准 OpenAI 协议，只有三处渠道私有：Bearer 是网页会话 JWT、免费档看
`billing_effective_multiplier`、未知模型会静默回落（用上游回显的 `model`
兜住）。额度按请求扣积分（每笔 1 分，与 token 量无关）。
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
from ...provider.base import ActivityResult, CheckinResult, ErrKind, Event, Model, Quota
from ...tasks.pacer import paced
from ..lazy_clients import LazyClients
from . import activity as raccoon_activity
from . import credential as raccoon_credential
from . import events as raccoon_events
from .credential import RaccoonCredential

#: 上游主机（网页与 API 同域）
RACCOON_HOST = "https://xiaohuanxiong.com"
EP_CHAT = "/api/web/llm/v2/chat/completions"
EP_MODELS = "/api/web/llm/v2/model_catalog"
EP_BALANCE = "/api/web/points/v1/balance"
EP_BILLS = "/api/web/points/v2/bills"
EP_HEARTBEAT = "/api/web/auth/v1/devices_current_heartbeat"

#: 官方 LLM 子进程的 User-Agent（openai SDK 形态）；不带也不影响调用，
#: 带上只是为了不把自己标成脚本——本渠道没有其它指纹伪装。
OFFICIAL_UA = "AsyncOpenAI/Python 2.8.0"


def prepare_body(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """下游请求体 → 上游请求体：深拷 messages、写死 model、强制 stream。

    `messages` 必须深拷贝：下游可能原地改写，不能穿透回引擎持有的
    `request.raw`（会话粘性指纹与续写都读它）。非流式由引擎聚合，故 `stream`
    无条件置 true——与其余渠道同口径，少一条分支。
    """
    body = dict(payload)
    body["messages"] = copy.deepcopy(payload.get("messages"))
    body["model"] = model
    body["stream"] = True
    return body


def request_headers(token: str, *, accept: str = "text/event-stream") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": accept,
        "User-Agent": OFFICIAL_UA,
    }


class UpstreamHTTPError(base.UpstreamHTTPError):
    """上游非 2xx；`kind()` 走本渠道的状态码规则。"""

    classify_status = staticmethod(raccoon_events.classify_status)


class UpstreamProtocolViolation(base.UpstreamProtocolViolation):
    """上游返回了本渠道约定之外的东西（模型回显不一致等）。"""


class RaccoonClient(LazyClients):
    """上游 HTTP 客户端。host 可覆盖，便于测试与灰度。"""

    def __init__(
        self,
        *,
        host: str = RACCOON_HOST,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
        proxy: str | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self._stream_client = stream_client
        self._short_client = short_client
        self._proxy = proxy

    async def stream_chat(self, payload: dict[str, Any], model: str,
                          token: str) -> AsyncIterator[Event]:
        """POST chat/completions 并逐事件产出；非 2xx 抛 UpstreamHTTPError。

        **模型回显校验**：上游对未知模型名静默回落默认模型（实测 2026-11-04），
        首帧带 `model` 时立即比对，不一致抛协议违规——宁可这次请求失败，也不让
        客户端「问 A 答 B」。
        """
        body = prepare_body(payload, model)
        expected = model.strip().lower()
        async with self._stream.stream(
            "POST", f"{self.host}{EP_CHAT}", json=body,
            headers=request_headers(token),
        ) as response:
            if response.status_code >= 400:
                raw = await base.read_body_bounded(response)
                raise UpstreamHTTPError(response.status_code, raw,
                                        **base.retry_hints(response.headers))
            async for frame in iter_frames(response.aiter_bytes()):
                for event in raccoon_events.parse_frame(frame):
                    echoed = event.model
                    if isinstance(echoed, str) and echoed.strip() \
                            and echoed.strip().lower() != expected:
                        raise UpstreamProtocolViolation(
                            f"model echo mismatch: asked {model!r}, "
                            f"upstream returned {echoed!r}")
                    yield event

    async def fetch_models(self, token: str) -> list[Model]:
        """GET 模型目录：一层的 `categories[].models[]`，按 id 去重。"""
        response = await self._short.get(
            f"{self.host}{EP_MODELS}",
            headers=request_headers(token, accept="application/json"))
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON models response") from error
        payload = data.get("data") if isinstance(data, dict) else None
        categories = payload.get("categories") if isinstance(payload, dict) else None
        if not isinstance(categories, list):
            raise UpstreamProtocolViolation("models response missing categories")
        models: dict[str, Model] = {}
        for category in categories:
            if not isinstance(category, dict):
                continue
            items = category.get("models")
            if not isinstance(items, list):
                continue
            for item in items:
                model = _model_from_item(item)
                if model is not None:
                    models.setdefault(model.id.lower(), model)
        if not models:
            raise UpstreamProtocolViolation("models api returned no models")
        return list(models.values())

    async def fetch_quota(self, token: str) -> Quota:
        """GET 积分余额 + 流水 → 额度（含四个池的明细与逐笔到期阶梯）。

        上游只给「当前可用」一个总数，没有总量/消费口径；`total=remaining`
        （健康度恒 100 直到 0）。**不编造**其它来源：日额度耗尽与余额耗尽是
        两回事，猜错会让调度把备用渠道误判为已耗尽。

        明细（`packages`）来自 balance 的四个池，逐笔到期（`expiry_ladder`）
        来自流水的 `daily_grant`——两者口径不同，不能互相换算，故分开取。
        流水失败**不影响**余额结论（余额是本接口的主要产物，到期阶梯只是
        调度排序的加分项）：流水拿不到就退化成「无阶梯」。
        """
        response = await self._short.get(
            f"{self.host}{EP_BALANCE}",
            headers=request_headers(token, accept="application/json"))
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON balance response") from error
        payload = data.get("data") if isinstance(data, dict) else None
        points = payload.get("available_points") if isinstance(payload, dict) else None
        if not isinstance(points, (int, float)) or isinstance(points, bool):
            raise UpstreamProtocolViolation("balance response missing available_points")
        remaining = float(points)
        now = int(time.time())
        packages = raccoon_activity.quota_packages(payload)
        ladder = await self._fetch_expiry_ladder(token)
        return Quota(remaining=remaining, total=remaining, packages=packages or None,
                     expiry_ladder=ladder, probed_at=now)

    async def _fetch_expiry_ladder(self, token: str) -> list[tuple[int, float]] | None:
        """流水 → 逐笔到期阶梯；失败返回 None（不阻断额度探测）。"""
        try:
            items = await self.fetch_bills(token)
        except (UpstreamHTTPError, UpstreamProtocolViolation):
            return None
        return raccoon_activity.expiry_ladder_from_bills(items)

    async def fetch_bills(self, token: str,
                          limit: int = raccoon_activity.BILL_LOOKBACK) -> list[Any]:
        """GET 积分流水（最近 `limit` 条，`type=all`）。"""
        response = await self._short.get(
            f"{self.host}{EP_BILLS}",
            params={"paging.limit": limit, "type": "all"},
            headers=request_headers(token, accept="application/json"))
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON bills response") from error
        payload = data.get("data") if isinstance(data, dict) else None
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise UpstreamProtocolViolation("bills response missing items")
        return items

    async def heartbeat(self, token: str, device_id: str) -> dict[str, Any]:
        """POST 设备心跳（网页端真实接口，幂等）。

        缺 `x-client-*` 头族会回 `100002 params_invalid_error` /
        `param client headers required invalid`——实测 2026-11-04。
        """
        headers = request_headers(token, accept="application/json")
        headers.update(raccoon_activity.heartbeat_headers(device_id))
        response = await self._short.post(
            f"{self.host}{EP_HEARTBEAT}", headers=headers)
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON heartbeat response") from error
        payload = data.get("data") if isinstance(data, dict) else None
        return payload if isinstance(payload, dict) else {}


def _model_from_item(item: Any) -> Model | None:
    """目录条目 → 中立 Model；缺 id 的条目跳过。

    倍率取 `billing_effective_multiplier`（含限时优惠后的**实际**倍率）：
    0 就是免费（调度器会优先选），非 0 透传。上下文/输出上限上游不提供，
    留 None——**不猜**（本仓压缩只在有上限时才裁剪）。
    """
    if not isinstance(item, dict):
        return None
    model_id = item.get("name") or item.get("model_name")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    tags = item.get("tags")
    tag_set = {str(tag).lower() for tag in tags} if isinstance(tags, list) else set()
    rate = item.get("billing_effective_multiplier")
    return Model(
        id=model_id.strip(),
        name=model_id.strip(),
        credit_rate=(float(rate)
                     if isinstance(rate, (int, float)) and not isinstance(rate, bool)
                     else None),
        supports_images=("vision" in tag_set) if tag_set else None,
        supports_tool_call=True,
        supports_reasoning=("reasoning" in tag_set) if tag_set else None,
    )


@dataclass(slots=True)
class RaccoonProvider:
    """Provider 协议实现。

    登录轨道不实现：凭证来自浏览器会话，只能由用户从 DevTools 复制后经
    「JSON 导入」录入（见 `credential.py`）。因此这里没有 `start_auth` /
    `poll_auth` / `complete_callback`——写了也只是永远不被调用的死代码；
    与 Zen/Kilo 的「按方法缺失自然跳过」同一分工。
    """

    client: RaccoonClient = field(default_factory=RaccoonClient)
    pacer: Any | None = None

    id: str = "raccoon"
    # 最近一次目录里的模型（小写）；用于发请求前的廉价校验。空集表示还没拉过
    # 目录，放行（真实保护靠上游回显的 `model`）。`init=False` 让它不参与构造。
    known_models: set[str] = field(default_factory=set, init=False)

    def import_credential(self, raw: dict) -> dict:
        """导入入参 → 落库形态；token 为空直接报错（不让空凭证进池）。"""
        credential = RaccoonCredential.from_import(raw)
        if not credential.access_token:
            raise UpstreamProtocolViolation(
                "missing access token: paste the xhx_token cookie value "
                "or {\"access_token\": \"...\"}")
        return credential.to_dict()

    def credential_expires_at(self, credential_data: dict) -> int:
        """JWT `exp`：供管理台提示「30 天后要重新取」；0 = 未知。"""
        return RaccoonCredential.from_dict(credential_data).expires_at

    def classify(self, status: int, body: bytes) -> ErrKind:
        return raccoon_events.classify_status(status, body)

    async def probe_quota(self, credential_data: dict) -> Quota:
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return Quota(probe_failed=True, probed_at=int(time.time()))
        return await self.client.fetch_quota(credential.access_token)

    # ---------------------------------------------------------- 签到 / 活跃

    async def checkin(self, credential_data: dict) -> CheckinResult:
        """「签到」= 摸一次积分服务触发懒发放，再核对当日流水是否真到账。

        上游没有领取接口（见 `activity.py` 模块文档），故本方法是**幂等只读**
        的组合：balance 触发补写 → bills 核对 `daily_grant`。只认流水，
        不看 HTTP 200——否则会重演「界面显示签到成功、积分没到」。
        """
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return CheckinResult(ok=False, message="凭证缺少 token")
        started = time.time()
        # ① 触达积分服务：服务端在此补写当天的 daily_grant
        await self.client.fetch_quota(credential.access_token)
        # ② 读流水核对：签到是否真到账
        try:
            items = await self.client.fetch_bills(credential.access_token)
        except UpstreamHTTPError as error:
            return CheckinResult(ok=False, message=f"流水查询失败（HTTP {error.status}）")
        except UpstreamProtocolViolation:
            return CheckinResult(ok=False, message="流水响应格式异常")
        return raccoon_activity.checkin_result(items, started)

    async def checkin_status(self, credential_data: dict) -> dict:
        """只读签到状态：「今天发放过没有」+ 发了多少。

        与 `checkin` 的区别：这里不判「是不是本次触发的」——状态查询只关心
        今日是否已发放，不需要触发懒发放（`started` 参数在 checkin 里才用）。
        """
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return {"active": False, "today_checked_in": False}
        items = await self.client.fetch_bills(credential.access_token)
        row = raccoon_activity.pick_today_grant(items, raccoon_activity.now_cst())
        if row is None:
            return {"active": True, "today_checked_in": False,
                    "today_credit": None,
                    "activity_name": "每日积分（服务端懒发放）"}
        points = row.get("points")
        return {
            "active": True,
            "today_checked_in": True,
            "today_credit": float(points) if isinstance(points, (int, float))
            and not isinstance(points, bool) else None,
            "activity_name": "每日积分（服务端懒发放）",
        }

    def checkin_scope(self, credential_data: dict) -> str:
        """签到隔离键：uid；身份未知返回空串（调用方回落凭证 ID）。

        与 CodeArts / Qoder 同取向：返回空串而非 `raccoon|`——后者会让同渠道
        所有身份未知的凭证算出同一 scope，`CheckinTask` 的 seen 集合只跑第一个。
        """
        uid = RaccoonCredential.from_dict(credential_data).uid
        return f"raccoon|{uid}" if uid else ""

    async def activity(self, credential_data: dict) -> ActivityResult:
        """活跃上报：发一次设备心跳（网页端真实接口，幂等）。

        上游没有「对话事件上报」，心跳是唯一可用的活跃手段；`user_info` 的
        `last_login_at` 会随之刷新。device_id 由 uid 稳定派生，随机值会被
        当成新设备（风控）。
        """
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return ActivityResult(ok=False, message="凭证缺少 token")
        try:
            payload = await self.client.heartbeat(
                credential.access_token,
                raccoon_credential.derive_device_id(credential.uid))
        except UpstreamHTTPError as error:
            return ActivityResult(ok=False, message=f"心跳失败（HTTP {error.status}）")
        except UpstreamProtocolViolation:
            return ActivityResult(ok=False, message="心跳响应格式异常")
        seen = payload.get("last_seen_at")
        trusted = payload.get("trusted_device")
        detail = "设备心跳已上报"
        if isinstance(seen, str) and seen:
            detail += f"（上游 last_seen_at={seen}）"
        if trusted is False:
            detail += "；注意：该设备未被上游信任"
        return ActivityResult(ok=True, message=detail, user_id=credential.uid)

    def activity_scope(self, credential_data: dict) -> str:
        """活跃上报隔离键：uid（同账号多凭证共享「每天一次」）。"""
        uid = RaccoonCredential.from_dict(credential_data).uid
        return f"raccoon|{uid}" if uid else ""

    async def list_models(self, credential_data: dict) -> list[Model]:
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            raise UpstreamProtocolViolation("raccoon credential missing token")
        models = await self.client.fetch_models(credential.access_token)
        self.known_models = {model.id.lower() for model in models}
        return models

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            raise UpstreamProtocolViolation("raccoon credential missing token")
        if self.known_models and model.strip().lower() not in self.known_models:
            # 目录里没有的模型：上游会静默回落默认模型并 200。这里按「模型不存在」
            # 处理（400/INVALID），不进上游也不冷却凭证。
            raise UpstreamHTTPError(
                404, b'{"error":{"message":"model not found in raccoon catalog"}}')
        key = credential.uid or "raccoon"
        async with paced(self.pacer, key):
            async for event in self.client.stream_chat(
                    payload, model, credential.access_token):
                yield event

    async def aclose(self) -> None:
        await self.client.aclose()