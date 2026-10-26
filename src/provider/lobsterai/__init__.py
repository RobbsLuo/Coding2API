"""LobsterAI（有道龙虾）渠道。

与其余七渠道的差异（决定本包为什么这样切）：

1. **对话只支持流式**：上游 `stream:false` 回 500，故请求体恒置
   `stream=true`，非流式由本服务的聚合层完成（`compat/openai/response`）。
   这与 CodeArts 那种「上游非流式、本地合成 SSE」是**反方向**的：本渠道
   上游是真流式，本服务只做「流 → 非流式」的聚合。

2. **身份三元组是凭证的一部分**：`uuid` / `firstKeyfrom` / `latestKeyfrom`
   由客户端生成、随凭证持久化，续期时原样回放。缺任何一个 → 永远续不了
   期。故 `import_credential` 强制校验它们，而不是等续期失败才暴露。

3. **积分是每日发放、当天有效**：签到三段式（slot → context → check_in），
   每次发 100 积分（实测 `rewardCredits=100`）。额度因此是**逐笔到期**的
   余额而非周期额度，故 `probe_quota` 的 `total` 恒为 None（上游没有
   「总量」这个概念），积分包按各自到期日进 `expiry_ladder`。

4. **额度耗尽可被签到恢复**：上游额度不足（40201 / 中文「积分不足」/ 402）
   归 `ErrKind.CREDIT`——与本项目既有的「冷却到次日 04:00 等签到恢复」
   口径天然对齐，无需新增错误分类。

装配：`LobsteraiProvider(client=LobsteraiClient())`，聊天走自己的 pacer 桶
（与其它渠道互不排队）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ...provider.base import (
    AuthFlow,
    AuthSession,
    CheckinResult,
    ErrKind,
    Event,
    Model,
    Quota,
    UpstreamProtocolViolation,
)
from ...tasks.pacer import paced
from . import events as lobsterai_events
from .client import LobsteraiClient
from .credential import LobsteraiCredential
from .oauth import build_login_url, new_session, parse_callback_url


def credential_key(credential: LobsteraiCredential) -> str:
    """聊天节流桶键：uid 优先，退到 uuid（同账号多凭证共享节流）。"""
    from ...tasks.pacer import stable_key

    return stable_key("lobsterai", credential.uid or credential.uuid)


@dataclass(slots=True)
class LobsteraiProvider:
    """Provider 协议实现（细接口）。只依赖注入的 client，不 import config。"""

    client: LobsteraiClient = field(default_factory=LobsteraiClient)
    pacer: Any | None = None

    id: str = "lobsterai"

    # ------------------------------------------------------ 凭证生命周期

    def import_credential(self, raw: dict) -> dict:
        """手工导入凭证 → 归一化落库。

        必填 `accessToken`；**同时要求身份三元组**（`uuid` / `firstKeyfrom` /
        `latestKeyfrom`）——它们不是可选元数据，而是续期能否成功的前提
        （上游把三者当同一台客户端的指纹）。缺了就导入即拒绝：让用户当场
        看到「少粘了一个字段」，比几小时后拿到一条 401 凭证再回头查要省事。
        """
        if not isinstance(raw, dict):
            raise UpstreamProtocolViolation("credential is not an object")
        credential = LobsteraiCredential.from_dict(raw)
        if not credential.access_token:
            raise UpstreamProtocolViolation("credential missing accessToken")
        missing = [name for name, value in (
            ("uuid", credential.uuid),
            ("firstKeyfrom", credential.first_keyfrom),
            ("latestKeyfrom", credential.latest_keyfrom),
        ) if not value]
        if missing:
            raise UpstreamProtocolViolation(
                "credential missing " + ", ".join(missing)
                + "（LobsterAI 的续期请求要带客户端身份三元组，"
                  "请从 LobsterAI 桌面客户端导出的凭证里完整复制）")
        return credential.to_dict()

    def credential_from(self, credential_data: dict) -> LobsteraiCredential:
        """供 RefreshTask 判断是否进入刷新窗口（预刷新）。"""
        return LobsteraiCredential.from_dict(credential_data)

    def classify(self, status: int, body: bytes) -> ErrKind:
        return lobsterai_events.classify_status(status, body)

    # ------------------------------------------------------------ 执行

    async def list_models(self, credential_data: dict) -> list[Model]:
        return await self.client.fetch_models(
            LobsteraiCredential.from_dict(credential_data))

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        """引擎调用入口：dict 凭证 → 上游流 → 中立事件。"""
        credential = LobsteraiCredential.from_dict(credential_data)
        key = credential_key(credential)
        async with paced(self.pacer, key):
            async for event in self.client.stream_chat(credential, payload, model):
                yield event

    async def refresh(self, credential_data: dict) -> dict:
        """预刷新：换新 token（**必须回写轮转后的 refresh_token**）。

        刷新唯一归 RefreshTask（`tasks/refresh.py`），额度探测等旁路**不得**
        顺手刷新——否则同一张票被两个地方各消费一次。
        """
        refreshed = await self.client.refresh_token(
            LobsteraiCredential.from_dict(credential_data))
        return refreshed.to_dict()

    # -------------------------------------------------------- 额度探测

    async def probe_quota(self, credential_data: dict) -> Quota:
        """读积分余额（只读，不消耗积分，不刷新凭据）。"""
        return await self.client.fetch_quota(
            LobsteraiCredential.from_dict(credential_data))

    # ------------------------------------------------------------ 签到

    async def checkin(self, credential_data: dict) -> CheckinResult:
        return await self.client.claim_checkin(
            LobsteraiCredential.from_dict(credential_data))

    async def checkin_status(self, credential_data: dict) -> dict:
        """只读签到状态（管理台展示）；活动不存在/改版时给非活动态。"""
        return await self.client.fetch_checkin_status(
            LobsteraiCredential.from_dict(credential_data))

    def checkin_scope(self, credential_data: dict) -> str:
        """签到隔离键：uid；身份未知时返回空串（调用方回落凭证 ID）。

        返回空串而非 `lobsterai|`——后者会让同渠道所有身份未知的凭证算出同
        一个 scope，`CheckinTask` 的 seen 集合只跑第一个账号。
        """
        uid = LobsteraiCredential.from_dict(credential_data).uid
        return f"lobsterai|{uid}" if uid else ""

    # ------------------------------------------------------------ 登录

    #
    # 登录**不**经 provider 的 `poll_auth`：`api/admin_auth` 对 paste 轨道的
    # 渠道直接调 `start_auth` / `complete_callback`（与 CodeArts / TRAE 同一
    # 分工），故这里不实现 `poll_auth`——写了也只是永远不被调用的死代码。

    def start_auth(self, callback_url: str) -> AuthSession:
        """生成登录 URL（paste 轨道：门户回环回调 → 用户把链接粘回来）。

        上游门户只接受**回环地址**作为 `redirect_uri`，而本服务可能部署在
        远端 / 容器里——浏览器访问 `127.0.0.1` 指的是用户自己那台机器。故
        门户会把浏览器重定向到一个打不开的回环地址，`code` 留在地址栏里，
        由用户粘贴回来。

        `state` 编码三样东西（用 `:` 分隔）：客户端 `uuid`、渠道归因、
        上游 state。三者都要在回调时取回：前两个是续期所需的身份字段，
        第三个用来把回调钉死在那一次登录（门户原样回显）。
        """
        session = new_session()
        return AuthSession(
            flow=AuthFlow.PASTE,
            state=f"{session.uuid}:{session.keyfrom}:{session.upstream_state}",
            auth_url=build_login_url(session, portal_base=self.client.portal_base),
            interval=None,
            callback_url=None,
        )

    async def complete_callback(self, raw_url: str, state: str) -> dict:
        """粘贴的回调链接 → 授权码换 token → 归一化凭证。

        `state` 形如 `uuid:keyfrom:upstream_state`：前两段是客户端
        身份（换 token 时必须原样带上），第三段是上游 state（校验回调属于
        那一次登录）。
        """
        uuid, keyfrom, upstream_state = _split_state(state)
        info = parse_callback_url(raw_url)
        if upstream_state and info.state and info.state != upstream_state:
            raise UpstreamProtocolViolation(
                "callback state does not match the pending login")
        credential = await self.client.exchange_code(
            info.code, uuid=uuid, keyfrom=keyfrom)
        return credential.to_dict()

    async def aclose(self) -> None:
        """释放内部 HTTP 连接池。"""
        await self.client.aclose()


def _split_state(state: str) -> tuple[str, str, str]:
    """`state` → `(uuid, keyfrom, upstream_state)`。

    段数不足时缺的段给空串（上游 state 缺失则跳过回调校验；`keyfrom`
    缺失时由 `keyfrom_body` 回落默认值 —— 上游只接受 `a-z0-9_-` 的
    合法归因值，空串会被记成一个不存在的渠道）。
    """
    parts = (state or "").split(":", 2)
    uuid = parts[0] if parts else ""
    keyfrom = parts[1] if len(parts) > 1 else ""
    upstream_state = parts[2] if len(parts) > 2 else ""
    return uuid, keyfrom, upstream_state


__all__ = ["LobsteraiProvider", "credential_key"]
