"""列表排序（src/sorting.py + 各仓储 / 查询 / 端点的 sort 参数）。

守着四条契约：
1. `sort` / `order` 一律走白名单——未知键、非法方向回落到端点默认，不报错。
2. 缺省（不传参）行为与引入排序前完全一致（默认顺序不变）。
3. `asc` / `desc` 都生效；SQL 列表用 `id`/`rowid` 兜底稳定次序。
4. `None` 值（未探测 / 无到期信息）无论升降序都排最后。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CREDENTIAL_SORT_KEYS, CredentialRepository
from src.main import build_app
from src.provider.base import Quota
from src.sorting import parse_sort_order, sort_rows, sql_order
from src.stats.collector import StatsCollector
from src.stats.query import StatsQuery
from tests.conftest import SECRET

ALLOWED = {"a": "a", "b": "b"}


def test_parse_sort_order_falls_back_on_unknown_key_and_order():
    # 命中白名单 + 合法方向
    assert parse_sort_order("b", "asc", ALLOWED, default_key="a", default_desc=True) == ("b", False)
    assert parse_sort_order("b", "desc", ALLOWED, default_key="a",
                            default_desc=False) == ("b", True)
    # 未知键 → 回落默认键；缺省方向 → 回落默认方向
    assert parse_sort_order("nope", None, ALLOWED, default_key="a",
                            default_desc=True) == ("a", True)
    assert parse_sort_order(None, "sideways", ALLOWED, default_key="a",
                            default_desc=False) == ("a", False)


def test_sql_order_builds_clause_and_tiebreak():
    assert sql_order("a", "asc", ALLOWED, default_key="a", default_desc=True) == "a ASC"
    assert sql_order("a", "asc", ALLOWED, default_key="a", default_desc=True,
                     tiebreak="id") == "a ASC, id ASC"
    # 与主列同名的 tiebreak 自动省略（避免 "a ASC, a ASC"）
    assert sql_order("a", "asc", {"a": "a"}, default_key="a", default_desc=True,
                     tiebreak="a") == "a ASC"
    # 默认方向回落（desc 默认）
    assert sql_order(None, None, ALLOWED, default_key="a", default_desc=True) == "a DESC"


def test_sort_rows_none_always_last():
    rows = [{"k": 3}, {"k": None}, {"k": 1}, {"k": 2}]
    allowed = {"k": lambda row: row["k"]}
    asc = sort_rows(rows, "k", "asc", allowed, default_key="k", default_desc=False)
    assert [r["k"] for r in asc] == [1, 2, 3, None]
    desc = sort_rows(rows, "k", "desc", allowed, default_key="k", default_desc=False)
    assert [r["k"] for r in desc] == [3, 2, 1, None]
    # 未知键回落默认（同样升序）
    fallback = sort_rows(rows, "ghost", None, allowed, default_key="k", default_desc=False)
    assert [r["k"] for r in fallback] == [1, 2, 3, None]


def test_credential_list_all_sort_keys():
    """凭证列表：各白名单键都可用；派生字段（到期额度）也能排。"""
    db = Database(":memory:")
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))
    low = repo.add(provider="trae", credential_data={"accessToken": "a"},
                   nickname="bravo", now=1)
    high = repo.add(provider="codebuddy", credential_data={"bearer_token": "b"},
                    nickname="alpha", now=2)
    repo.save_quota(low, Quota(remaining=10, total=100, probed_at=5,
                              expiry_ladder=[(1000, 30)]))
    repo.save_quota(high, Quota(remaining=90, total=100, probed_at=6))

    def ids(rows):
        return [r["id"] for r in rows]

    # 默认：created_at 升序（与历史行为一致），不受传参影响
    assert ids(repo.list_all()) == [low, high]
    assert ids(repo.list_all(sort=None, order=None)) == [low, high]
    # 昵称升序 / 降序
    assert ids(repo.list_all(sort="nickname", order="asc")) == [high, low]
    assert ids(repo.list_all(sort="nickname", order="desc")) == [low, high]
    # 额度剩余降序（high 更多）
    assert ids(repo.list_all(sort="quota_remaining", order="desc")) == [high, low]
    # 到期额度：high 无到期信息（None）→ 无论升降序都排最后
    assert ids(repo.list_all(sort="quota_expiring_credits",
                             order="asc")) == [low, high]
    assert ids(repo.list_all(sort="quota_expiring_credits",
                             order="desc")) == [low, high]
    # 未知键回落默认键（created_at），但 order 仍按传入生效
    assert ids(repo.list_all(sort="ghost", order="desc")) == [high, low]
    # 白名单里的每个键都至少能被选中（覆盖取值函数）
    for key in CREDENTIAL_SORT_KEYS:
        assert len(repo.list_all(sort=key, order="asc")) == 2
    db.close()


def test_group_stats_default_is_requests_desc():
    """分组统计默认按请求数降序（不再按分组键字母序）；asc/desc 均可覆盖。"""
    db = Database(":memory:")
    apply_schema(db.connect())
    collector, query = StatsCollector(db), StatsQuery(db)
    for _ in range(3):
        collector.record(username="u", provider="zen", model="m", ok=True)
    collector.record(username="u", provider="trae", model="m", ok=True)

    # 默认：请求数降序 → zen(3) 在 trae(1) 前，与字母序（trae < zen）相反
    default = query.by_provider(username="u")
    assert [r["provider"] for r in default] == ["zen", "trae"]
    # 显式升序请求数
    asc = query.by_provider(username="u", sort="requests", order="asc")
    assert [r["provider"] for r in asc] == ["trae", "zen"]
    # 按分组键排序
    by_key = query.by_provider(username="u", sort="group", order="asc")
    assert [r["provider"] for r in by_key] == ["trae", "zen"]
    db.close()


def test_group_events_default_desc_and_offset_path():
    """凭证分组（走明细表）也按请求数降序）。"""
    db = Database(":memory:")
    apply_schema(db.connect())
    collector, query = StatsCollector(db), StatsQuery(db)
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id="c1")
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id="c2")
    collector.record(username="u", provider="trae", model="m", ok=True,
                     credential_id="c2")
    rows = query.by_credential(username="u", sort="requests", order="desc")
    assert [r["credential_id"] for r in rows] == ["c2", "c1"]
    db.close()


def test_stats_events_by_column_uses_offset_pagination():
    """按非 rowid 列排序 → offset 分页：返回 total、next_before 恒 null。"""
    db = Database(":memory:")
    apply_schema(db.connect())
    collector, query = StatsCollector(db), StatsQuery(db)
    for index in range(4):
        collector.record(username="u", provider="trae", model=f"m{index}",
                         ok=True, now=1000 + index)
    # 默认（rowid 降序）走游标模式：无 total
    cursor = query.events(limit=2)
    assert cursor["total"] is None and cursor["next_before"] is not None
    # 按 model 升序 → offset 模式
    page = query.events(sort="model", order="asc", limit=2, offset=0)
    assert page["total"] == 4 and page["next_before"] is None
    assert [e["model"] for e in page["events"]] == ["m0", "m1"]
    page2 = query.events(sort="model", order="asc", limit=2, offset=2)
    assert [e["model"] for e in page2["events"]] == ["m2", "m3"]
    # 时间升序（rowid 升序）也切到 offset 模式
    asc = query.events(sort="time", order="asc")
    assert asc["total"] == 4 and [e["model"] for e in asc["events"]] == ["m0", "m1", "m2", "m3"]
    db.close()


def test_list_endpoints_accept_sort_params(tmp_path):
    """端到端：各列表端点接受 sort/order，未知键回落且不报错。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    credentials = app.state.credentials
    credentials.add(provider="trae", credential_data={"accessToken": "a"}, nickname="zzz")
    credentials.add(provider="trae", credential_data={"accessToken": "b"}, nickname="aaa")
    collector = app.state.stats_collector
    collector.record(username="root", provider="trae", model="m", ok=True)
    collector.rollup_hourly()
    app.state.services.alerts.record(rule="pool_empty", severity="critical", scope="pool",
                                     message="空池", now=1)
    app.state.audit.record(actor="root", action="login.success", now=1)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        creds = client.get("/api/credentials", params={"sort": "nickname",
                                                       "order": "asc"}).json()
        names = [c["nickname"] for c in creds["credentials"]]
        # 种子（zen/kilo）也在池里：只断言我们加的两条相对次序正确
        assert names.index("aaa") < names.index("zzz")
        # 未知键回落默认，200 而非报错
        assert client.get("/api/credentials", params={"sort": "ghost"}).status_code == 200
        assert client.get("/api/users", params={"sort": "created_at"}).status_code == 200
        assert client.get("/api/audit", params={"sort": "actor", "order": "asc"}).status_code == 200
        assert client.get("/api/alerts", params={"sort": "severity"}).status_code == 200
        assert client.get("/api/model-catalog",
                          params={"sort": "provider"}).status_code == 200
        assert client.get("/api/stats/by-provider",
                          params={"sort": "requests", "order": "asc"}).status_code == 200
        events = client.get("/api/stats/events",
                            params={"sort": "model", "order": "asc"}).json()
        assert events["total"] == 1 and events["next_before"] is None
