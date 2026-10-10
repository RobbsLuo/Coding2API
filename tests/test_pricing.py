"""成本估算（OpenRouter 刊例价）与统计成本列。

覆盖：单请求估算（缓存夹取、缺 token / 缺模型降级、候选键匹配）、汇率换算
（非法汇率兜底）、采集器写入与查询聚合（overview / by_provider / timeline /
events）、汇率热更。

价表本身与「模型列表」目录的构建 / 落盘 / 抓取在 `src/benchmarks.py`（单一
OpenRouter 数据源，与能力分同一次抓取），由 `tests/test_benchmarks.py` 覆盖；
这里只留纯计算与统计侧。
"""

from __future__ import annotations

import time

import pytest

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.pricing import estimate_cost_usd, to_cny
from src.stats.collector import StatsCollector
from src.stats.query import StatsQuery

# ------------------------------------------------------------------ 估算与换算


def test_estimate_cost_usd_splits_cached_and_uncached():
    table = {"m": (1.0, 2.0, 0.1)}       # USD / 百万 token
    cost = estimate_cost_usd(table, "m", input_tokens=1_000_000,
                             output_tokens=1_000_000, cached_tokens=400_000)
    # 未命中 60 万 * 1 + 命中 40 万 * 0.1 + 输出 100 万 * 2 = 0.6+0.04+2 = 2.64
    assert cost == pytest.approx(2.64)


def test_estimate_cost_usd_clamps_cached_to_input():
    table = {"m": (1.0, 0.0, 0.0)}
    # 上报 100 万缓存但只有 10 万输入：未命中夹到 0，不出现负的未命中
    cost = estimate_cost_usd(table, "m", input_tokens=100_000, output_tokens=0,
                             cached_tokens=1_000_000)
    assert cost == pytest.approx(100_000 * 0.0 / 1_000_000)  # 10 万全按缓存价 0


def test_estimate_cost_usd_model_case_insensitive_and_stripped():
    table = {"glm-5.2": (1.0, 0.0, 1.0)}
    assert estimate_cost_usd(table, " GLM-5.2 ", input_tokens=1_000_000,
                             output_tokens=0, cached_tokens=0) == pytest.approx(1.0)


def test_estimate_cost_usd_unknown_returns_none():
    table = {"m": (1.0, 2.0, 0.1)}
    assert estimate_cost_usd(table, "other", input_tokens=10, output_tokens=1,
                             cached_tokens=None) is None
    assert estimate_cost_usd(None, "m", input_tokens=10, output_tokens=1,
                             cached_tokens=None) is None
    assert estimate_cost_usd(table, None, input_tokens=10, output_tokens=1,
                             cached_tokens=None) is None
    assert estimate_cost_usd(table, "", input_tokens=10, output_tokens=1,
                             cached_tokens=None) is None


def test_estimate_cost_usd_without_input_tokens_is_none():
    """拿不到输入 token 就无法估算——不能拿 0 冒充「免费」。"""
    table = {"m": (1.0, 2.0, 0.1)}
    assert estimate_cost_usd(table, "m", input_tokens=None, output_tokens=5,
                             cached_tokens=None) is None


def test_estimate_cost_usd_clamps_negative_counts():
    table = {"m": (1.0, 2.0, 1.0)}
    assert estimate_cost_usd(table, "m", input_tokens=-5, output_tokens=-5,
                             cached_tokens=-5) == 0.0


def test_estimate_cost_usd_missing_output_uses_zero():
    table = {"m": (1.0, 2.0, 1.0)}
    assert estimate_cost_usd(table, "m", input_tokens=1_000_000, output_tokens=None,
                             cached_tokens=None) == pytest.approx(1.0)


def test_to_cny_falls_back_on_invalid_rate():
    assert to_cny(1.0, 6.7) == pytest.approx(6.7)
    assert to_cny(1.0, 0) == 1.0          # 非正 → 1:1
    assert to_cny(1.0, -3) == 1.0
    assert to_cny(1.0, "abc") == 1.0      # 非数值 → 1:1
    assert to_cny(1.0, float("nan")) == 1.0


# ------------------------------------------------------------------ 采集与查询


@pytest.fixture()
def collector_query(tmp_path):
    db = Database(tmp_path / "p.sqlite3")
    apply_schema(db.connect())
    prices = {"glm-5.2": (1.0, 2.0, 0.1)}
    collector = StatsCollector(db, prices=lambda: prices, usd_cny_rate=lambda: 6.7)
    yield collector, StatsQuery(db), db
    db.close()


def test_collector_writes_cost_at_record_time(collector_query):
    collector, query, db = collector_query
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=0)
    row = db.connect().execute(
        "SELECT cost_usd, cost_cny FROM usage_events").fetchone()
    assert row["cost_usd"] == pytest.approx(3.0)          # 1*1 + 1*2
    assert row["cost_cny"] == pytest.approx(3.0 * 6.7)


