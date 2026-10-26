"""统计查询（overview / by-* / timeline / model-timeline / events）。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from src.stats.collector import StatsCollector
from src.stats.query import MAX_DENSE_HOURS, StatsQuery
from src.tasks.retention import RetentionTask
from tests.helpers.constants import SECRET


def test_stats_overview_global_for_admin(stats):
    collector, query = stats
    collector.record(username="alice", provider="trae", model="m", ok=True)
    collector.record(username="bob", provider="trae", model="m", ok=True)
    assert query.overview()["requests"] == 2
    assert query.overview(username="alice")["requests"] == 1

def test_stats_by_model_aggregates(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=100, output_tokens=50, credit=1.0)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=False)
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True,
                     input_tokens=200, output_tokens=100, credit=2.0)
    rows = query.by_model(username="u")
    assert [row["model"] for row in rows] == ["glm-5.2", "kimi-k3"]
    glm = rows[0]
    assert glm["requests"] == 2 and glm["ok_count"] == 1
    assert glm["input_tokens"] == 100 and glm["output_tokens"] == 50
    assert glm["credit"] == 1.0
    kimi = rows[1]
    assert kimi["requests"] == 1 and kimi["ok_count"] == 1
    assert kimi["input_tokens"] == 200 and kimi["output_tokens"] == 100
    assert kimi["credit"] == 2.0

def test_stats_by_model_empty(stats):
    _collector, query = stats
    assert query.by_model() == []
    assert query.by_model(username="nobody") == []


def test_group_methods_reject_non_whitelisted_key(stats):
    """分组键是 SQL 片段拼接位：白名单外必须显式报错，不能静默拼进 SQL。

    公开入口（by_provider/by_model/by_user/by_credential）只传白名单常量，
    因此这条防御只在私有方法上可触达——但它是 SQL 注入的唯一闸门，必须守住。
    """
    import pytest

    from src.stats.query import _percentile_join

    _collector, query = stats
    for method in (query._group_hourly, query._group_events, _percentile_join):
        with pytest.raises(ValueError, match="非法分组键"):
            method("1=1; DROP TABLE usage_events")

def test_stats_by_user_aggregates(stats):
    collector, query = stats
    collector.record(username="alice", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=100, output_tokens=50, credit=1.0)
    collector.record(username="alice", provider="codebuddy", model="kimi-k3", ok=False)
    collector.record(username="bob", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=10, output_tokens=5, credit=0.5)
    rows = {row["username"]: row for row in query.by_user()}
    assert set(rows) == {"alice", "bob"}
    alice = rows["alice"]
    assert alice["requests"] == 2 and alice["ok_count"] == 1
    assert alice["input_tokens"] == 100 and alice["output_tokens"] == 50
    assert alice["credit"] == 1.0
    assert rows["bob"]["requests"] == 1 and rows["bob"]["ok_count"] == 1
    # 用户维度同样可按人过滤（viewer 只能看自己）
    assert [row["username"] for row in query.by_user(username="bob")] == ["bob"]
    assert query.by_user(username="nobody") == []

def test_stats_by_credential_aggregates(stats):
    """凭证维度读明细表（小时汇总主键里没有 credential_id）。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     credential_id="cred_a", input_tokens=100, output_tokens=50,
                     cached_tokens=30, credit=1.0)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     credential_id="cred_a", input_tokens=10, output_tokens=5,
                     cached_tokens=0, credit=0.5)
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True,
                     credential_id="cred_b", input_tokens=20, output_tokens=10,
                     credit=2.0)
    # 无凭证的请求（预热失败等）不可归属，不进分组
    collector.record(username="u", provider="trae", model="glm-5.2", ok=False,
                     error_type="upstream_error")

    rows = {row["credential_id"]: row for row in query.by_credential()}
    assert set(rows) == {"cred_a", "cred_b"}
    cred_a = rows["cred_a"]
    assert cred_a["requests"] == 2 and cred_a["ok_count"] == 2
    assert cred_a["input_tokens"] == 110 and cred_a["output_tokens"] == 55
    assert cred_a["cached_tokens"] == 30
    assert cred_a["credit"] == 1.5
    assert rows["cred_b"]["requests"] == 1
    # 无缓存上报时 cached_tokens 回 None（不把 0 当成真值）
    assert rows["cred_b"]["cached_tokens"] is None
    # 按用户/时间过滤同样生效（_where 拼的子句与 hourly 维度共用）
    assert query.by_credential(username="nobody") == []

def test_stats_by_credential_reports_name_and_provider(repo):
    """凭证分组随行下发渠道（画 icon）与昵称（与明细同口径显示凭证名）。

    昵称取自共享池，只对 operator 返回；渠道优先取凭证当前所属，
    凭证已删除则回退明细里的渠道。
    """
    credentials, db = repo
    collector, query = StatsCollector(db), StatsQuery(db)
    keep_id = credentials.add(provider="trae", credential_data={"accessToken": "t"},
                              nickname="主号")
    ghost_id = credentials.add(provider="trae", credential_data={"accessToken": "g"},
                               nickname="将删")
    blank_id = credentials.add(provider="codearts", credential_data={}, nickname="")
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id=keep_id)
    # 已删除凭证的明细跨渠道（凭证改过 provider）→ 回退取字典序最大的那个
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id=ghost_id)
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     credential_id=ghost_id)
    collector.record(username="u", provider="codearts", model="m", ok=True,
                     credential_id=blank_id)
    assert credentials.delete(ghost_id) is True

    rows = {row["credential_id"]: row for row in
            query.by_credential(include_credential_name=True)}
    assert rows[keep_id]["credential_name"] == "主号"
    assert rows[keep_id]["provider"] == "trae"
    assert rows[ghost_id]["credential_name"] is None        # 已删除 → 回退
    assert rows[ghost_id]["provider"] == "trae"             # 回退明细渠道
    assert rows[blank_id]["credential_name"] is None        # 空昵称 → 同回退
    assert rows[blank_id]["provider"] == "codearts"

    # 默认（viewer 口径）不下发共享池昵称，渠道照常给（前端 icon 依赖）
    default_rows = {row["credential_id"]: row for row in query.by_credential()}
    assert default_rows[keep_id]["credential_name"] is None
    assert default_rows[keep_id]["provider"] == "trae"

def test_stats_by_credential_endpoint_gates_credential_name(admin_client, tmp_path):
    """by-credential 端点：operator 拿昵称；viewer 不下发昵称但仍拿渠道。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="trae",
                                              credential_data={"accessToken": "t"},
                                              nickname="主号")
    app.state.stats_collector.record(username="root", provider="trae", model="m", ok=True,
                                     credential_id=credential_id)
    row = client.get("/api/stats/by-credential").json()["credentials"][0]
    assert row["credential_name"] == "主号" and row["provider"] == "trae"

    # 非 operator 身份：共享池昵称不下发，渠道 icon 依赖的 provider 照常
    reader_settings = Settings(_env_file=None, APP_SECRET=SECRET,
                               DATA_DIR=str(tmp_path / "reader"))
    reader_app = build_app(reader_settings)
    reader_cred = reader_app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "b"}, nickname="他人号")
    reader_app.state.stats_collector.record(username="alice", provider="codebuddy",
                                            model="m", ok=True,
                                            credential_id=reader_cred)
    with TestClient(reader_app) as viewer:
        viewer.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        seen = viewer.get("/api/stats/by-credential").json()["credentials"][0]
    assert seen["credential_name"] is None and seen["provider"] == "codebuddy"

def test_stats_groups_report_cached_tokens_and_known(stats):
    """各维度的缓存命中：任一明细上报过才给值，否则 None。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, cached_tokens=40)
    for rows in (query.by_provider(), query.by_model(), query.by_user()):
        assert rows[0]["cached_tokens"] == 40
    collector.record(username="v", provider="trae", model="m", ok=True)
    assert query.by_provider(username="v")[0]["cached_tokens"] is None

def test_stats_overview_empty_and_filters(stats):
    _collector, query = stats
    empty = query.overview()
    assert empty["requests"] == 0 and empty["success_rate"] is None
    assert empty["avg_latency_ms"] is None
    assert query.by_provider() == []

