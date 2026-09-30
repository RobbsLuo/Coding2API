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

import secrets
import time
from dataclasses import dataclass

import httpx

from ...provider.base import AuthResult, AuthSession
from . import auth as codearts_auth
from .auth import LoginConfig, LoginSession
from .events import UpstreamProtocolViolation

AUTH_STATE_TTL_SECONDS = 600
# 官方插件会挑一个本地空闲端口并真的监听；本服务走 ticket 轮询，不监听，
# 用一个固定占位端口即可（门户只把它原样拼进 redirect_uri）。
DEFAULT_CALLBACK_PORT = 12800


@dataclass
class _Reservation:
    username: str
    created_at: int
    session: LoginSession


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

    def session(self, auth_state: str, username: str) -> LoginSession | None:
        if not self.owner(auth_state, username):
            return None
        return self._entries[auth_state].session

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
                 store: AuthStateStore | None = None) -> None:
        self.config = config
        self.port = port
        self.store = store or AuthStateStore()
        self._client = client

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0),
                                             trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def start(self, username: str) -> AuthSession:
        """生成 PKCE/ticket/DPoP 私钥并登记 state（不触达上游，纯本地构造）。"""
        session = codearts_auth.new_login_session(self.config, port=self.port)
        reservation = self.store.begin(username, session)
        return AuthSession(flow="poll", state=reservation,
                           auth_url=session.auth_url, interval=5,
                           callback_url=None)

    async def poll(self, auth_state: str, username: str) -> AuthResult | None:
        """返回 None 表示用户还没完成授权；成功后 state 被消费。"""
        session = self.store.session(auth_state, username)
        if session is None:
            raise UpstreamProtocolViolation("unknown or consumed auth state")
        tokens = await codearts_auth.poll_ticket(
            self._http, self.config,
            ticket_id=session.ticket_id, secret=session.secret)
        if not tokens:
            return None                              # 等待用户在门户授权
        credential_data = codearts_auth.credential_data_from_tokens(
            tokens, session.dpop_private_jwk)
        if not self.store.consume(auth_state, username):
            raise UpstreamProtocolViolation("auth state was consumed concurrently")
        return AuthResult(credential_data=credential_data,
                          nickname=str(credential_data.get("nickname") or ""))
