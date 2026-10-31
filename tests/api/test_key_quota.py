"""Key 级配额：DB 聚合、仓储列、准入 429、管理台读写与 `/v1/usage`。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.db.conn import Database
from src.db.key_usage import KeyUsageRepository
from src.db.migrate import apply_schema
from src.db.repo import ApiKeyRepository
from src.key_quota import KeyQuota
from src.main import build_app
from tests.helpers.auth import isolated_settings
from tests.helpers.chat import chat_request  # noqa: F401 - 保持与既有出口测试同源
from tests.helpers.events import TRAE_GOOD
from tests.helpers.providers import ScriptedProvider


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "keyquota.sqlite3")
    apply_schema(database.connect())
    yield database
    database.close()


@pytest.fixture()
def usage(db):
    return KeyUsageRepository(db)


def _record(db, *, key_id, ts, ok=True, input_tokens=10, output_tokens=5,
            cost_usd=None, username="root"):
    """直接写一条明细（绕开 collector，专测聚合口径）。"""
    db.connect().execute(
        "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, ok, "
        "error_type, input_tokens, output_tokens, reasoning_tokens, cached_tokens, credit, "
        "credit_estimated, cost_usd, cost_cny, latency_ms, ttfb_ms, request_id, api_key_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"evt_{ts}_{key_id}", ts, username, "trae", None, "glm-5.2", int(ok), None,
         input_tokens, output_tokens, 0, None, None, 0, cost_usd, None, 10, 5, None, key_id))
    db.connect().commit()


# ------------------------------------------------------------ 聚合口径


def test_for_key_aggregates_window_and_splits_ok_failed(db, usage):
    _record(db, key_id="k1", ts=100, ok=True, input_tokens=10, output_tokens=5,
            cost_usd=0.25)
    _record(db, key_id="k1", ts=200, ok=False, input_tokens=3, output_tokens=2)
    _record(db, key_id="k2", ts=150)                 # 另一把 Key 不计入
    _record(db, key_id=None, ts=160)                 # 未归属不计入
    result = usage.for_key("k1", since=0)
    assert (result.requests, result.ok, result.failed) == (2, 1, 1)
    assert (result.input_tokens, result.output_tokens) == (13, 7)
    assert result.tokens == 20
    assert result.cost_usd == pytest.approx(0.25)


def test_for_key_respects_since_and_until(db, usage):
    _record(db, key_id="k1", ts=100)
    _record(db, key_id="k1", ts=200)
    _record(db, key_id="k1", ts=300)
    assert usage.for_key("k1", since=150).requests == 2
    assert usage.for_key("k1", since=0, until=300).requests == 2
    assert usage.for_key("k1", since=0, until=100).requests == 0


def test_for_key_returns_zeros_when_absent(db, usage):
    result = usage.for_key("ghost", since=0)
    assert (result.requests, result.tokens, result.cost_usd) == (0, 0, 0.0)


def test_map_for_batches_and_ignores_blank_ids(db, usage):
    _record(db, key_id="k1", ts=100)
    _record(db, key_id="k1", ts=110)
    _record(db, key_id="k2", ts=120)
    result = usage.map_for(["k1", "k2", "k3", ""], since=0)
    assert result["k1"].requests == 2
    assert result["k2"].requests == 1
    assert "k3" not in result and "" not in result
    assert usage.map_for([], since=0) == {}


# ------------------------------------------------------- 仓储与迁移


def test_repository_persists_quota_column(db):
    keys = ApiKeyRepository(db)
    created = keys.create("alice", "k", quota='{"rpm": 5}')
    assert created["quota"] == '{"rpm": 5}'
    assert keys.authenticate(created["api_key"])["quota"] == '{"rpm": 5}'
    assert keys.list_for("alice")[0]["quota"] == '{"rpm": 5}'
    # 改配额只影响该 Key，且带 username 越权防护
    assert keys.set_quota(created["id"], "alice", '{"daily_requests": 9}') is True
    assert keys.authenticate(created["api_key"])["quota"] == '{"daily_requests": 9}'
    assert keys.set_quota(created["id"], "bob", "") is False
    assert keys.set_quota("key_missing", "alice", "") is False
    # 清空 = 回到不受限
    assert keys.set_quota(created["id"], "alice", "") is True
    assert keys.authenticate(created["api_key"])["quota"] == ""


def test_legacy_api_keys_table_gains_quota_column(tmp_path):
    """老库升级：api_keys 无 quota 时补列，历史 Key 默认「不限制」。"""
    import sqlite3

    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE api_keys (
            id TEXT PRIMARY KEY, username TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
            key_digest TEXT NOT NULL UNIQUE, preview TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_used_at INTEGER)
    """)
    conn.execute("INSERT INTO api_keys (id, username, key_digest, preview, created_at)"
                 " VALUES ('key_old', 'root', 'digest', 'sk-…old', 1)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(api_keys)")}
    assert "quota" in columns
    assert db.connect().execute(
        "SELECT quota FROM api_keys WHERE id = 'key_old'").fetchone()[0] == ""
    db.close()


