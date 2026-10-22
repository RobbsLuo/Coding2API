"""CodeArts 上游客户端：华为云 `SDK-HMAC-SHA256` 签名 + snap 引擎 SSE。

三条链路（逆向记录 §2/§4/§7）：

* **推理** `POST {snap}/api/v2/chat/completions`（原生 OpenAI 形状）。旧端点
  `/v1/chat/chat` 走 §4 的 `task=chat` 形状，作为兼容路径保留。
* **模型发现** `GET {snap}/v1/model/builtin`（`Agent-Type: PromptCenter`）。
* **额度** `GET {snap}/snap-manager/v1/statistics/plugin`（`Agent-Type:
  PromptCenter`），单位本身就是积分（内置模型计费）；积分包带独立到期日，
  登记成 `expiry_ladder` 参与调度「快过期的先用」。**每日签到**
  走另一套接口（`GET {snap}/v1/ops/delivery` → `claim` → `confirm`，见下），
  临时凭证的续期由 `tasks.refresh.RefreshTask` 独占（先落库再同步）；
  额度探测只读余额，绝不在此刷新一次性 refresh_token，见 `probe_quota`。

签名不是 bearer：`x-auth-token` 传 STS security_token 会被 APIG 拒
（`APIG.0301 decrypt token fail`），真正的凭据是 AK/SK 签名（+ X-Security-Token）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...provider import base
from ...provider.base import Event, Model, Quota, UpstreamReloginRequired
from ...provider.proxy import build_client
from . import auth as codearts_auth
from . import events as codearts_events
from . import signer
from .credential import CodeArtsCredential, merge_refreshed
from .events import (
    AGENT_TYPE_PROMPT_CENTER,
    CAMPAIGN_DAILY_CREDIT,
    EP_CHAT,
    EP_CHAT_V2,
    EP_CURRENT_USER,
    EP_MODEL_BUILTIN,
    EP_OPS_CLAIM,
    EP_OPS_CONFIRM,
    EP_OPS_DELIVERY,
    EP_STATISTICS_PLUGIN,
    OPS_CHANNEL,
    SNAP_ENGINE_HOST,
    STS_HOST,
    UpstreamProtocolViolation,
)

logger = logging.getLogger(__name__)

# 流式无总超时防长流截断；短请求 30s 防悬挂（与 TRAE / Kilo 同策略）
# 写超时覆盖的是上传请求体阶段，不是首字等待：大上下文请求体可达数 MB，
# 经代理链路 10s 会稳定超时；首字等待由 read=None +
# stream_first_event_timeout_seconds 负责，两者不要混为一谈。
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)
SHORT_TIMEOUT = httpx.Timeout(30.0)


def sdk_date(now: float | None = None) -> str:
    """`X-Sdk-Date` 格式（UTC，`YYYYMMDDTHHMMSSZ`）。"""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))


def new_chat_id() -> str:
    """32 位十六进制 chat_id（UUID 去连字符）；上游对格式有硬校验。"""
    return secrets.token_hex(16)


def derive_chat_id(payload: dict[str, Any]) -> str:
    """由请求体派生稳定的 chat_id（32 hex）。

    稳定性即 prompt cache 亲和：同一段会话每轮都得到同一个 chat_id，上游的
    缓存命中率才起得来。取摘要而不是随机值，也就不需要在服务端维护会话状态。
    请求体缺失/不可序列化时退回随机值（宁可丢亲和也不能不发）。
    """
    try:
        raw = json.dumps(payload.get("messages"), sort_keys=True, ensure_ascii=False,
                         default=str)
    except (TypeError, ValueError):
        return new_chat_id()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _sign_credential(credential: CodeArtsCredential) -> signer.SignCredential:
    return signer.SignCredential(
        access_key_id=credential.access_key_id,
        secret_access_key=credential.secret_access_key,
        security_token=credential.security_token)


def request_path(url: str) -> tuple[str, str]:
    """URL → `(path, raw_query)`（签名用；去掉 host，保留原始 query 文本）。"""
    split = urlsplit(url)
    return split.path or "/", split.query


def prepare_body(payload: dict[str, Any], model: str, *,
                 chat_id: str, user_id: str = "",
                 legacy: bool = False) -> dict[str, Any]:
    """下游请求体 → 上游请求体。

    v2（默认）：原生 OpenAI 形状，强制 `stream=true`（非流式由引擎聚合）。

    legacy（`/v1/chat/chat`）：§4 的 `task=chat` 形状，`messages` 是**内容块
    数组（无 role）**。多轮对话如何折进无 role 的形状，逆向记录里只有单轮
    样例，故此处是**推断**：把每条消息折成一个文本块、非 user 角色加
    `role: ` 前缀以保留轮次边界。v2 才是官方客户端在用的路径。

    `messages` 深拷贝：下游可能原地改写，不能穿透回引擎持有的 `request.raw`
    （会话粘性指纹与续写都读它）。
    """
    messages = copy.deepcopy(payload.get("messages"))
    if legacy:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "client": "IDE",
            "task": "chat",
            "messages": _legacy_blocks(messages),
            "task_parameters": {"ide": "CodeArts Agent"},
            "batch_task_parameters": [],
            "attempt": 1,
            "not_allow_external_model": True,
            "stream": True,
            "model": model,
        }
        if user_id:
            body["user_id"] = user_id
        return body

    body = {key: value for key, value in payload.items()
            if key in ("temperature", "top_p", "max_tokens", "reasoning_effort",
                       "tools", "tool_choice", "stop", "presence_penalty",
                       "frequency_penalty", "response_format")}
    body.update({
        "model": model,
        "stream": True,
        "messages": messages,
        "chat_id": chat_id,
        "prompt_cache_key": chat_id,
        "tool_stream": True,
    })
    return body


def _legacy_blocks(messages: Any) -> list[dict[str, Any]]:
    """OpenAI messages → §4 的内容块数组（无 role），推断映射见 `prepare_body`。"""
    blocks: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return blocks
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = _flatten_content(message.get("content"))
        if not text:
            continue
        role = message.get("role")
        if isinstance(role, str) and role and role != "user":
            text = f"{role}: {text}"
        blocks.append({"type": "text", "text": text})
    return blocks


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def chat_headers(credential: CodeArtsCredential, *, trace_id: str) -> dict[str, str]:
    """聊天请求头（签名前）。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-auth-token": credential.security_token,
        "x-snap-traceid": trace_id,
        "X-Language": "zh-cn",
        "app-id": "CodeAgent3.0",
        "is_confidential": "false",
    }
    return headers


