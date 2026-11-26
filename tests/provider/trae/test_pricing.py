"""TRAE 单请求积分推算、回填与小时汇总。"""

from __future__ import annotations

import pytest

from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import ApiKeyRepository, CredentialRepository
from src.provider.base import Event, EventKind, Usage
from tests.helpers.constants import SECRET


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "e.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), ApiKeyRepository(db), db
    db.close()

def _insert_event(db, *, id, ts, provider, model, input_tokens, output_tokens,
                  cached_tokens=None, credit=None, credit_estimated=0):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, ok,"
            " input_tokens, output_tokens, cached_tokens, credit, credit_estimated)"
            " VALUES (?,?,?,?,?,?,1,?,?,?,?,?)",
            (id, ts, "u", provider, None, model, input_tokens, output_tokens,
             cached_tokens, credit, credit_estimated))

def test_pricing_estimate_credit_official_formula():
    """官方公式：积分 = (输入−缓存)×输入价 + 输出×输出价 + 缓存×缓存价（积分/百万）。"""
    from src.provider.trae import pricing

    # qwen-3.7-plus 官方 2/8/0.4 元/百万，×40 = 80/320/16 积分/百万，无折扣
    assert pricing.estimate_credit(
        "qwen-3.7-plus", input_tokens=1_000_000, output_tokens=0) == 80.0
    assert pricing.estimate_credit(
        "qwen-3.7-plus", input_tokens=0, output_tokens=1_000_000) == 320.0
    # 缓存命中的部分按缓存价计，不再按输入价计
    assert pricing.estimate_credit(
        "qwen-3.7-plus", input_tokens=1_000_000, output_tokens=0,
        cached_tokens=1_000_000) == 16.0
    # 混合：输入 1M 其中 0.5M 命中 + 输出 0.5M
    assert pricing.estimate_credit(
        "qwen-3.7-plus", input_tokens=1_000_000, output_tokens=500_000,
        cached_tokens=500_000) == round((500_000 * 80 + 500_000 * 320 + 500_000 * 16) / 1e6, 4)

def test_pricing_measured_discount_applied():
    """实测折扣模型：官方价 × 折扣（glm-5.2 输入/输出/缓存一致 0.675）。"""
    from src.provider.trae import pricing

    # glm-5.2 官方 8/28/2 元 → 320/1120/80，×0.675 = 216/756/54
    assert pricing.estimate_credit(
        "glm-5.2", input_tokens=1_000_000, output_tokens=0) == 216.0
    assert pricing.estimate_credit(
        "glm-5.2", input_tokens=0, output_tokens=1_000_000) == 756.0
    assert pricing.effective_prices("glm-5.2") == (216.0, 756.0, 54.0)

def test_pricing_measured_effective_override():
    """实测有效价覆盖：deepseek-v4.1-flash 缓存价是刊例的 ~5 倍，用实测值。"""
    from src.provider.trae import pricing

    # 刊例 2/8/0.04 元/M 会给出缓存价 0.56；实测有效价是 2.8（≈0.07 元/M）
    assert pricing.effective_prices("deepseek-v4.1-flash") == (28.0, 112.0, 2.8)
    assert pricing.estimate_credit(
        "deepseek-v4.1-flash", input_tokens=1_000_000, output_tokens=0) == 28.0
    assert pricing.estimate_credit(
        "deepseek-v4.1-flash", input_tokens=0, output_tokens=1_000_000) == 112.0
    # 全命中按缓存价 2.8/百万，而不是刊例的 0.56
    assert pricing.estimate_credit(
        "deepseek-v4.1-flash", input_tokens=1_000_000, output_tokens=0,
        cached_tokens=1_000_000) == 2.8
    # 大小写 / 空白归一后同样命中覆盖
    assert pricing.effective_prices("  DeepSeek-V4.1-Flash ") == (28.0, 112.0, 2.8)
    # doubao-seed-2.1-pro 也走覆盖（实测约为刊例 0.104）
    assert pricing.effective_prices("Doubao-Seed-2.1-Pro") == (24.87, 124.4, 4.93)

