"""华为云 officeAce（果办 / AgentArts 办公智能体）渠道。

与其余七渠道的差异（决定本包为什么这样切）：

1. **上游就是标准 OpenAI 协议**——模型网关 `{gateway}/v2/chat/completions`
   的请求/响应形状与 OpenAI 完全一致，因此对话层不需要私有信封解析（直接
   复用 `provider/openai_chunk` 与 `engine/sse`），这与 zen/kilo 同型，比
   CodeBuddy/Qoder 简单得多。

2. **两套凭据、寿命完全不同**（这是本包最容易踩错的地方）：

   | 凭据 | 寿命 | 用途 |
   |---|---|---|
   | `Basic model_app_key:model_app_secret` | **不过期** | 聊天转发 |
   | 临时 AK/SK + security_token + project_id | **约 2 小时** | 积分查询、签到 |

   聊天只认前者，积分/签到只认后者。把两者混用会得到 `APIG.0303 app not
   found`（实测）——恰恰是本项目最初试图复用 CodeArts 凭证时踩的坑。

3. **两套签名算法**：控制面 `/v1/claw/*` 用 `SDK-HMAC-SHA256`（与 CodeArts
   同型），模型网关的 `/v1/subscription*` 用 **`V11-HMAC-SHA256`**（多一层
   region 作用域 + 签名密钥派生）。用错会回 `APIG.0624` 或 401。

4. **积分制而非 token 制**：额度单位是 `officeace_points`，积分就是主账
   （没有并行的 token 池），故 `Quota.remaining`/`total` 直接是积分。签到走
   `POST /v1/subscription/bonus/claim`（**写操作**，body 必须是 {}）。

5. **OAuth 是 PKCE + DPoP**，与 CodeArts 同属华为云 STS 体系，故 DPoP 签名
   直接复用 `provider/codearts/dpop.py`（低 S 归一化那条实现是华为 STS 强制
   的，不能自己另写一份）。但 `client_id` 不同（`pdp5_for_agentarts` vs
   `codearts-agent`），**两种 refresh_token 互不通用**（实测
   `STS5.1806 invalid client id`），故 OAuth 与凭证类型各自独立实现。

装配：`OfficeAceProvider(client=OfficeAceClient())`，聊天走自己的 pacer 桶。
"""

from __future__ import annotations

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
from ..codearts import dpop
from . import events as officeace_events
from .client import OfficeAceClient
from .credential import OfficeAceCredential, validate_gateway_url
from .oauth import AuthStateStore


def credential_key(credential: OfficeAceCredential) -> str:
    """聊天节流桶键：account_id 优先，退到 app_key（同账号多凭证共享节流）。"""
    from ...tasks.pacer import stable_key

    return stable_key("officeace",
                      credential.account_id or credential.model_app_key)


