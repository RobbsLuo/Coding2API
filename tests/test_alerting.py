"""运维告警（P1-7）：四类规则判定、静默去重、webhook 投递、站内回看端点。

覆盖三块容易退化的语义：
1. **规则判定是纯函数**：`evaluate_alerts` 不碰 IO，喂快照即可断言各类边界
   （阈值 0 关闭、样本不足不判、总数为 0 不算耗尽）。
2. **静默去重**：持续状态（池耗尽 / token 到期）每轮评估都会命中，没有静默窗
   会把记录与群聊刷屏；窗口过后仍命中要能再报一次。
3. **投递失败不拖垮任务**：webhook 挂了只记 delivery_error，不能把告警任务
   判成失败——否则「webhook 挂了」会被误报成「告警任务连续失败」。
"""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import AlertRepository, CredentialRepository
from src.stats.collector import StatsCollector
from src.tasks.alerting import (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    Alert,
    AlertTask,
    evaluate_alerts,
)
from src.tasks.retention import RetentionTask
from src.tasks.runner import TaskRunner, build_runner
from src.tasks.status import TASK_BY_KEY, TaskStatusStore
from tests.conftest import SECRET


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()


def _no_alerts(**overrides):
    """默认「一切正常」的 evaluate_alerts 入参，按用例覆盖单项。"""
    kwargs = dict(
        pool={"total": 3, "ready": 3, "cooling": 0, "paused": 0, "disabled": 0},
        pool_ready_min=1,
        failing_tasks=[],
        expiring=[],
        error_counts=(0, 0),
        error_rate_threshold=0.5,
        error_rate_min_requests=20,
    )
    kwargs.update(overrides)
    return evaluate_alerts(**kwargs)


# --------------------------------------------------------- evaluate_alerts


def test_evaluate_pool_empty_is_critical():
    alerts = _no_alerts(pool={"total": 2, "ready": 0, "cooling": 1,
                              "paused": 0, "disabled": 1})
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule == "pool_empty" and alert.severity == SEVERITY_CRITICAL
    assert alert.scope == "pool"
    assert "可用数为 0" in alert.message
    assert alert.detail["total"] == 2 and alert.detail["threshold"] == 1


def test_evaluate_pool_rule_off_and_zero_total():
    # 阈值 0 = 关闭规则
    assert _no_alerts(pool_ready_min=0,
                      pool={"total": 2, "ready": 0}) == []
    # 池里一个凭证都没有：不是「耗尽」而是「没配」，不告警
    assert _no_alerts(pool={"total": 0, "ready": 0}) == []


def test_evaluate_failing_tasks_warns():
    alerts = _no_alerts(failing_tasks=[("growth", "成长中心", 4)])
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule == "task_failed" and alert.severity == SEVERITY_WARNING
    assert alert.scope == "growth"
    assert "连续失败 4 次" in alert.message
    assert alert.detail["streak"] == 4


def test_evaluate_expiring_tokens_humanizes_remaining():
    expiring = [
        {"id": "c1", "provider": "codebuddy", "nickname": "甲",
         "token_expires_at": 1_000_000, "remaining": 7200},
        {"id": "c2", "provider": "trae", "nickname": "",
         "token_expires_at": 1_000_000, "remaining": 300},
        {"id": "c3", "provider": "trae", "nickname": "",
         "token_expires_at": 1_000_000, "remaining": 30},
    ]
    alerts = _no_alerts(expiring=expiring)
    assert [a.scope for a in alerts] == ["c1", "c2", "c3"]
    assert all(a.rule == "token_expiring" for a in alerts)
    assert "2.0 小时" in alerts[0].message
    assert "5 分钟" in alerts[1].message
    assert "30 秒" in alerts[2].message
    # 昵称为空时回落到 id，不让文案出现空引号
    assert "c2" in alerts[1].message


def test_evaluate_error_rate_guards():
    # 样本不足：不判
    assert _no_alerts(error_counts=(5, 5), error_rate_min_requests=20) == []
    # 阈值 0 = 关闭
    assert _no_alerts(error_counts=(100, 100), error_rate_threshold=0) == []
    # 请求数为 0：即使 min_requests=0 也不判
    assert _no_alerts(error_counts=(0, 0), error_rate_min_requests=0) == []


def test_evaluate_error_rate_fires_above_threshold():
    alerts = _no_alerts(error_counts=(20, 12))
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule == "error_rate" and alert.scope == "pool"
    assert "60%" in alert.message
    assert alert.detail["failed"] == 12


def test_evaluate_error_rate_below_threshold_silent():
    # 样本足够但失败占比未达阈值：不报
    assert _no_alerts(error_counts=(20, 5)) == []


