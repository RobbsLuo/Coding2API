"""签到后台任务（跨渠道去重、失败隔离、作用域锁）。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.db.repo import CheckinEventRepository
from src.provider.codebuddy.checkin import CheckinResult
from src.tasks.checkin import CheckinTask, _cn_now
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

async def test_checkin_skips_enterprise_accounts(repo):
    """企业版账号没有个人版签到活动：跳过，不打上游、不触发失败告警。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy",
                    credential_data={"bearer_token": "e",
                                     "quota_probe_mode": "enterprise"})
    provider = ProbeProvider()
    task = CheckinTask(credentials, {"codebuddy": provider})
    report = await task.run_once()
    assert report.attempted == 0 and report.skipped == 1
    assert provider.checkin_calls == 0

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

async def test_checkin_skip_providers_stops_only_that_channel(repo):
    """渠道级止损：点名渠道不调上游，其它渠道照常签，且聊天能力不受影响。

    回归 2026-11-09：raccoon 上游自 11-05 停止写 daily_grant，三个号每轮
    都回「今日每日积分尚未发放」，7 天 484 次尝试仅 10 次成功。停用签到
    但又不能暂停凭证——`enabled=0` 会连聊天一起停掉。
    """
    credentials, _db = repo
    credentials.add(provider="raccoon", credential_data={"bearer_token": "r",
                                                        "uid": "u1"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "c",
                                                          "account_uid": "u2"})

    class TwoChannelProvider:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def checkin(self, data):
            self.calls.append(data["bearer_token"])
            return CheckinResult(ok=True, credit=100)

        def checkin_scope(self, data):
            return data.get("uid") or data.get("account_uid") or ""

    raccoon = TwoChannelProvider()
    raccoon.id = "raccoon"
    codebuddy = TwoChannelProvider()
    task = CheckinTask(credentials, {"raccoon": raccoon, "codebuddy": codebuddy},
                       skip_providers=" raccoon , ")
    report = await task.run_once()

    assert raccoon.calls == []                     # 点名渠道一次都没打
    assert codebuddy.calls == ["c"]                # 其它渠道照常
    assert report.succeeded == 1 and report.skipped >= 1

async def test_checkin_skip_providers_is_hot_reloadable(repo):
    """取值器每轮现读：热更关掉跳过 → 下一轮就能重新签。"""
    credentials, _db = repo
    credentials.add(provider="raccoon", credential_data={"bearer_token": "r",
                                                        "uid": "u1"})
    provider = ProbeProvider()
    provider.id = "raccoon"
    enabled = {"skip": True}
    task = CheckinTask(credentials, {"raccoon": provider},
                       skip_providers=lambda: "raccoon" if enabled["skip"] else "")
    assert (await task.run_once()).attempted == 0
    enabled["skip"] = False
    assert (await task.run_once()).attempted == 1

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
    # 第二轮：ok 已封账（skipped），bad 在 10 分钟退避到期后重试成功
    retry_at = time.struct_time((2026, 9, 11, 10, 10, 0, 3, 254, 0))
    second = await task.run_once(now=retry_at)
    assert second.skipped == 1 and second.succeeded == 1 and second.failed == 0
    # 第三轮：全部封账，不再调上游
    third = await task.run_once(now=retry_at)
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