def test_collector_cost_none_when_model_unmatched(collector_query):
    collector, _query, db = collector_query
    collector.record(username="u", provider="trae", model="unknown-model", ok=True,
                     input_tokens=10, output_tokens=20)
    row = db.connect().execute(
        "SELECT cost_usd, cost_cny FROM usage_events").fetchone()
    assert row["cost_usd"] is None and row["cost_cny"] is None


def test_collector_without_prices_never_sets_cost(tmp_path):
    db = Database(tmp_path / "p2.sqlite3")
    apply_schema(db.connect())
    collector = StatsCollector(db)               # 老装配：不估算成本
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=100, output_tokens=100)
    row = db.connect().execute(
        "SELECT cost_usd FROM usage_events").fetchone()
    assert row["cost_usd"] is None
    db.close()


def test_collector_cost_failure_is_swallowed(tmp_path, caplog):
    """价表取值器抛异常只降级为无成本，绝不影响统计写入。"""
    db = Database(tmp_path / "p3.sqlite3")
    apply_schema(db.connect())

    def boom():
        raise RuntimeError("price source down")

    collector = StatsCollector(db, prices=boom, usd_cny_rate=lambda: 6.7)
    with caplog.at_level("WARNING"):
        collector.record(username="u", provider="trae", model="m", ok=True,
                         input_tokens=10, output_tokens=10)
    row = db.connect().execute(
        "SELECT cost_usd FROM usage_events").fetchone()
    assert row["cost_usd"] is None
    assert any("成本估算失败" in r.getMessage() for r in caplog.records)
    db.close()


def test_query_overview_and_provider_cost(collector_query):
    collector, query, _db = collector_query
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=1_000_000, output_tokens=0)
    collector.record(username="u", provider="trae", model="unpriced", ok=True,
                     input_tokens=1_000_000, output_tokens=0)
    overview = query.overview(username="u")
    assert overview["cost_usd"] == pytest.approx(1.0)     # 仅可定价那条计入
    assert overview["cost_cny"] == pytest.approx(6.7)
    rows = {row["provider"]: row for row in query.by_provider(username="u")}
    assert rows["trae"]["cost_usd"] == pytest.approx(1.0)


def test_query_cost_none_when_nothing_priced(collector_query):
    collector, query, _db = collector_query
    collector.record(username="u", provider="trae", model="unpriced", ok=True,
                     input_tokens=1_000_000, output_tokens=0)
    assert query.overview(username="u")["cost_usd"] is None
    assert query.overview(username="u")["cost_cny"] is None
    assert query.by_provider(username="u")[0]["cost_usd"] is None


def test_query_events_carry_cost(collector_query):
    collector, query, _db = collector_query
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=1_000_000, output_tokens=0)
    event = query.events(username="u")["events"][0]
    assert event["cost_usd"] == pytest.approx(1.0)
    assert event["cost_cny"] == pytest.approx(6.7)


def test_query_timeline_cost_metric_none_for_unpriced(collector_query):
    collector, query, _db = collector_query
    hour = 1_700_000_000
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=1_000_000, output_tokens=0, now=hour)
    collector.record(username="u", provider="trae", model="unpriced", ok=True,
                     input_tokens=1_000_000, output_tokens=0, now=hour)
    points = query.timeline(username="u", metric="cost")
    assert points[0]["trae"] == pytest.approx(6.7)        # 仅可定价部分


def test_query_timeline_cost_none_when_hour_unpriced(collector_query):
    collector, query, _db = collector_query
    hour = 1_700_000_000
    collector.record(username="u", provider="trae", model="unpriced", ok=True,
                     input_tokens=100, output_tokens=0, now=hour)
    assert query.timeline(username="u", metric="cost")[0]["trae"] is None


def test_query_model_timeline_cost_metric(collector_query):
    collector, query, _db = collector_query
    hour = 1_700_000_000
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=1_000_000, output_tokens=0, now=hour)
    result = query.model_timeline(username="u", metric="cost")
    assert result["models"] == ["glm-5.2"]
    assert result["points"][0]["glm-5.2"] == pytest.approx(6.7)


def test_query_model_timeline_cost_unpriced_is_none(collector_query):
    collector, query, _db = collector_query
    hour = 1_700_000_000
    collector.record(username="u", provider="trae", model="unpriced", ok=True,
                     input_tokens=100, output_tokens=0, now=hour)
    result = query.model_timeline(username="u", metric="cost")
    assert result["points"][0]["unpriced"] is None


def test_collector_rollup_preserves_cost(collector_query):
    collector, query, db = collector_query
    now = int(time.time())
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=1_000_000, output_tokens=0, now=now)
    collector.record(username="u", provider="trae", model="unpriced", ok=True,
                     input_tokens=1_000_000, output_tokens=0, now=now)
    collector.rollup_hourly(since=now - 10)
    row = db.connect().execute(
        "SELECT cost_usd_sum, cost_cny_sum, cost_known FROM usage_hourly").fetchone()
    assert row["cost_usd_sum"] == pytest.approx(1.0)
    assert row["cost_cny_sum"] == pytest.approx(6.7)
    assert row["cost_known"] == 1

