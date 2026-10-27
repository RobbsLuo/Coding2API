"""凭证仓储：模型级/账号级冷却持久化、清理与保留任务。"""

from __future__ import annotations

import time

from src.engine.scheduler import ErrorOutcome
from src.provider.base import ErrKind
from src.stats.collector import StatsCollector
from src.tasks.retention import RetentionTask


def test_repo_persists_model_cooldowns_separately_from_account(repo):
    """模型级冷却只写独立表：账号级 cooling_until 必须保持为空。"""
    from src.engine.scheduler import ModelCooldown

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(
        err_count=0, model_cooldowns={"glm-5.2": ModelCooldown(
            cooling_until=9999, hits=1, reason="model")}))

    candidate = credentials.candidates()[0]
    assert candidate.cooling_until is None            # 账号级未被污染
    assert not candidate.is_selectable(1000, "glm-5.2")
    assert candidate.is_selectable(1000, "kimi-k2")   # 其他模型仍可用
    assert credentials.model_cooldowns_for(cid, now=1000) == {"glm-5.2": 9999}

def test_repo_model_cooldown_upsert_escalates_hits(repo):
    """同 (凭证, 模型) 反复命中时原地升级 hits，不产生多行。"""
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=100, hits=1, reason="model")}))
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=200, hits=2, reason="model")}))
    rows = db.connect().execute(
        "SELECT cooling_until, hits, reason FROM credential_model_cooldowns").fetchall()
    assert [(r["cooling_until"], r["hits"], r["reason"]) for r in rows] == [(200, 2, "model")]

def test_repo_candidates_carry_reason_so_backoff_escalates(repo):
    """candidates() 必须把 reason 一并发出来，否则 blocked 退避永远停在 6h。

    note_error 用 reason 判断「是否同一原因」；漏读会让每次都被当成换了原因
    而把 hits 重置为 1，6h→12h→24h 的升级形同虚设。
    """
    from src.engine.scheduler import Scheduler

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    scheduler = Scheduler()
    now = 1_000_000

    def note(**kw):
        candidate = credentials.candidates()[0]
        outcome = scheduler.note_error(candidate, ErrKind.BLOCKED, now,
                                       model="glm-5.2", **kw)
        credentials.save_error(cid, outcome)
        return outcome.model_cooldowns["glm-5.2"]

    first = note()
    assert (first.hits, first.reason) == (1, "blocked")
    assert credentials.candidates()[0].model_cooldowns["glm-5.2"].reason == "blocked"
    second = note()
    assert second.hits == 2 and second.cooling_until == now + 12 * 3600
    third = note()
    assert third.hits == 3 and third.cooling_until == now + 24 * 3600

def test_repo_account_cooldown_clears_model_entries(repo):
    """账号级冷却必须清空模型级条目：否则「切模型」能绕过账号级限流。"""
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=9999, hits=1)}))
    credentials.save_error(cid, ErrorOutcome(cooling_until=8888, err_count=0))
    assert db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"] == 0

def test_repo_save_success_clears_blocked_only(repo):
    """成功清除 negative cache，但不清 6004 模型级限流（对齐上游重置）。"""
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "blocked-model": ModelCooldown(cooling_until=9999, hits=1, reason="blocked"),
        "limited-model": ModelCooldown(cooling_until=9999, hits=1, reason="model")}))
    credentials.save_success(cid, model="blocked-model")
    remaining = {r["model"] for r in db.connect().execute(
        "SELECT model FROM credential_model_cooldowns").fetchall()}
    assert remaining == {"limited-model"}
    # 不传 model 时不做任何模型级清理
    credentials.save_success(cid)
    assert db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"] == 1

def test_repo_revive_and_delete_clear_model_cooldowns(repo):
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=9999, hits=1)}))
    assert credentials.revive(cid) is True
    count = db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"]
    assert count == 0
    # 再次写入后删除凭证：残留行必须一并清掉（重建同 id 不应继承旧冷却）
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=9999, hits=1)}))
    assert credentials.delete(cid) is True
    assert db.connect().execute(
        "SELECT COUNT(*) AS c FROM credential_model_cooldowns").fetchone()["c"] == 0

def test_repo_purges_only_expired_model_cooldowns(repo):
    from src.engine.scheduler import ModelCooldown

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "old": ModelCooldown(cooling_until=500, hits=1),
        "fresh": ModelCooldown(cooling_until=5000, hits=1)}))
    assert credentials.purge_expired_model_cooldowns(now=1000) == 1
    names = {r["model"] for r in db.connect().execute(
        "SELECT model FROM credential_model_cooldowns").fetchall()}
    assert names == {"fresh"}

