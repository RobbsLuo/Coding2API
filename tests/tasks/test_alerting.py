"""运维告警：规则判定（纯函数）与 TaskStatusStore 连续失败跟踪。"""

from __future__ import annotations

from src.tasks.alerting import SEVERITY_CRITICAL, SEVERITY_WARNING, evaluate_alerts
from src.tasks.status import TaskStatusStore


def _no_alerts(**overrides):
    """默认「一切正常」的 evaluate_alerts 入参，按用例覆盖单项。"""
    kwargs = dict(
        pool={"total": 3, "ready": 3, "cooling": 0, "paused": 0, "disabled": 0},
        pool_ready_min=1,
        failing_tasks=[],
        expiring=[],
        error_counts=(0, 0),
        error_rate_threshold=0.5,
        error_rate_min_requests=20,
    )
    kwargs.update(overrides)
    return evaluate_alerts(**kwargs)

# --------------------------------------------------------- evaluate_alerts


def test_evaluate_pool_empty_is_critical():
    alerts = _no_alerts(pool={"total": 2, "ready": 0, "cooling": 1,
                              "paused": 0, "disabled": 1})
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule == "pool_empty" and alert.severity == SEVERITY_CRITICAL
    assert alert.scope == "pool"
    assert "可用数为 0" in alert.message
    assert alert.detail["total"] == 2 and alert.detail["threshold"] == 1

def test_evaluate_pool_rule_off_and_zero_total():
    # 阈值 0 = 关闭规则
    assert _no_alerts(pool_ready_min=0,
                      pool={"total": 2, "ready": 0}) == []
    # 池里一个凭证都没有：不是「耗尽」而是「没配」，不告警
    assert _no_alerts(pool={"total": 0, "ready": 0}) == []

def test_evaluate_failing_tasks_warns():
    alerts = _no_alerts(failing_tasks=[("growth", "成长中心", 4)])
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule == "task_failed" and alert.severity == SEVERITY_WARNING
    assert alert.scope == "growth"
    assert "连续失败 4 次" in alert.message
    assert alert.detail["streak"] == 4

def test_evaluate_expiring_tokens_humanizes_remaining():
    expiring = [
        {"id": "c1", "provider": "codebuddy", "nickname": "甲",
         "token_expires_at": 1_000_000, "remaining": 7200},
        {"id": "c2", "provider": "trae", "nickname": "",
         "token_expires_at": 1_000_000, "remaining": 300},
        {"id": "c3", "provider": "trae", "nickname": "",
         "token_expires_at": 1_000_000, "remaining": 30},
    ]
    alerts = _no_alerts(expiring=expiring)
    assert [a.scope for a in alerts] == ["c1", "c2", "c3"]
    assert all(a.rule == "token_expiring" for a in alerts)
    assert "2.0 小时" in alerts[0].message
    assert "5 分钟" in alerts[1].message
    assert "30 秒" in alerts[2].message
    # 昵称为空时回落到 id，不让文案出现空引号
    assert "c2" in alerts[1].message

def test_evaluate_error_rate_guards():
    # 样本不足：不判
    assert _no_alerts(error_counts=(5, 5), error_rate_min_requests=20) == []
    # 阈值 0 = 关闭
    assert _no_alerts(error_counts=(100, 100), error_rate_threshold=0) == []
    # 请求数为 0：即使 min_requests=0 也不判
    assert _no_alerts(error_counts=(0, 0), error_rate_min_requests=0) == []

def test_evaluate_error_rate_fires_above_threshold():
    alerts = _no_alerts(error_counts=(20, 12))
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule == "error_rate" and alert.scope == "pool"
    assert "60%" in alert.message
    assert alert.detail["failed"] == 12

def test_evaluate_error_rate_below_threshold_silent():
    # 样本足够但失败占比未达阈值：不报
    assert _no_alerts(error_counts=(20, 5)) == []

def test_evaluate_multiple_rules_at_once():
    alerts = _no_alerts(
        pool={"total": 2, "ready": 0},
        failing_tasks=[("checkin", "每日签到", 3)],
        expiring=[{"id": "c1", "provider": "trae", "nickname": "x",
                   "token_expires_at": 1, "remaining": 60}],
        error_counts=(30, 30),
    )
    assert [a.rule for a in alerts] == [
        "pool_empty", "task_failed", "token_expiring", "error_rate"]

# --------------------------------------------------------- TaskStatusStore


def test_status_store_tracks_consecutive_failures():
    store = TaskStatusStore(now=lambda: 1.0)
    store.record("growth", started_at=0, ok=False, report=None, error="boom")
    store.record("growth", started_at=0, ok=False, report=None, error="boom")
    assert store.consecutive_failures("growth") == 2
    store.record("growth", started_at=0, ok=True, report={}, error=None)
    assert store.consecutive_failures("growth") == 0
    # 从未运行的任务不算连续失败
    assert store.consecutive_failures("checkin") == 0

def test_status_store_failing_threshold_and_sorting():
    store = TaskStatusStore(now=lambda: 1.0)
    for _ in range(3):
        store.record("growth", started_at=0, ok=False, report=None, error="x")
    store.record("checkin", started_at=0, ok=False, report=None, error="x")
    # 阈值 0 = 关闭规则
    assert store.failing(0) == []
    assert store.failing(2) == [("growth", 3)]
    assert store.failing(1) == [("checkin", 1), ("growth", 3)]

