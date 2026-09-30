"""Qoder 设备码 PKCE 登录（poll 轨道，Q17=C）。

流程（PROPOSAL §3.3 / 逆向记录）：

1. 本地生成 PKCE `verifier/challenge(S256)` 与 `nonce`，拼出授权 URL
   `{website}/device/selectAccounts?...`，用户浏览器授权；
2. 后端轮询 `GET {openapi}/api/v1/deviceToken/poll?nonce=&verifier=&challenge_method=S256`
   —— HTTP 404/202 = **等待授权**（不是错误）；
3. 拿到 token 后尽力 `GET {openapi}/api/v1/userinfo` 补 uid/昵称/组织，
   归一成凭证返回。

state 归属与消费语义（对齐 CodeBuddy，AGENTS.md）：reservation 是**本地**
随机串，只有发起登录的那个 username 能轮询；取消或成功消费后不可重放；TTL 到期
自动清理。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from ...provider.base import AuthResult, AuthSession
from .credential import QoderCredential, credential_from_device
from .events import (
    CLIENT_UA,
    DEFAULT_REALM,
    EP_DEVICE_POLL,
    EP_USERINFO,
    UpstreamProtocolViolation,
    get_realm_config,
)

logger = logging.getLogger(__name__)

AUTH_STATE_TTL_SECONDS = 600
# 设备授权接口用 404/202 表示「用户还没完成授权」，不是错误（实测）
PENDING_STATUS = (202, 404)
CHALLENGE_METHOD = "S256"
_PKCE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def machine_id_for(realm: str) -> str:
    """本机在官方客户端里的 machine_id（优先读官方落盘，缺则生成）。

    与官方客户端共用同一个设备标识能显著降低风控概率；读不到时返回一个
    **进程级常量**（按平台模块生成一次），而不是每次登录换一个随机值。
    """
    config = get_realm_config(realm)
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, config.domain, ".auth", "machine_id"),
                  os.path.join(home, f".{config.domain}", ".auth", "machine_id")]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip()
        except OSError:
            continue
        if value:
            return value
    return _fallback_machine_id()


def _fallback_machine_id() -> str:
    """读不到官方 machine_id 时的稳定回落（同一进程内一致）。"""
    cached = getattr(_fallback_machine_id, "_cached", "")
    if cached:
        return cached
    seed = f"{uuid.getnode()}:{os.getpid()}:qoder-machine"
    value = hashlib.md5(seed.encode("utf-8")).hexdigest()
    _fallback_machine_id._cached = value  # type: ignore[attr-defined]
    return value


def pkce_pair(verifier: str | None = None) -> tuple[str, str]:
    """RFC 7636 S256：`(verifier, challenge)`，challenge 为 BASE64URL(SHA256)。"""
    if verifier is None:
        verifier = "".join(
            _PKCE_ALPHABET[byte % len(_PKCE_ALPHABET)] for byte in os.urandom(64))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_auth_url(realm: str, *, challenge: str, nonce: str,
                   machine_id: str) -> str:
    """按区域拼授权 URL（参数集差异见 events.RealmConfig 的 send_* 开关）。"""
    config = get_realm_config(realm)
    query = {"challenge": challenge, "challenge_method": CHALLENGE_METHOD,
             "nonce": nonce}
    if config.send_redirect_uri:
        query["redirect_uri"] = config.redirect_uri
    if config.send_client_id:
        query["client_id"] = config.client_id
        query["machine_id"] = machine_id
    return f"{config.website}/device/selectAccounts?{urlencode(query)}"


@dataclass
class _Reservation:
    username: str
    created_at: int
    verifier: str
    nonce: str
    realm: str
    machine_id: str


class AuthStateStore:
    """state 的归属校验、消费与过期清理（与 CodeBuddy 同语义，不同存储载荷）。"""

    def __init__(self, ttl_seconds: int = AUTH_STATE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, _Reservation] = {}

    def begin(self, username: str, *, verifier: str, nonce: str, realm: str,
              machine_id: str, now: int | None = None) -> str:
        self.cleanup(now)
        reservation = secrets.token_urlsafe(18)
        self._entries[reservation] = _Reservation(
            username=username, created_at=int(now if now is not None else time.time()),
            verifier=verifier, nonce=nonce, realm=realm, machine_id=machine_id)
        return reservation

    def owner(self, auth_state: str, username: str) -> bool:
        entry = self._entries.get(auth_state)
        return entry is not None and entry.username == username

    def reservation(self, auth_state: str, username: str) -> _Reservation | None:
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


class QoderOAuth:
    """设备码 PKCE 登录（poll 轨道）。

    `realm` 决定 openapi / website；`client` 可注入（测试与 MockTransport）。
    区域由主线程按 `QODER_API_ENDPOINT` 推导（见交付说明），一个实例服务一个
    区域；两个区域同时需要登录时用两个实例。
    """

    def __init__(
        self,
        realm: str = DEFAULT_REALM,
        *,
        client: httpx.AsyncClient | None = None,
        store: AuthStateStore | None = None,
        machine_id: str | None = None,
        openapi: str | None = None,
        website: str | None = None,
        ttl_seconds: int = AUTH_STATE_TTL_SECONDS,
        now: Any | None = None,
    ) -> None:
        self.realm = realm
        config = get_realm_config(realm)
        self.openapi = (openapi or config.openapi).rstrip("/")
        self.website = (website or config.website).rstrip("/")
        self.store = store or AuthStateStore(ttl_seconds=ttl_seconds)
        self._machine_id = machine_id
        self._client = client
        self._now = now or (lambda: int(time.time()))

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0),
                                             trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _machine(self) -> str:
        return self._machine_id or machine_id_for(self.realm)

    async def start(self, username: str) -> AuthSession:
        """生成 PKCE 与授权 URL 并登记 state（不触达上游，纯本地构造）。"""
        verifier, challenge = pkce_pair()
        config = get_realm_config(self.realm)
        nonce = str(uuid.uuid4()) if config.nonce_dashed else uuid.uuid4().hex
        machine_id = self._machine()
        auth_url = build_auth_url(self.realm, challenge=challenge, nonce=nonce,
                                  machine_id=machine_id)
        reservation = self.store.begin(username, verifier=verifier, nonce=nonce,
                                       realm=self.realm, machine_id=machine_id)
        return AuthSession(flow="poll", state=reservation, auth_url=auth_url,
                           interval=5, callback_url=None)

    async def poll(self, auth_state: str, username: str) -> AuthResult | None:
        """返回 None 表示仍在等待授权；成功后 state 被消费。"""
        reservation = self.store.reservation(auth_state, username)
        if reservation is None:
            raise UpstreamProtocolViolation("unknown or consumed auth state")
        data = await self._fetch_device_token(reservation)
        if data is None:
            return None                                    # 用户还没完成授权
        userinfo = await self._fetch_userinfo(
            credential_from_device(data, realm=reservation.realm))
        credential = credential_from_device(data, realm=reservation.realm,
                                            userinfo=userinfo)
        if not credential.access_token:
            raise UpstreamProtocolViolation("device token response missing token")
        # uid 拿不到时用 device token 前 16 位占位：随机值会让同一账号每次登录
        # 变成不同设备（COSY 机器码漂移 → 风控），前缀至少对同一 token 稳定。
        if not credential.uid:
            credential.uid = credential.access_token[:16]
        if not self.store.consume(auth_state, username):
            raise UpstreamProtocolViolation("auth state was consumed concurrently")
        return AuthResult(credential_data=credential.to_dict(),
                          nickname=credential.nickname)

    async def _fetch_device_token(self, reservation: _Reservation,
                                  ) -> dict[str, Any] | None:
        response = await self._http.get(
            f"{self.openapi}{EP_DEVICE_POLL}",
            params={"nonce": reservation.nonce, "verifier": reservation.verifier,
                    "challenge_method": CHALLENGE_METHOD},
            headers={"Accept": "application/json", "User-Agent": CLIENT_UA})
        if response.status_code in PENDING_STATUS:
            return None
        if response.status_code != 200:
            raise UpstreamProtocolViolation(
                f"device poll http {response.status_code}: "
                f"{response.content[:160]!r}")
        body = _json_object(response, "device poll")
        token = body.get("token") or body.get("device_token")
        if not isinstance(token, str) or not token.strip():
            return None                                    # 授权完成但 token 未就绪
        return body

    async def _fetch_userinfo(self, credential: QoderCredential) -> dict[str, Any]:
        """尽力补身份：失败只记日志（uid 缺失已由 poll 兜底）。"""
        if not credential.access_token:
            return {}
        try:
            response = await self._http.get(
                f"{self.openapi}{EP_USERINFO}",
                headers={"Accept": "application/json", "User-Agent": CLIENT_UA,
                         "Authorization": f"Bearer {credential.access_token}"})
        except httpx.HTTPError as error:
            logger.warning("Qoder userinfo 拉取失败: %s", error)
            return {}
        if response.status_code != 200:
            logger.warning("Qoder userinfo HTTP %s", response.status_code)
            return {}
        body = _json_object(response, "userinfo")
        return body


def _json_object(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as error:
        raise UpstreamProtocolViolation(f"{what} response is not JSON") from error
    if not isinstance(data, dict):
        raise UpstreamProtocolViolation(f"{what} response is not an object")
    return data


# `sys` 仅在类型/平台分支下使用；显式声明以免 ruff 误删（见 machine_id_for）。
