"""TRAE 凭证类型与解析（独立模块，避免 client ↔ callback 循环导入）。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .events import UpstreamProtocolViolation


@dataclass(slots=True)
class TraeCredential:
    """归一化后的 TRAE 凭证（嵌套形与扁平形共用）。"""

    uid: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    domain: str = "trae.cn"
    api_host: str = ""
    machine_id: str = ""
    device_id: str = ""
    enterprise_id: str = ""
    nickname: str = ""

    def needs_refresh(self, skew_seconds: int, now: int | None = None) -> bool:
        if self.expires_at <= 0:
            return True
        current = int(now if now is not None else time.time())
        return current + skew_seconds >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid, "accessToken": self.access_token,
            "refreshToken": self.refresh_token, "expiresAt": self.expires_at,
            "domain": self.domain, "apiHost": self.api_host,
            "machineId": self.machine_id, "deviceId": self.device_id,
            "enterpriseId": self.enterprise_id, "nickname": self.nickname,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TraeCredential:
        return cls(
            uid=str(raw.get("uid") or ""),
            access_token=str(raw.get("accessToken") or raw.get("access_token") or ""),
            refresh_token=str(raw.get("refreshToken") or raw.get("refresh_token") or ""),
            expires_at=int(raw.get("expiresAt") or raw.get("expires_at") or 0),
            domain=str(raw.get("domain") or "trae.cn"),
            api_host=str(raw.get("apiHost") or raw.get("api_host") or ""),
            machine_id=str(raw.get("machineId") or raw.get("machine_id") or ""),
            device_id=str(raw.get("deviceId") or raw.get("device_id") or ""),
            enterprise_id=str(raw.get("enterpriseId") or raw.get("enterprise_id") or ""),
            nickname=str(raw.get("nickname") or ""),
        )


def parse_credential(raw: bytes | dict[str, Any]) -> TraeCredential:
    """兼容嵌套形 {"auth":{...},"account":{...}} 与扁平形。"""
    if isinstance(raw, bytes):
        try:
            raw = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise UpstreamProtocolViolation("credential is not valid JSON") from error
    if not isinstance(raw, dict):
        raise UpstreamProtocolViolation("credential is not an object")

    if "auth" in raw:
        auth = raw.get("auth")
        account = raw.get("account")
        if not isinstance(auth, dict) or not isinstance(account, dict):
            raise UpstreamProtocolViolation("nested credential sections must be objects")
        merged = {
            "accessToken": auth.get("accessToken"), "refreshToken": auth.get("refreshToken"),
            "expiresAt": auth.get("expiresAt"), "domain": auth.get("domain"),
            "apiHost": auth.get("apiHost"), "machineId": auth.get("machineId"),
            "deviceId": auth.get("deviceId"), "uid": account.get("uid"),
            "enterpriseId": account.get("enterpriseId"), "nickname": account.get("nickname"),
        }
        credential = TraeCredential.from_dict(merged)
    else:
        credential = TraeCredential.from_dict(raw)

    if not credential.access_token:
        raise UpstreamProtocolViolation("credential missing accessToken")
    return credential