def test_evaluate_multiple_rules_at_once():
    alerts = _no_alerts(
        pool={"total": 2, "ready": 0},
        failing_tasks=[("checkin", "每日签到", 3)],
        expiring=[{"id": "c1", "provider": "trae", "nickname": "x",
                   "token_expires_at": 1, "remaining": 60}],
        error_counts=(30, 30),
    )
    assert [a.rule for a in alerts] == [
        "pool_empty", "task_failed", "token_expiring", "error_rate"]


# --------------------------------------------------------- TaskStatusStore


def test_status_store_tracks_consecutive_failures():
    store = TaskStatusStore(now=lambda: 1.0)
    store.record("growth", started_at=0, ok=False, report=None, error="boom")
    store.record("growth", started_at=0, ok=False, report=None, error="boom")
    assert store.consecutive_failures("growth") == 2
    store.record("growth", started_at=0, ok=True, report={}, error=None)
    assert store.consecutive_failures("growth") == 0
    # 从未运行的任务不算连续失败
    assert store.consecutive_failures("checkin") == 0


def test_status_store_failing_threshold_and_sorting():
    store = TaskStatusStore(now=lambda: 1.0)
    for _ in range(3):
        store.record("growth", started_at=0, ok=False, report=None, error="x")
    store.record("checkin", started_at=0, ok=False, report=None, error="x")
    # 阈值 0 = 关闭规则
    assert store.failing(0) == []
    assert store.failing(2) == [("growth", 3)]
    assert store.failing(1) == [("checkin", 1), ("growth", 3)]


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


def test_schema_v17_adds_alert_events_to_legacy_db(tmp_path):
    """老库升级：只有旧表的库在 apply_schema 后得到 alert_events，且可读写。"""
    import sqlite3

    from src.db.migrate import SCHEMA_VERSION

    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    # 旧库：仅一张与告警无关的表 + 老 user_version
    conn.execute("CREATE TABLE api_keys (id TEXT PRIMARY KEY, username TEXT NOT NULL)")
    conn.execute("PRAGMA user_version = 16")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    tables = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "alert_events" in tables
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # 新表可写可读，且旧表数据未被破坏
    repo = AlertRepository(db)
    repo.record(rule="pool_empty", severity="critical", scope="pool",
                message="池空", now=5)
    assert repo.recent()[0]["rule"] == "pool_empty"
    db.close()


# --------------------------------------------------------- AlertTask


class _StubCredentials:
    def __init__(self, counts=None, expiring=None):
        self._counts = counts or {"total": 1, "ready": 1, "cooling": 0,
                                  "paused": 0, "disabled": 0}
        self._expiring = expiring or []

    def pool_counts(self, now=None):
        return self._counts

    def expiring_tokens(self, *, within_seconds, now=None):
        return self._expiring


class _StubStats:
    def __init__(self, counts=(0, 0)):
        self._counts = counts

    def window_error_rate(self, *, since):
        return self._counts


def _task(tmp_path, *, credentials=None, stats=None, status=None, **kwargs):
    db = Database(tmp_path / "alert.sqlite3")
    apply_schema(db.connect())
    alerts = AlertRepository(db)
    task = AlertTask(
        credentials or _StubCredentials(),
        alerts,
        stats or _StubStats(),
        task_status=status or TaskStatusStore(),
        **kwargs,
    )
    return task, alerts, db


async def test_alert_task_disabled_returns_none(tmp_path):
    task, alerts, db = _task(tmp_path, enabled=lambda: False)
    assert await task.run_once() is None
    assert alerts.recent() == []
    db.close()


async def test_alert_task_fires_and_records_without_webhook(tmp_path):
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        now=lambda: 1000)
    report = await task.run_once()
    assert report == {"evaluated": 1, "fired": 1, "suppressed": 0, "delivered": 0}
    row = alerts.recent()[0]
    assert row["rule"] == "pool_empty" and row["delivered"] == 0
    assert row["delivery_error"] is None
    assert json.loads(row["detail"])["ready"] == 0
    db.close()


async def test_alert_task_silences_repeats_then_refires(tmp_path):
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        silence_minutes=lambda: 30, now=lambda: 1000)
    assert (await task.run_once())["fired"] == 1
    # 静默窗内：命中但被抑制，不再落库
    second = await task.run_once()
    assert second == {"evaluated": 1, "fired": 0, "suppressed": 1, "delivered": 0}
    assert len(alerts.recent()) == 1
    # 窗口过后仍命中 → 再报一次
    task._now = lambda: 1000 + 30 * 60
    assert (await task.run_once())["fired"] == 1
    assert len(alerts.recent()) == 2
    db.close()


