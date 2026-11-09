"""WPS 灵犀（Lingxi / 灵犀 Claw）渠道。

与其余渠道的差异（决定了本包为什么这样切）：

1. **三条请求才是一次对话**：建 cowork 会话 → 领 credits run → 打
   `agent-runtime/chat/completions`。上游**按 run 计费**（一个 run 内多次调用
   只扣一次智点），故 run 按「账号 × 模型」缓存 1 小时、并发单飞（见
   `client.ensure_run`）。
2. **鉴权只有 cookie**：`wps_sid`，没有 refresh 接口。过期只能重新粘贴，
   真正的判据是上游 401（不按时长猜，见 `credential.py`）。
3. **私有 SSE**：`choices[].text` 而非 `delta.content`，没有 `[DONE]`，
   `usage.total_tokens` 是上下文大小（自算）、`cached_tokens` /
   `reasoning_tokens` 是扁平字段（见 `events.py`）。
4. **工具 schema 必须先压平**：`anyOf` / `$ref` / 数组型 `type` 会让上游
   整轮回「AI服务异常」，见 `client.sanitize_schema`。

登录轨道不实现：凭据是 WPS 账号 cookie，只能由用户从浏览器（网页版
`lingxi.kdocs.cn` 同样可用）复制后经「JSON 导入」录入 —— 与 Zen/Kilo 的
「方法缺失自然跳过」同一分工。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ...provider.base import (
    CheckinResult,
    ErrKind,
    Event,
    Model,
    Quota,
    UpstreamProtocolViolation,
)
from ...tasks.pacer import paced
from ..login_identity import login_identity_for
from . import events as lingxi_events
from .client import LingxiClient, RunExpired, UpstreamHTTPError
from .credential import LingxiCredential


@dataclass(slots=True)
class LingxiProvider:
    """Provider 协议实现（细接口）。只依赖注入的 client，不 import config。"""

    client: LingxiClient = field(default_factory=LingxiClient)
    pacer: Any | None = None

    id: str = "lingxi"

    # ------------------------------------------------------ 凭证生命周期

    def import_credential(self, raw: dict) -> dict:
        """手工导入：整段 Cookie / 裸 `wps_sid`；缺 sid 直接拒绝。"""
        return LingxiCredential.from_import(raw).to_dict()

    def classify(self, status: int, body: bytes) -> ErrKind:
        return lingxi_events.classify_status(status, body)

    def login_identity(self, credential_data: dict) -> str:
        """重新登录合并键：上游 `uid`（缺省用 sid 哈希，见 credential.identity）。"""
        return login_identity_for(self.id, credential_data)

    # ------------------------------------------------------------ 额度

    async def probe_quota(self, credential_data: dict) -> Quota:
        credential = LingxiCredential.from_dict(credential_data)
        if not credential.wps_sid:
            return Quota(probe_failed=True, probed_at=int(time.time()))
        return await self.client.probe_quota(credential)

    # ------------------------------------------------------------ 签到

    async def checkin(self, credential_data: dict) -> CheckinResult:
        credential = LingxiCredential.from_dict(credential_data)
        if not credential.wps_sid:
            return CheckinResult(ok=False, message="凭证缺少 wps_sid")
        try:
            return await self.client.checkin(credential)
        except UpstreamHTTPError as error:
            if error.status == 401:
                return CheckinResult(ok=False, message="登录态已失效，请重新粘贴 Cookie")
            return CheckinResult(ok=False, message=f"签到失败（HTTP {error.status}）")
        except (UpstreamProtocolViolation, OSError):
            return CheckinResult(ok=False, message="签到响应格式异常")

    async def checkin_status(self, credential_data: dict) -> dict:
        credential = LingxiCredential.from_dict(credential_data)
        if not credential.wps_sid:
            return {"active": False, "today_checked_in": False}
        return await self.client.checkin_status(credential)

    def checkin_scope(self, credential_data: dict) -> str:
        """签到隔离键：uid / sid 哈希；身份未知返回空串（调用方回落凭证 ID）。"""
        identity = LingxiCredential.from_dict(credential_data).identity()
        return f"lingxi|{identity}" if identity else ""

    # ------------------------------------------------------------ 模型

    async def list_models(self, credential_data: dict) -> list[Model]:
        credential = LingxiCredential.from_dict(credential_data)
        if not credential.wps_sid:
            raise UpstreamProtocolViolation("lingxi credential missing wps_sid")
        return await self.client.fetch_models(credential)

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        """聊天：run 失效时丢弃缓存重建一次（上游 `SQLNoRows`）。"""
        credential = LingxiCredential.from_dict(credential_data)
        if not credential.wps_sid:
            raise UpstreamProtocolViolation("lingxi credential missing wps_sid")
        async with paced(self.pacer, credential.identity()):
            # 两轮要么 return 要么 raise，循环不会自然耗尽（按构造不可达分支）
            for attempt in (0, 1):  # pragma: no branch
                try:
                    async for event in self.client.stream_chat(
                            credential, payload, model):
                        yield event
                    return
                except RunExpired:
                    self.client.forget_run(credential, model)
                    if attempt:
                        raise UpstreamProtocolViolation(
                            "灵犀会话反复失效（重建后仍 SQLNoRows）") from None

    async def aclose(self) -> None:
        await self.client.aclose()


__all__ = ["LingxiProvider"]