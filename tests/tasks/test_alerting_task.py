"""运维告警：AlertTask 静默去重、webhook 投递与脱敏。"""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from src.tasks.alerting import SEVERITY_CRITICAL, SEVERITY_WARNING, Alert
from src.tasks.status import TASK_BY_KEY
from tests.tasks._alerting_support import StubCredentials as _StubCredentials
from tests.tasks._alerting_support import make_alert_task as _task


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
    assert report == {"evaluated": 1, "fired": 1, "suppressed": 0, "pending": 0,
                      "resolved": 0, "delivered": 0}
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
    assert second == {"evaluated": 1, "fired": 0, "suppressed": 1, "pending": 0,
                      "resolved": 0, "delivered": 0}
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

async def test_alert_task_redacts_webhook_secret_from_log_and_db(tmp_path, caplog):
    """webhook URL 的 path/query 常即密钥，日志与落库都不得带上整条 URL。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    secret_url = "https://hooks.example/T000/B000/super-secret-token"
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: secret_url, client=client, now=lambda: 5)
    with caplog.at_level("WARNING"):
        await task.run_once()
    # 日志只留 host，不带密钥 path
    assert "super-secret-token" not in caplog.text
    assert "hooks.example" in caplog.text
    # 落库的 delivery_error 同样不含密钥（httpx 异常文本会内嵌整条 URL）
    error = alerts.recent()[0]["delivery_error"]
    assert "super-secret-token" not in error
    await client.aclose()
    db.close()

def test_redact_webhook_url_falls_back_for_opaque_value():
    """非 URL 的 webhook 值（如误配成裸主机名）也一律脱敏为占位串。"""
    from src.tasks.alerting import _redact_webhook_url

    assert _redact_webhook_url("not-a-url") == "<webhook>"
    assert _redact_webhook_url("https://hook.example/x") == "https://hook.example"

async def test_alert_task_blocks_webhook_resolving_to_private_host(tmp_path, monkeypatch):
    """投递前 DNS 复核命中内网 → 跳过投递并记脱敏错误（SSRF 兜底）。"""
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(str(request.url))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # 覆盖 tests/tasks/conftest.py 的放行桩，走真实复核逻辑
    monkeypatch.setattr("src.tasks.alerting.blocked_webhook_host", lambda url: True)
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hooks.example/super-secret-token",
        client=client, now=lambda: 5)
    report = await task.run_once()
    assert report["delivered"] == 0
    assert posted == []                      # 根本没发出去
    row = alerts.recent()[0]
    assert row["delivered"] == 0
    assert "BlockedWebhookHost" in row["delivery_error"]
    assert "super-secret-token" not in row["delivery_error"]
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

        async def post(self, url, content=None, headers=None):
            captured.append({"url": url, "json": json.loads(content),
                             "headers": headers})
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

async def test_alert_task_webhook_redacts_pii_nickname(tmp_path):
    """nickname 常被填成邮箱/手机号：站内留原文，外发 webhook 必须打码。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    expiring = [{"id": "c1", "provider": "trae", "nickname": "user@example.com",
                 "token_expires_at": 1300}]
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(expiring=expiring),
        token_expiry_hours=lambda: 1,
        webhook_url=lambda: "https://hook.example/a", client=client, now=lambda: 1000)
    report = await task.run_once()
    assert report["delivered"] == 1
    # 站内记录保留原文，便于定位
    assert "user@example.com" in json.loads(alerts.recent()[0]["detail"])["nickname"]
    # 外发 payload 的 message / detail 都不得含 PII
    assert "user@example.com" not in json.dumps(seen[0], ensure_ascii=False)
    assert seen[0]["detail"]["nickname"] == "***"
    await client.aclose()
    db.close()


