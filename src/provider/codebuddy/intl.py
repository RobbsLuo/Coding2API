"""CodeBuddy 国际站（www.codebuddy.ai）渠道。

与国内站（copilot.tencent.com）**同协议**：同一套 `/v2/chat/completions`、
`/v3/config`、`/v2/plugin/auth/*`、`/v2/billing/*` 路径，同一个 Keycloak realm
（`copilot`）。实测（2026-10-12，匿名探针）两站路由表一致，所以这里直接复用
`CodeBuddyClient`，不复制一份协议代码。

**独立渠道而非「改 CODEBUDDY_API_ENDPOINT 切站点」**：endpoint 在本项目是进程级
单值，切过去会让存量国内站凭证全部打到国际站。国际版与国内版是两套账号体系
（各自注册、各自计费），需要同时持有。

**唯一的协议行为差异：首条 system 强制**。国际站网关要求 `messages[0]` 是
system prompt，否则整单 400 / 11128 `first message is not system prompt`
（2026-10-12 真实国际站凭证实测：无 system → 400，同请求前置中性 system →
200）。国内站无此要求，因此 `require_leading_system` 只在国际站装配时开，
存量国内站出站行为不变。注入正文取中性最小句且不带任何厂商指纹（否则撞
11128 内容风控）；客户端自带 system 时不注入、不覆盖。

其余能力差异（只保留**已验证存在**的路径）：

- 聊天 / 模型列表 / 额度探测 / 设备码登录 / token 刷新 —— 全部启用。
  判据见 TECHNICAL §5.5：匿名请求命中这些路径返回网关 401（路由已注册），
  而未注册路径返回应用层 JSON 404（`/billing/meter/daily-checkin` 实测 401，
  `/billing/nope` 实测 404，两者可区分）。
- 签到 / 成长中心 / 活跃上报 —— **全部关闭**。
  - 成长中心：国际站网关把整个 `/v2/activity/*` 前缀挂在认证插件后面（实测
    `/v2/activity/nope` 也返回 401 而非 404），因此 401 无法证明子路由存在；
    而成长中心含抽奖 / 连登兑换 / 盲盒等**不可逆**动作，在未验证的端点上
    盲跑会真实消耗账号权益。
  - 活跃上报：**启用**。活跃上报打的 `/v2/report` 与成长中心**不是同一路径**
    ——2026-10-12 实测国际站 `/v2/report` 带凭证回 `200 {"code":0}`，而同批
    探针里 `/v2/report-nope` 回应用层 `404 Route Not Found`，两者可区分，
    即端点真实存在且接受上报。（早先「国际站无活跃上报」的判断只探过
    `/v2/activity/*` 前缀，没探过 `/v2/report`，属探针覆盖不全。）

  处置：`growth` / `checkin` / `checkin_scope` / `checkin_status` 置 `None`——
  任务层与 API 层都用 `getattr(provider, ..., None)` / `hasattr(provider, ...)`
  探测能力，置 `None` 即自动跳过，**无需在任务层加渠道分支**；管理台的
  「签到」「成长中心」按钮按 `supportsCheckin()` / `provider === "codebuddy"`
  判定，对本渠道天然不出现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..base import Model
from .client import CodeBuddyProvider


@dataclass(slots=True)
class CodeBuddyIntlProvider(CodeBuddyProvider):
    """国际站渠道；差异只由渠道 id 与下面几个 `None` 表达。

    保留 dataclass 字段覆盖而非另写一份实现：客户端、凭证解析、事件分类、
    签到协议、活跃上报、刷新全部共用国内站代码，无此能力的调用点由 `None`
    表达。
    """

    id: str = "codebuddy_intl"

    async def list_models(self, credential_data: dict[str, Any]) -> list[Model]:
        """国际站列表只保留当前账号套餐真正允许的模型。

        上游 `/v3/config` 不按套餐过滤：免费号也会看到 `gpt-5.6-luna` 等
        调用时才 400 / 11158 的模型。探活复用父类的动态拉取与静态兜底，
        再按当前凭证过滤；限流 / 网络抖动不会误删模型（见 client 的
        `filter_available_models`）。
        """
        # dataclass(slots=True) 会重建类，零参 super() 的 __class__ 单元指向旧类，
        # 必须显式调用父类方法。
        models = await CodeBuddyProvider.list_models(self, credential_data)
        return await self.client.filter_available_models(
            self.credential_from(credential_data), models)

    # 下面几项刻意覆盖父类方法为 None（不是删方法）：调用方一律用
    # `getattr(provider, "growth", None)` 之类探测能力，置 None 就是
    # 「本渠道没有该能力」，无需在任务层加渠道分支。
    growth = None
    # 签到三项（后台任务用 checkin_scope 探测，管理台用 checkin /
    # checkin_status 探测），渠道侧无此活动，一并关闭。
    checkin = None
    checkin_scope = None
    checkin_status = None