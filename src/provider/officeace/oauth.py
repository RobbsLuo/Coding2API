"""officeAce 登录闭环：PKCE + DPoP + 云端授权码轮询 + STS 换票 + 开通校验。

完整链路（每一环都实测过，见 docs/officeace-orcaterm-feasibility.md）：

```
① POST {cloud}/v1/claw/auth/state              → state（云端签发，无需鉴权）
② 本地生成 PKCE + DPoP 密钥对（私钥只留本地）
③ 让用户去浏览器登录华为云并授权
④ GET  {cloud}/v1/claw/auth/code?state=…        → 202 等 / 200 拿到 code
⑤ POST {sts}/v1/oauth2/tokens                   → 临时 AK/SK + project_id
⑥ GET  {cloud}/v1/claw/client-permission-validate
                                                → model_app_key / secret / 网关
⑦ 入库：聊天用 Basic(app_key:secret)（**不过期**）；
        临时 AK/SK 另存，用来签名控制面与续期
```

为什么自己实现轮询、不依赖 OfficeAce 桌面端：桌面端必须开着、监听端口还是
随机的（`OFFICE_CLAW_WINDOWS_RANDOM_PORTS=true`），它一关导入功能就废了。
`④` 是整件事的钥匙——桌面端也是靠轮询它把授权码取回来的。

与 CodeArts 的登录**不能混用**：`client_id` 不同（`pdp5_for_agentarts` vs
`codearts-agent`），而 refresh_token 绑定 client_id，混用回
`STS5.1806 invalid client id`（实测）。故本模块独立实现，只复用
`codearts/dpop.py` 的 ES256 低 S 签名原语（华为 STS 强制要求低 S，自己另写
一份必然踩高 S 被拒的坑）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..base import (
    AuthFlow,
    AuthResult,
    AuthSession,
    UpstreamProtocolViolation,
    read_body_bounded,
)
from ..codearts import dpop
from ..proxy import build_client
from . import events as officeace_events
from .credential import OfficeAceCredential, parse_tokens
from .events import (
    CLIENT_ID,
    EP_AUTH_CODE,
    EP_AUTH_STATE,
    EP_CLIENT_PERMISSION,
    EP_OAUTH_TOKENS,
    SIGN_REGION,
)
from .signer import SignCredential, build_v4_headers

logger = logging.getLogger(__name__)

#: 登录会话存活时间：用户走完华为云登录通常 1~3 分钟，10 分钟足够。
LOGIN_TTL_SECONDS = 600
#: 同时最多允许几条「还没登完」的会话。每个 pending 会话每次轮询都在向云端
#: 要授权码；不限量会让一次误点或卡住的面板攒出几十条，等于拿用户账号去压
#: 华为云。
MAX_PENDING_SESSIONS = 5


class TokenEndpointError(RuntimeError):
    """令牌端点非 2xx（由 client 翻成需重新登录 / 可重试的上游错误）。"""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        text = body.decode("utf-8", errors="replace")
        super().__init__(f"token endpoint http {status}: {text[:200]}")


@dataclass(frozen=True, slots=True)
class LoginConfig:
    """登录相关地址与标识（生产用默认值，测试可整组替换）。"""

    client_id: str = CLIENT_ID
    cloud_base: str = officeace_events.AGENTARTS_HOST
    auth_base: str = officeace_events.AUTH_HOST
    sts_host: str = officeace_events.STS_HOST
    region: str = SIGN_REGION

    @property
    def token_url(self) -> str:
        return self.sts_host + EP_OAUTH_TOKENS

    @property
    def redirect_uri(self) -> str:
        """回调地址由**云端**自己承接（本服务不监听任何端口）。"""
        return self.cloud_base.rstrip("/") + officeace_events.REDIRECT_PATH


@dataclass(frozen=True, slots=True)
class LoginSession:
    """一次登录尝试的全部状态。

    `dpop_private_jwk` 必须与 refresh_token 一起落盘：STS 把 refresh_token
    绑定在签发时的 DPoP 公钥上，换新密钥刷新会被拒（`InvalidDPoPHeader`）。
    """

    state: str
    code_verifier: str
    code_challenge: str
    auth_url: str
    dpop_private_jwk: dict[str, str] = field(default_factory=dict)
    created_at: int = 0
    polls: int = 0


@dataclass(slots=True)
class _Reservation:
    """一条 pending 登录：归属人 + 创建时刻 + 会话。

    归属按用户名校验是安全约束而非风格：单槽全局态会让两个管理员的并发登录
    互相覆盖，把 A 的凭证记到 B 名下。
    """

    username: str
    created_at: int
    session: LoginSession


def pkce_pair(verifier: str | None = None) -> tuple[str, str]:
    """PKCE `(code_verifier, code_challenge)`；challenge = BASE64URL(SHA256(v))。"""
    value = verifier or secrets.token_urlsafe(32)
    digest = hashlib.sha256(value.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return value, challenge


class AuthStateStore:
    """pending 登录会话的登记、归属校验、消费与过期清理。

    对外 `state` 是**本地生成的 reservation**，不是云端签发的 state：
    云端 state 会被写进授权链接（浏览器可见），拿它当会话键等于让人凭 URL
    就能消费别人的登录。
    """

    def __init__(self, ttl_seconds: int = LOGIN_TTL_SECONDS,
                 max_pending: int = MAX_PENDING_SESSIONS) -> None:
        self._ttl = ttl_seconds
        self._max_pending = max_pending
        self._entries: dict[str, _Reservation] = {}

    def begin(self, username: str, session: LoginSession,
              now: int | None = None) -> str:
        self.cleanup(now)
        # 攒了太多没登完的就顶掉最老的：拒绝会让人以为功能坏了，而每条
        # pending 会话都在轮询云端，留一堆也只是浪费。
        while len(self._entries) >= self._max_pending:
            oldest = min(self._entries, key=lambda key: self._entries[key].created_at)
            del self._entries[oldest]
        reservation = secrets.token_urlsafe(18)
        self._entries[reservation] = _Reservation(
            username=username,
            created_at=int(now if now is not None else time.time()),
            session=session)
        return reservation

    def owner(self, auth_state: str, username: str,
              now: int | None = None) -> bool:
        """归属校验，**含 TTL**：过期 state 不得再轮询或重放。"""
        entry = self._entries.get(auth_state)
        if entry is None or entry.username != username:
            return False
        current = int(now if now is not None else time.time())
        if current - entry.created_at >= self._ttl:
            del self._entries[auth_state]
            return False
        return True

    def session_entry(self, auth_state: str, username: str,
                      now: int | None = None) -> _Reservation | None:
        if not self.owner(auth_state, username, now):
            return None
        return self._entries[auth_state]

    def touch(self, auth_state: str, now: int | None = None) -> None:
        """记一次轮询（排障用；会话可能已被并发消费，故容忍缺失）。"""
        entry = self._entries.get(auth_state)
        if entry is None:
            return
        session = entry.session
        entry.session = LoginSession(
            state=session.state, code_verifier=session.code_verifier,
            code_challenge=session.code_challenge, auth_url=session.auth_url,
            dpop_private_jwk=session.dpop_private_jwk,
            created_at=session.created_at, polls=session.polls + 1)

    def consume(self, auth_state: str, username: str,
                now: int | None = None) -> bool:
        """消费：成功后该 state 不可再次轮询或重放。"""
        if not self.owner(auth_state, username, now):
            return False
        del self._entries[auth_state]
        return True

    def cancel(self, auth_state: str, username: str,
               now: int | None = None) -> bool:
        return self.consume(auth_state, username, now)

    def cleanup(self, now: int | None = None) -> None:
        current = int(now if now is not None else time.time())
        for key in [k for k, v in self._entries.items()
                    if current - v.created_at >= self._ttl]:
            del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)


class OfficeAceOAuth:
    """登录流程实现（**poll 轨道**：云端承接回调，服务端轮询取码）。"""

    def __init__(self, config: LoginConfig | None = None, *,
                 client: httpx.AsyncClient | None = None,
                 store: AuthStateStore | None = None,
                 proxy: str | None = None,
                 now: Callable[[], int] | None = None) -> None:
        self.config = config or LoginConfig()
        self.store = store or AuthStateStore()
        self._client = client
        self._proxy = proxy
        self._now = now or (lambda: int(time.time()))

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_client(timeout=httpx.Timeout(30.0),
                                        proxy=self._proxy)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ---------------------------------------------------------------- 发起

    async def start(self, username: str, *, force_login: bool = False) -> AuthSession:
        """向云端要 state、生成 PKCE/DPoP，返回授权链接。

        `state` 必须由云端签发（本地造的不被 `/v1/claw/auth/code` 认，
        实测回 `AgentArts.11000004`）。`force_login` 走「先登出」的地址，用于
        **新增另一个账号**：浏览器里还有华为云会话时云端会直接放行当前账号，
        用户根本看不到登录页。
        """
        state = await self._fetch_state()
        verifier, challenge = pkce_pair()
        session = LoginSession(
            state=state, code_verifier=verifier, code_challenge=challenge,
            auth_url=officeace_events.build_authorize_url(
                auth_base=self.config.auth_base,
                client_id=self.config.client_id, state=state,
                code_challenge=challenge,
                redirect_uri=self.config.redirect_uri),
            dpop_private_jwk=dpop.new_private_jwk(),
            created_at=self._now(), polls=0)
        reservation = self.store.begin(username, session, self._now())
        auth_url = session.auth_url
        if force_login:
            auth_url = officeace_events.build_switch_account_url(
                auth_base=self.config.auth_base, authorize_url=auth_url)
        return AuthSession(
            flow=AuthFlow.POLL, state=reservation, auth_url=auth_url,
            interval=int(officeace_events.POLL_INTERVAL_SECONDS),
            callback_url=None)

    async def _fetch_state(self) -> str:
        response = await self._http.post(
            f"{self.config.cloud_base}{EP_AUTH_STATE}", json={},
            headers={"Content-Type": "application/json"})
        if response.status_code >= 400:
            raise UpstreamProtocolViolation(
                f"auth state rejected: HTTP {response.status_code}",
                public="无法向 officeAce 申请登录状态，请稍后重试")
        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON auth state response") from error
        state = payload.get("state") if isinstance(payload, dict) else None
        if not isinstance(state, str) or not state:
            raise UpstreamProtocolViolation("auth state response missing state")
        return state

    # ---------------------------------------------------------------- 轮询

    async def poll(self, auth_state: str, username: str) -> AuthResult | None:
        """返回 None 表示用户还没完成授权；成功后 state 被消费。"""
        entry = self.store.session_entry(auth_state, username, self._now())
        if entry is None:
            raise UpstreamProtocolViolation("unknown or expired auth state")
        self.store.touch(auth_state, self._now())
        code = await self._fetch_code(entry.session.state)
        if code is None:
            return None
        credential = await self._finish(code, entry.session)
        if not self.store.consume(auth_state, username, self._now()):
            # 另一路轮询已先把这条会话消费掉了：它已经把凭证落库，这里不能
            # 再落一条（否则同一账号出现两条重复凭证）。
            raise UpstreamProtocolViolation("auth state was consumed concurrently")
        return AuthResult(credential_data=credential.to_dict(),
                          nickname=credential.nickname)

    async def _fetch_code(self, state: str) -> str | None:
        """`GET /v1/claw/auth/code?state=`；202 / 无码 → None（继续等）。"""
        response = await self._http.get(
            f"{self.config.cloud_base}{EP_AUTH_CODE}",
            params={"state": state}, headers={"Accept": "application/json"})
        if response.status_code == officeace_events.CODE_NOT_READY:
            return None
        if response.status_code == 404:
            raise UpstreamProtocolViolation(
                "授权码已失效或 state 不被识别，请重新发起登录")
        if response.status_code >= 400:
            # 云端偶发 4xx/5xx 不算失败：下一轮再问，别把一次抖动变成
            # 「登录失败，请重来」。
            logger.info("officeAce 取授权码返回 HTTP %s，稍后重试",
                        response.status_code)
            return None
        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON auth code response") from error
        if not isinstance(payload, dict):
            raise UpstreamProtocolViolation("auth code response is not an object")
        if payload.get("error"):
            raise UpstreamProtocolViolation(
                f"auth code rejected: {payload.get('error_msg') or payload['error']}",
                public="officeAce 授权被拒绝，请重新登录")
        for key in ("code", "authorization_code"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        # 云端说好了却没给码：等下一轮（客户端也是这么处理的）。
        logger.warning("officeAce 授权码响应缺少 code 字段")
        return None

    # ---------------------------------------------------------------- 换票

    async def _finish(self, code: str,
                      session: LoginSession) -> OfficeAceCredential:
        tokens = await self.exchange_code(
            code, code_verifier=session.code_verifier,
            dpop_private_jwk=session.dpop_private_jwk)
        credential = parse_tokens(tokens)
        credential.client_id = self.config.client_id
        credential.dpop_private_jwk = dict(session.dpop_private_jwk)
        info = await self.fetch_model_info(credential)
        if not info["app_key"] or not info["app_secret"]:
            # 未开通的账号：云端 200 但 `subscription: null`（实测三账号全如此）。
            # 这不是登录失败，而是「还没兑换邀请码」——必须给可行动的指引，
            # 否则用户会以为是自己登录姿势不对。
            raise UpstreamProtocolViolation(
                "officeAce account is not provisioned",
                public=("这个账号还没开通 officeAce 服务。请先在 OfficeAce "
                        "客户端里登录并兑换邀请码完成开通，再回来重新登录。"))
        if not info["base_url"]:
            raise UpstreamProtocolViolation(
                "model gateway address missing",
                public="云端没有返回模型网关地址，请稍后重试")
        credential.model_app_key = info["app_key"]
        credential.model_app_secret = info["app_secret"]
        credential.model_base_url = info["base_url"]
        credential.account_id = info["account_id"] or credential.account_id
        credential.principal_id = info["principal_id"] or credential.principal_id
        # 昵称只用于展示：优先上游给的、其次用户名、最后账号 ID（都没有就留空，
        # 前端按凭证 ID 兜底）。用 `or` 链而非 if：三者都缺是合法情况，
        # STS 响应本身并不带 nickname 字段。
        credential.nickname = (credential.nickname or credential.user_name
                               or credential.account_id)
        return credential

    async def exchange_code(self, code: str, *, code_verifier: str,
                            dpop_private_jwk: dict[str, str]) -> dict[str, Any]:
        """授权码换临时 AK/SK（`authorization_code`）。"""
        return await self._post_tokens({
            "client_id": self.config.client_id,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": self.config.redirect_uri,
        }, dpop_private_jwk)

    async def refresh_tokens(self, *, refresh_token: str,
                             dpop_private_jwk: dict[str, str],
                             client_id: str = "") -> dict[str, Any]:
        """refresh_token 换新临时凭证。

        必须沿用**签发该票时那一对** DPoP 私钥与 client_id；缺私钥在本地直接
        拒绝（上游会回 `InvalidDPoPHeader`，本地报错更快也更清楚）。
        """
        if not refresh_token:
            raise UpstreamProtocolViolation("credential missing refresh_token")
        if not dpop_private_jwk:
            raise UpstreamProtocolViolation(
                "credential missing dpop_private_jwk (required to refresh)")
        return await self._post_tokens({
            "client_id": client_id or self.config.client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, dpop_private_jwk)

    async def _post_tokens(self, form: dict[str, str],
                           dpop_private_jwk: dict[str, str]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "DPoP": dpop.sign_proof(dpop_private_jwk, self.config.token_url),
        }
        response = await self._http.post(self.config.token_url, data=form,
                                         headers=headers)
        if response.status_code >= 400:
            raise TokenEndpointError(response.status_code,
                                     await read_body_bounded(response))
        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON token response") from error
        if not isinstance(payload, dict):
            raise UpstreamProtocolViolation("token response is not an object")
        return payload

    # ------------------------------------------------------ 开通校验 / 网关

    async def fetch_model_info(self, credential: OfficeAceCredential) -> dict[str, str]:
        """`GET /v1/claw/client-permission-validate` → 网关地址与 app key。

        `SDK-HMAC-SHA256` 签名（控制面）。未开通的账号回 200 但
        `subscription: null`，`parse_model_info` 会给出空 app_key。
        """
        url = f"{self.config.cloud_base}{EP_CLIENT_PERMISSION}"
        headers = build_v4_headers(
            SignCredential(credential.access_key_id,
                           credential.secret_access_key,
                           credential.security_token,
                           credential.project_id),
            method="GET", url=url,
            headers={"Content-Type": "application/json;charset=utf8"},
            payload=b"", x_sdk_date=_sdk_date(self._now()))
        response = await self._http.get(url, headers=headers)
        if response.status_code >= 400:
            body = await read_body_bounded(response)
            raise UpstreamProtocolViolation(
                "permission validate HTTP "
                f"{response.status_code}: "
                f"{body.decode('utf-8', errors='replace')[:200]}",
                public="officeAce 开通校验失败，请稍后重试")
        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation(
                "non-JSON permission validate response") from error
        return officeace_events.parse_model_info(payload)


def _sdk_date(moment: int | None = None) -> str:
    """`X-Sdk-Date`：UTC `YYYYMMDDTHHMMSSZ`（签名与请求必须同值）。"""
    return time.strftime("%Y%m%dT%H%M%SZ",
                         time.gmtime(moment if moment is not None else time.time()))
