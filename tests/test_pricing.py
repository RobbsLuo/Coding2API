"""成本估算（models.dev 刊例价）与统计成本列。

覆盖：价表构建（原厂优先 / 最高价回退 / 坏条目）、单请求估算（缓存夹取、
缺 token / 缺模型降级）、汇率换算（非法汇率兜底）、落盘快照（版本 / 过期 /
损坏）、采集器写入与查询聚合（overview / by_provider / timeline / events）、
后台价表刷新任务装配。
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.pricing import (
    PRICES_FILENAME,
    PRICES_MAX_AGE_SECONDS,
    PRICES_VERSION,
    build_price_table,
    estimate_cost_usd,
    fetch_prices,
    load_prices,
    prices_path,
    save_prices,
    to_cny,
)
from src.runtime_settings import HOT_BY_KEY, RuntimeSettings
from src.stats.collector import StatsCollector
from src.stats.query import StatsQuery
from tests.conftest import SECRET

# ------------------------------------------------------------------ 价表构建


def test_build_price_table_prefers_vendor_over_higher_priced_reseller():
    """有原厂 provider（canonical 前缀 == provider id）时优先它，哪怕别人更贵。"""
    raw = {
        "zhipuai": {"models": {"glm-5.2": {
            "id": "glm-5.2", "cost": {"input": 1.4, "output": 4.4, "cache_read": 0.26}}}},
        "reseller": {"models": {"glm-5.2": {
            "id": "glm-5.2", "cost": {"input": 9.9, "output": 9.9},
            "canonical_model_id": "zhipuai/glm-5.2"}}},
    }
    # zhipuai 条目没有 canonical（上游没填），但它是 key 命中的 id；reseller 标了
    # canonical 指向 zhipuai → 原厂是 zhipuai。这里 zhipuai 缺 canonical 故不被
    # 认作 vendor，回退到 input 最高（reseller 9.9）——验证「缺 canonical 不猜」。
    table = build_price_table(raw)
    assert table["glm-5.2"] == (9.9, 9.9, 9.9)


def test_build_price_table_vendor_priority_when_canonical_present():
    """canonical 前缀 == provider id → 判为原厂，优先于高价转售。"""
    raw = {
        "zhipuai": {"models": {"glm-5.2": {
            "id": "glm-5.2", "canonical_model_id": "zhipuai/glm-5.2",
            "cost": {"input": 1.4, "output": 4.4, "cache_read": 0.26}}}},
        "reseller": {"models": {"glm-5.2": {
            "id": "glm-5.2", "canonical_model_id": "zhipuai/glm-5.2",
            "cost": {"input": 9.9, "output": 9.9}}}},
    }
    table = build_price_table(raw)
    assert table["glm-5.2"] == (1.4, 4.4, 0.26)  # 原厂 zhipuai


def test_build_price_table_fallback_picks_highest_input_and_fills_cache():
    """无原厂时取 input 最高者；cache_read 缺失按 input 原价计（不打折也不免费）。"""
    raw = {
        "cheap": {"models": {"m": {"id": "m", "cost": {"input": 1, "output": 2}}}},
        "pricey": {"models": {"m": {"id": "m", "cost": {"input": 3}}}},
        "tie": {"models": {"m": {"id": "m", "cost": {"input": 3}}}},
    }
    table = build_price_table(raw)
    # input 3 打平按 provider id 升序（pricey < tie），取 pricey；output 缺失 → 0
    assert table["m"] == (3.0, 0.0, 3.0)


@pytest.mark.parametrize("raw", [None, [], "x", 5])
def test_build_price_table_non_dict_is_empty(raw):
    assert build_price_table(raw) == {}


def test_build_price_table_skips_bad_entries():
    """结构异常一律跳过：provider 非 dict / models 非 dict / cost 非 dict /
    input 缺失或非数值 / id 非串且 key 也非串。"""
    raw = {
        "ok": {"models": {
            "good": {"id": "good", "cost": {"input": 1, "output": 2}},
            "noid": {"cost": {"input": 1}},                 # 无 id：回落 key "noid"
            "nocost": {"id": "nocost"},                     # 无 cost
            "costnotdict": {"id": "cnd", "cost": "x"},      # cost 非 dict
            "badid": {"id": 5, "cost": {"input": 1}},       # id 非串 → 跳过
            "noinput": {"id": "ni", "cost": {"output": 1}},  # input 缺失
            "nullinput": {"id": "nul", "cost": {"input": None}},
            "neginput": {"id": "neg", "cost": {"input": -1}},
            "boolinput": {"id": "bool", "cost": {"input": True}},
            "notext": "not-a-dict",
        }},
        "badd": 5,
        "badmodels": {"models": "nope"},
    }
    table = build_price_table(raw)
    assert set(table) == {"good", "noid"}
    assert table["noid"] == (1.0, 0.0, 1.0)


def test_build_price_table_ignores_canonical_without_slash():
    """canonical 无 '/' 时不判为原厂（_is_vendor 返回 False）。"""
    raw = {
        "a": {"models": {"m": {"id": "m", "canonical_model_id": "noslash",
                               "cost": {"input": 1}}}},
        "b": {"models": {"m": {"id": "m", "cost": {"input": 2}}}},
    }
    assert build_price_table(raw)["m"] == (2.0, 0.0, 2.0)


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


# ------------------------------------------------------------------ 落盘快照


def test_prices_roundtrip(tmp_path):
    save_prices(str(tmp_path), {"m": (1.0, 2.0, 0.5)})
    assert load_prices(str(tmp_path)) == {"m": (1.0, 2.0, 0.5)}
    assert prices_path(str(tmp_path)).endswith(PRICES_FILENAME)


def test_prices_missing_file_is_empty(tmp_path):
    assert load_prices(str(tmp_path)) == {}


def test_prices_version_mismatch_is_empty(tmp_path, caplog):
    save_prices(str(tmp_path), {"m": (1.0, 2.0, 0.5)})
    path = prices_path(str(tmp_path))
    payload = json.loads((tmp_path / PRICES_FILENAME).read_text(encoding="utf-8"))
    payload["version"] = PRICES_VERSION + 1
    (tmp_path / PRICES_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_prices(str(tmp_path)) == {}
    assert any("价表版本不匹配" in r.getMessage() for r in caplog.records)
    assert path is not None


def test_prices_expired_is_empty(tmp_path):
    save_prices(str(tmp_path), {"m": (1.0, 2.0, 0.5)})
    assert load_prices(str(tmp_path),
                       now=time.time() + PRICES_MAX_AGE_SECONDS + 10) == {}


def test_prices_broken_json_is_empty(tmp_path, caplog):
    (tmp_path / PRICES_FILENAME).write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_prices(str(tmp_path)) == {}
    assert any("价表读取失败" in r.getMessage() for r in caplog.records)


def test_prices_skips_bad_rows(tmp_path):
    """坏行跳过而不拖垮整表：非 list / 长度不对 / 含非数值。"""
    payload = {
        "version": PRICES_VERSION, "saved_at": time.time(), "currency": "USD",
        "prices": {
            "good": [1, 2, 3],
            "notlist": "x",
            "wronglen": [1, 2],
            "hasnull": [1, None, 3],
            "neg": [1, -2, 3],
            "notanumber": [1, "x", 3],
            "boolval": [1, True, 3],
        },
    }
    (tmp_path / PRICES_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_prices(str(tmp_path)) == {"good": (1.0, 2.0, 3.0)}


def test_prices_saved_at_missing_is_error(tmp_path, caplog):
    payload = {"version": PRICES_VERSION, "currency": "USD",
               "prices": {"m": [1, 2, 3]}}
    (tmp_path / PRICES_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_prices(str(tmp_path)) == {}
    assert any("价表读取失败" in r.getMessage() for r in caplog.records)


def test_prices_prices_not_dict(tmp_path):
    payload = {"version": PRICES_VERSION, "saved_at": time.time(), "prices": []}
    (tmp_path / PRICES_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_prices(str(tmp_path)) == {}


def test_save_prices_failure_is_logged(tmp_path, caplog):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir")
    with caplog.at_level("WARNING"):
        save_prices(str(blocker), {"m": (1.0, 2.0, 3.0)})
    assert any("价表落盘失败" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ 网络拉取


async def test_fetch_prices_builds_table_via_mock_transport():
    raw = {"zhipuai": {"models": {"glm-5.2": {
        "id": "glm-5.2", "cost": {"input": 1.4, "output": 4.4}}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw)

    table = await fetch_prices("https://models.dev/api.json",
                               transport=httpx.MockTransport(handler))
    assert table == {"glm-5.2": (1.4, 4.4, 1.4)}


async def test_fetch_prices_propagates_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_prices("https://x", transport=httpx.MockTransport(handler))


async def test_fetch_prices_default_transport_branch(monkeypatch):
    """不注入 transport 时走真实 AsyncClient 构造（此处用替身覆盖该分支）。"""
    import httpx

    class FakeClient:
        def __init__(self, **_kwargs):
            self.kwargs = _kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url):
            return httpx.Response(
                200, json={"p": {"models": {"m": {"id": "m", "cost": {"input": 1, "output": 2}}}}},
                request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    table = await fetch_prices("https://x")
    assert table == {"m": (1.0, 2.0, 1.0)}


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


# ------------------------------------------------------------------ 配置热更


def test_usd_cny_rate_is_hot_setting(tmp_path):
    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import load_runtime_settings

    db = Database(tmp_path / "r.sqlite3")
    apply_schema(db.connect())
    config = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                      USD_CNY_RATE=7.0)
    runtime: RuntimeSettings = load_runtime_settings(
        config, RuntimeSettingsRepository(db))
    assert runtime.usd_cny_rate == 7.0
    runtime.set("usd_cny_rate", 6.5)
    assert runtime.usd_cny_rate == 6.5
    runtime.reset("usd_cny_rate")
    assert runtime.usd_cny_rate == 7.0
    db.close()


def test_price_catalog_minutes_has_floor():
    spec = HOT_BY_KEY["price_catalog_minutes"]
    assert spec.task == "price_catalog"
    assert spec.floor == 60


# ------------------------------------------------------------------ 应用装配


class _PriceStub:
    """最小 provider：只满足启动预热对 list_models 的调用。"""

    id = "kilo"

    async def list_models(self, _data):
        return []


def _price_app(tmp_path, **env):
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", **env)
    return build_app(settings, providers={"kilo": _PriceStub()})


def test_app_restores_price_snapshot_and_estimates_cost(tmp_path):
    """启动回灌落盘价表：新写入的明细立刻能估出成本（零上游请求）。"""
    from fastapi.testclient import TestClient

    save_prices(str(tmp_path), {"glm-5.2": (1.0, 2.0, 0.1)})
    app = _price_app(tmp_path)
    with TestClient(app):
        assert app.state.price_table == {"glm-5.2": (1.0, 2.0, 0.1)}
        app.state.stats_collector.record(
            username="u", provider="trae", model="glm-5.2", ok=True,
            input_tokens=1_000_000, output_tokens=0)
        row = app.state.stats_collector._db.connect().execute(
            "SELECT cost_usd FROM usage_events").fetchone()
    assert row["cost_usd"] == pytest.approx(1.0)


def test_app_restore_price_failure_is_logged(tmp_path, monkeypatch, caplog):
    """恢复价表抛异常只记日志，服务照常启动。"""
    from fastapi.testclient import TestClient

    import src.main as main

    def boom(_data_dir):
        raise RuntimeError("bad snapshot")

    monkeypatch.setattr(main, "load_prices", boom)
    app = _price_app(tmp_path)
    with caplog.at_level("WARNING"), TestClient(app):
        assert app.state.price_table == {}
    assert any("恢复落盘价表失败" in r.getMessage() for r in caplog.records)


async def test_price_catalog_refresh_reports_count(tmp_path, monkeypatch):
    """后台价表刷新一轮：换入新表 + 落盘 + 只回报条目数。"""
    import src.main as main

    async def fake_fetch(_url):
        return {"glm-5.2": (1.0, 2.0, 0.1), "glm-4.6": (0.5, 1.0, 0.1)}

    monkeypatch.setattr(main, "fetch_prices", fake_fetch)
    app = _price_app(tmp_path)
    async with app.router.lifespan_context(app):
        runner = app.state.task_runner
        assert await runner._guarded(runner._price_catalog(), "价表刷新",
                                     key="price_catalog")
        run = runner.status.get("price_catalog")
        assert run.ok is True and run.report == {"models": 2}
    assert app.state.price_table == {"glm-5.2": (1.0, 2.0, 0.1),
                                     "glm-4.6": (0.5, 1.0, 0.1)}
    assert load_prices(str(tmp_path)) == app.state.price_table


async def test_price_catalog_refresh_empty_table_raises(tmp_path, monkeypatch):
    """上游返回空表视为失败（不覆盖已有价表）：宁可保留旧表也不清空。"""
    import src.main as main

    async def empty(_url):
        return {}

    monkeypatch.setattr(main, "fetch_prices", empty)
    app = _price_app(tmp_path)
    app.state.price_table["glm-5.2"] = (1.0, 2.0, 0.1)
    async with app.router.lifespan_context(app):
        runner = app.state.task_runner
        assert await runner._guarded(runner._price_catalog(), "价表刷新",
                                     key="price_catalog") is False
    assert app.state.price_table == {"glm-5.2": (1.0, 2.0, 0.1)}


def test_price_catalog_task_status_and_interval(tmp_path):
    from fastapi.testclient import TestClient

    app = _price_app(tmp_path)
    with TestClient(app):
        status = {item["key"]: item for item in app.state.task_runner.task_status()}
    assert status["price_catalog"]["interval_seconds"] == 86400
    assert status["price_catalog"]["enabled"] is True
    assert status["price_catalog"]["last_ok"] is None


def test_build_runner_without_price_catalog_keeps_task_hidden(tmp_path):
    """不注入刷新协程时不装配这条循环（老调用方/测试保持原行为）。"""
    from src.tasks.runner import build_runner

    db = Database(tmp_path / "c.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    config = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                      PRICE_CATALOG_MINUTES=1)
    runner = build_runner(credentials, {}, None, config)
    keys = {item["key"] for item in runner.task_status()}
    assert "price_catalog" not in keys
    assert runner._price_catalog_interval == 3600      # 下限 60 分钟
    db.close()


# --------------------------------------------------------- 启动预热（无快照时补拉）

async def test_warm_price_table_fetches_when_empty():
    """无落盘快照时立即补拉一次，避免首次部署成本空窗到下一轮。"""
    from src.main import _warm_price_table

    called = []

    async def refresh():
        called.append(True)
        return {"models": 3}

    await _warm_price_table(refresh, {})
    assert called == [True]


async def test_warm_price_table_skips_when_snapshot_present():
    """已有快照就交给周期任务，不重复拉（models.dev 是数 MB 大表）。"""
    from src.main import _warm_price_table

    called = []

    async def refresh():
        called.append(True)
        return {"models": 3}

    await _warm_price_table(refresh, {"glm-5.2": (1.0, 2.0, 0.1)})
    assert called == []


async def test_warm_price_table_swallows_fetch_error(caplog):
    """补拉失败只记日志：成本显示 — 而已，不影响启动与聊天。"""
    from src.main import _warm_price_table

    async def boom():
        raise RuntimeError("network down")

    with caplog.at_level("WARNING"):
        await _warm_price_table(boom, {})
    assert any("启动预热价表失败" in r.getMessage() for r in caplog.records)


def test_shutdown_cancels_inflight_price_warmup(tmp_path, monkeypatch):
    """关机时价表预热若仍在飞，取消它，别把上游请求带出事件循环。"""
    from fastapi.testclient import TestClient

    from src import main

    started = asyncio.Event()
    cancelled = []

    async def hang(_url):
        started.set()
        try:
            await asyncio.Event().wait()        # 永不返回，直到被取消
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(main, "fetch_prices", hang)
    app = _price_app(tmp_path)
    with TestClient(app):
        # 预热在后台线程的事件循环里跑，轮询等它进入抓取（避免时序竞态）
        deadline = time.monotonic() + 2
        while not started.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.is_set()                 # 预热已进入抓取
    assert cancelled == [True]                  # 关机时被取消

