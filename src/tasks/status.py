"""后台任务运行态（进程内）与任务清单。

为什么只存内存：管理台要看的是「最近一轮跑成功没有、什么时候跑的」。落库
能留下历史，但要引入新表、保留期清理与迁移；而「最近一轮」在进程重启后本来
就是未知的——显示「本次启动以来未运行」比编造一条历史更诚实。上游同类项目
（ithtelab/workbuddy-manager）是「容器日志重建即丢，所以采集落库」，本项目
任务在进程内、结果直接可得，不需要那一层。

任务 key 是配置项与运行态的连接键：`HOT_SETTINGS` 里标了 `task` 的配置项会被
管理台归到对应任务卡片下，改周期时人就在看这个任务的运行情况。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TaskSpec:
    """一个后台任务的静态描述（名称 + 一句话说明）。"""

    key: str
    name: str
    description: str


# 与 TaskRunner.start() 里建立的循环一一对应。这里只描述「有哪些任务」，
# 周期与开关是运行态（可热更），由 TaskRunner.task_status() 现算。
TASK_SPECS: tuple[TaskSpec, ...] = (
    TaskSpec("quota_probe", "额度探测",
             "探测上游剩余额度并写回凭证健康度；周期见「额度探测周期」。"),
    TaskSpec("refresh", "token 预刷新",
             "到期前窗口内轮换 refresh token；只在进入窗口时真正调用上游。"),
    TaskSpec("checkin", "每日签到",
             "全天每 10 分钟检查一次；成功后该凭证当日封账，失败持续重试。"),
    TaskSpec("growth", "成长中心",
             "领旅行礼物 / 派 Buddy / 领任务奖 / 补登 / 兑换 / 抽奖 / 开盲盒；"
             "周期见「成长中心周期」。"),
    TaskSpec("activity", "活跃上报",
             "按配置时点为 CodeBuddy 账号补发对话事件；关闭时每轮都是 no-op。"),
    TaskSpec("retention", "明细清理",
             "小时汇总 + 90 天明细清理，顺带回收过期冷却与积分流水。"),
    TaskSpec("model_catalog", "渠道模型列表刷新",
             "兜底重拉各渠道模型表：没有它，列表只在有人访问 /v1/models 或 "
             "Playground 时才刷新，纯 API 用法的部署会让归属表与落盘快照变陈旧。"),
    TaskSpec("openrouter_catalog", "模型目录刷新（OpenRouter）",
              "一次抓取 OpenRouter 公开模型列表，落盘三份数据：刊例价（成本估算）、"
              "明细元数据（「模型列表」页）、Artificial Analysis 能力指数"
              "（Playground 与 /v1/models 的 benchmarks）。拉取失败只是成本与"
              "分数显示 —，不影响模型列表。"),
    TaskSpec("alert", "运维告警",
             "周期评估四类风险（池耗尽 / 任务连续失败 / token 临近到期 / 上游"
             "错误率骤升），命中落站内记录并可推送 webhook；周期见「运维告警周期」。"),
)

TASK_BY_KEY: dict[str, TaskSpec] = {spec.key: spec for spec in TASK_SPECS}


@dataclass(frozen=True)
class TaskRun:
    """一次**真实执行**的结果（no-op 不入账，见 TaskStatusStore）。"""

    started_at: float
    finished_at: float
    ok: bool
    report: dict[str, Any] | None
    error: str | None


class TaskStatusStore:
    """进程内任务运行态：上次真跑于何时、结果如何、跑了多少轮。

    只记录真实执行：`_sync_checkin` 这类「醒了但未到点」的 no-op 不覆盖上一次
    结果，否则页面会显示「签到刚刚跑过」，而当天其实一次都没签。
    """

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._runs: dict[str, TaskRun] = {}
        self._counts: dict[str, int] = {}
        # 连续失败计数（P1-7）：成功清零、失败 +1。只记真实执行——no-op 轮次
        # 不调 record，不会误清也不会误增。
        self._failure_streaks: dict[str, int] = {}

    def record(self, key: str, *, started_at: float, ok: bool,
               report: dict[str, Any] | None, error: str | None) -> None:
        self._runs[key] = TaskRun(started_at=started_at, finished_at=self._now(),
                                  ok=ok, report=report, error=error)
        self._counts[key] = self._counts.get(key, 0) + 1
        if ok:
            self._failure_streaks[key] = 0
        else:
            self._failure_streaks[key] = self._failure_streaks.get(key, 0) + 1

    def runs(self, key: str) -> int:
        return self._counts.get(key, 0)

    def consecutive_failures(self, key: str) -> int:
        """连续失败次数（P1-7 告警）：成功一轮即清零；从未失败返回 0。"""
        return self._failure_streaks.get(key, 0)

    def failing(self, threshold: int) -> list[tuple[str, int]]:
        """连续失败达到阈值的任务 (key, streak)，按 key 排序稳定输出。

        threshold ≤0 时视为规则关闭，恒返回空列表。只包含「已真跑过且连续
        失败」的任务：从未运行的 key 不在 `_failure_streaks` 里，不会被误报。
        """
        if threshold <= 0:
            return []
        return sorted((key, streak) for key, streak in self._failure_streaks.items()
                      if streak >= threshold)

    def get(self, key: str) -> TaskRun | None:
        return self._runs.get(key)
