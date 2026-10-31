"""成长中心后台任务（去重、失败隔离、pacer、due）。"""

from __future__ import annotations

from src.provider.base import GrowthResult
from src.tasks.growth import GrowthTask
from src.tasks.pacer import Pacer
from tests.provider.codebuddy._growth_support import StubGrowthProvider as _StubGrowthProvider


async def test_growth_task_records_event_and_last_result(growth_repo):
    """一轮成功：落 events 表 + 回写 credentials.growth_last_result。"""
    credentials, events = growth_repo
    credential_id = credentials.add(provider="codebuddy",
                                    credential_data={"bearer_token": "t"})
    provider = _StubGrowthProvider()
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.attempted == 1 and report.succeeded == 1 and report.failed == 0
    stored = events.latest_for(credential_id)
    assert stored["ok"] == 1 and stored["report"] == "领旅行礼物：+10 积分"
    assert stored["credit"] == 10.0 and stored["trigger"] == "auto"
    listed = credentials.list_all()
    assert listed[0]["growth_last_result"] == "领旅行礼物：+10 积分"
    assert listed[0]["growth_last_run_at"] is not None

async def test_growth_task_skips_providers_without_growth_and_disabled(growth_repo):
    """TRAE（无成长中心）与硬禁用凭证都跳过，且不发任何请求。"""
    credentials, events = growth_repo
    credentials.add(provider="trae", credential_data={"accessToken": "t"})
    disabled_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    from src.engine.scheduler import Candidate, Scheduler

    credentials.save_error(disabled_id, Scheduler().note_error(
        Candidate(credential_id=disabled_id, provider="codebuddy"),
        __import__("src.provider.base", fromlist=["ErrKind"]).ErrKind.DEAD, 0))
    provider = _StubGrowthProvider()
    report = await GrowthTask(credentials, {"codebuddy": provider, "trae": object()},
                              events).run_once()
    assert report.attempted == 0 and report.skipped == 2
    assert provider.calls == 0

async def test_growth_task_skips_soft_disabled_credentials(growth_repo):
    """`enabled=0`（用户软暂停）不参与成长中心：暂停不能只停调度。

    与 checkin / refresh / quota_probe 同口径。此前只判 `disabled`，于是
    「暂停」的号仍被后台任务拿去领积分。
    """
    credentials, events = growth_repo
    paused_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "p"})
    assert credentials.set_enabled(paused_id, False)
    provider = _StubGrowthProvider()
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.attempted == 0 and report.skipped == 1
    assert provider.calls == 0


async def test_growth_task_paused_does_not_claim_shared_account_scope(growth_repo):
    """暂停凭证必须早于 `seen.add` 跳过，否则会白占同账号的名额。"""
    credentials, events = growth_repo
    paused_id = credentials.add(provider="codebuddy",
                                credential_data={"bearer_token": "a", "account_uid": "same"})
    credentials.set_enabled(paused_id, False)
    credentials.add(provider="codebuddy",
                    credential_data={"bearer_token": "b", "account_uid": "same"})
    provider = _StubGrowthProvider()
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.attempted == 1 and report.succeeded == 1
    assert provider.calls == 1


async def test_growth_task_dedupes_same_upstream_account(growth_repo):
    """同上游账号多凭证共享一轮：避免重复领取与多余请求。"""
    credentials, events = growth_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "same"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b",
                                                          "account_uid": "same"})
    provider = _StubGrowthProvider()
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.attempted == 1 and report.skipped == 1
    assert provider.calls == 1

async def test_growth_task_empty_scope_does_not_share(growth_repo):
    """身份未知（空 scope）时绝不共享：宁可多领一次也不能漏掉一个账号。"""
    credentials, events = growth_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    provider = _StubGrowthProvider(scope="")
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.attempted == 2 and provider.calls == 2

async def test_growth_task_failure_is_isolated_and_recorded(growth_repo):
    """单凭证异常不拖累其余；失败也要落库（否则线上坏了没人知道）。"""
    credentials, events = growth_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "1"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b",
                                                          "account_uid": "2"})
    provider = _StubGrowthProvider(error=RuntimeError("boom"))
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.attempted == 2 and report.failed == 2
    assert events.latest_for(credentials.candidates()[0].credential_id) is None

async def test_growth_task_marks_session_dead_result_as_failed(growth_repo):
    """session_dead 结果记为失败（调度器据此硬禁用凭证）。"""
    credentials, events = growth_repo
    credential_id = credentials.add(provider="codebuddy",
                                    credential_data={"bearer_token": "t"})
    dead = GrowthResult(ok=False, session_dead=True, report="登录态已失效，请重新登录")
    provider = _StubGrowthProvider(result=dead)
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.failed == 1 and report.succeeded == 0
    stored = events.latest_for(credential_id)
    assert stored["session_dead"] == 1 and stored["ok"] == 0

async def test_growth_task_uses_pacer_and_due_is_true(growth_repo):
    """节流器每轮每凭证各取一次 turn；due() 恒真（节奏由 runner 间隔控制）。"""
    credentials, events = growth_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t",
                                                          "account_uid": "1"})
    turns: list[int] = []

    class CountingPacer(Pacer):
        async def wait_turn(self) -> None:
            turns.append(1)

    task = GrowthTask(credentials, {"codebuddy": _StubGrowthProvider()}, events,
                      pacer=CountingPacer(1, 1))
    await task.run_once(trigger="manual")
    assert len(turns) == 1
    assert task.due() is True
    assert events.latest_for(credentials.candidates()[0].credential_id)["trigger"] == "manual"

async def test_growth_task_skips_vanished_credential_data(growth_repo):
    """凭证在读取时消失 → 计入 skipped（不能抛异常中断整轮）。"""
    credentials, events = growth_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    original = credentials.credential_data
    credentials.credential_data = lambda _cid: None       # type: ignore[method-assign]
    report = await GrowthTask(credentials, {"codebuddy": _StubGrowthProvider()},
                              events).run_once()
    credentials.credential_data = original                # type: ignore[method-assign]
    assert report.skipped == 1 and report.attempted == 0

async def test_growth_task_pacer_disabled_path(growth_repo):
    """未装配 pacer 时不报错（老部署/测试路径）。"""
    credentials, events = growth_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    report = await GrowthTask(credentials, {"codebuddy": _StubGrowthProvider()},
                              events, pacer=None).run_once()
    assert report.succeeded == 1
