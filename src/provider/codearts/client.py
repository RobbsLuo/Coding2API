"""CodeArts 上游客户端：华为云 `SDK-HMAC-SHA256` 签名 + snap 引擎 SSE。

三条链路（逆向记录 §2/§4/§7）：

* **推理** `POST {snap}/api/v2/chat/completions`（原生 OpenAI 形状）；
  福利模型（限时免费套餐）必须追加 `maas_type: benefit`，否则报
  `InferHub.002002009.404 model is not registered`。旧端点 `/v1/chat/chat`
  走 §4 的 `task=chat` 形状，作为兼容路径保留。
* **模型发现** `GET {snap}/v1/model/builtin`（`Agent-Type: PromptCenter`）
  + `GET {benefit}/api/v1/gateway/config` 两路合并；福利是按账号授予的，
  因此「哪些模型该带 benefit 头」按 **uid** 分别记账，不跨账号共享。
* **额度** `GET {benefit}/api/v1/user/tokens/balance`，单位是 **token**：免费
  额度为**每日 1000 万 token、当日 0 点清零**（官方「每日千万 Token 免费领」），
  不是按月的套餐积分。**当日没用完即作废**，故 `parse_balance` 把当日剩余登记成
  到期点＝次日 0 点的 `expiry_ladder`，让调度器「快过期的先用」把 CodeArts 排在
  其它渠道之前。CodeArts 没有签到接口，临时凭证的续期由
  `tasks.refresh.RefreshTask` 独占（先落库再同步）；额度探测只读余额，
  绝不在此刷新一次性 refresh_token，见 `probe_quota`。

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
from ...provider.base import Event, EventKind, Model, Quota
from ...provider.token_expiry import normalize_epoch
from . import auth as codearts_auth
from . import events as codearts_events
from . import signer
from .credential import CodeArtsCredential, merge_refreshed
from .events import (
    AGENT_TYPE_PROMPT_CENTER,
    BENEFIT_HOST,
    EP_BENEFIT_CLAIM,
    EP_BENEFIT_CONFIG,
    EP_CHAT,
    EP_CHAT_V2,
    EP_CURRENT_USER,
    EP_MODEL_BUILTIN,
    EP_TOKEN_BALANCE,
    HEADER_MAAS_TYPE,
    MAAS_BENEFIT,
    SNAP_ENGINE_HOST,
    STS_HOST,
    UpstreamProtocolViolation,
)

logger = logging.getLogger(__name__)

# 流式无总超时防长流截断；短请求 30s 防悬挂（与 TRAE / Kilo 同策略）
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
SHORT_TIMEOUT = httpx.Timeout(30.0)

# 冷启动种子：首次发现完成前也要带 benefit 头，否则第一轮请求必吃一次
# 404 未注册（逆向记录 §7「冷启动种子」同款取舍）。
BENEFIT_SEED: frozenset[str] = frozenset({
    "deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "glm-5.3-flash",
})


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
                 chat_id: str, benefit: bool = False, user_id: str = "",
                 legacy: bool = False) -> dict[str, Any]:
    """下游请求体 → 上游请求体。

    v2（默认）：原生 OpenAI 形状，强制 `stream=true`（非流式由引擎聚合）。
    `maas_type` 是**头**不是 body 字段，故 body 与 benefit 无关（便于缓存命中）。

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


