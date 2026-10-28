"""渠道 ToS 风险登记（运营判断，非上游条款原文）。

目的是把「合规灰区」从散落的 `docs/` 搬到**导入凭证的必经路径**上：管理员加号
时能看到这个渠道的评级与理由，审计流水里也留一条。评级不参与调度、不阻断导入
（红线渠道根本没接，不需要运行时拦）。

**评级口径**（本项目自己的运营判断，依据见 `docs/channel-integration-recommendation-*`
与各渠道条款；不是律师意见，也不引用条款原文）：

- `green`：上游公开提供 API / 免费层，正常调用不依赖逆向客户端协议；
- `gray`：靠复用订阅制客户端的私有协议/会话，条款未明确允许第三方转发或多用户
  共享——实践中普遍存在，但存在被收紧/封号的可能；
- `red`：条款明文禁止中转或脚本直调。**当前没有接入任何 red 渠道**（Freebuff
  之类在评估阶段即被排除），保留该档位给未来的登记。

新增渠道必须同时登记；漏登记会在导入时回落 `gray` 并注明「未登记」，宁可
保守也不默认标成 green。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..engine.model_resolver import KNOWN_PROVIDERS

__all__ = ["CHANNEL_TOS", "TosRisk", "tos_for"]

TosLevel = Literal["green", "gray", "red"]


@dataclass(frozen=True)
class TosRisk:
    level: TosLevel
    reason: str
    # 依据来源（文档路径或「运营判断」），审计/界面用来说明这行不是条款原文
    basis: str = "运营判断"

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "reason": self.reason, "basis": self.basis}


CHANNEL_TOS: dict[str, TosRisk] = {
    "zen": TosRisk(
        "green",
        "官方公开网关 /zen/v1，免费层免鉴权，调用不依赖逆向客户端",
        basis="docs/competitor-scan-2026-10-22.md"),
    "kilo": TosRisk(
        "green",
        "标准 OpenAI 兼容网关，官方 /models 带 isFree 标记",
        basis="docs/competitor-scan-2026-10-22.md"),
    "codebuddy": TosRisk(
        "gray",
        "复用个人版客户端私有协议（/v2/plugin/*）；条款未明确允许多用户共享或第三方转发",
        basis="运营判断"),
    "codebuddy_intl": TosRisk(
        "gray",
        "同国内站协议、独立账号体系；条款未明确允许多用户共享或第三方转发",
        basis="运营判断"),
    "trae": TosRisk(
        "gray",
        "复用 IDE 客户端协议与登录态；条款未明确允许脚本直调与多用户共享",
        basis="运营判断"),
    "qoder": TosRisk(
        "gray",
        "COSY 私有签名协议 + 设备指纹；条款未明确允许第三方转发",
        basis="运营判断"),
    "codearts": TosRisk(
        "gray",
        "华为云 CodeArts 订阅的 OAuth/积分接口，按账号并发受限；共享范围条款未明确",
        basis="运营判断"),
    "officeace": TosRisk(
        "gray",
        "AgentArts 控制面开通的应用凭据，按账号下发；条款未明确允许中转",
        basis="运营判断"),
}

_UNKNOWN = TosRisk("gray", "渠道未登记 ToS 风险（按保守档处理）", basis="未登记")


def tos_for(provider_id: str) -> TosRisk:
    """渠道 → 风险登记；未知渠道回落 gray（绝不默认 green）。"""
    risk = CHANNEL_TOS.get(provider_id)
    if risk is None:
        return _UNKNOWN
    return risk


# KNOWN_PROVIDERS 新增渠道时强制同步登记：模块导入即校验，漏登记直接起不来。
_MISSING = sorted(set(KNOWN_PROVIDERS) - set(CHANNEL_TOS))
if _MISSING:  # pragma: no cover - 配置错误，正常代码路径不会触发
    raise RuntimeError(f"渠道缺少 ToS 风险登记: {_MISSING}")