def test_stats_groups_report_latency_percentiles(stats):
    """分组行带 P95 延迟（耗时/首字各算一条），与均值一起下发。

    分位值只能从明细算（小时汇总只有 sum/count），故只覆盖 90 天；
    口径与均值一致：只算成功请求。
    """
    collector, query = stats
    # 10 条成功：latency 100..1000、ttfb 10..100。P95 = 第 10 小 = 1000 / 100
    for value in range(100, 1100, 100):
        collector.record(username="u", provider="trae", model="m", ok=True,
                         latency_ms=value, ttfb_ms=value // 10)
    # 失败请求带超大延迟：不进分位（否则尾延迟被失败请求顶爆）
    collector.record(username="u", provider="trae", model="m", ok=False, latency_ms=99999,
                     ttfb_ms=99999)
    row = query.by_provider(username="u")[0]
    assert row["p95_latency_ms"] == 1000 and row["p95_ttfb_ms"] == 100
    # 均值口径与 overview 一致（÷ 成功数，失败不计入）
    overview = query.overview(username="u")
    assert row["avg_latency_ms"] == overview["avg_latency_ms"] == 550
    assert row["avg_ttfb_ms"] == overview["avg_ttfb_ms"] == 55

def test_stats_group_percentiles_use_nearest_rank(stats):
    """nearest-rank：第 p 百分位 = 第 ⌈p·n⌉ 小的值（向上取整）。

    n=20 时 P95 取第 19 小而非第 20 小（⌈19⌉=19）；n=10 时两者都是第 10 小。
    用整数公式 `(95*n + 99)/100` 实现，避开浮点误差把名次算偏。
    """
    collector, query = stats
    for value in range(1, 21):                       # 1..20
        collector.record(username="u", provider="trae", model="m", ok=True,
                         latency_ms=value * 100)
    assert query.by_provider(username="u")[0]["p95_latency_ms"] == 1900
    for value in range(21, 31):                      # 追加 10 条 → n=30，第 29 小
        collector.record(username="u", provider="trae", model="m", ok=True,
                         latency_ms=value * 100)
    assert query.by_provider(username="u")[0]["p95_latency_ms"] == 2900

def test_stats_group_percentiles_rank_latency_and_ttfb_separately(stats):
    """ttfb 对非流式 / 未产出首字的成功请求为 NULL：两列各自独立排名。

    `COUNT(col)` 忽略 NULL，天然只数有值样本；NULL 行排在样本之后不占名次。
    """
    collector, query = stats
    # 3 条成功有 latency、其中只有 1 条有 ttfb
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=100, ttfb_ms=10)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=200, ttfb_ms=None)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=300, ttfb_ms=None)
    row = query.by_provider(username="u")[0]
    assert row["p95_latency_ms"] == 300              # 3 个样本 → 第 ⌈2.85⌉=3 小
    assert row["p95_ttfb_ms"] == 10                  # 1 个样本 → 唯一值
    # 均值口径照旧：ttfb_sum 把 NULL 折成 0 累加、除以同一个 ok_count
    # （与 overview / collector 逐字节一致，不是新口径）
    assert row["avg_latency_ms"] == 200 and row["avg_ttfb_ms"] == 3

def test_stats_group_percentiles_null_when_no_success(stats):
    """全失败 / 无延迟上报：分位列 None（前端显示 —），不拿 0 冒充有数据。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=False, latency_ms=500)
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     latency_ms=None, ttfb_ms=None)
    rows = {row["provider"]: row for row in query.by_provider(username="u")}
    assert rows["trae"]["p95_latency_ms"] is None and rows["trae"]["p95_ttfb_ms"] is None
    assert rows["codebuddy"]["p95_latency_ms"] is None
    # 行仍在（请求数 > 0），只是延迟列空——LEFT JOIN 而非 INNER 的用意
    assert rows["trae"]["requests"] == 1
    assert rows["codebuddy"]["requests"] == 1

def test_stats_group_percentiles_single_sample(stats):
    """单样本分组：P95 即该样本本身（⌈0.95⌉=1）。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=250, ttfb_ms=60)
    row = query.by_provider(username="u")[0]
    assert row["p95_latency_ms"] == 250 and row["p95_ttfb_ms"] == 60

def test_stats_group_percentiles_all_dimensions(stats):
    """四个维度都带分位值：三条路径（hourly × 3 + events）共用同一实现。"""
    collector, query = stats
    for i, value in enumerate(range(100, 1100, 100)):
        collector.record(username="u", provider="trae", model="m", ok=True,
                         credential_id="cred_a", latency_ms=value, ttfb_ms=value // 10,
                         now=1_700_000_000 + i)
    for rows in (query.by_provider(username="u"), query.by_model(username="u"),
                 query.by_user(username="u"),
                 query.by_credential(username="u")):
        assert rows[0]["p95_latency_ms"] == 1000 and rows[0]["p95_ttfb_ms"] == 100

def test_stats_group_percentiles_respect_scope_and_window(stats):
    """分位子查询独立应用作用域与时间窗（取值列用 ts，不是 hour_utc）。

    窗外的大延迟若混进来，P95 会被与当前范围无关的旧数据顶高。
    """
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=100, now=1000)
    collector.record(username="v", provider="trae", model="m", ok=True,
                     latency_ms=900, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=99999, now=9000)
    # 只看 u：v 的 900 不进分位
    assert query.by_provider(username="u")[0]["p95_latency_ms"] == 99999
    # until=7200 切掉 9000 那条：只剩 100
    assert query.by_provider(username="u", until=7200)[0]["p95_latency_ms"] == 100
    # since=7200 只留 9000 那条
    assert query.by_provider(username="u", since=7200)[0]["p95_latency_ms"] == 99999
    # 明细路径（凭证维度）同样尊重时间窗
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id="cred_a", latency_ms=100, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id="cred_a", latency_ms=99999, now=9000)
    assert query.by_credential(username="u", until=7200)[0]["p95_latency_ms"] == 100

def test_stats_groups_sort_by_latency_keys(stats):
    """延迟排序键：均值走 SQL 表达式，分位走 JOIN 列；ok_count=0 排最后。"""
    collector, query = stats
    # trae：2 条成功（latency 100/200）→ 均值 150、P95 200
    for value in (100, 200):
        collector.record(username="u", provider="trae", model="m", ok=True,
                         latency_ms=value, ttfb_ms=10)
    # codebuddy：1 条成功 latency 500 → 均值/P95 都是 500
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     latency_ms=500, ttfb_ms=90)
    # zen：全失败 → ok_count=0，延迟键上必须是 NULL（排最后）
    collector.record(username="u", provider="zen", model="m", ok=False, latency_ms=9999)

    def order(sort: str, order_by: str) -> list[str]:
        return [row["provider"] for row in
                query.by_provider(username="u", sort=sort, order=order_by)]

    # 均值降序：codebuddy(500) > trae(150) > zen(NULL)
    assert order("avg_latency_ms", "desc") == ["codebuddy", "trae", "zen"]
    assert order("avg_ttfb_ms", "desc") == ["codebuddy", "trae", "zen"]
    # 分位降序：codebuddy(500) > trae(200) > zen(NULL)
    assert order("p95_latency_ms", "desc") == ["codebuddy", "trae", "zen"]
    # 升序时 SQLite 把 NULL 排最前（与降序排最后相反）：缺失不是「最小」，
    # 但这是 SQLite 对所有可空列的既有语义，延迟列不特殊化
    assert order("p95_ttfb_ms", "asc") == ["zen", "trae", "codebuddy"]
    # 未知键仍回落默认（请求数降序）：trae 2 > codebuddy 1 = zen 1，并列按分组键
    assert order("ghost", "desc") == ["trae", "codebuddy", "zen"]

def test_stats_group_percentiles_survive_hourly_rollup(stats):
    """分位值与汇总无关：rollup 重算小时表不影响明细上的分位结果。"""
    collector, query = stats
    for value in (100, 200, 300):
        collector.record(username="u", provider="trae", model="m", ok=True,
                         latency_ms=value)
    before = query.by_provider(username="u")[0]["p95_latency_ms"]
    collector.rollup_hourly()
    assert query.by_provider(username="u")[0]["p95_latency_ms"] == before == 300

