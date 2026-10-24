"""分组分位（P50/P90/P95/P99）与失败归因查询。

分位只从明细表算（小时汇总只有 sum/count），口径为 nearest-rank：
第 p 百分位 = 第 ⌈p·n/100⌉ 小的值，n 为该组**有值**样本数（NULL 不占名次）。
"""

from __future__ import annotations

import time

from src.stats.query import PERCENTILES


def _record_latencies(collector, values, *, provider="trae", model="m", username="u",
                      now: int | None = None):
    """写一批成功请求：latency 取 `values`，ttfb 取 1/10（两列独立排名）。"""
    for index, value in enumerate(values):
        collector.record(username=username, provider=provider, model=model, ok=True,
                         latency_ms=value, ttfb_ms=value // 10,
                         now=None if now is None else now + index)


def test_group_rows_carry_every_percentile(stats):
    """一次查询下发全部四档分位（latency 与 ttfb 各一列）。"""
    collector, query = stats
    _record_latencies(collector, range(100, 2100, 100))       # n=20
    row = query.by_provider(username="u")[0]
    # n=20：P50 第 10 小、P90 第 18 小、P95 第 19 小、P99 第 20 小
    assert row["p50_latency_ms"] == 1000 and row["p50_ttfb_ms"] == 100
    assert row["p90_latency_ms"] == 1800 and row["p90_ttfb_ms"] == 180
    assert row["p95_latency_ms"] == 1900 and row["p95_ttfb_ms"] == 190
    assert row["p99_latency_ms"] == 2000 and row["p99_ttfb_ms"] == 200
    # 每档都在：加档只改 PERCENTILES，行结构自动跟上
    for pct in PERCENTILES:
        assert f"p{pct}_latency_ms" in row and f"p{pct}_ttfb_ms" in row


def test_percentile_rank_matches_nearest_rank_formula(stats):
    """小样本（n=7）逐档核对整数公式 `⌈p·n/100⌉`，含向上取整边界。"""
    collector, query = stats
    values = [value * 100 for value in range(1, 8)]            # 100..700，n=7
    _record_latencies(collector, values)
    row = query.by_provider(username="u")[0]
    for pct in PERCENTILES:
        rank = -(-pct * len(values) // 100)                    # ⌈p·n/100⌉
        expected = values[rank - 1]
        assert row[f"p{pct}_latency_ms"] == expected, pct
        assert row[f"p{pct}_ttfb_ms"] == expected // 10, pct


def test_percentiles_null_when_group_has_no_success(stats):
    """无成功样本：四档全 NULL（前端显示 —），不拿 0 冒充。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=False, latency_ms=500)
    row = query.by_provider(username="u")[0]
    for pct in PERCENTILES:
        assert row[f"p{pct}_latency_ms"] is None
        assert row[f"p{pct}_ttfb_ms"] is None


def test_percentiles_available_on_every_dimension(stats):
    """四条分组路径（hourly × 3 + events）共用同一分位实现。"""
    collector, query = stats
    _record_latencies(collector, range(100, 1100, 100), now=1_700_000_000)
    for index in range(10):
        collector.record(username="u", provider="trae", model="m", ok=True,
                         credential_id="cred_a", latency_ms=(index + 1) * 100,
                         ttfb_ms=(index + 1) * 10, now=1_700_000_000 + index)
    for rows in (query.by_provider(username="u"), query.by_model(username="u"),
                 query.by_user(username="u"), query.by_credential(username="u")):
        assert rows[0]["p50_latency_ms"] == 500
        assert rows[0]["p99_latency_ms"] == 1000


def test_group_sort_by_each_percentile(stats):
    """p50/p90/p99 都是合法排序键（JOIN 列别名），与 p95 同一路径。"""
    collector, query = stats
    for provider, latency in (("trae", 100), ("codebuddy", 200), ("zen", 300)):
        collector.record(username="u", provider=provider, model="m", ok=True,
                         latency_ms=latency, ttfb_ms=latency // 10)
    for pct in PERCENTILES:
        for kind in ("latency_ms", "ttfb_ms"):
            rows = query.by_provider(username="u", sort=f"p{pct}_{kind}", order="desc")
            assert [row["provider"] for row in rows] == ["zen", "codebuddy", "trae"]


def test_events_filter_only_failed(stats):
    """`ok=0` 只看失败：失败归因卡片点行下钻时靠它对齐计数。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="ok-model", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="bad-model", ok=False,
                     error_type="rate_limit", now=1001)
    failed = query.events(username="u", ok=0)
    assert [row["model"] for row in failed["events"]] == ["bad-model"]
    # offset 模式同样生效（total 跟着过滤）
    assert query.events(username="u", ok=0, sort="ts", order="asc")["total"] == 1
    assert [row["model"] for row in query.events(username="u", ok=1)["events"]] == ["ok-model"]
    # 不传 = 不限制（历史行为）
    assert len(query.events(username="u")["events"]) == 2