async def test_alert_task_empty_nickname_not_masked_in_payload(tmp_path):
    """昵称为空时不产生 `***` 噪声（保持空串）。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    expiring = [{"id": "c1", "provider": "trae", "nickname": "",
                 "token_expires_at": 1300}]
    task, _alerts, db = _task(
        tmp_path, credentials=_StubCredentials(expiring=expiring),
        token_expiry_hours=lambda: 1,
        webhook_url=lambda: "https://hook.example/a", client=client, now=lambda: 1000)
    await task.run_once()
    assert seen[0]["detail"]["nickname"] == ""
    await client.aclose()
    db.close()


async def test_alert_task_webhook_total_budget_exceeded(tmp_path, monkeypatch):
    """多地址总投递超预算：本轮判失败，不把后台循环拖死。"""
    monkeypatch.setattr("src.tasks.alerting._WEBHOOK_TOTAL_BUDGET", 0.01)

    class _SlowClient:
        async def post(self, url, content=None, headers=None):
            await asyncio.sleep(1)
            return httpx.Response(200)

    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a", client=_SlowClient(),
        now=lambda: 5)
    report = await task.run_once()
    assert report["delivered"] == 0
    assert alerts.recent()[0]["delivery_error"] is not None
    db.close()


def test_public_detail_masks_only_present_pii():
    from src.tasks.alerting import _public_detail

    assert _public_detail({"nickname": "a@b.c", "provider": "trae"}) == {
        "nickname": "***", "provider": "trae"}
    assert _public_detail({"nickname": "", "provider": "trae"}) == {
        "nickname": "", "provider": "trae"}


def test_alert_public_message_masks_pii_fragments():
    from src.tasks.alerting import Alert

    alert = Alert(rule="token_expiring", severity=SEVERITY_WARNING, scope="c1",
                  message="凭证「a@b.c」即将到期", pii=("a@b.c",))
    assert alert.public_message() == "凭证「***」即将到期"
    # 空片段不参与替换（避免把整条文案打散）
    assert Alert(rule="r", severity=SEVERITY_WARNING, scope="s",
                 message="x", pii=("",)).public_message() == "x"


def test_alert_task_spec_registered():
    assert TASK_BY_KEY["alert"].name == "运维告警"

def test_alert_humanize_boundaries():
    """_humanize 三段分支都要走到（小时 / 分钟 / 秒）。"""
    from src.tasks.alerting import _humanize

    assert _humanize(3600) == "1.0 小时"
    assert _humanize(3599) == "59 分钟"
    assert _humanize(59) == "59 秒"

def test_alert_dataclass_dedup_key():
    alert = Alert(rule="pool_empty", severity="critical", scope="pool", message="x")
    assert alert.dedup_key == "pool_empty:pool"

# ------------------------------------------------------------------ 迟滞 / 恢复


async def test_alert_task_for_ticks_defers_until_streak(tmp_path):
    """连续命中 for_ticks 轮才真正告警；之前只累计（pending，不落库）。"""
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        for_ticks=lambda: 2, silence_minutes=lambda: 0, now=lambda: 1000)
    first = await task.run_once()
    assert first["pending"] == 1 and first["fired"] == 0
    assert alerts.recent() == []
    second = await task.run_once()
    assert second["pending"] == 0 and second["fired"] == 1
    assert len(alerts.recent()) == 1
    db.close()


async def test_alert_task_streak_resets_when_signal_clears(tmp_path):
    """未达阈值前信号消失 → 连击清零，下次重新从 1 开始累积。"""
    creds = _StubCredentials(counts={"total": 1, "ready": 0})
    task, alerts, db = _task(
        tmp_path, credentials=creds, for_ticks=lambda: 2,
        silence_minutes=lambda: 0, now=lambda: 1000)
    assert (await task.run_once())["pending"] == 1
    creds._counts = {"total": 1, "ready": 1}          # 恢复
    assert (await task.run_once())["evaluated"] == 0
    creds._counts = {"total": 1, "ready": 0}          # 再次命中
    assert (await task.run_once())["pending"] == 1    # 重新累积，而非立即报
    assert alerts.recent() == []
    db.close()


async def test_alert_task_clear_ticks_resolves_and_notifies(tmp_path):
    """告警中连续 clear_ticks 轮未命中 → 写 resolved_at 并外推恢复通知。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    creds = _StubCredentials(counts={"total": 1, "ready": 0})
    task, alerts, db = _task(
        tmp_path, credentials=creds, clear_ticks=lambda: 2,
        webhook_url=lambda: "https://hook.example/a", client=client,
        silence_minutes=lambda: 0, now=lambda: 1000)
    assert (await task.run_once())["fired"] == 1
    creds._counts = {"total": 1, "ready": 1}          # 恢复
    # 第 1 轮未命中：还没到 clear_ticks，不算恢复
    first = await task.run_once()
    assert first["resolved"] == 0
    assert alerts.recent()[0]["resolved_at"] is None
    # 第 2 轮未命中：确认恢复
    second = await task.run_once()
    assert second["resolved"] == 1 and second["delivered"] == 1
    assert alerts.recent()[0]["resolved_at"] == 1000
    # 外推的恢复通知带 status=resolved
    assert seen[-1]["status"] == "resolved"
    assert seen[-1]["rule"] == "pool_empty"
    assert "已恢复" in seen[-1]["message"]
    await client.aclose()
    db.close()


