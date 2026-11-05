"""实测扣费校准：目录 x0 vs `usage_events` 里的真实扣费。"""

from __future__ import annotations

import time

import pytest

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.paid_models import (
    OBSERVATION_WINDOW_SECONDS,
    SNAPSHOT_TTL_SECONDS,
    PaidModelRepository,
)
from src.stats.collector import StatsCollector


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "paid.sqlite3")
    apply_schema(database.connect())
    yield database
    database.close()


def _record(db, *, provider="codebuddy_intl", model="deepseek-v4.1-flash",
            credit=0.14, estimated=False, now=None) -> None:
    StatsCollector(db).record(
        username="u", provider=provider, model=model, ok=True,
        credit=credit, credit_estimated=estimated,
        now=int(now if now is not None else time.time()))


def test_observed_rates_only_counts_upstream_reported_credit(db):
    """只认上游明确扣费：推算值（credit_estimated=1）与 0 都不算证据。"""
    now = int(time.time())
    _record(db, credit=0.2, now=now)
    _record(db, credit=0.4, now=now)
    # 推算值：TRAE 按价表反解，不是上游报的
    _record(db, provider="trae", credit=5.0, estimated=True, now=now)
    # 明确报 0：真免费，不是收费证据
    _record(db, provider="zen", credit=0.0, now=now)
    # credit 缺失
    _record(db, provider="kilo", credit=None, now=now)

    rates = PaidModelRepository(db).observed_rates()
    assert rates == {("codebuddy_intl", "deepseek-v4.1-flash"): pytest.approx(0.3)}


def test_observed_rates_window_excludes_stale_rows(db):
    """窗口外的历史扣费不算：上游改回免费后不能一直被当收费晾着。"""
    now = int(time.time())
    _record(db, now=now - OBSERVATION_WINDOW_SECONDS - 60)
    assert PaidModelRepository(db).observed_rates() == {}
    _record(db, now=now)
    assert PaidModelRepository(db).observed_rates() == {
        ("codebuddy_intl", "deepseek-v4.1-flash"): pytest.approx(0.14)}


def test_observed_rates_merges_raw_id_variants(db):
    """同一归一键的多种写法（大小写 / free 尾缀）先累加再平均。"""
    now = int(time.time())
    _record(db, model="DeepSeek-V4.1-Flash", credit=0.2, now=now)
    _record(db, model="deepseek-v4.1-flash", credit=0.4, now=now)
    _record(db, model="deepseek-v4.1-flash-free", credit=0.6, now=now)
    assert PaidModelRepository(db).observed_rates() == {
        ("codebuddy_intl", "deepseek-v4.1-flash"): pytest.approx(0.4)}


def test_observed_rates_snapshot_reused_until_ttl(db):
    """快照按 TTL 复用：选号是热路径，不能每个请求都做一次聚合。"""
    clock = [1000.0]
    repo = PaidModelRepository(db, now_fn=lambda: clock[0])
    now = int(time.time())
    _record(db, credit=0.2, now=now)
    assert repo.observed_rates() == {
        ("codebuddy_intl", "deepseek-v4.1-flash"): pytest.approx(0.2)}

    # TTL 内新增的扣费看不到（快照命中）
    _record(db, credit=0.4, now=now)
    assert repo.observed_rates() == {
        ("codebuddy_intl", "deepseek-v4.1-flash"): pytest.approx(0.2)}

    clock[0] += SNAPSHOT_TTL_SECONDS
    assert repo.observed_rates() == {
        ("codebuddy_intl", "deepseek-v4.1-flash"): pytest.approx(0.3)}

    # invalidate 立即失效（刚写完明细的调用方）
    _record(db, credit=0.6, now=now)
    repo.invalidate()
    assert repo.observed_rates() == {
        ("codebuddy_intl", "deepseek-v4.1-flash"): pytest.approx(0.4)}
