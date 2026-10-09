"""CodeArts 额度单位换算：上游按 **token** 计量，本服务统一折成「积分」。

上游余额接口（`GET {opengw}/api/v1/user/tokens/balance`）返回的是**每日免费
token 池**，量级达千万（官方「每日千万 Token 免费领」），直接展示既难看，也
让「窗口内到期额度多者先用」的跨渠道排序拿千万级 token 与其它渠道的几百积分
硬比——CodeArts 恒占优。故本服务把 token 折算成与其余渠道同口径的「积分」：

- 每日池满额 10,000,000 token ≡ **1000 积分**；
- 换算比例固定 **1 积分 = 10,000 token**，保号、保留小数。

**这里的「积分」是为跨渠道排序合成的折算值，与上游 CodeArts 的积分（Credits）
同名但不同物**（Q72 实测）：上游积分是系统内置模型（GLM-5.2 / OpenPangu 等）
的计费单位，量级为「体验版 500/月 + 活动福利」，与每日 token 池并行的另一套
账（`GET {snap}/snap-manager/v1/statistics/plugin` 可见，本服务暂未接入）。
两者不要互相换算，展示层也必须区分（见 `web/src/api/display.ts`）。

上游若调整池大小，只需同步本常量；换算只此一处定义，余额、到期阶梯、
单请求扣池与历史数据回填共用，避免各写各的除数。
"""

from __future__ import annotations

# 每日池满额（token）与对应的积分满额：10,000,000 token = 1000 积分。
DAILY_POOL_TOKENS = 10_000_000.0
DAILY_POOL_CREDITS = 1000.0
TOKENS_PER_CREDIT = DAILY_POOL_TOKENS / DAILY_POOL_CREDITS   # = 10_000


def tokens_to_credits(tokens: float) -> float:
    """token → 积分（1 积分 = 10000 token）。调用方保证入参非负。"""
    return tokens / TOKENS_PER_CREDIT
