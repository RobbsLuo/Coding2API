"""TRAE 凭证类型与解析（独立模块，避免 client ↔ callback 循环导入）。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ..token_expiry import credential_expiry
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

    def token_expires_at(self) -> int:
        """access token 到期 epoch（秒）：显式 expiresAt 缺失时回落 JWT `exp`。

        与 CodeBuddy 同一套回落（TRAE 的 expiresAt 实测一直是真实值，
        这里只在导入脏凭证/上游省略该字段时兜底）。
        """
        return credential_expiry(self.to_dict())

    def needs_refresh(self, skew_seconds: int, now: int | None = None) -> bool:
        # 到期未知 → True（保守刷新；见 test_credential_needs_refresh）
        expires_at = self.token_expires_at()
        if expires_at <= 0:
            return True
        current = int(now if now is not None else time.time())
        return current + skew_seconds >= expires_at

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
            # 只存上游显式给出的 expiresAt；JWT 派生值走 token_expires_at()，
            # 不落回 JSON——否则刷新换到新 token 后旧派生值会残留成「权威」到期
            # （与 CodeBuddy 同一纪律，两个渠道该字段的语义必须一致）。
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


# 签到设备标识：x-device-id 需要是**数字串**且**不能复用**。
#
# 这一段被推翻过两次，把观测数据留在这里，避免再凭单次对照下结论：
#   同一账号 claim 结果（✓=code:0，✗=9074）：
#     登录 deviceId(hex32)  → ✗        派生 16 位数字 → ✗（4 次全失败）
#     随机 16 位数字        → ✓        登录 machineId(hex32) → ✗
#     空串                  → 9004 参数错误
#   **一旦某个账号当天签到成功，之后任何 device_id 都返回 code:0**（幂等），
#   所以「成功」必须配合 status.checked_in 判断，否则会得出诸如「hex32 能过」
#   这类错误结论。
#
# 结论：9074 与 device_id 的具体取值关系未确定（数字串是必要条件，非充分条件）。
# 因此不猜格式，改为「每次 claim 生成一个新的随机数字串 + 9074 退避重试」：
# 成熟实现（trae2api-more / auto-checkin-hub）同样把 9074 当限流退避，
# 退避节奏 60→120→240→480s 封顶。
_CHECKIN_DEVICE_DIGITS = 16


def new_checkin_device_id() -> str:
    """生成一个全新的签到设备 ID：16 位数字串。

    每次调用都不同——设备号复用是 9074 的可疑诱因之一（观测：派生值即
    「同 uid 永远同样的号」，用久后连续失败；换成没用过的号当次即成功）。
    不持久化：设备号只是签到 API 的校验参数，上游按 uid 记账，换号不影响发放。
    """
    import secrets

    return f"{secrets.randbelow(10 ** _CHECKIN_DEVICE_DIGITS):0{_CHECKIN_DEVICE_DIGITS}d}"