async def test_checkin_day_boundary_is_beijing_regardless_of_local_tz(repo, monkeypatch):
    """日界固定北京时间：进程 TZ 改成 UTC 也不得偏移。

    上游「每日一次」按北京时间切。此前用 `time.localtime()`，TZ≠CN 时
    日界会偏几小时（UTC 下 16:00 之后的签到被算成次日 → 每天漏签一次）。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "cb"})
    provider = ProbeProvider()
    task = CheckinTask(credentials, {"codebuddy": provider})
    # 北京时间 2026-09-11 00:30 = UTC 2026-09-10 16:30
    early = int(datetime(2026, 9, 11, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        assert task._day_key(_cn_now(early)) == "2026-09-11"
        assert task._day_key(_cn_now(early + 3600)) == "2026-09-11"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_checkin_day_end_handles_month_end():
    """月末不能构造出「32 号」：`datetime` 不做溢出归一（mktime 才会）。"""
    task = CheckinTask(None, {})                       # 只算日期，不用仓储
    last_day = time.struct_time((2026, 9, 30, 23, 0, 0, 2, 273, 0))
    end = task._day_end(last_day)
    assert datetime.fromtimestamp(end, ZoneInfo("Asia/Shanghai")).day == 1
    # 2026-10-01 00:00 北京时间
    assert end == int(datetime(2026, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


async def test_checkin_records_audit_events(repo):
    """每轮签到落一行审计日志：成功与失败都记，trigger=auto。

    「签到显示成功却没加积分」类问题要靠逐次记录定位；只记成功会让失败静默。
    """
    credentials, db = repo
    credentials.add(provider="codebuddy",
                    credential_data={"bearer_token": "a", "account_uid": "a"})
    credentials.add(provider="codebuddy",
                    credential_data={"bearer_token": "b", "account_uid": "b"})

    class Provider:
        id = "codebuddy"

        async def checkin(self, data):
            if data["bearer_token"] == "b":
                return CheckinResult(ok=False, code=7, message="上游忙")
            return CheckinResult(ok=True, credit=1000.0, code=0, message="签到成功")

        def checkin_scope(self, data):
            return f"cb|{data['account_uid']}"

    events = CheckinEventRepository(db)
    task = CheckinTask(credentials, {"codebuddy": Provider()}, events=events)
    report = await task.run_once()
    assert report.succeeded == 1 and report.failed == 1

    rows = [events.recent(candidate.credential_id)[0]
            for candidate in credentials.candidates()]
    assert {(row["ok"], row["trigger"]) for row in rows} == {(1, "auto"), (0, "auto")}
    # 成功行的领取积分如实落库；失败行 code/文案保留
    success = next(row for row in rows if row["ok"] == 1)
    failure = next(row for row in rows if row["ok"] == 0)
    assert success["credit"] == 1000.0 and success["code"] == 0
    assert failure["code"] == 7 and failure["message"] == "上游忙"


async def test_checkin_failure_backoff_skips_then_retries(repo):
    """连续失败按 10m → 20m 退避：到期前跳过且不调上游，到期后重试。

    回归 2026-11-05：raccoon 两个号从 00:29 到 17:54 每 10 分钟锤一次
    「今日积分尚未发放」，一天 145 条 WARNING。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "x"})
    provider = ProbeProvider(checkin_ok=False)
    task = CheckinTask(credentials, {"codebuddy": provider})
    t0 = time.struct_time((2026, 9, 11, 10, 0, 0, 3, 254, 0))
    t9 = time.struct_time((2026, 9, 11, 10, 9, 0, 3, 254, 0))
    t10 = time.struct_time((2026, 9, 11, 10, 10, 0, 3, 254, 0))
    t29 = time.struct_time((2026, 9, 11, 10, 29, 0, 3, 254, 0))
    t30 = time.struct_time((2026, 9, 11, 10, 30, 0, 3, 254, 0))

    assert (await task.run_once(now=t0)).failed == 1
    assert provider.checkin_calls == 1

    skipped = await task.run_once(now=t9)               # 10 分钟退避未到期
    assert skipped.attempted == 0 and skipped.skipped == 1
    assert provider.checkin_calls == 1

    assert (await task.run_once(now=t10)).failed == 1   # 到期 → 重试，退避翻倍
    assert provider.checkin_calls == 2

    skipped = await task.run_once(now=t29)              # 20 分钟退避未到期
    assert skipped.attempted == 0 and provider.checkin_calls == 2
    assert (await task.run_once(now=t30)).failed == 1
    assert provider.checkin_calls == 3


