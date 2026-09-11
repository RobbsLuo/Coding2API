"""TRAE 登录闭环：登录 URL 构造 + 回调链接解析（Q17=C callback 轨道）。

回调地址来自 PUBLIC_BASE_URL，废弃原实现的 127.0.0.1:18080 硬编码。
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlparse

from .credential import TraeCredential
from .events import CLIENT_ID, IDE_VERSION, UpstreamProtocolViolation

LOGIN_VERSION = "1"
PLUGIN_VERSION = "2.3.62834"
LOGIN_HOST = "https://www.trae.cn"
LOGIN_PATH = "/authorization"


@dataclass(slots=True)
class CallbackInfo:
    refresh_token: str
    uid: str
    nickname: str
    enterprise_id: str
    expires_at: int


def new_machine_identity() -> tuple[str, str]:
    """生成 hex32 的 machine_id / device_id（登录 URL 与落盘凭证共用）。"""
    return secrets.token_hex(16), secrets.token_hex(16)


def build_login_url(callback_url: str, *, machine_id: str, device_id: str) -> str:
    """构造 TRAE 登录 URL（复刻原实现的参数集，勿增删）。

    端点是 ConsoleHost + /authorization；machine/device id 与落盘凭证共用同一对，
    否则登录态与凭证不匹配。
    """
    trace_id = (machine_id + device_id)[-16:]
    query = (
        f"login_version={LOGIN_VERSION}"
        "&auth_from=solo"
        "&login_channel=native_ide"
        f"&plugin_version={PLUGIN_VERSION}"
        "&auth_type=local"
        f"&client_id={CLIENT_ID}"
        "&redirect=0"
        "&auth_callback_url=" + quote(callback_url, safe="") +
        f"&login_trace_id={trace_id}"
        f"&machine_id={quote(machine_id, safe='')}"
        f"&device_id={quote(device_id, safe='')}"
        f"&x_device_id={quote(device_id, safe='')}"
        f"&x_machine_id={quote(machine_id, safe='')}"
        "&x_device_brand=PC"
        "&x_device_type=PC"
        "&x_os_version=1.0"
        f"&x_app_version={quote(IDE_VERSION, safe='')}"
        "&x_app_type=stable"
    )
    return f"{LOGIN_HOST}{LOGIN_PATH}?{query}"


def _get_string(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _get_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _parse_json_param(raw: str) -> dict:
    """回调参数是 URL 编码的 JSON，可能被二次编码。"""
    text = unquote(raw)
    for _ in range(2):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            text = unquote(text)
            continue
        return parsed if isinstance(parsed, dict) else {}
    return {}


def parse_callback_url(raw_url: str) -> CallbackInfo:
    """解析浏览器回调链接，提取 refreshToken / userInfo。

    refreshToken 缺失时回退 userJwt 内的 Token/RefreshToken（原实现的容错）。
    """
    if not raw_url or not raw_url.strip():
        raise UpstreamProtocolViolation("empty callback url")
    query = parse_qs(urlparse(raw_url.strip()).query)

    refresh_token = (query.get("refreshToken") or [""])[0]
    user_info: dict = {}
    if query.get("userInfo"):
        user_info = _parse_json_param(query["userInfo"][0])
    user_jwt: dict = {}
    if query.get("userJwt"):
        user_jwt = _parse_json_param(query["userJwt"][0])

    if not refresh_token:
        token = _get_string(user_jwt, "Token")
        refresh_token = _get_string(user_jwt, "RefreshToken") or token
    if not refresh_token:
        raise UpstreamProtocolViolation("callback missing refreshToken and userJwt.Token")

    return CallbackInfo(
        refresh_token=refresh_token,
        uid=_get_string(user_info, "uid") or _get_string(user_info, "UID"),
        nickname=_get_string(user_info, "nickname") or _get_string(user_info, "ScreenName"),
        enterprise_id=_get_string(user_info, "enterpriseId")
        or _get_string(user_info, "EnterpriseID"),
        expires_at=_get_int(user_info, "expiresAt"),
    )


def credential_from_callback(info: CallbackInfo, access_token: str, *,
                             machine_id: str, device_id: str,
                             api_host: str = "https://api.trae.com.cn") -> TraeCredential:
    """回调信息 + ExchangeToken 结果 → 可入库凭证。"""
    return TraeCredential(
        uid=info.uid, access_token=access_token, refresh_token=info.refresh_token,
        expires_at=info.expires_at or int(time.time()) + 3600, domain="trae.cn",
        api_host=api_host, machine_id=machine_id, device_id=device_id,
        enterprise_id=info.enterprise_id, nickname=info.nickname,
    )
