"""统计采集、小时汇总与保留期回收。"""

from __future__ import annotations

import time

import pytest

from src.tasks.retention import RetentionTask


def test_stats_records_and_redacts(stats):
    collector, query = stats
    collector.record(username="alice", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=10, output_tokens=20, latency_ms=100, ttfb_ms=30)
    collector.record(username="alice", provider="codebuddy", model="glm-5.2", ok=False,
                     error_type="upstream_error", credit=0.5)
    overview = query.overview(username="alice")
    assert overview["requests"] == 2 and overview["ok_count"] == 1
    assert overview["success_rate"] == 0.5
    assert overview["input_tokens"] == 10
    assert overview["credit"] == 0.5
    assert overview["avg_latency_ms"] == 100
    assert overview["avg_ttfb_ms"] == 30
    by_provider = query.by_provider(username="alice")
    assert [row["provider"] for row in by_provider] == ["codebuddy", "trae"]

def test_stats_credit_is_null_when_never_known(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True)
    assert query.overview(username="u")["credit"] is None
    assert query.overview(username="u")["credit_estimated"] is False

def test_stats_estimated_credit_flag_flows_through(stats):
    """推算积分：明细标 1、总览/按渠道标 ≈、events 带标记；无 credit 的推算标记被丢弃。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="deepseek-v4.1-flash", ok=True,
                     input_tokens=1000, output_tokens=200, credit=0.05,
                     credit_estimated=True)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     input_tokens=10, output_tokens=5, credit=1.5)
    # credit 缺失时推算标记无意义，落库应为 0
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     credit_estimated=True)

    overview = query.overview(username="u")
    assert overview["credit"] == 1.55
    assert overview["credit_estimated"] is True

    rows = {row["provider"]: row for row in query.by_provider(username="u")}
    assert rows["trae"]["credit_estimated"] is True
    assert rows["codebuddy"]["credit_estimated"] is False

    events = query.events(username="u")["events"]
    by_model = {e["model"]: e for e in events}
    assert by_model["deepseek-v4.1-flash"]["credit_estimated"] == 1
    assert by_model["glm-5.2"]["credit_estimated"] == 0
    # 只有真值 credit 的渠道不标推算
    collector.record(username="v", provider="codebuddy", model="m", ok=True, credit=2.0)
    assert query.overview(username="v")["credit_estimated"] is False
    assert query.by_provider(username="v")[0]["credit_estimated"] is False

def test_stats_rollup_preserves_estimated_known(stats):
    """retention rollup 重算小时汇总时保留 credit_estimated_known。"""
    collector, query = stats
    now = int(time.time())
    collector.record(username="u", provider="trae", model="m", ok=True, credit=0.05,
                     credit_estimated=True, now=now)
    collector.record(username="u", provider="trae", model="m", ok=True, credit=0.10,
                     credit_estimated=True, now=now)
    collector.record(username="u", provider="trae", model="m", ok=True, credit=0.20,
                     now=now)
    collector.rollup_hourly(since=now - 10)
    row = query._db.connect().execute(
        "SELECT credit_estimated_known, credit_known FROM usage_hourly").fetchone()
    assert row["credit_estimated_known"] == 2
    assert row["credit_known"] == 3

def test_stats_normalizes_model_and_error_type(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="x" * 100, ok=False,
                     error_type="not-a-controlled-value", credit=-5)
    rows = query.by_provider(username="u")
    assert rows[0]["requests"] == 1
    row = query._db.connect().execute(
        "SELECT model, error_type, credit FROM usage_events").fetchone()
    assert row["model"] == "unknown" and row["error_type"] is None and row["credit"] is None

@pytest.mark.parametrize("model", [None, "", "has space", "bad\ncontrol", 5])
def test_safe_model_rejects_invalid(stats, model):
    collector, query = stats
    collector.record(username="u", provider="trae", model=model, ok=True)
    assert query._db.connect().execute(
        "SELECT model FROM usage_events").fetchone()["model"] == "unknown"

def test_purge_keeps_partially_populated_hour_intact(stats):
    """边界小时不得被删一半：否则下一轮 rollup 把汇总行覆盖成缩小值。

    rollup 对整行是 REPLACE 语义，purge 只删已完全过期的小时（切点向上
    对齐到小时边界），保证「仍有明细的小时保有全部明细」。
    """
    collector, _query = stats
    now = int(time.time())
    hour = (now // 3600) * 3600
    # 同一小时内两条明细：早的已过期（200 天前同一小时），晚的还在
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, now=hour - 200 * 86400 + 1)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=10, now=hour + 1)
    RetentionTask(collector, retention_days=90).run_once()
    row = collector._db.connect().execute(
        "SELECT SUM(input_tokens) AS t, SUM(requests) AS r FROM usage_hourly").fetchone()
    assert (row["t"], row["r"]) == (110, 2)      # 两条都在，未被截断

def test_stats_retention_and_rollup(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, input_tokens=5,
                     latency_ms=10, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, input_tokens=5,
                     latency_ms=20, now=2000)
    task = RetentionTask(collector, retention_days=90)
    first = task.run_once()
    assert first["rolled_up"] >= 1
    rows = query._db.connect().execute(
        "SELECT requests, input_tokens FROM usage_hourly").fetchall()
    assert sum(r["requests"] for r in rows) == 2
    # 清理：把 now 推到 91 天之后
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    assert collector.purge_expired(90, now=1000 + 91 * 86400) == 1
    # 小时汇总永久保留
    assert query._db.connect().execute(
        "SELECT COUNT(*) AS c FROM usage_hourly").fetchone()["c"] >= 1

def test_stats_rollup_is_idempotent(stats):
    collector, _query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=10_000)
    collector.rollup_hourly()
    collector.rollup_hourly()
    total = collector._db.connect().execute(
        "SELECT SUM(requests) AS s FROM usage_hourly").fetchone()["s"]
    assert total == 1

def test_stats_record_handles_negative_and_bool_tokens(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=-1, output_tokens=True, reasoning_tokens=None,
                     latency_ms=-5, ttfb_ms=True, credit=-1.0)
    row = query._db.connect().execute(
        "SELECT input_tokens, output_tokens, latency_ms, credit FROM usage_events").fetchone()
    assert row["input_tokens"] is None and row["output_tokens"] is None
    assert row["latency_ms"] is None and row["credit"] is None

def test_stats_record_rejects_non_finite_credit(stats):
    """L1：NaN/Inf 不进聚合（污染不可回滚），落库为 NULL。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credit=float("inf"))
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credit=float("nan"))
    rows = query._db.connect().execute(
        "SELECT credit FROM usage_events").fetchall()
    assert [r["credit"] for r in rows] == [None, None]

