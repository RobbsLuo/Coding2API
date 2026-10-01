"""CodeArts（华为云码道 / snap-access 盘古引擎）渠道。

与 CodeBuddy / TRAE 的差异（决定本包为什么这样切）：

1. **鉴权不是 bearer**，是华为云 `SDK-HMAC-SHA256`（AK/SK + STS security
   token）；凭证是 OAuth PKCE 换来的**临时 AK/SK**，带 `expiration`。
2. **refresh_token 一次性且与 client_id + DPoP 私钥绑定**，刷新必须回写新
   refresh_token；这把「保活」变成了凭证生命周期里的一等公民。轮转由
   `tasks.refresh.RefreshTask`（唯一持有者，**先落库再同步**）承担，额度探测
   等旁路**不得**顺手刷新——否则一个一次性 refresh_token 被两个地方各消费一次，
   后到的必然报 `the refresh token has been used`，DB 里的 token 也被烧掉。
3. **没有每日签到接口**（额度为每日 token 池、0 点清零，逆向记录 §6）。对应能力由
   任务侧 `RefreshTask` 的到期预刷新承担，故本 provider **不实现 checkin**
   （任务侧靠 getattr 探测自然跳过）。
4. **SSE 不是标准分隔**（逐行 `data:`、无空行），且 `text` 是**累计全文**，
   解析层必须做替换语义的差量，见 `events.TextSnapshot`。

装配（主线程，见交付说明）：`CodeArtsProvider(client=CodeArtsClient())`，
聊天走自己的 pacer 桶（与 CB/TRAE/zen/kilo 互不排队）；凭证导入走
`import_credential`（AK/SK 手工导入，或由 OAuth 登录流程直接落库）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ...provider.base import ErrKind, Event, Model, Quota
from . import dpop
from . import events as codearts_events
from .client import CodeArtsClient
from .credential import CodeArtsCredential
from .events import UpstreamProtocolViolation


def credential_key(credential: CodeArtsCredential) -> str:
    """聊天节流桶键：uid 优先，退到 AK（同一账号不同凭证共享节流）。"""
    from ...tasks.pacer import stable_key

    return stable_key("codearts", credential.uid or credential.access_key_id)


@dataclass(slots=True)
class CodeArtsProvider:
    """Provider 协议实现（细接口，Q16=A）。只依赖注入的 client，不 import config。"""

    client: CodeArtsClient = field(default_factory=CodeArtsClient)
    pacer: Any | None = None

    id: str = "codearts"

    def import_credential(self, raw: dict) -> dict:
        """手工导入 AK/SK 凭证 → 归一化落库。

        必填 AK/SK：缺一个都签不出请求，导入即拒绝（比等聊天时 401 更早、
        更明确）。`dpop_private_jwk` 缺失是**允许**的（手工凭证无从刷新），
        此时 `needs_refresh` 仍在到期后返回 True，刷新会以明确错误失败。
        """
        if not isinstance(raw, dict):
            raise UpstreamProtocolViolation("credential is not an object")
        credential = CodeArtsCredential.from_dict(raw)
        if not credential.access_key_id or not credential.secret_access_key:
            raise UpstreamProtocolViolation(
                "credential missing access_key_id / secret_access_key")
        # P-256 私钥有效性在导入时校验：坏 JWK 只有到刷新才会暴露，而那时
        # 用户已经以为登录成功了（`InvalidDPoPHeader` 只在 STS 端出现）。
        if credential.dpop_private_jwk:
            try:
                dpop.private_key_from_jwk(credential.dpop_private_jwk)
            except ValueError as error:
                raise UpstreamProtocolViolation(f"invalid dpop_private_jwk: {error}") from error
        return credential.to_dict()

    def credential_from(self, credential_data: dict) -> CodeArtsCredential:
        """供 RefreshTask 判断是否进入刷新窗口（Q12 预刷新）。"""
        return CodeArtsCredential.from_dict(credential_data)

    def classify(self, status: int, body: bytes) -> ErrKind:
        return codearts_events.classify_status(status, body)

    async def list_models(self, credential_data: dict) -> list[Model]:
        return await self.client.fetch_models(CodeArtsCredential.from_dict(credential_data))

    async def probe_quota(self, credential_data: dict) -> Quota:
        # 只读余额：刷新（一次性 refresh_token）唯一归 RefreshTask，见模块 docstring。
        return await self.client.probe_quota(
            CodeArtsCredential.from_dict(credential_data))

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        """引擎调用入口：dict 凭证 → 上游流 → 中立事件。"""
        credential = CodeArtsCredential.from_dict(credential_data)
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

    async def refresh(self, credential_data: dict) -> dict:
        """预刷新：回写新 refresh_token 与临时凭证（一次性 token 必须轮转）。"""
        refreshed = await self.client.refresh_token(
            CodeArtsCredential.from_dict(credential_data))
        return refreshed.to_dict()

    async def aclose(self) -> None:
        """释放内部 HTTP 连接池。"""
        await self.client.aclose()
