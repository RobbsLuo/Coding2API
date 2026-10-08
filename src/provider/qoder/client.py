"""Qoder 上游客户端：COSY 签名推理流、模型发现、额度、签到、刷新。

三类端点（PROPOSAL §3.3 / TECHNICAL §3.16）：

* **推理** `POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation`
  —— 请求体是自定义 Base64 变体，头是整套 `cosy-*`；响应是**信封式 SSE**
  （`data:{"headers":…,"body":"<内层 chunk>","statusCodeValue":200}`），
  解包在 `events.decode_envelope` / `parse_inner_chunk`。
* **模型发现** `GET {gateway}/algo/api/v2/model/list?Encode=1` —— **必须**带整套
  COSY 签名头（签名 body 为 `qoder_encode(b"")`）；不带头的裸 GET 会 403，而
  带头后用 POST/PUT 会被上游 400「Request method ... not supported」拒绝，故
  方法固定 GET（本渠道最容易踩的坑，`events.MODELS_SIGN_PLAIN` 记着这个事实）。
* **openapi 业务端点**（纯 Bearer，无 COSY 签名）：额度 `/api/v2/quota/usage`、
  活动制签到 `/sash/api/v1/me/campaigns` + `/sash/api/v1/me/campaigns/{id}/claim`、
  旧签到 `/sash/api/v1/me/daily-check-in/{status,claim}`（回退）、刷新
  `/api/v1/deviceToken/refresh`、身份 `/api/v1/userinfo`。

签到自 2026-10 起改为**活动制**：旧的 `daily-check-in` 接口返回
`status: DISABLED`，上游只在通用 campaign 里发奖。活动制请求必须带
`Cosy-ClientType: 10`，否则上游静默返回空列表（见 `events.EP_CAMPAIGNS`）。

区域由**凭证**推断（`detect_realm_from_domain` + 显式 realm），推理主机候选来自
`events.gateway_candidates`（国际版 api1→api2→api3 故障切换）；主机切换不影响
签名有效性（签名只覆盖去 `/algo` 的 path）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

import httpx

from ...engine.sse import iter_frames
from ...provider import base
from ...provider.base import (
    CheckinResult,
    CheckinStatus,
    Event,
    EventKind,
    Model,
    Quota,
    UpstreamTransportError,
)
from ...provider.proxy import build_client
from . import events as qoder_events
from .cosy import CosySession, CosySessionCache, qoder_encode
from .credential import QoderCredential, merge_refreshed
from .events import (
    EP_CAMPAIGN_CLAIM,
    EP_CAMPAIGNS,
    EP_CHAT,
    EP_CHECKIN_CLAIM,
    EP_CHECKIN_STATUS,
    EP_MODELS,
    EP_QUOTA,
    EP_USERINFO,
    MODELS_SIGN_PLAIN,
    UpstreamProtocolViolation,
    gateway_candidates,
)

logger = logging.getLogger(__name__)

# 流式无总超时防长流截断；短请求 30s 防悬挂（与其余渠道同策略）
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
SHORT_TIMEOUT = httpx.Timeout(30.0)

# 本区域无签到接口的 HTTP 状态（国际版实测 404；405/410 同义）。活动制与旧接口
# 共用：两者都不可用才算「本区域无签到」。
CHECKIN_UNAVAILABLE_STATUS = (404, 405, 410)
CHECKIN_ALREADY_CLAIMED = "ALREADY_CLAIMED"
# 活动制签到请求头：缺它上游静默返回空 campaign 列表（实测，见 events.EP_CAMPAIGNS）
CAMPAIGN_HEADERS = {"Cosy-ClientType": qoder_events.COSY_CLIENT_TYPE}
MODEL_CACHE_TTL_SECONDS = 600

# 上游 body 里的会话失效标记（刷新无意义，需重新登录）
SESSION_DEAD_MARKERS = qoder_events.SESSION_DEAD_MARKERS


class UpstreamHTTPError(base.UpstreamHTTPError):
    """Qoder 上游非 2xx；kind() 走 Qoder 的状态码规则（401/403 抖动归 SOFT）。"""

    classify_status = staticmethod(qoder_events.classify_status)


def realm_for(credential: QoderCredential,
              *, default: str = qoder_events.DEFAULT_REALM) -> str:
    """凭证所属区域：显式 realm 优先，其次按 domain 推断。"""
    if credential.realm:
        return credential.realm
    if credential.domain:
        return qoder_events.detect_realm_from_domain(credential.domain)
    return default


def build_openapi_headers(credential: QoderCredential) -> dict[str, str]:
    """openapi 业务端点的纯 Bearer 头（无 COSY 签名）。

    `X-Machine-ID` / `X-Session-ID` 同样由 uid 稳定派生：openapi 侧也按设备
    维度做风控，随机值会与 COSY 推理侧的机器码不一致。
    """
    from .cosy import derive_id

    config = credential.realm_config()
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": qoder_events.CLIENT_UA,
        "Authorization": f"Bearer {credential.access_token}",
        "X-Request-ID": _openapi_request_id(credential.uid),
        "X-Machine-ID": derive_id(credential.uid, "machine"),
        "X-Session-ID": derive_id(credential.uid, "session"),
        "Origin": config.website,
        "Referer": config.website + "/",
    }


def _openapi_request_id(uid: str) -> str:
    from .cosy import derive_request_id

    return derive_request_id(uid)


def build_chat_body(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """下游 chat 请求体 → Qoder `agent_chat_generation` 请求体。

    **只做结构映射，不注入/不虚构上游模板字段**：逆向记录里的
    `baseprompt.json` 模板与 `chat_context` / `business` 卡片只在桌面端上下文
    里被验证过，凭空补一套没验证过的默认值会把「上游改版」伪装成「我们发对了」。
    这里保留 messages/tools/参数，加官方客户端要求的会话字段（只支持流式）。
    """
    messages = payload.get("messages")
    body: dict[str, Any] = {
        "messages": json.loads(json.dumps(messages)) if messages is not None else [],
        "model": model,
        "stream": True,
        "agent_id": "agent_common",
    }
    for key in ("tools", "tool_choice", "temperature", "top_p", "stop",
                "presence_penalty", "frequency_penalty", "response_format"):
        if key in payload and payload[key] is not None:
            body[key] = payload[key]
    parameters: dict[str, Any] = {}
    max_tokens = payload.get("max_tokens")
    if max_tokens is None:
        max_tokens = payload.get("max_completion_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        parameters["max_tokens"] = max_tokens
    effort = payload.get("reasoning_effort")
    if not effort and isinstance(payload.get("reasoning"), dict):
        effort = payload["reasoning"].get("effort")
    if effort:
        parameters["reasoning_effort"] = str(effort)
    if parameters:
        body["parameters"] = parameters
    return body


class QoderClient:
    """上游 HTTP 客户端。`host` = openapi（业务），`gateway` = 推理网关。

    主机与登录地址可覆盖（测试 / 灰度）；HTTP 客户端可注入 MockTransport。
    """

    def __init__(
        self,
        *,
        host: str = qoder_events.REALM_CONFIGS[qoder_events.DEFAULT_REALM].openapi,
        gateway: str = qoder_events.REALM_CONFIGS[qoder_events.DEFAULT_REALM].gateway,
        gateway_fallbacks: Iterable[str] | None = None,
        sessions: CosySessionCache | None = None,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
        now: Callable[[], int] | None = None,
        proxy: str | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.gateway = gateway.rstrip("/")
        # 覆盖候选主机（仅测试用；生产走区域配置的 api1/api2/api3 切换）
        self._gateway_fallbacks = tuple(gateway_fallbacks or ())
        self.sessions = sessions or CosySessionCache()
        self._stream_client = stream_client
        self._short_client = short_client
        self._now = now or (lambda: int(time.time()))
        self._model_cache: dict[str, tuple[float, list[Model]]] = {}
        self._proxy = proxy

    @property
    def _stream(self) -> httpx.AsyncClient:
        if self._stream_client is None:
            self._stream_client = build_client(timeout=STREAM_TIMEOUT, proxy=self._proxy)
        return self._stream_client

    @property
    def _short(self) -> httpx.AsyncClient:
        if self._short_client is None:
            self._short_client = build_client(timeout=SHORT_TIMEOUT, proxy=self._proxy)
        return self._short_client

    async def aclose(self) -> None:
        for client in (self._stream_client, self._short_client):
            if client is not None:
                await client.aclose()

    # --------------------------------------------------------------- 推理流

    async def stream_chat(self, credential: QoderCredential, payload: dict[str, Any],
                          model: str) -> AsyncIterator[Event]:
        """COSY 签名 + 信封 SSE 解包。

        上游把自身故障也包装成**流内** `statusCodeValue != 200`（HTTP 仍 200），
        故这里两类错误都要处理：HTTP 非 2xx 抛 `UpstreamHTTPError`，流内
        `statusCodeValue != 200` 产出 `EventKind.ERROR`（带上分类，executor
        按分类冷却）。
        """
        session = self.sessions.get(credential)
        body = build_chat_body(payload, model)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        encoded = qoder_encode(raw)
        hosts = self._gateway_hosts(credential)
        # 非最后一台：传输层故障换官方备用主机；最后一台原样上抛（executor 归
        # SOFT 短冷却）。最后一台不进 try，避免「最后一台失败」被误当成可切换。
        for host in hosts[:-1]:
            try:
                async for event in self._stream_host(session, host, encoded, model):
                    yield event
                return
            except httpx.TransportError as error:
                logger.warning("Qoder 推理主机 %s 传输失败，切换备用主机: %s", host, error)
        async for event in self._stream_host(session, hosts[-1], encoded, model):
            yield event

    async def _stream_host(self, session: CosySession, host: str, encoded: str,
                           model: str) -> AsyncIterator[Event]:
        """对单台主机建流并产出中立事件；HTTP 非 2xx 抛 `UpstreamHTTPError`。"""
        url = host + EP_CHAT
        headers = session.headers(body=encoded, raw_url=url, model_key=model, sse=True)
        async with self._stream.stream("POST", url, content=encoded.encode("utf-8"),
                                       headers=headers) as response:
            if response.status_code >= 400:
                raise UpstreamHTTPError(response.status_code,
                                        await base.read_body_bounded(response))
            async for frame in iter_frames(response.aiter_bytes()):
                for event in self._decode_frame(frame):
                    yield event

    def _gateway_hosts(self, credential: QoderCredential) -> list[str]:
        """本次推理用的主机候选：显式覆盖优先，否则区域配置的故障切换列表。"""
        if self._gateway_fallbacks:
            return list(self._gateway_fallbacks)
        realm = realm_for(credential)
        configured = gateway_candidates(realm)
        # client 的 gateway 覆盖了区域主选时，用它替换列表首项（测试与灰度）
        if configured and configured[0] != self.gateway:
            configured = [self.gateway, *configured[1:]]
        return configured or [self.gateway]

    def _decode_frame(self, frame: Any) -> list[Event]:
        """信封帧 → 中立事件（流内错误归一为带分类的 ERROR 事件）。"""
        envelope = qoder_events.decode_envelope(frame)
        if envelope is None:
            return []
        if envelope.status != 200:
            kind = qoder_events.classify_error_code(envelope.status, envelope.body)
            return [Event(kind=EventKind.ERROR, error_code=envelope.status,
                          error_message=envelope.body[:200], error_kind=kind)]
        if envelope.body == "[DONE]":
            return []
        return qoder_events.parse_inner_chunk(envelope.body)

    # ------------------------------------------------------------ 模型发现

    async def fetch_models(self, credential: QoderCredential) -> list[Model]:
        """`GET {gateway}{EP_MODELS}`，仍需带整套 COSY 签名头（含签名 body）。

        实测（2026-09-30，国内版）：模型清单端点**只接受 GET**——带 COSY 签名头
        的 POST/PUT 会被上游以 400「Request method 'POST' not supported」拒绝。
        之前误判为 POST：所谓「裸 GET 会 403」是**不带 COSY 头**时的现象，带上
        `Authorization: Bearer COSY.<payload>.<sig>` 后 GET 正常 200 返回清单。
        签名仍覆盖 `qoder_encode("")`（与请求体一致），headers 里去掉
        x-model-key / x-model-source（清单是账号级、不针对单个模型）。
        清单按 realm 缓存 10 分钟（模型只能由有凭证的调用方拉到，故缓存键含
        凭证区域而非 token）。
        """
        realm = realm_for(credential)
        cached = self._model_cache.get(realm)
        now = time.time()
        if cached is not None and now - cached[0] < MODEL_CACHE_TTL_SECONDS:
            return cached[1]
        sign_body = qoder_encode(MODELS_SIGN_PLAIN)
        session = self.sessions.get(credential)
        payload: dict[str, Any] | None = None
        last_error: Exception | None = None
        for host in self._gateway_hosts(credential):
            url = host + EP_MODELS
            headers = session.headers(body=sign_body, raw_url=url, model_key="",
                                      sse=False, accept="application/json")
            headers["content-type"] = "application/json"
            try:
                response = await self._short.get(url, headers=headers)
                if response.status_code >= 400:
                    raise UpstreamHTTPError(response.status_code, response.content)
                payload = response.json()
                break
            except (httpx.TransportError, ValueError) as error:
                # ValueError 覆盖 JSON 解析失败：清单解析不了时换主机没意义，
                # 但把会话/签名错误（403）留给上抛更清楚，故两类分开处理。
                last_error = error
                continue
        if payload is None:
            # 循环里只吞传输/解析错（UpstreamHTTPError 会直接上抛），
            # 走到这里说明所有主机都在传输层或 JSON 解析上失败。传输层失败
            # 归 `UpstreamTransportError`（可翻译成 network_unreachable），
            # 解析失败才是协议违规——两者的用户动作不同。
            if isinstance(last_error, httpx.TransportError):
                raise UpstreamTransportError(
                    f"model list request failed: {last_error}") from last_error
            raise UpstreamProtocolViolation(
                f"model list request failed: {last_error}") from last_error
        if not isinstance(payload, dict):
            raise UpstreamProtocolViolation("models response is not an object")
        models = qoder_events.parse_models(payload)
        self._model_cache[realm] = (now, models)
        return models

    # ---------------------------------------------------------------- 额度

    async def probe_quota(self, credential: QoderCredential) -> Quota:
        """`GET {openapi}/api/v2/quota/usage`（`userQuota` + `addOnQuota`）。"""
        data = await self._get_json(f"{self.host}{EP_QUOTA}", credential)
        return qoder_events.parse_quota(data, now=self._now())

    # ---------------------------------------------------------------- 签到

    async def checkin(self, credential: QoderCredential) -> CheckinResult:
        """签到三态：已签（今日）/ 新签成功 / 本区域无此接口（国际版 404）。

        自 2026-10 起上游是**活动制**：优先走 `/me/campaigns`（必须带
        `Cosy-ClientType: 10`）→ 筛可领的 `CLAIM_BENEFIT` → claim。活动制接口
        本身不可用（404/405/410）时才整体回退旧的 `daily-check-in` 流程，避免
        上游彻底下线旧接口后签到直接失败。

        「本区域无此接口」归一成 `ok=False` 但带明确 message 的结果——
        `CheckinResult` 没有 skipped 位，`ok=True` 会被 CheckinTask 当成功
        封账（当天不再重试），而 `ok=True, already_checked_in=False` 又会让
        管理台把「无接口」显示成「刚签到」。调用方（管理台）靠 message 区分。
        """
        try:
            data = await self._get_json(
                f"{self.host}{EP_CAMPAIGNS}", credential,
                extra_headers=CAMPAIGN_HEADERS)
        except UpstreamHTTPError as error:
            if error.status not in CHECKIN_UNAVAILABLE_STATUS:
                raise
            return await self._legacy_checkin(credential)
        # 封账到当前签到窗口结束（= 下一轮 10:00 开放时刻），见
        # `events.checkin_window_end` 与 `CheckinResult.seal_until`
        seal_until = qoder_events.checkin_window_end(data)
        status = qoder_events.checkin_status_from_campaigns(data)
        if status.today_checked_in:
            return CheckinResult(ok=True, already_checked_in=True,
                                 message="今日已签到", status=status,
                                 seal_until=seal_until)
        campaign = qoder_events.checkin_campaign(data)
        if campaign is None:
            # 有签到活动但今日无可领项（如已领/同自然人已领）：不重试
            message = ("官方签到活动未开放" if not status.active
                       else "今日暂无可领取的签到奖励")
            return CheckinResult(ok=True, message=message, status=status,
                                 seal_until=seal_until)
        return await self._claim_campaign(credential, status, campaign,
                                          seal_until=seal_until)

    async def _legacy_checkin(self, credential: QoderCredential) -> CheckinResult:
        """旧 `daily-check-in/{status,claim}` 流程（活动制接口不可用时的回退）。"""
        status, unavailable = await self._legacy_checkin_status(credential)
        if unavailable:
            return CheckinResult(ok=False, message=unavailable, status=None)
        if status is not None and status.today_checked_in:
            return CheckinResult(ok=True, already_checked_in=True,
                                 message="今日已签到", status=status)
        if status is not None and not status.active:
            return CheckinResult(ok=True, message="官方签到活动未开放", status=status)
        return await self._claim_legacy_checkin(credential, status)

    async def _legacy_checkin_status(
        self, credential: QoderCredential,
    ) -> tuple[CheckinStatus | None, str]:
        """旧 `daily-check-in/status`；返回 `(status, 不可用原因)`。

        接口不存在（404/405/410）返回 `(None, 原因)`——国际版实测如此，
        这是「本区域没有该活动」而不是错误。
        """
        url = f"{self.host}{EP_CHECKIN_STATUS}"
        try:
            data = await self._get_json(url, credential)
        except UpstreamHTTPError as error:
            if error.status in CHECKIN_UNAVAILABLE_STATUS:
                return None, (f"本区域未开放签到接口（HTTP {error.status}）")
            raise
        return qoder_events.checkin_status_from(data, now=self._now()), ""

    async def fetch_checkin_status(
        self, credential: QoderCredential,
    ) -> tuple[CheckinStatus | None, str]:
        """签到状态；返回 `(status, 不可用原因)`。优先活动制，回退旧接口。"""
        try:
            data = await self._get_json(
                f"{self.host}{EP_CAMPAIGNS}", credential,
                extra_headers=CAMPAIGN_HEADERS)
        except UpstreamHTTPError as error:
            if error.status not in CHECKIN_UNAVAILABLE_STATUS:
                raise
            return await self._legacy_checkin_status(credential)
        return qoder_events.checkin_status_from_campaigns(data), ""

    async def _claim_campaign(self, credential: QoderCredential,
                              status: CheckinStatus | None,
                              campaign: dict[str, Any],
                              *, seal_until: int | None = None) -> CheckinResult:
        """活动制 claim：`POST /me/campaigns/{id}/claim`（带 Cosy-ClientType）。

        上游响应不区分「重放/同自然人已领」与「新领」时都算成功（`ok=True`）：
        `replayed:true` 是幂等重放，`BLOCKED/SAME_PERSON_ALREADY_CLAIMED` 是同一
        自然人已领——两者归一为「今日已签」，否则 CheckinTask 会每 10 分钟无谓重试。
        """
        campaign_id = str(campaign.get("campaignId") or "")
        url = f"{self.host}{EP_CAMPAIGN_CLAIM.format(campaign_id=campaign_id)}"
        data = await self._post_json(url, {}, credential,
                                     extra_headers=CAMPAIGN_HEADERS)
        if qoder_events.claim_already_done(data):
            return CheckinResult(ok=True, already_checked_in=True,
                                 message="今日已签到", status=status,
                                 seal_until=seal_until)
        if str(data.get("status") or "") == qoder_events.CLAIM_STATUS_BLOCKED:
            # 明确被拒（非「同一自然人已领」）：真实失败，交给重试/人工处理
            reason = str(data.get("failureCode") or "BLOCKED")
            return CheckinResult(ok=False, message=f"领取被拒（{reason}）",
                                 status=status)
        reward = qoder_events.campaign_claim_credit(data)
        message = "签到成功" if reward is not None else "签到成功（活动响应异常）"
        return CheckinResult(ok=True, credit=reward, code=0,
                             message=message, status=status, seal_until=seal_until)

    async def _claim_legacy_checkin(self, credential: QoderCredential,
                                    status: CheckinStatus | None) -> CheckinResult:
        url = f"{self.host}{EP_CHECKIN_CLAIM}"
        try:
            data = await self._post_json(url, {}, credential)
        except UpstreamHTTPError as error:
            body = error.body.decode("utf-8", errors="replace")
            # 409 / ALREADY_CLAIMED：并发或重复领取，归一为「今日已签」（不是失败）
            if error.status == 409 or CHECKIN_ALREADY_CLAIMED in body:
                return CheckinResult(ok=True, already_checked_in=True,
                                     code=error.status, message="今日已签到",
                                     status=status)
            raise
        if str(data.get("result") or "") == CHECKIN_ALREADY_CLAIMED:
            return CheckinResult(ok=True, already_checked_in=True, code=409,
                                 message="今日已签到", status=status)
        if data.get("success") is False:
            return CheckinResult(ok=False, code=_opt_int(data.get("code")),
                                 message=str(data.get("error") or data)[:200],
                                 status=status)
        reward = _opt_float(data.get("rewardCredits"))
        return CheckinResult(ok=True, credit=reward, code=0,
                             message="签到成功", status=status)

    # ---------------------------------------------------------------- 身份

    async def fetch_userinfo(self, credential: QoderCredential) -> dict[str, Any]:
        """`GET {openapi}/api/v1/userinfo`（补齐 uid / 昵称 / 组织）。"""
        return await self._get_json(f"{self.host}{EP_USERINFO}", credential)

    # ---------------------------------------------------------------- 刷新

    async def refresh_token(self, credential: QoderCredential) -> dict[str, Any]:
        """`POST {openapi}/api/v1/deviceToken/refresh` → 新凭证 dict。

        返回 `dict`（而非凭证对象）以对齐任务层语义：RefreshTask 直接把它写回
        `credential_data`。刷新成功后旧 COSY 会话立即失效（token 变了）。
        """
        config = credential.realm_config()
        if not credential.refresh_token:
            raise UpstreamProtocolViolation("credential missing refresh_token")
        data = await self._post_json(
            f"{self.host}{qoder_events.EP_DEVICE_REFRESH}",
            {"refresh_token": credential.refresh_token}, credential,
            skip_auth=True)
        refreshed = merge_refreshed(credential, data)
        if not refreshed.access_token:
            raise UpstreamProtocolViolation("refresh returned no token")
        self.sessions.invalidate(credential.uid or refreshed.access_token[:16])
        logger.info("Qoder 凭证已刷新（realm=%s domain=%s）", credential.realm,
                    config.domain)
        return refreshed.to_dict()
    # --------------------------------------------------------------- 内部

    async def _get_json(self, url: str, credential: QoderCredential, *,
                        extra_headers: dict[str, str] | None = None,
                        ) -> dict[str, Any]:
        headers = build_openapi_headers(credential)
        if extra_headers:
            headers.update(extra_headers)
        response = await self._short.get(url, headers=headers)
        return _parse_json_response(response, url)

    async def _post_json(self, url: str, payload: dict[str, Any],
                         credential: QoderCredential, *,
                         skip_auth: bool = False,
                         extra_headers: dict[str, str] | None = None,
                         ) -> dict[str, Any]:
        headers = build_openapi_headers(credential)
        if extra_headers:
            headers.update(extra_headers)
        if skip_auth:
            # 刷新时代入的 access token 可能已失效，带上它会让上游先判 401；
            # 刷新端点只认 refresh_token，无需 Authorization。
            headers.pop("Authorization", None)
        response = await self._short.post(url, json=payload, headers=headers)
        return _parse_json_response(response, url)


def _parse_json_response(response: httpx.Response, url: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise UpstreamHTTPError(response.status_code, response.content)
    try:
        data = response.json()
    except ValueError as error:
        raise UpstreamProtocolViolation(f"non-JSON response from {url}") from error
    if not isinstance(data, dict):
        raise UpstreamProtocolViolation(f"unexpected response shape from {url}")
    return data


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


__all__ = [
    "CHECKIN_UNAVAILABLE_STATUS",
    "MODEL_CACHE_TTL_SECONDS",
    "QoderClient",
    "UpstreamHTTPError",
    "build_chat_body",
    "build_openapi_headers",
    "realm_for",
    "session_dead",
]


def session_dead(text: str) -> bool:
    """响应体是否带「会话已失效」标记（TOKEN_EXPIRE / 12153 / Offline user）。"""
    return qoder_events.is_session_dead(text)