async def test_checkin_manual_trigger_bypasses_failure_backoff(repo):
    """管理台「立即运行」不受失败退避限制（管理员要能当场重试）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "x"})
    provider = ProbeProvider(checkin_ok=False)
    task = CheckinTask(credentials, {"codebuddy": provider})
    t0 = time.struct_time((2026, 9, 11, 10, 0, 0, 3, 254, 0))
    t1 = time.struct_time((2026, 9, 11, 10, 1, 0, 3, 254, 0))

    assert (await task.run_once(now=t0)).failed == 1
    manual = await task.run_once(now=t1, trigger="manual")
    assert manual.attempted == 1 and manual.failed == 1
    assert provider.checkin_calls == 2


async def test_checkin_success_clears_failure_backoff(repo):
    """成功清零连续失败计数：再次失败从 10 分钟重新起算，不继承旧档位。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "x"})

    class Sequence(ProbeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.results: list[CheckinResult] = []
            self.seal_until: int | None = None

        async def checkin(self, _data):
            self.checkin_calls += 1
            if self.results:
                return self.results.pop(0)
            return CheckinResult(ok=False, message="上游忙")

    provider = Sequence()
    task = CheckinTask(credentials, {"codebuddy": provider})
    t0 = time.struct_time((2026, 9, 11, 10, 0, 0, 3, 254, 0))
    t10 = time.struct_time((2026, 9, 11, 10, 10, 0, 3, 254, 0))
    t11 = time.struct_time((2026, 9, 11, 10, 11, 0, 3, 254, 0))
    t20 = time.struct_time((2026, 9, 11, 10, 20, 0, 3, 254, 0))
    t21 = time.struct_time((2026, 9, 11, 10, 21, 0, 3, 254, 0))

    assert (await task.run_once(now=t0)).failed == 1
    # 10:10 成功，但封账只到同一时刻 → 下一轮立即可再试（模拟短周期渠道）
    provider.results.append(CheckinResult(ok=True, credit=10,
                                          seal_until=int(datetime(
                                              2026, 9, 11, 10, 10,
                                              tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())))
    assert (await task.run_once(now=t10)).succeeded == 1
    # 10:11 再失败：退避应回到基础 10 分钟（10:21），而不是旧的 20 分钟
    assert (await task.run_once(now=t11)).failed == 1
    skipped = await task.run_once(now=t20)
    assert skipped.attempted == 0 and skipped.skipped == 1
    assert (await task.run_once(now=t21)).failed == 1
    assert provider.checkin_calls == 4


def test_checkin_backoff_delay_doubles_and_caps_at_one_hour():
    """退避档位 10m → 20m → 40m → 封顶 1h（不再增长，避免整天不重试）。"""
    from src.tasks.checkin import _cn_epoch

    task = CheckinTask(None, {})                       # 只测状态机，不用仓储
    now = time.struct_time((2026, 9, 11, 10, 0, 0, 3, 254, 0))
    delays = []
    for _ in range(6):
        task._note_failure("2026-09-11:x", now)
        delays.append(task._failures["2026-09-11:x"][1] - _cn_epoch(now))
        assert task._backed_off("2026-09-11:x", now, manual=False) is True
        assert task._backed_off("2026-09-11:x", now, manual=True) is False
    assert delays == [600, 1200, 2400, 3600, 3600, 3600]


def test_checkin_failure_retry_at_delays_past_local_backoff():
    """渠道点名的重试时刻（小浣熊 01:30）只能延后、不能提前本地退避。"""
    from src.tasks.checkin import _cn_epoch

    task = CheckinTask(None, {})
    now = time.struct_time((2026, 9, 11, 0, 2, 0, 4, 254, 0))
    grace = int(datetime(2026, 9, 11, 1, 30,
                         tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    task._note_failure("2026-09-11:x", now, retry_at=grace)
    assert task._failures["2026-09-11:x"][1] == grace
    before = time.struct_time((2026, 9, 11, 1, 20, 0, 4, 254, 0))
    after = time.struct_time((2026, 9, 11, 1, 30, 0, 4, 254, 0))
    assert task._backed_off("2026-09-11:x", before, manual=False) is True
    assert task._backed_off("2026-09-11:x", after, manual=False) is False

    # 渠道给的时刻比本地退避更早 → 仍按 10 分钟退避，不被缩短
    task._note_failure("2026-09-11:y", now, retry_at=_cn_epoch(now))
    assert task._failures["2026-09-11:y"][1] == _cn_epoch(now) + 600


async def test_checkin_failure_state_pruned_on_new_day(repo):
    """跨天清退避：昨天的失败不能把今天的首次签到挡掉。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "x"})
    provider = ProbeProvider(checkin_ok=False)
    task = CheckinTask(credentials, {"codebuddy": provider})
    today = time.struct_time((2026, 9, 11, 23, 55, 0, 4, 255, 0))
    tomorrow = time.struct_time((2026, 9, 12, 0, 1, 0, 5, 256, 0))

    assert (await task.run_once(now=today)).failed == 1
    assert any(key.startswith("2026-09-11:") for key in task._failures)
    report = await task.run_once(now=tomorrow)
    assert report.attempted == 1 and report.failed == 1
    assert not any(key.startswith("2026-09-11:") for key in task._failures)


async def test_checkin_exception_log_includes_type_when_message_empty(repo, caplog):
    """异常消息为空时日志仍要带类名（原先只打空串，排障看不到原因）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "x"})

    class Boom(ProbeProvider):
        async def checkin(self, _data):
            raise TimeoutError()

    task = CheckinTask(credentials, {"codebuddy": Boom()})
    with caplog.at_level("WARNING", logger="src.tasks.checkin"):
        report = await task.run_once()
    assert report.failed == 1
    assert any("TimeoutError" in record.getMessage() for record in caplog.records)


class AutoLoginProvider:
    """带 `relogin` 的渠道替身：签到结果与登录结果都可控。"""

    id = "raccoon"

    def __init__(self, *, checkin_results=None, relogin_result=None,
                 relogin_error=None):
        self.checkin_results = list(checkin_results or [])
        self.relogin_result = relogin_result
        self.relogin_error = relogin_error
        self.checkin_calls = 0
        self.relogin_calls = 0

    async def checkin(self, data):
        self.checkin_calls += 1
        if self.checkin_results:
            return self.checkin_results.pop(0)
        return CheckinResult(ok=True, credit=300.0, message="每日积分 +300")

    async def relogin(self, data):
        self.relogin_calls += 1
        if self.relogin_error is not None:
            raise self.relogin_error
        return self.relogin_result

    def checkin_scope(self, data):
        return f"raccoon|{data.get('uid', '')}"


def _failed_grant():
    return CheckinResult(ok=False, message="上游今日每日积分尚未发放")


def _raccoon_credential(credentials, *, with_login=True):
    data = {"access_token": "tok", "uid": "u1"}
    if with_login:
        data.update({"phone": "13900000001", "password": "Abcd123456"})
    return credentials.add(provider="raccoon", credential_data=data)


async def test_checkin_autologin_off_by_default(repo):
    """默认关：失败就失败，绝不拿用户密码去登录。"""
    credentials, _db = repo
    _raccoon_credential(credentials)
    provider = AutoLoginProvider(checkin_results=[_failed_grant()])
    report = await CheckinTask(credentials, {"raccoon": provider}).run_once()
    assert report.failed == 1
    assert provider.relogin_calls == 0


async def test_checkin_autologin_needs_phone_and_password(repo):
    """开了开关但凭证没带登录凭据 → 不登录（纯 token 用法不受影响）。"""
    credentials, _db = repo
    _raccoon_credential(credentials, with_login=False)
    provider = AutoLoginProvider(checkin_results=[_failed_grant()])
    task = CheckinTask(credentials, {"raccoon": provider}, auto_login=True)
    assert (await task.run_once()).failed == 1
    assert provider.relogin_calls == 0


async def test_checkin_autologin_refreshes_and_rechecks(repo):
    """登录成功 → 新会话落库 + 立刻重核一次，当天这笔就算签到了。"""
    credentials, db = repo
    cid = _raccoon_credential(credentials)
    provider = AutoLoginProvider(
        checkin_results=[_failed_grant()],
        relogin_result={"access_token": "fresh", "uid": "u1",
                        "phone": "13900000001", "password": "Abcd123456"})
    report = await CheckinTask(credentials, {"raccoon": provider},
                               auto_login=True).run_once()

    assert report.succeeded == 1 and report.failed == 0
    assert provider.relogin_calls == 1 and provider.checkin_calls == 2
    # 新 token 已落库（下轮签到直接用新会话）
    assert credentials.credential_data(cid)["access_token"] == "fresh"
    assert credentials.credential_data(cid)["password"] == "Abcd123456"


async def test_checkin_autologin_only_once_per_day(repo):
    """同一凭证一天最多登一次：密码错了也不许拿同一份错密码反复撞。"""
    credentials, _db = repo
    _raccoon_credential(credentials)
    provider = AutoLoginProvider(checkin_results=[_failed_grant(),
                                                  _failed_grant()])
    task = CheckinTask(credentials, {"raccoon": provider}, auto_login=True)

    assert (await task.run_once()).failed == 1
    assert (await task.run_once(trigger="manual")).failed == 1
    assert provider.relogin_calls == 1


async def test_checkin_autologin_rejection_disables_credential(repo):
    """上游明确拒绝密码 → 标「需重新登录」并停手，而不是每小时重试。"""
    from src.provider.base import UpstreamReloginRequired

    credentials, _db = repo
    cid = _raccoon_credential(credentials)
    provider = AutoLoginProvider(
        checkin_results=[_failed_grant()],
        relogin_error=UpstreamReloginRequired("手机号或密码错误"))
    events = CheckinEventRepository(_db)
    report = await CheckinTask(credentials, {"raccoon": provider},
                               auto_login=True, events=events).run_once()

    assert report.failed == 1
    row = credentials.list_all()[0]
    assert row["disabled"] == 1 and "重新登录" in row["disabled_reason"]
    # 失败原因落在签到审计里，别看起来像「上游没发积分」
    assert "重新登录" in events.recent(cid)[0]["message"]


async def test_checkin_autologin_failure_keeps_result_and_credential(repo):
    """登录遇网络/5xx → 保留原结论、凭据不动，下轮按退避再试。"""
    credentials, db = repo
    cid = _raccoon_credential(credentials)
    provider = AutoLoginProvider(checkin_results=[_failed_grant()],
                                 relogin_error=RuntimeError("connect timeout"))
    report = await CheckinTask(credentials, {"raccoon": provider},
                               auto_login=True).run_once()

    assert report.failed == 1
    assert credentials.credential_data(cid)["access_token"] == "tok"
    assert credentials.list_all()[0]["disabled"] == 0


async def test_checkin_autologin_none_result_keeps_failure(repo):
    """`relogin` 返回 None（渠道没登录轨道/凭据不完整）→ 按原失败处理。"""
    credentials, _db = repo
    _raccoon_credential(credentials)
    provider = AutoLoginProvider(checkin_results=[_failed_grant()],
                                 relogin_result=None)
    report = await CheckinTask(credentials, {"raccoon": provider},
                               auto_login=True).run_once()
    assert report.failed == 1
    assert provider.checkin_calls == 1


async def test_checkin_autologin_skips_provider_without_relogin(repo):
    """渠道没有 `relogin`（其它 8 家）→ 开关打开也不动。"""
    credentials, _db = repo
    credentials.add(provider="raccoon", credential_data={
        "access_token": "tok", "uid": "u1",
        "phone": "13900000001", "password": "Abcd123456"})
    provider = ProbeProvider(checkin_ok=False)
    provider.id = "raccoon"
    report = await CheckinTask(credentials, {"raccoon": provider},
                               auto_login=True).run_once()
    assert report.failed == 1


async def test_checkin_autologin_is_hot_reloadable(repo):
    """取值器每轮现读：热更打开后下一轮就能登录。"""
    credentials, _db = repo
    _raccoon_credential(credentials)
    flag = {"on": False}
    provider = AutoLoginProvider(
        checkin_results=[_failed_grant(), _failed_grant()],
        relogin_result={"access_token": "fresh", "uid": "u1",
                        "phone": "13900000001", "password": "Abcd123456"})
    task = CheckinTask(credentials, {"raccoon": provider},
                       auto_login=lambda: flag["on"])
    assert (await task.run_once()).failed == 1
    assert provider.relogin_calls == 0
    flag["on"] = True
    # manual 绕过失败退避（否则 10 分钟退避内本轮直接跳过）
    assert (await task.run_once(trigger="manual")).succeeded == 1
    assert provider.relogin_calls == 1


async def test_checkin_autologin_records_are_pruned_across_days(repo):
    """跨天清掉「今天已登录」记录，否则第二天永远不再尝试登录。"""
    from src.tasks.checkin import _cn_epoch

    credentials, _db = repo
    _raccoon_credential(credentials)
    provider = AutoLoginProvider(
        checkin_results=[_failed_grant()],
        relogin_result={"access_token": "fresh", "uid": "u1",
                        "phone": "13900000001", "password": "Abcd123456"})
    task = CheckinTask(credentials, {"raccoon": provider}, auto_login=True)

    today = datetime(2026, 11, 18, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    tomorrow = today + timedelta(days=1)
    task._login_days["stale"] = "2026-11-17"      # 前一天残留
    task._prune(today.timetuple())
    assert "stale" not in task._login_days

    # 同一天只登一次；跨天后又能登（第二天的凭证同样失败、同样带登录凭据）
    assert (await task.run_once(now=today.timetuple())).succeeded == 1
    assert provider.relogin_calls == 1
    # 第二天同样「上游没发」→ 记录已被清掉，可以再登一次
    provider.checkin_results.append(_failed_grant())
    assert (await task.run_once(now=tomorrow.timetuple())).succeeded == 1
    assert provider.relogin_calls == 2
    assert _cn_epoch(tomorrow.timetuple()) > _cn_epoch(today.timetuple())


async def test_checkin_autologin_waits_for_pacer(repo):
    """与签到同一 Pacer：登录也是打上游的写请求，不许绕开频率风控。"""
    credentials, _db = repo
    _raccoon_credential(credentials)
    provider = AutoLoginProvider(
        checkin_results=[_failed_grant()],
        relogin_result={"access_token": "fresh", "uid": "u1",
                        "phone": "13900000001", "password": "Abcd123456"})

    waits = {"n": 0}

    class PacerStub:
        async def wait_turn(self) -> None:
            waits["n"] += 1

    task = CheckinTask(credentials, {"raccoon": provider}, auto_login=True,
                       pacer=PacerStub())
    assert (await task.run_once()).succeeded == 1
    # 签到一次 + 按需登录一次 = 两次取轮次
    assert waits["n"] == 2


async def test_checkin_autologin_reason_is_fixed_text_not_upstream(repo, caplog):
    """落库原因用固定文案：上游原文只进 logger（该值经 /api/credentials 回显）。"""
    from src.provider.base import UpstreamReloginRequired

    credentials, _db = repo
    _raccoon_credential(credentials)
    provider = AutoLoginProvider(
        checkin_results=[_failed_grant()],
        relogin_error=UpstreamReloginRequired("upstream echo ZZZ-1: account rejected"))
    with caplog.at_level("WARNING"):
        await CheckinTask(credentials, {"raccoon": provider},
                          auto_login=True).run_once()

    row = credentials.list_all()[0]
    assert row["disabled_reason"] == "自动登录被上游拒绝，需重新登录"
    assert "ZZZ-1" not in row["disabled_reason"]
    assert any("ZZZ-1" in record.getMessage() for record in caplog.records)


async def test_checkin_autologin_pacer_failure_does_not_mark_attempted(repo):
    """`wait_turn()` 抛 BaseException 时不能先记「今天登录过」——否则当天静默漏签。"""
    credentials, _db = repo
    _raccoon_credential(credentials)
    provider = AutoLoginProvider(checkin_results=[_failed_grant()])

    class CancellingPacer:
        async def wait_turn(self) -> None:
            raise KeyboardInterrupt                 # BaseException（取消/超时同族）

    task = CheckinTask(credentials, {"raccoon": provider}, auto_login=True,
                       pacer=CancellingPacer())
    with pytest.raises(KeyboardInterrupt):
        await task.run_once()
    assert task._login_days == {}
    assert provider.relogin_calls == 0


def test_checkin_seal_until_is_capped_at_48h():
    """上游给超长 `seal_until` 时钳到 now+48h：否则该凭证长期不再签到。"""
    from src.tasks.checkin import _cn_epoch

    task = CheckinTask.__new__(CheckinTask)
    now = time.struct_time((2026, 10, 8, 10, 0, 0, 0, 1, -1))

    class Result:
        seal_until = 4102444800                     # 2100 年：明显超长

    capped = task._seal_deadline(Result(), now)
    assert capped == _cn_epoch(now) + 48 * 3600


def test_checkin_seal_until_keeps_cross_day_window():
    """正常跨天窗口（Qoder 10:00 → 次日 09:59）必须原样保留，不被钳到当日 24:00。"""
    from src.tasks.checkin import _cn_epoch

    task = CheckinTask.__new__(CheckinTask)
    now = time.struct_time((2026, 10, 8, 0, 5, 0, 0, 1, -1))
    window_end = _cn_epoch(time.struct_time((2026, 10, 9, 9, 59, 0, 0, 4, -1)))

    class Result:
        seal_until = window_end

    assert task._seal_deadline(Result(), now) == window_end
    # 无 seal_until 时仍封到本地当日 24:00
    class NoSeal:
        seal_until = None

    assert task._seal_deadline(NoSeal(), now) == task._day_end(now)


async def test_checkin_skips_when_provider_not_registered(repo):
    """凭证的渠道没有 provider（渠道下线 / 配置漂移）：跳过而不是抛。

    这条分支同时覆盖 `provider is None` 的 `data`/`checkin_scope` 判断——两个
    都是 getattr/None 检查，少一个都会让整轮签到失败。
    """
    credentials, _db = repo
    credentials.add(provider="ghost_channel", credential_data={"bearer_token": "g"})
    provider = ProbeProvider()
    task = CheckinTask(credentials, {"codebuddy": provider})
    report = await task.run_once()
    assert report.attempted == 0 and report.skipped == 1
    assert provider.checkin_calls == 0
