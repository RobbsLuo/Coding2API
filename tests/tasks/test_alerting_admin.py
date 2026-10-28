"""运维告警：令牌到期窗口、错误率统计、仓储与 /api/alerts 端点、设置热更。"""

from __future__ import annotations

import base64
import json

import pytest

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import AlertRepository
from src.stats.collector import StatsCollector
from tests.helpers.constants import SECRET


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"

# ------------------------------------------------- CredentialRepository.expiring_tokens


def test_expiring_tokens_window_and_exclusions(repo):
    credentials, db = repo
    now = 1_000_000
    # 落在窗口内
    soon = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "t", "expires_at": now + 3600}, nickname="甲", now=now)
    # 已过期（在 now 之前）→ 不算「即将到期」
    credentials.add(provider="trae", credential_data={
        "bearer_token": "t", "expires_at": now - 10}, now=now)
    # 窗口外（超出 within）
    credentials.add(provider="trae", credential_data={
        "bearer_token": "t", "expires_at": now + 100_000}, now=now)
    # 到期未知（0）→ 不当成已过期
    credentials.add(provider="trae", credential_data={"bearer_token": "t"}, now=now)
    # 硬禁用凭证排除
    dead = credentials.add(provider="trae", credential_data={
        "bearer_token": "t", "expires_at": now + 60}, now=now)
    db.connect().execute("UPDATE credentials SET disabled = 1 WHERE id = ?", (dead,))
    db.connect().commit()

    rows = credentials.expiring_tokens(within_seconds=7200, now=now)
    assert [r["id"] for r in rows] == [soon]
    assert rows[0]["nickname"] == "甲" and rows[0]["provider"] == "codebuddy"

def test_expiring_tokens_derives_from_blob_when_column_null(repo):
    """token_expires_at 列为 NULL（老库未回填）时从密文派生。"""
    credentials, db = repo
    now = 1_000_000
    cid = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": _jwt({"exp": now + 1800})}, now=now)
    db.connect().execute(
        "UPDATE credentials SET token_expires_at = NULL WHERE id = ?", (cid,))
    db.connect().commit()
    rows = credentials.expiring_tokens(within_seconds=3600, now=now)
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["token_expires_at"] == now + 1800

def test_expiring_tokens_off_returns_empty(repo):
    credentials, _db = repo
    assert credentials.expiring_tokens(within_seconds=0, now=1) == []

def test_expiring_tokens_skips_tokens_shorter_lived_than_window(repo):
    """寿命本就短于窗口的 token 不报：命中是常态，规则没有信息量。

    CodeArts 的 STS 临时凭证只有 2h 寿命，默认窗口 24h——不管续期成功与否它都
    永远「即将到期」，每个静默窗报一次只会把真告警淹掉。它真出问题时预刷新任务
    会失败并标记需重新登录（进而触发池空告警）。
    """
    credentials, _db = repo
    now = 1_000_000
    # 2h 寿命（expiration 与 refresh_token JWT 的 iat 相差 7200）
    credentials.add(provider="codearts", credential_data={
        "expiration": now + 3600,
        "refresh_token": _jwt({"iat": now + 3600 - 7200, "exp": now + 30 * 86400})},
        now=now)
    # 30 天寿命的同形态凭证：窗口 24h 相对它很短，必须照报
    long_lived = credentials.add(provider="codearts", credential_data={
        "expiration": now + 3600,
        "refresh_token": _jwt({"iat": now + 3600 - 30 * 86400, "exp": now + 30 * 86400})},
        now=now)
    rows = credentials.expiring_tokens(within_seconds=86400, now=now)
    assert [r["id"] for r in rows] == [long_lived]

# --------------------------------------------------------- StatsCollector


def test_window_error_rate_counts_requests_and_failures(repo):
    _credentials, db = repo
    stats = StatsCollector(db)
    stats.record(username="u", provider="trae", model="m", ok=True, now=1000)
    stats.record(username="u", provider="trae", model="m", ok=False, now=1100)
    stats.record(username="u", provider="trae", model="m", ok=False, now=1200)
    requests, failed = stats.window_error_rate(since=1000)
    assert (requests, failed) == (3, 2)
    # 窗口外不计
    assert stats.window_error_rate(since=1150) == (1, 1)

