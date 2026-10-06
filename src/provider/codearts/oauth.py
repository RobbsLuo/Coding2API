"""CodeArts OAuth 登录（poll 轨道：ticket 轮询）。

官方 `huaweicloud.authentication` 插件是「授权码回调 + ticket 轮询」双通道：
回调监听 `127.0.0.1:{port}/oauth/callback` 拿 `code`，再用 `exchange_code` 换令牌。
本服务没有（也不该有）用户本机的回调监听，因此只走 **ticket 轮询通道**：
门户授权完成后，`GET {snap-manager}/v1/login/ticket?ticket_id=&secret=` 直接回
含临时 AK/SK 的完整令牌响应，无需回调。

`port` 仅用于拼 authorize URL 与 redirect_uri，不真正监听；两个区域/多环境
同时登录时各持一个实例，`state` 归属与消费由 `AuthStateStore` 保证。
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import httpx

from ...provider.base import AuthFlow, AuthResult, AuthSession
from ...provider.proxy import build_client
from . import auth as codearts_auth
from .auth import LoginConfig, LoginSession
from .events import UpstreamProtocolViolation

if TYPE_CHECKING:
    from .client import CodeArtsClient

logger = logging.getLogger(__name__)

AUTH_STATE_TTL_SECONDS = 600
# 官方插件会挑一个本地空闲端口并真的监听；本服务走「粘贴回调链接」通道，
# 不监听，用一个固定占位端口即可（门户只把它原样拼进 redirect_uri，换 token
# 时也要原样回传，故两端必须一致）。
DEFAULT_CALLBACK_PORT = 12800


def _token_error_message(error: Exception) -> str:
    """令牌端点错误 → 用户可读文案（带上游原文，便于定位）。"""
    body = getattr(error, "body", b"") or b""
    text = body.decode("utf-8", errors="replace").strip()
    return f"CodeArts token exchange rejected: {text[:200]}" if text \
        else "CodeArts token exchange rejected"


def extract_authorization_code(raw: str) -> str:
    """从粘贴内容里取授权码。

    接受三种输入：完整回调 URL（`http://127.0.0.1:12800/oauth/callback?code=…`）、
    只截取的 query 串（`code=…&state=…`）、以及用户只复制的裸 code 值。
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value and "code=" not in value and "?" not in value:
        return value                                     # 裸 code
    query = urlsplit(value).query if "://" in value or "?" in value else value
    codes = parse_qs(query).get("code") or []
    return codes[0].strip() if codes else ""


def parse_first_stage_callback(raw: str) -> tuple[str, str]:
    """识别「首次回调」（尚未登录门户）：返回 `(门户下发的 secret, 登录页 URL)`。

    形态：`http://127.0.0.1/oauth/callback?secret=<hex64>&redirect=<门户登录页>`
    （注意**没有 code**、没有 `:port`）。非该形态返回 `("", "")`：
    带 code 的第二次回调、裸 code、任意其他链接都不算。
    """
    value = (raw or "").strip()
    if not value or ("://" not in value and "?" not in value):
        return "", ""
    query = urlsplit(value).query if "://" in value or "?" in value else value
    params = parse_qs(query)
    if params.get("code"):
        return "", ""                                    # 第二次回调，不算
    secret = (params.get("secret") or [""])[0].strip()
    redirect = (params.get("redirect") or [""])[0].strip()
    if not secret or not redirect:
        return "", ""
    return secret, redirect


@dataclass
class _Reservation:
    username: str
    created_at: int
    session: LoginSession
    # 门户首次回调下发的 secret（ticket 轮询必须用这份——本地生成的那份上游
    # 不认，实测恒回「无效 ticketId」）；""=尚未收到首次回调。
    portal_secret: str = ""


