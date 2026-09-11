"""CodeBuddy OAuth 设备码轮询登录（Q17=C poll 轨道）。

流程（来自 codebuddy2api 的实测实现）：
1. POST /v2/plugin/auth/state?platform=CLI → {authUrl, state}
2. 用户在浏览器打开 authUrl 授权
3. 后端轮询 GET /v2/plugin/auth/token?state=... →
   code=11217 表示「等待登录」，code=0 时给出 accessToken
4. 再 GET /v2/plugin/login/account?state=... 拿账号信息

state 的所有权与消费语义（AGENTS.md）：一旦取消或消费便不可轮询或重放。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...provider.base import AuthResult, AuthSession
from .credential import CodeBuddyCredential
from .events import UpstreamProtocolViolation
from .headers import EP_AUTH_STATE, EP_AUTH_TOKEN, EP_LOGIN_ACCOUNT, auth_start_headers, host_of

AUTH_STATE_TTL_SECONDS = 600
PENDING_CODE = 11217          # 等待用户完成登录
ACCOUNT_PENDING_CODE = 12151  # 等待账号信息准备完成


def _poll_headers() -> dict[str, str]:
    return {"Accept": "application/json, text/plain, */*"}


@dataclass
class AuthProgress:
    """分阶段登录进度。敏感信息只留在服务端，不进前端响应。"""

    token_data: dict[str, Any] | None = None
    stage: str = "token"


@dataclass
class _Reservation:
    username: str
    created_at: int
    upstream_state: str
    progress: AuthProgress = field(default_factory=AuthProgress)


class AuthStateStore:
    """state 的归属校验、消费与过期清理。"""

    def __init__(self, ttl_seconds: int = AUTH_STATE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, _Reservation] = {}

    def begin(self, username: str, upstream_state: str, now: int | None = None) -> str:
        self.cleanup(now)
        reservation = secrets.token_urlsafe(18)
        self._entries[reservation] = _Reservation(
            username=username, created_at=int(now if now is not None else time.time()),
            upstream_state=upstream_state)
        return reservation

    def upstream_state(self, auth_state: str, username: str) -> str | None:
        """轮询上游时必须用上游发的 state，而不是本地的 reservation。"""
        if not self.owner(auth_state, username):
            return None
        return self._entries[auth_state].upstream_state

    def owner(self, auth_state: str, username: str) -> bool:
        entry = self._entries.get(auth_state)
        return entry is not None and entry.username == username

    def progress(self, auth_state: str, username: str) -> AuthProgress | None:
        if not self.owner(auth_state, username):
            return None
        return self._entries[auth_state].progress

    def set_progress(self, auth_state: str, progress: AuthProgress) -> None:
        if auth_state in self._entries:
            self._entries[auth_state].progress = progress

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
        for key in [k for k, v in self._entries.items() if current - v.created_at >= self._ttl]:
            del self._entries[key]


class CodeBuddyOAuth:
    def __init__(self, endpoint: str, *, client: httpx.AsyncClient | None = None,
                 store: AuthStateStore | None = None) -> None:
        self.endpoint = endpoint
        self.store = store or AuthStateStore()
        self._client = client

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def start(self, username: str) -> AuthSession:
        """申请 auth state。校验通过前不登记 state、不让前端导航。"""
        headers = auth_start_headers(host_of(self.endpoint))
        response = await self._http.post(
            f"{self.endpoint}{EP_AUTH_STATE}?platform=CLI", json={}, headers=headers)
        if response.status_code != 200:
            raise UpstreamProtocolViolation("auth service unavailable")
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("code") != 0 or not isinstance(data, dict):
            raise UpstreamProtocolViolation("auth state request rejected")
        auth_url = data.get("authUrl")
        upstream_state = data.get("state")
        # 校验通过后才登记 state（无效的 authUrl 不允许让前端导航）
        if not isinstance(auth_url, str) or not auth_url:
            raise UpstreamProtocolViolation("auth response missing authUrl")
        if not isinstance(upstream_state, str) or not upstream_state:
            raise UpstreamProtocolViolation("auth response missing state")
        reservation = self.store.begin(username, upstream_state)
        return AuthSession(
            flow="poll", state=reservation, auth_url=auth_url, interval=5,
            callback_url=None)

    async def poll(self, auth_state: str, username: str) -> AuthResult | None:
        """返回 None 表示仍在等待；成功后 state 被消费。"""
        progress = self.store.progress(auth_state, username)
        upstream_state = self.store.upstream_state(auth_state, username)
        if progress is None or upstream_state is None:
            raise UpstreamProtocolViolation("unknown or consumed auth state")

        if progress.token_data is None:
            token_data = await self._fetch_token(upstream_state)
            if token_data is None:
                return None                          # 用户还没完成授权
            progress.token_data = token_data
            progress.stage = "account"

        accounts = await self._fetch_accounts(upstream_state, progress.token_data)
        if accounts is None:
            return None                              # 账号信息还在准备
        credential = _build_credential(progress.token_data, accounts)
        if not self.store.consume(auth_state, username):
            raise UpstreamProtocolViolation("auth state was consumed concurrently")
        return AuthResult(credential_data=credential.to_dict(),
                          nickname=credential.nickname)

    async def _fetch_token(self, auth_state: str) -> dict[str, Any] | None:
        response = await self._http.get(
            f"{self.endpoint}{EP_AUTH_TOKEN}?state={auth_state}", headers=_poll_headers())
        if response.status_code != 200:
            raise UpstreamProtocolViolation("auth service unavailable")
        body = response.json()
        if not isinstance(body, dict):
            raise UpstreamProtocolViolation("invalid auth response")
        code = body.get("code")
        if code == PENDING_CODE:
            return None
        if code != 0:
            raise UpstreamProtocolViolation(f"auth rejected with code {code!r}")
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("accessToken"), str):
            raise UpstreamProtocolViolation("auth response missing accessToken")
        return data

    async def _fetch_accounts(self, auth_state: str,
                              token_data: dict[str, Any]) -> dict[str, Any] | None:
        headers = _poll_headers() | {"Authorization": f"Bearer {token_data['accessToken']}"}
        response = await self._http.get(
            f"{self.endpoint}{EP_LOGIN_ACCOUNT}?state={auth_state}", headers=headers)
        if response.status_code != 200:
            raise UpstreamProtocolViolation("account service unavailable")
        body = response.json()
        if not isinstance(body, dict):
            raise UpstreamProtocolViolation("invalid account response")
        code = body.get("code")
        if code == ACCOUNT_PENDING_CODE:
            return None
        if code != 0:
            raise UpstreamProtocolViolation(f"account request rejected with code {code!r}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise UpstreamProtocolViolation("account response missing data")
        return data


def _build_credential(token_data: dict[str, Any],
                      accounts: dict[str, Any]) -> CodeBuddyCredential:
    account = accounts.get("account") if isinstance(accounts.get("account"), dict) else {}
    user_id = token_data.get("user_id") or accounts.get("user_id") or ""
    return CodeBuddyCredential(
        bearer_token=str(token_data.get("accessToken") or ""),
        user_id=str(user_id),
        account_uid=str(account.get("uid") or accounts.get("account_uid") or ""),
        domain=str(token_data.get("domain") or ""),
        enterprise_id=str(token_data.get("enterprise_id")
                          or account.get("enterpriseId") or ""),
        department_full_name=str(account.get("departmentFullName") or ""),
        refresh_token=str(token_data.get("refreshToken") or ""),
        expires_at=_expires_at(token_data),
        auth_source="oauth",                      # 只能由此可信入口写入
        nickname=str(account.get("nickname") or accounts.get("nickname") or ""),
    )


def _expires_at(token_data: dict[str, Any]) -> int:
    raw = token_data.get("expires_at")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    created = token_data.get("created_at")
    expires_in = token_data.get("expires_in")
    if isinstance(created, (int, float)) and isinstance(expires_in, (int, float)):
        return int(created) + int(expires_in)
    return 0
