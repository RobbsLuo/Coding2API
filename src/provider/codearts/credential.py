"""CodeArts 凭证类型与解析。

CodeArts 的凭证不是 bearer token 而是**华为云 STS 临时 AK/SK**（登录换来的
`credentials{access_key_id, secret_access_key, security_token, expiration}`），
外加一把与 `refresh_token` 绑定的 DPoP 私钥：

* `refresh_token` / `client_id` / `dpop_private_jwk` 三者绑定，缺一不可刷新
  （client_id 不符 → `STS5.1806 invalid client id`；私钥换新 →
  `invalid refresh token: 'InvalidDPoPHeader'`）；
* `refresh_token` 一次性：刷新后必须回写新值，用旧值再刷会被拒
  （`the refresh token has been used`）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ...provider.token_expiry import normalize_epoch
from .events import CLIENT_ID, UpstreamProtocolViolation


@dataclass(slots=True)
class CodeArtsCredential:
    """归一化后的 CodeArts 凭证（扁平形落盘，AK/SK + DPoP 私钥）。"""

    uid: str = ""
    user_name: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    security_token: str = ""
    # 上游给的到期时间：可能是 epoch（数字）或 ISO8601（字符串），统一归一为秒。
    expiration: int = 0
    refresh_token: str = ""
    client_id: str = CLIENT_ID
    # OAuth 登录后与 refresh_token 绑定的一次性凭据；旧凭证可能为空。
    code_verifier: str = ""
    # P-256 私钥 JWK（含 d）；刷新必须原样复用，不能重新生成。
    dpop_private_jwk: dict[str, str] = field(default_factory=dict)
    domain: str = ""
    domain_id: str = ""
    nickname: str = ""

    def token_expires_at(self) -> int:
        """STS 临时凭证到期 epoch（秒）；未知为 0。

        `expiration` 在解析时已归一到秒（ISO8601 / 毫秒 / 数字串都在
        `_expiration_from_raw` 里处理），此处不再做第二遍解析——AK/SK 不是
        JWT，没有 `exp` 可回落，凭「未知」去猜 TTL 只会制造凭空捏造的预警。
        """
        return self.expiration

    # refresh_skew_hours 的**上限**（秒）。STS 临时凭证实测寿命 2h（模块文档），
    # 而全局默认 REFRESH_SKEW_HOURS=24h ≫ 寿命：skew 的语义是「离到期还剩
    # skew 秒就提前刷」，所以 24h skew 让 needs_refresh 对本渠道**恒为真**，
    # RefreshTask 每一轮（约 60min）都会去烧一张一次性 refresh_token——票是
    # 单次的，多烧一张就多一次「被别人/被自己消费掉」的风险面。
    # 封顶 30min：把「一进窗口就每轮都刷」降成「离到期 30min 内才刷」。
    # 不能更小的硬约束是轮询周期（默认 60min）：窗口窄于周期就会整轮漏过，
    # 凭证静默过期（实测 STS 只有 2h 余量可浪费）。
    refresh_skew_cap_seconds: ClassVar[int] = 1800

    def needs_refresh(self, skew_seconds: int, now: int | None = None) -> bool:
        """临时凭证本身到期前刷新。

        不要求已有 refresh_token：ticket 轮询通道换来的凭证**没有**
        refresh_token（旧登录通道），它同样会到期；此时刷新必然失败并被
        RefreshTask 记为 failed，但那比「凭证静默过期 → 聊天 401 硬禁用」好。

        `skew_seconds`（全局 REFRESH_SKEW_HOURS）按本渠道封顶，见
        `refresh_skew_cap_seconds`。

        到期未知（0）→ False（不猜 TTL，见 `token_expires_at`）。
        """
        if self.expiration <= 0:
            return False
        current = int(now if now is not None else time.time())
        lead = min(skew_seconds, self.refresh_skew_cap_seconds)
        return current + lead >= self.expiration

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid, "user_name": self.user_name,
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
            "security_token": self.security_token,
            "expiration": self.expiration,
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "code_verifier": self.code_verifier,
            "dpop_private_jwk": dict(self.dpop_private_jwk),
            "domain": self.domain, "domain_id": self.domain_id,
            "nickname": self.nickname,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CodeArtsCredential:
        # token 响应用 `user_id`（见 merge_refreshed），落库形态用 `uid`：都认，
        # 否则登录时 uid 被丢掉（昵称/节流桶只能回落 AK）。
        return cls(
            uid=str(raw.get("uid") or raw.get("user_id") or ""),
            user_name=str(raw.get("user_name") or raw.get("userName") or ""),
            access_key_id=str(raw.get("access_key_id") or raw.get("accessKeyId") or ""),
            secret_access_key=str(
                raw.get("secret_access_key") or raw.get("secretAccessKey") or ""),
            security_token=str(raw.get("security_token") or raw.get("securityToken") or ""),
            expiration=_expiration_from_raw(raw.get("expiration")),
            refresh_token=str(raw.get("refresh_token") or raw.get("refreshToken") or ""),
            client_id=str(raw.get("client_id") or raw.get("clientId") or CLIENT_ID),
            code_verifier=str(raw.get("code_verifier") or raw.get("codeVerifier") or ""),
            dpop_private_jwk=_jwk_from_raw(raw.get("dpop_private_jwk")),
            domain=str(raw.get("domain") or ""),
            domain_id=str(raw.get("domain_id") or raw.get("domainId") or ""),
            nickname=str(raw.get("nickname") or ""),
        )


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
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
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


def parse_credentials(raw: dict[str, Any]) -> CodeArtsCredential:
    """`/v1/oauth2/tokens` 响应 → 凭证（含 ticket 通道的 legacy 字段兜底）。

    上游两种 envelope：新版 `credentials`，旧版 `credential{access,secret,
    securitytoken,expires_at}`。两者都见过，都要认。
    """
    if not isinstance(raw, dict):
        raise UpstreamProtocolViolation("token response is not an object")

    nested = raw.get("credentials")
    nested = nested if isinstance(nested, dict) else {}
    legacy = raw.get("credential")
    legacy = legacy if isinstance(legacy, dict) else {}

    access_key_id = nested.get("access_key_id") or legacy.get("access") or ""
    secret_access_key = nested.get("secret_access_key") or legacy.get("secret") or ""
    security_token = (nested.get("security_token")
                      or legacy.get("securitytoken") or "")

    credentials = CodeArtsCredential.from_dict({
        **raw,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "security_token": security_token,
        "expiration": nested.get("expiration") or legacy.get("expires_at"),
    })
    if not credentials.access_key_id or not credentials.secret_access_key:
        raise UpstreamProtocolViolation("token response missing credentials")
    return credentials


def merge_refreshed(
    credential: CodeArtsCredential, raw: dict[str, Any],
) -> CodeArtsCredential:
    """刷新响应 → 新凭证：**必须回写轮转后的 refresh_token 与 user 信息**。

    refresh_token 一次性，旧值在刷新成功那一刻即作废；不回写等于把一张废票
    当成有效刷新依据，下一轮预刷新必然 `has been used`。
    """
    refreshed = parse_credentials(raw)
    return CodeArtsCredential(
        uid=str(raw.get("user_id") or refreshed.uid or credential.uid),
        user_name=str(raw.get("user_name") or refreshed.user_name
                      or credential.user_name),
        access_key_id=refreshed.access_key_id,
        secret_access_key=refreshed.secret_access_key,
        security_token=refreshed.security_token,
        expiration=refreshed.expiration or credential.expiration,
        refresh_token=refreshed.refresh_token or credential.refresh_token,
        # client_id 与 DPoP 私钥是 refresh_token 的绑定项：上游没回传时
        # 必须沿用旧值，绝不能换成默认 client_id / 新密钥。
        client_id=credential.client_id or refreshed.client_id,
        code_verifier=refreshed.code_verifier or credential.code_verifier,
        dpop_private_jwk=refreshed.dpop_private_jwk or dict(credential.dpop_private_jwk),
        domain=refreshed.domain or credential.domain,
        domain_id=refreshed.domain_id or credential.domain_id,
        nickname=credential.nickname,
    )
