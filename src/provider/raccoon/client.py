"""小浣熊上游客户端 + Provider 实现。

标准 OpenAI 协议，只有三处渠道私有：Bearer 是网页会话 JWT、免费档看
`billing_effective_multiplier`、未知模型会静默回落（用上游回显的 `model`
兜住）。额度按请求扣积分（每笔 1 分，与 token 量无关）。
"""

from __future__ import annotations

import contextlib
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
#: 流水过滤口径：`all` 混消费；`income` 只留发放（签到/到期阶梯用后者）
BILL_TYPE_ALL = "all"
BILL_TYPE_INCOME = "income"
EP_HEARTBEAT = "/api/web/auth/v1/devices_current_heartbeat"
#: 网页会话级「页面访问」：每日积分延期/账单物化的触发点（心跳不算）
EP_USER_INFO = "/api/web/auth/v1/user_info"

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
        # 流水只取一次：明细（逐笔发放 + 到期）与到期阶梯同一份数据，避免
        # 两个请求之间上游又发了一笔导致面板与阶梯对不上。
        items = await self._fetch_income(token)
        packages = raccoon_activity.quota_packages(payload, items)
        # 阶梯与明细同源：组头「窗口内到期」按明细里每笔的**剩余**算，
        # 不再用「发放额」（那样会把已消耗的部分也算成将要过期）。
        ladder = raccoon_activity.ladder_from_packages(packages)
        return Quota(remaining=remaining, total=remaining, packages=packages or None,
                     expiry_ladder=ladder, probed_at=now)

    async def _fetch_income(self, token: str) -> list[Any] | None:
        """发放流水（`type=income`）；失败返回 None（不阻断额度探测）。"""
        try:
            return await self.fetch_bills(token, bill_type=BILL_TYPE_INCOME)
        except (UpstreamHTTPError, UpstreamProtocolViolation):
            return None

    async def fetch_bills(self, token: str,
                          limit: int = raccoon_activity.BILL_LOOKBACK,
                          bill_type: str = BILL_TYPE_ALL) -> list[Any]:
        """GET 积分流水（最近 `limit` 条）。

        `type` 是上游的**服务端过滤**：`all` 混着逐次调用的消费流水，
        `income` 只留发放（`daily_grant` / `reward_grant`）。签到与到期阶梯
        必须用 `income`——`all` 里消费占绝大多数，活跃账号的每日发放会被
        挤出条数窗口（实测 2026-11-10：055920 的 11-09 那笔排在第 62 条，
        60 条窗口里一条 grant 都看不到，于是误报「今日尚未发放」）。
        """
        response = await self._short.get(
            f"{self.host}{EP_BILLS}",
            params={"paging.limit": limit, "type": bill_type},
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

    async def visit(self, token: str) -> dict[str, Any]:
        """GET 网页会话 `user_info`：模拟一次页面访问（幂等、只读）。

        为什么需要它：每日 300 积分在 00:00 自动发放，但只有「当天登录过」
        才会延期到 7 天；设备心跳只刷新 `last_seen_at`，实测连续几天只发
        心跳时每笔积分仍在当日 24:00 清零。网页加载时的 `user_info` 请求
        是会话级访问，能触发延期与账单物化（实测调用后 4 分钟账单才出现）。
        """
        response = await self._short.get(
            f"{self.host}{EP_USER_INFO}",
            headers=request_headers(token, accept="application/json"))
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON user_info response") from error
        payload = data.get("data") if isinstance(data, dict) else None
        return payload if isinstance(payload, dict) else {}

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
        """「签到」= 页面访问 + 摸一次积分服务 + 核对流水。

        上游没有领取接口（见 `activity.py` 模块文档），故本方法是**幂等只读**
        的组合：`user_info`（会话级访问，触发延期/账单物化）→ balance（触发
        余额刷新）→ bills 核对 `daily_grant`。只认流水，不看 HTTP 200——
        否则会重演「界面显示签到成功、积分没到」。

        页面访问失败不阻断核对：活跃任务每天还会再访问一次；这里只是让
        签到本身也带一次「登录」，避免活跃任务被关闭时完全没有延期动作。
        """
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return CheckinResult(ok=False, message="凭证缺少 token")
        started = time.time()
        # ① 页面级访问：当天这笔积分只有被会话访问才延期到 7 天
        with contextlib.suppress(UpstreamHTTPError, UpstreamProtocolViolation):
            await self.client.visit(credential.access_token)
        # ② 触达积分服务：服务端在此刷新余额/补写当天流水
        await self.client.fetch_quota(credential.access_token)
        # ③ 读流水核对：签到是否真到账。必须用 `income` 过滤——`all` 里
        # 消费流水占绝大多数，活跃账号的每日发放会被挤出条数窗口。
        try:
            items = await self.client.fetch_bills(credential.access_token,
                                                  bill_type=BILL_TYPE_INCOME)
        except UpstreamHTTPError as error:
            return CheckinResult(ok=False, message=f"流水查询失败（HTTP {error.status}）")
        except UpstreamProtocolViolation:
            return CheckinResult(ok=False, message="流水响应格式异常")
        return raccoon_activity.checkin_result(items, started)

    async def checkin_status(self, credential_data: dict) -> dict:
        """只读签到状态：「今天有没有可用积分」+ 有多少。

        与 `checkin` 的区别：这里不判「是不是本次触发的」——状态查询只关心
        今日是否已签/有没有可用积分，不需要触发访问或余额探测。今天没有新
        发放但仍有未过期每日积分时也算已签（积分可用，只是今天还没落账）。
        """
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return {"active": False, "today_checked_in": False}
        items = await self.client.fetch_bills(credential.access_token,
                                              bill_type=BILL_TYPE_INCOME)
        now = raccoon_activity.now_cst()
        row = raccoon_activity.pick_today_grant(items, now)
        if row is not None:
            credit: float | None = float(row.get("points") or 0)
        else:
            active = raccoon_activity.pick_active_grants(items, now)
            credit = (sum(float(item.get("points") or 0) for item in active)
                      if active else None)
        return {
            "active": True,
            "today_checked_in": credit is not None,
            "today_credit": credit,
            "activity_name": "每日积分（服务端发放/登录延期）",
        }

    def checkin_scope(self, credential_data: dict) -> str:
        """签到隔离键：uid；身份未知返回空串（调用方回落凭证 ID）。

        与 CodeArts / Qoder 同取向：返回空串而非 `raccoon|`——后者会让同渠道
        所有身份未知的凭证算出同一 scope，`CheckinTask` 的 seen 集合只跑第一个。
        """
        uid = RaccoonCredential.from_dict(credential_data).uid
        return f"raccoon|{uid}" if uid else ""

    async def activity(self, credential_data: dict) -> ActivityResult:
        """活跃上报：页面访问 + 设备心跳（都幂等、只读）。

        为什么不是只发心跳：心跳只刷新设备 `last_seen_at`，不算「当天登录」；
        实测连续几天只发心跳时，每日积分仍会在当日 24:00 清零。网页会话级
        的 `user_info` 访问才触发延期。故先访问、再心跳；访问失败按活跃失败
        返回，让任务在同一小时窗口内重试（心跳本身仍会照发）。
        """
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return ActivityResult(ok=False, message="凭证缺少 token")
        token = credential.access_token
        visit_error: str | None = None
        try:
            await self.client.visit(token)
        except UpstreamHTTPError as error:
            visit_error = f"页面访问失败（HTTP {error.status}）"
        except UpstreamProtocolViolation:
            visit_error = "页面访问响应格式异常"
        try:
            payload = await self.client.heartbeat(
                token, raccoon_credential.derive_device_id(credential.uid))
        except UpstreamHTTPError as error:
            message = f"心跳失败（HTTP {error.status}）"
            if visit_error:
                message += f"；{visit_error}"
            return ActivityResult(ok=False, message=message)
        except UpstreamProtocolViolation:
            message = "心跳响应格式异常"
            if visit_error:
                message += f"；{visit_error}"
            return ActivityResult(ok=False, message=message)
        seen = payload.get("last_seen_at")
        trusted = payload.get("trusted_device")
        detail = "设备心跳已上报"
        if isinstance(seen, str) and seen:
            detail += f"（上游 last_seen_at={seen}）"
        if trusted is False:
            detail += "；注意：该设备未被上游信任"
        if visit_error:
            return ActivityResult(ok=False, user_id=credential.uid,
                                  message=f"{visit_error}；{detail}")
        return ActivityResult(ok=True, message=f"页面访问已上报；{detail}",
                              user_id=credential.uid)

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