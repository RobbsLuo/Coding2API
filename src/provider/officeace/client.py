"""officeAce 上游客户端：标准 OpenAI 透传 + 积分/签到控制面。

与其余渠道的关键差异：

* **聊天是标准 OpenAI 协议**（`{gateway}/v2/chat/completions` + Basic 认证），
  请求/响应形状与 zen / kilo 完全一致，故复用 `engine/sse` 与
  `openai_chunk`，本包不写私有信封解析。
* **认证是 Basic app_key:app_secret**，这对凭据**不过期**；临时 AK/SK 只用于
  控制面。混用会得到 `APIG.0303 app not found`（这正是最初试图复用 CodeArts
  凭证时踩的坑，实测）。
* **限流走伪 200**：HTTP 200 + `data:{"error":...}`，且实测「首帧正常、第二帧
  才是错误」也出现过。故流解析必须把错误帧照常产出（见 `events.parse_all_events`），
  不能假设 2xx 就是好流。
* **模型级拒绝（81004/81009）不牵连整条凭据**：上游回 403/404，按状态码直接
  判 DEAD 会把整条凭据打死，而真实原因只是「这个号没这个模型」。
* **推理模型占多数**（可用 11 个里 9 个是推理模型），上游把 `max_tokens` 当
  「推理 + 正文」总预算，给小了正文直接为空 → 见 `events.clamp_max_tokens`。

积分是**主账**（不像 CodeArts 有并行的每日 token 池），故 `Quota.remaining` /
`total` 直接就是积分。
"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from ...engine.sse import iter_frames
from ...provider import base
from ...provider.base import (
    Event,
    Model,
    Quota,
    UpstreamProtocolViolation,
    UpstreamReloginRequired,
)
from ...provider.proxy import build_client
from .. import naming
from . import events as officeace_events
from .credential import OfficeAceCredential, merge_refreshed
from .events import (
    EP_BONUS_CLAIM,
    EP_CHAT,
    EP_MODELS,
    EP_SUBSCRIPTION,
    EP_TOKEN_RATES,
    SIGN_REGION,
)
from .oauth import LoginConfig, TokenEndpointError
from .signer import SignCredential, build_headers

logger = logging.getLogger(__name__)

# 流式无总超时防长流截断；短请求 30s 防悬挂（与其余渠道同策略）。
# 写超时覆盖上传请求体阶段，不覆盖首字等待（大上下文请求体可达数 MB）。
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)
SHORT_TIMEOUT = httpx.Timeout(30.0)

#: 签到活动展示名（上游没给活动名时的兜底）。
DEFAULT_CHECKIN_NAME = "每日签到有礼"

#: 签到请求体。**不能是空 body**：官方调用点是无 body 的 POST，但实测那样
#: 返回 500 `OfficeAce.11020001`；必须给一个空 JSON 对象（{}）才回 200。
#: 该字节串同时参与 V11 签名，故必须与 `content=` 逐字节一致。
CLAIM_BODY = b"{}"


def prepare_body(payload: dict[str, Any], model: str) -> tuple[dict[str, Any], int | None]:
    """下游请求体 → 上游请求体，并返回「抬过/夹过」后的 `max_tokens`。

    deepcopy 的必要性与 TRAE/Zen 相同：下游可能原地改写 messages，不能穿透回
    引擎持有的 `request.raw`（会话粘性指纹与续写都读它）。`stream` 无条件置
    true——非流式由引擎聚合。

    `max_tokens` 按 `events.clamp_max_tokens` 改写（推理预算 + 上游硬上限），
    `max_completion_tokens` 一并归一成 `max_tokens`（上游只认后者）。
    """
    body = dict(payload)
    body["messages"] = copy.deepcopy(payload.get("messages"))
    body["model"] = model
    body["stream"] = True
    raw_max = payload.get("max_tokens")
    if raw_max is None:
        raw_max = payload.get("max_completion_tokens")
    body.pop("max_completion_tokens", None)
    sent: int | None = None
    if isinstance(raw_max, int) and not isinstance(raw_max, bool) and raw_max > 0:
        sent = officeace_events.clamp_max_tokens(raw_max, model)
        body["max_tokens"] = sent
    return body, sent


def request_headers(credential: OfficeAceCredential) -> dict[str, str]:
    """聊天请求头：Basic(app_key:app_secret) + SSE。"""
    return {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": credential.authorization,
    }


class UpstreamHTTPError(base.UpstreamHTTPError):
    """officeAce 上游非 2xx；kind() 走 officeAce 的状态码 + 业务码规则。"""

    classify_status = staticmethod(officeace_events.classify_status)


class OfficeAceClient:
    """上游 HTTP 客户端。主机可覆盖，便于测试与灰度。"""

    def __init__(
        self,
        *,
        cloud_base: str = officeace_events.AGENTARTS_HOST,
        gateway: str = officeace_events.DEFAULT_GATEWAY_HOST,
        login: LoginConfig | None = None,
        region: str = SIGN_REGION,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
        now: Callable[[], int] | None = None,
        proxy: str | None = None,
    ) -> None:
        self.cloud_base = cloud_base.rstrip("/")
        self.gateway = gateway.rstrip("/")
        self.login = login or LoginConfig(cloud_base=self.cloud_base)
        self.region = region
        self._stream_client = stream_client
        self._short_client = short_client
        self._now = now or (lambda: int(time.time()))
        self._proxy = proxy
        # 模型费率负缓存：{小写模型名: cost_coefficient}；失败不清空（宁可旧值）
        self._rates: dict[str, float] = {}

    def _stream(self) -> httpx.AsyncClient:
        if self._stream_client is None:
            self._stream_client = build_client(timeout=STREAM_TIMEOUT,
                                               proxy=self._proxy)
        return self._stream_client

    def _short(self) -> httpx.AsyncClient:
        if self._short_client is None:
            self._short_client = build_client(timeout=SHORT_TIMEOUT,
                                              proxy=self._proxy)
        return self._short_client

    async def aclose(self) -> None:
        for client in (self._stream_client, self._short_client):
            if client is not None:
                await client.aclose()

    # ---------------------------------------------------------------- 聊天

    async def stream_chat(self, credential: OfficeAceCredential,
                          payload: dict[str, Any],
                          model: str) -> AsyncIterator[Event]:
        """POST `/v2/chat/completions` 并逐事件产出中立 Event。

        非 2xx 抛 `UpstreamHTTPError`；2xx 里的错误帧照常产出 ERROR 事件
        （伪 200）。
        """
        if not credential.model_base_url:
            raise UpstreamProtocolViolation(
                "credential missing model_base_url",
                public="该凭证缺少 officeAce 网关地址，请重新登录该账号")
        url = f"{self._gateway_of(credential)}{EP_CHAT}"
        body, bumped = prepare_body(payload, model)
        if bumped is not None and bumped != payload.get("max_tokens"):
            logger.info("officeAce 调整 max_tokens：客户端 %s → 上游 %s（model=%s）",
                        payload.get("max_tokens", payload.get("max_completion_tokens")),
                        bumped, model)
        async with self._stream().stream(
            "POST", url, json=body, headers=request_headers(credential),
        ) as response:
            if response.status_code >= 400:
                raw = await base.read_body_bounded(response)
                raise UpstreamHTTPError(response.status_code, raw,
                                        **base.retry_hints(response.headers))
            async for frame in iter_frames(response.aiter_bytes()):
                for event in officeace_events.parse_all_events(frame):
                    yield event

    # ------------------------------------------------------------ 模型发现

    async def fetch_models(self, credential: OfficeAceCredential) -> list[Model]:
        """`GET /v2/models`（Basic 认证）→ 中立 Model 列表。

        **不做主动探活**：实测目录 34 个里只有 11 个真能用（其余 `81004` /
        `81009`），但探活要真发推理、消耗积分，而额度是稀缺资源。模型级拒绝
        由引擎在真实请求上按 `BLOCKED` 负缓存处理（见 `classify_status`），
        比开机烧 34 次推理更省。
        """
        if not credential.model_base_url:
            raise UpstreamProtocolViolation(
                "credential missing model_base_url",
                public="该凭证缺少 officeAce 网关地址，请重新登录该账号")
        url = f"{self._gateway_of(credential)}{EP_MODELS}"
        response = await self._short().get(
            url, headers={"Authorization": credential.authorization,
                          "Accept": "application/json"})
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON models response") from error
        items = officeace_events.parse_models(payload)
        rates = await self._fetch_rates_quietly(credential)
        # `parse_models` 已保证每条都带 id，故此处不再筛空 id（筛了是死分支）。
        models = _models_from_items(items, rates)
        if not models:
            raise UpstreamProtocolViolation("models api returned no models")
        return models

    async def _fetch_rates_quietly(self, credential: OfficeAceCredential,
                                   ) -> dict[str, float]:
        """费率目录（best-effort）：失败沿用上次，绝不让模型列表整体失败。"""
        if not credential.has_control_plane:
            return dict(self._rates)
        try:
            data = await self._v11_json("GET", EP_TOKEN_RATES, credential)
        except (UpstreamHTTPError, UpstreamProtocolViolation) as error:
            logger.warning("officeAce 模型费率拉取失败（沿用上次）: %s", error)
            return dict(self._rates)
        # `parse_rates` 自己是宽容的（坏条目跳过、结构缺字段当空表）。因此
        # **空结果不覆盖旧值**：真结构异常与「上游确实没给费率」都会得到空表，
        # 而费率只影响展示，留着上一轮的值比清空更有用（清空会让整张模型列表
        # 的倍率列突然全空）。
        parsed = officeace_events.parse_rates(data)
        if parsed:
            self._rates = parsed
        return dict(self._rates)

    # ------------------------------------------------------------ 额度探测

    async def probe_quota(self, credential: OfficeAceCredential) -> Quota:
        """读积分额度（**只读**：不领奖励、不刷新凭据）。

        刷新（一次性 refresh_token）唯一归 `tasks.refresh.RefreshTask`——
        此处顺手刷新会让一张单次票被两个地方各消费一次，后到的必然报
        `the refresh token has been used`（CodeArts 上实测过这个事故）。

        没有控制面凭据（只有聊天 app key）时回 `probe_failed`：那是「探不到」
        而不是「没额度」，绝不能让它显示成 0 被调度器当耗尽。
        """
        moment = self._now()
        if not credential.has_control_plane:
            return Quota(probe_failed=True, probed_at=moment)
        try:
            payload = await self._v11_json("GET", EP_SUBSCRIPTION, credential,
                                           headers={"x-subscription-type": "v2"})
            summary = officeace_events.summarize_subscription(payload, now=moment)
        except UpstreamHTTPError as error:
            if error.status in (401, 403):
                # app key 坏 / 临时凭证过期：前者 DEAD，后者等 RefreshTask
                raise
            logger.warning("officeAce 订阅查询失败: %s", error)
            return Quota(probe_failed=True, probed_at=moment)
        except UpstreamProtocolViolation as error:
            logger.warning("officeAce 订阅结构异常（按探测失败处理）: %s", error)
            return Quota(probe_failed=True, probed_at=moment)
        return Quota(
            remaining=summary["remaining"],
            total=summary["total"],
            cycle_end=summary["cycle_end"],
            expiry_ladder=summary["expiry_ladder"] or None,
            packages=summary["packages"] or None,
            probed_at=moment,
        )

    # ---------------------------------------------------------------- 签到

    async def fetch_checkin_status(self, credential: OfficeAceCredential,
                                   ) -> dict[str, Any]:
        """签到状态 → 管理台展示用的 dict（**只读**，不发积分）。

        没有控制面凭据时给「非活动」而不是报错：管理台照常能打开，签到任务
        在 `claim_bonus` 里给出明确失败原因。
        """
        inactive = {"active": False, "today_checked_in": False,
                    "today_credit": None, "activity_name": DEFAULT_CHECKIN_NAME}
        if not credential.has_control_plane:
            return inactive
        payload = await self._v11_json("GET", EP_SUBSCRIPTION, credential,
                                       headers={"x-subscription-type": "v2"})
        summary = officeace_events.summarize_subscription(payload, now=self._now())
        daily = summary.get("daily")
        if not isinstance(daily, dict):
            return inactive
        claimed = bool(daily.get("claimed"))
        return {
            "active": True,
            "today_checked_in": claimed,
            "today_credit": float(daily.get("credit") or 0.0) if claimed else None,
            "total_credits": summary["remaining"],
            "activity_name": str(daily.get("activity_name") or DEFAULT_CHECKIN_NAME),
        }

    async def claim_bonus(self, credential: OfficeAceCredential,
                          ) -> base.CheckinResult:
        """`POST /v1/subscription/bonus/claim` 领当日奖励积分。

        **这是写操作**：调用即真的发积分（实测 4000 新用户 + 1000 每日）。
        上游对当日重复领取是幂等的（回 200 + 同样的活动列表，`current_value`
        不变），故不需要本地再记「今天领过」。

        请求体必须是空 JSON 对象且带 `Content-Type: application/json`：实测裸
        POST（或空 body）会被判 500 `OfficeAce.11020001`，见 `CLAIM_BODY`。
        """
        if not credential.has_control_plane:
            return base.CheckinResult(
                ok=False,
                message=("该凭证只有聊天凭据（无临时 AK/SK），无法签到。"
                         "请用「登录渠道账号」重新添加该账号。"))
        moment = self._now()
        before = await self._bonus_snapshot(credential)
        payload = await self._v11_json(
            "POST", EP_BONUS_CLAIM, credential,
            headers={"x-subscription-type": "v2",
                     "Content-Type": "application/json;charset=utf8"},
            body=CLAIM_BODY)
        after = officeace_events.bonus_remaining(payload)
        # 「领到了多少」只能按**剩余量的增量**算：`points` 是活动累计发放量，
        # 由多个渠道（桌面端签到、活动页手动领）共同贡献，拿它当本次收获会虚高。
        gained = sum(max(0.0, after.get(activity, 0.0) - before.get(activity, 0.0))
                     for activity in after)
        already = all(after.get(activity, 0.0) <= before.get(activity, 0.0)
                      for activity in after) if before else False
        if gained <= 0:
            if already or before:
                return base.CheckinResult(ok=True, already_checked_in=True, code=0,
                                          message="今日已签到")
            # 上游既没给活动也没给增量：不封账，让任务下轮再试（漏签的代价
            # 高于多刷几行日志）。
            return base.CheckinResult(
                ok=False, message="签到响应里没有奖励活动，稍后重试")
        return base.CheckinResult(
            ok=True, credit=gained, code=0,
            message=f"签到成功，领到 {gained:g} 积分",
            seal_until=_next_beijing_midnight(moment))

    async def _bonus_snapshot(self, credential: OfficeAceCredential) -> dict[str, float]:
        """签到前的奖励余额（best-effort：失败按空表，增量退化为「全算新领」）。"""
        try:
            payload = await self._v11_json("GET", EP_SUBSCRIPTION, credential,
                                           headers={"x-subscription-type": "v2"})
        except (UpstreamHTTPError, UpstreamProtocolViolation) as error:
            logger.warning("officeAce 签到前余额快照失败: %s", error)
            return {}
        return officeace_events.bonus_remaining(payload)

    # ---------------------------------------------------------------- 刷新

    async def refresh_token(self, credential: OfficeAceCredential,
                            ) -> OfficeAceCredential:
        """refresh_token 换新 STS 凭证，**回写轮转后的 refresh_token**。

        `client_id` / DPoP 私钥随 refresh_token 绑定，必须沿用凭证里的原值；
        否则上游回 `invalid client id` / `InvalidDPoPHeader`。

        只有终态错误（票已消费 / 绑定项不符）翻成 `UpstreamReloginRequired`
        让 `RefreshTask` 停手；5xx / 网络失败原样上抛，下一轮可重试。
        """
        from .oauth import OfficeAceOAuth

        oauth = OfficeAceOAuth(self.login, client=self._short())
        try:
            tokens = await oauth.refresh_tokens(
                refresh_token=credential.refresh_token,
                dpop_private_jwk=credential.dpop_private_jwk,
                client_id=credential.client_id)
        except TokenEndpointError as error:
            reason = officeace_events.relogin_required(error.body)
            if not reason:
                raise
            raise UpstreamReloginRequired(reason) from error
        return merge_refreshed(credential, tokens)

    # ---------------------------------------------------------------- 内部

    def _credential_sign(self, credential: OfficeAceCredential) -> SignCredential:
        return SignCredential(
            access_key_id=credential.access_key_id,
            secret_access_key=credential.secret_access_key,
            security_token=credential.security_token,
            project_id=credential.project_id)

    def _gateway_of(self, credential: OfficeAceCredential) -> str:
        """该凭证的模型网关（凭证自带优先，缺省回装配默认值）。

        去掉尾部的 `/v2`：两条导入路径给的地址形状不同——`client-permission-
        validate` 下发的是裸主机（`https://modelgw-xxx...`），而桌面端的
        `models.json` 里 `api_base` 自带 `/v2`。不归一就会拼出
        `/v2/v2/chat/completions`（实测 404）。
        """
        base = (credential.model_base_url or self.gateway).rstrip("/")
        if base.endswith("/v2"):
            base = base[:-3]
        return base

    async def _v11_json(self, method: str, path: str,
                        credential: OfficeAceCredential,
                        *, headers: dict[str, str] | None = None,
                        body: bytes | None = None) -> dict[str, Any]:
        """对网关发一次 V11 签名请求（控制面：订阅 / 费率 / 签到）。"""
        if not credential.has_control_plane:
            raise UpstreamProtocolViolation(
                "credential missing access_key_id / secret_access_key")
        url = f"{self._gateway_of(credential)}{path}"
        signed = build_headers(
            self._credential_sign(credential), method=method, url=url,
            headers=headers or {}, payload=body or b"",
            x_sdk_date=_sdk_date(self._now()), region=self.region)
        response = await self._short().request(
            method, url, headers=signed, content=body if body is not None else None)
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response),
                                    **base.retry_hints(response.headers))
        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation(
                f"non-JSON response from {path}") from error
        if not isinstance(payload, dict):
            raise UpstreamProtocolViolation(
                f"response from {path} is not an object")
        return payload


def _models_from_items(items: list[dict[str, Any]],
                       rates: dict[str, float]) -> list[Model]:
    """模型条目 + 费率表 → 中立 Model 列表（排序保持上游顺序）。"""
    models: list[Model] = []
    for item in items:
        model_id = officeace_events.model_id_from_item(item)
        models.append(_model_from_item(
            item, model_id, rates.get(model_id.strip().lower())))
    return models


def _model_from_item(item: dict[str, Any], model_id: str,
                     coefficient: float | None) -> Model:
    """上游模型条目 + 费率 → 中立 Model（元数据有则透传，无则留 None）。

    `credit_rate` 用官方 `cost_coefficient`：它是「这个模型比基准贵几倍」的
    系数，正是展示层要的「消耗倍数」语义，与 CodeBuddy 的 `x0.29 credits`
    同口径。
    """
    def as_int(*keys: str) -> int | None:
        for key in keys:
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    def as_bool(*keys: str) -> bool | None:
        for key in keys:
            value = item.get(key)
            if isinstance(value, bool):
                return value
        return None

    raw_name = item.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else ""
    return Model(
        id=model_id,
        name=name or naming.display_model_name(model_id) or model_id,
        credit_rate=coefficient,
        max_input_tokens=as_int("context_length", "max_context_tokens",
                                "max_input_tokens"),
        max_output_tokens=as_int("max_output_tokens", "max_tokens"),
        supports_images=as_bool("supports_images", "vision"),
        supports_tool_call=as_bool("supports_tool_call", "tool_call"),
        supports_reasoning=as_bool("supports_reasoning", "reasoning"),
    )


def _sdk_date(moment: int | None = None) -> str:
    """`X-Sdk-Date`：UTC `YYYYMMDDTHHMMSSZ`。"""
    return time.strftime("%Y%m%dT%H%M%SZ",
                        time.gmtime(moment if moment is not None else time.time()))


def _next_beijing_midnight(moment: int) -> int:
    """下一个北京时间 0 点的 epoch 秒（签到封账用）。

    上游的 daily 奖励 `expired_time` 是当日 23:59:59（UTC+8），次日会换一条
    新的；封到「下一个北京 0 点」正好让任务在新活动出现时立刻再跑一轮。
    """
    offset = officeace_events.BEIJING_OFFSET_SECONDS
    shifted = moment + offset
    day_start = shifted - (shifted % 86400)
    return day_start + 86400 - offset