def test_stats_record_defaults_username_when_blank(stats):
    collector, query = stats
    collector.record(username="", provider="trae", model="m", ok=True)
    assert query.by_provider()[0]["requests"] == 1


def test_metrics_provider_model_and_tokens_aggregate_hourly(stats):
    """`/metrics` 的两个小时汇总查询：按 (渠道, 模型) / 渠道聚合、稳定排序。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=10, output_tokens=20, reasoning_tokens=1, cached_tokens=3)
    collector.record(username="u", provider="trae", model="m", ok=False)
    collector.record(username="v", provider="codebuddy", model="m", ok=True,
                     input_tokens=7)
    assert query.metrics_provider_model() == [
        ("codebuddy", "m", 1, 1), ("trae", "m", 2, 1)]
    assert query.metrics_provider_tokens() == [
        ("codebuddy", 7, 0, 0, 0), ("trae", 10, 20, 1, 3)]


def test_error_totals_counts_only_controlled_types(stats):
    """未知 error_type 归一为 None → 不计入 /metrics 的失败计数。"""
    collector, _query = stats
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="rate_limit")
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="not-controlled")
    collector.record(username="u", provider="codebuddy", model="m", ok=True)
    collector.record(username="u", provider="codebuddy", model="m", ok=False,
                     error_type="upstream_error")
    # 稳定排序：按 (provider, kind)
    assert collector.error_totals() == [("codebuddy", "upstream_error", 1),
                                        ("trae", "rate_limit", 1)]


# ------------------------------------------------------------ 实时流回调


def test_record_publishes_redacted_row_after_commit(stats):
    """落库后广播：拿到 rowid 与已脱敏字段（不含提示词/请求头）。"""
    _collector, _query = stats
    from src.stats.collector import StatsCollector

    published: list[dict] = []
    collector = StatsCollector(stats[0]._db, on_record=published.append)
    collector.record(username="alice", provider="trae", model="glm-5.2", ok=True,
                     credential_id="cred-1", input_tokens=10, output_tokens=20,
                     latency_ms=100, now=1234)
    assert len(published) == 1
    row = published[0]
    assert row["rowid"] > 0
    assert row["ts"] == 1234 and row["username"] == "alice"
    assert row["provider"] == "trae" and row["model"] == "glm-5.2"
    assert row["credential_id"] == "cred-1" and row["ok"] == 1
    assert row["input_tokens"] == 10 and row["output_tokens"] == 20
    assert row["latency_ms"] == 100
    # 广播的字段集与 /api/stats/events 一致：不含 credential_name（端点按角色补）
    assert "credential_name" not in row
    assert "prompt" not in row and "headers" not in row
    # 派生速度列也在广播里：本行未带首字数据 → 不可测
    assert row["tps"] is None and row["tps_visible"] is None


def test_record_publishes_per_request_speed(stats):
    """广播行自带逐请求速度（实时表那一列不查库）：规则与明细端点一致。"""
    from src.stats.collector import StatsCollector

    published: list[dict] = []
    collector = StatsCollector(stats[0]._db, on_record=published.append)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=1000, reasoning_tokens=750,
                     latency_ms=6000, ttfb_ms=1000)      # 生成 5.0s：总 200 / 正文 50
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="upstream_error", output_tokens=900,
                     latency_ms=3000, ttfb_ms=1000)      # 失败请求不算
    assert (published[0]["tps"], published[0]["tps_visible"]) == (200.0, 50.0)
    assert (published[1]["tps"], published[1]["tps_visible"]) == (None, None)


def test_record_without_callback_is_silent(stats):
    """未装配实时流（脚本 / 老装配）：写入照常，不报错。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True)
    assert query.overview(username="u")["requests"] == 1


