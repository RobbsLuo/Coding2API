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


# 签到设备标识：必须是 **16 位纯数字**。用 hex32（登录 URL 里的 machine/device id）
# 调 claim 会稳定得到 9074「当前参与用户太多」——这个码看起来像限流，实际是设备
# 标识格式不符（实测：hex32 → 9074，16 位数字 / 随机 hex → code:0；空串 → 9004）。
# 派生规则来自公开逆向（trae2api-more 的 CheckinDeviceID）：sha256(identity) 取模
# 10^16 后补零成 16 位；generation > 0 时把代数并入摘要，用于 9074 时轮换设备。
_CHECKIN_DEVICE_MODULUS = 10 ** 16


def checkin_device_id(identity: str, generation: int = 0) -> str:
    """派生签到的 X-Device-Id：16 位纯数字，由账号身份确定性地导出。

    identity 为空时返回空串（调用方会退回凭证自带的 device_id）。
    """
    import hashlib

    if not identity:
        return ""
    material = identity if generation <= 0 else f"{identity}#gen{generation}"
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return f"{int.from_bytes(digest, 'big') % _CHECKIN_DEVICE_MODULUS:016d}"
