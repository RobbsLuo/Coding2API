"""CodeBuddy 凭证类型与解析（独立模块，避免 client 与其他子模块循环导入）。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ..token_expiry import credential_expiry
from .events import UpstreamProtocolViolation


@dataclass(slots=True)
class CodeBuddyCredential:
    """归一化凭证。bearer-only 手动凭证只有 bearer_token 也是合法的。"""

    bearer_token: str = ""
    user_id: str = ""
    account_uid: str = ""
    domain: str = ""
    enterprise_id: str = ""
    department_full_name: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    auth_source: str = "unknown"          # manual | oauth | unknown
    quota_probe_mode: str = "personal"    # personal | enterprise
    nickname: str = ""

    @property
    def is_oauth(self) -> bool:
        return self.auth_source == "oauth"

    def token_expires_at(self) -> int:
        """access token 到期 epoch（秒）：显式 expires_at 缺失时回落 JWT `exp`。

        实测 CodeBuddy 的 token 响应（OAuth 登录与刷新）都不带 `expires_at` /
        `created_at` / `expires_in`，只看 `expires_at` 会得到恒为 0 的未知值，
        预刷新永不触发——token 过期后被上游 401 硬禁用，而 revive 不自愈。
        """
        return credential_expiry(self.to_dict())

    def needs_refresh(self, skew_seconds: int, now: int | None = None) -> bool:
        """只有 OAuth 凭证参与刷新；bearer-only 手动凭证永不刷新（AGENTS.md）。

        到期时间取 `token_expires_at()`（含 JWT 回落），不能直接用 `expires_at`：
        后者对 CodeBuddy 恒为 0，会让预刷新永不触发。
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
            "bearer_token": self.bearer_token, "user_id": self.user_id,
            "account_uid": self.account_uid, "domain": self.domain,
            "enterprise_id": self.enterprise_id,
            "department_full_name": self.department_full_name,
            "refresh_token": self.refresh_token, "expires_at": self.expires_at,
            "auth_source": self.auth_source, "quota_probe_mode": self.quota_probe_mode,
            "nickname": self.nickname,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CodeBuddyCredential:
        source = raw.get("auth_source")
        return cls(
            bearer_token=str(raw.get("bearer_token") or raw.get("bearerToken") or ""),
            user_id=str(raw.get("user_id") or ""),
            account_uid=str(raw.get("account_uid") or ""),
            domain=str(raw.get("domain") or ""),
            enterprise_id=str(raw.get("enterprise_id") or ""),
            department_full_name=str(raw.get("department_full_name") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            # 只存上游显式给出的 expires_at；JWT 派生值走 token_expires_at()，
            # 不落回 JSON——否则刷新换到新 token 后旧派生值会残留成「权威」到期。
            expires_at=int(raw.get("expires_at") or 0),
            auth_source=source if source in ("manual", "oauth") else "unknown",
            quota_probe_mode="enterprise"
            if raw.get("quota_probe_mode") == "enterprise" else "personal",
            nickname=str(raw.get("nickname") or ""),
        )


def parse_credential(raw: bytes | dict[str, Any], *,
                     auth_source: str = "manual") -> CodeBuddyCredential:
    """手动导入路径：接受 token / access_token / bearer_token 任一键名。

    auth_source 只能由可信创建入口显式写入：手动添加为 manual，OAuth 为 oauth。
    """
    if isinstance(raw, bytes):
        try:
            raw = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise UpstreamProtocolViolation("credential is not valid JSON") from error
    if not isinstance(raw, dict):
        raise UpstreamProtocolViolation("credential is not an object")

    token = raw.get("bearer_token") or raw.get("access_token") or raw.get("token")
    if not isinstance(token, str) or not token.strip():
        raise UpstreamProtocolViolation("credential missing bearer token")
    credential = CodeBuddyCredential.from_dict(raw)
    credential.bearer_token = token.strip()
    # AGENTS.md 约束：来源只能由可信创建入口显式写入。
    # - 字段缺失 → 由入口决定（手动导入 = manual）
    # - 字段存在但非法 → 保持 unknown，绝不能默认成 manual
    if "auth_source" not in raw:
        credential.auth_source = auth_source
    return credential