# --------------------------------------------------------- AlertRepository


def test_alert_repository_record_recent_last_ts_prune(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = AlertRepository(db)
    repo.record(rule="pool_empty", severity="critical", scope="pool",
                message="池空", detail="{}", now=100)
    repo.record(rule="task_failed", severity="warning", scope="growth",
                message="挂了", now=200)
    recent = repo.recent()
    assert [r["ts"] for r in recent] == [200, 100]
    assert repo.last_ts("pool_empty", "pool") == 100
    assert repo.last_ts("pool_empty", "other") is None
    # limit 收敛到 [1, 200]
    assert len(repo.recent(limit=0)) == 1
    assert repo.prune(keep_days=1, now=100 + 2 * 86400) == 2
    assert repo.recent() == []
    db.close()

def test_alert_repository_records_delivery_failure(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = AlertRepository(db)
    repo.record(rule="error_rate", severity="warning", scope="pool",
                message="错误率高", delivered=False, delivery_error="boom", now=1)
    row = repo.recent()[0]
    assert row["delivered"] == 0 and row["delivery_error"] == "boom"
    db.close()


def test_alert_repository_resolve_marks_all_unresolved_in_episode(tmp_path):
    """恢复标记该 (rule, scope) **全部**未恢复的行：同一次故障的提醒行一起收尾。

    只标最近一条会让更早的提醒永远停在未恢复——角标数 `resolved_at IS NULL`，
    于是永远清不干净。历史已恢复的循环不受影响（它们已有 resolved_at）。
    """
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = AlertRepository(db)
    repo.record(rule="pool_empty", severity="critical", scope="pool",
                message="第一次", now=100)
    repo.record(rule="pool_empty", severity="critical", scope="pool",
                message="第二次", now=200)
    assert repo.resolve("pool_empty", "pool", now=250) == 2
    rows = repo.recent()             # 倒序：第二条先
    assert all(row["resolved_at"] == 250 for row in rows)
    assert repo.count_unresolved() == 0
    # 已全部恢复：再 resolve 无事发生
    assert repo.resolve("pool_empty", "pool", now=260) == 0
    db.close()


def test_alert_repository_resolve_by_id_touches_single_row(tmp_path):
    """人工处置只动目标行：同组其它未恢复记录不受影响（不误收仍在告警的）。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = AlertRepository(db)
    first = repo.record(rule="pool_empty", severity="critical", scope="pool",
                        message="第一次", now=100)
    second = repo.record(rule="pool_empty", severity="critical", scope="pool",
                         message="第二次", now=200)
    assert repo.resolve_by_id(first, now=250) == 1
    rows = {row["id"]: row for row in repo.recent()}
    assert rows[first]["resolved_at"] == 250
    assert rows[second]["resolved_at"] is None
    # 已恢复 / 不存在都返回 0（调用方据此回 400）
    assert repo.resolve_by_id(first, now=260) == 0
    assert repo.resolve_by_id("alert_missing", now=270) == 0
    db.close()


def test_alert_repository_unresolved_returns_only_open_rows(tmp_path):
    """`unresolved()` 供 AlertTask 重启重建：只回未恢复行，含恢复文案所需字段。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = AlertRepository(db)
    repo.record(rule="pool_empty", severity="critical", scope="pool",
                message="池空", detail='{"ready": 0}', now=100)
    repo.record(rule="task_failed", severity="warning", scope="growth",
                message="挂了", now=200)
    repo.resolve("task_failed", "growth", now=210)
    rows = repo.unresolved()
    assert [row["rule"] for row in rows] == ["pool_empty"]
    assert rows[0]["scope"] == "pool" and rows[0]["message"] == "池空"
    assert rows[0]["detail"] == '{"ready": 0}' and rows[0]["severity"] == "critical"
    db.close()

def test_alerts_requires_admin(admin_client):
    _app, client = admin_client
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    assert client.get("/api/alerts").status_code == 403

def test_alerts_returns_recent(admin_client):
    app, client = admin_client
    app.state.services.alerts.record(
        rule="task_failed", severity="warning", scope="growth",
        message="挂了", now=123)
    body = client.get("/api/alerts").json()
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["rule"] == "task_failed"


def test_resolve_alert_marks_one_row_and_audits(admin_client):
    """人工标记恢复：只动目标行、写审计，其余未恢复行不受影响。"""
    app, client = admin_client
    alerts = app.state.services.alerts
    alerts.record(rule="pool_empty", severity="critical", scope="pool",
                  message="池空", now=100)
    alerts.record(rule="task_failed", severity="warning", scope="growth",
                  message="挂了", now=200)
    target = alerts.recent()[0]["id"]      # 倒序：task_failed 那行

    response = client.post(f"/api/alerts/{target}/resolve")

    assert response.status_code == 200 and response.json() == {"ok": True}
    rows = {row["id"]: row for row in alerts.recent()}
    assert rows[target]["resolved_at"] is not None
    assert alerts.count_unresolved() == 1          # pool_empty 仍在告警中
    audit = app.state.services.audit.query(action="alert.resolve")
    assert len(audit) == 1
    assert audit[0]["actor"] == "root" and audit[0]["target"] == target


def test_resolve_alert_unknown_or_already_resolved_is_400(admin_client):
    """不存在或已恢复都回 400：静默成功会让运维以为点对了。"""
    app, client = admin_client
    alerts = app.state.services.alerts
    assert client.post("/api/alerts/alert_missing/resolve").status_code == 400
    alerts.record(rule="pool_empty", severity="critical", scope="pool",
                  message="池空", now=100)
    target = alerts.recent()[0]["id"]
    assert client.post(f"/api/alerts/{target}/resolve").status_code == 200
    assert client.post(f"/api/alerts/{target}/resolve").status_code == 400


def test_resolve_alert_requires_admin_and_csrf(admin_client):
    """人工覆盖机器判定：admin-only + CSRF，且 viewer 不能动。"""
    app, client = admin_client
    app.state.services.alerts.record(rule="pool_empty", severity="critical",
                                     scope="pool", message="池空", now=100)
    target = app.state.services.alerts.recent()[0]["id"]
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    assert client.post(f"/api/alerts/{target}/resolve").status_code == 403

def test_settings_snapshot_carries_alert_owner(admin_client):
    _app, client = admin_client
    by_key = {item["key"]: item for item in client.get("/api/settings").json()["settings"]}
    assert by_key["alert_enabled"]["task"] == "alert"
    assert by_key["alert_webhook_url"]["task"] == "alert"
    assert by_key["alert_error_rate_threshold"]["task"] == "alert"
    assert by_key["alert_enabled"]["group"] is None

def test_alert_settings_hot_update(tmp_path):
    """告警阈值可热更：写入后立即生效，恢复默认回落 env。"""
    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import load_runtime_settings

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    base = Settings(_env_file=None, APP_SECRET=SECRET)
    runtime = load_runtime_settings(base, RuntimeSettingsRepository(db))
    assert runtime.alert_task_failures == 3
    runtime.set_many({"alert_task_failures": 5, "alert_webhook_url": "https://x"})
    assert runtime.alert_task_failures == 5
    assert runtime.alert_webhook_url == "https://x"
    runtime.set_many({"alert_task_failures": None, "alert_webhook_url": None})
    assert runtime.alert_task_failures == 3
    assert runtime.alert_webhook_url == ""
    db.close()

def test_alert_webhook_rejects_non_http_scheme_and_too_many(tmp_path):
    """webhook 只接受 http/https，且条目数有上限。"""

    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import InvalidSetting, load_runtime_settings

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    base = Settings(_env_file=None, APP_SECRET=SECRET)
    runtime = load_runtime_settings(base, RuntimeSettingsRepository(db))
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x"):
        with pytest.raises(InvalidSetting, match="只支持 http/https"):
            runtime.set_many({"alert_webhook_url": bad})
    too_many = ",".join(f"https://h{i}" for i in range(20))
    with pytest.raises(InvalidSetting, match="最多"):
        runtime.set_many({"alert_webhook_url": too_many})
    # SSRF：内网/回环/链路本地（云元数据）地址一律拒
    for blocked in ("http://127.0.0.1:9000/x", "http://169.254.169.254/latest/meta-data",
                    "http://10.0.0.5/hook", "http://[::1]:80/hook",
                    "http://localhost/hook", "http://224.0.0.1/hook",
                    "http://0.0.0.0/hook"):
        with pytest.raises(InvalidSetting, match="内网/回环/保留"):
            runtime.set_many({"alert_webhook_url": blocked})
    # 无主机名
    with pytest.raises(InvalidSetting, match="缺少主机名"):
        runtime.set_many({"alert_webhook_url": "http:///nohost"})
    # 逗号分隔的合法多地址通过
    runtime.set_many({"alert_webhook_url": "https://a, http://b"})
    assert runtime.alert_webhook_url == "https://a, http://b"
    db.close()


def test_blocked_webhook_host_covers_literal_and_resolved_paths(monkeypatch):
    """投递前复核（SSRF 兜底）：字面量 IP 直接判，域名看解析结果，失败即阻断。"""
    from src.runtime_settings import blocked_webhook_host

    # 无主机名 / 字面量黑名单：不查 DNS 直接判
    assert blocked_webhook_host("http:///nohost") is True
    assert blocked_webhook_host("http://127.0.0.1/hook") is True

    # 域名解析到公网 → 放行
    monkeypatch.setattr("src.runtime_settings.socket.getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert blocked_webhook_host("https://hook.example/a") is False

    # 域名解析到内网（云元数据 169.254.169.254）→ 阻断
    monkeypatch.setattr("src.runtime_settings.socket.getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("169.254.169.254", 0))])
    assert blocked_webhook_host("https://metadata.example/latest") is True

    # IPv6 链路本地带 `%en0` 作用域后缀：剥掉后再判，不得因后缀漏过
    monkeypatch.setattr("src.runtime_settings.socket.getaddrinfo",
                        lambda host, port: [(10, 1, 6, "", ("fe80::1%en0", 0, 0, 0))])
    assert blocked_webhook_host("https://linklocal.example/x") is True

    # 解析失败（NXDOMAIN/网络不通）→ fail closed，宁可漏报一条告警
    def _boom(host, port):
        raise OSError("name resolution failed")

    monkeypatch.setattr("src.runtime_settings.socket.getaddrinfo", _boom)
    assert blocked_webhook_host("https://unresolvable.example/x") is True


def test_env_webhook_url_is_validated_at_load(tmp_path):
    """env 提供的 ALERT_WEBHOOK_URL 也要过校验（不能只靠运行时失败兜底）。"""
    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import InvalidSetting, load_runtime_settings

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    base = Settings(_env_file=None, APP_SECRET=SECRET,
                    ALERT_WEBHOOK_URL="http://169.254.169.254/latest")
    with pytest.raises(InvalidSetting, match="内网/回环/保留"):
        load_runtime_settings(base, RuntimeSettingsRepository(db))
    db.close()


def test_feishu_and_slack_urls_share_webhook_validation(tmp_path):
    """新增出口与通用 webhook 同一套 URL 校验，不能成为 SSRF 侧门。"""
    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import InvalidSetting, load_runtime_settings

    db = Database(tmp_path / "t2.sqlite3")
    apply_schema(db.connect())
    base = Settings(_env_file=None, APP_SECRET=SECRET)
    runtime = load_runtime_settings(base, RuntimeSettingsRepository(db))
    for key in ("alert_feishu_webhook_url", "alert_slack_webhook_url"):
        with pytest.raises(InvalidSetting, match="只支持 http/https"):
            runtime.set_many({key: "file:///etc/passwd"})
        with pytest.raises(InvalidSetting, match="内网/回环/保留"):
            runtime.set_many({key: "http://127.0.0.1/x"})
        runtime.set_many({key: "https://hooks.example/x"})
        assert getattr(runtime, key) == "https://hooks.example/x"
    db.close()
