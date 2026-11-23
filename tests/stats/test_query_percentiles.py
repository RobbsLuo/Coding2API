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


# --------------------------------------------------- rescued 口径判据（P2-16）

#: `errors()` 的 rescued 口径参照实现（逐行相关子查询）。生产查询必须与它
#: 逐字段等价，否则「救回数」会悄悄漂移。任何改写都要同时复核这几条用例。
#:
#: P2-16 原本要求把这段改写成「先聚合成功集再 LEFT JOIN」，但实测该写法在
#: 所有场景下都更慢（见 `errors()` docstring 与 `tests/stats/test_query_percentiles.py`
#: 里的说明）：救援判定**不叠加时间窗**，聚合侧因此必须物化全表成功集
#: （90 天可达 85 万行），而逐行 `EXISTS` 是走索引的点查。
_REFERENCE_ERRORS_SQL = """
    SELECT e.provider, e.model, e.error_type, COUNT(*) AS failures,
           SUM(CASE WHEN EXISTS (
               SELECT 1 FROM usage_events s
               WHERE s.request_id = e.request_id
                 AND s.username = e.username AND s.ok = 1)
           THEN 1 ELSE 0 END) AS rescued,
           MAX(e.ts) AS last_ts
    FROM usage_events e
    WHERE e.ts >= ? AND e.ts < ? AND e.ok = 0
    GROUP BY e.provider, e.model, e.error_type
    ORDER BY failures DESC, e.provider, e.model, e.error_type
    LIMIT ?
"""


def _reference_errors(db, *, since, until, limit=50):
    rows = db.connect().execute(_REFERENCE_ERRORS_SQL, (since, until, limit)).fetchall()
    return [{"provider": row["provider"], "model": row["model"],
             "error_type": row["error_type"], "failures": int(row["failures"]),
             "rescued": int(row["rescued"]), "last_ts": int(row["last_ts"])}
            for row in rows]


def _seed_rescue_edge_cases(collector, *, base=1_700_000_000):
    """把「救回」判定的所有边界一次性铺进库，供新旧口径对拍。

    覆盖：窗口内/外成功、同 request_id 多行成功（不能放大 failures）、
    request_id 为 NULL、跨用户同 ID、窗口左界之前成功、窗口右界之后成功。
    """
    from src.request_context import set_request_id

    rows = [
        # (request_id, username, provider, ok, ts)
        ("r-dup", "u", "trae", 0, base + 1),        # 3 行成功（重复）仍只算 1 次救回
        ("r-dup", "u", "codebuddy", 1, base + 2),
        ("r-dup", "u", "trae", 1, base + 3),
        ("r-dup", "u", "trae", 1, base + 4),
        (None, "u", "trae", 0, base + 5),            # 无 request_id：不可救回
        (None, "u", "trae", 1, base + 6),
        ("r-cross", "alice", "trae", 0, base + 7),   # bob 的成功不能救 alice
        ("r-cross", "bob", "codebuddy", 1, base + 8),
        ("r-early", "u", "trae", 0, base + 10),      # 成功在窗口左界之前
        ("r-early", "u", "codebuddy", 1, base - 100),
        ("r-late", "u", "trae", 0, base + 11),       # 成功在窗口右界之后
        ("r-late", "u", "codebuddy", 1, base + 500),
        ("r-two", "u", "trae", 0, base + 12),        # 同一请求两次失败都算救回
        ("r-two", "u", "trae", 0, base + 13),
        ("r-two", "u", "codebuddy", 1, base + 14),
    ]
    try:
        for request_id, username, provider, ok, ts in rows:
            set_request_id(request_id)
            collector.record(username=username, provider=provider, model="glm",
                             ok=bool(ok), error_type=None if ok else "invalid_request",
                             now=ts)
    finally:
        set_request_id(None)


def test_errors_rescued_matches_reference_exits_query(stats):
    """`errors()` 与参照实现**逐字段等价**（rescued 口径的判据）。"""
    collector, query = stats
    base = 1_700_000_000
    _seed_rescue_edge_cases(collector, base=base)

    since, until = base, base + 100
    new = query.errors(since=since, until=until, now=base)
    reference = _reference_errors(collector._db, since=since, until=until)
    assert new == reference
    # 边界本身的期望值（防止两边一起错）
    by_type = {row["error_type"]: row for row in new}
    assert by_type["invalid_request"]["failures"] == 7
    # 救回：r-dup 1 + r-early 1 + r-late 1 + r-two 2（bob/alice 跨用户不通、NULL 不算）
    assert by_type["invalid_request"]["rescued"] == 5


def test_errors_rescued_counts_each_failure_row_once_with_duplicate_successes(stats):
    """同一 (request_id, username) 有多行成功：`DISTINCT` 保证每行失败只算一次救回。"""
    from src.request_context import set_request_id

    collector, query = stats
    base = 1_700_000_000
    try:
        set_request_id("r")
        collector.record(username="u", provider="trae", model="glm", ok=False,
                         error_type="rate_limit", now=base)
        for _ in range(5):
            collector.record(username="u", provider="codebuddy", model="glm", ok=True,
                             now=base + 1)
    finally:
        set_request_id(None)
    row = query.errors(since=base - 10, now=base)[0]
    # 若成功集没去重，`LEFT JOIN` 会把 failures 放大成 5
    assert row["failures"] == 1 and row["rescued"] == 1


def test_errors_rescued_sees_success_before_window_start(stats):
    """成功行也可能在窗口**左界之前**（同 request 的早先尝试）：同样算救回。

    新写法若给成功集加上 `ts >= since` 就会漏掉这类行——这条用例专门钉住
    「救援判定不叠加时间窗」的两端。
    """
    from src.request_context import set_request_id

    collector, query = stats
    base = 1_700_000_000
    try:
        set_request_id("r-before")
        collector.record(username="u", provider="codebuddy", model="glm", ok=True,
                         now=base - 5_000)
        collector.record(username="u", provider="trae", model="glm", ok=False,
                         error_type="upstream_error", now=base)
    finally:
        set_request_id(None)
    rows = query.errors(since=base, until=base + 10, now=base)
    assert rows[0]["failures"] == 1 and rows[0]["rescued"] == 1
