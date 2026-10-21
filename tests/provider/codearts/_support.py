"""CodeArts 测试共享替身与装配助手。

`_cred`(4) / `_jwk`(5) / `_token_response`(4) / `_client`(3) 曾在该目录的
多个测试文件里各写一份逐字节等价的实现，收敛到这里。
"""

from __future__ import annotations

import time

from src.provider.codearts import dpop
from src.provider.codearts.client import CodeArtsClient
from src.provider.codearts.credential import CodeArtsCredential
from tests.helpers.http import provider_client

__all__ = ["client", "cred", "jwk", "token_response"]


def cred(**over) -> CodeArtsCredential:
    """构造 CodeArts 凭证（默认 2h 后到期）。"""
    base = {
        "uid": "u1", "user_name": "alice",
        "access_key_id": "AK", "secret_access_key": "SK", "security_token": "TOK",
        "expiration": int(time.time()) + 7200,
    }
    base.update(over)
    return CodeArtsCredential.from_dict(base)


def jwk() -> dict[str, str]:
    """新生成一对 P-256 私钥 JWK。"""
    return dpop.new_private_jwk()


def client(handler, **kw) -> CodeArtsClient:
    """用 `handler` 造 CodeArtsClient（默认填好两个测试主机）。"""
    kw.setdefault("endpoint", "https://snap.test")
    kw.setdefault("sts_host", "https://sts.test")
    return provider_client(CodeArtsClient, handler, **kw)


def token_response(**over) -> dict:
    """构造令牌交换 / 刷新的上游响应（默认第二个账号 bob）。"""
    payload = {
        "credentials": {"access_key_id": "AK2", "secret_access_key": "SK2",
                        "security_token": "TOK2", "expiration": 1999999999},
        "refresh_token": "rt2", "user_id": "u2", "user_name": "bob",
    }
    payload.update(over)
    return payload