def test_record_swallows_publish_failure(stats):
    """订阅者出问题不能让统计写入（进而让聊天响应）失败。"""
    from src.stats.collector import StatsCollector

    def boom(_row):
        raise RuntimeError("subscriber exploded")

    collector = StatsCollector(stats[0]._db, on_record=boom)
    collector.record(username="u", provider="trae", model="m", ok=True)
    assert stats[1].overview(username="u")["requests"] == 1


def test_record_publishes_normalized_model_and_error_type(stats):
    """广播的是**归一后**的值（与明细表一致），不是调用方原始入参。"""
    from src.stats.collector import StatsCollector

    published: list[dict] = []
    collector = StatsCollector(stats[0]._db, on_record=published.append)
    collector.record(username="u", provider="trae", model="x" * 100, ok=False,
                     error_type="not-controlled")
    assert published[0]["model"] == "unknown"
    assert published[0]["error_type"] is None


def test_window_key_usage_groups_by_key_and_orders_by_tokens(stats):
    """按 Key 告警的数据源：只统计带 Key 的明细，token 降序。"""
    from src.request_context import set_api_key_id

    collector, _query = stats
    for key_id, tokens in (("k1", 10), ("k1", 30), ("k2", 999), (None, 5000)):
        set_api_key_id(key_id)
        try:
            collector.record(username=f"u-{key_id}", provider="trae", model="m",
                             ok=True, input_tokens=tokens, output_tokens=0)
        finally:
            set_api_key_id(None)
    rows = collector.window_key_usage(since=0)
    assert [row["api_key_id"] for row in rows] == ["k2", "k1"]
    assert rows[1]["tokens"] == 40
    assert rows[1]["requests"] == 2
    # 窗口外不返回（since 取未来）
    assert collector.window_key_usage(since=int(time.time()) + 3600) == []


def test_window_cost_sums_known_hours(stats):
    """预算告警的数据源：窗口内成本求和 + 可定价小时数。"""
    collector, _query = stats
    collector.record(username="u", provider="trae", model="deepseek-v4.1-flash",
                     ok=True, input_tokens=100, output_tokens=10)
    collector.rollup_hourly()
    spent, known, hours = collector.window_cost(since=0)
    assert spent >= 0
    assert hours == 1
    assert known in (0, 1)          # 价表未装配时 cost 为 NULL → known=0
    # 窗口外（未来）→ 全 0，不是 None
    assert collector.window_cost(since=int(time.time()) + 3600) == (0.0, 0, 0)
