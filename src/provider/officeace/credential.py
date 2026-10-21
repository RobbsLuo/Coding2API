"""officeAce 凭证类型与解析。

两套凭据必须分开存在同一个凭证对象里，因为**寿命与用途完全不同**：

| 字段组 | 寿命 | 用途 |
|---|---|---|
| `model_app_key` / `model_app_secret` / `model_base_url` | **不过期** | 聊天（Basic 认证） |
| `access_key_id` / `secret_access_key` / `security_token` | **约 2 小时** | 积分查询、签到 |
| `project_id` | 同上 | V11 签名的 `X-Project-ID` 头 |

丢了两套的区别会得到一个很迷惑的现象：聊天一直正常（Basic 不过期），但
积分列空白、签到一直失败（临时 AK/SK 过期）——反过来也成立。

`refresh_token` / `client_id` / `dpop_private_jwk` 三者绑定且一次性轮换，
与 CodeArts 同型（同一套华为云 STS），但 `client_id` 不同
（`pdp5_for_agentarts` vs `codearts-agent`），**两种票互不通用**（实测
`STS5.1806 invalid client id`）。故这里**不复用** CodeArts 的凭证类，
只复用它的 dpop / signer 原语。
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ...provider.token_expiry import normalize_epoch
from ..base import UpstreamProtocolViolation
from .events import CLIENT_ID


@dataclass(slots=True)
class OfficeAceCredential:
    """归一化后的 officeAce 凭证（扁平形落盘）。"""

    # --- 身份（选号节流与签到 scope）---
    account_id: str = ""
    principal_id: str = ""
    user_name: str = ""
    nickname: str = ""

    # --- 聊天凭据：Basic app_key:app_secret（不过期）---
    model_app_key: str = ""
    model_app_secret: str = ""
    model_base_url: str = ""

    # --- 控制面凭据：华为云临时 AK/SK（约 2h）---
    access_key_id: str = ""
    secret_access_key: str = ""
    security_token: str = ""
    project_id: str = ""
    expiration: int = 0

    # --- 续期（一次性轮换，与 client_id + DPoP 私钥绑定）---
    refresh_token: str = ""
    client_id: str = CLIENT_ID
    dpop_private_jwk: dict[str, str] = field(default_factory=dict)

    #: 临时凭证寿命实测约 2h；封顶 45min，理由与 CodeArts 完全相同：
    #: 全局 skew 默认 24h ≫ 寿命，不封顶会让 `needs_refresh` 恒为真，
    #: 每轮预刷新都去烧一张一次性 refresh_token。封顶值必须**严格大于**
    #: 预刷新轮询周期（默认 30min），否则窗口整轮落在两次轮询之间。
    refresh_skew_cap_seconds: ClassVar[int] = 2700

    def token_expires_at(self) -> int:
        """控制面临时凭证到期 epoch（秒）；未知为 0。

        AK/SK 不是 JWT，没有 `exp` 可回落；凭「未知」去猜 TTL 只会制造
        凭空捏造的预警。
        """
        return self.expiration

    def needs_refresh(self, skew_seconds: int, now: int | None = None) -> bool:
        """临时凭证到期前刷新。

        只有**同时**持有 `refresh_token` 与 DPoP 私钥时才值得刷——两者缺一
        刷了也必失败（上游回 `InvalidDPoPHeader`），只会烧掉一次性票。
        """
        if self.expiration <= 0:
            return False
        if not self.refresh_token or not self.dpop_private_jwk:
            return False
        current = int(now if now is not None else time.time())
        lead = min(skew_seconds, self.refresh_skew_cap_seconds)
        return current + lead >= self.expiration

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "principal_id": self.principal_id,
            "user_name": self.user_name,
            "nickname": self.nickname,
            "model_app_key": self.model_app_key,
            "model_app_secret": self.model_app_secret,
            "model_base_url": self.model_base_url,
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
            "security_token": self.security_token,
            "project_id": self.project_id,
            "expiration": self.expiration,
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "dpop_private_jwk": dict(self.dpop_private_jwk),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OfficeAceCredential:
        """宽容解析：camelCase / snake_case 都认（两种写法上游都用过）。"""
        if not isinstance(raw, dict):
            raise UpstreamProtocolViolation("credential is not an object")
        return cls(
            account_id=_text(raw, "account_id", "accountId"),
            principal_id=_text(raw, "principal_id", "principalId", "user_id", "userId"),
            user_name=_text(raw, "user_name", "userName"),
            nickname=_text(raw, "nickname"),
            model_app_key=_text(raw, "model_app_key", "modelAppKey"),
            model_app_secret=_text(raw, "model_app_secret", "modelAppSecret"),
            model_base_url=_text(raw, "model_base_url", "modelBaseUrl").rstrip("/"),
            access_key_id=_text(raw, "access_key_id", "accessKeyId"),
            secret_access_key=_text(raw, "secret_access_key", "secretAccessKey"),
            security_token=_text(raw, "security_token", "securityToken"),
            project_id=_text(raw, "project_id", "projectId"),
            expiration=_expiration_from_raw(raw.get("expiration")),
            refresh_token=_text(raw, "refresh_token", "refreshToken"),
            client_id=_text(raw, "client_id", "clientId") or CLIENT_ID,
            dpop_private_jwk=_jwk_from_raw(raw.get("dpop_private_jwk")),
        )

    @property
    def authorization(self) -> str:
        """聊天用的 Basic 头值（`Basic base64(app_key:app_secret)`）。"""
        if not self.model_app_key:
            return ""
        raw = f"{self.model_app_key}:{self.model_app_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    @property
    def has_control_plane(self) -> bool:
        """是否具备查询积分/签到的控制面凭据（临时 AK/SK）。"""
        return bool(self.access_key_id and self.secret_access_key)

    @property
    def has_chat(self) -> bool:
        """是否具备聊天凭据（Basic app key）。"""
        return bool(self.model_app_key and self.model_app_secret)


def _text(raw: dict[str, Any], *keys: str) -> str:
    """按优先级取第一个非空字符串字段。"""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _expiration_from_raw(raw: Any) -> int:
    """到期时间归一：数字（epoch，可为毫秒）/ ISO8601 / 数字字符串 → 秒。"""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        return normalize_epoch(int(raw))
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            return normalize_epoch(int(text))
        except ValueError:
            pass
        from .events import epoch_from_iso

        return epoch_from_iso(text)
    return 0


def _jwk_from_raw(raw: Any) -> dict[str, str]:
    """DPoP 私钥 JWK：只收字符串字段，畸形输入返回空（由刷新时报错兜底）。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, str)}


