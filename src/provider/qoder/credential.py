"""Qoder 凭证类型与解析（独立模块，避免 client / cosy / auth 循环导入）。

Qoder 是**真实账号渠道**：凭证来自设备码 PKCE 登录（`dt-` access token +
`drt-` refresh token），没有虚拟凭证。字段与协议事实的对应关系：

* `access_token`：`Authorization: Bearer <token>` 与 COSY `info` 里的
  `security_oauth_token` 都用它；
* `refresh_token`：`drt-` 前缀走 `/api/v1/deviceToken/refresh`，是保活的唯一
  依据（无 refresh_token 的凭证到期即硬失效，预刷新判定必须能识别出来）；
* `uid`：稳定派生 `cosy-machineid` / `cosy-machinetoken` 的种子，缺失时
  `cosy.py` 回落到 `"anonymous"`（同一进程内稳定，但多账号会被上游关联）；
* `realm`：区域（cn / intl），决定 openapi / gateway / 签到端点是否存在；
* `user_type` / `organization_id` / `organization_name`：COSY `info` 的身份
  JSON 字段，上游按它们做权益路由。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ...provider.token_expiry import credential_expiry
from .events import (
    DEFAULT_REALM,
    DEFAULT_USER_TYPE,
    detect_realm_from_domain,
    get_realm_config,
)

# 凭证 JSON 里可能承载 access token 的键名（顺序即优先级）。
_TOKEN_KEYS: tuple[str, ...] = (
    "access_token", "accessToken", "token", "bearer_token", "bearerToken")
# 到期时间的候选键名。
_EXPIRY_KEYS: tuple[str, ...] = ("expires_at", "expiresAt")


def _opt_str(raw: dict[str, Any], *keys: str) -> str:
    """取第一个非空字符串值（键名兼容多种上游形状）。"""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _opt_int(raw: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return int(value)
    return 0


@dataclass(slots=True)
class QoderCredential:
    """归一化凭证。`uid` 缺失也合法（COSY 会话按 anonymous 派生设备指纹）。"""

    access_token: str = ""
    uid: str = ""
    nickname: str = ""
    realm: str = DEFAULT_REALM
    refresh_token: str = ""
    expires_at: int = 0
    auth_source: str = "unknown"        # manual | oauth | unknown
    user_type: str = DEFAULT_USER_TYPE
    organization_id: str = ""
    organization_name: str = ""
    domain: str = ""

    @property
    def is_oauth(self) -> bool:
        return self.auth_source == "oauth"

    def realm_config(self):
        """本凭证所属区域的地址与登录参数（未知区域回落国内版）。"""
        return get_realm_config(self.realm)

    def token_expires_at(self) -> int:
        """access token 到期 epoch（秒）：显式 `expires_at` 缺失时回落 JWT `exp`。

        上游 deviceToken 响应带 `expires_in`/`expires_at`，但刷新与部分老响应
        可能都不带；此时只有 JWT `exp` 能给出权威到期时间（同 CodeBuddy）。
        两者都拿不到返回 0（未知）——不猜 TTL。
        """
        return credential_expiry(self.to_dict())

    def needs_refresh(self, skew_seconds: int, now: int | None = None) -> bool:
        """只有 OAuth 凭证参与刷新；手工/pAT 凭证没有 refresh_token 时永不刷新。

        到期时间取 `token_expires_at()`（含 JWT 回落），不能只看 `expires_at`：
        上游部分响应不带显式到期时间，只看它会让预刷新永不触发，
        token 过期后聊天 401 → 被分类成 SOFT/DEAD，凭证无法自愈。
        """
        if not self.is_oauth or not self.refresh_token:
            return False
        expires_at = self.token_expires_at()
        if expires_at <= 0:
            return False
        current = int(now if now is not None else time.time())
        return current + skew_seconds >= expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "uid": self.uid,
            "nickname": self.nickname,
            "realm": self.realm,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "auth_source": self.auth_source,
            "user_type": self.user_type,
            "organization_id": self.organization_id,
            "organization_name": self.organization_name,
            "domain": self.domain,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> QoderCredential:
        if not isinstance(raw, dict):
            raw = {}
        source = raw.get("auth_source")
        realm = _opt_str(raw, "realm", "region")
        domain = _opt_str(raw, "domain")
        if not realm:
            # 区域优先显式字段，其次按域名推断（历史凭证只有 domain）
            realm = detect_realm_from_domain(domain) if domain else DEFAULT_REALM
        return cls(
            access_token=_opt_str(raw, *_TOKEN_KEYS),
            uid=_opt_str(raw, "uid", "user_id", "userId", "id"),
            nickname=_opt_str(raw, "nickname", "name"),
            realm=realm,
            refresh_token=_opt_str(raw, "refresh_token", "refreshToken"),
            # 只存上游显式给出的到期时间；JWT 派生值走 token_expires_at()，
            # 不落回 JSON——否则刷新换到新 token 后旧派生值会残留成「权威」到期。
            expires_at=_opt_int(raw, *_EXPIRY_KEYS),
            auth_source=source if source in ("manual", "oauth") else "unknown",
            user_type=_opt_str(raw, "user_type", "userType") or DEFAULT_USER_TYPE,
            organization_id=_opt_str(raw, "organization_id", "organizationId"),
            organization_name=_opt_str(raw, "organization_name", "organizationName"),
            domain=domain,
        )


def _device_token(data: dict[str, Any]) -> str:
    """deviceToken 响应里的 access token（两种键名上游都用过）。"""
    for key in ("token", "device_token", "access_token"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def expires_from_device(data: dict[str, Any]) -> int:
    """deviceToken 响应的到期 epoch（秒）；不可得返回 0。

    上游三种形态（按可靠性排序）：`expires_in`（**毫秒**）/ `expires_at`
    （RFC3339，可能带时区）/ 都不给。前两种直接可用；都不给时返回 0 而不是
    参考实现的「默认 30 天」——30 天是编造值，会让预刷新在真实到期前 30 天
    就无条件发车；这里让 `token_expires_at()` 回落到 access token 的 JWT `exp`。
    """
    expires_in = data.get("expires_in")
    if (isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
            and expires_in > 0):
        # 上游给的是毫秒（同参考实现），转秒
        return int(time.time()) + int(expires_in) // 1000
    raw = data.get("expires_at")
    if isinstance(raw, str) and raw.strip():
        from datetime import datetime

        text = raw.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return 0
        return int(parsed.timestamp())
    return 0


def credential_from_device(
    data: dict[str, Any], *,
    realm: str = DEFAULT_REALM,
    userinfo: dict[str, Any] | None = None,
    auth_source: str = "oauth",
    fallback_uid: str = "",
) -> QoderCredential:
    """deviceToken 响应（+ 可选 userinfo）→ 落库凭证。

    uid 优先取 userinfo（`id`），其次 deviceToken 响应的 `user_id`，最后用
    `fallback_uid` 兜底（无 uid 时 COSY 设备指纹退化成 anonymous，多账号会被
    上游关联，因此登录流程只能给一个明确的占位值，绝不能随机造）。
    """
    info = userinfo if isinstance(userinfo, dict) else {}
    uid = (_opt_str(info, "id")
           or _opt_str(data, "user_id", "userId", "uid")
           or fallback_uid)
    return QoderCredential(
        access_token=_device_token(data),
        uid=uid,
        nickname=_opt_str(info, "name", "nickname"),
        realm=realm,
        refresh_token=_opt_str(data, "refresh_token", "refreshToken"),
        expires_at=expires_from_device(data),
        auth_source=auth_source,
        user_type=_opt_str(info, "user_type", "userType") or DEFAULT_USER_TYPE,
        organization_id=_opt_str(info, "organization_id", "organizationId"),
        organization_name=_opt_str(info, "organization_name", "organizationName"),
        domain=get_realm_config(realm).domain,
    )


def merge_refreshed(
    credential: QoderCredential, data: dict[str, Any],
) -> QoderCredential:
    """deviceToken/refresh 响应 → 新凭证（保留身份字段，覆盖 token 与到期）。

    上游刷新响应只回 token 族字段，不含 uid/昵称/组织；这些必须沿用旧值，
    否则刷新一次就把账号身份刷没了（COSY `info` 会退化成 anonymous 设备）。
    """
    return QoderCredential(
        access_token=_device_token(data) or credential.access_token,
        uid=credential.uid,
        nickname=credential.nickname,
        realm=credential.realm,
        refresh_token=_opt_str(data, "refresh_token", "refreshToken")
        or credential.refresh_token,
        expires_at=expires_from_device(data) or credential.expires_at,
        auth_source=credential.auth_source,
        user_type=credential.user_type,
        organization_id=credential.organization_id,
        organization_name=credential.organization_name,
        domain=credential.domain or get_realm_config(credential.realm).domain,
    )


def parse_credential(raw: bytes | dict[str, Any], *,
                     auth_source: str = "manual") -> QoderCredential:
    """手动导入路径：接受 access_token / token / bearer_token 任一键名。

    `auth_source` 只能由可信创建入口显式写入：手动添加为 manual，OAuth 为
    oauth（AGENTS.md 约束）。字段存在但非法时保持 unknown，绝不默认成 manual。
    """
    if isinstance(raw, bytes):
        try:
            raw = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            from .events import UpstreamProtocolViolation

            raise UpstreamProtocolViolation("credential is not valid JSON") from error
    if not isinstance(raw, dict):
        from .events import UpstreamProtocolViolation

        raise UpstreamProtocolViolation("credential is not an object")

    credential = QoderCredential.from_dict(raw)
    if not credential.access_token:
        from .events import UpstreamProtocolViolation

        raise UpstreamProtocolViolation("credential missing access token")
    if "auth_source" not in raw:
        credential.auth_source = auth_source
    return credential
