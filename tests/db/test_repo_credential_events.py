"""凭证可用性时间线（credential_events）：状态迁移落库、时间窗查询、保留期裁剪。

时间线的价值在于**不用翻服务端日志**回答「昨天 14:00 这个号为什么不可用」，
所以这里逐条钉住「哪个状态迁移对应哪种 event」，以及「没发生迁移就不许落行」。
"""

from __future__ import annotations

import time

from src.db.migrate import _read_schema
from src.db.repo import (
    CREDENTIAL_EVENT_KINDS,
    CREDENTIAL_EVENT_RETENTION_DAYS,
    CredentialEventRepository,
)
from src.engine.scheduler import ErrorOutcome, ModelCooldown
from src.provider.base import ErrKind
from src.request_context import set_request_id
from src.stats.collector import StatsCollector
from src.tasks.retention import RetentionTask


def _events(db, credential_id: str | None = None) -> list[dict]:
    """按写入顺序（rowid）取时间线：同一秒内的多条必须稳定可比。"""
    sql = "SELECT * FROM credential_events"
    params: tuple = ()
    if credential_id is not None:
        sql += " WHERE credential_id = ?"
        params = (credential_id,)
    return [dict(row) for row in
            db.connect().execute(f"{sql} ORDER BY rowid", params).fetchall()]


def _add(credentials, provider: str = "codebuddy") -> str:
    return credentials.add(provider=provider, credential_data={"bearer_token": "t"})


def test_event_kinds_match_schema_check():
    """枚举与 schema 的 CHECK 不得漂移。

    写入非法 event 会被 SQLite 的 CHECK 拒绝（整笔状态迁移回滚），所以两边
    必须同时改；这条测试就是提醒「加事件别忘了改表」。
    """
    schema = _read_schema()
    for kind in CREDENTIAL_EVENT_KINDS:
        assert f"'{kind}'" in schema, kind


def test_set_enabled_records_switch_events(repo):
    """用户软开关：停用/启用各落一行，且只记在真实存在的凭证上。"""
    credentials, db = repo
    cid = _add(credentials)

    assert credentials.set_enabled(cid, False) is True
    assert credentials.set_enabled(cid, True) is True

    events = _events(db)
    assert [(e["event"], e["detail"], e["model"], e["reason"]) for e in events] == [
        ("disabled", "管理台停用", None, ""),
        ("enabled", "管理台启用", None, ""),
    ]
    # 不存在的凭证没有状态迁移，就没有历史（不能凭空落一行）
    assert credentials.set_enabled("cred_missing", False) is False
    assert len(_events(db)) == 2


def test_save_error_records_model_and_account_cooldowns(repo):
    """冷却开始：模型级带 model，账号级 model 为空；本次不动冷却时不落行。"""
    credentials, db = repo
    cid = _add(credentials)

    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "glm-5.2": ModelCooldown(cooling_until=9999, hits=2, reason="model")}))
    event = _events(db)[0]
    assert (event["event"], event["reason"], event["model"]) == (
        "cooldown_start", "model", "glm-5.2")
    assert "9999" in event["detail"] and "第 2 次" in event["detail"]

    credentials.save_error(cid, ErrorOutcome(
        cooling_until=8888, cooling_kind=ErrKind.SOFT.value))
    event = _events(db)[-1]
    assert (event["event"], event["reason"], event["model"]) == (
        "cooldown_start", ErrKind.SOFT.value, None)
    assert "8888" in event["detail"]

    # `cooling_until=None` 表示「本次不动冷却」：落一行会被读成「此刻刚进冷却」，
    # 与库里的旧值自相矛盾，故不记。
    before = len(_events(db))
    credentials.save_error(cid, ErrorOutcome(err_count=1))
    assert len(_events(db)) == before


