"""officeAce 测试共享替身与装配助手。"""

from __future__ import annotations

from src.provider.codearts import dpop
from src.provider.officeace.client import OfficeAceClient
from src.provider.officeace.credential import OfficeAceCredential
from src.provider.officeace.oauth import LoginConfig
from tests.helpers.http import provider_client

__all__ = ["MOMENT", "client", "cred", "expires_at", "jwk", "login_config",
           "token_response"]

#: 固定「当前时刻」：2026-10-21T01:30:52Z（北京时间同日 09:30，当日签到窗口内）。
MOMENT = 1792546252


def cred(**over) -> OfficeAceCredential:
    """构造 officeAce 凭证（默认两套凭据齐全、临时凭证 2h 后到期）。"""
    base = {
        "account_id": "acct-1",
        "principal_id": "prin-1",
        "user_name": "alice",
        "nickname": "alice",
        "model_app_key": "AK",
        "model_app_secret": "SK",
        "model_base_url": "https://gw.test/v2",
        "access_key_id": "AK",
        "secret_access_key": "SK",
        "security_token": "TOK",
        "project_id": "pid",
        "expiration": MOMENT + 7200,
        "refresh_token": "rt",
        "dpop_private_jwk": jwk(),
    }
    base.update(over)
    return OfficeAceCredential.from_dict(base)


def jwk() -> dict[str, str]:
    """新生成一对 P-256 私钥 JWK。"""
    return dpop.new_private_jwk()


def client(handler, **kw) -> OfficeAceClient:
    """用 `handler` 造 OfficeAceClient（默认填好三个测试主机与固定时钟）。"""
    kw.setdefault("cloud_base", "https://cloud.test")
    kw.setdefault("gateway", "https://gw.test")
    kw.setdefault("login", login_config())
    kw.setdefault("now", lambda: MOMENT)
    return provider_client(OfficeAceClient, handler, **kw)


def login_config(**over) -> LoginConfig:
    """测试用登录配置（地址全指向假主机）。"""
    base = {
        "cloud_base": "https://cloud.test",
        "auth_base": "https://auth.test",
        "sts_host": "https://sts.test",
    }
    base.update(over)
    return LoginConfig(**base)


def token_response(**over) -> dict:
    """构造令牌交换 / 刷新的上游响应（默认第二个账号 bob）。"""
    payload = {
        "credentials": {"access_key_id": "AK2", "secret_access_key": "SK2",
                        "security_token": "TOK2", "project_id": "pid2",
                        "expiration": 1999999999},
        "refresh_token": "rt2", "id_token": "", "user_id": "prin-2",
    }
    payload.update(over)
    return payload


def expires_at(seconds_from_moment: int) -> int:
    """相对固定时钟的到期时刻（测试里写相对量比写绝对时间可读）。"""
    return MOMENT + seconds_from_moment