# ------------------------------------------------------------ HTTP 层


@pytest.fixture()
def provider():
    return ScriptedProvider([TRAE_GOOD])


@pytest.fixture()
def app(tmp_path, provider):
    settings = isolated_settings(tmp_path)
    return build_app(settings, providers={"trae": provider})


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _record_via_collector(app, key_id: str, *, ok: bool = True) -> None:
    """走 StatsCollector 写一条明细：顺带覆盖 B1 的 contextvar 归属链路。"""
    from src.request_context import set_api_key_id

    set_api_key_id(key_id)
    try:
        app.state.stats_collector.record(username="root", provider="trae",
                                         model="glm-5.2", ok=ok,
                                         input_tokens=10, output_tokens=5)
    finally:
        set_api_key_id(None)


def _key(app, **kwargs) -> dict:
    return app.state.api_keys.create("root", "k", **kwargs)


def _chat(client, key: str, **payload):
    body = {"messages": [{"role": "user", "content": "hi"}], **payload}
    return client.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"}, json=body)


def test_quota_rpm_blocks_second_request_with_429(client, app):
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    created = _key(app, quota=json.dumps({"rpm": 1}))
    assert _chat(client, created["api_key"]).status_code == 200
    blocked = _chat(client, created["api_key"])
    assert blocked.status_code == 429
    assert blocked.json()["error"]["type"] == "quota_exceeded"
    assert "requests per minute" in blocked.json()["error"]["message"]
    assert int(blocked.headers["Retry-After"]) >= 1


def test_quota_daily_requests_uses_recorded_usage(client, app):
    created = _key(app, quota=json.dumps({"daily_requests": 1}))
    _record_via_collector(app, created["id"])
    blocked = _chat(client, created["api_key"])
    assert blocked.status_code == 429
    assert "requests per day" in blocked.json()["error"]["message"]
    # Retry-After 到下一个北京时间 0 点（1..86400 秒）
    assert 1 <= int(blocked.headers["Retry-After"]) <= 86400


def test_quota_tpm_blocks_on_estimated_tokens(client, app):
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    created = _key(app, quota=json.dumps({"tpm": 1}))
    response = _chat(client, created["api_key"], max_tokens=1000)
    assert response.status_code == 429
    assert "tokens per minute" in response.json()["error"]["message"]


def test_quota_concurrency_released_after_response(client, app):
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    created = _key(app, quota=json.dumps({"max_concurrency": 1}))
    for _ in range(3):        # 串行请求：每次结束后名额都归还
        assert _chat(client, created["api_key"]).status_code == 200


def test_quota_concurrency_blocks_inflight(client, app):
    created = _key(app, quota=json.dumps({"max_concurrency": 1}))
    # 手工占一个名额，模拟另一个在途请求
    app.state.services.key_quota.admit(created["id"], KeyQuota(max_concurrency=1))
    response = _chat(client, created["api_key"])
    assert response.status_code == 429
    assert "concurrent" in response.json()["error"]["message"]


def test_unlimited_key_is_untouched(client, app):
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    created = _key(app)
    for _ in range(3):
        assert _chat(client, created["api_key"]).status_code == 200


# ------------------------------------------------ 用户级配额（/v1 也要判）

def test_user_quota_applies_to_v1_even_with_unlimited_key(client, app):
    """管理员按人设的配额必须在 `/v1` 生效。

    自助建 Key 时配额由本人填写，只看 Key 自己的 quota 就等于让用户造一把
    空配额的 Key 绕开人均配额（Playground 早已判，`/v1` 原来漏了）。
    """
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.user_repo.set_policy("root", allowed_models="",
                                   quota=json.dumps({"daily_requests": 1}))
    created = _key(app)                       # Key 本身不限额
    _record_via_collector(app, created["id"])
    blocked = _chat(client, created["api_key"])
    assert blocked.status_code == 429
    assert "user" in blocked.json()["error"]["message"]