def test_overview_matches_timeline_beyond_retention_window(stats):
    """超过 90 天的时段：总览与图表必须同值（同读小时汇总）。

    此前总览读 usage_events（90 天）、图表读 usage_hourly（永久），
    选「全部」时同屏两个数字矛盾：明细被清理后总览缩水、图表不变。
    """
    collector, query = stats
    now = int(time.time())
    old = now - 200 * 86400               # 已超出 90 天明细保留期
    for ts in (old, now):
        collector.record(username="u", provider="trae", model="m", ok=True,
                         input_tokens=5, now=ts)
    RetentionTask(collector, retention_days=90).run_once()
    assert query.overview(username="u")["requests"] == 2          # 含已过期的老明细
    assert sum(p["trae"] for p in query.timeline(username="u")) == 2

def test_overview_reasoning_and_cached_aggregate_from_hourly(stats):
    """小时表带上 reasoning/cached 后，总览仍能给准确值（与明细同源）。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, reasoning_tokens=7, cached_tokens=40)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=50, reasoning_tokens=3)     # 未上报 cached
    collected = query.overview(username="u")
    assert collected["reasoning_tokens"] == 10
    assert collected["cached_tokens"] == 40      # 只有上报过的那条计入
    assert collected["input_tokens"] == 150
    # 未上报过 cached 的时段 → None，不能当成 0
    collector.record(username="v", provider="trae", model="m", ok=True)
    assert query.overview(username="v")["cached_tokens"] is None

def test_overview_latency_averages_only_successful_requests(stats):
    """均值口径与图表一致：除以 ok_count，失败请求不拉偏「典型耗时」。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=100, ttfb_ms=30)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=300, ttfb_ms=50)
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="upstream_error", latency_ms=900, ttfb_ms=900)
    overview = query.overview(username="u")
    assert overview["avg_latency_ms"] == 200    # (100+300)/2，失败那条不计入
    assert overview["avg_ttfb_ms"] == 40

def test_stats_overview_since_filter(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, now=9000)
    assert query.overview(username="u", since=5000)["requests"] == 1

def test_stats_until_is_exclusive_upper_bound(stats):
    """until 是半开上界的排除端：同一时刻的数据既不在「..到 T」也不在「T..」重复计。

    两条记录落在不同小时桶（1000 → 桶 0，9000 → 桶 7200）。until=7200 只留桶 0，
    且 hour_utc < 7200 严格成立（7200 本身被排除）。
    """
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, now=9000)
    assert query.overview(username="u", until=7200)["requests"] == 1
    assert query.overview(username="u", since=7200)["requests"] == 1
    # 两端都限：闭下界 7200 + 开上界 9000 → 含桶 7200，含桶 9000 被排除（此处仅 1 条）
    assert query.overview(username="u", since=7200, until=9000)["requests"] == 1
    # until 落在桶下界：桶 0（hour_utc=0）不满足 0 < 0，被排除
    assert query.overview(username="u", until=0)["requests"] == 0

def test_stats_until_filters_groups_and_timeline(stats):
    """until 贯通分组（逐小时汇总与逐明细两条路径）与时间序列。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, now=9000)
    collector.rollup_hourly()
    # 小时汇总路径：桶 0 与桶 7200，until=7200 只留桶 0
    assert query.by_provider(username="u", until=7200)[0]["requests"] == 1
    # 时间轴按小时补齐：桶 0 有数据、桶 3600 补 0，桶 7200 被 until 排除
    points = query.timeline(username="u", until=7200)
    assert [point["hour"] for point in points] == [0, 3600]
    assert points[0]["trae"] == 1 and points[1]["trae"] == 0
    # 明细路径（ts 列）：凭证维度读 usage_events，until 也按 ts 过滤
    by_cred = query.by_credential(username="u", until=7200)
    assert sum(row["requests"] for row in by_cred) == 0  # 明细无 credential_id 不分组
    events = query.events(username="u", until=7200)["events"]
    assert [row["ts"] for row in events] == [1000]

def test_stats_export_events_until_filter(stats):
    """导出取数同样尊重 until（exports 与 events 共用 _where 的时间窗）。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, now=9000)
    rows = query.export_events(username="u", until=7200)
    assert [row["ts"] for row in rows] == [1000]