def test_pricing_unknown_model_and_missing_input():
    """未收录模型 / 缺输入 token → None（不推算，展示层保持 —）。"""
    from src.provider.trae import pricing

    assert pricing.effective_prices("sagitta") is None
    assert pricing.estimate_credit("sagitta", input_tokens=100, output_tokens=10) is None
    assert pricing.estimate_credit("glm-5.2", input_tokens=None, output_tokens=10) is None
    # bool 不是合法 token 数（int 子类陷阱）
    assert pricing.estimate_credit("glm-5.2", input_tokens=True, output_tokens=1) is None

def test_pricing_cached_clamped_and_missing_output():
    """缓存 > 输入时按输入截断；输出缺失按 0；缓存缺失按 0。"""
    from src.provider.trae import pricing

    assert pricing.estimate_credit(
        "glm-5.2", input_tokens=1_000_000, output_tokens=0,
        cached_tokens=9_999_999) == 216.0 * 0.25   # 全命中按缓存价 54/百万
    assert pricing.estimate_credit(
        "glm-5.2", input_tokens=1_000_000, output_tokens=None) == 216.0
    assert pricing.estimate_credit(
        "glm-5.2", input_tokens=1_000_000, output_tokens=0,
        cached_tokens=True) == 216.0               # bool 缓存值视为缺失

def test_fill_estimated_credit_only_when_missing():
    """USAGE 事件补推算：有上游 credit 不覆盖；非 USAGE 不动；未收录模型不动。"""
    from src.provider.trae.client import _fill_estimated_credit

    upstream = Event(kind=EventKind.USAGE, usage=Usage(
        input_tokens=1_000_000, output_tokens=0, credit=99.0))
    _fill_estimated_credit(upstream, "glm-5.2")
    assert upstream.usage.credit == 99.0
    assert upstream.usage.credit_estimated is False

    content = Event(kind=EventKind.CONTENT, content="hi")
    _fill_estimated_credit(content, "glm-5.2")
    assert content.usage is None

    no_usage = Event(kind=EventKind.USAGE, usage=None)
    _fill_estimated_credit(no_usage, "glm-5.2")
    assert no_usage.usage is None

    unknown = Event(kind=EventKind.USAGE, usage=Usage(input_tokens=10, output_tokens=1))
    _fill_estimated_credit(unknown, "sagitta")
    assert unknown.usage.credit is None
    assert unknown.usage.credit_estimated is False

    filled = Event(kind=EventKind.USAGE, usage=Usage(input_tokens=1_000_000, output_tokens=0))
    _fill_estimated_credit(filled, "glm-5.2")
    assert filled.usage.credit == 216.0
    assert filled.usage.credit_estimated is True


def test_fill_estimated_credit_prefers_effective_cached_tokens():
    """上游不报缓存时用 provider 推算的有效命中定价，而不是把 0 当真。"""
    from src.provider.trae.client import _fill_estimated_credit

    event = Event(kind=EventKind.USAGE, usage=Usage(
        input_tokens=1_000_000, output_tokens=0, cached_tokens=0,
        cached_tokens_effective=500_000))
    _fill_estimated_credit(event, "glm-5.2")
    # glm-5.2 有效价 (216, 756, 54)：50 万命中 → 108 + 27
    assert event.usage.credit == 135.0
    assert event.usage.credit_estimated is True

    fallback = Event(kind=EventKind.USAGE, usage=Usage(
        input_tokens=1_000_000, output_tokens=0, cached_tokens=1_000_000))
    _fill_estimated_credit(fallback, "glm-5.2")
    assert fallback.usage.credit == 54.0            # effective 为 None → 用真值