def test_save_error_records_hard_failure(repo):
    """硬禁用：优先写可操作原因，为空才回落历史的 "session dead"。"""
    credentials, db = repo
    cid = _add(credentials)

    credentials.save_error(cid, ErrorOutcome(disabled=True, disabled_reason="账号被风控限制"))
    event = _events(db)[-1]
    assert (event["event"], event["reason"]) == ("hard_failure", "dead")
    assert "账号被风控限制" in event["detail"]

    credentials.save_error(cid, ErrorOutcome(disabled=True))
    assert "session dead" in _events(db)[-1]["detail"]


def test_revive_records_manual_recovery(repo):
    """人工恢复：保留/强制/本地冷却三种语义都写进 detail，reason 记当时来源。"""
    credentials, db = repo
    cid = _add(credentials)
    future = int(time.time()) + 3600

    credentials.save_error(cid, ErrorOutcome(
        cooling_until=future, cooling_kind=ErrKind.RESTRICTED.value))
    assert credentials.revive(cid) is True
    event = _events(db)[-1]
    assert (event["event"], event["reason"]) == ("revived", ErrKind.RESTRICTED.value)
    assert "保留上游点名冷却" in event["detail"]

    assert credentials.revive(cid, force=True) is True
    event = _events(db)[-1]
    assert event["reason"] == "forced" and "强制清除" in event["detail"]

    other = _add(credentials)
    credentials.save_error(other, ErrorOutcome(
        cooling_until=future, cooling_kind=ErrKind.OTHER.value))
    assert credentials.revive(other) is True
    event = _events(db, other)[-1]
    assert event["reason"] == ErrKind.OTHER.value and "清除本地冷却" in event["detail"]

    # 不存在的凭证：没有迁移，不落行
    assert credentials.revive("cred_missing") is False
    assert len(_events(db, "cred_missing")) == 0


def test_cool_provider_records_channel_level_event(repo):
    """渠道级风控：每个受影响的凭证各落一行；已经更晚的冷却不重复记。"""
    credentials, db = repo
    first = _add(credentials)
    second = _add(credentials)
    other_channel = _add(credentials, provider="trae")
    until = int(time.time()) + 3600

    assert credentials.cool_provider("codebuddy", until=until) == 2
    events = _events(db)
    assert {e["credential_id"] for e in events} == {first, second}
    assert all(e["event"] == "cooldown_start"
               and e["reason"] == ErrKind.RESTRICTED.value for e in events)
    assert all(str(until) in e["detail"] for e in events)
    assert _events(db, other_channel) == []

    # 目标时刻不晚于现有冷却 → 不改库也不落时间线（否则同一段冷却刷两遍）
    assert credentials.cool_provider("codebuddy", until=until) == 0
    assert len(_events(db)) == 2


def test_cooldown_end_events_from_purge_and_success(repo):
    """冷却结束：到期回收、成功响应清负缓存、整渠道人工恢复都要留痕。"""
    credentials, db = repo
    cid = _add(credentials)

    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "old": ModelCooldown(cooling_until=500, hits=1, reason="model")}))
    assert credentials.purge_expired_model_cooldowns(now=1000) == 1
    event = _events(db)[-1]
    assert (event["event"], event["reason"], event["model"]) == (
        "cooldown_end", "expired", "old")

    credentials.save_error(cid, ErrorOutcome(
        cooling_until=600, cooling_kind=ErrKind.SOFT.value))
    assert credentials.purge_expired_account_cooldowns(now=1000) == 1
    event = _events(db)[-1]
    assert (event["event"], event["reason"], event["model"]) == (
        "cooldown_end", "expired", None)

    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "blocked-model": ModelCooldown(cooling_until=9999, hits=1, reason="blocked")}))
    credentials.save_success(cid, model="blocked-model")
    event = _events(db)[-1]
    assert (event["event"], event["reason"], event["model"]) == (
        "cooldown_end", "blocked", "blocked-model")
    assert "成功响应" in event["detail"]

    # 没命中负缓存行：不记「冷却结束」（本来就不在冷却里）
    before = len(_events(db))
    credentials.save_success(cid, model="not-blocked")
    assert len(_events(db)) == before

    refreshed = _add(credentials)
    credentials.save_error(refreshed, ErrorOutcome(model_cooldowns={
        "m2": ModelCooldown(cooling_until=9999, hits=1, reason="blocked")}))
    assert credentials.clear_blocked(provider="codebuddy", model="m2") == 1
    event = _events(db, refreshed)[-1]
    assert (event["event"], event["reason"], event["model"]) == (
        "cooldown_end", "blocked", "m2")
    assert "人工恢复" in event["detail"]