def test_stats_hourly_rolls_via_retention(stats):
    """record 即时累加汇总（总览/图表不必等一轮 retention）；rollup 幂等不双计。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1_700_003_600)
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="rate_limit", now=1_700_003_601)
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     now=1_700_003_602, input_tokens=7, output_tokens=3,
                     credit=0.5, latency_ms=100)
    # 增量累加：最新小时立即可见（不需等 5 分钟一轮的 rollup）。
    # 时间轴按 since 补齐：since=1_700_000_000 落在桶 1699999200，故首桶是空的
    points = query.timeline(username="u", since=1_700_000_000)
    assert [point["hour"] for point in points] == [1699999200, 1700002800]
    assert points[1]["trae"] == 2 and points[1]["codebuddy"] == 1
    # 全量重算与该小时的增量值一致（幂等，不双计）
    RetentionTask(collector, retention_days=90).run_once()
    points = query.timeline(username="u", since=1_700_000_000)
    assert points[1]["trae"] == 2 and points[1]["codebuddy"] == 1
    bucket = collector._db.connect().execute(
        "SELECT SUM(requests), SUM(ok_count), SUM(input_tokens), SUM(output_tokens), "
        "SUM(credit_known), SUM(latency_sum) FROM usage_hourly").fetchone()
    assert tuple(bucket) == (3, 2, 7, 3, 1, 100)

def test_stats_timeline_by_hour_and_provider(stats):
    collector, query = stats
    # 同一小时两条明细（同一 provider 累积到同一点，跨 provider 分列）
    collector.record(username="u", provider="codebuddy", model="m", ok=True, now=1_700_000_000)
    collector.record(username="u", provider="codebuddy", model="m", ok=True, now=1_700_000_100)
    collector.record(username="u", provider="trae", model="m", ok=True, now=1_700_000_200)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     now=1_700_000_000 + 3600)  # 下一小时
    collector.rollup_hourly()
    points = query.timeline(username="u")
    # 两小时、每个小时都含 codebuddy/trae 两路计数
    assert len(points) == 2
    first = points[0]
    assert first["codebuddy"] == 2 and first["trae"] == 1
    assert points[1]["trae"] == 1
    # since 过滤按小时边界（第二小时整点 = first hour 整点 + 3600）
    second_hour = first["hour"] + 3600
    filtered = query.timeline(username="u", since=second_hour)
    assert len(filtered) == 1 and filtered[0]["trae"] == 1

def test_stats_timeline_metric_dimensions(stats):
    """timeline 支持请求数/token/耗时/首字四维；均值按成功数归一；非法回退。"""
    collector, query = stats
    base = 1_700_000_000
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     input_tokens=10, output_tokens=5, latency_ms=100, ttfb_ms=40, now=base)
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     input_tokens=20, output_tokens=5, latency_ms=300, ttfb_ms=60, now=base)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, output_tokens=50, latency_ms=200, ttfb_ms=80, now=base)
    collector.rollup_hourly()

    req = query.timeline(username="u")  # 默认请求次数
    first = req[0]
    assert first["codebuddy"] == 2 and first["trae"] == 1
    # token = 输入 + 输出（跨渠道各自的合计）
    tok = query.timeline(username="u", metric="tokens")[0]
    assert tok["codebuddy"] == 40 and tok["trae"] == 150
    # 耗时/首字 = 成功请求均值：(100+300)/2=200，trae 单条即自身
    lat = query.timeline(username="u", metric="latency")[0]
    assert lat["codebuddy"] == 200 and lat["trae"] == 200
    ttf = query.timeline(username="u", metric="ttfb")[0]
    assert ttf["codebuddy"] == 50 and ttf["trae"] == 80
    # 非法 metric 回退请求次数
    assert query.timeline(username="u", metric="bogus") == req


def test_stats_timeline_derived_metrics(stats):
    """派生指标：成功率 / 缓存命中率 / 单请求成本，均为「先聚合后相除」。"""
    collector, query = stats
    base = 1_699_999_200          # 整点：与 usage_hourly 的 hour 桶对齐
    # codebuddy：3 次请求 2 成功，输入 100 命中 40，单次成本由 pricing 决定
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     input_tokens=40, cached_tokens=40, now=base)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     input_tokens=40, cached_tokens=0, now=base)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=False,
                     error_type="rate_limit", input_tokens=20, now=base)
    collector.rollup_hourly()

    rate = query.timeline(username="u", metric="success_rate")[0]
    # 3 请求 2 成功 → 0.6667（不是逐小时平均，而是 sum/sum）
    assert rate["codebuddy"] == pytest.approx(0.6667)
    cache = query.timeline(username="u", metric="cache_rate")[0]
    # 命中 40 / 输入 100 → 0.4
    assert cache["codebuddy"] == pytest.approx(0.4)


def test_stats_timeline_derived_metrics_no_sample_is_none(stats):
    """无样本的派生指标给 None（断线），不画成 0%。"""
    collector, query = stats
    # 未上报缓存 → cache_rate 为 None；无请求的小时在稠密轴上同样是 None
    collector.record(username="u", provider="trae", model="unpriced", ok=True,
                     input_tokens=100, now=1_700_000_000)
    collector.rollup_hourly()
    assert query.timeline(username="u", metric="cache_rate")[0]["trae"] is None
    # 无可定价明细 → 单请求成本 None
    assert query.timeline(username="u", metric="cost_per_request")[0]["trae"] is None
    # 全失败的小时：均值类为 None（不是 0 ms）
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="upstream_error", now=1_700_003_600)
    collector.rollup_hourly()
    lat = query.timeline(username="u", metric="latency", since=1_700_000_000)
    assert lat[1]["trae"] is None


def test_stats_timeline_dense_hours(stats):
    """时间轴稠密：缺失小时补桶，可加量补 0、均值补 None。"""
    collector, query = stats
    base = 1_699_999_200          # 整点：与 usage_hourly 的 hour 桶对齐
    collector.record(username="u", provider="trae", model="m", ok=True, now=base)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     now=base + 5 * 3600)      # 中间空 4 小时
    collector.rollup_hourly()

    points = query.timeline(username="u")
    assert len(points) == 6                     # 首末之间每小时一个点
    assert [point["hour"] for point in points] == [base + i * 3600 for i in range(6)]
    # 请求数：空档补 0
    assert [point["trae"] for point in points] == [1, 0, 0, 0, 0, 1]
    # 均值：空档补 None（不是 0 ms）
    lat = query.timeline(username="u", metric="latency")
    assert lat[1]["trae"] is None


def test_stats_timeline_total_recomputes_ratio(stats):
    """`__total` 按同一小时的 sum/count 重新相除，不是各渠道值相加。"""
    collector, query = stats
    base = 1_699_999_200
    # codebuddy：2 请求全成功、各 100ms；trae：2 请求 1 成功、各 900ms
    for _ in range(2):
        collector.record(username="u", provider="codebuddy", model="m", ok=True,
                         latency_ms=100, now=base)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=900, now=base)
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="rate_limit", latency_ms=900, now=base)
    collector.rollup_hourly()

    # 请求数：可加 → 4
    req = query.timeline(username="u")[0]
    assert req["codebuddy"] == 2 and req["trae"] == 2 and req["__total"] == 4
    # 耗时：全池加权均值 (100+100+900)/3 = 366.67 → 367；相加会得到 1000
    lat = query.timeline(username="u", metric="latency")[0]
    assert lat["__total"] == 367
    # 成功率：3 成功 / 4 请求 = 0.75；相加会得到 1.5
    rate = query.timeline(username="u", metric="success_rate")[0]
    assert rate["__total"] == pytest.approx(0.75)


def test_stats_timeline_dense_hours_respects_window(stats):
    """稠密轴按 since/until 补齐窗口两端；跨度超上限时退回已观测小时。"""
    collector, query = stats
    base = 1_699_999_200          # 整点：与 usage_hourly 的 hour 桶对齐
    collector.record(username="u", provider="trae", model="m", ok=True, now=base)
    collector.rollup_hourly()

    # 窗口从更早开始 → 前面补空桶
    points = query.timeline(username="u", since=base - 2 * 3600)
    assert [point["hour"] for point in points] == [base - 7200, base - 3600, base]
    assert points[0]["trae"] == 0
    # 跨度超 MAX_DENSE_HOURS（92 天）→ 不补，只回已观测小时
    wide = query.timeline(username="u", since=base - MAX_DENSE_HOURS * 3600)
    assert [point["hour"] for point in wide] == [base]


def test_stats_model_timeline_dense_and_derived(stats):
    """model_timeline 同样稠密补桶，派生指标语义与 timeline 一致。"""
    collector, query = stats
    base = 1_699_999_200          # 整点：与 usage_hourly 的 hour 桶对齐
    collector.record(username="u", provider="trae", model="a", ok=True,
                     input_tokens=50, cached_tokens=25, now=base)
    collector.record(username="u", provider="trae", model="a", ok=False,
                     error_type="rate_limit", now=base + 2 * 3600)
    collector.rollup_hourly()

    result = query.model_timeline(username="u", metric="success_rate")
    assert [point["hour"] for point in result["points"]] == [
        base, base + 3600, base + 7200]
    assert result["points"][0]["a"] == 1.0
    assert result["points"][1]["a"] is None     # 空档：比率断线
    assert result["points"][2]["a"] == 0.0
    cache = query.model_timeline(username="u", metric="cache_rate")
    assert cache["points"][0]["a"] == pytest.approx(0.5)


def test_stats_percentile_timeline(stats):
    """分位时序：P50/P95 按小时算，只统计成功请求，缺失小时补 None。"""
    collector, query = stats
    base = 1_699_999_200          # 整点：与 usage_hourly 的 hour 桶对齐
    for latency in (100, 200, 300, 400, 1000):
        collector.record(username="u", provider="trae", model="m", ok=True,
                         latency_ms=latency, now=base)
    # 失败请求的延迟不计入（ok=0）
    collector.record(username="u", provider="trae", model="m", ok=False,
                     error_type="rate_limit", latency_ms=99999, now=base)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     latency_ms=700, now=base + 2 * 3600)

    result = query.percentile_timeline(username="u")
    assert result["metric"] == "latency" and result["window_days"] == 90
    points = result["points"]
    assert [point["hour"] for point in points] == [base, base + 3600, base + 7200]
    # 5 个样本：P50 = ⌈0.5·5⌉ = 第 3 小 = 300；P95 = ⌈0.95·5⌉ = 第 5 小 = 1000
    assert points[0]["p50"] == 300 and points[0]["p95"] == 1000
    assert points[0]["samples"] == 5
    assert points[1]["p50"] is None and points[1]["samples"] == 0
    assert points[2]["p50"] == 700
    # ttfb 口径：无 ttfb 数据 → 全 None
    assert all(point["p50"] is None
               for point in query.percentile_timeline(username="u",
                                                      metric="ttfb")["points"])


def test_stats_percentile_timeline_endpoint(admin_client):
    """percentile-timeline 端点：作用域与 metric 透传。"""
    _app, client = admin_client
    collector = client.app.state.stats_collector
    collector.record(username="root", provider="trae", model="m", ok=True,
                     latency_ms=500, ttfb_ms=120)
    body = client.get("/api/stats/percentile-timeline").json()
    assert body["metric"] == "latency" and body["points"][0]["p50"] == 500
    ttfb = client.get("/api/stats/percentile-timeline?metric=ttfb").json()
    assert ttfb["metric"] == "ttfb" and ttfb["points"][0]["p95"] == 120

def test_stats_model_timeline_metric_and_ttfb_rollup(stats):
    """model_timeline 指标切换 + ttfb 聚合进小时表（老库补列后可用）。"""
    collector, query = stats
    base = 1_699_999_200          # 整点：与 usage_hourly 的 hour 桶对齐
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     input_tokens=10, output_tokens=5, latency_ms=100, ttfb_ms=40, now=base)
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     input_tokens=20, output_tokens=5, latency_ms=300, ttfb_ms=60, now=base)
    collector.rollup_hourly()

    tok = query.model_timeline(username="u", metric="tokens")
    assert tok["points"][0]["glm-5.2"] == 40
    ttf = query.model_timeline(username="u", metric="ttfb")
    assert ttf["points"][0]["glm-5.2"] == 50
    # Top N 排序口径不随 metric 变（仍按请求量）
    assert query.model_timeline(username="u", metric="latency")["models"] == ["glm-5.2"]
    # ttfb 确实聚合进了小时表
    conn = collector._db.connect()
    row = conn.execute("SELECT ttfb_sum, latency_sum FROM usage_hourly").fetchone()
    assert row["ttfb_sum"] == 100 and row["latency_sum"] == 400

def test_stats_model_timeline_top_models_and_points(stats):
    """按模型趋势：Top N 宽表点列，按总量降序；其余模型不出线。"""
    collector, query = stats
    base = 1_699_999_200          # 整点：与 usage_hourly 的 hour 桶对齐
    # glm-5.2 共 4 次（最热）、kimi-k3 共 2 次、legacy-old 共 1 次（超出 Top 2）
    for _ in range(4):
        collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True, now=base)
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True, now=base)
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True,
                     now=base + 3600)
    collector.record(username="u", provider="codebuddy", model="legacy-old", ok=True,
                     now=base + 3600)
    collector.rollup_hourly()

    result = query.model_timeline(username="u", top=2)
    assert result["models"] == ["glm-5.2", "kimi-k3"]
    points = result["points"]
    assert len(points) == 2
    assert points[0] == {"hour": base // 3600 * 3600, "glm-5.2": 4, "kimi-k3": 1}
    assert points[1]["glm-5.2"] == 0 and points[1]["kimi-k3"] == 1
    # username 过滤：只统计指定用户
    assert query.model_timeline(username="nobody")["points"] == []

def test_stats_query_by_provider_since_filter(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True, now=9000)
    assert query.by_provider(username="u", since=5000)[0]["requests"] == 1

def test_stats_overview_provider_filter(stats):
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True)
    collector.record(username="u", provider="codebuddy", model="m", ok=True)
    assert query.overview(username="u", provider="trae")["requests"] == 1

def test_stats_model_timeline_filters(stats):
    """按模型趋势：username / since 筛选生效。"""
    collector, query = stats
    collector.record(username="alice", provider="trae", model="m", ok=True, now=1000)
    collector.record(username="bob", provider="trae", model="m2", ok=True, now=9000)
    collector.rollup_hourly()

    scoped = query.model_timeline(username="alice")
    assert scoped["models"] == ["m"]
    recent = query.model_timeline(since=3600)          # bob 的小时桶 7200 命中
    assert recent["models"] == ["m2"]

def test_stats_cached_tokens_overview_and_events(stats):
    """输入缓存命中：汇总（无上报则 None）与明细返回。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     input_tokens=100, cached_tokens=40)
    collector.record(username="u", provider="trae", model="m", ok=True, input_tokens=50)
    overview = query.overview()
    assert overview["input_tokens"] == 150
    assert overview["cached_tokens"] == 40              # 只有一条上报 → SUM=40

    events = query.events()                          # 新→旧：后插入的未上报记录在前
    assert [e["cached_tokens"] for e in events["events"]] == [None, 40]

