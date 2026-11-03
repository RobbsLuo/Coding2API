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