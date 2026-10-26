"""LobsterAI 上游客户端：对话流、模型目录、积分、签到与令牌交换。

三处协议私有行为决定了本模块的形状（详见 `events.py` 模块头）：

1. **只支持流式**：`prepare_body` 无条件置 `stream=true`，非流式由本服务的
   聚合层完成（`compat/openai/response.aggregate`）。
2. **错误藏在 200 的流首**：流内错误帧解析成 `EventKind.ERROR` 事件交给
   引擎（引擎据此换号 / 冷却），语义与「HTTP 层报错」完全一致。
3. **能力头 + 版本头必须带**：缺能力头则模型目录少一批、关闭思考直接 500。

**思考档位翻译**：远端把产品侧档位（`off`/`high`/`max`）映射成 wire 值
（`off`/`high`/`xhigh`），两者不是同一套命名（远端 `level:"max"` ↔
`openclawLevel:"xhigh"`）。直接发 `max` 与不发参数无差别，而发一个远端
不认的档位会 500。故本模块按**远端下发的 `thinkingConfig`**做翻译：
已知档位 → 换成 wire 值；未知档位或模型未收录 → **删掉该字段**
（用上游默认档位），绝不发一个可能触发 500 的值。
"""

from __future__ import annotations

import copy
import logging
import time
import uuid as uuid_module
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import httpx

from ...engine.sse import iter_frames
from ...provider import base
from ...provider.base import (
    Event,
    Model,
    Quota,
    UpstreamProtocolViolation,
)
from ..lazy_clients import LazyClients
from . import events as lobsterai_events
from .credential import LobsteraiCredential, resolve_uid
from .events import DEFAULT_KEYFROM

logger = logging.getLogger(__name__)

#: 客户端版本号缓存有效期（秒）。版本号是日期式的、变更频率极低，而
#: 签到每次都要带它——不缓存会让每轮签到多一次跨域请求。
VERSION_CACHE_TTL_SECONDS = 3600

#: 签到幂等键前缀（UUID4 本身即可，前缀只为日志可读）。
_IDEMPOTENCY_PREFIX = "lobsterai-checkin-"


class UpstreamHTTPError(base.UpstreamHTTPError):
    """LobsterAI 上游非 2xx；kind() 走本渠道的状态码规则。"""

    classify_status = staticmethod(lobsterai_events.classify_status)


def keyfrom_body(credential: LobsteraiCredential, client_version: str,
                 ) -> dict[str, str]:
    """身份载荷（exchange / refresh / 模型目录 query 共用）。

    四个字段的语义（**keyfrom 是渠道归因字符串，不是时间戳**）：

    * `firstKeyfrom` / `latestKeyfrom`：**渠道归因来源**。官方客户端的定义
      （`src/shared/keyfrom/constants.ts`）是「本机首次 / 最近一次启动的
      投放渠道」，取值形如 `official` / `bilibili` / `partner_a`，只允许
      `a-z0-9_-`、长度 1..64；官方包默认 `official`，**首次写入后不再覆盖**。
      上游拿它做渠道投放统计，因此**必须原样回放**（续期时也不改）——
      写错格式（比如塞时间戳）会被当成一个不存在的渠道。
    * `version`：客户端版本号。
    * `uuid` / `userId`：缺失时**不带该键**（而非带空串）——空串可能被
      上游当成非法值。
    """
    body: dict[str, str] = {
        "firstKeyfrom": credential.first_keyfrom or DEFAULT_KEYFROM,
        "latestKeyfrom": (credential.latest_keyfrom
                           or credential.first_keyfrom or DEFAULT_KEYFROM),
        "version": client_version,
    }
    if credential.uuid:
        body["uuid"] = credential.uuid
    if credential.user_id:
        body["userId"] = credential.user_id
    return body


