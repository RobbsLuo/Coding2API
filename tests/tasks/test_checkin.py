"""签到后台任务（跨渠道去重、失败隔离、作用域锁）。"""

from __future__ import annotations

import time

from src.provider.codebuddy.checkin import CheckinResult
from src.tasks.checkin import CheckinTask
from tests.helpers.scheduler import disabled_outcome as _disabled_outcome
from tests.tasks._support import ProbeProvider


async def test_checkin_empty_scope_falls_back_to_credential_id(repo):
    """身份未知（provider 返回空 scope）时绝不共享：两个账号必须各签一次。

    回归用例：CB 的 OAuth 凭证实测 account_uid / user_id 都是空串，
    共享 `endpoint|` 会让第二个凭证被 seen 集合永久跳过，且没有任何报错。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})

    class AnonymousProvider:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def checkin(self, _data):
            self.calls += 1
            return CheckinResult(ok=True, credit=100)

        def checkin_scope(self, _data):
            return ""        # 身份未知

    provider = AnonymousProvider()
    task = CheckinTask(credentials, {"codebuddy": provider})
    report = await task.run_once()
    assert report.attempted == 2 and report.succeeded == 2 and report.skipped == 0
    assert provider.calls == 2

async def test_checkin_dedupes_same_upstream_account(repo):
    """同上游账号的多张凭证只签一次（AGENTS.md 约束）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "same"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b",
                                                          "account_uid": "same"})
    provider = ProbeProvider()
    task = CheckinTask(credentials, {"codebuddy": provider})
    report = await task.run_once()
    assert report.attempted == 1 and report.skipped == 1
    assert provider.checkin_calls == 1

async def test_checkin_same_scope_failure_still_dedupes(repo):
    """同账号多凭证：首条失败时本轮不重试第二条（seen 去重）。

    行序不变则下一轮仍是首条先被试，第二条永远轮不到 —— 这是有意的上限
    （避免同账号重复打上游），不是「下轮必重试」。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "same"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b",
                                                          "account_uid": "same"})
    provider = ProbeProvider(checkin_ok=False)
    task = CheckinTask(credentials, {"codebuddy": provider})
    report = await task.run_once()
    assert report.attempted == 1 and report.skipped == 1 and report.failed == 1
    assert provider.checkin_calls == 1

async def test_checkin_skips_paused_credential_and_frees_scope(repo):
    """回归 #15：暂停（enabled=0）的旧凭证不得占用上游账号名额。

    行序更靠前且会话已死的暂停凭证若仍被尝试，会先 `seen.add(scope)`，后面
    那条启用中的同账号凭证每轮都被去重挡下 → 渠道签到永久静默失效。
    """
    credentials, _db = repo
    paused = credentials.add(provider="codebuddy", credential_data={"bearer_token": "dead",
                                                                    "account_uid": "same"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "live",
                                                          "account_uid": "same"})
    credentials.set_enabled(paused, False)

    class HalfDead(ProbeProvider):
        """暂停那条会话已死：若它仍被尝试，本轮就会 failed=1 且占用账号名额。"""

        async def checkin(self, data):
            self.checkin_calls += 1
            return CheckinResult(ok=data["bearer_token"] != "dead", credit=10)

    provider = HalfDead()
    task = CheckinTask(credentials, {"codebuddy": provider})
    report = await task.run_once()
    assert report.attempted == 1 and report.succeeded == 1
    assert report.failed == 0 and report.skipped == 1
    assert provider.checkin_calls == 1

async def test_checkin_skips_disabled(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    credentials.save_error(credential_id, _disabled_outcome())
    task = CheckinTask(credentials, {"codebuddy": ProbeProvider()})
    report = await task.run_once()
    assert report.attempted == 0
    assert report.skipped == 1

async def test_checkin_supports_both_providers(repo):
    """TRAE 与 CodeBuddy 都实现签到后，任务要能覆盖两个上游。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "cb"})
    credentials.add(provider="trae", credential_data={"accessToken": "t", "uid": "tr"})

    class TraeCheckinProvider:
        id = "trae"

        def __init__(self) -> None:
            self.calls = 0

        async def checkin(self, _data):
            self.calls += 1
            from src.provider.base import CheckinResult

            return CheckinResult(ok=True, credit=None, message="今天已签到",
                                 already_checked_in=True)

        def checkin_scope(self, data):
            return f"trae|{data.get('uid', '')}"

    trae = TraeCheckinProvider()
    task = CheckinTask(credentials, {"codebuddy": ProbeProvider(), "trae": trae})
    report = await task.run_once()
    assert report.attempted == 2
    assert report.succeeded == 2
    assert trae.calls == 1