def chat_headers(credential: CodeArtsCredential, *, trace_id: str,
                 benefit: bool = False) -> dict[str, str]:
    """聊天请求头（签名前）；`maas_type` 在签名前写入 → 计入 SignedHeaders。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-auth-token": credential.security_token,
        "x-snap-traceid": trace_id,
        "X-Language": "zh-cn",
        "app-id": "CodeAgent3.0",
        "is_confidential": "false",
    }
    if benefit:
        headers[HEADER_MAAS_TYPE] = MAAS_BENEFIT
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
        benefit_host: str = BENEFIT_HOST,
        sts_host: str = STS_HOST,
        legacy: bool = False,
        benefit_auto_claim: bool = True,
        login: codearts_auth.LoginConfig | None = None,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.benefit_host = benefit_host.rstrip("/")
        self.sts_host = sts_host.rstrip("/")
        self.benefit_auto_claim = benefit_auto_claim
        self._legacy = legacy
        self.login = login or codearts_auth.LoginConfig(
            snap_manager=self.endpoint + codearts_events.SNAP_MANAGER_PREFIX,
            sts_host=self.sts_host)
        self._stream_client = stream_client
        self._short_client = short_client
        # 每账号的福利目录（uid → 小写模型 id 集合）。整体替换、不跨账号共享：
        # 福利是按账号授予的，A 有 B 没有时判定依据必须是发起请求的那个账号。
        self._benefit_models: dict[str, frozenset[str]] = {}

    def _stream(self) -> httpx.AsyncClient:
        if self._stream_client is None:
            self._stream_client = httpx.AsyncClient(timeout=STREAM_TIMEOUT,
                                                    trust_env=False)
        return self._stream_client

    def _short(self) -> httpx.AsyncClient:
        if self._short_client is None:
            self._short_client = httpx.AsyncClient(timeout=SHORT_TIMEOUT,
                                                   trust_env=False)
        return self._short_client

    async def aclose(self) -> None:
        for client in (self._stream_client, self._short_client):
            if client is not None:
                await client.aclose()

    def chat_url(self) -> str:
        """本次聊天要用的完整地址（legacy 走 `/v1/chat/chat`）。"""
        path = EP_CHAT if self._legacy else EP_CHAT_V2
        return f"{self.endpoint}{path}"

    def is_benefit_model(self, credential: CodeArtsCredential, model: str) -> bool:
        """该账号的该模型是否走福利路由。

        目录里查得到 → 按目录（上游把它登记成内置即自动摘掉 benefit 头）；
        目录未知（还没发现过 / 福利来源失败）→ 回退冷启动种子，宁可多带一次
        头（带错只是路由到福利通道）也不能让模型 404 未注册。
        """
        catalog = self._benefit_models.get(credential.uid)
        if catalog is None:
            return model.lower() in BENEFIT_SEED
        return model.lower() in catalog

    # --------------------------------------------------------------- 聊天流

    async def stream_chat(self, credential: CodeArtsCredential, payload: dict[str, Any],
                          model: str) -> AsyncIterator[Event]:
        """POST 聊天端点并逐行产出中立事件。非 2xx 抛 UpstreamHTTPError。

        流量走 `/api/v2/chat/completions`（福利模型带 `maas_type: benefit`）。
        CodeArts 的 SSE **没有空行分隔**，故不能用 `engine.sse.iter_frames`；
        逐行解析 + `TextSnapshot`（累计全文 → 增量）在 `events.py`。
        """
        url = self.chat_url()
        benefit = self.is_benefit_model(credential, model) and not self._legacy
        chat_id = derive_chat_id(payload)
        body = prepare_body(payload, model, chat_id=chat_id, benefit=benefit,
                            user_id=credential.user_name, legacy=self._legacy)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        path, query = request_path(url)
        headers = signer.sign_headers(
            _sign_credential(credential), method="POST", path=path, raw_query=query,
            headers=chat_headers(credential, trace_id=secrets.token_hex(16),
                                 benefit=benefit),
            payload=raw, x_sdk_date=sdk_date())
        snapshot = codearts_events.TextSnapshot()
        async with self._stream().stream("POST", url, content=raw, headers=headers) as response:
            if response.status_code >= 400:
                body_bytes = await response.aread()
                raise UpstreamHTTPError(response.status_code, body_bytes)
            async for line in codearts_events.iter_data_lines(response.aiter_bytes()):
                for event in codearts_events.parse_line(line, snapshot):
                    _fill_estimated_credit(event, benefit=benefit)
                    yield event

    # ------------------------------------------------------------ 模型发现

    async def fetch_models(self, credential: CodeArtsCredential) -> list[Model]:
        """内置 + 限时福利两路合并（一路失败不影响另一路，全灭才抛违规）。

        内置来源登记了同名模型即视为「转正」——该模型不再需要 benefit 头
        （逆向记录 §7：目录里有但没标 → 不带，福利模型转正后能自动摘掉）。
        """
        builtin: list[Model] = []
        builtin_error: str = ""
        try:
            builtin = await self._fetch_builtin_models(credential)
        except (UpstreamHTTPError, UpstreamProtocolViolation) as error:
            builtin_error = str(error)
            logger.warning("CodeArts 内置模型拉取失败: %s", error)

        benefit: list[Model] = []
        benefit_error: str | None = None
        if self.benefit_auto_claim:
            await self._claim_benefit_quietly(credential)
        try:
            benefit = await self._fetch_benefit_models(credential)
        except (UpstreamHTTPError, UpstreamProtocolViolation) as error:
            benefit_error = str(error)
            logger.warning("CodeArts 福利模型拉取失败: %s", error)

        if benefit_error is None:
            # 只有福利来源**成功**才整体替换目录（含成功但为空：套餐轮换后
            # 不留旧标记）；失败时保留上一轮，避免非种子福利模型丢 benefit 头。
            promoted = {model.id.lower() for model in builtin}
            self._benefit_models[credential.uid] = frozenset(
                model.id.lower() for model in benefit
                if model.id.lower() not in promoted)

        merged: list[Model] = []
        seen: set[str] = set()
        for model in [*builtin, *benefit]:
            key = model.id.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(model)
        if not merged:
            raise UpstreamProtocolViolation(
                f"models api returned empty list (builtin={builtin_error}, "
                f"benefit={benefit_error or ''})")
        return merged

    async def _fetch_builtin_models(self, credential: CodeArtsCredential) -> list[Model]:
        url = f"{self.endpoint}{EP_MODEL_BUILTIN}"
        data = await self._get_json(url, credential,
                                    short_headers(AGENT_TYPE_PROMPT_CENTER))
        return _models_from_items(data.get("builtinModels"), benefit=False)

    async def _fetch_benefit_models(self, credential: CodeArtsCredential) -> list[Model]:
        url = f"{self.benefit_host}{EP_BENEFIT_CONFIG}"
        data = await self._get_json(url, credential, short_headers())
        result = data.get("result")
        items = result.get("models") if isinstance(result, dict) else None
        return _models_from_items(items, benefit=True)

    # ------------------------------------------------------ 福利领取/额度

    async def claim_benefit(self, credential: CodeArtsCredential) -> dict[str, Any]:
        """领取限时福利（幂等；官方客户端打开模型菜单即调用）。

        未领取时福利模型一律返回 `InferHub.4004.200 benefit not found`。
        """
        url = f"{self.benefit_host}{EP_BENEFIT_CLAIM}"
        return await self._request_json("POST", url, credential, short_headers(), {})

    async def _claim_benefit_quietly(self, credential: CodeArtsCredential) -> None:
        """发现流程里的自动领取：失败不影响列表（只记日志）。"""
        try:
            await self.claim_benefit(credential)
        except (UpstreamHTTPError, UpstreamProtocolViolation) as error:
            logger.warning("CodeArts 福利自动领取失败: %s", error)

    async def probe_quota(self, credential: CodeArtsCredential) -> Quota:
        """额度余额（只读，不刷新）。

        CodeArts 没有每日签到接口，额度是**每日 token 池**（当日 0 点清零）。
        临时凭证的续期**不在**这里做：refresh_token 是一次性的，必须由
        `tasks.refresh.RefreshTask`（先落库再同步）独占轮转。若此处顺手刷新，
        一个 refresh_token 会被两个地方各消费一次，后到的报
        `the refresh token has been used`，且此处刷新结果不落库、DB 里的
        token 被烧成废票——实测由此把整条渠道打成 APIG.0602 硬失效。
        """
        url = f"{self.benefit_host}{EP_TOKEN_BALANCE}"
        data = await self._get_json(url, credential, short_headers())
        return parse_balance(data)

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
        """
        tokens = await codearts_auth.refresh_tokens(
            self._short(), self.login, refresh_token=credential.refresh_token,
            dpop_private_jwk=credential.dpop_private_jwk,
            code_verifier=credential.code_verifier)
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
            raise UpstreamHTTPError(response.status_code, response.content)
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation(f"non-JSON response from {url}") from error
        if not isinstance(data, dict):
            raise UpstreamProtocolViolation(f"unexpected response shape from {url}")
        return data


