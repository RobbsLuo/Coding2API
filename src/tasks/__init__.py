"""后台任务包：全局节流器、额度探测、签到、token 预刷新、明细清理。

TaskReport 是各任务共用的执行报告结构，放包顶层避免循环 import
（子模块从 `.` 取用，__init__ 不反向 import 子模块）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TaskReport:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"attempted": self.attempted, "succeeded": self.succeeded,
                "failed": self.failed, "skipped": self.skipped}