@dataclass(slots=True)
class OfficeAceProvider:
    """Provider 协议实现（细接口）。只依赖注入的 client，不 import config。"""

    client: OfficeAceClient = field(default_factory=OfficeAceClient)
    pacer: Any | None = None

    id: str = "officeace"

    # ------------------------------------------------------ 凭证生命周期

    def import_credential(self, raw: dict) -> dict:
        """手工导入凭证 → 归一化落库。

        最小可用集合是 `model_app_key` + `model_app_secret`（聊天必需，且不过
        期）、外加 `model_base_url`（网关地址每个账号不同，必填）。临时
        AK/SK 缺失是**允许**的：那种凭证能聊天但查不到积分、签不了到——与
        「只从桌面端抠出 models.json」的现状一致，不该因此拒绝导入。
        """
        if not isinstance(raw, dict):
            raise UpstreamProtocolViolation("credential is not an object")
        credential = OfficeAceCredential.from_dict(raw)
        if not credential.model_app_key or not credential.model_app_secret:
            raise UpstreamProtocolViolation(
                "credential missing model_app_key / model_app_secret")
        if not credential.model_base_url:
            raise UpstreamProtocolViolation(
                "credential missing model_base_url (officeAce 网关地址)")
        # 网关地址是**长期**凭据的收件方（Basic app_key:app_secret 不过期），
        # 手工导入时必须是 https 且不指向内网——见 `validate_gateway_url`。
        validate_gateway_url(credential.model_base_url)
        # DPoP 私钥有效性在导入时校验：坏 JWK 只有到刷新才暴露，而那时用户
        # 已经以为登录成功了（`InvalidDPoPHeader` 只在 STS 端出现）。
        if credential.dpop_private_jwk:
            try:
                dpop.private_key_from_jwk(credential.dpop_private_jwk)
            except ValueError as error:
                raise UpstreamProtocolViolation(
                    f"invalid dpop_private_jwk: {error}") from error
        return credential.to_dict()

    def credential_from(self, credential_data: dict) -> OfficeAceCredential:
        """供 RefreshTask 判断是否进入刷新窗口（预刷新）。"""
        return OfficeAceCredential.from_dict(credential_data)

    def classify(self, status: int, body: bytes) -> ErrKind:
        return officeace_events.classify_status(status, body)

    # ------------------------------------------------------------ 登录
    #
    # 登录**不**经 provider 方法：`api/admin_auth` 对 poll 轨道的渠道直接调用
    # `services.upstream_auth[provider]` 上的 OAuth 实例（`start` / `poll`），
    # 与 CodeArts / Qoder 同一分工。故这里不实现 `start_auth` / `poll_auth` /
    # `complete_callback`——写了也只是永远不被调用的死代码。登录实例由 main
    # 装配进 `app.state.upstream_auth`，其连接池在 lifespan 关闭阶段释放。

    # ------------------------------------------------------------ 执行

    async def list_models(self, credential_data: dict) -> list[Model]:
        return await self.client.fetch_models(
            OfficeAceCredential.from_dict(credential_data))

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        """引擎调用入口：dict 凭证 → 上游流 → 中立事件。"""
        credential = OfficeAceCredential.from_dict(credential_data)
        key = credential_key(credential)
        async with paced(self.pacer, key):
            async for event in self.client.stream_chat(credential, payload, model):
                yield event

    # -------------------------------------------------------- 额度探测

    async def probe_quota(self, credential_data: dict) -> Quota:
        """读积分额度（只读，不消耗积分，不刷新凭据）。

        刷新（一次性 refresh_token）唯一归 RefreshTask，理由见
        `provider/codearts/__init__.py` 的同款约束。
        """
        return await self.client.probe_quota(
            OfficeAceCredential.from_dict(credential_data))

    async def refresh(self, credential_data: dict) -> dict:
        """预刷新：回写新 refresh_token 与临时凭证（一次性 token 必须轮转）。"""
        refreshed = await self.client.refresh_token(
            OfficeAceCredential.from_dict(credential_data))
        return refreshed.to_dict()

    # ------------------------------------------------------------ 签到

    async def checkin(self, credential_data: dict) -> CheckinResult:
        return await self.client.claim_bonus(
            OfficeAceCredential.from_dict(credential_data))

    async def checkin_status(self, credential_data: dict) -> dict:
        """只读签到状态（管理台展示）；活动不存在/改版时给非活动态。"""
        return await self.client.fetch_checkin_status(
            OfficeAceCredential.from_dict(credential_data))

    def checkin_scope(self, credential_data: dict) -> str:
        """签到隔离键：account_id；身份未知时返回空串（调用方回落凭证 ID）。

        返回空串而非 `officeace|`——后者会让同渠道所有身份未知的凭证算出同
        一个 scope，`CheckinTask` 的 seen 集合只跑第一个账号。
        """
        account_id = OfficeAceCredential.from_dict(credential_data).account_id
        return f"officeace|{account_id}" if account_id else ""

    async def aclose(self) -> None:
        """释放内部 HTTP 连接池。"""
        await self.client.aclose()


# `AuthStateStore` 在门面层再导出一次：main 装配自定义 store（测试注入）时
# 不必深引 `oauth` 子模块。
__all__ = ["AuthStateStore", "OfficeAceProvider", "credential_key"]