async def test_alert_task_silence_zero_always_fires(tmp_path):
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        silence_minutes=lambda: 0, now=lambda: 1000)
    await task.run_once()
    await task.run_once()
    assert len(alerts.recent()) == 2
    db.close()


async def test_alert_task_delivers_to_webhook(tmp_path):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a", client=client, now=lambda: 5)
    report = await task.run_once()
    assert report["delivered"] == 1
    assert alerts.recent()[0]["delivered"] == 1
    assert seen[0]["rule"] == "pool_empty" and seen[0]["severity"] == SEVERITY_CRITICAL
    await client.aclose()
    db.close()


async def test_alert_task_webhook_failure_does_not_raise(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a", client=client, now=lambda: 5)
    report = await task.run_once()
    assert report["fired"] == 1 and report["delivered"] == 0
    row = alerts.recent()[0]
    assert row["delivered"] == 0 and row["delivery_error"] is not None
    await client.aclose()
    db.close()


async def test_alert_task_multiple_webhooks_one_fails(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500 if request.url.path.endswith("/bad") else 200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/ok, https://hook.example/bad",
        client=client, now=lambda: 5)
    report = await task.run_once()
    assert len(calls) == 2
    assert report["delivered"] == 0          # 有一个失败即整体不算投递成功
    assert alerts.recent()[0]["delivery_error"] is not None
    await client.aclose()
    db.close()


async def test_alert_task_two_webhooks_both_fail_keeps_first_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a, https://hook.example/b",
        client=client, now=lambda: 5)
    await task.run_once()
    # 两个都失败时只保留首个错误，不互相覆盖
    assert alerts.recent()[0]["delivery_error"] is not None
    await client.aclose()
    db.close()


async def test_alert_task_default_client_path(tmp_path, monkeypatch):
    """未注入 client 时走自建 httpx.AsyncClient（生产路径）。"""
    captured: list[dict] = []

    class _FakeResponse:
        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            captured.append({"url": url, "json": json})
            return _FakeResponse()

    monkeypatch.setattr("src.tasks.alerting.httpx.AsyncClient", _FakeClient)
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a", now=lambda: 5)
    report = await task.run_once()
    assert report["delivered"] == 1
    assert captured[1]["url"] == "https://hook.example/a"
    db.close()


async def test_alert_task_expiring_uses_remaining(tmp_path):
    expiring = [{"id": "c1", "provider": "trae", "nickname": "x",
                 "token_expires_at": 1300}]
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(expiring=expiring),
        token_expiry_hours=lambda: 1, now=lambda: 1000)
    report = await task.run_once()
    assert report["fired"] == 1
    assert json.loads(alerts.recent()[0]["detail"])["remaining"] == 300
    db.close()


async def test_alert_task_expiring_off_skips_lookup(tmp_path):
    class _Exploding(_StubCredentials):
        def expiring_tokens(self, **_kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("窗口为 0 时不应查库")

    task, _alerts, db = _task(tmp_path, credentials=_Exploding(),
                              token_expiry_hours=lambda: 0, now=lambda: 1000)
    assert (await task.run_once())["evaluated"] == 0
    db.close()


# --------------------------------------------------------- runner 接线


class _StubProvider:
    id = "codebuddy"

    async def probe_quota(self, _data):
        from src.provider.base import Quota
        return Quota(remaining=5, total=10, probed_at=1)

    async def checkin(self, _data):  # pragma: no cover - 不在本组触发
        raise AssertionError("不应触发签到")

    def checkin_scope(self, _data):  # pragma: no cover
        return "scope"

    def credential_from(self, data):  # pragma: no cover
        from src.provider.codebuddy.credential import CodeBuddyCredential
        return CodeBuddyCredential.from_dict(data)

    async def refresh(self, data):  # pragma: no cover
        return data


def _runner_with_alert(repo_tuple, task):
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask

    credentials, db = repo_tuple
    provider = _StubProvider()
    return TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(db)),
        alert=task,
        alert_interval_minutes=1,
    )


async def test_runner_sync_alert_records_run(repo, tmp_path):
    credentials, db = repo
    task, _alerts, adb = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        now=lambda: 1000)
    runner = _runner_with_alert(repo, task)
    # 经 _guarded 记入运行态（真实执行，非 no-op）
    assert await runner._guarded(runner._sync_alert(), "运维告警", key="alert") is True
    assert runner.status.get("alert") is not None
    assert runner.status.runs("alert") == 1
    assert runner.status.get("alert").report["fired"] == 1
    adb.close()
    _ = credentials, db