async def test_alert_task_clear_streak_resets_on_rehit(tmp_path):
    """恢复迟滞中又重新命中 → 恢复连击清零，告警保持有效、不误撤。"""
    creds = _StubCredentials(counts={"total": 1, "ready": 0})
    task, alerts, db = _task(
        tmp_path, credentials=creds, clear_ticks=lambda: 3,
        silence_minutes=lambda: 0, now=lambda: 1000)
    assert (await task.run_once())["fired"] == 1
    creds._counts = {"total": 1, "ready": 1}
    assert (await task.run_once())["resolved"] == 0
    creds._counts = {"total": 1, "ready": 0}          # 又命中
    assert (await task.run_once())["fired"] == 1      # 静默窗 0 → 再报一次
    creds._counts = {"total": 1, "ready": 1}
    # 之前的 1 轮恢复连击已清零，需重新攒满 3 轮
    assert (await task.run_once())["resolved"] == 0
    assert (await task.run_once())["resolved"] == 0
    assert (await task.run_once())["resolved"] == 1
    db.close()


async def test_alert_task_resolve_without_webhook_writes_only(tmp_path):
    """未配置 webhook：恢复只写库，delivered 保持 0。"""
    creds = _StubCredentials(counts={"total": 1, "ready": 0})
    task, alerts, db = _task(
        tmp_path, credentials=creds, clear_ticks=lambda: 1,
        silence_minutes=lambda: 0, now=lambda: 1000)
    await task.run_once()
    creds._counts = {"total": 1, "ready": 1}
    report = await task.run_once()
    assert report["resolved"] == 1 and report["delivered"] == 0
    assert alerts.recent()[0]["resolved_at"] == 1000
    db.close()


# ------------------------------------------------- 重启后重建「告警中」集合


def _task_after_restart(tmp_path, rows, **kwargs):
    """先落库若干告警行、再构造任务：模拟「重启后进程内状态为空、库里仍有未恢复行」。"""
    from src.db.conn import Database
    from src.db.migrate import apply_schema
    from src.db.repo import AlertRepository
    from src.tasks.alerting import AlertTask
    from src.tasks.status import TaskStatusStore
    from tests.tasks._alerting_support import StubStats

    db = Database(tmp_path / "alert.sqlite3")
    apply_schema(db.connect())
    alerts = AlertRepository(db)
    for row in rows:
        alerts.record(**row)
    task = AlertTask(
        kwargs.pop("credentials", _StubCredentials()),
        alerts, kwargs.pop("stats", StubStats()),
        task_status=kwargs.pop("status", TaskStatusStore()), **kwargs)
    return task, alerts, db