def test_export_events_filter_only_failed(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="ok-model", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="bad-model", ok=False, now=1001)
    assert [row["model"] for row in query.export_events(username="u", ok=0)] == ["bad-model"]


def test_errors_groups_by_provider_model_and_error_type(stats):
    """按 (provider, model, error_type) 分组计数，并按失败数降序。"""
    collector, query = stats
    now = 1_700_000_000
    for offset in range(3):
        collector.record(username="u", provider="trae", model="glm", ok=False,
                         error_type="rate_limit", now=now + offset)
    collector.record(username="u", provider="trae", model="glm", ok=False,
                     error_type="upstream_error", now=now + 10)
    collector.record(username="v", provider="codebuddy", model="kimi", ok=False,
                     error_type="rate_limit", now=now + 20)
    # 成功请求不计入归因
    collector.record(username="u", provider="trae", model="glm", ok=True, now=now + 30)
    rows = query.errors(since=now - 100, now=now)
    assert rows == [
        {"provider": "trae", "model": "glm", "error_type": "rate_limit",
         "failures": 3, "rescued": 0, "last_ts": now + 2},
        {"provider": "codebuddy", "model": "kimi", "error_type": "rate_limit",
         "failures": 1, "rescued": 0, "last_ts": now + 20},
        {"provider": "trae", "model": "glm", "error_type": "upstream_error",
         "failures": 1, "rescued": 0, "last_ts": now + 10},
    ]


def test_errors_keeps_unknown_error_type_and_respects_scope(stats):
    """未上报错误类型（NULL）也是失败，单独成组；作用域按 username 收窄。"""
    collector, query = stats
    now = 1_700_000_000
    collector.record(username="u", provider="trae", model="glm", ok=False, now=now)
    collector.record(username="v", provider="trae", model="glm", ok=False,
                     error_type="upstream_error", now=now)
    all_rows = query.errors(since=now - 10, now=now)
    assert {(row["error_type"]) for row in all_rows} == {None, "upstream_error"}
    scoped = query.errors(username="u", since=now - 10, now=now)
    assert len(scoped) == 1 and scoped[0]["error_type"] is None


def test_errors_default_window_is_last_24h(stats):
    """不给 since 时默认最近 24 小时（`now` 可注入，测试不依赖真实时钟）。"""
    collector, query = stats
    now = 1_700_000_000
    collector.record(username="u", provider="trae", model="glm", ok=False,
                     error_type="rate_limit", now=now - 60)
    collector.record(username="u", provider="trae", model="glm", ok=False,
                     error_type="rate_limit", now=now - 2 * 86400)
    assert len(query.errors(now=now)) == 1
    # 不给 now：走真实时钟。另写一条「此刻」的失败，证明该分支按真实时间开窗
    collector.record(username="u", provider="codebuddy", model="kimi", ok=False,
                     error_type="rate_limit")
    assert len(query.errors()) == 1


def test_errors_limit_is_floored_at_one(stats):
    """limit=0 仍返回一组（`max(1, limit)`）：空响应会被前端读成「没有失败」。"""
    collector, query = stats
    now = 1_700_000_000
    collector.record(username="u", provider="trae", model="glm", ok=False,
                     error_type="rate_limit", now=now)
    collector.record(username="u", provider="codebuddy", model="kimi", ok=False,
                     error_type="rate_limit", now=now)
    assert len(query.errors(since=now - 10, limit=0, now=now)) == 1
    assert len(query.errors(since=now - 10, limit=1, now=now)) == 1
    assert len(query.errors(since=now - 10, limit=5, now=now)) == 2