def test_stats_model_timeline_endpoint(admin_client):
    """model-timeline 端点透传聚合结果；metric 参数透传。"""
    _app, client = admin_client
    collector = client.app.state.stats_collector
    collector.record(username="root", provider="trae", model="m", ok=True,
                     input_tokens=10, output_tokens=5, ttfb_ms=40)
    collector.rollup_hourly()
    body = client.get("/api/stats/model-timeline").json()
    assert body["models"] == ["m"] and body["points"]
    # metric 参数切换取值语义（默认请求次数 vs tokens 合计）
    tokens = client.get("/api/stats/model-timeline?metric=tokens").json()
    assert tokens["points"][0]["m"] == 15
    # 非法 metric 回退请求次数
    assert client.get("/api/stats/model-timeline?metric=bogus").json()["points"] == body["points"]

def test_stats_events_empty(stats):
    """空库：明细返回空页且无游标。"""
    _collector, query = stats
    assert query.events() == {"events": [], "next_before": None, "total": None}


def test_stats_events_drilldown_filters(stats):
    """钻取过滤：provider / model / credential_id 三者独立可叠加（AND）。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="glm-5.2", ok=True,
                     credential_id="c1")
    collector.record(username="u", provider="trae", model="kimi-k3", ok=True,
                     credential_id="c2")
    collector.record(username="u", provider="codebuddy", model="glm-5.2", ok=True,
                     credential_id="c1")

    by_provider = query.events(provider="trae")["events"]
    assert {e["model"] for e in by_provider} == {"glm-5.2", "kimi-k3"}
    by_model = query.events(model="glm-5.2")["events"]
    assert {e["provider"] for e in by_model} == {"trae", "codebuddy"}
    by_cred = query.events(credential_id="c1")["events"]
    assert len(by_cred) == 2
    # 叠加：trae + glm-5.2 只剩一条
    combined = query.events(provider="trae", model="glm-5.2")["events"]
    assert len(combined) == 1 and combined[0]["credential_id"] == "c1"
    # offset 模式（非默认排序）同样带过滤
    assert query.events(provider="trae", sort="ts", order="asc")["total"] == 2


def test_export_events_orders_ascending_and_respects_filters(stats):
    """导出固定旧→新（升序），并复用同一套过滤；多取一条供截断判断。"""
    collector, query = stats
    for index in range(3):
        collector.record(username="u", provider="trae", model=f"m{index}", ok=True,
                         credential_id="c1" if index < 2 else "c2")
    rows = query.export_events()
    assert [r["model"] for r in rows] == ["m0", "m1", "m2"]     # 升序
    filtered = query.export_events(credential_id="c1")
    assert [r["model"] for r in filtered] == ["m0", "m1"]
    # limit 生效且多取一条（limit=1 → 返回 2 条供调用方判断截断）
    assert len(query.export_events(limit=1)) == 2


def test_stats_events_pagination_and_filters(stats):
    """明细查询：新→旧、rowid 游标翻页不漏不重、筛选组合生效。"""
    collector, query = stats
    for index in range(5):
        collector.record(username="alice" if index % 2 else "bob",
                         provider="trae", model=f"m{index}", ok=bool(index % 2),
                         error_type=None if index % 2 else "rate_limit",
                         now=1000 + index)

    page1 = query.events(limit=2)
    assert [e["model"] for e in page1["events"]] == ["m4", "m3"]
    assert page1["next_before"] == page1["events"][-1]["rowid"]
    page2 = query.events(limit=2, before=page1["next_before"])
    assert [e["model"] for e in page2["events"]] == ["m2", "m1"]
    page3 = query.events(limit=2, before=page2["next_before"])
    assert [e["model"] for e in page3["events"]] == ["m0"]
    assert page3["next_before"] is None                     # 到底

    # 组合筛选：按用户（新→旧）；按时间下限
    assert [e["model"] for e in query.events(username="alice")["events"]] == ["m3", "m1"]
    assert [e["model"] for e in query.events(since=1003)["events"]] == ["m4", "m3"]

def test_stats_events_include_credential_name(repo):
    """明细带凭证 ID 与昵称：存在时 JOIN 昵称；已删除/无凭证时回退 NULL。"""
    credentials, db = repo
    collector, query = StatsCollector(db), StatsQuery(db)
    keep_id = credentials.add(provider="trae", credential_data={"accessToken": "t"},
                              nickname="主号")
    ghost_id = credentials.add(provider="trae", credential_data={"accessToken": "g"},
                               nickname="将删")
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id=keep_id)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id=ghost_id)
    collector.record(username="u", provider="trae", model="m", ok=True)  # 无凭证
    assert credentials.delete(ghost_id) is True

    blank_id = credentials.add(provider="codearts", credential_data={}, nickname="")
    collector.record(username="u", provider="codearts", model="m", ok=True,
                     credential_id=blank_id)

    rows = {e["credential_id"]: e for e in
            query.events(include_credential_name=True)["events"]}
    assert rows[keep_id]["credential_name"] == "主号"
    assert rows[ghost_id]["credential_name"] is None        # 已删除 → 回退
    assert rows[None]["credential_name"] is None            # 无凭证
    assert rows[blank_id]["credential_name"] is None        # 空昵称 → 同回退

    # 默认（viewer 口径）不暴露共享池昵称
    default_rows = {e["credential_id"]: e for e in query.events()["events"]}
    assert default_rows[keep_id]["credential_name"] is None

def test_stats_events_endpoint_scope_and_clamp(admin_client):
    """明细端点：admin 可看他人；limit 被夹在 1..200。"""
    _app, client = admin_client
    collector = client.app.state.stats_collector
    for index in range(3):
        collector.record(username="other", provider="trae", model=f"m{index}", ok=True)

    body = client.get("/api/stats/events").json()
    assert [e["model"] for e in body["events"]] == ["m2", "m1", "m0"]
    scoped = client.get("/api/stats/events", params={"username": "other"}).json()
    assert len(scoped["events"]) == 3
    limited = client.get("/api/stats/events", params={"limit": 2}).json()
    assert len(limited["events"]) == 2 and limited["next_before"] is not None
    assert len(client.get("/api/stats/events", params={"limit": 0}).json()["events"]) == 1
    huge = client.get("/api/stats/events", params={"limit": 10 ** 9}).json()
    assert len(huge["events"]) == 3

def test_stats_events_restrict_non_admin(tmp_path):
    """非 admin 只见自己的明细，指定他人用户名无效。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.config import Settings
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    app.state.stats_collector.record(username="alice", provider="trae", model="m", ok=True)
    app.state.stats_collector.record(username="bob", provider="trae", model="m", ok=True)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        mine = client.get("/api/stats/events").json()
        assert [e["username"] for e in mine["events"]] == ["alice"]
        forced = client.get("/api/stats/events", params={"username": "bob"}).json()
        assert [e["username"] for e in forced["events"]] == ["alice"]