def test_backfill_estimated_credit_fills_history(repo):
    """历史 trae 明细补推算：只动 credit 为 NULL / credit_estimated=1 的 trae 行，幂等。"""
    from src.provider.trae.backfill import backfill_estimated_credit

    _db = repo[2]
    base = 1_700_000_000
    # 待补：glm-5.2 输入 1M → 216 积分
    _insert_event(_db, id="a", ts=base, provider="trae", model="glm-5.2",
                  input_tokens=1_000_000, output_tokens=0)
    # 未收录模型：保持 NULL
    _insert_event(_db, id="b", ts=base, provider="trae", model="sagitta",
                  input_tokens=10, output_tokens=1)
    # 非 trae 渠道：不碰
    _insert_event(_db, id="c", ts=base, provider="codebuddy", model="glm-5.2",
                  input_tokens=1_000_000, output_tokens=0)
    # 已有真值：不覆盖
    _insert_event(_db, id="d", ts=base, provider="trae", model="glm-5.2",
                  input_tokens=1_000_000, output_tokens=0,
                  credit=9.9, credit_estimated=0)

    assert backfill_estimated_credit(_db) == 1
    rows = {r["id"]: r for r in _db.connect().execute(
        "SELECT id, credit, credit_estimated FROM usage_events")}
    assert (rows["a"]["credit"], rows["a"]["credit_estimated"]) == (216.0, 1)
    assert rows["b"]["credit"] is None and rows["b"]["credit_estimated"] == 0
    assert rows["c"]["credit"] is None
    assert (rows["d"]["credit"], rows["d"]["credit_estimated"]) == (9.9, 0)

    assert backfill_estimated_credit(_db) == 0        # 幂等：补过即跳过

def test_backfill_estimated_credit_recomputes_stale_estimates(repo):
    """单价表调整后，旧推算值（credit_estimated=1）重算；上游真值不动。"""
    from src.provider.trae.backfill import backfill_estimated_credit

    _db = repo[2]
    base = 1_700_000_000
    # 旧推算：deepseek-v4.1-flash 按老缓存价 0.56 算出的 0.56，需刷新为 28.0
    _insert_event(_db, id="a", ts=base, provider="trae", model="deepseek-v4.1-flash",
                  input_tokens=1_000_000, output_tokens=0,
                  credit=0.56, credit_estimated=1)
    # 已是最新推算：不动
    _insert_event(_db, id="b", ts=base, provider="trae", model="glm-5.2",
                  input_tokens=1_000_000, output_tokens=0,
                  credit=216.0, credit_estimated=1)
    # 上游真值：不动
    _insert_event(_db, id="c", ts=base, provider="trae", model="deepseek-v4.1-flash",
                  input_tokens=1_000_000, output_tokens=0,
                  credit=9.9, credit_estimated=0)

    assert backfill_estimated_credit(_db) == 1
    rows = {r["id"]: r for r in _db.connect().execute(
        "SELECT id, credit, credit_estimated FROM usage_events")}
    assert (rows["a"]["credit"], rows["a"]["credit_estimated"]) == (28.0, 1)
    assert (rows["b"]["credit"], rows["b"]["credit_estimated"]) == (216.0, 1)
    assert (rows["c"]["credit"], rows["c"]["credit_estimated"]) == (9.9, 0)

    assert backfill_estimated_credit(_db) == 0        # 重算后值稳定，再次执行无更新

def test_backfill_estimated_credit_rolls_up_hourly(repo):
    """回填后重算小时汇总，credit_sum / credit_estimated_known 跟着变。"""
    from src.provider.trae.backfill import backfill_estimated_credit
    from src.stats.collector import StatsCollector

    _db = repo[2]
    base = 1_700_000_000
    _insert_event(_db, id="a", ts=base, provider="trae", model="glm-5.2",
                  input_tokens=1_000_000, output_tokens=0)
    collector = StatsCollector(_db)
    collector.rollup_hourly()
    before = _db.connect().execute(
        "SELECT credit_sum, credit_known, credit_estimated_known FROM usage_hourly"
    ).fetchone()
    assert (before["credit_sum"], before["credit_known"]) == (0.0, 0)

    assert backfill_estimated_credit(_db) == 1
    collector.rollup_hourly()
    after = _db.connect().execute(
        "SELECT credit_sum, credit_known, credit_estimated_known FROM usage_hourly"
    ).fetchone()
    assert after["credit_sum"] == 216.0
    assert after["credit_known"] == 1 and after["credit_estimated_known"] == 1