def short_headers(agent_type: str | None = None) -> dict[str, str]:
    """短请求（模型/额度/身份）公共头；security token 由签名器补。"""
    headers = {"Content-Type": "application/json", "X-Language": "zh-cn"}
    if agent_type:
        headers["Agent-Type"] = agent_type
    return headers


class UpstreamHTTPError(base.UpstreamHTTPError):
    """CodeArts 上游非 2xx；kind() 走 CodeArts 的状态码规则。"""

    classify_status = staticmethod(codearts_events.classify_status)


class CodeArtsClient:
    """上游 HTTP 客户端。主机与登录配置可覆盖，便于测试与灰度。"""

    def __init__(
        self,
        *,
        endpoint: str = SNAP_ENGINE_HOST,
        sts_host: str = STS_HOST,
        legacy: bool = False,
        login: codearts_auth.LoginConfig | None = None,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
        proxy: str | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.sts_host = sts_host.rstrip("/")
        self._legacy = legacy
        self.login = login or codearts_auth.LoginConfig(
            snap_manager=self.endpoint + codearts_events.SNAP_MANAGER_PREFIX,
            sts_host=self.sts_host)
        self._stream_client = stream_client
        self._short_client = short_client
        self._proxy = proxy

    def _stream(self) -> httpx.AsyncClient:
        if self._stream_client is None:
            self._stream_client = build_client(timeout=STREAM_TIMEOUT, proxy=self._proxy)
        return self._stream_client

    def _short(self) -> httpx.AsyncClient:
        if self._short_client is None:
            self._short_client = build_client(timeout=SHORT_TIMEOUT, proxy=self._proxy)
        return self._short_client

    async def aclose(self) -> None:
        for client in (self._stream_client, self._short_client):
            if client is not None:
                await client.aclose()

    def chat_url(self) -> str:
        """本次聊天要用的完整地址（legacy 走 `/v1/chat/chat`）。"""
        path = EP_CHAT if self._legacy else EP_CHAT_V2
        return f"{self.endpoint}{path}"

    # --------------------------------------------------------------- 聊天流

    async def stream_chat(self, credential: CodeArtsCredential, payload: dict[str, Any],
                          model: str) -> AsyncIterator[Event]:
        """POST 聊天端点并逐行产出中立事件。非 2xx 抛 UpstreamHTTPError。

        流量走 `/api/v2/chat/completions`。CodeArts 的 SSE **没有空行分隔**，
        故不能用 `engine.sse.iter_frames`；逐行解析 + `TextSnapshot`
        （累计全文 → 增量）在 `events.py`。
        """
        url = self.chat_url()
        chat_id = derive_chat_id(payload)
        body = prepare_body(payload, model, chat_id=chat_id,
                            user_id=credential.user_name, legacy=self._legacy)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        path, query = request_path(url)
        headers = signer.sign_headers(
            _sign_credential(credential), method="POST", path=path, raw_query=query,
            headers=chat_headers(credential, trace_id=secrets.token_hex(16)),
            payload=raw, x_sdk_date=sdk_date())
        snapshot = codearts_events.TextSnapshot()
        async with self._stream().stream("POST", url, content=raw, headers=headers) as response:
            if response.status_code >= 400:
                body_bytes = await base.read_body_bounded(response)
                raise UpstreamHTTPError(response.status_code, body_bytes,
                                        **base.retry_hints(response.headers))
            async for line in codearts_events.iter_data_lines(response.aiter_bytes()):
                for event in codearts_events.parse_line(line, snapshot):
                    yield event

    # ------------------------------------------------------------ 模型发现

    async def fetch_models(self, credential: CodeArtsCredential) -> list[Model]:
        """内置模型列表；空结果按上游协议违规处理（不静默返回空）。

        空列表会被下游读成「这个渠道没有模型」（列表接口据此剔除该渠道），而
        「这次没拉到」该退避重试，两者不是一回事，故抛违规让调用方退避。
        """
        builtin = await self._fetch_builtin_models(credential)
        if not builtin:
            raise UpstreamProtocolViolation(
                "models api returned empty list",
                public="upstream returned no available models")
        return builtin

    async def _fetch_builtin_models(self, credential: CodeArtsCredential) -> list[Model]:
        url = f"{self.endpoint}{EP_MODEL_BUILTIN}"
        data = await self._get_json(url, credential,
                                    short_headers(AGENT_TYPE_PROMPT_CENTER))
        return _models_from_items(data.get("builtinModels"))

    # ------------------------------------------------------------ 额度

    async def probe_quota(self, credential: CodeArtsCredential) -> Quota:
        """积分余额（只读，不刷新）。

        走 `{snap}/snap-manager/v1/statistics/plugin`：内置模型按积分计费，
        积分包带独立到期日，登记成 `expiry_ladder` 参与「快过期先用」的选号排序。

        临时凭证的续期**不在**这里做：refresh_token 是一次性的，必须由
        `tasks.refresh.RefreshTask`（先落库再同步）独占轮转。若此处顺手刷新，
        一个 refresh_token 会被两个地方各消费一次，后到的报
        `the refresh token has been used`，且此处刷新结果不落库、DB 里的
        token 被烧成废票——实测由此把整条渠道打成 APIG.0602 硬失效。
        """
        return await self._fetch_credits(credential)

    async def _fetch_credits(self, credential: CodeArtsCredential) -> Quota:
        """`/snap-manager/v1/statistics/plugin` → 额度（单位已是积分）。

        **前缀必须带 `snap-manager`**（实测不带回 APIG.0101），与 `/v1/ops/*`
        相反；头 `Agent-Type: PromptCenter` 同内置模型接口。
        """
        url = f"{self.endpoint}{EP_STATISTICS_PLUGIN}"
        data = await self._get_json(
            url, credential, short_headers(AGENT_TYPE_PROMPT_CENTER))
        return parse_credits(data)

    # ------------------------------------------------------------ 每日签到

    async def fetch_checkin_status(self, credential: CodeArtsCredential,
                                   ) -> dict[str, Any] | None:
        """查「每日签到领1000 积分」活动（只读）；上游没有/改版返回 None。

        走 `snap` 根的 `/v1/ops/delivery`——**不带 `snap-manager` 前缀**（带了
        回 APIG.0101）。
        """
        url = (f"{self.endpoint}{EP_OPS_DELIVERY}"
               f"?channel={OPS_CHANNEL}")
        data = await self._get_json(
            url, credential, short_headers(AGENT_TYPE_PROMPT_CENTER))
        return codearts_events.daily_credit_campaign(data)

    async def claim_daily_credit(self, credential: CodeArtsCredential,
                                 ) -> tuple[int, str]:
        """领取当日签到积分，返回 `(业务码, 提示文案)`。

        **claim 之后必须 confirm**：`CLAIMED`（已领未确认）是上游真实存在的
        状态（实测三账号之一正卡在这一档），漏掉 confirm 积分可能不到账。
        所以本方法把两步捆在一起，调用方无需知道这个两步协议。

        业务码取上游 `code`（`0` 为成功）。非 0 是**业务**失败而非签名失败——
        实测有 `40001尚未到达权益刷新时间` 这类码，走 HTTP 200，所以不能只看
        HTTP 状态码。
        """
        url = f"{self.endpoint}{EP_OPS_CLAIM}"
        key = (f"claim_{CAMPAIGN_DAILY_CREDIT}"
               f"_{int(time.time() * 1000)}")
        body = {"campaignId": CAMPAIGN_DAILY_CREDIT,
                "idempotentKey": key, "channel": OPS_CHANNEL}
        data = await self._request_json(
            "POST", url, credential,
            short_headers(AGENT_TYPE_PROMPT_CENTER), body)
        code = _opt_int(data.get("code"))
        message = str(data.get("message") or "")
        if code != 0:
            return (-1 if code is None else code), (message or "领取失败")
        await self._confirm_daily_credit(credential)
        return 0, message

    async def _confirm_daily_credit(self, credential: CodeArtsCredential) -> bool:
        """确认已领积分（claim 的第二步）；返回是否成功。

        积分在 **claim 时就已到账**（实测：claim 后总额 +1000），confirm 只把
        状态从 `CLAIMED` 推到 `CONFIRMED`。所以 confirm 失败不丢积分，但它决定
        本方法返回值——`checkin()` 的 `CLAIMED` 分支要靠它决定是否封账，吞掉失败
        会让 `CheckinTask` 当天封账后再也不补确认。
        """
        try:
            await self._request_json(
                "POST", f"{self.endpoint}{EP_OPS_CONFIRM}", credential,
                short_headers(AGENT_TYPE_PROMPT_CENTER),
                {"campaignId": CAMPAIGN_DAILY_CREDIT})
        except (UpstreamHTTPError, UpstreamProtocolViolation) as error:
            logger.warning("CodeArts 签到确认失败: %s", error)
            return False
        return True

    async def checkin(self, credential: CodeArtsCredential,
                      *, now: int | None = None) -> base.CheckinResult:
        """每日签到状态机：查活动 → 按 status 决定领/补确认/跳过。

        `CheckinTask` 只认 `CheckinResult.ok`（成功即当日封账）与
        `already_checked_in`（已签不算错误），故这里把上游四种 status 归一到
        该语义，细节见 `codearts_events` 顶部的 status 说明。三种真实状态
        （实测三账号各占一档）都有对应分支：

        * `ELIGIBLE` 且 `claimable` → claim + confirm；
        * `CLAIMED`（已领未确认）→ **只补 confirm**，不能当已签跳过；
        * `CONFIRMED` / `CONSUMED` → 已签，不碰写接口。

        活动不存在 / 已过期一律 `ok=True`（不是错误），否则后台任务会每 10 分钟
        刷失败日志；`ELIGIBLE` 但不可领、或未知 status 归 `ok=False` 让任务重试
        —— 漏签一天 1000 积分的代价高于多刷几行日志。
        """
        campaign = await self.fetch_checkin_status(credential)
        if campaign is None:
            return base.CheckinResult(ok=True, message="官方签到活动未开放")
        stamp = int(time.time()) if now is None else now
        end = codearts_events.campaign_end_epoch(campaign)
        if end is not None and end <= stamp:
            return base.CheckinResult(ok=True, message="官方签到活动已结束")
        status = codearts_events.campaign_status(campaign)
        if status in codearts_events.CHECKIN_DONE:
            return base.CheckinResult(ok=True, already_checked_in=True, code=0,
                                      message="今日已签到")
        if status == codearts_events.CHECKIN_CLAIMED:
            # 已领未确认：补齐第二步。confirm 失败要 ok=False，否则 CheckinTask
            # 当日封账、再也不会补确认（积分已在 claim 时到账，这里只是状态收尾）。
            confirmed = await self._confirm_daily_credit(credential)
            if not confirmed:
                return base.CheckinResult(ok=False, message="签到确认失败，稍后重试")
            return base.CheckinResult(ok=True, code=0, message="已补确认签到")
        if (status == codearts_events.CHECKIN_ELIGIBLE
                and codearts_events.campaign_claimable(campaign)):
            code, message = await self.claim_daily_credit(credential)
            if code != 0:
                return base.CheckinResult(ok=False, code=code, message=message)
            return base.CheckinResult(
                ok=True, credit=codearts_events.campaign_credit(campaign), code=0,
                message=message or "签到成功")
        # ELIGIBLE 但不可领 / 未知 status：交给下一轮重试，不静默吞掉。
        return base.CheckinResult(ok=False, message="当前不可领取")

    # ------------------------------------------------------------ 身份/刷新

    async def caller_identity(self, credential: CodeArtsCredential,
                              ) -> tuple[str, str, str]:
        """`GET {sts}/v5/caller-identity` → `(uid, name, domain_id)`。

        只在 `sts.cn-north-4` 域可用（`iam.myhuaweicloud.com` 返回 APIGW.0101）。
        """
        url = f"{self.sts_host}{codearts_events.EP_CALLER_IDENTITY}"
        data = await self._get_json(url, credential, short_headers())
        urn = data.get("principal_urn")
        name = ""
        if isinstance(urn, str) and ":user:" in urn:
            name = urn.rsplit(":user:", 1)[1]
        return (str(data.get("principal_id") or ""), name,
                str(data.get("account_id") or ""))

    async def current_user(self, credential: CodeArtsCredential) -> tuple[str, str, str]:
        """`GET {snap}/snap-manager/v1/current/user` → `(user_id, user_name, domain_id)`。

        前缀 `snap-manager` 必须带：漏掉返回 `APIG.0101 The API does not exist`。
        """
        url = (f"{self.endpoint}{codearts_events.SNAP_MANAGER_PREFIX}"
               f"{EP_CURRENT_USER}")
        data = await self._get_json(url, credential, short_headers())
        return (str(data.get("user_id") or ""), str(data.get("user_name") or ""),
                str(data.get("domain_id") or ""))

    async def refresh_token(self, credential: CodeArtsCredential) -> CodeArtsCredential:
        """refresh_token 换新 STS 凭证，**回写轮转后的 refresh_token**。

        `client_id` / DPoP 私钥随 refresh_token 绑定，必须沿用凭证里的原值；
        否则上游回 `invalid client id` / `InvalidDPoPHeader`。

        令牌端点把终态错误（票已消费/失配）翻译成 `UpstreamReloginRequired`，
        好让 RefreshTask 停手并提示重新登录，而不是每小时拿同一张废票重试。
        非终态的 5xx / 网络失败原样上抛：那类可重试，交给 RefreshTask 下一轮。
        """
        try:
            tokens = await codearts_auth.refresh_tokens(
                self._short(), self.login, refresh_token=credential.refresh_token,
                dpop_private_jwk=credential.dpop_private_jwk,
                code_verifier=credential.code_verifier)
        except codearts_auth.TokenEndpointError as error:
            reason = codearts_events.relogin_required(error.body)
            if not reason:
                raise
            raise UpstreamReloginRequired(reason) from error
        return merge_refreshed(credential, tokens)

    # --------------------------------------------------------------- 内部

    async def _get_json(self, url: str, credential: CodeArtsCredential,
                        headers: dict[str, str]) -> dict[str, Any]:
        return await self._request_json("GET", url, credential, headers, None)

    async def _request_json(self, method: str, url: str, credential: CodeArtsCredential,
                            headers: dict[str, str], body: dict[str, Any] | None,
                            ) -> dict[str, Any]:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        path, query = request_path(url)
        signed = signer.sign_headers(
            _sign_credential(credential), method=method, path=path, raw_query=query,
            headers=headers, payload=raw, x_sdk_date=sdk_date())
        response = await self._short().request(method, url, content=raw if body is not None
                                               else None, headers=signed)
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation(
                f"non-JSON response from {url}",
                public="upstream returned a non-JSON response") from error
        if not isinstance(data, dict):
            raise UpstreamProtocolViolation(
                f"unexpected response shape from {url}",
                public="upstream returned an unexpected response shape")
        return data


def _models_from_items(items: Any) -> list[Model]:
    """内置模型条目 → 中立 Model；畸形条目跳过。

    倍率：条目带 `credit[]`，其中 `ratio_display`（如 `"0.7x"`）是官方对外
    展示的消耗倍率，取首条可解析项作为本渠道 `credit_rate`；上游不给该字段时
    保持 `None`（**不**冒充 zen/kilo 的 x0「免费」——免费层是另一回事）。
    """
    if not isinstance(items, list):
        return []
    models: list[Model] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = _first_str(item, ("model_id", "modelId", "id", "model_name", "name"))
        if not model_id:
            continue
        models.append(Model(
            id=model_id,
            name=_first_str(item, ("model_name", "modelName", "name", "display_name"))
            or model_id,
            credit_rate=_credit_rate(item.get("credit")),
            max_input_tokens=_first_int(item, ("context_window", "contextWindow",
                                               "max_input_tokens")),
            max_output_tokens=_first_int(item, ("max_tokens", "maxTokens",
                                                "max_output_tokens")),
        ))
    return models


def _credit_rate(credit: Any) -> float | None:
    """内置条目的 `credit[]` → 消耗倍率（`ratio_display` 形如 `"0.7x"`）。

    多档（按上下文长度分段计费）时取首条：同一模型的各档倍率一致，取哪档都一样。
    上游换成纯数字或去掉了 `x` 后缀时按同义处理；取不到返回 None（不猜）。
    """
    if not isinstance(credit, list):
        return None
    for tier in credit:
        if not isinstance(tier, dict):
            continue
        rate = _parse_ratio(tier.get("ratio_display"))
        if rate is not None:
            return rate
    return None


def _parse_ratio(value: Any) -> float | None:
    """`"0.7x"` / `"0.7"` / `0.7` → 0.7；其余返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = _RATIO_RE.match(value.strip())
    if match is None:
        return None
    return float(match.group(1))


_RATIO_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)x?$")


def _first_str(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_int(item: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _opt_int(value: Any) -> int | None:
    """可空的整数字段 → int；非 int / bool / 缺失返回 None（区分「未提供」与 0）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# 积分账里代表「本账号积分合计」的那一条 metric（实测：其余是分项包）。
_CREDIT_TOTAL_METRIC = "usageTotalPackageCredit"
# 分项包 → 中文包名（展示用）。上游只给英文 metric 名，包名映射写在这里，
# 未知 metric 回落到 metric 名本身（上游加包时前端不至于显示空白）。
_CREDIT_PACKAGE_NAMES: dict[str, str] = {
    "usageBasicPackageCredit": "基础积分",
    "usageOnDemandPackageCredit": "按需积分",
    "usageBonusPackageCredit": "福利积分",
}


def parse_credits(data: dict[str, Any], *, now: int | None = None) -> Quota:
    """`/snap-manager/v1/statistics/plugin` 响应 → Quota（单位：积分）。

    内置模型（GLM-5.2 / OpenPangu 等）按积分计费，这是本渠道唯一的额度来源。
    上游数值本身已是积分，**不做单位折算**。

    形状（实测 2026-10-15）：
    `metrics[]` 每项含 `package_credit_amount` / `_used` / `_remain`，其中
    `usageTotalPackageCredit` 是合计，其余（Basic/OnDemand/Bonus）是分项包。
    故 `remaining/total` 取合计项，分项写进 `packages`（展示用），并给积分包
    登记到期阶梯——顶层 `end_date` 是整份套餐的到期日（实测每账号不同，如
    `2026-11-02`），积分用不完同样作废，应参与「快过期先用」。

    取不到合计项、或合计项无有效数值时返回 `probe_failed=True`（**未知**），
    绝不返回 `remaining=0` 被判「已耗尽」——积分账缺失不代表内置模型不能用。
    """
    stamp = int(time.time()) if now is None else now
    metrics = data.get("metrics")
    if not isinstance(metrics, list):
        return Quota(probe_failed=True, probed_at=stamp)
    total_item = next(
        (item for item in metrics
         if isinstance(item, dict) and item.get("name") == _CREDIT_TOTAL_METRIC),
        None)
    if total_item is None:
        return Quota(probe_failed=True, probed_at=stamp)
    remaining = _opt_number(total_item, "package_credit_remain")
    total = _opt_number(total_item, "package_credit_amount")
    if remaining is None and total is None:
        return Quota(probe_failed=True, probed_at=stamp)
    end = _package_end_date(data.get("end_date"))
    packages: list[dict[str, Any]] = []
    for item in metrics:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or name == _CREDIT_TOTAL_METRIC:
            continue
        amount = _opt_number(item, "package_credit_amount")
        used = _opt_number(item, "package_credit_used")
        if amount is None and used is None:
            continue
        packages.append({
            "name": _CREDIT_PACKAGE_NAMES.get(name, name),
            "total": amount or 0.0,
            "used": used or 0.0,
            "end": end,
        })
    ladder = [(end, remaining)] if end and remaining and remaining > 0 else None
    return Quota(remaining=remaining, total=total, cycle_end=end,
                 expiry_ladder=ladder, packages=packages or None, probed_at=stamp)


def _opt_number(obj: dict[str, Any], key: str) -> float | None:
    """取可空数值字段；bool / 非数值 / 缺失返回 None（区分「未提供」与 0）。"""
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _package_end_date(value: Any) -> int | None:
    """套餐到期日（`"2026-11-02"`，本地 0 点）→ epoch；解析不出返回 None。

    上游只给日期不给时刻，按服务端本地时区（部署为 Asia/Shanghai）取当日 0 点，
    与「0 点清零」的其它口径一致。
    """
    if not isinstance(value, str):
        return None
    match = _DATE_RE.match(value.strip())
    if match is None:
        return None
    try:
        local = time.strptime(match.group(0), "%Y-%m-%d")
    except ValueError:
        # 正则只保证「四位-两位-两位」形状，月份/日仍可能是 13/45 这类非法值。
        return None
    return int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                            0, 0, 0, 0, 0, -1)))


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