async def test_alert_task_rehydrates_and_resolves_after_restart(tmp_path):
    """重启后重建「告警中」：条件已消失的历史未恢复行被正常标记恢复。

    不重建的话恢复分支只遍历空的 `_active`，那行永远等不到 `resolved_at`
    ——角标（数 `resolved_at IS NULL`）与「已恢复」标记就永久卡住。
    """
    task, alerts, db = _task_after_restart(
        tmp_path,
        [dict(rule="pool_empty", severity=SEVERITY_CRITICAL, scope="pool",
              message="池空", detail='{"ready": 0}', now=100)],
        credentials=_StubCredentials(counts={"total": 1, "ready": 1}),  # 已恢复
        clear_ticks=lambda: 1, silence_minutes=lambda: 0, now=lambda: 1000)
    report = await task.run_once()
    assert report["resolved"] == 1
    assert alerts.recent()[0]["resolved_at"] == 1000
    assert alerts.count_unresolved() == 0
    db.close()


async def test_alert_task_rehydrated_entries_keep_latest_row_per_key(tmp_path):
    """同一 (rule, scope) 的多次提醒：重建按键去重、以最后一行为准，恢复时一起收尾。"""
    task, alerts, db = _task_after_restart(
        tmp_path,
        [dict(rule="pool_empty", severity=SEVERITY_CRITICAL, scope="pool",
              message="第一次", now=100),
         dict(rule="pool_empty", severity=SEVERITY_CRITICAL, scope="pool",
              message="第二次", now=200)],
        credentials=_StubCredentials(counts={"total": 1, "ready": 1}),
        clear_ticks=lambda: 1, silence_minutes=lambda: 0, now=lambda: 1000)
    assert list(task._active) == ["pool_empty:pool"]
    assert task._active["pool_empty:pool"].message == "第二次"
    assert (await task.run_once())["resolved"] == 1
    assert alerts.count_unresolved() == 0
    db.close()


async def test_alert_task_rehydrated_entry_respects_silence_window(tmp_path):
    """重建的「告警中」条目仍受静默窗约束：窗口内再次命中不重复落库。"""
    task, alerts, db = _task_after_restart(
        tmp_path,
        [dict(rule="pool_empty", severity=SEVERITY_CRITICAL, scope="pool",
              message="旧", now=1000)],
        credentials=_StubCredentials(counts={"total": 1, "ready": 0}),  # 仍命中
        silence_minutes=lambda: 30, now=lambda: 1000)
    report = await task.run_once()
    assert report == {"evaluated": 1, "fired": 0, "suppressed": 1, "pending": 0,
                      "resolved": 0, "delivered": 0}
    assert len(alerts.recent()) == 1
    db.close()


