"""额度探测失败 → 「需重新登录」的可操作原因。

额度探测只是一次只读 GET，但它和聊天请求命中同一份凭证：上游回 DEAD
（灵犀 401、CodeBuddy 且账号被限制等）时，这张凭证已经不可能再被聊天
路径救活。历史实现只写 `health = NULL`（未知），凭证仍是 `enabled=1`，
调度器下一轮照样挑它、再撞一次 401，管理台也要等到某次聊天失败才看到
「session dead」。

这里只把**分类为 DEAD** 的失败升级成硬禁用；其余失败（网络抖动、探测
接口单独 4xx）保持原口径「未知」，绝不因为一时探不到额度就把号停掉。
"""

from __future__ import annotations

from .base import ErrKind, UpstreamHTTPError

#: 不同渠道的 DEAD 文案：管理台直接显示，必须指向下一步动作。
_LINGXI_REASON = (
    "WPS 登录态已失效（同一浏览器登录新账号会顶掉旧会话），"
    "请重新粘贴 Cookie；多账号请用独立浏览器配置分别登录"
)
_GENERIC_REASON = "登录态失效（额度探测返回 DEAD），请重新登录或重新粘贴凭证"


def relogin_reason(provider_id: str, error: Exception) -> str:
    """DEAD 失败 → 可操作原因；其他失败返回空串（调用方保持「未知」）。"""
    if not isinstance(error, UpstreamHTTPError):
        return ""
    try:
        kind = error.kind()
    except (AttributeError, TypeError):
        # 基类异常没绑定 classify_status（只有测试替身会这么用）：不升级处置
        return ""
    if kind is not ErrKind.DEAD:
        return ""
    return _LINGXI_REASON if provider_id == "lingxi" else _GENERIC_REASON