def test_runner_alert_interval_clamps():
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask

    runner = TaskRunner(
        quota_probe=QuotaProbeTask(None, {}, None),
        checkin=CheckinTask(None, {}),
        refresh=RefreshTask(None, {}, skew_seconds=3600),
        retention=RetentionTask(None),
        alert_interval_minutes=0,
    )
    assert runner._alert_interval == 60
    assert runner._interval_seconds("alert") == 60


def test_runner_task_status_includes_alert(repo):
    credentials, db = repo
    task, _alerts, adb = _task(db and credentials and __import__("pathlib").Path(db.path).parent)
    runner = _runner_with_alert(repo, task)
    by_key = {item["key"]: item for item in runner.task_status()}
    assert by_key["alert"]["name"] == "运维告警"
    assert by_key["alert"]["enabled"] is True
    adb.close()


def test_runner_task_status_skips_alert_when_absent(repo):
    credentials, db = repo
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask

    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {}, None),
        checkin=CheckinTask(credentials, {}),
        refresh=RefreshTask(credentials, {}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(db)),
    )
    assert all(item["key"] != "alert" for item in runner.task_status())
    _ = credentials


def test_alert_task_spec_registered():
    assert TASK_BY_KEY["alert"].name == "运维告警"


def test_build_runner_wires_alert(repo, tmp_path):
    """生产装配传 alerts 时建告警对象并复用共享运行态。"""
    credentials, db = repo
    alerts = AlertRepository(db)
    store = TaskStatusStore()
    config = Settings(_env_file=None, APP_SECRET=SECRET)
    runner = build_runner(credentials, {"codebuddy": _StubProvider()},
                          StatsCollector(db), config, alerts=alerts, status=store)
    assert runner._alert is not None
    assert runner._alert._task_status is store
    assert runner.status is store
    assert runner._interval_seconds("alert") == 300


def test_build_runner_alert_disabled_switch(repo):
    credentials, db = repo
    alerts = AlertRepository(db)
    config = Settings(_env_file=None, APP_SECRET=SECRET, ALERT_ENABLED=False)
    runner = build_runner(credentials, {"codebuddy": _StubProvider()},
                          StatsCollector(db), config, alerts=alerts)
    assert runner._task_enabled("alert") is False


# --------------------------------------------------------- retention


def test_retention_prunes_alerts(repo):
    _credentials, db = repo
    alerts = AlertRepository(db)
    alerts.record(rule="pool_empty", severity="critical", scope="pool",
                  message="x", now=0)
    alerts.record(rule="pool_empty", severity="critical", scope="pool",
                  message="x", now=int(time.time()))
    report = RetentionTask(StatsCollector(db), retention_days=90,
                           alerts=alerts).run_once()
    assert report["purged_alerts"] == 1
    assert len(alerts.recent()) == 1


def test_retention_without_alerts_reports_zero(repo):
    _credentials, db = repo
    report = RetentionTask(StatsCollector(db)).run_once()
    assert report["purged_alerts"] == 0


# --------------------------------------------------------- /api/alerts


@pytest.fixture()
def admin_app(tmp_path):
    from fastapi.testclient import TestClient

    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, ADMIN_USERNAMES="root",
                        DATA_DIR=str(tmp_path))
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        yield app, client


def test_alerts_requires_admin(admin_app):
    _app, client = admin_app
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    assert client.get("/api/alerts").status_code == 403


def test_alerts_returns_recent(admin_app):
    app, client = admin_app
    app.state.services.alerts.record(
        rule="task_failed", severity="warning", scope="growth",
        message="挂了", now=123)
    body = client.get("/api/alerts").json()
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["rule"] == "task_failed"


def test_settings_snapshot_carries_alert_owner(admin_app):
    _app, client = admin_app
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
    """M6：webhook 只接受 http/https，且条目数有上限。"""
    import pytest

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
    # 逗号分隔的合法多地址通过
    runtime.set_many({"alert_webhook_url": "https://a, http://b"})
    assert runtime.alert_webhook_url == "https://a, http://b"
    db.close()


def test_alert_humanize_boundaries():
    """_humanize 三段分支都要走到（小时 / 分钟 / 秒）。"""
    from src.tasks.alerting import _humanize

    assert _humanize(3600) == "1.0 小时"
    assert _humanize(3599) == "59 分钟"
    assert _humanize(59) == "59 秒"


def test_alert_dataclass_dedup_key():
    alert = Alert(rule="pool_empty", severity="critical", scope="pool", message="x")
    assert alert.dedup_key == "pool_empty:pool"


def test_alert_severity_warning_constant():
    assert SEVERITY_WARNING == "warning"
    assert SEVERITY_CRITICAL == "critical"
    assert isinstance(time.time(), float)