class AuthStateStore:
    """state 的归属校验、消费与过期清理（对齐 CodeBuddy 的同名实现语义）。"""

    def __init__(self, ttl_seconds: int = AUTH_STATE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, _Reservation] = {}

    def begin(self, username: str, session: LoginSession,
              now: int | None = None) -> str:
        self.cleanup(now)
        reservation = secrets.token_urlsafe(18)
        self._entries[reservation] = _Reservation(
            username=username,
            created_at=int(now if now is not None else time.time()),
            session=session)
        return reservation

    def owner(self, auth_state: str, username: str) -> bool:
        entry = self._entries.get(auth_state)
        return entry is not None and entry.username == username

    def session_entry(self, auth_state: str, username: str) -> _Reservation | None:
        if not self.owner(auth_state, username):
            return None
        return self._entries[auth_state]

    def consume(self, auth_state: str, username: str) -> bool:
        """消费：成功后该 state 不可再次轮询或重放。"""
        if not self.owner(auth_state, username):
            return False
        del self._entries[auth_state]
        return True

    def cancel(self, auth_state: str, username: str) -> bool:
        return self.consume(auth_state, username)

    def cleanup(self, now: int | None = None) -> None:
        current = int(now if now is not None else time.time())
        for key in [k for k, v in self._entries.items()
                    if current - v.created_at >= self._ttl]:
            del self._entries[key]


