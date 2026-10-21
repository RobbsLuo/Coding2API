"""凭证刷新后台任务：到期筛选、relogin 标记与失败隔离。"""

from __future__ import annotations

import asyncio
import time

from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.scheduler import ErrorOutcome, ModelCooldown
from src.provider.base import UpstreamReloginRequired
from src.provider.codebuddy.client import CodeBuddyCredential
from src.tasks.refresh import RefreshTask
from tests.helpers.chat import jwt_token as _jwt
from tests.helpers.constants import SECRET
from tests.helpers.scheduler import disabled_outcome as _disabled_outcome
from tests.tasks._support import ProbeProvider


async def test_refresh_task_only_touches_due_credentials(repo):
    credentials, _db = repo
    now = 1_000_000
    soon = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": now + 100})
    later = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": now + 999_999})
    provider = ProbeProvider(refresh_new="NEW")
    task = RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600,
                       now=lambda: now)
    report = await task.run_once()
    assert report.succeeded == 1 and provider.refresh_calls == 1
    assert credentials.credential_data(soon)["bearer_token"] == "NEW"
    assert credentials.credential_data(later)["bearer_token"] == "old"

async def test_refresh_task_skips_paused_credential(repo):
    """回归 #15：用户软暂停（enabled=0）的凭证不该被预刷新。

    线上症状是任务报告 `failed=1`，而失败者正是早已停用的旧凭证 —— 预刷新
    白占调度窗口，真实失败被掩盖。
    """
    credentials, _db = repo
    paused = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1_000_100})
    credentials.set_enabled(paused, False)
    provider = ProbeProvider(refresh_new="NEW")
    task = RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600,
                       now=lambda: 1_000_000)
    report = await task.run_once()
    assert report.attempted == 0 and report.skipped == 1 and report.failed == 0
    assert provider.refresh_calls == 0
    assert credentials.credential_data(paused)["bearer_token"] == "old"

async def test_refresh_task_marks_relogin_required_on_terminal_failure(repo):
    """一次性续期凭据被作废 → 标记「需重新登录」并停止重试。

    旧行为是每轮任务刷一条一模一样的 warning：refresh_token 已废，重试永远
    成功不了。标记后本轮记 failed，下轮 `candidates()` 把它计入 skipped，
    管理台按 disabled_reason 提示需要人工重新登录。
    """
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1_000_100})

    class DeadProvider(ProbeProvider):
        async def refresh(self, _data):
            self.refresh_calls += 1
            # 上游原文可能内嵌 token/URL；落库必须换成固定文案（见 RELOGIN_REASON）
            raise UpstreamReloginRequired(
                "upstream rejected: refresh_token=SECRET-LEAK-42")

    provider = DeadProvider()
    task = RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600,
                       now=lambda: 1_000_000)
    report = await task.run_once()
    assert report.failed == 1 and report.succeeded == 0
    row = credentials.list_all()[0]
    assert row["disabled"] == 1
    # 固定文案：上游原文（可能含 token）绝不写进 disabled_reason
    assert row["disabled_reason"] == "刷新令牌已失效，需重新登录"
    assert "SECRET-LEAK-42" not in row["disabled_reason"]
    # 凭证字段未被失败路径改写（旧票仍原样留着，供管理员复核）
    assert credentials.credential_data(credential_id)["bearer_token"] == "old"
    # 第二轮：已硬禁用 → 直接跳过，不再发刷新请求
    again = await task.run_once()
    assert again.skipped == 1 and provider.refresh_calls == 1

async def test_refresh_task_relogin_clears_model_cooldowns(repo):
    """标记「需重新登录」同时清掉模型级冷却，避免残留负缓存误导排查。"""
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1_000_100})
    credentials.save_error(credential_id, ErrorOutcome(
        model_cooldowns={"m": ModelCooldown(cooling_until=9_999_999_999, hits=1,
                                           reason="model")}))
    assert credentials.model_cooldowns_for(credential_id) == {"m": 9_999_999_999}

    class DeadProvider(ProbeProvider):
        async def refresh(self, _data):
            raise UpstreamReloginRequired("刷新令牌已失效，需重新登录")

    report = await RefreshTask(credentials, {"codebuddy": DeadProvider()},
                               skew_seconds=3600, now=lambda: 1_000_000).run_once()
    assert report.failed == 1
    assert credentials.candidates()[0].disabled is True
    assert credentials.model_cooldowns_for(credential_id) == {}

