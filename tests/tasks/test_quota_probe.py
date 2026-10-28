"""额度探测任务。"""

from __future__ import annotations

from src.tasks.pacer import Pacer
from src.tasks.quota_probe import QuotaProbeTask
from tests.tasks._support import ProbeProvider


async def test_quota_probe_updates_health(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()})
    report = await task.run_once()
    assert report.succeeded == 1
    assert credentials.candidates()[0].health == 80

async def test_quota_probe_failure_marks_unknown_not_zero(repo):
    """探测失败 → health=NULL（unknown），绝不当作 0 分（三态）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider(error=RuntimeError("boom"))})
    report = await task.run_once()
    assert report.failed == 1
    assert credentials.candidates()[0].health is None

async def test_quota_probe_skips_unknown_provider_and_missing_data(repo):
    credentials, _db = repo
    credentials.add(provider="ghost", credential_data={"x": 1})
    task = QuotaProbeTask(credentials, {})
    report = await task.run_once()
    assert report.skipped == 1

async def test_quota_probe_uses_pacer(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    turns: list[int] = []

    class CountingPacer(Pacer):
        async def wait_turn(self) -> None:
            turns.append(1)

    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()},
                          CountingPacer(1, 1))
    await task.run_once()
    assert len(turns) == 1
    turns.clear()
    await task.run_once(apply_pacing=False)
    assert turns == []

async def test_quota_probe_skips_when_credential_vanished(repo):
    """凭证在探测前被删除 → 计入 skipped（background 93-96）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()})
    original = credentials.credential_data
    credentials.credential_data = lambda _cid: None       # type: ignore[method-assign]
    report = await task.run_once()
    credentials.credential_data = original                # type: ignore[method-assign]
    assert report.skipped == 1


async def test_quota_probe_scopes_credential_for_proxy_stickiness(repo):
    """探活必须在「该凭证」的上下文里发（出口 IP 与推理一致）。"""
    from src.request_context import get_credential_id

    credentials, _db = repo
    credential_id = credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    seen: list[str | None] = []

    class _Scoped:
        async def probe_quota(self, data):
            seen.append(get_credential_id())
            from src.provider.base import Quota
            return Quota(remaining=1, total=2, probed_at=1)

    task = QuotaProbeTask(credentials, {"codebuddy": _Scoped()})
    await task.run_once()
    assert seen == [credential_id]
    # 退出循环后归还上下文，不把凭证 id 泄漏给后续任务
    assert get_credential_id() is None