async def test_checkin_reports_failure_and_success_callback(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "x"})
    seen: list[str] = []
    task = CheckinTask(credentials, {"codebuddy": ProbeProvider(checkin_ok=False)},
                       on_success=seen.append)
    report = await task.run_once()
    assert report.failed == 1 and seen == []

async def test_checkin_error_is_isolated(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a"})

    class Boom(ProbeProvider):
        async def checkin(self, _data):
            raise RuntimeError("boom")

    task = CheckinTask(credentials, {"codebuddy": Boom()})
    report = await task.run_once()
    assert report.failed == 1

async def test_checkin_due_is_always_true():
    """未配置时点（留空）时保持旧行为：全天每 10 分钟一轮，due 恒 True。"""
    task = CheckinTask(None, {})  # type: ignore[arg-type]
    midnight = time.struct_time((2026, 9, 11, 0, 30, 0, 3, 254, 0))
    late = time.struct_time((2026, 9, 11, 23, 59, 0, 3, 254, 0))
    assert task.due(now=midnight) is True
    assert task.due(now=late) is True

async def test_checkin_success_locks_scope_for_the_day(repo):
    """成功签到即封账该 scope：当日不再调上游；失败 scope 下轮（10 分钟一轮）继续重试。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "ok"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b",
                                                          "account_uid": "bad"})

    class Flaky:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {"ok": 0, "bad": 0}
            self.bad_fail_first = True

        def checkin_scope(self, data):
            return f"cb|{data['account_uid']}"

        async def checkin(self, data):
            uid = data["account_uid"]
            self.calls[uid] += 1
            if uid == "ok":
                return CheckinResult(ok=True, credit=100)
            # bad 首次失败（上游瞬时故障），之后恢复——验证失败凭证下轮重试
            if self.bad_fail_first:
                self.bad_fail_first = False
                return CheckinResult(ok=False, message="参与人数过多")
            return CheckinResult(ok=True, credit=50)

    provider = Flaky()
    task = CheckinTask(credentials, {"codebuddy": provider})
    now = time.struct_time((2026, 9, 11, 10, 0, 0, 3, 254, 0))
    first = await task.run_once(now=now)
    assert first.succeeded == 1 and first.failed == 1
    # 第二轮：ok 已封账（skipped），bad 重试成功
    second = await task.run_once(now=now)
    assert second.skipped == 1 and second.succeeded == 1 and second.failed == 0
    # 第三轮：全部封账，不再调上游
    third = await task.run_once(now=now)
    assert third.attempted == 0 and third.skipped == 2
    assert provider.calls == {"ok": 1, "bad": 2}
    # 次日清账：所有 scope 重新可签
    tomorrow = time.struct_time((2026, 9, 12, 9, 0, 0, 4, 255, 0))
    next_day = await task.run_once(now=tomorrow)
    assert next_day.attempted == 2 and next_day.succeeded == 2
    assert provider.calls == {"ok": 2, "bad": 3}

async def test_checkin_on_success_callback_fires(repo):
    """签到成功 → 触发额度重探测回调（background 164）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t",
                                                          "account_uid": "a"})
    seen: list[str] = []
    task = CheckinTask(credentials, {"codebuddy": ProbeProvider()}, on_success=seen.append)
    report = await task.run_once()
    assert report.succeeded == 1 and len(seen) == 1