def _models_from_items(items: Any, *, benefit: bool) -> list[Model]:
    """模型条目（内置/福利两种形状）→ 中立 Model；畸形条目跳过。

    倍率：内置条目带 `credit[]`，其中 `ratio_display`（如 `"0.7x"`）是官方对外
    展示的消耗倍率，取首条可解析项作为本渠道 `credit_rate`。福利条目来自每日
    免费 token 池、上游不给该字段，保持 `None`（**不**冒充 zen/kilo 的 x0「免费」
    ——它消耗的是每日 token 额度，额度用尽即不可用）。福利模型的单请求扣池
    改在 usage 事件上按 token 1:1 推算，见 `_fill_estimated_credit`。
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
            credit_rate=None if benefit else _credit_rate(item.get("credit")),
            max_input_tokens=_first_int(item, ("context_window", "contextWindow",
                                               "max_input_tokens")),
            max_output_tokens=_first_int(item, ("max_tokens", "maxTokens",
                                                "max_output_tokens")),
        ))
    return models


def _fill_estimated_credit(event: Event, *, benefit: bool) -> None:
    """福利模型 USAGE 事件补记**推算额度消耗**（单位：token）。

    CodeArts 上游的 usage 只给 token 数、不带单请求额度字段，而福利模型实测
    按每日 token 池 **1:1** 扣减（`credit_events` 反解：一条输入 32 + 输出 694
    = 726 token 的请求，池余额恰好 −726），故本服务按「输入 + 输出 token」推算
    该请求的额度消耗并标 `credit_estimated`（展示层加 ≈）。

    只补福利模型：内置模型不扣这条每日 token 池（它走 `credit[]` 的付费倍率），
    补了会把 token 数当成池消耗。上游将来若直接回传 `credit` 则不覆盖；
    两个 token 都没有时不猜（保持 None）。
    """
    usage = event.usage
    if (not benefit or event.kind is not EventKind.USAGE or usage is None
            or usage.credit is not None):
        return
    if usage.input_tokens is None and usage.output_tokens is None:
        return
    usage.credit = float((usage.input_tokens or 0) + (usage.output_tokens or 0))
    usage.credit_estimated = True


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


def parse_balance(data: dict[str, Any], *, now: int | None = None) -> Quota:
    """`/api/v1/user/tokens/balance` 响应 → Quota。

    上游实际返回的是**每日免费 token 池**（实测 2026-09-30）：
    `daily_token_limit` / `daily_tokens_used`（+ 等价的 `total_quota` /
    `total_balance` / `used_amount`）。官方口径是「每日 1000 万 token、当日 0 点
    清零」，故有 `daily_token_limit` 时一律按**当日**口径算剩余，而不是拿可能
    代表套餐的 `total_quota` 冒充。

    每日池同时是**用完即弃**：当日没用完的额度 0 点清零、不累计。故这里按与
    CodeBuddy/TRAE 相同的口径登记 `expiry_ladder`（到期点＝次日 0 点、金额＝
    当日剩余），让调度器的「窗口内即将到期额度多者先用」把 CodeArts 排在其它
    渠道之前——否则每天会白丢一个用不完的 1000 万池。

    没有每日字段时退化为键名宽容解析（套餐/月度形状或旧字段），此时不登记
    到期阶梯（无可靠重置时间）；全部取不到时返回 `probe_failed=True`（**未知**），
    绝不返回 `total=None, remaining=None` 被判「已耗尽」。
    """
    stamp = int(time.time()) if now is None else now
    result = data.get("result")
    result = result if isinstance(result, dict) else data
    cycle_end = _first_epoch(result, ("cycle_end", "expire_time", "expires_at",
                                      "reset_time", "next_reset_time"))
    daily_limit = _first_number(result, ("daily_token_limit",))
    if daily_limit is not None and daily_limit > 0:
        daily_used = _first_number(result, ("daily_tokens_used",)) or 0.0
        remaining = max(0.0, daily_limit - daily_used)
        reset = _next_local_midnight(stamp)
        # 已用尽（remaining=0）不携带可消耗额度，不进到期排序（对空包排第一没意义）
        ladder = [(reset, remaining)] if remaining > 0 else None
        return Quota(remaining=remaining, total=daily_limit, cycle_end=reset,
                     expiry_ladder=ladder, probed_at=stamp)
    remaining = _first_number(result, ("remaining", "remaining_tokens", "tokens_balance",
                                       "balance", "available", "available_tokens",
                                       "total_balance"))
    total = _first_number(result, ("total", "total_tokens", "quota", "total_quota"))
    used = _first_number(result, ("used", "used_tokens", "consumed", "used_balance",
                                  "used_amount"))
    if remaining is None and total is not None and used is not None:
        remaining = max(0.0, total - used)
    if remaining is None and total is None:
        return Quota(probe_failed=True, probed_at=stamp)
    return Quota(remaining=remaining, total=total, cycle_end=cycle_end,
                 probed_at=stamp)


def _next_local_midnight(now: int) -> int:
    """距 `now` 最近的下一个本地 0 点（epoch）。

    CodeArts 每日池在**当日 0 点**清零、不累计，上游不返回重置时间戳，故按服务端
    本地时区（部署为 Asia/Shanghai）推算。与 `scheduler.next_credit_reset` 同口径：
    用 `time.localtime` / `time.mktime` 走系统时区，保证调度窗口与「0 点清零」一致。
    """
    local = time.localtime(now)
    midnight = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                            0, 0, 0, 0, 0, -1))
    return int(midnight) + 86400


def _first_number(obj: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _first_epoch(obj: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        epoch = normalize_epoch(int(value))
        if epoch > 0:
            return epoch
    return None
