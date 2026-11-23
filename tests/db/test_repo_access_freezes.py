"""access_freezes 仓储：命中计数、列表、解冻、阈值语义。"""

from __future__ import annotations

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import AccessFreezeRepository


def _repo(tmp_path):
    db = Database(tmp_path / "freeze.sqlite3")
    apply_schema(db.connect())
    return db, AccessFreezeRepository(db)


def test_record_count_and_threshold(tmp_path):
    db, repo = _repo(tmp_path)
    assert repo.count(subject="alice", provider="trae") == 0
    repo.record_hit(subject="alice", provider="trae", reason="code=11128", now=10)
    assert repo.count(subject="alice", provider="trae") == 1
    assert repo.is_frozen(subject="alice", provider="trae", threshold=1) is True
    assert repo.is_frozen(subject="alice", provider="trae", threshold=2) is False
    # 阈值 0 = 只记录不冻结
    assert repo.is_frozen(subject="alice", provider="trae", threshold=0) is False
    # 渠道/发起者隔离
    assert repo.count(subject="bob", provider="trae") == 0
    assert repo.count(subject="alice", provider="codebuddy") == 0
    db.close()


def test_recent_filters_and_order(tmp_path):
    db, repo = _repo(tmp_path)
    repo.record_hit(subject="alice", provider="trae", reason="a", now=1)
    repo.record_hit(subject="bob", provider="codebuddy", reason="b", now=2)
    repo.record_hit(subject="alice", provider="trae", reason="c", now=3)

    all_rows = repo.recent()
    assert [row["reason"] for row in all_rows] == ["c", "b", "a"]
    assert repo.recent(subject="alice")[0]["reason"] == "c"
    assert repo.recent(subject="alice", provider="codebuddy") == []
    assert repo.recent(provider="codebuddy")[0]["subject"] == "bob"
    assert repo.recent(limit=1, offset=1)[0]["subject"] == "bob"
    db.close()


def test_subjects_aggregates_hits(tmp_path):
    db, repo = _repo(tmp_path)
    repo.record_hit(subject="alice", provider="trae", reason="a", now=1)
    repo.record_hit(subject="alice", provider="trae", reason="b", now=5)
    rows = repo.subjects()
    assert rows == [{"subject": "alice", "provider": "trae",
                     "hits": 2, "last_at": 5}]
    db.close()


def test_unfreeze_deletes_only_target_pair(tmp_path):
    db, repo = _repo(tmp_path)
    repo.record_hit(subject="alice", provider="trae", reason="a", now=1)
    repo.record_hit(subject="alice", provider="codebuddy", reason="b", now=2)
    assert repo.unfreeze(subject="alice", provider="trae") == 1
    assert repo.count(subject="alice", provider="trae") == 0
    assert repo.count(subject="alice", provider="codebuddy") == 1
    assert repo.unfreeze(subject="alice", provider="trae") == 0
    db.close()