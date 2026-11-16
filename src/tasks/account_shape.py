"""账号形态判定：哪些凭证不该跑「个人版」后台任务。

CodeBuddy 企业版与个人版是两套端点（额度探测已按 `quota_probe_mode ==
"enterprise"` 走另一条接口，见 `provider/codebuddy/client.py`
`_fetch_enterprise_quota`）。个人版的签到 / 成长中心活动企业号没有入口，硬跑
只会稳定失败：

- 签到：企业号打个人版签到端点必然失败，把「今日全成功」涂黑，还会触发
  `checkin_failing` 告警；
- 成长中心：领礼 / 派 Buddy / 任务奖同样不存在，白耗全局 Pacer。

2026-11-03 `misakano7545/workbuddy2api-panel` 27217e4 / f6c4a45 给出同一结论
（按账号形态跳过，而不是按「任务跑成功但上游回失败」事后告警）。
"""
from __future__ import annotations

from typing import Any

__all__ = ["ENTERPRISE_QUOTA_MODE", "is_enterprise_account"]

ENTERPRISE_QUOTA_MODE = "enterprise"


def is_enterprise_account(data: dict[str, Any] | None) -> bool:
    """凭证是否是上游的企业版账号（只影响个人版任务，不影响聊天/额度）。"""
    return isinstance(data, dict) and data.get("quota_probe_mode") == ENTERPRISE_QUOTA_MODE