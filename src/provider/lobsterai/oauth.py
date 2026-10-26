"""LobsterAI 登录闭环：登录 URL 构造 + 回调链接解析（paste 轨道）。

**为什么是 paste 轨道而不是 callback 轨道**：上游门户只接受
**回环地址**（`http://127.0.0.1:<port>/auth/callback`）作为 `redirect_uri`
（见官方客户端的本地回调设计文档），而本服务可能部署在远端 / NAS /
容器里——浏览器访问 `127.0.0.1` 指的是**用户自己那台机器**，不是本服务。
故采用与 CodeArts 相同的「粘贴回调链接」轨道：门户把浏览器重定向到一个
打不开的回环地址，`code` 只留在浏览器地址栏里，用户把整条链接粘回来，
服务端据此换 token。

**没有 PKCE、没有 DPoP**：上游只认 `authCode` + 客户端身份三元组
（`uuid` / `firstKeyfrom` / `latestKeyfrom`）。这三个字段由**本地生成**
并在每次 exchange / refresh 时原样回放——上游据此认定「同一台客户端」。

**state 双重校验**：上游 `state` 与本地 reservation 是**两个不同的值**。
上游 state 放进登录 URL（浏览器可见、门户原样回显），用于把回调钉死在
那一次登录；本地 reservation 是服务端签发的会话键（前端持有、回传时用于
定位这次登录）。两者不混用：拿上游 state 当会话键等于让任何人凭 URL
消费别人的登录。
"""

from __future__ import annotations

import secrets
import time
import uuid as uuid_module
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlparse

from ..base import UpstreamProtocolViolation
from . import events as lobsterai_events

#: 登录超时（秒）：门户授权 + 用户切回浏览器完成粘贴，给足 10 分钟。
AUTH_STATE_TTL_SECONDS = 600

#: 回调用的回环地址端口。**不真的监听**——只为了让门户把浏览器重定向到
#: 一个「打不开的本地地址」，`code` 留在地址栏里供用户粘贴。取一个不常用
#: 的端口，降低与用户本机既有服务撞车（撞上时浏览器会打开那个服务，地址栏
#: 里就没有 `code` 了）。
CALLBACK_PORT = 52641
CALLBACK_PATH = "/auth/callback"

#: 登录 URL 的 fragment 路由（门户是 SPA，参数在 `#` 之后的 query 里）。
LOGIN_FRAGMENT = "/portal#/login"


@dataclass(slots=True)
class LobsteraiLoginSession:
    """一次登录的客户端侧材料（本地生成，不含任何上游凭据）。"""

    #: 客户端安装标识（exchange / refresh 都要带，丢了就续不了期）。
    uuid: str
    #: 首次登录时间戳（毫秒字符串）。
    first_keyfrom: str
    #: 上游 state（放进登录 URL，门户原样回显）。
    upstream_state: str


def new_session() -> LobsteraiLoginSession:
    """生成一次登录的客户端身份三元组 + 上游 state。"""
    return LobsteraiLoginSession(
        uuid=str(uuid_module.uuid4()),
        first_keyfrom=str(int(time.time() * 1000)),
        upstream_state=secrets.token_urlsafe(16),
    )


def build_login_url(session: LobsteraiLoginSession, *,
                    portal_base: str = lobsterai_events.PORTAL_BASE,
                    ) -> str:
    """构造门户登录 URL。

    参数（照上游客户端的形态，勿增删）：

    * `source=electron`：告诉门户走客户端登录通道；
    * `redirect_uri`：回环回调地址（门户只接受回环地址）；
    * `state`：本次登录的随机 state，门户原样回显。
    """
    redirect_uri = (f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}")
    base = f"{portal_base.rstrip('/')}{LOGIN_FRAGMENT}"
    return (f"{base}?source=electron"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            f"&state={quote(session.upstream_state, safe='')}")


@dataclass(slots=True)
class CallbackInfo:
    """回调链接解析结果。"""

    code: str
    state: str


def parse_callback_url(raw_url: str) -> CallbackInfo:
    """从粘贴内容里取 `code` 与 `state`。

    接受三种输入：完整回调 URL（`http://127.0.0.1:52641/auth/callback?code=…&state=…`）、
    只截取的 query 串（`code=…&state=…`）、以及只有裸 `code` 的情况
    （此时没有 state 可校验，调用方须自行决定是否放行——本模块的调用点
    要求 state 一致，故裸 code 会被拒）。
    """
    value = (raw_url or "").strip()
    if not value:
        raise UpstreamProtocolViolation("empty callback url")
    query = (urlparse(value).query if "://" in value or "?" in value
             else value)
    params = parse_qs(query)
    code = (params.get("code") or [""])[0].strip()
    state = (params.get("state") or [""])[0].strip()
    if not code:
        raise UpstreamProtocolViolation(
            "callback URL is missing the authorization code")
    return CallbackInfo(code=code, state=state)