def test_recent_filters_window_paginates_and_clamps(repo):
    """时间窗是闭区间、按 (ts, id) 倒序、limit 收敛到 [1, 200]。"""
    credentials, db = repo
    cid = _add(credentials)
    conn = db.connect()
    for index in range(5):
        conn.execute(
            "INSERT INTO credential_events "
            "(id, credential_id, ts, event, reason, model, detail, request_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"evt_{index}", cid, 100 + index, "cooldown_start", "", None, "", None))
    conn.commit()
    events = CredentialEventRepository(db)

    assert [e["id"] for e in events.recent(cid)] == [
        "evt_4", "evt_3", "evt_2", "evt_1", "evt_0"]
    assert [e["id"] for e in events.recent(cid, since=101)] == [
        "evt_4", "evt_3", "evt_2", "evt_1"]
    assert [e["id"] for e in events.recent(cid, until=102)] == [
        "evt_2", "evt_1", "evt_0"]
    assert [e["id"] for e in events.recent(cid, since=101, until=103)] == [
        "evt_3", "evt_2", "evt_1"]
    # 分页：offset 与 limit 组合，不能重复也不能漏行
    assert [e["id"] for e in events.recent(cid, limit=2, offset=2)] == [
        "evt_2", "evt_1"]
    # 越界值收敛：0 → 1 行，超大 → 全量（不会把前端一次拉爆）
    assert len(events.recent(cid, limit=0)) == 1
    assert len(events.recent(cid, limit=10_000)) == 5
    # 别的凭证的行不混进来
    assert events.recent("cred_other") == []


def test_prune_keeps_retention_boundary(repo):
    """保留期按 ts 裁剪：边界时刻（== cutoff）保留，只有更早的才删。"""
    _credentials, db = repo
    events = CredentialEventRepository(db)
    now = 1_000_000
    cutoff = now - CREDENTIAL_EVENT_RETENTION_DAYS * 86400
    conn = db.connect()
    for event_id, ts in (("old", cutoff - 1), ("edge", cutoff), ("new", now)):
        conn.execute(
            "INSERT INTO credential_events (id, credential_id, ts, event) "
            "VALUES (?,?,?,?)", (event_id, "cred", ts, "enabled"))
    conn.commit()

    assert events.prune(now=now) == 1
    assert {e["id"] for e in events.recent("cred")} == {"edge", "new"}


def test_retention_task_prunes_credential_events(repo):
    """留存任务每轮裁剪时间线；未注入仓储（老调用方）时计数为 0。"""
    _credentials, db = repo
    collector = StatsCollector(db)
    events = CredentialEventRepository(db)
    conn = db.connect()
    conn.execute(
        "INSERT INTO credential_events (id, credential_id, ts, event) "
        "VALUES (?,?,?,?)", ("old", "cred", 1, "enabled"))
    conn.commit()

    report = RetentionTask(collector, credential_events=events).run_once(now=100 * 86400)
    assert report["purged_credential_events"] == 1
    assert RetentionTask(collector).run_once()["purged_credential_events"] == 0


def test_request_id_recorded_from_context(repo):
    """时间线的 request_id 取自请求上下文（后台任务为 None），便于与日志对齐。"""
    credentials, db = repo
    cid = _add(credentials)

    set_request_id("req_abc")
    try:
        credentials.set_enabled(cid, False)
    finally:
        set_request_id(None)

    assert _events(db)[0]["request_id"] == "req_abc"
