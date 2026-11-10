"""签到审计日志仓储：记录、倒序查询、裁剪与索引。"""

from __future__ import annotations

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import CheckinEventRepository
from src.provider.base import CheckinResult


def test_checkin_events_record_recent_and_prune(tmp_path):
    """成功/失败/已签各记一行；倒序读取；裁剪只删超出保留期的行。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    events = CheckinEventRepository(db)
    now = 1_700_000_000

    events.record(credential_id="c1",
                  result=CheckinResult(ok=True, credit=1000.0, code=0,
                                       message="签到成功"),
                  now=now)
    events.record(credential_id="c1",
                  result=CheckinResult(ok=False, code=40001, message="尚未到达权益刷新时间"),
                  now=now + 60)
    events.record(credential_id="c2", trigger="manual",
                  result=CheckinResult(ok=True, already_checked_in=True, code=0,
                                       message="今日已签到"),
                  now=now + 120)

    rows = events.recent("c1")
    assert [row["ok"] for row in rows] == [0, 1]        # 倒序：最新在前
    assert rows[0]["message"] == "尚未到达权益刷新时间" and rows[0]["code"] == 40001
    assert rows[1]["credit"] == 1000.0 and rows[1]["already"] == 0
    # trigger 默认 auto（定时任务）
    assert rows[1]["trigger"] == "auto" and rows[1]["credential_id"] == "c1"

    manual = events.recent("c2")
    assert manual[0]["trigger"] == "manual" and manual[0]["already"] == 1
    # 已签行的 credit 为 None 时如实存 None，不猜 0
    assert manual[0]["credit"] is None

    # limit 生效并收敛到 [1, 200]
    assert len(events.recent("c1", limit=1)) == 1
    assert len(events.recent("c1", limit=0)) == 1        # 下限 1
    assert len(events.recent("c1", limit=10_000)) == 2   # 上限不影响本就很少的行

    # prune：cutoff = now - keep_days*86400，严格早于 cutoff 的删
    assert events.prune(keep_days=0, now=now + 3600) == 3
    assert events.recent("c1") == []
    db.close()


def test_checkin_events_index_exists(tmp_path):
    """(credential_id, ts) 索引必须建出来（历史查询走它）。"""
    db = Database(tmp_path / "index.sqlite3")
    apply_schema(db.connect())
    indexes = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_checkin_cred_ts" in indexes
    db.close()

def test_checkin_events_latest_since_picks_newest_per_credential(tmp_path):
    """只取窗口内每凭证最新一条：带结果给列表页「今日签到」，缺席 = 没结论。

    窗口前的行不参与（昨天的成功不能冒充今天）；同凭证多行取 ts 最大那条
    ——签到失败后重试成功是常见序列，取错会把已签的号显示成失败。
    """
    db = Database(tmp_path / "latest.sqlite3")
    apply_schema(db.connect())
    events = CheckinEventRepository(db)
    now = 1_700_000_000

    events.record(credential_id="c1",
                  result=CheckinResult(ok=False, message="昨天的失败"),
                  now=now - 10)
    events.record(credential_id="c1",
                  result=CheckinResult(ok=False, code=40001, message="尚未到达权益刷新时间"),
                  now=now)
    events.record(credential_id="c1",
                  result=CheckinResult(ok=True, credit=100.0, code=0, message="领取成功"),
                  now=now + 60)
    events.record(credential_id="c2", result=CheckinResult(ok=True),
                  now=now - 5)

    latest = events.latest_since(now)
    assert set(latest) == {"c1"}                       # c2 只有窗口前的行 → 缺席
    assert latest["c1"]["ok"] == 1 and latest["c1"]["credit"] == 100.0
    assert latest["c1"]["message"] == "领取成功" and latest["c1"]["ts"] == now + 60
    # 空窗口返回空字典（不抛错、不返回 None）
    assert events.latest_since(now + 10_000) == {}
    db.close()


def test_consecutive_failures_counts_only_trailing_streak(tmp_path):
    """只数最近一段连续失败：中间成功一次即清零，窗口外不参与。"""
    db = Database(tmp_path / "streak.sqlite3")
    apply_schema(db.connect())
    events = CheckinEventRepository(db)
    now = 1_700_000_000

    def row(credential_id: str, ok: bool, offset: int, message: str = "") -> None:
        events.record(credential_id=credential_id,
                      result=CheckinResult(ok=ok, message=message), now=now + offset)

    # c1：失败、失败、失败（连续 3）
    row("c1", False, 0)
    row("c1", False, 10)
    row("c1", False, 20)
    # c2：失败、成功、失败 → 最近一次成功 → 不算
    row("c2", False, 0)
    row("c2", True, 10)
    row("c2", False, 20)
    # c3：只有 1 次失败（不到阈值）
    row("c3", False, 30, "上游繁忙")
    # c4：窗口外的连续失败不计
    row("c4", False, -10_000)
    row("c4", False, -9_000)

    rows = events.consecutive_failures(min_streak=2, since=now)
    assert rows == [{"credential_id": "c1", "streak": 3, "last_ts": now + 20,
                     "message": ""}]
    # 阈值 1：c3 也进来（c2 最近一次成功仍排除）
    short = events.consecutive_failures(min_streak=1, since=now)
    assert {item["credential_id"] for item in short} == {"c1", "c3"}
    assert short[0]["credential_id"] == "c1"          # 按 streak 降序
    assert "上游繁忙" in {item["message"] for item in short}
    # 规则关闭：阈值 0 恒空
    assert events.consecutive_failures(min_streak=0, since=now) == []
    db.close()