# ------------------------------------------------------------- CSV 导出（4.11）

def test_stats_events_csv_export(admin_client):
    """CSV 导出：带 BOM、表头、公式防护，含行数/截断响应头。"""
    _app, client = admin_client
    collector = client.app.state.stats_collector
    collector.record(username="other", provider="trae", model="=cmd|calc", ok=True,
                     input_tokens=10, output_tokens=5, now=1_700_000_000)
    # 有生成窗口的成功请求：速度两列应有值（正文 50 / 总产出 200 tok/s）
    collector.record(username="other", provider="trae", model="fast", ok=True,
                     output_tokens=1000, reasoning_tokens=750,
                     latency_ms=6000, ttfb_ms=1000, now=1_700_000_050)
    collector.record(username="other", provider="codebuddy", model="m2", ok=False,
                     error_type="rate_limit", now=1_700_000_100)

    response = client.get("/api/stats/events", params={"format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["x-export-row-count"] == "3"
    assert response.headers["x-export-truncated"] == "0"
    assert "attachment" in response.headers["content-disposition"]
    assert ".csv" in response.headers["content-disposition"]
    text = response.text
    assert text.startswith("\ufeff")                  # BOM 供 Excel 认 UTF-8
    lines = text.lstrip("\ufeff").split("\r\n")
    assert lines[0].startswith("时间,用户,渠道,凭证,模型")
    assert "正文速度 tok/s,总产出速度 tok/s" in lines[0]
    # 速度与页面明细表同口径：可测的行有值，不可测的行留空
    assert ",50.0,200.0," in text
    # 模型列以 = 开头 → 加前导单引号防公式注入
    assert "'=cmd|calc" in text
    assert "失败" in text and "rate_limit" in text


def test_stats_events_csv_export_filters_and_scope(admin_client):
    """导出复用同一套钻取过滤与作用域。"""
    _app, client = admin_client
    collector = client.app.state.stats_collector
    collector.record(username="other", provider="trae", model="glm", ok=True)
    collector.record(username="other", provider="codebuddy", model="glm", ok=True)

    filtered = client.get("/api/stats/events",
                          params={"format": "csv", "provider": "trae"})
    body = filtered.text.lstrip("\ufeff")
    assert body.count("\r\n") == 2                    # 表头 + 1 行
    assert "trae" in body and "codebuddy" not in body


def test_stats_events_csv_export_requires_operator(tmp_path):
    """viewer 导出被拒（403）；admin 放行。"""
    from fastapi.testclient import TestClient

    from src.auth.rbac import ROLE_ADMIN, ROLE_VIEWER
    from src.auth.session import create_session_token
    from src.auth.users import create_password_hash
    from src.db.conn import Database
    from src.db.migrate import apply_schema
    from src.db.repo import UserRepository

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", USERS_FILE=str(tmp_path / "none.txt"))
    db = Database(settings.db_path)
    apply_schema(db.connect())
    users = UserRepository(db)
    users.create("root", create_password_hash("x"), role=ROLE_ADMIN, enabled=True, now=1)
    users.create("looker", create_password_hash("x"), role=ROLE_VIEWER, enabled=True, now=1)
    db.close()
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("looker", SECRET))
        assert client.get("/api/stats/events",
                          params={"format": "csv"}).status_code == 403
        # 同一 viewer 走 JSON 明细仍可用（只是看不到他人凭证昵称）
        assert client.get("/api/stats/events").status_code == 200
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        assert client.get("/api/stats/events",
                          params={"format": "csv"}).status_code == 200


def test_stats_events_csv_export_truncates(admin_client, monkeypatch):
    """超上限：只导出上限条，响应头标截断。"""
    from src.api import admin_stats

    _app, client = admin_client
    collector = client.app.state.stats_collector
    for index in range(3):
        collector.record(username="other", provider="trae", model=f"m{index}", ok=True)
    monkeypatch.setattr(admin_stats, "EXPORT_MAX_ROWS", 2)
    response = client.get("/api/stats/events", params={"format": "csv"})
    assert response.headers["x-export-row-count"] == "2"
    assert response.headers["x-export-truncated"] == "1"


def test_overview_output_speed_divides_totals_not_per_request_average(stats):
    """输出速度 = 总输出 token ÷ 生成时间总和，不是逐条比值的平均。

    逐条平均会被短请求带歪：本例两条长请求各自约 100 / 91 tok/s，逐条平均
    约 95.5；总量相除是 94.1。断言后者证明实现走的是总量口径。
    """
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=300, latency_ms=4000, ttfb_ms=1000)   # 生成 3.0s
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=500, latency_ms=6500, ttfb_ms=1000)   # 生成 5.5s
    overview = query.overview(username="u")
    assert overview["tps"] == round(800 * 1000 / 8500, 1) == 94.1
    assert overview["tps_estimated"] is False


def test_overview_output_speed_counts_short_outputs_excludes_zero(stats):
    """不设输出量下限：短输出照样进样本，只有「产出 0 个 token」与失败被排除。

    短输出也是真实产出；剔掉它们会让短问答为主的渠道永远没有速度。输出为 0 /
    未上报的请求没有可测产出（比值恒为 0），不算样本——那是「没产出」不是「慢」。
    """
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=3, latency_ms=1000, ttfb_ms=100)    # 3 token / 0.9s
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=97, latency_ms=1100, ttfb_ms=100)   # 97 token / 1.0s
    overview = query.overview(username="u")
    # 总量相除：100 token / 1.9s（不是逐条平均）
    assert overview["tps"] == round(100 * 1000 / 1900, 1) == 52.6
    assert overview["tps_estimated"] is False
    # 输出 0 / 未上报 / 失败：都不进样本
    collector.record(username="v", provider="trae", model="m", ok=True,
                     output_tokens=0, latency_ms=1000, ttfb_ms=100)
    collector.record(username="v", provider="trae", model="m", ok=True,
                     output_tokens=None, latency_ms=1000, ttfb_ms=100)
    collector.record(username="v", provider="trae", model="m", ok=False,
                     error_type="upstream_error", output_tokens=5000,
                     latency_ms=9000, ttfb_ms=100)
    assert query.overview(username="v")["tps"] is None


def test_overview_output_speed_estimates_without_first_byte_data(stats):
    """无首字数据（非流式）时按端到端耗时估算（含首字等待），样本标 ≈。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=1000, latency_ms=2000, ttfb_ms=None)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=200, latency_ms=500, ttfb_ms=None)
    overview = query.overview(username="u")
    assert overview["tps"] == round(1200 * 1000 / 2500, 1) == 480.0
    assert overview["tps_estimated"] is True


def test_overview_output_speed_estimate_uses_same_output_floor(stats):
    """估算口径与路由口径同门槛（>0 token），两口径只差在分母。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=199, latency_ms=1000, ttfb_ms=None)
    overview = query.overview(username="u")
    assert overview["tps"] == 199.0
    assert overview["tps_estimated"] is True
    # 短输出同样算（无输出量下限）；0 个 token 不算样本
    collector.record(username="v", provider="trae", model="m", ok=True,
                     output_tokens=5, latency_ms=1000, ttfb_ms=None)
    assert query.overview(username="v")["tps"] == 5.0
    collector.record(username="w", provider="trae", model="m", ok=True,
                     output_tokens=0, latency_ms=1000, ttfb_ms=None)
    assert query.overview(username="w")["tps"] is None