# -------------------------------------- 单 Key 用量暴涨 / 非工作时段请求


def test_evaluate_key_burst_fires_per_key():
    alerts = _no_alerts(
        key_usage=[{"api_key_id": "k1", "username": "alice",
                    "requests": 12, "tokens": 900_000},
                   {"api_key_id": "k2", "username": "bob",
                    "requests": 3, "tokens": 100}],
        key_burst_tokens=500_000,
    )
    assert [a.rule for a in alerts] == ["key_burst"]
    alert = alerts[0]
    assert alert.scope == "k1"
    assert alert.severity == SEVERITY_WARNING
    assert alert.detail["tokens"] == 900_000
    assert "alice" in alert.message


def test_evaluate_key_burst_threshold_disabled():
    assert _no_alerts(key_usage=[{"api_key_id": "k1", "username": "a",
                                  "requests": 1, "tokens": 10**9}],
                      key_burst_tokens=0) == []


def test_evaluate_key_burst_below_threshold_silent():
    assert _no_alerts(key_usage=[{"api_key_id": "k1", "username": "a",
                                  "requests": 1, "tokens": 499_999}],
                      key_burst_tokens=500_000) == []


def test_evaluate_key_burst_handles_missing_fields():
    """老行/异常行缺字段：按 0 处理，不抛异常。"""
    alerts = _no_alerts(key_usage=[{"api_key_id": "k1", "tokens": 600_000}],
                        key_burst_tokens=500_000)
    assert alerts[0].scope == "k1"
    assert alerts[0].detail["username"] is None
    assert alerts[0].detail["requests"] == 0


def test_evaluate_off_hours_fires_above_min_requests():
    alerts = _no_alerts(error_counts=(30, 0), off_hours=True,
                        off_hours_min_requests=20)
    assert [a.rule for a in alerts] == ["off_hours"]
    assert alerts[0].detail["requests"] == 30


def test_evaluate_off_hours_requires_min_requests():
    assert _no_alerts(error_counts=(19, 0), off_hours=True,
                      off_hours_min_requests=20) == []


def test_evaluate_off_hours_disabled_by_zero_threshold_or_flag():
    assert _no_alerts(error_counts=(999, 0), off_hours=True,
                      off_hours_min_requests=0) == []
    assert _no_alerts(error_counts=(999, 0), off_hours=False,
                      off_hours_min_requests=20) == []


def test_in_off_hours_window_semantics():
    from src.tasks.alerting import in_off_hours

    # 白天工作 9..19：区间半开 [9, 19)
    assert in_off_hours(8, 9, 19) is True
    assert in_off_hours(9, 9, 19) is False
    assert in_off_hours(18, 9, 19) is False
    assert in_off_hours(19, 9, 19) is True
    # 跨零点工作 22..7
    assert in_off_hours(23, 22, 7) is False
    assert in_off_hours(6, 22, 7) is False
    assert in_off_hours(7, 22, 7) is True
    assert in_off_hours(12, 22, 7) is True
    # 相等 = 关闭
    assert in_off_hours(3, 0, 0) is False

# ------------------------------------------------------------ 预算分层告警


def test_parse_levels_dedupes_sorts_and_filters():
    from src.tasks.alerting import parse_levels

    assert parse_levels("50,80,95,100") == (50, 80, 95, 100)
    assert parse_levels("100, 50,abc,0,101,50,") == (50, 100)
    assert parse_levels("") == ()
    assert parse_levels("  ,  ") == ()


def test_month_start_is_local_month_boundary():
    from src.tasks.alerting import month_start

    start = month_start(1_790_000_000)
    import time

    tm = time.localtime(start)
    assert (tm.tm_mday, tm.tm_hour, tm.tm_min) == (1, 0, 0)


def test_evaluate_budget_reports_highest_reached_level():
    alerts = _no_alerts(budget_cny=100.0, budget_spent=85.0,
                        budget_levels=(50, 80, 95, 100))
    assert [a.rule for a in alerts] == ["budget"]
    assert alerts[0].scope == "budget_80"
    assert alerts[0].severity == SEVERITY_WARNING
    assert alerts[0].detail["percent"] == 85.0


def test_evaluate_budget_overrun_is_critical():
    alerts = _no_alerts(budget_cny=100.0, budget_spent=140.0,
                        budget_levels=(50, 80, 95, 100))
    assert alerts[0].scope == "budget_100"
    assert alerts[0].severity == SEVERITY_CRITICAL


def test_evaluate_budget_below_first_level_silent():
    assert _no_alerts(budget_cny=100.0, budget_spent=49.9,
                      budget_levels=(50, 80)) == []


def test_evaluate_budget_disabled_by_zero_budget_or_empty_levels():
    assert _no_alerts(budget_cny=0.0, budget_spent=999.0,
                      budget_levels=(50,)) == []
    assert _no_alerts(budget_cny=100.0, budget_spent=999.0,
                      budget_levels=()) == []
    assert _no_alerts(budget_cny=100.0, budget_spent=0.0,
                      budget_levels=(50,)) == []