def parse_tokens(raw: dict[str, Any]) -> OfficeAceCredential:
    """STS 令牌响应 → 临时凭据 + 轮换后的 refresh_token。

    华为 STS 的响应形状（CodeArts 同域，实测一致）：

    ```
    {"credentials": {"access_key_id","secret_access_key","security_token",
                     "expiration","project_id"},
     "refresh_token": "<新票>", "id_token": "<JWT>", "user_id": "..."}
    ```

    `refresh_token` 一次性：调用方**必须**把新值回写，否则下一轮必然
    `the refresh token has been used`。
    """
    if not isinstance(raw, dict):
        raise UpstreamProtocolViolation("token response is not an object")
    nested = raw.get("credentials")
    nested = nested if isinstance(nested, dict) else {}
    access_key_id = nested.get("access_key_id") or ""
    secret_access_key = nested.get("secret_access_key") or ""
    if not access_key_id or not secret_access_key:
        raise UpstreamProtocolViolation("token response missing credentials")
    credential = OfficeAceCredential.from_dict({
        **raw,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "security_token": nested.get("security_token") or "",
        "project_id": nested.get("project_id") or raw.get("project_id") or "",
        "expiration": nested.get("expiration") or raw.get("expiration"),
        "user_id": raw.get("user_id") or "",
    })
    if not credential.user_name:
        credential.user_name = _id_token_name(str(raw.get("id_token") or ""))
    return credential


def _id_token_name(id_token: str) -> str:
    """`id_token` 的 payload 里取用户名（`preferred_username` / `name`）。

    只解码不验签：签名由上游校验，这里仅用于凭证昵称展示。
    """
    parts = id_token.split(".")
    if len(parts) < 2:
        return ""
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("preferred_username", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def merge_refreshed(credential: OfficeAceCredential,
                    raw: dict[str, Any]) -> OfficeAceCredential:
    """刷新响应 → 新凭证：**必须回写轮转后的 refresh_token**。

    refresh_token 一次性，旧值在刷新成功那一刻即作废；不回写等于把一张废票
    当成有效刷新依据，下一轮预刷新必然 `has been used`。

    `client_id` 与 DPoP 私钥是 refresh_token 的绑定项：响应里没有时**必须**
    沿用旧值，绝不能换成默认 client_id 或新密钥。
    """
    refreshed = parse_tokens(raw)
    return OfficeAceCredential(
        account_id=refreshed.account_id or credential.account_id,
        principal_id=refreshed.principal_id or credential.principal_id,
        user_name=refreshed.user_name or credential.user_name,
        nickname=credential.nickname,
        # 聊天凭据不随刷新变化（Basic app key 不过期），沿用旧值
        model_app_key=credential.model_app_key,
        model_app_secret=credential.model_app_secret,
        model_base_url=credential.model_base_url,
        access_key_id=refreshed.access_key_id,
        secret_access_key=refreshed.secret_access_key,
        security_token=refreshed.security_token,
        project_id=refreshed.project_id or credential.project_id,
        expiration=refreshed.expiration or credential.expiration,
        refresh_token=refreshed.refresh_token or credential.refresh_token,
        client_id=credential.client_id or refreshed.client_id,
        dpop_private_jwk=(dict(credential.dpop_private_jwk)
                          or refreshed.dpop_private_jwk),
    )