class CodeArtsOAuth:
    def __init__(self, config: LoginConfig, *, port: int = DEFAULT_CALLBACK_PORT,
                 client: httpx.AsyncClient | None = None,
                 store: AuthStateStore | None = None,
                 identity_client: CodeArtsClient | None = None,
                 proxy: str | None = None) -> None:
        self.config = config
        self.port = port
        self.store = store or AuthStateStore()
        self._client = client
        # 登录后补账号身份（uid/user_name/nickname）的签名客户端：token 响应
        # 通常不带用户名，不补则凭证昵称为空、统计明细的凭证列空白。生产装配
        # 由 main 传入 provider 的 client；None = 跳过补身份（旧行为）。
        self._identity_client = identity_client
        self._proxy = proxy

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_client(timeout=httpx.Timeout(30.0), proxy=self._proxy)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def start(self, username: str) -> AuthSession:
        """生成 PKCE/ticket/DPoP 私钥并登记 state（不触达上游，纯本地构造）。

        flow="paste"：门户把 code 302 回 `127.0.0.1:{port}`（服务端不监听），
        前端因此展示「粘贴回调链接」入口，走 `/upstream/complete` 由服务端换 token。
        """
        session = codearts_auth.new_login_session(self.config, port=self.port)
        reservation = self.store.begin(username, session)
        return AuthSession(flow=AuthFlow.PASTE, state=reservation,
                           auth_url=session.auth_url, interval=None,
                           callback_url=None)

    async def poll(self, auth_state: str, username: str) -> AuthResult | None:
        """返回 None 表示用户还没完成授权；成功后 state 被消费。

        ticket 轮询必须用**门户首次回调下发的 secret**（本地生成的那份上游
        不认）；没收到首次回调时仍按原样轮询——上游会对本地 secret 回
        「无效 ticketId」，由调用方转成「改走粘贴回调链接」的提示。
        """
        reservation = self.store.session_entry(auth_state, username)
        if reservation is None:
            raise UpstreamProtocolViolation("unknown or consumed auth state")
        session = reservation.session
        secret = reservation.portal_secret or session.secret
        try:
            tokens = await codearts_auth.poll_ticket(
                self._http, self.config,
                ticket_id=session.ticket_id, secret=secret)
        except codearts_auth.TokenEndpointError as error:
            # ticket 通道对服务端常被判「无效 ticketId」——门户实际走回调通道，
            # 这里把上游原文转成受控 400，提示用户改走「粘贴回调链接」。
            raise UpstreamProtocolViolation(_token_error_message(error)) from error
        if not tokens:
            return None                              # 等待用户在门户授权
        return await self._finish(auth_state, username, tokens, session)

    async def complete_callback(self, raw_url: str, auth_state: str,
                                username: str) -> AuthResult:
        """用浏览器回跳链接里的 `code` 换 token（服务端无法监听 127.0.0.1 回调）。

        门户授权完成后浏览器会跳到 `http://127.0.0.1:{port}/oauth/callback?code=…`；
        本服务不监听该端口，用户把整条地址（或其中 code）粘回来即可。PKCE
        `code_verifier` 与 DPoP 私钥取自 start 时登记的会话，保证与授权一致。

        例外：官方扩展在用户**尚未登录门户**时会先回调一次，只带
        `secret` + `redirect`（无 code）——识别后给出「先去门户登录」的可行动
        指引，并记下门户 secret 供后续 ticket 轮询使用。
        """
        reservation = self.store.session_entry(auth_state, username)
        if reservation is None:
            raise UpstreamProtocolViolation("unknown or consumed auth state")
        session = reservation.session
        portal_secret, login_url = parse_first_stage_callback(raw_url)
        if portal_secret:
            reservation.portal_secret = portal_secret
            raise UpstreamProtocolViolation(
                "这是门户登录前的首次回调（还没有 code）。请先在浏览器打开 "
                f"{login_url} 完成华为云登录，浏览器会再次回调 127.0.0.1 并带上 "
                "code，把那一条地址粘回来即可。")
        code = extract_authorization_code(raw_url)
        if not code:
            raise UpstreamProtocolViolation(
                "callback URL is missing the authorization code")
        try:
            exchange = await codearts_auth.exchange_code(
                self._http, self.config, code=code,
                code_verifier=session.code_verifier, port=self.port,
                dpop_private_jwk=session.dpop_private_jwk)
        except codearts_auth.TokenEndpointError as error:
            raise UpstreamProtocolViolation(_token_error_message(error)) from error
        return await self._finish(auth_state, username, exchange.tokens, session)

    async def _finish(self, auth_state: str, username: str, tokens: dict,
                      session: LoginSession) -> AuthResult:
        credential_data = codearts_auth.credential_data_from_tokens(
            tokens, session.dpop_private_jwk)
        await self._fill_identity(credential_data)
        if not self.store.consume(auth_state, username):
            raise UpstreamProtocolViolation("auth state was consumed concurrently")
        return AuthResult(credential_data=credential_data,
                          nickname=str(credential_data.get("nickname") or ""))

    async def _fill_identity(self, credential_data: dict) -> None:
        """登录后尽力补齐账号身份（uid/user_name/nickname），就地改写。

        token 响应通常不带用户名（`credential_data_from_tokens` 的 user_name
        回落随之落空），不补则凭证昵称为空、统计明细的凭证列空白。身份接口
        失败（区域限制/网络抖动）**不阻断登录**：只记警告，昵称缺失时展示层
        回退凭证 id 前缀。
        """
        client = self._identity_client
        if client is None:
            return
        from .credential import CodeArtsCredential

        credential = CodeArtsCredential.from_dict(credential_data)
        identity: tuple[str, str, str] | None = None
        for fetch in (client.caller_identity, client.current_user):
            try:
                identity = await fetch(credential)
            except Exception as error:
                # caller_identity 只在部分区域可用，失败就换 current_user；
                # 两个都失败才放弃（登录本身不受影响）。
                logger.warning("CodeArts 登录后补身份失败 %s: %s",
                               getattr(fetch, "__name__", "?"), error)
            else:
                break
        if identity is None:
            return
        uid, name, domain_id = identity
        # 只补缺失项：token 自带的身份优先（与 merge_refreshed / TRAE 同口径），
        # 身份接口只负责把「响应里没有」的名字捡回来，不改写已有值。
        credential.uid = credential.uid or uid
        credential.user_name = credential.user_name or name
        credential.domain_id = credential.domain_id or domain_id
        if not credential.nickname:
            credential.nickname = credential.user_name
        credential_data.update(credential.to_dict())
