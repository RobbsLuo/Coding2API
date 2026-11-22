"""`TestClient` 起停的公共辅助。

为什么需要「等启动任务跑完」：`build_app` 的 lifespan 用
`asyncio.create_task(runner.start())` 拉起后台装配（`src/main.py`），而
`start()` 里**首轮** quota / refresh / backup / retention 是立即执行的
（不等第一个周期，否则短命进程永远错过备份与清理）。

这带来一个测试侧的竞态：不进 lifespan 的 TestClient 会把它当成后台任务，
测试体可能与首轮 retention（按保留期删旧行）**并发**。任何用 1970 年之类
「早已过期」时间戳造数据的用例，会随调度时机被删掉一半——表现为本地绿、
CI 红，且重跑结果不稳定。

这里在 `with TestClient(...)` 之后显式等启动任务收尾，把「启动完成」变成
一个确定状态。代价是每个用例多等一次首轮备份（VACUUM INTO，毫秒级）。
"""

from __future__ import annotations

from typing import Any

#: 等启动任务的上限：首轮含一次库备份 + retention 全表聚合，给足 60 秒；
#: 超时说明装配真的卡住了，应当测试失败而不是静默继续。
STARTUP_WAIT_SECONDS = 60.0


def wait_for_startup(app: Any, client: Any) -> None:
    """在 TestClient 的事件循环里等 `runner.start()` 任务结束（幂等）。

    用 portal 直接跑协程，而不是 `time.sleep` 轮询：任务在同一个事件循环里，
    轮询只会让 CPython 的线程切换把它继续往后排。
    """
    task = getattr(app.state, "task_runner_start_task", None)
    portal = getattr(client, "portal", None)
    if task is None or portal is None:
        return

    async def _wait() -> None:
        # 任务可能已经被 accept/断言消费过异常；`start()` 内部 `_guarded` 已吞
        # 掉各任务自身异常，这里 `return_exceptions` 只兜「任务被取消」这类
        # 装配层失败，不掩盖真实错误（取消会在下一次请求上体现为 500）。
        import asyncio

        await asyncio.wait_for(asyncio.shield(task), STARTUP_WAIT_SECONDS)

    portal.call(_wait)