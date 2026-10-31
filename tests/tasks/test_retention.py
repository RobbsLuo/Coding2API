"""RetentionTask 的 rollup 窗口：只重算最近窗口，旧小时不得被改小。

全表 GROUP BY 每 5 分钟跑一次会长时间持有 SQLite 写锁，期间的 `record()`
撞 busy_timeout 后被上层吞掉 → 用量丢失。周期路径改成「最近 2 小时、整点
对齐」的增量重算；历史全量重算由 `scripts/rollup_hourly.py` 显式触发。
"""

from __future__ import annotations

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.stats.collector import StatsCollector
from src.tasks.retention import ROLLUP_OVERLAP_SECONDS, RetentionTask


class _RecordingCollector:
    """只记录窗口起点（本用例不落库）。"""

    def __init__(self) -> None:
        self.since: int | None = -1
        self.purged_now: int | None = -1     # run_once 必须把 now 也传给清理

    def rollup_hourly(self, since: int | None = None) -> int:
        self.since = since
        return 0

    def purge_expired(self, _days: int, now: int | None = None) -> int:
        self.purged_now = now
        return 0


def test_rollup_since_aligns_down_to_whole_hour():
    now = 1_700_000_000
    since = RetentionTask.rollup_since(now)
    assert since % 3600 == 0
    # 必须覆盖完整窗口，且边界小时整小时参与重算（否则 REPLACE 会写小）
    assert since <= now - ROLLUP_OVERLAP_SECONDS
    assert since > now - ROLLUP_OVERLAP_SECONDS - 3600


def test_rollup_since_near_epoch_falls_back_to_full_scan():
    """时间戳极早（测试/时钟异常）时退回全量，宁可慢也不漏。"""
    assert RetentionTask.rollup_since(0) == 0
    assert RetentionTask.rollup_since(60) == 0


def test_run_once_passes_window_to_rollup():
    collector = _RecordingCollector()
    task = RetentionTask(collector)          # type: ignore[arg-type]
    report = task.run_once(now=1_700_000_000)
    assert collector.since == RetentionTask.rollup_since(1_700_000_000)
    # now 同样传播给清理：不传 on 就会用真实时间（测试输出依赖系统时钟）
    assert collector.purged_now == 1_700_000_000
    assert report["rolled_up"] == 0


def test_periodic_rollup_does_not_rewrite_old_hours(tmp_path):
    """窗口外的小时保持原值：证明周期任务不再全表重算。"""
    db = Database(tmp_path / "st.sqlite3")
    apply_schema(db.connect())
    collector = StatsCollector(db)
    now = 1_700_000_000
    old_hour = ((now - 10 * 86400) // 3600) * 3600
    recent_hour = ((now - 60) // 3600) * 3600

    collector.record(username="alice", provider="trae", model="m", ok=True,
                     input_tokens=1, now=old_hour + 10)
    collector.record(username="alice", provider="trae", model="m", ok=True,
                     input_tokens=5, now=recent_hour + 10)
    # 人工改掉旧小时（模拟「历史上已被对账过的汇总」）：窗口内重算不得碰它
    with db.transaction() as conn:
        conn.execute("UPDATE usage_hourly SET requests = 999 WHERE hour_utc = ?",
                     (old_hour,))

    report = RetentionTask(collector).run_once(now=now)
    assert report["rolled_up"] >= 1

    with db.transaction() as conn:
        old = conn.execute("SELECT requests FROM usage_hourly WHERE hour_utc = ?",
                           (old_hour,)).fetchone()
        recent = conn.execute(
            "SELECT requests, input_tokens FROM usage_hourly WHERE hour_utc = ?",
            (recent_hour,)).fetchone()
    assert old["requests"] == 999               # 窗口外：完全不参与重算
    assert recent["requests"] == 1 and recent["input_tokens"] == 5
    db.close()
