"""OTLP/HTTP JSON 指标导出后台任务：一轮 = 组 payload → POST `{endpoint}/v1/metrics`。

为什么是后台任务而不是请求路径：导出要打外部采集端，放在请求上等于让聊天
等一个第三方；周期导出 + 累计快照让服务与采集端解耦（采集端挂掉只丢一轮
观测，不影响任何请求）。

**未配置端点 = 每轮 no-op**：返回 None，不覆盖上次运行态（页面显示「未运行」
而不是「刚跑过」），连接都不建——这就是「关 → 零出站」的实现点。

失败只告警一次（`OtlpExporter` 内部负责），且这里**永不抛**：任务抛异常会
触发「任务连续失败」告警，而「采集端连不上」不等于「本服务坏了」。
"""

from __future__ import annotations

from typing import Any

from ..observability.otlp import OtlpExporter

__all__ = ["OtlpExportTask"]


class OtlpExportTask:
    """一轮 = 导出一份累计指标快照（`exporter` 关闭时是 no-op）。"""

    def __init__(self, exporter: OtlpExporter) -> None:
        self._exporter = exporter

    async def run_once(self) -> dict[str, Any] | None:
        """导出一次；端点未配置返回 None（no-op，不覆盖上次运行态）。"""
        if not self._exporter.enabled:
            return None
        ok = await self._exporter.export_once()
        return {"exported": ok, "failed": 0 if ok else 1}
