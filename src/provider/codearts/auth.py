"""CodeArts 登录闭环：PKCE + ticket 轮询 + 授权码/刷新令牌换 STS 临时凭证。

流程（逆向记录 §3）：

1. 生成 `ticket_id`（hex32）、`secret`（hex32）、PKCE `code_verifier/challenge`；
2. 打开 `{portal}/authorize?...` 让用户授权；
3. 两条通道拿结果：
   * 回调带 `code` → `POST {snap-manager}/v1/oauth2/tokens`（authorization_code）；
   * 轮询 `GET {snap-manager}/v1/login/ticket?ticket_id=&secret=`（兜底通道）。
4. 刷新 → `POST {sts}/v1/oauth2/tokens`（refresh_token），**必须带 DPoP 证明**。

本模块只做「构造请求与解析响应」，HTTP 客户端由调用方注入（`client.py` 持有
连接池，测试注入 MockTransport）。凭证归一化在 `credential.py`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from . import dpop
from .events import (
    CLIENT_ID,
    EP_LOGIN_TICKET,
    EP_OAUTH_TOKENS,
    PORTAL_HOST,
    SNAP_ENGINE_HOST,
    SNAP_MANAGER_PREFIX,
    STS_HOST,
    UpstreamProtocolViolation,
)

# 官方 huaweicloud.authentication 插件用的插件名/版本（authorize URL 与 ticket
# 轮询头都要带上；漏掉会被门户拒绝）。
PLUGIN_NAME = "huaweicloud.authentication"
PLUGIN_VERSION = "1.0.0"
REDIRECT_PATH = "/oauth/callback"
# portal 只认插件实际使用的方法名，"S256" 会被拒（逆向记录 §3 的 URL 实测）。
CODE_CHALLENGE_METHOD = "SHA-256"


class TokenEndpointError(RuntimeError):
    """令牌端点的非 2xx（由 client 转成 UpstreamHTTPError 参与错误分类）。"""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        text = body.decode("utf-8", errors="replace")
        super().__init__(f"token endpoint http {status}: {text[:200]}")


@dataclass(frozen=True, slots=True)
class LoginConfig:
    """登录相关地址与插件标识（生产用默认值，测试可整组替换）。"""

    client_id: str = CLIENT_ID
    portal_host: str = PORTAL_HOST
    snap_manager: str = SNAP_ENGINE_HOST + SNAP_MANAGER_PREFIX
    sts_host: str = STS_HOST
    redirect_path: str = REDIRECT_PATH
    plugin_name: str = PLUGIN_NAME
    plugin_version: str = PLUGIN_VERSION

    @property
    def token_url(self) -> str:
        """换 token 的端点（authorization_code 与 refresh_token 共用 STS 域）。"""
        return self.sts_host + EP_OAUTH_TOKENS


@dataclass(frozen=True, slots=True)
class LoginSession:
    """一次登录尝试的全部状态；`dpop_private_jwk` 必须与 refresh_token 一起落盘。"""

    ticket_id: str
    secret: str
    code_verifier: str
    code_challenge: str
    auth_url: str
    dpop_private_jwk: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TokenExchange:
    """令牌响应 + 本次使用的 DPoP 私钥（登录时由本模块生成）。"""

    tokens: dict[str, Any]
    dpop_private_jwk: dict[str, str]


def new_ticket_identity() -> tuple[str, str]:
    """生成 `(ticket_id, secret)`，各 32 位十六进制（逆向记录 §3）。"""
    return secrets.token_hex(16), secrets.token_hex(16)


def pkce_pair(verifier: str | None = None) -> tuple[str, str]:
    """PKCE `(code_verifier, code_challenge)`；`challenge = BASE64URL(SHA256(v))`。"""
    value = verifier or secrets.token_urlsafe(64)
    digest = hashlib.sha256(value.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return value, challenge


def build_authorize_url(config: LoginConfig, *, ticket_id: str, code_challenge: str,
                        port: int) -> str:
    """门户授权链接（参数集与官方插件逐项对齐，勿增删）。"""
    query = urlencode({
        "theme": "2",
        "locale": "zh-cn",
        "uri_scheme": config.client_id,
        "client_id": config.client_id,
        "port": str(port),
        "code_challenge": code_challenge,
        "code_challenge_method": CODE_CHALLENGE_METHOD,
        "ticket_id": ticket_id,
        "plugin-name": config.plugin_name,
        "plugin-version": config.plugin_version,
    })
    return f"{config.portal_host}/authorize?{query}"


def new_login_session(config: LoginConfig, *, port: int) -> LoginSession:
    """生成一次完整登录所需的随机量与 URL（DPoP 私钥同时生成）。"""
    ticket_id, secret = new_ticket_identity()
    verifier, challenge = pkce_pair()
    return LoginSession(
        ticket_id=ticket_id, secret=secret, code_verifier=verifier,
        code_challenge=challenge,
        auth_url=build_authorize_url(config, ticket_id=ticket_id,
                                     code_challenge=challenge, port=port),
        dpop_private_jwk=dpop.new_private_jwk())


def redirect_uri(config: LoginConfig, port: int) -> str:
    return f"http://127.0.0.1:{port}{config.redirect_path}"


def _parse_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as error:
        raise UpstreamProtocolViolation(
            f"non-JSON token response from {response.request.url}") from error
    if not isinstance(data, dict):
        raise UpstreamProtocolViolation("token response is not an object")
    return data


async def _post_form(client: httpx.AsyncClient, url: str, form: dict[str, str],
                     dpop_private_jwk: dict[str, str]) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "DPoP": dpop.sign_proof(dpop_private_jwk, url),
    }
    response = await client.post(url, data=form, headers=headers)
    if response.status_code >= 400:
        raise TokenEndpointError(response.status_code, response.content)
    return _parse_json(response)


async def exchange_code(client: httpx.AsyncClient, config: LoginConfig, *, code: str,
                        code_verifier: str, port: int,
                        dpop_private_jwk: dict[str, str] | None = None,
                        ) -> TokenExchange:
    """授权码换 token；未提供私钥时新生成一对并随结果返回（须落盘）。"""
    private_jwk = dpop_private_jwk or dpop.new_private_jwk()
    tokens = await _post_form(client, config.token_url, {
        "client_id": config.client_id,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(config, port),
    }, private_jwk)
    return TokenExchange(tokens=tokens, dpop_private_jwk=private_jwk)


async def refresh_tokens(client: httpx.AsyncClient, config: LoginConfig, *,
                         refresh_token: str, dpop_private_jwk: dict[str, str],
                         code_verifier: str = "") -> dict[str, Any]:
    """refresh_token 换新临时凭证。

    必须传入**签发该 refresh_token 时那一对** DPoP 私钥；缺私钥直接拒绝本地
    发车（上游会回 `InvalidDPoPHeader`，本地报错更快也更清楚）。
    """
    if not refresh_token:
        raise UpstreamProtocolViolation("credential missing refresh_token")
    if not dpop_private_jwk:
        raise UpstreamProtocolViolation(
            "credential missing dpop_private_jwk (required to refresh)")
    return await _post_form(client, config.token_url, {
        "client_id": config.client_id,
        "code_verifier": code_verifier,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, dpop_private_jwk)


async def poll_ticket(client: httpx.AsyncClient, config: LoginConfig, *,
                      ticket_id: str, secret: str) -> dict[str, Any]:
    """轮询 ticket 通道（兜底）：返回原始令牌响应，无结果时返回空 dict。

    `not_logged_in` 等「还没扫码/还没授权」的状态按空结果处理——轮询是给前端
    反复调的，不能把「等待中」当错误抛。
    """
    headers = {"plugin-name": config.plugin_name, "plugin-version": config.plugin_version}
    response = await client.get(
        f"{config.snap_manager}{EP_LOGIN_TICKET}",
        params={"ticket_id": ticket_id, "secret": secret}, headers=headers)
    if response.status_code >= 400:
        raise TokenEndpointError(response.status_code, response.content)
    data = _parse_json(response)
    if not _has_credentials(data):
        return {}
    return data


def _has_credentials(tokens: dict[str, Any]) -> bool:
    credentials = tokens.get("credentials")
    credentials = credentials if isinstance(credentials, dict) else {}
    legacy = tokens.get("credential")
    legacy = legacy if isinstance(legacy, dict) else {}
    return bool(credentials.get("security_token") or legacy.get("securitytoken"))


def credential_data_from_tokens(tokens: dict[str, Any],
                                dpop_private_jwk: dict[str, str]) -> dict[str, Any]:
    """令牌响应 + DPoP 私钥 → 落库用扁平凭证 dict。"""
    from .credential import parse_credentials

    credential = parse_credentials(tokens)
    credential.dpop_private_jwk = dict(dpop_private_jwk)
    if not credential.nickname:
        credential.nickname = credential.user_name
    return credential.to_dict()


def dump_jwk(jwk: dict[str, str]) -> str:
    """JWK → 紧凑 JSON（日志/调试用，不落库；落库走 to_dict）。"""
    return json.dumps(jwk, separators=(",", ":"), sort_keys=True)
