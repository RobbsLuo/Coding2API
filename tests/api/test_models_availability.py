"""模型可用性只读视图 `GET /api/models/availability`。

口径（详见 `src/api/admin_model_availability.py`）：

* operator 门槛（admin / operator 可读，viewer 403，未登录 401）；
* 窗口默认 7 天、钳到 `[1, 30]`，`limit` 钳到 `[1, 200]`——看板端点不该被
  前端缓存里的越界参数打成 400；
* 只认 `upstream_blocked` / `invalid_request` 两类 `error_type`（前者是协议
  预留值，后者是当前 executor 对「上游不认这个模型」的归类）；
* `blocked` 现算：拿当前 `MODEL_BLOCKLIST` 做 glob 匹配，热更后立即反映；
* 聚合走 `idx_usage_ts`（窗口是绝对时刻 `since`）并限行数，超出截断。
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.main import build_app
from tests.helpers.constants import SECRET

PATH = "/api/models/availability"


def _insert(app, *, provider: str, model: str, error_type: str | None,
            ts: int | None = None, event_id: str | None = None) -> None:
    """直接写一条明细。

    不走 `StatsCollector.record`：`upstream_blocked` 不在
    `CONTROLLED_ERROR_TYPES` 里，会被 `_normalize_error_type` 置 None，而那
    正是本端点要收的协议预留值。
    """
    stamp = int(time.time()) if ts is None else ts
    with app.state.stats_collector._db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, model, ok, error_type)"
            " VALUES (?,?,?,?,?,?,?)",
            (event_id or f"evt_{provider}_{model}_{stamp}_{error_type}",
             stamp, "root", provider, model, 0, error_type))


def _rows(client, **params) -> dict:
    response = client.get(PATH, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ 鉴权

def test_requires_session(tmp_path):
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get(PATH).status_code == 401


def test_viewer_is_forbidden(tmp_path):
    """viewer 能看自己的用量但看不了运维看板（与凭证页同一门槛）。"""
    app = build_app(_settings(tmp_path))
    app.state.user_repo.create("watcher", "hash", role="viewer")
    with TestClient(app) as client:
        client.cookies.set("coding2api_session",
                           create_session_token("watcher", SECRET))
        assert client.get(PATH).status_code == 403


def test_operator_can_read(tmp_path):
    app = build_app(_settings(tmp_path))
    app.state.user_repo.create("ops", "hash", role="operator")
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("ops", SECRET))
        body = _rows(client)
    assert body["rows"] == []


def _settings(tmp_path):
    from src.config import Settings

    return Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                    ADMIN_USERNAMES="root")


# ------------------------------------------------------------ 聚合与过滤

def test_aggregates_by_provider_and_model(admin_client):
    app, client = admin_client
    now = int(time.time())
    for offset in (10, 20, 30):
        _insert(app, provider="trae", model="glm-5.3", error_type="invalid_request",
                ts=now - offset)
    _insert(app, provider="kilo", model="space-bunny", error_type="upstream_blocked",
            ts=now - 5)
    rows = _rows(client)["rows"]
    assert [row["provider"] for row in rows] == ["trae", "kilo"]  # 失败次数降序
    first = rows[0]
    assert first["model"] == "glm-5.3"
    assert first["failures"] == 3
    assert first["last_failed_at"] == now - 10
    assert rows[1]["failures"] == 1


def test_ignores_other_error_types_and_successes(admin_client):
    """其它失败原因（限流 / 凭证失效 / 上游 5xx）与成功请求都不算「模型下线」。"""
    app, client = admin_client
    for error_type in ("rate_limit", "credential_unavailable",
                       "upstream_error", "model_unavailable", None):
        _insert(app, provider="trae", model="glm-5.3", error_type=error_type)
    with app.state.stats_collector._db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, model, ok)"
            " VALUES (?,?,?,?,?,?)",
            ("evt_ok", int(time.time()), "root", "trae", "glm-5.3", 1))
    assert _rows(client)["rows"] == []


def test_model_not_found_counts_as_model_level_failure(admin_client):
    """「上游无此模型」（`model_not_found`）计入模型级失败。

    2026-10-26 起该类从 `invalid_request` 里独立出来（此前被读成「客户端
    参数写错了」），但**仍属**模型级失败，必须继续进可用性视图。
    """
    app, client = admin_client
    _insert(app, provider="officeace", model="deepseek-v4.1-flash-1017",
            error_type="model_not_found")
    rows = _rows(client)["rows"]
    assert len(rows) == 1
    assert rows[0]["provider"] == "officeace"
    assert rows[0]["failures"] == 1


def test_window_excludes_old_failures(admin_client):
    app, client = admin_client
    now = int(time.time())
    _insert(app, provider="trae", model="recent", error_type="invalid_request",
            ts=now - 3600)
    _insert(app, provider="trae", model="ancient", error_type="invalid_request",
            ts=now - 40 * 86400)
    assert [row["model"] for row in _rows(client)["rows"]] == ["recent"]
    # 放宽到 30 天也看不到 40 天前的那条（上限就是 30 天）
    assert [row["model"] for row in _rows(client, days=30)["rows"]] == ["recent"]


def test_days_is_clamped(admin_client):
    _app, client = admin_client
    assert _rows(client, days=0)["window_days"] == 1
    assert _rows(client, days=-5)["window_days"] == 1
    assert _rows(client, days=7)["window_days"] == 7
    assert _rows(client, days=999)["window_days"] == 30


def test_since_is_absolute_and_matches_window(admin_client):
    _app, client = admin_client
    before = int(time.time())
    body = _rows(client, days=3)
    after = int(time.time())
    assert before - 3 * 86400 <= body["since"] <= after - 3 * 86400


def test_limit_truncates_and_flags(admin_client):
    app, client = admin_client
    now = int(time.time())
    for index in range(5):
        _insert(app, provider="trae", model=f"m{index}", error_type="invalid_request",
                ts=now - index, event_id=f"evt_{index}")
    body = _rows(client, limit=3)
    assert len(body["rows"]) == 3
    assert body["truncated"] is True
    # 失败次数相同 → 按 (provider, model) 升序，截断结果稳定
    assert [row["model"] for row in body["rows"]] == ["m0", "m1", "m2"]
    full = _rows(client, limit=5)
    assert full["truncated"] is False
    assert len(full["rows"]) == 5


def test_limit_is_clamped(admin_client):
    _app, client = admin_client
    assert _rows(client, limit=0)["rows"] == []
    assert _rows(client, limit=-1)["rows"] == []
    # 上限 200：多写一条即触发截断（证明 limit 被钳到 200 而不是 9999）
    app, client = admin_client
    now = int(time.time())
    for index in range(201):
        _insert(app, provider="trae", model=f"m{index:03d}",
                error_type="invalid_request", ts=now, event_id=f"evt_{index}")
    body = _rows(client, limit=9999)
    assert len(body["rows"]) == 200 and body["truncated"] is True


# ------------------------------------------------------------- blocked 现算

def test_blocked_reflects_current_blocklist(admin_client):
    app, client = admin_client
    _insert(app, provider="trae", model="glm-5.3", error_type="invalid_request")
    _insert(app, provider="kilo", model="space-bunny", error_type="invalid_request")
    rows = {row["model"]: row for row in _rows(client)["rows"]}
    assert rows["glm-5.3"]["blocked"] is False

    app.state.runtime_settings.set("model_blocklist", "glm-*")
    rows = {row["model"]: row for row in _rows(client)["rows"]}
    assert rows["glm-5.3"]["blocked"] is True
    assert rows["space-bunny"]["blocked"] is False


def test_blocked_matches_normalized_key_and_display_name(admin_client):
    """记录里的模型名可能是展示名（`Kimi K3`）：与黑名单同口径归一再比。"""
    app, client = admin_client
    _insert(app, provider="trae", model="Kimi K3", error_type="invalid_request")
    app.state.runtime_settings.set("model_blocklist", "kimi-k3")
    row = _rows(client)["rows"][0]
    assert row["blocked"] is True


def test_error_types_reported(admin_client):
    """响应里带口径说明，前端不必硬编码「哪几类算下线」。"""
    _app, client = admin_client
    assert _rows(client)["error_types"] == [
        "upstream_blocked", "model_not_found", "invalid_request"]


# ------------------------------------------------------- 探活结论与人工接管

PROBES = "/api/models/probes"


def _block(app, *, provider: str, model: str, until: int | None = None,
           reason: str = "blocked") -> str:
    """给某渠道加一个凭证并写一条模型级冷却行，返回凭证 id。"""
    credentials = app.state.services.credentials
    credential_id = credentials.add(provider=provider,
                                    credential_data={"bearer_token": "t"})
    with app.state.services.credentials._db.transaction() as conn:
        conn.execute(
            "INSERT INTO credential_model_cooldowns "
            "(credential_id, model, cooling_until, hits, reason) VALUES (?,?,?,?,?)",
            (credential_id, model,
             until if until is not None else int(time.time()) + 3600, 2, reason))
    return credential_id


def _operator_client(tmp_path, role: str = "operator"):
    from src.config import Settings

    app = build_app(Settings(_env_file=None, APP_SECRET=SECRET,
                             DATA_DIR=str(tmp_path), ADMIN_USERNAMES="root"))
    app.state.user_repo.create("ops", "hash", role=role)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("ops", SECRET))
    return app, client


def test_probes_requires_session(tmp_path):
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get(PROBES).status_code == 401
        assert client.post(f"{PROBES}/probe", json={}).status_code == 401


def test_probes_viewer_is_forbidden(tmp_path):
    app = build_app(_settings(tmp_path))
    app.state.user_repo.create("watcher", "hash", role="viewer")
    with TestClient(app) as client:
        client.cookies.set("coding2api_session",
                           create_session_token("watcher", SECRET))
        assert client.get(PROBES).status_code == 403
        assert client.post(f"{PROBES}/probe",
                           json={"provider": "trae", "model": "m"}).status_code == 403


def test_probes_lists_blocked_rows(tmp_path):
    """仍被负缓存的 (渠道, 模型) 出现在列表里，带号数与最晚解除时刻。"""
    app, client = _operator_client(tmp_path)
    with client:
        _block(app, provider="trae", model="glm-5.3")
        body = client.get(PROBES).json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert (row["provider"], row["model"]) == ("trae", "glm-5.3")
    assert row["credentials"] == 1
    assert row["state"] == ""          # 从未探过：没有结论，不是「健康」
    assert row["manual"] == ""


def test_probes_aggregates_credentials_and_ignores_other_reasons(tmp_path):
    """同模型多号合并成一行；`reason='model'`（限流）不进这张表。"""
    app, client = _operator_client(tmp_path)
    with client:
        _block(app, provider="trae", model="glm-5.3")
        _block(app, provider="trae", model="glm-5.3")
        _block(app, provider="trae", model="limited", reason="model")
        rows = client.get(PROBES).json()["rows"]
    assert [row["model"] for row in rows] == ["glm-5.3"]
    assert rows[0]["credentials"] == 2


def test_probes_ignores_expired_rows(tmp_path):
    """已过期的负缓存不在列表里（调度侧本来就会重试）。"""
    app, client = _operator_client(tmp_path)
    with client:
        _block(app, provider="trae", model="glm-5.3", until=1)
        assert client.get(PROBES).json()["rows"] == []


def test_probes_shows_manual_row_without_blocked(tmp_path):
    """人工接管但负缓存已清：那一行仍要露出，否则无法交还自动探活。"""
    app, client = _operator_client(tmp_path)
    with client:
        app.state.services.model_probes.set_manual(
            provider="trae", model="glm-5.3", manual="manual")
        rows = client.get(PROBES).json()["rows"]
    assert len(rows) == 1
    assert rows[0]["credentials"] == 0
    assert rows[0]["manual"] == "manual"


def test_probes_hides_blacklisted_models(tmp_path):
    """命中黑名单的行整条不返回：黑名单是「主动弃用」，不该再去救它。"""
    app, client = _operator_client(tmp_path)
    with client:
        app.state.runtime_settings.set("model_blocklist", "glm-*")
        _block(app, provider="trae", model="glm-5.3")
        _block(app, provider="trae", model="kimi-k3")
        rows = client.get(PROBES).json()["rows"]
    assert [row["model"] for row in rows] == ["kimi-k3"]


def test_probes_hides_blacklisted_manual_row(tmp_path):
    """只留人工标记、没有负缓存的黑名单行同样滤掉。"""
    app, client = _operator_client(tmp_path)
    with client:
        app.state.runtime_settings.set("model_blocklist", "glm-5.3")
        app.state.services.model_probes.set_manual(
            provider="trae", model="glm-5.3", manual="manual")
        app.state.services.model_probes.set_manual(
            provider="trae", model="kimi-k3", manual="manual")
        rows = client.get(PROBES).json()["rows"]
    assert [row["model"] for row in rows] == ["kimi-k3"]


def test_probes_sorted_failed_first_then_until(tmp_path):
    """排序：failed 结论优先，其后按最晚解除时刻降序（最该处理的排最前）。"""
    app, client = _operator_client(tmp_path)
    with client:
        _block(app, provider="trae", model="a")
        _block(app, provider="trae", model="b")
        probes = app.state.services.model_probes
        probes.record(provider="trae", model="b", state="failed", error="no model")
        rows = client.get(PROBES).json()["rows"]
    assert [row["model"] for row in rows] == ["b", "a"]
    assert rows[0]["state"] == "failed"


def test_probe_now_requires_target_fields(tmp_path):
    app, client = _operator_client(tmp_path)
    with client:
        response = client.post(f"{PROBES}/probe", json={"provider": "trae"})
    assert response.status_code == 400


def test_probe_now_clears_blocked_on_success(tmp_path):
    """立即探活成功 → 负缓存清空 + 审计留痕（恢复入口）。"""
    from src.provider.base import Event, EventKind

    class Provider:
        id = "trae"

        async def stream_chat(self, data, payload, model):
            yield Event(EventKind.CONTENT, content="pong")

    app, client = _operator_client(tmp_path)
    with client:
        # 探活任务在 lifespan 里装配，必须先进入上下文才能替换 provider。
        app.state.model_probe_task._providers["trae"] = Provider()
        _block(app, provider="trae", model="glm-5.3")
        body = client.post(f"{PROBES}/probe",
                           json={"provider": "trae", "model": "glm-5.3"}).json()
        rows = client.get(PROBES).json()["rows"]
    assert body == {"provider": "trae", "model": "glm-5.3",
                    "state": "ok", "error": ""}
    # 负缓存已清（`credentials`/`until` 归零），但结论行保留：页面要能显示
    # 「探过、已恢复」，而不是整行凭空消失。
    assert len(rows) == 1
    assert rows[0]["credentials"] == 0
    assert rows[0]["until"] == 0
    assert rows[0]["state"] == "ok"
    actions = [item["action"] for item in
               app.state.services.audit.query(limit=10)]
    assert "model.probe" in actions


def test_probe_now_records_failure_without_clearing(tmp_path):
    """探活失败（额度类）→ inconclusive，负缓存保留，页面能看到原因。"""
    from src.provider.base import ErrKind, Event, EventKind

    class Provider:
        id = "trae"

        async def stream_chat(self, data, payload, model):
            yield Event(EventKind.ERROR, error_message="rate limited",
                        error_kind=ErrKind.SOFT)

    app, client = _operator_client(tmp_path)
    with client:
        app.state.model_probe_task._providers["trae"] = Provider()
        _block(app, provider="trae", model="glm-5.3")
        body = client.post(f"{PROBES}/probe",
                           json={"provider": "trae", "model": "glm-5.3"}).json()
    assert body["state"] == "inconclusive"
    assert body["error"] == "rate limited"
    assert len(app.state.services.credentials.blocked_cooldowns()) == 1


def test_manual_takeover_toggles(tmp_path):
    """人工接管 on/off：写标记 + 审计；off 后交还自动探活。"""
    app, client = _operator_client(tmp_path)
    with client:
        first = client.post(f"{PROBES}/manual",
                            json={"provider": "trae", "model": "glm-5.3",
                                  "manual": "on"}).json()
        assert first == {"provider": "trae", "model": "glm-5.3", "manual": True}
        assert app.state.services.model_probes.manual_marked() == {("trae", "glm-5.3")}
        second = client.post(f"{PROBES}/manual",
                             json={"provider": "trae", "model": "glm-5.3",
                                   "manual": "off"}).json()
    assert second["manual"] is False
    assert app.state.services.model_probes.manual_marked() == set()
    actions = [item["action"] for item in app.state.services.audit.query(limit=10)]
    assert actions.count("model.probe_manual") == 2


def test_manual_takeover_rejects_bad_value(tmp_path):
    app, client = _operator_client(tmp_path)
    with client:
        response = client.post(f"{PROBES}/manual",
                               json={"provider": "trae", "model": "m",
                                     "manual": "yes"})
    assert response.status_code == 400


def test_manual_takeover_requires_target(tmp_path):
    app, client = _operator_client(tmp_path)
    with client:
        response = client.post(f"{PROBES}/manual", json={"manual": "on"})
    assert response.status_code == 400