def chat_headers(credential: LobsteraiCredential, client_version: str,
                 *, accept: str = "text/event-stream, application/json",
                 ) -> dict[str, str]:
    """对话 / 模型目录请求头。

    头集与官方客户端一致（逐项对照 `src/shared/enterpriseAccount/constants.ts`
    与 `src/shared/providers/modelRuntimeProfiles.ts`）：

    * `X-LobsterAI-Client-Capabilities`：三个能力都要（见 `events` 模块头）；
    * `X-LobsterAI-Client-Version`：客户端版本号；
    * `X-LobsterAI-Account-Mode`：账号模式。官方客户端**每个请求都带**
      （个人号 `personal` / 企业号 `enterprise`），缺了服务端会当成上下文
      不完整（企业场景回 `41612 AccountModeMismatch`）。

    **不设**腾讯系那套 `X-Domain` / `X-Product` / `X-IDE-*` 归属头：LobsterAI
    不认，带上不仅无用，还可能让服务端按错误的客户端形态归因。
    """
    return {
        "Authorization": f"Bearer {credential.access_token}",
        "Content-Type": "application/json",
        "Accept": accept,
        "User-Agent": lobsterai_events.USER_AGENT,
        "X-LobsterAI-Client-Capabilities": lobsterai_events.CLIENT_CAPABILITIES,
        "X-LobsterAI-Client-Version": client_version,
        "X-LobsterAI-Account-Mode": lobsterai_events.ACCOUNT_MODE,
    }