async def test_refresh_task_skips_manual_and_missing(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t",
                                                          "auth_source": "manual"})
    credentials.add(provider="ghost", credential_data={"x": 1})
    task = RefreshTask(credentials, {"codebuddy": ProbeProvider()}, skew_seconds=3600,
                       now=lambda: 1)
    report = await task.run_once()
    assert report.attempted == 0 and report.skipped == 2

async def test_refresh_task_isolates_failures(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "t", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})

    class Boom(ProbeProvider):
        async def refresh(self, _data):
            raise RuntimeError("boom")

    task = RefreshTask(credentials, {"codebuddy": Boom()}, skew_seconds=3600,
                       now=lambda: 1)
    report = await task.run_once()
    assert report.failed == 1

async def test_refresh_task_skips_credential_deleted_before_lock(repo):
    """等编辑锁期间凭证被删 → 持锁后重读为 None，记 skipped 而不是崩溃。"""
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "t", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})

    class Vanishing(ProbeProvider):
        async def refresh(self, _data):
            raise AssertionError("凭证已删，不应发起刷新")

    # 先占住编辑锁，让 run_once 在锁上等待；等它挂起后删掉凭证再放锁
    lock = credentials.edit_lock(credential_id)
    await lock.acquire()

    async def release_after_delete() -> None:
        await asyncio.sleep(0.01)
        credentials.delete(credential_id)
        lock.release()

    task = RefreshTask(credentials, {"codebuddy": Vanishing()}, skew_seconds=3600,
                       now=lambda: 1)
    _, report = await asyncio.gather(release_after_delete(), task.run_once())
    assert report.skipped == 1 and report.attempted == 1

def test_needs_refresh_without_builder_returns_false():
    from src.tasks.refresh import _needs_refresh

    assert _needs_refresh(object(), {}, 10, 0) is False

async def test_refresh_task_skips_disabled(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "t", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})
    credentials.save_error(credential_id, _disabled_outcome())
    task = RefreshTask(credentials, {"codebuddy": ProbeProvider()}, skew_seconds=3600,
                       now=lambda: 1)
    report = await task.run_once()
    assert report.skipped == 1

def test_refresh_task_refreshes_codebuddy_without_explicit_expiry(tmp_path):
    """回归：CB 凭证只有 JWT exp 时也要被预刷新（修复前 needs_refresh 恒 False）。

    这是顺带修掉的真实故障：token 到期后上游 401 → DEAD 硬禁用，
    而 revive 不会补刷新，凭证无法自愈。
    """
    import asyncio

    from src.tasks.refresh import RefreshTask

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    now = int(time.time())
    expiring = credentials.add(provider="codebuddy", now=now, credential_data={
        "bearer_token": _jwt(now + 3600), "refresh_token": "r", "auth_source": "oauth"})
    fresh = credentials.add(provider="codebuddy", now=now, credential_data={
        "bearer_token": _jwt(now + 40 * 86400), "refresh_token": "r",
        "auth_source": "oauth"})
    unknown = credentials.add(provider="codebuddy", now=now, credential_data={
        "bearer_token": "plain", "refresh_token": "r", "auth_source": "oauth"})

    refreshed_ids: list[str] = []

    class Provider:
        id = "codebuddy"

        def credential_from(self, data):
            return CodeBuddyCredential.from_dict(data)

        async def refresh(self, data):
            refreshed_ids.append(data["bearer_token"])
            return {"bearer_token": _jwt(now + 90 * 86400, iat=now), "refresh_token": "r",
                    "auth_source": "oauth"}

    task = RefreshTask(credentials, {"codebuddy": Provider()}, skew_seconds=24 * 3600,
                       now=lambda: now)
    report = asyncio.run(task.run_once())
    assert report.attempted == 1 and report.succeeded == 1
    assert len(refreshed_ids) == 1
    # 只有进入窗口的那条被刷新，到期时间与签发时间都推进
    row = db.connect().execute(
        "SELECT token_expires_at, token_issued_at FROM credentials WHERE id = ?",
        (expiring,)).fetchone()
    assert tuple(row) == (now + 90 * 86400, now)
    untouched = db.connect().execute(
        "SELECT token_expires_at FROM credentials WHERE id = ?", (fresh,)).fetchone()[0]
    assert untouched == now + 40 * 86400
    # 到期时间未知的凭证不刷新（未知不等于紧急，避免每轮空打上游）
    assert db.connect().execute(
        "SELECT token_expires_at FROM credentials WHERE id = ?",
        (unknown,)).fetchone()[0] == 0
    db.close()