def test_repo_model_cooldowns_for_skips_expired(repo):
    from src.engine.scheduler import ModelCooldown

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "old": ModelCooldown(cooling_until=500, hits=1),
        "fresh": ModelCooldown(cooling_until=5000, hits=1)}))
    assert credentials.model_cooldowns_for(cid, now=1000) == {"fresh": 5000}

def test_retention_task_purges_expired_model_cooldowns(repo):
    """留存任务顺带回收过期冷却行；未注入凭证仓储时该计数为 0。"""
    from src.engine.scheduler import ModelCooldown

    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "m": ModelCooldown(cooling_until=int(time.time()) - 10, hits=1)}))
    collector = StatsCollector(_db)
    report = RetentionTask(collector, credentials=credentials).run_once()
    assert report["expired_coolings"] == 1
    # 不传 credentials（旧调用方）时不报错，计数为 0
    assert RetentionTask(collector).run_once()["expired_coolings"] == 0

def test_retention_task_clears_expired_account_cooldown(repo):
    """账号级冷却过期后置 NULL：否则陈旧值会永久留在库里。

    选号用 `cooling_until > now` 判断，过期值不挡凭证，但管理台列表下发的是
    原始值——一条几周前的冷却会让该行在「冷却」维度排序失真。
    """
    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    now = int(time.time())
    credentials.save_error(cid, ErrorOutcome(cooling_until=now - 10, err_count=0))
    assert credentials.candidates()[0].cooling_until == now - 10

    collector = StatsCollector(_db)
    report = RetentionTask(collector, credentials=credentials).run_once()
    assert report["expired_account_coolings"] == 1
    assert credentials.candidates()[0].cooling_until is None
    # 再跑一轮：已经没有过期值，计数归零（幂等）
    assert RetentionTask(collector, credentials=credentials).run_once()[
        "expired_account_coolings"] == 0

def test_retention_task_keeps_active_account_cooldown(repo):
    """仍在冷却期内的账号级冷却不能被清掉：清掉等于提前解冻一个被限流的号。"""
    credentials, _db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    future = int(time.time()) + 3600
    credentials.save_error(cid, ErrorOutcome(cooling_until=future, err_count=0))

    collector = StatsCollector(_db)
    report = RetentionTask(collector, credentials=credentials).run_once()
    assert report["expired_account_coolings"] == 0
    assert credentials.candidates()[0].cooling_until == future
    # 未注入凭证仓储时同样不报错
    assert RetentionTask(collector).run_once()["expired_account_coolings"] == 0

def test_retention_task_prunes_audit_events(repo):
    """留存任务按保留期回收审计流水；未注入时计数为 0（README「审计保留 90 天」）。"""
    from src.db.repo import AuditRepository

    _credentials, db = repo
    audit = AuditRepository(db)
    # 两条都远在过去；retention_days=0 → cutoff=当前时刻，两条都 < cutoff 即被删
    audit.record(actor="root", action="login.success", now=100)
    audit.record(actor="root", action="login.failure", now=200)
    collector = StatsCollector(db)
    report = RetentionTask(collector, retention_days=0, audit=audit).run_once()
    assert report["purged_audit"] == 2
    assert audit.query() == []
    # 不传 audit（旧调用方）时不报错，计数为 0
    assert RetentionTask(collector).run_once()["purged_audit"] == 0

def test_credential_edit_lock_is_reused_per_id(repo):
    """编辑锁按凭证 id 分键复用：同 id 返回同一把锁，不同 id 各自独立。"""
    credentials, db = repo
    first = credentials.edit_lock("cred_a")
    assert credentials.edit_lock("cred_a") is first
    assert credentials.edit_lock("cred_b") is not first
    db.close()

def test_save_error_disabled_uses_actionable_reason(repo):
    """硬禁用优先写 outcome 的可操作原因，缺失才回落 "session dead"。

    管理台直接展示 `disabled_reason`：11140「账号被上游限制」写成 session dead
    会把人引向「重新登录」，而实际动作是申诉或换号。
    """
    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.save_error(cid, ErrorOutcome(disabled=True,
                                             disabled_reason="账号被上游限制，需申诉"))
    row = db.connect().execute(
        "SELECT disabled, disabled_reason FROM credentials WHERE id = ?", (cid,)).fetchone()
    assert (row["disabled"], row["disabled_reason"]) == (1, "账号被上游限制，需申诉")

    other = credentials.add(provider="codebuddy", credential_data={"bearer_token": "u"})
    credentials.save_error(other, ErrorOutcome(disabled=True))
    row = db.connect().execute(
        "SELECT disabled_reason FROM credentials WHERE id = ?", (other,)).fetchone()
    assert row["disabled_reason"] == "session dead"