async def test_alert_task_rehydrated_resolve_masks_pii(tmp_path):
    """重建的历史行仍带 PII 片段：恢复通知外发时昵称要打码，不泄露原文。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, _alerts, db = _task_after_restart(
        tmp_path,
        [dict(rule="token_expiring", severity=SEVERITY_WARNING, scope="cred_1",
              message="凭证「a@b.c」即将到期",
              detail=json.dumps({"nickname": "a@b.c"}), now=100)],
        credentials=_StubCredentials(counts={"total": 1, "ready": 1}),
        clear_ticks=lambda: 1, silence_minutes=lambda: 0,
        webhook_url=lambda: "https://hook.example/a", client=client,
        now=lambda: 1000)
    await task.run_once()
    assert seen[-1]["status"] == "resolved"
    assert "a@b.c" not in seen[-1]["message"] and "***" in seen[-1]["message"]
    assert seen[-1]["detail"]["nickname"] == "***"
    await client.aclose()
    db.close()


def test_alert_from_row_tolerates_dirty_detail():
    """重建时 detail 可能是空串 / 非法 JSON / 非对象（历史脏行）——一律归空。"""
    from src.tasks.alerting import _alert_from_row

    base = {"rule": "pool_empty", "severity": SEVERITY_CRITICAL, "scope": "pool",
            "message": "池空"}
    for dirty in ("", "not-json", "[1, 2]", '"text"', None):
        rebuilt = _alert_from_row({**base, "detail": dirty})
        assert rebuilt.detail == {} and rebuilt.pii == ()
    # 有 nickname 则反推出 PII 片段（恢复通知据此打码）
    rebuilt = _alert_from_row({**base, "detail": json.dumps({"nickname": "a@b.c"})})
    assert rebuilt.pii == ("a@b.c",)


def test_sign_webhook_body_is_stable_hmac():
    from src.tasks.alerting import sign_webhook_body

    body = b'{"a":1}'
    first = sign_webhook_body("secret", body)
    assert first == sign_webhook_body("secret", body)      # 确定性
    assert first.startswith("sha256=")
    assert first != sign_webhook_body("other", body)       # 换密钥即变
    assert sign_webhook_body("", body) == ""               # 无密钥不加签名头


async def test_alert_task_signs_webhook_body(tmp_path):
    """webhook 带 HMAC 签名，且签名覆盖实际发送的字节。"""
    import hashlib
    import hmac

    seen: list[tuple[bytes, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.content, request.headers.get("X-Coding2API-Signature", "")))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, _alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        webhook_url=lambda: "https://hook.example/a", client=client,
        app_secret=lambda: "top-secret", now=lambda: 5)
    report = await task.run_once()
    assert report["delivered"] == 1
    body, signature = seen[0]
    expected = "sha256=" + hmac.new(b"top-secret", body, hashlib.sha256).hexdigest()
    assert signature == expected
    await client.aclose()
    db.close()

def test_alert_severity_warning_constant():
    assert SEVERITY_WARNING == "warning"
    assert SEVERITY_CRITICAL == "critical"
    assert isinstance(time.time(), float)

class _KeyStats:
    """统计替身：错误率固定 0，按 Key 用量可注入并记录调用窗口。"""

    def __init__(self, key_usage=None):
        self.calls: list[int] = []
        self._key_usage = key_usage or []

    def window_error_rate(self, *, since):
        return (0, 0)

    def window_key_usage(self, *, since, limit=50):
        self.calls.append(since)
        return self._key_usage


async def test_alert_task_key_burst_rule_uses_its_own_window(tmp_path):
    stats = _KeyStats([{"api_key_id": "k1", "username": "alice",
                        "requests": 5, "tokens": 900_000}])
    task, alerts, db = _task(
        tmp_path, stats=stats, now=lambda: 10_000,
        key_burst_tokens=lambda: 500_000,
        key_burst_window_minutes=lambda: 10,
    )
    await task.run_once()
    row = alerts.recent()[0]
    assert row["rule"] == "key_burst"
    # 窗口用 key_burst_window_minutes（10 分钟），不是错误率窗口
    assert stats.calls == [10_000 - 600]


async def test_alert_task_key_burst_skipped_when_window_closed(tmp_path):
    stats = _KeyStats([{"api_key_id": "k1", "username": "a",
                        "requests": 5, "tokens": 900_000}])
    task, alerts, db = _task(
        tmp_path, stats=stats, now=lambda: 10_000,
        key_burst_tokens=lambda: 500_000,
        key_burst_window_minutes=lambda: 0,
    )
    await task.run_once()
    assert stats.calls == []
    assert alerts.recent() == []


async def test_alert_task_off_hours_rule_reads_local_clock(tmp_path):
    """非工作时段按本地时钟判定：构造一个明确在工作时间外的时刻。"""

    def noon_like_utc() -> int:
        # 取本地时区 03:00 的时刻（任何时区下 `hour == 3` 都成立）
        local = time.localtime()
        return int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                3, 0, 0, 0, 0, -1)))

    class _BusyStats:
        def window_error_rate(self, *, since):
            return (30, 0)

    task, alerts, db = _task(
        tmp_path, stats=_BusyStats(), now=noon_like_utc,
        off_hours_start=lambda: 9, off_hours_end=lambda: 19,
        off_hours_min_requests=lambda: 20,
    )
    await task.run_once()
    assert [row["rule"] for row in alerts.recent()] == ["off_hours"]


class _CostStats:
    """统计替身：成本按月起点聚合，记录调用窗口。"""

    def __init__(self, spent=0.0):
        self.calls: list[int] = []
        self._spent = spent

    def window_error_rate(self, *, since):
        return (0, 0)

    def window_cost(self, *, since):
        self.calls.append(since)
        return (self._spent, 1, 1)


async def test_alert_task_budget_uses_month_start(tmp_path):
    from src.tasks.alerting import month_start

    stats = _CostStats(85.0)
    task, alerts, db = _task(
        tmp_path, stats=stats, now=lambda: 1_790_000_000,
        budget_cny=lambda: 100.0, budget_levels=lambda: "50,80,95,100",
    )
    await task.run_once()
    assert stats.calls == [month_start(1_790_000_000)]
    row = alerts.recent()[0]
    assert row["rule"] == "budget"
    assert row["scope"] == "budget_80"


async def test_alert_task_budget_disabled_skips_query(tmp_path):
    stats = _CostStats(999.0)
    task, alerts, db = _task(tmp_path, stats=stats, now=lambda: 1_790_000_000,
                             budget_cny=lambda: 0.0)
    await task.run_once()
    assert stats.calls == []
    assert alerts.recent() == []


async def test_alert_task_budget_level_upgrade_resolves_old_level(tmp_path):
    """50% 档告警后升到 80%：旧档不再命中，一轮迟滞后写成已恢复。"""
    stats = _CostStats(60.0)
    task, alerts, db = _task(
        tmp_path, stats=stats, now=lambda: 1_790_000_000,
        budget_cny=lambda: 100.0, budget_levels=lambda: "50,80,100",
        clear_ticks=lambda: 1, silence_minutes=lambda: 0,
    )
    await task.run_once()
    assert [row["scope"] for row in alerts.recent()] == ["budget_50"]

    stats._spent = 85.0
    await task.run_once()
    scopes = [row["scope"] for row in alerts.recent()]
    assert "budget_80" in scopes
    # 50% 档已被写成恢复（resolved_at 非空），不再算未恢复
    unresolved = [row["scope"] for row in alerts.unresolved()]
    assert unresolved == ["budget_80"]

async def test_alert_task_delivers_feishu_and_slack_shapes(tmp_path):
    """飞书 / Slack 各按自己的消息格式发送，仍带签名头。"""
    seen: list[tuple[str, dict, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, json.loads(request.content),
                     request.headers.get("X-Coding2API-Signature")))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        feishu_url=lambda: "https://open.feishu.cn/hook/f",
        slack_url=lambda: "https://hooks.slack.example/s",
        client=client, now=lambda: 5, app_secret=lambda: "secret")
    report = await task.run_once()
    assert report["delivered"] == 1
    assert alerts.recent()[0]["delivered"] == 1

    by_host = {host: body for host, body, _ in seen}
    feishu = by_host["open.feishu.cn"]
    assert feishu["msg_type"] == "text"
    assert "Coding2API" in feishu["content"]["text"]
    slack = by_host["hooks.slack.example"]
    assert set(slack) == {"text"} and "Coding2API" in slack["text"]
    assert all(signature for _, _, signature in seen)
    await client.aclose()
    db.close()

async def test_alert_task_channel_failure_isolated(tmp_path):
    """一个渠道失败不影响其它渠道发送，但整体不算投递成功。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(500 if request.url.host.endswith("feishu.cn") else 200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task, alerts, db = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        feishu_url=lambda: "https://open.feishu.cn/hook/f",
        slack_url=lambda: "https://hooks.slack.example/s",
        client=client, now=lambda: 5)
    report = await task.run_once()
    assert sorted(calls) == ["hooks.slack.example", "open.feishu.cn"]
    assert report["delivered"] == 0
    assert alerts.recent()[0]["delivery_error"] is not None
    await client.aclose()
    db.close()
