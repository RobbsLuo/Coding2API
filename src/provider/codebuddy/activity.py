"""CodeBuddy 活跃上报（B1.7，默认关闭的可选功能）。

用途：上游「成长中心」的**连登天数 / 活跃地图**按日统计客户端对话事件，
本模块代替官方客户端补发一条 `chat_request_send` 事件，让自动化部署的账号
也能续上连登。与积分、调度**无关**：上报失败不影响任何聊天请求。

协议全部来自实测（2026-09-21，直连 CN 上游，勿凭直觉改）：

- 端点 `POST {endpoint}/v2/report`（与聊天同一基址，如
  `https://copilot.tencent.com`）；请求头与聊天一致（Authorization、
  X-Domain/Host 由 endpoint 派生、X-User-Id）。
- 请求体是**事件数组** `[chatRequestEvent]`，不是单对象。
- `eventCode` 用 `chat_request_send`；字段照抄官方客户端全字段形状
  （实测 35 键；不用最小 3 字段，防上游后续加严）。
- **`userId` 必填**：`userId`（或 `X-User-Id`）缺失时上游返回 HTTP 200
  `{"code":0}` 但**静默丢弃**，连登天数不变——实测复现两次。
  本网关 OAuth 凭证的 `user_id` / `account_uid` 实测均为空（上游账号接口
  未回填），因此 userId 从 bearer JWT 的 `sub` 取（实测有效：连登 1→2）。
- 每号每天 1 次即可（与官方单日一次对话等价），不做多时点高频上报。

风险（见 README「活跃上报」与 PROPOSAL §3.1）：官方条款禁止脚本篡改活动
数据，处罚为取消资格并追回已发礼品。默认关闭，开启前自行评估账号风险；
三套客户端指纹与事件名依赖上游实现，改版即失效，**不作为可靠性功能**。
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .client import build_headers
from .credential import CodeBuddyCredential
from .events import UpstreamProtocolViolation

logger = logging.getLogger(__name__)

EP_REPORT = "/v2/report"

# 事件里的模型信息：只用于填充形状，上游不校验一致性（实测任意值均 200）
_EVENT_MODEL_ID = "deepseek-v4-flash"
_EVENT_MODEL_NAME = "DeepSeek V4 Flash"


class ActivityRejected(Exception):
    """上报被上游拒绝（HTTP 非 2xx 或业务 code≠0）。"""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status
        self.message = message


@dataclass(slots=True)
class ActivityResult:
    """一条上报的结果。ok=False 时 message 必为一句人话。"""

    ok: bool
    message: str = ""
    user_id: str = ""


def user_id_from_token(token: str) -> str:
    """从 bearer JWT 取稳定账号标识（`sub`）。

    本网关凭证的 user_id / account_uid 实测为空，而上报的 userId 必填；
    `sub` 实测是 36 位 UUID（OAuth 标准用户标识），用作 userId 可点亮连登。
    解析失败（非 JWT / 无 sub / 非字符串）返回空串，由调用方判为不可上报——
    绝不编造一个 id 去上报（编造的 id 同样会被静默丢弃，还掩盖了根因）。
    """
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return ""
    if not isinstance(claims, dict):
        return ""
    sub = claims.get("sub")
    return sub.strip() if isinstance(sub, str) and sub.strip() else ""


def resolve_user_id(credential: CodeBuddyCredential) -> str:
    """上报用的 userId：优先凭证里的身份，回落 bearer JWT 的 sub。"""
    return (credential.account_uid or credential.user_id
            or user_id_from_token(credential.bearer_token))


def chat_request_event(user_id: str, *, conversation_id: str | None = None,
                       now_ms: int | None = None) -> dict[str, Any]:
    """构造官方客户端形状的 chat_request_send 事件（全字段）。

    conversationId / requestId 无需真实会话（上游不校验一致性，实测）。
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    conversation = conversation_id or f"c2a-{now}-{uuid.uuid4().hex[:8]}"
    return {
        "eventCode": "chat_request_send",
        "timestamp": now,
        "reportDelay": 0,
        "mode": "craft",
        "conversationId": conversation,
        "requestId": conversation,
        "inputLength": 12,
        "requestModelId": _EVENT_MODEL_ID,
        "requestModelName": _EVENT_MODEL_NAME,
        "isPlan": False,
        "isAutoExecuteTerminal": False,
        "isAutoModify": False,
        "codebaseEnable": False,
        "maxToken": 0,
        "maxSteps": 0,
        "temperature": 0,
        "maxRetries": 0,
        "mentionContexts": [],
        "knowledgeId": [],
        "knowledgeName": [],
        "codebaseId": "",
        "mentionContextCount": 0,
        "command": "",
        "expertId": "",
        "recommendId": "",
        "skillId": "",
        "skillCount": 0,
        "totalCount": 0,
        "fileUri": "",
        "presentAt": now,
        "traceId": "",
        "rootRequestId": conversation,
        "parentConversationId": conversation,
        "agentName": "default",
        "agentType": "conversation",
        "userId": user_id,
    }


def _envelope(body: Any) -> None:
    """校验 code=0 的信封；上游业务失败也是 HTTP 200 + code≠0。"""
    if not isinstance(body, dict):
        raise UpstreamProtocolViolation("report response is not an object")
    code = body.get("code")
    if code != 0:
        message = body.get("msg")
        raise UpstreamProtocolViolation(
            f"report rejected with code {code!r}: "
            f"{message if isinstance(message, str) else ''}")


class CodeBuddyActivity:
    """活跃上报客户端。只做请求 + 解析，不重试（每天一次，失败下轮再说）。"""

    def __init__(self, endpoint: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.endpoint = endpoint
        self._client = client

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def report_chat(self, credential: CodeBuddyCredential) -> ActivityResult:
        """补发一条 chat_request_send。userId 缺失时直接判失败，不发无意义请求。"""
        user_id = resolve_user_id(credential)
        if not user_id:
            return ActivityResult(ok=False, message="无法确定账号 userId（凭证与 token 均无）")
        headers = build_headers(credential, self.endpoint)
        headers["X-User-Id"] = user_id
        response = await self._http.post(
            f"{self.endpoint}{EP_REPORT}",
            json=[chat_request_event(user_id)], headers=headers)
        if not 200 <= response.status_code < 300:
            raise ActivityRejected(response.status_code, _message_of(response))
        try:
            body = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON report response") from error
        _envelope(body)
        return ActivityResult(ok=True, user_id=user_id)


def _message_of(response: httpx.Response) -> str:
    """从失败响应里取一句人话；取不到就空串（调用方用状态码兜底）。"""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        message = body.get("msg") or body.get("message")
        if isinstance(message, str):
            return message
    return ""