def test_overview_output_speed_prefers_routed_over_estimate(stats):
    """两条口径都有样本时用路由口径（生成时间总和），不混入估算样本。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=400, latency_ms=5000, ttfb_ms=1000)     # 路由
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=1000, latency_ms=2000, ttfb_ms=None)    # 估算样本
    overview = query.overview(username="u")
    assert overview["tps"] == 100.0                 # 只算 400 tok / 4.0s
    assert overview["tps_estimated"] is False


def test_overview_output_speed_respects_scope_and_window(stats):
    """速度读明细表，独立应用作用域与时间窗（取值列是 ts）。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=400, latency_ms=5000, ttfb_ms=1000, now=1000)
    collector.record(username="v", provider="trae", model="m", ok=True,
                     output_tokens=900, latency_ms=2000, ttfb_ms=500, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=900, latency_ms=2000, ttfb_ms=500, now=9000)
    assert query.overview(username="u")["tps"] == round(1300 * 1000 / 5500, 1)
    assert query.overview(username="u", since=5000)["tps"] == 600.0
    assert query.overview(username="u", until=5000)["tps"] == 100.0
    # provider 过滤同样生效（总览卡片支持按渠道收窄）
    assert query.overview(provider="trae", since=5000)["tps"] == 600.0


def test_stats_groups_report_output_speed_with_estimate_fallback(stats):
    """分组行带速度：路由口径优先，无样本的组回落估算并标 tps_estimated。"""
    collector, query = stats
    # trae：有首字数据 → 路由口径 400 tok / 4.0s = 100
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=400, latency_ms=5000, ttfb_ms=1000)
    # codebuddy：无首字数据 → 估算口径 600 tok / 3.0s = 200
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     output_tokens=600, latency_ms=3000, ttfb_ms=None)
    # zen：生成窗口与端到端耗时都过短 → 两个口径都没有样本
    collector.record(username="u", provider="zen", model="m", ok=True,
                     output_tokens=50, latency_ms=150, ttfb_ms=100)
    rows = {row["provider"]: row for row in query.by_provider(username="u")}
    assert rows["trae"]["tps"] == 100.0 and rows["trae"]["tps_estimated"] is False
    assert rows["codebuddy"]["tps"] == 200.0
    assert rows["codebuddy"]["tps_estimated"] is True
    assert rows["zen"]["tps"] is None and rows["zen"]["tps_estimated"] is False


def test_stats_groups_output_speed_all_dimensions_and_sort(stats):
    """四个维度都带速度值；排序键走明细聚合列（无样本的组排最后）。"""
    collector, query = stats
    for value, provider in ((200, "trae"), (800, "codebuddy")):
        collector.record(username="u", provider=provider, model="m", ok=True,
                         credential_id=f"cred_{provider}",
                         output_tokens=value, latency_ms=2000, ttfb_ms=1000)
    # zen 全失败 → 速度列 NULL（不是 0）
    collector.record(username="u", provider="zen", model="m", ok=False,
                     error_type="upstream_error", output_tokens=900,
                     latency_ms=2000, ttfb_ms=1000)
    assert {row["tps"] for row in query.by_provider(username="u")} == {200.0, 800.0, None}
    assert {row["tps"] for row in query.by_credential(username="u")} == {200.0, 800.0}
    # 模型 / 用户维度把两个渠道合并成一行：总量相除得 1000 tok / 2.0s
    for rows in (query.by_model(username="u"), query.by_user(username="u")):
        assert [row["tps"] for row in rows] == [500.0]
    order = [row["provider"] for row in
             query.by_provider(username="u", sort="tps", order="desc")]
    assert order == ["codebuddy", "trae", "zen"]
    # 升序时 NULL 排最前（SQLite 对可空列的既有语义，延迟列同样如此）
    assert query.by_provider(username="u", sort="tps", order="asc")[0]["provider"] == "zen"


def test_stats_group_output_speed_respects_window(stats):
    """分组速度与分位同源明细表：时间窗按 ts 过滤，不受小时汇总干扰。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=400, latency_ms=5000, ttfb_ms=1000, now=1000)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=900, latency_ms=2000, ttfb_ms=500, now=9000)
    assert query.by_provider(username="u", until=5000)[0]["tps"] == 100.0
    assert query.by_provider(username="u", since=5000)[0]["tps"] == 600.0


def test_speed_value_branches():
    """`_speed_value`：两个口径的分母/分子都要 > 0，缺一就是「没有样本」。"""
    from src.stats.query import _speed_value

    # 路由口径：总产出 400 / 4000ms = 100，正文 300 / 4000ms = 75
    assert _speed_value(400, 300, 4000, 0, 0, 0) == (100.0, 75.0, False)
    # 路由口径缺项 → 回落估算口径（300 / 1000ms = 300，正文 200 / 1000ms = 200）
    assert _speed_value(0, 0, 0, 300, 200, 1000) == (300.0, 200.0, True)
    assert _speed_value(400, 300, 0, 0, 0, 0) == (None, None, False)
    assert _speed_value(400, 300, None, 0, 0, 0) == (None, None, False)
    assert _speed_value(0, 0, 4000, 300, 200, 1000) == (300.0, 200.0, True)
    assert _speed_value(None, None, 4000, None, None, 1000) == (None, None, False)
    assert _speed_value(0, 0, 0, 300, 200, None) == (None, None, False)
    assert _speed_value(0, 0, 0, 300, None, 1000) == (300.0, None, True)
    assert _speed_value(0, 0, 0, 0, 0, 0) == (None, None, False)
    # 正文分子为 0（整段输出都是思考 token）→ 正文列 None，不拿 0 冒充速度
    assert _speed_value(400, 0, 4000, 0, 0, 0) == (100.0, None, False)
    assert _speed_value(0, 0, 0, 300, 0, 1000) == (300.0, None, True)


def test_overview_output_speed_reports_visible_and_total(stats):
    """总览同时给正文速度与总产出速度：同分母，分子分别扣 / 不扣思考 token。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=1000, reasoning_tokens=750,
                     latency_ms=6000, ttfb_ms=1000)      # 生成 5.0s
    overview = query.overview(username="u")
    assert overview["tps"] == 200.0                     # 1000 / 5.0s
    assert overview["tps_visible"] == 50.0              # (1000-750) / 5.0s
    assert overview["tps_estimated"] is False