def test_errors_empty(stats):
    _collector, query = stats
    assert query.errors(now=int(time.time())) == []


def test_errors_counts_rescued_attempts_by_request_id(stats):
    """同 request_id 另有成功行 = 换号救回，单列下发（failures − rescued 才是受损数）。

    换号重试是常态：`invalid_request` 每次上游拒绝都落一行，随后换渠道成功
    也留着这条失败。不区分会把「已救回的尝试」读成「用户没拿到结果」。
    """
    from src.request_context import set_request_id

    collector, query = stats
    now = 1_700_000_000
    try:
        # req-a：先失败后成功（救回）
        set_request_id("req-a")
        collector.record(username="u", provider="trae", model="glm", ok=False,
                         error_type="invalid_request", now=now)
        collector.record(username="u", provider="codebuddy", model="glm", ok=True,
                         now=now + 1)
        # req-b：只失败（未救回）
        set_request_id("req-b")
        collector.record(username="u", provider="trae", model="glm", ok=False,
                         error_type="invalid_request", now=now + 2)
        # req-c：同一渠道两次失败 + 一次成功，两行都算救回
        set_request_id("req-c")
        collector.record(username="u", provider="trae", model="glm", ok=False,
                         error_type="invalid_request", now=now + 3)
        collector.record(username="u", provider="trae", model="glm", ok=False,
                         error_type="invalid_request", now=now + 4)
        collector.record(username="u", provider="trae", model="glm", ok=True, now=now + 5)
    finally:
        set_request_id(None)

    rows = query.errors(since=now - 10, now=now)
    assert len(rows) == 1
    row = rows[0]
    assert row["failures"] == 4          # req-a 1 + req-b 1 + req-c 2
    assert row["rescued"] == 3           # req-a 1 + req-c 2
    assert row["failures"] - row["rescued"] == 1   # 只有 req-b 真正受损


def test_errors_rescued_ignores_rows_without_request_id(stats):
    """老库历史行 request_id 为 NULL：无法关联成功行，恒算未救回（不误报救回）。"""
    collector, query = stats
    now = 1_700_000_000
    collector.record(username="u", provider="trae", model="glm", ok=False,
                     error_type="rate_limit", now=now)
    row = query.errors(since=now - 10, now=now)[0]
    assert row["failures"] == 1 and row["rescued"] == 0


def test_errors_rescued_is_scoped_to_same_user(stats):
    """request_id 来自入站 `X-Request-ID`（客户端可伪造）：跨用户不能互相「救回」。

    只按 ID 关联时，bob 的成功行会把 alice 的失败标成已救回（实测可复现），
    传了 `username=` 的查询同样会读到跨租户数据。
    """
    from src.request_context import set_request_id

    collector, query = stats
    now = 1_700_000_000
    try:
        set_request_id("shared-id")
        collector.record(username="alice", provider="trae", model="glm", ok=False,
                         error_type="rate_limit", now=now)
        collector.record(username="bob", provider="codebuddy", model="glm", ok=True,
                         now=now + 1)
    finally:
        set_request_id(None)

    alice = query.errors(username="alice", since=now - 10, now=now)
    assert alice[0]["failures"] == 1 and alice[0]["rescued"] == 0
    everyone = query.errors(since=now - 10, now=now)
    assert sum(row["rescued"] for row in everyone) == 0


def test_errors_rescued_sees_success_after_window_end(stats):
    """救援判定不叠加时间窗：成功行可能落在窗口右界之后（同一请求的重试）。"""
    from src.request_context import set_request_id

    collector, query = stats
    now = 1_700_000_000
    try:
        set_request_id("req-late")
        collector.record(username="u", provider="trae", model="glm", ok=False,
                         error_type="invalid_request", now=now)
        collector.record(username="u", provider="codebuddy", model="glm", ok=True,
                         now=now + 500)
    finally:
        set_request_id(None)

    # 窗口 [now-10, now+100)：成功行在窗口外，仍应算救回
    rows = query.errors(since=now - 10, until=now + 100, now=now)
    assert rows[0]["failures"] == 1 and rows[0]["rescued"] == 1
