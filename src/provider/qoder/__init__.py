"""Qoder（阿里，COSY 私有协议）渠道。

与 zen/kilo 的本质区别：Qoder 是**真实账号渠道**——凭证只能由设备码 PKCE
登录换取（`auth.py`），加密入库，没有虚拟凭证。

包内划分：

* `events.py`：区域常量、信封 SSE 解包、错误分类、模型/额度/签到解析（纯函数）；
* `cosy.py`：自定义 Base64、设备指纹派生、RSA/AES 会话材料与签名头（纯函数 +
  会话缓存）；
* `credential.py`：凭证 dataclass 与 deviceToken 响应归一；
* `auth.py`：poll 轨道登录（`QoderOAuth`，state 归属/消费/TTL）；
* `client.py`：上游 HTTP（推理流 / 模型 / 额度 / 签到 / 刷新）；
* 本文件：Provider 协议实现。

装配（主线程）：`QoderProvider(client=QoderClient(host=..., gateway=...), pacer=...)`，
登录用 `QoderOAuth(realm)` 挂到 `app.state.upstream_auth["qoder"]`。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ...provider.base import (
    CheckinResult,
    CheckinStatus,
    ErrKind,
    Event,
    Model,
    Quota,
)
from . import events as qoder_events
from .client import QoderClient, realm_for
from .credential import QoderCredential, parse_credential

logger = logging.getLogger(__name__)


def credential_key(credential: QoderCredential) -> str:
    """聊天节流桶键：uid 优先，退到 access token（同账号多凭证共桶）。"""
    from ...tasks.pacer import stable_key

    return stable_key("qoder", credential.uid or credential.access_token)


def status_to_dict(status: CheckinStatus) -> dict[str, Any]:
    """中立签到状态 → JSON 形状（管理台展示；与 CodeBuddy 同口径）。

    `CheckinStatus` 是中立层 dataclass（无 to_dict），而管理台接口按 dict 返回；
    这里显式列字段而不是 `asdict`，避免中立层新增字段时静默泄漏到 API。
    """
    return {
        "active": status.active,
        "today_checked_in": status.today_checked_in,
        "streak_days": status.streak_days,
        "today_credit": status.today_credit,
        "total_credits": status.total_credits,
        "activity_name": status.activity_name,
        "is_streak_day": status.is_streak_day,
    }


@dataclass(slots=True)
class QoderProvider:
    """Provider 协议实现（细接口，Q16=A）。

    `pacer`：聊天请求节流器（Qoder 上游按账号频控）；None 表示不限速。
    """

    client: QoderClient = field(default_factory=QoderClient)
    pacer: Any | None = None

    id: str = "qoder"

    # -------------------------------------------------------- 凭证生命周期

    def import_credential(self, raw: dict) -> dict:
        """手工导入 access token（无 refresh_token 时不可刷新，但仍可用）。"""
        return parse_credential(raw, auth_source="manual").to_dict()

    def credential_from(self, credential_data: dict) -> QoderCredential:
        """供 RefreshTask 判断是否进入刷新窗口（Q12 预刷新）。"""
        return QoderCredential.from_dict(credential_data)

    async def refresh(self, credential_data: dict) -> dict:
        """OAuth 凭证预刷新；手工 bearer 凭证没有 refresh_token，原样返回。"""
        credential = QoderCredential.from_dict(credential_data)
        if not credential.is_oauth or not credential.refresh_token:
            return credential_data
        return await self.client.refresh_token(credential)

    # ---------------------------------------------------------------- 执行

    def classify(self, status: int, body: bytes) -> ErrKind:
        return qoder_events.classify_status(status, body)

    async def probe_quota(self, credential_data: dict) -> Quota:
        return await self.client.probe_quota(QoderCredential.from_dict(credential_data))

    async def list_models(self, credential_data: dict) -> list[Model]:
        return await self.client.fetch_models(
            QoderCredential.from_dict(credential_data))

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        credential = QoderCredential.from_dict(credential_data)
        key = credential_key(credential)
        if self.pacer is not None:
            await self.pacer.wait_turn(key)
        try:
            async for event in self.client.stream_chat(credential, payload, model):
                yield event
        finally:
            # 并发模式必须归还名额，否则该凭证被当成永远在途而失去节流
            if self.pacer is not None:
                self.pacer.release(key)

    # ---------------------------------------------------------------- 签到

    async def checkin(self, credential_data: dict) -> CheckinResult:
        return await self.client.checkin(QoderCredential.from_dict(credential_data))

    async def checkin_status(self, credential_data: dict) -> dict:
        """只读签到状态（管理台展示连续天数）；本区域无接口时返回非活动状态。"""
        status, _ = await self.client.fetch_checkin_status(
            QoderCredential.from_dict(credential_data))
        return status_to_dict(status or CheckinStatus(activity_name="Qoder 每日签到"))

    def checkin_scope(self, credential_data: dict) -> str:
        """签到隔离键：区域 + uid；身份未知时返回空串（调用方回落凭证 ID）。

        返回空串而不是「区域|」是安全取向：后者会让同区域所有未知身份的
        凭证算出同一个 scope，CheckinTask 的 seen 集合只跑第一个账号。
        """
        credential = QoderCredential.from_dict(credential_data)
        if not credential.uid:
            return ""
        return f"{realm_for(credential)}|{credential.uid}"

    # ---------------------------------------------------------------- 其他

    async def aclose(self) -> None:
        """释放内部 HTTP 连接池。"""
        await self.client.aclose()