def test_user_quota_rpm_blocks_v1(client, app):
    """用户级 rpm 同样作用于 /v1：换一把 Key 也不能绕过。"""
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.user_repo.set_policy("root", allowed_models="",
                                   quota=json.dumps({"rpm": 1}))
    first = _key(app)
    second = _key(app)
    assert _chat(client, first["api_key"]).status_code == 200
    blocked = _chat(client, second["api_key"])   # 另一把 Key，同一用户
    assert blocked.status_code == 429
    assert "user" in blocked.json()["error"]["message"]


def test_user_quota_release_returns_both_layers(client, app):
    """两层租约必须同时归还：否则用户级并发名额会被自己占死。"""
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.user_repo.set_policy("root", allowed_models="",
                                   quota=json.dumps({"max_concurrency": 1}))
    created = _key(app)
    for _ in range(3):        # 串行请求：每次都归还两层名额
        assert _chat(client, created["api_key"]).status_code == 200
    assert app.state.services.user_quota.inflight("root") == 0


def test_user_quota_rejected_releases_key_slot(client, app):
    """用户级拒绝时 Key 级的在途名额要立刻归还，不能泄漏。"""
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.user_repo.set_policy("root", allowed_models="",
                                   quota=json.dumps({"max_concurrency": 1}))
    created = _key(app, quota=json.dumps({"max_concurrency": 5}))
    app.state.services.user_quota.admit("root", KeyQuota(max_concurrency=1))
    blocked = _chat(client, created["api_key"])
    assert blocked.status_code == 429
    assert app.state.services.key_quota.inflight(created["id"]) == 0


def test_dirty_quota_text_degrades_to_unlimited(client, app, caplog):
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    created = _key(app, quota="{not json")
    with caplog.at_level("WARNING"):
        assert _chat(client, created["api_key"]).status_code == 200
    assert "无法解析" in caplog.text


def test_quota_exceeded_does_not_call_upstream(client, app, provider):
    created = _key(app, quota=json.dumps({"daily_requests": 1}))
    _record_via_collector(app, created["id"])
    response = _chat(client, created["api_key"])
    assert response.status_code == 429
    assert provider.calls == 0


def test_streaming_request_releases_concurrency_lease(client, app):
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    created = _key(app, quota=json.dumps({"max_concurrency": 1}))
    stream = _chat(client, created["api_key"], stream=True)
    assert stream.status_code == 200
    assert stream.text            # 消费完流
    assert app.state.services.key_quota.inflight(created["id"]) == 0


def test_streaming_preflight_failure_releases_lease(client, app):
    """400（preflight 失败）也必须归还名额，否则一把 Key 会被自己锁死。"""
    created = _key(app, quota=json.dumps({"max_concurrency": 1}))
    response = _chat(client, created["api_key"], stream=True, model="glm-5.2@codebuddy")
    assert response.status_code == 400
    assert app.state.services.key_quota.inflight(created["id"]) == 0


def test_non_streaming_upstream_failure_releases_lease(client, app):
    created = _key(app, quota=json.dumps({"max_concurrency": 1}))
    # 池为空 → 503，名额仍须归还
    assert _chat(client, created["api_key"]).status_code == 503
    assert app.state.services.key_quota.inflight(created["id"]) == 0


# ------------------------------------------------------- 用户级聚合（Playground）

def test_for_user_aggregates_across_entry_points(db, usage):
    """按用户名聚合：该用户经任何入口的用量都计入用户级配额。

    Playground 的会话直连 `api_key_id` 为 NULL，若照 Key 那样过滤
    `api_key_id IS NOT NULL`，它自己的流量就不吃自己那份配额——配额形同虚设。
    """
    _record(db, key_id="key_a", ts=1000, username="alice")
    _record(db, key_id=None, ts=1001, username="alice")          # Playground 直连
    _record(db, key_id="key_b", ts=1002, username="bob")         # 别人的流量不计
    alice = usage.for_user("alice", since=0)
    assert (alice.requests, alice.tokens) == (2, 30)
    # until 上界与 since 下界都生效
    assert usage.for_user("alice", since=0, until=1001).requests == 1
    assert usage.for_user("alice", since=2000).requests == 0