def auth_headers(client_version: str) -> dict[str, str]:
    """令牌端点请求头（exchange / refresh 不带 Bearer）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": lobsterai_events.USER_AGENT,
        "X-LobsterAI-Client-Version": client_version,
    }


def prepare_body(payload: dict[str, Any], model: str,
                 credential: LobsteraiCredential, client_version: str,
                 *, thinking_levels: dict[str, str] | None = None) -> dict[str, Any]:
    """OpenAI 请求体 → LobsterAI 请求体。

    * `messages` 深拷贝：下游可能原地改写 messages，不能穿透回引擎持有的
      `request.raw`（会话粘性指纹与续写都读它）。
    * `stream` 无条件置 true（上游非流式返回 500）。
    * `model` 写死为渠道原始 id。
    * `tool_choice:"none"` → 删除该键（上游不认这个取值）。
    * `reasoning_effort` 按远端 `thinkingConfig` 翻译；翻译不出就删掉。
    """
    body: dict[str, Any] = dict(payload)
    body["messages"] = copy.deepcopy(payload.get("messages"))
    body["model"] = model
    body["stream"] = True
    choice = body.get("tool_choice")
    if choice == "none" or (isinstance(choice, dict)
                            and choice.get("type") == "none"):
        body.pop("tool_choice", None)
    effort = body.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        wire = _translate_effort(effort, thinking_levels)
        if wire is None:
            body.pop("reasoning_effort", None)
        else:
            body["reasoning_effort"] = wire
    # 身份载荷随对话一起发（上游据此归因）。
    body.update(keyfrom_body(credential, client_version))
    return body


def _translate_effort(effort: str, levels: dict[str, str] | None) -> str | None:
    """客户端档位 → 上游 wire 档位；翻译不出返回 None（删掉该字段）。

    `levels` 是该模型远端下发的 `{产品档位: wire 档位}`（`thinkingConfig.options`）。
    查不到时**宁可不发**：发一个上游不认的档位会直接 500，而不发就用上游
    自己的默认档位——后者总是合法的。
    """
    if not levels:
        return None
    return levels.get(effort) or None


def _iso_to_epoch(text: str) -> int:
    """ISO 8601 时间串 → epoch 秒；解析失败返回 0。

    上游的积分包到期时间是 ISO 串（实测 `2026-10-23T01:21:23`，无时区）。
    **无时区时按本地时区解释**：上游是网易的国内服务，时间口径是 UTC+8，
    而本服务的日界（签到封账、冷却到次日 04:00）也用本地时区——两处一致。
    """
    if not text:
        return 0
    from datetime import datetime

    normalized = text.strip().replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return 0


class LobsteraiClient(LazyClients):
    """上游 HTTP 客户端。端点可覆盖，便于测试与灰度。"""

    def __init__(
        self,
        *,
        api_base: str = lobsterai_events.API_BASE,
        portal_base: str = lobsterai_events.PORTAL_BASE,
        version_api: str = lobsterai_events.VERSION_API,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
        proxy: str | None = None,
        now: Any = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.portal_base = portal_base.rstrip("/")
        self.version_api = version_api
        self._stream_client = stream_client
        self._short_client = short_client
        self._proxy = proxy
        self._now = now or time.time
        # 版本号缓存（1h）：只缓存成功结果——缓存兜底值会让一次瞬时故障在
        # TTL 内持续影响后续请求。
        self._version: str | None = None
        self._version_at: float = 0.0
        # 思考档位表：`{模型 id: {产品档位: wire 档位}}`，由模型目录解析而来。
        self._thinking_levels: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------- 版本号

    async def client_version(self) -> str:
        """客户端当前版本号（动态真值，取不到用兜底常量）。

        签到活动按版本门禁：写死旧版本号会拿到 `slotState=empty`（签不到）。
        """
        now = self._now()
        if (self._version is not None
                and now - self._version_at < VERSION_CACHE_TTL_SECONDS):
            return self._version
        version = ""
        try:
            response = await self._short.get(
                self.version_api,
                headers={"Accept": "application/json",
                         "User-Agent": lobsterai_events.USER_AGENT})
            if response.status_code < 400:
                version = _extract_version(response.json())
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("LobsterAI 客户端版本查询失败（用兜底值）: %s", error)
        if not version:
            return lobsterai_events.FALLBACK_CLIENT_VERSION
        self._version = version
        self._version_at = now
        return version

    # ------------------------------------------------------------- 对话流

    async def stream_chat(self, credential: LobsteraiCredential,
                          payload: dict[str, Any], model: str,
                          ) -> AsyncIterator[Event]:
        """POST 对话代理并逐事件产出中立 Event。非 2xx 抛 UpstreamHTTPError。

        上游只支持流式，故这里恒发 `stream=true`；非流式出口由本服务聚合。
        """
        version = await self.client_version()
        body = prepare_body(payload, model, credential, version,
                            thinking_levels=self._thinking_levels.get(model))
        async with self._stream.stream(
            "POST", f"{self.api_base}{lobsterai_events.EP_CHAT}",
            json=body, headers=chat_headers(credential, version),
        ) as response:
            if response.status_code >= 400:
                raw = await base.read_body_bounded(response)
                raise UpstreamHTTPError(response.status_code, raw,
                                        **base.retry_hints(response.headers))
            async for frame in iter_frames(response.aiter_bytes()):
                for event in lobsterai_events.parse_all_events(frame):
                    yield event

    # ------------------------------------------------------------- 模型目录

    async def fetch_models(self, credential: LobsteraiCredential) -> list[Model]:
        """拉模型目录（Bearer 端点），失败回落公开定价目录。

        两个端点的内容一致，差别只在鉴权：公开目录**无需登录**，故在
        凭证失效时也能给出模型清单（管理台不会因此看不到这条渠道）。
        """
        version = await self.client_version()
        try:
            data = await self._envelope(
                "GET", lobsterai_events.EP_MODELS, credential, version,
                query=keyfrom_body(credential, version))
        except (UpstreamHTTPError, UpstreamProtocolViolation) as error:
            logger.info("LobsterAI 鉴权模型目录不可用（回退公开目录）: %s", error)
            return await self._fetch_public_models()
        entries = data if isinstance(data, list) else []
        models = [self._to_model(item) for item in entries
                  if isinstance(item, dict)]
        models = [model for model in models if model is not None]
        if not models:
            return await self._fetch_public_models()
        self._remember_thinking_levels(entries)
        return models

    async def _fetch_public_models(self) -> list[Model]:
        """公开定价目录（无需鉴权）→ 模型列表。

        公开目录的 `textModels` 与鉴权目录同构（另有 `imageModels` /
        `videoModels`，本服务只接文本模型）。
        """
        response = await self._short.get(
            f"{self.api_base}{lobsterai_events.EP_PRICING_CATALOG}",
            headers={"Accept": "application/json",
                     "User-Agent": lobsterai_events.USER_AGENT})
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        data = lobsterai_events.expect_object(
            lobsterai_events.parse_envelope(response.content, "LobsterAI 公开目录"),
            "LobsterAI 公开目录")
        entries = data.get("textModels")
        entries = entries if isinstance(entries, list) else []
        models = [self._to_model(item) for item in entries
                  if isinstance(item, dict)]
        models = [model for model in models if model is not None]
        if not models:
            raise UpstreamProtocolViolation("LobsterAI 公开目录没有文本模型")
        self._remember_thinking_levels(entries)
        return models

    def _remember_thinking_levels(self, entries: list[Any]) -> None:
        """记下各模型的思考档位表（供请求体翻译用）。"""
        self._thinking_levels = thinking_levels_of(entries)

    def _to_model(self, item: dict[str, Any]) -> Model | None:
        """目录条目 → 中立 Model（缺失字段留 None，绝不编造）。"""
        model_id = item.get("modelId")
        if not isinstance(model_id, str) or not model_id:
            return None
        name = item.get("modelName")
        context = item.get("contextWindow")
        max_tokens = item.get("maxTokens")
        rate = item.get("costMultiplier")
        thinking = item.get("thinkingConfig")
        return Model(
            id=model_id,
            name=name if isinstance(name, str) else "",
            # `costMultiplier` 是**相对倍率**（实测 0.05 = 官方价的 5%），
            # 不是「免费」：置 0 会被调度器当成免费档优先选它，故原样透出。
            credit_rate=(float(rate)
                         if isinstance(rate, (int, float)) and not isinstance(rate, bool)
                         else None),
            max_input_tokens=(int(context)
                              if isinstance(context, int) and not isinstance(context, bool)
                              else None),
            max_output_tokens=(int(max_tokens)
                               if isinstance(max_tokens, int) and not isinstance(max_tokens, bool)
                               else None),
            supports_images=(item.get("supportsImage")
                             if isinstance(item.get("supportsImage"), bool) else None),
            supports_tool_call=(item.get("supportsToolCalling")
                                if isinstance(item.get("supportsToolCalling"), bool)
                                else None),
            supports_reasoning=isinstance(thinking, dict),
        )

    # ------------------------------------------------------------- 积分额度

    async def fetch_quota(self, credential: LobsteraiCredential) -> Quota:
        """读积分余额（只读，不消耗积分，不刷新凭据）。

        口径：`profile-summary` 的 `totalCreditsRemaining` 是**唯一**的真实
        余额（`/api/user/quota` 只含 300 的免费池，不含活动与订阅积分）。

        **总量上游不给**（积分来自签到 / 活动 / 订阅 / 加油包，各有各的到期
        日，没有一个统一的「总量」），故 `total=None`；健康度按「剩余为 0 =
        已耗尽，否则未知」处理（见 `provider.base.health_score`）。积分包按
        各自到期日进 `expiry_ladder`，选号时「快过期的先用」。
        """
        moment = int(self._now())
        data = await self._envelope(
            "GET", lobsterai_events.EP_PROFILE_SUMMARY, credential,
            await self.client_version())
        data = lobsterai_events.expect_object(data, "LobsterAI 积分响应")
        remaining = data.get("totalCreditsRemaining")
        if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
            raise UpstreamProtocolViolation(
                "LobsterAI 积分响应缺少 totalCreditsRemaining")
        remaining = max(0.0, float(remaining))
        items = data.get("creditItems")
        items = items if isinstance(items, list) else []
        ladder: list[tuple[int, float]] = []
        packages: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            balance = item.get("creditsRemaining")
            if not isinstance(balance, (int, float)) or isinstance(balance, bool):
                continue
            balance = max(0.0, float(balance))
            expires_at = _iso_to_epoch(str(item.get("expiresAt") or ""))
            if expires_at > 0 and balance > 0:
                ladder.append((expires_at, balance))
            packages.append({
                "name": str(item.get("label") or item.get("type") or "积分包"),
                "total": balance,
                "used": 0.0,
                "end": expires_at or None,
            })
        ladder.sort(key=lambda pair: pair[0])
        return Quota(remaining=remaining, total=None, cycle_end=None,
                     expiry_ladder=ladder or None, packages=packages or None,
                     probed_at=moment)

    # ------------------------------------------------------------- 签到

    async def fetch_checkin_status(self, credential: LobsteraiCredential,
                                   ) -> dict[str, Any]:
        """签到状态 → 管理台展示用的 dict（只读，不发积分）。"""
        inactive = {"active": False, "today_checked_in": False,
                    "today_credit": None, "activity_name": "每日积分礼"}
        version = await self.client_version()
        slot = await self._activity_slot(credential, version)
        if slot is None:
            return inactive
        activity_code, revision = slot
        context = await self._activity_context(credential, version,
                                               activity_code, revision)
        state = (context or {}).get("state")
        state = state if isinstance(state, dict) else {}
        claimed = state.get("claimedToday") is True
        reward = state.get("rewardCredits")
        return {
            "active": True,
            "today_checked_in": claimed,
            "today_credit": (float(reward)
                             if claimed and isinstance(reward, (int, float))
                             and not isinstance(reward, bool) else None),
            "total_credits": None,
            "activity_name": "每日积分礼",
        }

    async def claim_checkin(self, credential: LobsteraiCredential,
                            ) -> base.CheckinResult:
        """执行每日签到（三段式：slot → context → actions/check_in）。

        **幂等靠客户端**：请求带 `idempotencyKey`（UUID4），且签到前先查
        context 的 `state.claimedToday` 与 `actions` 是否含 `check_in`。
        两步预检查都要做——只看 `claimedToday` 会漏掉「活动有但今天不该领」
        的情形。

        上游的奖励是**每日发放**（实测 `rewardCredits=100`），积分**当天
        有效**（过期作废），故封账到下一个本地 0 点。
        """
        version = await self.client_version()
        slot = await self._activity_slot(credential, version)
        if slot is None:
            return base.CheckinResult(
                ok=False, message="当前无可用签到活动（活动未投放或已下线）")
        activity_code, revision = slot
        context = await self._activity_context(credential, version,
                                               activity_code, revision)
        if context is None:
            return base.CheckinResult(ok=False, message="签到活动状态查询失败")
        state = context.get("state")
        state = state if isinstance(state, dict) else {}
        if state.get("claimedToday") is True:
            return base.CheckinResult(ok=True, already_checked_in=True, code=0,
                                      message="今日已签到")
        actions = context.get("actions")
        actions = actions if isinstance(actions, list) else []
        if "check_in" not in actions:
            return base.CheckinResult(ok=False, message="当前活动不提供签到动作")
        body = {
            "configRevision": revision,
            "idempotencyKey": _IDEMPOTENCY_PREFIX + str(uuid_module.uuid4()),
            "payload": {},
        }
        data = await self._envelope(
            "POST",
            f"{lobsterai_events.EP_ACTIVITY_BASE}/{activity_code}/actions/check_in",
            credential, version, body=body)
        granted = _granted_credits(data)
        # 复核：只有 claimedToday 真的翻转才算签到成功（上游可能静默拒绝）。
        after = await self._activity_context(credential, version,
                                             activity_code, revision)
        after_state = (after or {}).get("state")
        after_state = after_state if isinstance(after_state, dict) else {}
        if after_state.get("claimedToday") is not True:
            return base.CheckinResult(
                ok=False, message="签到未生效（上游已签到状态未翻转），稍后重试")
        return base.CheckinResult(
            ok=True, credit=granted, code=0,
            message=f"签到成功，领到 {granted:g} 积分",
            seal_until=_next_local_midnight(int(self._now())))

    async def _activity_slot(self, credential: LobsteraiCredential,
                             version: str) -> tuple[str, int] | None:
        """查活动槽位 → `(activityCode, configRevision)`；无可用活动返回 None。"""
        query = {
            "placement": lobsterai_events.SLOT_PLACEMENT,
            "clientVersion": version,
            "containerApiVersion": lobsterai_events.SLOT_CONTAINER_API_VERSION,
            "platform": lobsterai_events.SLOT_PLATFORM,
        }
        data = await self._envelope(
            "GET", lobsterai_events.EP_ACTIVITY_SLOT, credential, version,
            query=query)
        data = lobsterai_events.expect_object(data, "LobsterAI 活动槽位")
        if data.get("slotState") != "available":
            return None
        activity = data.get("activity")
        activity = activity if isinstance(activity, dict) else {}
        code = activity.get("activityCode")
        if not isinstance(code, str) or not code:
            return None
        revision = activity.get("configRevision")
        revision = (int(revision)
                    if isinstance(revision, int) and not isinstance(revision, bool)
                    else 0)
        return code, revision

    async def _activity_context(self, credential: LobsteraiCredential,
                                version: str, activity_code: str,
                                revision: int) -> dict[str, Any] | None:
        """查活动上下文（今天领了没、有哪些动作）。查询失败返回 None。"""
        try:
            data = await self._envelope(
                "GET",
                f"{lobsterai_events.EP_ACTIVITY_BASE}/{activity_code}/context",
                credential, version, query={"configRevision": str(revision)})
        except (UpstreamHTTPError, UpstreamProtocolViolation) as error:
            logger.warning("LobsterAI 签到上下文查询失败: %s", error)
            return None
        return lobsterai_events.expect_object(data, "LobsterAI 活动上下文")

    # ------------------------------------------------------------- 令牌

    async def exchange_code(self, code: str, *, uuid: str, keyfrom: str,
                            ) -> LobsteraiCredential:
        """授权码 → 凭证（登录）。

        无 PKCE、无 DPoP：上游只认 `authCode` + 身份三元组。`uuid` 与
        `firstKeyfrom` 由**本地生成**（客户端身份），换 token 时必须原样带上。
        """
        version = await self.client_version()
        body = keyfrom_body(
            LobsteraiCredential(uuid=uuid, first_keyfrom=keyfrom,
                                latest_keyfrom=keyfrom),
            version)
        body["authCode"] = code
        data = await self._envelope("POST", lobsterai_events.EP_EXCHANGE,
                                    None, version, body=body)
        data = lobsterai_events.expect_object(data, "LobsterAI 登录响应")
        return self._credential_from_payload(
            data, uuid=uuid, first_keyfrom=keyfrom,
            latest_keyfrom=body["latestKeyfrom"])

    async def refresh_token(self, credential: LobsteraiCredential,
                            ) -> LobsteraiCredential:
        """刷新令牌 → 新凭证（**必须回写轮转后的 refresh_token**）。

        身份三元组原样回放（上游据此认同一台客户端）。
        """
        version = await self.client_version()
        body = keyfrom_body(credential, version)
        body["refreshToken"] = credential.refresh_token
        data = await self._envelope("POST", lobsterai_events.EP_REFRESH,
                                    None, version, body=body)
        data = lobsterai_events.expect_object(data, "LobsterAI 续期响应")
        refreshed = self._credential_from_payload(
            data, uuid=credential.uuid, first_keyfrom=credential.first_keyfrom,
            latest_keyfrom=credential.latest_keyfrom or credential.first_keyfrom)
        # 上游**可能不轮转** refresh_token；响应里没有就沿用旧值——丢掉它
        # 等于让凭证失去续期能力。
        if not refreshed.refresh_token:
            refreshed.refresh_token = credential.refresh_token
        # 身份三元组不来自响应（它只认请求里带的那些），故无需在此回落：
        # `_credential_from_payload` 收的就是调用方传进来的原值。
        # `user_id` / `nickname` 来自响应，缺失时沿用旧值（展示层用昵称）。
        if not refreshed.user_id:
            refreshed.user_id = credential.user_id
        if not refreshed.nickname:
            refreshed.nickname = credential.nickname
        return refreshed

    def _credential_from_payload(self, data: dict[str, Any], *, uuid: str,
                                 first_keyfrom: str,
                                 latest_keyfrom: str) -> LobsteraiCredential:
        """exchange / refresh 响应 → 归一化凭证。"""
        access = data.get("accessToken")
        if not isinstance(access, str) or not access:
            raise UpstreamProtocolViolation("LobsterAI 令牌响应缺少 accessToken")
        refresh = data.get("refreshToken")
        expires_in = data.get("expiresIn")
        expires_at = 0
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
            expires_at = int(self._now()) + int(expires_in)
        user = data.get("user")
        user = user if isinstance(user, dict) else {}
        nickname = user.get("nickname")
        user_id = user.get("userId")
        if not isinstance(user_id, str) or not user_id:
            yid = user.get("yid")
            user_id = yid if isinstance(yid, str) else ""
        return LobsteraiCredential(
            uid=resolve_uid(data, access),
            access_token=access,
            refresh_token=(refresh if isinstance(refresh, str) else ""),
            expires_at=expires_at,
            uuid=uuid,
            first_keyfrom=first_keyfrom,
            latest_keyfrom=latest_keyfrom,
            user_id=user_id,
            nickname=nickname if isinstance(nickname, str) else "",
        )

    # ------------------------------------------------------------- 内部

    async def _envelope(self, method: str, path: str,
                        credential: LobsteraiCredential | None,
                        version: str, *, body: dict[str, Any] | None = None,
                        query: dict[str, str] | None = None) -> Any:
        """发请求并解统一信封 `{code, msg, data}`（返回 `data`）。

        鉴权端点（exchange / refresh）**不带 Bearer**；其余端点带。
        非 2xx 抛 `UpstreamHTTPError`；信封结构不合法抛
        `UpstreamProtocolViolation`。
        """
        headers = (auth_headers(version) if credential is None
                   else chat_headers(credential, version, accept="application/json"))
        url = f"{self.api_base}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        response = await self._short.request(
            method, url, json=body, headers=headers)
        if response.status_code >= 400:
            raw = await base.read_body_bounded(response)
            raise UpstreamHTTPError(response.status_code, raw,
                                    **base.retry_hints(response.headers))
        return lobsterai_events.parse_envelope(response.content, "LobsterAI")


def _extract_version(payload: Any) -> str:
    """版本接口响应 → 版本号串（空串表示取不到）。

    响应**不是**统一信封：载荷在 `data.value.version`，且同层还有 `date` /
    下载地址等字段——只接受**日期式数字版本**（形如 `2026.10.23`），
    上游若把 `version` 换成别的东西，宁可走兜底常量。
    """
    if not isinstance(payload, dict):
        return ""
    outer = payload.get("data")
    outer = outer if isinstance(outer, dict) else {}
    value = outer.get("value")
    value = value if isinstance(value, dict) else {}
    version = value.get("version")
    if not isinstance(version, str):
        return ""
    trimmed = version.strip()
    if not trimmed:
        return ""
    parts = trimmed.split(".")
    if len(parts) < 2 or not all(part.isdigit() for part in parts):
        return ""
    return trimmed


def thinking_levels_of(entries: list[Any]) -> dict[str, dict[str, str]]:
    """目录条目 → `{模型 id: {产品档位: wire 档位}}`。

    远端的 `thinkingConfig.options[]` 每项给两个档位名：`level`（产品侧，
    如 `off`/`high`/`max`）与 `openclawLevel`（发给上游的 wire 值，如
    `off`/`high`/`xhigh`）。两者**不是同一套命名**，直接发 `max` 与不发
    参数无差别。
    """
    result: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("modelId")
        config = entry.get("thinkingConfig")
        if not isinstance(model_id, str) or not isinstance(config, dict):
            continue
        options = config.get("options")
        if not isinstance(options, list):
            continue
        levels: dict[str, str] = {}
        for option in options:
            if not isinstance(option, dict):
                continue
            level = option.get("level")
            wire = option.get("openclawLevel")
            if isinstance(level, str) and isinstance(wire, str) and level and wire:
                levels[level] = wire
        if levels:
            result[model_id] = levels
    return result


def _granted_credits(data: Any) -> float:
    """签到响应 → 本次发放的积分。

    上游在 `result.creditsGranted` / `result.rewardCredits` 给出本次发放量；
    两个字段都没有时回落到**每日固定奖励**（实测 `rewardCredits=100`）——
    「签到了但不知道领了多少」比「按固定值报」更没用。
    """
    result = data.get("result") if isinstance(data, dict) else {}
    result = result if isinstance(result, dict) else {}
    for key in ("creditsGranted", "rewardCredits", "credits"):
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return lobsterai_events.CHECKIN_REWARD_CREDITS


def _next_local_midnight(moment: int) -> int:
    """下一个本地 0 点的 epoch 秒（签到封账用）。

    上游的签到积分**当天有效**（过期作废），故封到本地当日结束——与
    `tasks/checkin.py` 的默认封账口径一致（渠道不额外给 `seal_until` 时
    走那个默认）。
    """
    import time as time_module

    local = time_module.localtime(moment)
    return int(time_module.mktime((local.tm_year, local.tm_mon, local.tm_mday + 1,
                                   0, 0, 0, 0, 1, -1)))