def test_overview_output_speed_visible_null_when_all_reasoning(stats):
    """整段输出都是思考 token 时正文速度为 None（前端显示 —），不是 0。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=600, reasoning_tokens=600,
                     latency_ms=4000, ttfb_ms=1000)
    overview = query.overview(username="u")
    assert overview["tps"] == 200.0
    assert overview["tps_visible"] is None


def test_overview_output_speed_drops_unmeasurable_generation_window(stats):
    """生成窗口 <200ms 的样本按「无法测量」剔除，不当成极快。

    上游把整段内容攒成一批吐时首帧与末帧几乎同时到达，`latency - ttfb` 只有
    几毫秒，比值会被放大成天文数字（实测 901 token / 3ms = 30 万 tok/s）。
    """
    collector, query = stats
    # 无法测量：131ms 生成 500 token（约 3800 tok/s，且窗口太短）
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=500, latency_ms=1131, ttfb_ms=1000)
    # 同一窗口内另一条正常样本：400 token / 4.0s = 100
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=400, latency_ms=5000, ttfb_ms=1000)
    assert query.overview(username="u")["tps"] == 100.0


def test_overview_output_speed_only_leaves_unmeasurable_samples(stats):
    """只剩不可测样本时按无样本处理（None），两个口径都不拿畸形比值顶上。"""
    collector, query = stats
    # 生成窗口 3ms（路由口径不可测），端到端 3000 token / 1200ms = 2500 tok/s
    # （估算口径也超过物理上限）→ 两个口径都没有合格样本。
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=3000, latency_ms=1200, ttfb_ms=1197)
    overview = query.overview(username="u")
    assert overview["tps"] is None and overview["tps_visible"] is None


def test_overview_output_speed_drops_implausibly_fast_samples(stats):
    """窗口够长但单条快过物理上限（>1000 tok/s）的样本同样剔除。

    这类样本与「批到达」同源：窗口被上游的分批节奏拉长了一点，但仍然远短于
    真实生成时长（2000 token / 300ms ≈ 6666 tok/s；端到端 2000 / 1300ms ≈ 1538
    也超上限，两个口径都该拒收）。
    """
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=2000, latency_ms=1300, ttfb_ms=1000)
    assert query.overview(username="u")["tps"] is None
    # 恰好 1000 tok/s（1000 token / 1000ms）算合格样本，上限是「不超过」
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=1000, latency_ms=2000, ttfb_ms=1000)
    overview = query.overview(username="u")
    assert overview["tps"] == 1000.0


def test_stats_groups_output_speed_reports_visible_and_sort(stats):
    """分组行同时下发正文 / 总产出速度，且两列都能独立排序。"""
    collector, query = stats
    collector.record(username="u", provider="trae", model="m", ok=True,
                     output_tokens=1000, reasoning_tokens=900,
                     latency_ms=6000, ttfb_ms=1000)       # 总 200，正文 20
    collector.record(username="u", provider="codebuddy", model="m", ok=True,
                     output_tokens=600, reasoning_tokens=0,
                     latency_ms=4000, ttfb_ms=1000)       # 总 200，正文 200
    rows = {row["provider"]: row for row in query.by_provider(username="u")}
    assert rows["trae"]["tps"] == 200.0 and rows["trae"]["tps_visible"] == 20.0
    assert rows["codebuddy"]["tps"] == 200.0 and rows["codebuddy"]["tps_visible"] == 200.0
    # 总产出相同 → 按分组键升序；正文速度排序把 codebuddy 排前
    assert [row["provider"] for row in
            query.by_provider(username="u", sort="tps_visible", order="desc")] == [
        "codebuddy", "trae"]
    # 估算口径也带正文速度
    collector.record(username="u", provider="zen", model="m", ok=True,
                     output_tokens=800, reasoning_tokens=600,
                     latency_ms=4000, ttfb_ms=None)       # 估算：总 200，正文 50
    zen = {row["provider"]: row for row in query.by_provider(username="u")}["zen"]
    assert zen["tps"] == 200.0 and zen["tps_visible"] == 50.0
    assert zen["tps_estimated"] is True


def test_events_rows_carry_per_request_speed(stats):
    """逐请求明细带本条速度：与分组同一套样本筛选，但逐行不回落估算口径。

    逐行展示拿不到相邻样本摊平首字等待，估算口径（分母含首字）在单条上只会
    误导，故没有生成窗口的行给 None（前端 —）。
    """
    from src.stats.query import row_speed

    collector, query = stats
    collector.record(username="u", provider="trae", model="routed", ok=True,
                     output_tokens=1000, reasoning_tokens=750,
                     latency_ms=6000, ttfb_ms=1000)            # 生成 5.0s：总 200 / 正文 50
    collector.record(username="u", provider="trae", model="short", ok=True,
                     output_tokens=99, latency_ms=6000, ttfb_ms=1000)      # 短输出也算
    collector.record(username="u", provider="trae", model="zero", ok=True,
                     output_tokens=0, latency_ms=6000, ttfb_ms=1000)       # 没产出：不算
    collector.record(username="u", provider="trae", model="batched", ok=True,
                     output_tokens=500, latency_ms=1100, ttfb_ms=1000)     # 窗口仅 100ms
    collector.record(username="u", provider="trae", model="implausible", ok=True,
                     output_tokens=2000, latency_ms=1400, ttfb_ms=1200)  # 窗口 200ms 却 4000 tok/s
    collector.record(username="u", provider="trae", model="all-reasoning", ok=True,
                     output_tokens=600, reasoning_tokens=600,
                     latency_ms=4000, ttfb_ms=1000)            # 正文为 0 → 正文列 None
    collector.record(username="u", provider="trae", model="failed", ok=False,
                     error_type="upstream_error", output_tokens=900,
                     latency_ms=5000, ttfb_ms=1000)            # 失败请求不算
    collector.record(username="u", provider="trae", model="no-ttfb", ok=True,
                     output_tokens=900, latency_ms=5000, ttfb_ms=None)     # 非流式：不估算
    rows = {row["model"]: row for row in query.events(username="u")["events"]}
    assert (rows["routed"]["tps"], rows["routed"]["tps_visible"]) == (200.0, 50.0)
    assert (rows["all-reasoning"]["tps"], rows["all-reasoning"]["tps_visible"]) == (200.0, None)
    # 短输出（99 token / 5.0s）照样给值：不设输出量下限
    assert (rows["short"]["tps"], rows["short"]["tps_visible"]) == (19.8, 19.8)
    for model in ("zero", "batched", "implausible", "failed", "no-ttfb"):
        assert (rows[model]["tps"], rows[model]["tps_visible"]) == (None, None)
    # SQL 列与实时流用的 Python 版逐行同值（同一 `_routed_guard`）
    for row in rows.values():
        assert (row["tps_visible"], row["tps"]) == row_speed(
            ok=row["ok"], output_tokens=row["output_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            latency_ms=row["latency_ms"], ttfb_ms=row["ttfb_ms"])


def test_events_sort_by_speed_keys(stats):
    """明细可按逐请求速度排序：正文与总产出各自为键，不可测的行排最后。"""
    collector, query = stats
    # 同一生成窗口 2.0s：总产出 500 / 正文 100（思考占大半）
    collector.record(username="u", provider="trae", model="reasoning", ok=True,
                     output_tokens=1000, reasoning_tokens=800,
                     latency_ms=3000, ttfb_ms=1000)
    # 总产出 300 / 正文 300（无思考）
    collector.record(username="u", provider="trae", model="plain", ok=True,
                     output_tokens=600, latency_ms=3000, ttfb_ms=1000)
    # 窗口不可测 → 两列都是 NULL
    collector.record(username="u", provider="trae", model="unmeasurable", ok=True,
                     output_tokens=900, latency_ms=2000, ttfb_ms=1900)
    by_total = query.events(username="u", sort="tps", order="desc")
    assert [row["model"] for row in by_total["events"]] == [
        "reasoning", "plain", "unmeasurable"]
    # 换列排序走 offset 分页，故额外下发 total（游标模式没有）
    assert by_total["total"] == 3
    assert [row["model"] for row in
            query.events(username="u", sort="tps_visible", order="desc")["events"]] == [
        "plain", "reasoning", "unmeasurable"]


def test_row_speed_python_matches_sql_guards():
    """实时流那侧的 Python 版逐行速度：同规则、逐分支。"""
    from src.stats.query import row_speed

    # 输出 1000 / 生成 5.0s = 200；正文 (1000 − 750) = 50
    assert row_speed(ok=True, output_tokens=1000, reasoning_tokens=750,
                     latency_ms=6000, ttfb_ms=1000) == (50.0, 200.0)
    # 正文为 0（整段思考）→ 正文列 None；思考大于输出时折 0（上游偶发）
    assert row_speed(ok=True, output_tokens=600, reasoning_tokens=600,
                     latency_ms=4000, ttfb_ms=1000) == (None, 200.0)
    assert row_speed(ok=True, output_tokens=600, reasoning_tokens=900,
                     latency_ms=4000, ttfb_ms=1000) == (None, 200.0)
    # 失败请求 / 缺字段（历史行可能没有输出、耗时或首字）
    assert row_speed(ok=False, output_tokens=1000, reasoning_tokens=0,
                     latency_ms=6000, ttfb_ms=1000) == (None, None)
    assert row_speed(ok=True, output_tokens=None, reasoning_tokens=None,
                     latency_ms=6000, ttfb_ms=1000) == (None, None)
    assert row_speed(ok=True, output_tokens=1000, reasoning_tokens=None,
                     latency_ms=None, ttfb_ms=1000) == (None, None)
    assert row_speed(ok=True, output_tokens=1000, reasoning_tokens=None,
                     latency_ms=6000, ttfb_ms=None) == (None, None)
    # 没产出 / 生成窗口过短 / 快过物理上限
    assert row_speed(ok=True, output_tokens=0, reasoning_tokens=0,
                     latency_ms=6000, ttfb_ms=1000) == (None, None)
    assert row_speed(ok=True, output_tokens=500, reasoning_tokens=0,
                     latency_ms=1100, ttfb_ms=1000) == (None, None)
    assert row_speed(ok=True, output_tokens=2000, reasoning_tokens=0,
                     latency_ms=1400, ttfb_ms=1200) == (None, None)
