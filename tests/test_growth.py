"""成长中心测试（M1.6）：协议层解析 / 编排策略 / 后台任务 / 管理台端点。

夹具形状全部取自真实上游响应（2026-09 活动版本，实测抓取），
字段名不凭直觉编造——上游改版时这些夹具会先红。
"""

from __future__ import annotations

import httpx
import pytest

from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import SCHEMA_VERSION, apply_schema
from src.db.repo import CredentialRepository, GrowthRepository
from src.main import build_app
from src.provider.base import GrowthResult, GrowthStep, StepStatus
from src.provider.codebuddy.client import CodeBuddyCredential, CodeBuddyProvider
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.growth import (
    EP_BUDDY_OPEN,
    EP_BUDDY_QUOTA,
    EP_ENERGY,
    EP_LOTTERY_CHANCES,
    EP_LOTTERY_DRAW,
    EP_MAKEUP_USE,
    EP_REDEEM,
    EP_REDEEM_SUMMARY,
    EP_STREAK,
    EP_TASK_ACCEPT,
    EP_TASKS,
    EP_TRAVEL_CLAIM,
    EP_TRAVEL_CONFIG,
    EP_TRAVEL_DEPART,
    EP_TRAVEL_STATUS,
    CodeBuddyGrowth,
    GrowthRejected,
    as_int,
    client_token,
    dig,
    is_unknown_tier,
    parse_redeem_summary,
    parse_reward,
    parse_streak,
    parse_tasks,
    parse_travel_locations,
    parse_travel_status,
)
from src.provider.codebuddy.growth_runner import (
    MAKEUP_MAX_PER_RUN,
    GrowthRunner,
    _eta,
    _fmt,
    _report,
)
from src.tasks.growth import GrowthTask
from src.tasks.pacer import Pacer
from src.tasks.runner import TaskRunner, build_runner

SECRET = "test-secret-0123456789"

TRAVEL_ARRIVED = {"code": 0, "msg": "OK", "data": {
    "state": "arrived", "buddy_id": 7485932, "record_id": 277990,
    "location": {"id": 1, "code": "coffee", "name": "咖啡馆", "duration_hours": 3},
    "depart_at": 1785255159, "arrive_at": 1785265959, "server_now": 1789820066,
    "daily_limit_reached": False, "duration_hours": 3, "reward_credit": 10}}

TRAVEL_TRAVELING = {"code": 0, "data": {
    "state": "traveling", "location": {"id": 2, "name": "书店"},
    "arrive_at": 1000, "server_now": 100, "daily_limit_reached": False}}

TRAVEL_IDLE = {"code": 0, "data": {
    "state": "idle", "location": {"id": 1, "name": "咖啡馆"},
    "daily_limit_reached": False}}

TASKS_BODY = {"code": 0, "data": {"tasks": [
    # 未领取：必须先 accept 一次（进度从领取才计）
    {"task_code": "t_new", "title": "新任务", "accept_status": "",
     "progress": {"current": 0, "target": 1}, "reward_credit": 300, "locked": False},
    # 已接单未完成：跳过，不重复领取
    {"task_code": "t_doing", "title": "进行中", "accept_status": "accepted",
     "progress": {"current": 0, "target": 3}},
    # 已接单且完成：领奖
    {"task_code": "t_done", "title": "已完成", "accept_status": "accepted",
     "progress": {"current": 1, "target": 1}, "reward_credit": 50, "reward_energy": 5},
    # 已领奖 / 锁定：跳过
    {"task_code": "t_claimed", "title": "已领", "accept_status": "claimed"},
    {"task_code": "t_locked", "title": "锁定", "accept_status": "", "locked": True},
    # progress 缺失：target 按 1，current 0 → 未完成
    {"task_code": "t_bare", "title": "无进度", "accept_status": ""},
]}}

STREAK_BODY = {"code": 0, "data": {
    "streak": {"days": 0, "month_total_days": 3, "makeup_dates": []},
    "makeup_cards": {"balance": 0, "max": 4},
    "redemption_status": {"tier_7d_status": "locked", "tier_14d_status": "locked",
                          "tier_28d_status": "locked"}}}

REDEEM_LOCKED = {"code": 0, "data": {
    "starter_status": "locked", "advanced_status": "locked", "legendary_status": "locked",
    "starter_count": 0, "total_consumed": 0}}


# --------------------------------------------------------------- 解析层

def test_dig_and_as_int_tolerate_junk():
    """信封查找与宽松数值转换：坏数据不能抛异常（上游会返回 Infinity）。"""
    assert dig({"data": {"a": 1}}, "a") == 1
    assert dig({"result": {"resp": {"a": 2}}}, "a") == 2
    assert dig({"a": None}, "a") is None
    assert dig([1, 2], "a") is None
    assert as_int("1.5") == 1
    assert as_int(float("inf")) == 0
    assert as_int(True) == 1
    assert as_int(None, default=7) == 7


def test_client_token_is_prefixed_uuid():
    """抽奖/兑换必须带 u-<uuid>：缺了上游直接 400（实测）。"""
    token = client_token()
    assert token.startswith("u-") and len(token) > 20
    assert token != client_token()


def test_parse_travel_status_variants():
    arrived = parse_travel_status(TRAVEL_ARRIVED["data"])
    assert arrived.state == "arrived" and arrived.record_id == 277990
    assert arrived.reward_credit == 10.0 and arrived.location_name == "咖啡馆"
    # state 缺失/非法 → idle（不能因为字段异常就以为要派 Buddy）
    assert parse_travel_status({}).state == "idle"
    assert parse_travel_status({"state": 5}).state == "idle"
    assert parse_travel_status({"state": "idle", "daily_limit_reached": True}) \
        .daily_limit_reached is True
    # location 非 dict 时不能炸
    assert parse_travel_status({"state": "traveling", "location": "x"}).location_name == ""


def test_parse_travel_locations_filters_non_dicts():
    locations = parse_travel_locations({"locations": [
        {"id": 1, "name": "咖啡馆", "duration_hours_min": 1}, "junk"]})
    assert len(locations) == 1 and locations[0].name == "咖啡馆"
    assert parse_travel_locations({"locations": "x"}) == []


def test_parse_tasks_three_state_semantics():
    """accept_status 三态与进度语义：决定「接单」还是「领奖」。"""
    tasks = parse_tasks(TASKS_BODY["data"])
    assert len(tasks) == 6
    by_code = {task.task_code: task for task in tasks}
    assert by_code["t_new"].completed is False and by_code["t_new"].accept_status == ""
    assert by_code["t_doing"].accept_status == "accepted"
    assert by_code["t_done"].completed is True
    assert by_code["t_locked"].locked is True
    # target 缺失按 1：0 >= 0 会被误判成已完成
    assert by_code["t_bare"].progress_target == 1
    assert by_code["t_bare"].completed is False
    assert parse_tasks({"tasks": "junk"}) == []


def test_parse_streak_card_shapes_and_dates():
    """makeup_cards 兼容对象与裸数字；makeup_dates 兼容 streak 内层与顶层。"""
    inner = parse_streak(STREAK_BODY["data"])
    assert inner.makeup_cards == 0 and inner.days == 0 and inner.makeup_dates == []
    bare = parse_streak({"makeup_cards": 2, "streak": {"days": 5,
                                                       "makeup_dates": ["2026-09-01", 7]}})
    assert bare.makeup_cards == 2 and bare.days == 5
    assert bare.makeup_dates == ["2026-09-01"]        # 非字符串日期被过滤
    top = parse_streak({"makeup_dates": ["2026-09-02"]})
    assert top.makeup_dates == ["2026-09-02"]
    assert parse_streak({"streak": {"days": "5"}}).days is None


def test_parse_redeem_summary_skips_missing_tiers():
    assert parse_redeem_summary(REDEEM_LOCKED["data"]) == {
        "starter": "locked", "advanced": "locked", "legendary": "locked"}
    # 字段缺失不猜"可兑换"：接口改版时不该对三档无脑 POST
    assert parse_redeem_summary({"starter_status": "unlocked"}) == {"starter": "unlocked"}


def test_parse_reward_reads_actual_values():
    credit, energy = parse_reward({"data": {"credit": 10, "energy": 5}})
    assert credit == 10.0 and energy == 5.0
    assert parse_reward({}) == (None, None)
    assert parse_reward({"credit": True}) == (None, None)


def test_growth_rejected_needs_attention_boundary():
    """4xx 是业务规则（不算故障），5xx 与"没拿到响应"才需要人看。"""
    assert GrowthRejected(400, "x").needs_attention is False
    assert GrowthRejected(429, "x").needs_attention is False
    assert GrowthRejected(500, "x").needs_attention is True
    assert GrowthRejected(-1, "network").needs_attention is True
    assert "400" in str(GrowthRejected(400))


def test_is_unknown_tier_only_for_param_rejections():
    """只有「参数里 tier 不认识」的 400 才允许换写法重试。"""
    assert is_unknown_tier(GrowthRejected(400, "unknown tier")) is True
    assert is_unknown_tier(GrowthRejected(400, "invalid tier value")) is True
    assert is_unknown_tier(GrowthRejected(400, "unsupported tier")) is True
    assert is_unknown_tier(GrowthRejected(400, "invalid request")) is False
    assert is_unknown_tier(GrowthRejected(500, "unknown tier")) is False


def test_eta_and_fmt_are_display_only():
    """展示函数遇到坏数据必须返回空串/原样，绝不能抛异常。"""
    assert _eta(1000, 100) == "，约 15 分钟后回"
    assert _eta(10000, 100) == "，约 2.8 小时后回"
    assert _eta(100, 1000) == "，已到达待领取"
    assert _eta(None, 1) == "" and _eta("x", 1) == "" and _eta(float("inf"), 0) == ""
    assert _fmt(10.0) == "10" and _fmt(1.5) == "1.5" and _fmt("x") == "x"


def test_report_summarizes_steps_and_tail():
    result = GrowthResult(steps=[
        GrowthStep("领旅行礼物", StepStatus.DONE, "咖啡馆 带回 10 积分"),
        GrowthStep("Buddy 旅行中", StepStatus.IDLE, "书店，约 1 小时后回"),
        GrowthStep("开盲盒", StepStatus.FAILED, "HTTP 500"),
    ], credit=10.0, energy=18, streak_days=4)
    report = _report(result)
    assert "领旅行礼物" in report and "开盲盒" in report
    assert "Buddy 旅行中" not in report          # idle 不进汇报（无事发生）
    assert "能量 18" in report and "连签 4 天" in report and "+共 10 积分" in report
    assert _report(GrowthResult()) == "成长中心无可领取项"


# --------------------------------------------------------------- 协议层

def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)


async def test_growth_client_reads_every_endpoint():
    """七个只读端点的路径与解析（路径写错会在这里红）。"""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return {
            EP_TRAVEL_STATUS: httpx.Response(200, json=TRAVEL_ARRIVED),
            EP_TRAVEL_CONFIG: httpx.Response(200, json={"code": 0, "data": {"locations": [
                {"id": 1, "name": "咖啡馆"}]}}),
            EP_TASKS: httpx.Response(200, json=TASKS_BODY),
            EP_STREAK: httpx.Response(200, json=STREAK_BODY),
            EP_REDEEM_SUMMARY: httpx.Response(200, json=REDEEM_LOCKED),
            EP_LOTTERY_CHANCES: httpx.Response(200, json={"code": 0, "data": {"balance": 0}}),
            EP_BUDDY_QUOTA: httpx.Response(200, json={"code": 0, "data": {
                "affordable": 1, "balance": 18, "cost_per_open": 10, "max_open_count": 5}}),
            EP_ENERGY: httpx.Response(200, json={"code": 0, "data": {"balance": 18}}),
        }[request.url.path]

    client = CodeBuddyGrowth("https://e", client=_client(handler))
    credential = CodeBuddyCredential(bearer_token="t")
    assert (await client.travel_status(credential)).state == "arrived"
    assert (await client.travel_locations(credential))[0].name == "咖啡馆"
    assert len(await client.tasks(credential)) == 6
    assert (await client.streak(credential)).days == 0
    assert (await client.redeem_summary(credential))["starter"] == "locked"
    assert await client.lottery_chances(credential) == 0
    assert await client.buddy_quota(credential) == (1, 10, 5)
    assert await client.energy(credential) == 18
    assert seen == [EP_TRAVEL_STATUS, EP_TRAVEL_CONFIG, EP_TASKS, EP_STREAK,
                    EP_REDEEM_SUMMARY, EP_LOTTERY_CHANCES, EP_BUDDY_QUOTA, EP_ENERGY]
    await client.aclose()


async def test_growth_client_lazy_pool_and_close():
    client = CodeBuddyGrowth("https://e")
    assert client._http is client._http
    await client.aclose()


async def test_growth_client_writes_send_expected_payloads():
    """写操作的 payload 形状（client_token 缺失上游直接 400）。"""
    payloads: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        payloads.append((request.url.path, _json.loads(request.content or b"")))
        return httpx.Response(200, json={"code": 0, "data": {
            "credit": 10, "energy": 5, "makeup_cards": {"balance": 1},
            "prize_name": "冰箱贴", "buddy": "小猫", "location": {"name": "书店"},
            "duration_hours": 2}})

    client = CodeBuddyGrowth("https://e", client=_client(handler))
    credential = CodeBuddyCredential(bearer_token="t")
    assert await client.claim_travel(credential, 277990) == (10.0, 5.0)
    await client.depart(credential, 1)
    assert await client.accept_task(credential, "t1") == (10.0, 5.0)
    assert await client.use_makeup_card(credential, "2026-09-01") == 1
    assert await client.redeem(credential, 7) == (10.0, 5.0)
    await client.draw_lottery(credential)
    await client.open_buddy(credential, 2)
    paths = [path for path, _payload in payloads]
    assert paths == [EP_TRAVEL_CLAIM, EP_TRAVEL_DEPART, EP_TASK_ACCEPT, EP_MAKEUP_USE,
                     EP_REDEEM, EP_LOTTERY_DRAW, EP_BUDDY_OPEN]
    by_path = dict(payloads)
    assert by_path[EP_TRAVEL_CLAIM] == {"record_id": 277990}
    assert by_path[EP_TRAVEL_DEPART] == {"location_id": 1}
    assert by_path[EP_TASK_ACCEPT] == {"task_code": "t1"}
    assert by_path[EP_MAKEUP_USE]["target_date"] == "2026-09-01"
    assert by_path[EP_MAKEUP_USE]["client_token"].startswith("u-")
    assert by_path[EP_REDEEM]["tier"] == 7          # 天数，不是档位名
    assert by_path[EP_BUDDY_OPEN]["count"] == 2
    await client.aclose()


async def test_growth_client_use_makeup_card_tolerates_shapes():
    """补登回包形状未知（该写路径实测未验证过）：对象/裸值/缺失都不能炸。"""
    # 缺失字段 → None（「上游没说」与「卡用完了」是两回事）
    for body, expected in (({"makeup_cards": {"balance": 3}}, 3),
                           ({"makeup_cards": 2}, 2),
                           ({"makeup_cards": 0}, 0),
                           ({}, None)):
        async def handler(_request: httpx.Request, _body=body) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "data": _body})

        client = CodeBuddyGrowth("https://e", client=_client(handler))
        got = await client.use_makeup_card(CodeBuddyCredential(bearer_token="t"), "d")
        assert got == expected
        await client.aclose()


async def test_growth_client_maps_failures_to_rejections():
    """401/403 与 5xx 都抛 GrowthRejected 并带上状态码与上游消息。"""
    async def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "msg": "登录过期"})

    client = CodeBuddyGrowth("https://e", client=_client(unauthorized))
    with pytest.raises(GrowthRejected) as caught:
        await client.travel_status(CodeBuddyCredential(bearer_token="t"))
    assert caught.value.status == 401 and caught.value.message == "登录过期"
    await client.aclose()

    async def server_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"boom")

    client2 = CodeBuddyGrowth("https://e", client=_client(server_error))
    with pytest.raises(GrowthRejected) as caught2:
        await client2.tasks(CodeBuddyCredential(bearer_token="t"))
    assert caught2.value.status == 503 and caught2.value.message == ""
    assert caught2.value.needs_attention is True
    await client2.aclose()


async def test_growth_client_rejects_bad_envelopes_and_non_json():
    """HTTP 200 但业务码非 0 / 缺 data / 非 JSON：必须显式报错，不能当空结果。"""
    cases = [
        httpx.Response(200, json={"code": 5, "msg": "boom"}),
        httpx.Response(200, json={"code": 0}),
        httpx.Response(200, json=[1]),
        httpx.Response(200, content=b"<html>"),
    ]
    for response in cases:
        async def handler(_request: httpx.Request, _response=response) -> httpx.Response:
            return _response

        client = CodeBuddyGrowth("https://e", client=_client(handler))
        with pytest.raises(UpstreamProtocolViolation):
            await client.energy(CodeBuddyCredential(bearer_token="t"))
        await client.aclose()


# --------------------------------------------------------------- 编排层

def _scripted(handler) -> CodeBuddyGrowth:
    return CodeBuddyGrowth("https://e", client=_client(handler))


def _runner(handler, *, allow_irreversible: bool = True) -> GrowthRunner:
    return GrowthRunner(_scripted(handler), allow_irreversible=allow_irreversible)


async def _run(handler, *, allow_irreversible: bool = True):
    runner = _runner(handler, allow_irreversible=allow_irreversible)
    return await runner.run(CodeBuddyCredential(bearer_token="t"))


async def test_runner_full_happy_path_collects_every_reward():
    """一轮完整跑通：礼物 / 派 Buddy / 接单 / 领奖 / 连登兑换 / 抽奖 / Buddy 盲盒。"""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_ARRIVED)
        if path == EP_TRAVEL_CLAIM:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 8}})
        if path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {
                "locations": [{"id": 1, "name": "咖啡馆"}]}})
        if path == EP_TRAVEL_DEPART:
            # 真实响应把时长放在 location 内层
            return httpx.Response(200, json={"code": 0, "data": {
                "location": {"name": "咖啡馆", "duration_hours": 3}}})
        if path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t_new", "title": "新任务", "accept_status": "",
                 "progress": {"current": 0, "target": 1}},
                {"task_code": "t_done", "title": "已完成", "accept_status": "accepted",
                 "progress": {"current": 1, "target": 1}, "reward_credit": 50}]}})
        if path == EP_TASK_ACCEPT:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 50}})
        if path == EP_STREAK:
            return httpx.Response(200, json={"code": 0, "data": {
                "streak": {"days": 7, "makeup_dates": []},
                "makeup_cards": {"balance": 2}}})
        if path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "unlocked", "advanced_status": "locked"}})
        if path == EP_REDEEM:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 0, "energy": 2}})
        if path == EP_MAKEUP_USE:
            return httpx.Response(200, json={"code": 0, "data": {"makeup_cards": 1}})
        if path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 2}})
        if path == EP_LOTTERY_DRAW:
            return httpx.Response(200, json={"code": 0, "data": {"prize_name": "冰箱贴",
                                                                 "need_address": True}})
        if path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {
                "affordable": 1, "cost_per_open": 10, "max_open_count": 5}})
        if path == EP_BUDDY_OPEN:
            return httpx.Response(200, json={"code": 0, "data": {"buddy": "小猫"}})
        if path == EP_ENERGY:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 18}})
        return httpx.Response(404, json={"code": 404})

    result = await _run(handler)
    assert result.ok is True and result.session_dead is False
    details = {step.name: step for step in result.steps}

    def done(name: str) -> GrowthStep:
        return next(step for step in result.steps
                    if step.name == name and step.status == StepStatus.DONE)

    assert details["领旅行礼物"].detail == "咖啡馆 带回 8 积分"
    assert details["派 Buddy"].detail == "去咖啡馆（3 小时后回）"
    assert details["领取任务"].detail == "「新任务」（进度开始计）"
    assert details["领任务奖"].credit == 50.0
    assert details["连登兑换"].detail == "「入门」+0 积分 +2 能量"
    assert done("开盲盒").detail == "冰箱贴（实物奖，需到成长中心填写收件信息）"
    assert done("Buddy 盲盒").detail == "×1（小猫）"
    # 实际发放值优先：claim 回包 8 覆盖了 status 里的 10
    assert result.credit == 58.0 and result.energy == 18 and result.streak_days == 7
    assert result.gained is True and result.failed == []
    # 抽奖还剩 1 次 → 记一句下轮继续，但不影响成功
    assert any(step.detail.startswith("还剩 1 次") for step in result.steps)
    assert not any(step.name == "补登" for step in result.steps)   # 无卡可补


async def test_runner_daily_limit_and_traveling_are_idle_not_failure():
    """名额用完 / Buddy 在路上是日常状态：不算失败，也不该继续派出发。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json={"code": 0, "data": {
                "state": "idle", "daily_limit_reached": True}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.ok is True and result.failed == []
    assert result.steps[0].status == StepStatus.IDLE
    assert result.steps[0].detail == "今日旅行名额已用完"
    assert result.gained is False
    assert "成长中心无可领取项" in result.report

    async def traveling(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_TRAVELING)
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(traveling)
    assert result2.ok is True
    assert result2.steps[0].name == "Buddy 旅行中"
    assert "约 15 分钟后回" in result2.steps[0].detail


async def test_runner_depart_failure_and_empty_locations():
    """派 Buddy 失败：4xx 业务规则不算硬失败；没有目的地则跳过。"""
    async def rejected(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {
                "locations": [{"id": 1, "name": "咖啡馆"}]}})
        if request.url.path == EP_TRAVEL_DEPART:
            return httpx.Response(400, json={"code": 400, "msg": "今日名额已用完"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(rejected)
    failed = [step for step in result.steps if step.name == "派 Buddy"]
    assert failed[0].status == StepStatus.IDLE         # 业务规则不算失败
    assert "今日名额已用完" in failed[0].detail
    assert result.ok is True

    async def no_location(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(no_location)
    assert result2.ok is True and result2.steps[0].detail == "无可选目的地"


async def test_runner_task_failure_is_recorded_per_task():
    """单条任务失败只记该条，不影响其余任务与整轮结论。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t1", "title": "坏任务", "accept_status": ""},
                {"task_code": "t2", "title": "好任务", "accept_status": ""}]}})
        if request.url.path == EP_TASK_ACCEPT:
            import json as _json

            code = _json.loads(request.content)["task_code"]
            if code == "t1":
                return httpx.Response(500, content=b"boom")
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert "坏任务" in result.report and "好任务" in result.report
    # 5xx 记失败，但同一轮「好任务」成功了 → 整体仍算成功（一条接口抖动不该判整轮死）
    assert len(result.failed) == 1
    assert result.ok is True and result.gained is True


async def test_runner_makeup_uses_one_card_and_reports_leftover():
    """补登：一轮只花一张卡；还有可补天数时留到下轮。"""
    used: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json={"code": 0, "data": {
                "streak": {"days": 3, "makeup_dates": ["2026-09-01", "2026-09-02"]},
                "makeup_cards": {"balance": 3}}})
        if request.url.path == EP_MAKEUP_USE:
            import json as _json

            used.append(_json.loads(request.content)["target_date"])
            return httpx.Response(200, json={"code": 0, "data": {"makeup_cards": 2}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert used == ["2026-09-01"]                     # 一轮只补一天
    assert MAKEUP_MAX_PER_RUN == 1
    details = [step.detail for step in result.steps if step.name == "补登"]
    assert "剩 2 张卡" in details[0]
    assert any("另有 1 天可补" in detail for detail in details)


async def test_runner_makeup_failure_stops_and_card_balance_zero_skips():
    """补登失败 → 记失败并停止补登；没有卡/没有可补日期 → 完全不请求。"""
    called: list[str] = []

    async def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json={"code": 0, "data": {
                "streak": {"days": 3, "makeup_dates": ["2026-09-01"]},
                "makeup_cards": {"balance": 1}}})
        if request.url.path == EP_MAKEUP_USE:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(failing)
    assert result.ok is False
    assert any(step.name == "补登" and step.status == StepStatus.FAILED for step in result.steps)
    assert "2026-09-01" not in [step.name for step in result.steps]   # 步骤名稳定，不带日期

    async def no_card(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        return httpx.Response(200, json={"code": 0, "data": {}})

    await _run(no_card)
    assert EP_MAKEUP_USE not in called


async def test_runner_redeem_retries_with_tier_name_on_unknown_tier():
    """/redeem 收天数；若上游改成只认档位名，退档位名重试（参数校验阶段，安全）。"""
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "unlocked"}})
        if request.url.path == EP_REDEEM:
            import json as _json

            tier = _json.loads(request.content)["tier"]
            tiers.append(tier)
            if isinstance(tier, int):
                return httpx.Response(400, json={"code": 400, "msg": "unknown tier"})
            return httpx.Response(200, json={"code": 0, "data": {"credit": 2}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert tiers == [7, "starter"]                   # 先天数，失败后退档位名
    assert any(step.name == "连登兑换" and step.status == StepStatus.DONE
               for step in result.steps)


async def test_runner_redeem_business_rejection_and_hard_failure():
    """未解锁的 400 不重试也不算硬失败；5xx 记失败。"""
    tiers: list[object] = []

    async def rejected(request: httpx.Request) -> httpx.Response:
        if request.url.path in (EP_TRAVEL_STATUS,):
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "unlocked"}})
        if request.url.path == EP_REDEEM:
            import json as _json

            tiers.append(_json.loads(request.content)["tier"])
            return httpx.Response(400, json={"code": 400, "msg": "invalid request"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(rejected)
    assert tiers == [7]                              # 业务拒绝不重试
    assert result.ok is True                         # 也不算硬失败

    async def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "unlocked"}})
        if request.url.path == EP_REDEEM:
            return httpx.Response(503, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(broken)
    assert result2.ok is False
    assert any("连登兑换" in step.name for step in result2.failed)


async def test_runner_lottery_no_chance_and_failure_paths():
    """抽奖次数为 0 → 完全不请求 draw；draw 失败分业务态与硬失败。"""
    called: list[str] = []

    async def no_chance(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 0}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(no_chance)
    assert EP_LOTTERY_DRAW not in called and EP_BUDDY_OPEN not in called
    assert result.ok is True

    async def draw_business_error(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            return httpx.Response(400, json={"code": 400,
                                             "msg": "insufficient lottery chance balance"})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(draw_business_error)
    assert result2.ok is True                        # 次数为 0 是常态，不是故障
    assert result2.gained is False

    async def draw_broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            return httpx.Response(500, content=b"boom")
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result3 = await _run(draw_broken)
    assert result3.ok is False


async def test_runner_lottery_prize_shapes_and_buddy_box_failure():
    """奖品名可能是对象/数字；Buddy 盲盒失败要记失败。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            # prize 是数字 + require_address：不能因类型报错而丢掉整次中奖
            return httpx.Response(200, json={"code": 0, "data": {
                "prize": 3, "require_address": True}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {
                "affordable": 9, "max_open_count": 3}})
        if request.url.path == EP_BUDDY_OPEN:
            return httpx.Response(400, json={"code": 400, "msg": "能量不足"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    draw = [step for step in result.steps if step.name == "开盲盒"
            and step.status == StepStatus.DONE]
    assert draw[0].detail == "3（实物奖，需到成长中心填写收件信息）"
    box = [step for step in result.steps if step.name == "Buddy 盲盒"]
    # 能量不足是业务规则（上限夹取后仍被上游拒）→ 记 IDLE 不算硬失败
    assert box[0].status == StepStatus.IDLE and result.ok is True
    assert "能量不足" in box[0].detail

    async def broken_box(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 0}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 1}})
        if request.url.path == EP_BUDDY_OPEN:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(broken_box)
    assert result2.ok is False
    assert any(step.name == "Buddy 盲盒" and step.status == StepStatus.FAILED
               for step in result2.steps)


# --------------------------------------------------------------- 编排层收尾

def test_report_omits_tail_when_only_steps():
    """没有能量/连签/积分时汇报只有步骤，不留空括号。"""
    report = _report(GrowthResult(steps=[GrowthStep("领旅行礼物", StepStatus.DONE, "咖啡馆")]))
    assert report == "领旅行礼物：咖啡馆"
    assert _report(GrowthResult(steps=[GrowthStep("步", StepStatus.DONE, "")])) == "步"


async def test_runner_stops_immediately_on_session_dead():
    """401/403 → 立即停止后续请求（再打只会一路 401）并标记 session_dead。"""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"code": 401, "msg": "登录过期"})

    result = await _run(handler)
    assert result.session_dead is True and result.ok is False
    assert calls == [EP_TRAVEL_STATUS]              # 一个 401 之后不再打任何请求
    assert "重新登录" in result.report

    # 任务阶段失败同理停止
    async def tasks_unauthorized(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        return httpx.Response(403, json={"code": 403, "msg": "过期"})

    calls.clear()
    result2 = await _run(tasks_unauthorized)
    assert result2.session_dead is True
    assert calls == [EP_TRAVEL_STATUS, EP_TRAVEL_CONFIG, EP_TASKS]


async def test_runner_irreversible_switch_skips_dangerous_actions():
    """关闭不可逆动作：仍领礼物，但抽奖/兑换/开盲盒/补登一律不发请求。"""
    called: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_ARRIVED)
        if request.url.path == EP_TRAVEL_CLAIM:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 10}})
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t", "title": "任务", "accept_status": "accepted",
                 "progress": {"current": 1, "target": 1}}]}})
        if request.url.path == EP_TASK_ACCEPT:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 5}})
        return httpx.Response(200, json={"code": 0, "data": {"balance": 5}})

    result = await _run(handler, allow_irreversible=False)
    assert EP_LOTTERY_CHANCES not in called and EP_LOTTERY_DRAW not in called
    assert EP_REDEEM_SUMMARY not in called and EP_REDEEM not in called
    assert EP_BUDDY_QUOTA not in called and EP_BUDDY_OPEN not in called
    assert EP_MAKEUP_USE not in called and EP_STREAK not in called
    # 可逆的仍要做：礼物 + 任务奖
    assert result.credit == 15.0
    skipped = {step.name for step in result.steps if step.status == StepStatus.SKIPPED}
    assert {"补登", "连登兑换", "开盲盒", "Buddy 盲盒"} <= skipped
    assert result.ok is True


async def test_runner_travel_and_redeem_exception_paths():
    """非 HTTP 异常（协议违规/网络）与「领取失败也算硬失败」的分支。"""
    # 领旅行礼物失败：即使 4xx 也标 attention（礼物没领到会过期）
    async def claim_rejected(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_ARRIVED)
        if request.url.path == EP_TRAVEL_CLAIM:
            return httpx.Response(400, json={"code": 400, "msg": "记录已失效"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(claim_rejected)
    assert result.ok is False
    failed = [step for step in result.steps if step.name == "领旅行礼物"]
    assert failed[0].status == StepStatus.FAILED and "记录已失效" in failed[0].detail

    # 协议违规（上游改版）→ 计为需要关注的失败
    async def protocol_violation(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json={"code": 0, "data": {"state": "idle"}})
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, content=b"<html>")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result2 = await _run(protocol_violation)
    assert result2.ok is False
    hit = next(step for step in result2.steps if step.name == "查旅行地点")
    assert "响应结构异常" in hit.detail

    # 连登兑换：首次 400 非 tier 问题 → 不重试、算业务态
    async def redeem_non_tier(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {"starter_status": "ready"}})
        if request.url.path == EP_REDEEM:
            return httpx.Response(400, json={"code": 400, "msg": "not enough days"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result3 = await _run(redeem_non_tier)
    assert result3.ok is True
    assert any("连登兑换" in step.name and "not enough days" in step.detail
               for step in result3.steps)

    # 连登兑换：tier 重试后仍失败（如 401） → session_dead 并停止
    async def redeem_retry_unauthorized(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {"starter_status": "ready"}})
        if request.url.path == EP_REDEEM:
            import json as _json

            tier = _json.loads(request.content)["tier"]
            if isinstance(tier, int):
                return httpx.Response(400, json={"code": 400, "msg": "unknown tier"})
            return httpx.Response(403, json={"code": 403, "msg": "过期"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result4 = await _run(redeem_retry_unauthorized)
    assert result4.session_dead is True

    # 连登兑换：tier 重试成功 → 正常记奖励
    async def redeem_retry_ok(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {"starter_status": "ready"}})
        if request.url.path == EP_REDEEM:
            import json as _json

            if isinstance(_json.loads(request.content)["tier"], int):
                return httpx.Response(400, json={"code": 400, "msg": "unknown tier"})
            return httpx.Response(200, json={"code": 0, "data": {"credit": 3, "energy": 1}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result5 = await _run(redeem_retry_ok)
    assert result5.ok is True and result5.credit == 3.0


async def test_runner_energy_and_streak_display_failures_are_silent():
    """展示字段（能量/连签）取不到时静默：不能因此把成功的领取判成失败。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_ARRIVED)
        if request.url.path == EP_TRAVEL_CLAIM:
            return httpx.Response(200, json={"code": 0, "data": {"credit": 10}})
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(500, content=b"boom")
        if request.url.path == EP_ENERGY:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    # 关闭不可逆动作时不会查连登状态；能量查询失败必须静默
    result = await _run(handler, allow_irreversible=False)
    assert result.energy is None
    assert result.credit == 10.0
    assert result.ok is True
    assert not any(step.status == StepStatus.FAILED for step in result.steps)

    # 开启不可逆动作时，连登状态 500 记失败；但已有成功的领取 → 整体仍成功
    result2 = await _run(handler, allow_irreversible=True)
    assert any(step.name == "查连登状态" and step.status == StepStatus.FAILED
               for step in result2.steps)
    assert result2.ok is True and result2.gained is True


async def test_runner_buddy_box_reports_default_name_and_gain_flags():
    """Buddy 名字缺失时回落「新 Buddy」；gained/failed 属性口径稳定。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 0}})
        if request.url.path == EP_BUDDY_QUOTA:
            return httpx.Response(200, json={"code": 0, "data": {"affordable": 1}})
        if request.url.path == EP_BUDDY_OPEN:
            return httpx.Response(200, json={"code": 0, "data": {"buddies": [1, 2]}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.gained is True and result.failed == []
    box = next(step for step in result.steps if step.name == "Buddy 盲盒")
    assert box.detail == "×1（新 Buddy）"        # bundles 是列表 → 不是 str → 回落


async def test_runner_unknown_provider_errors_are_attention():
    """非 HTTP 异常（网络中断等）按需要关注处理。"""
    async def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    result = await _run(broken)
    assert result.ok is False
    assert any(step.status == StepStatus.FAILED for step in result.steps)


# --------------------------------------------------------------- 后台任务

@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "growth.sqlite3")
    apply_schema(db.connect())
    cipher = CredentialCipher(SECRET)
    yield CredentialRepository(db, cipher), GrowthRepository(db)
    db.close()


class _StubGrowthProvider:
    """可控的成长中心 provider：记录调用、可注入结果或异常。"""

    id = "codebuddy"

    def __init__(self, *, result=None, error=None, scope="acct") -> None:
        self.calls = 0
        self._result = result or GrowthResult(
            ok=True, report="领旅行礼物：+10 积分", credit=10.0,
            steps=[GrowthStep("领旅行礼物", StepStatus.DONE, "+10 积分")])
        self._error = error
        self._scope = scope
        self.allow_flags: list[bool] = []

    async def growth(self, _data, *, allow_irreversible: bool = True):
        self.calls += 1
        self.allow_flags.append(allow_irreversible)
        if self._error is not None:
            raise self._error
        return self._result

    def checkin_scope(self, data):
        # 与真实 provider 同款语义：身份未知（含空 scope）时返回空串，
        # 由任务回落到 credential_id（绝不共享）
        identity = data.get("account_uid", "")
        if not self._scope or not identity:
            return ""
        return f"{self._scope}|{identity}"


async def test_growth_task_records_event_and_last_result(repo):
    """一轮成功：落 events 表 + 回写 credentials.growth_last_result。"""
    credentials, events = repo
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


async def test_growth_task_skips_providers_without_growth_and_disabled(repo):
    """TRAE（无成长中心）与硬禁用凭证都跳过，且不发任何请求。"""
    credentials, events = repo
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


async def test_growth_task_dedupes_same_upstream_account(repo):
    """同上游账号多凭证共享一轮：避免重复领取与多余请求。"""
    credentials, events = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "same"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b",
                                                          "account_uid": "same"})
    provider = _StubGrowthProvider()
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.attempted == 1 and report.skipped == 1
    assert provider.calls == 1


async def test_growth_task_empty_scope_does_not_share(repo):
    """身份未知（空 scope）时绝不共享：宁可多领一次也不能漏掉一个账号。"""
    credentials, events = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    provider = _StubGrowthProvider(scope="")
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.attempted == 2 and provider.calls == 2


async def test_growth_task_failure_is_isolated_and_recorded(repo):
    """单凭证异常不拖累其余；失败也要落库（否则线上坏了没人知道）。"""
    credentials, events = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "a",
                                                          "account_uid": "1"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b",
                                                          "account_uid": "2"})
    provider = _StubGrowthProvider(error=RuntimeError("boom"))
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.attempted == 2 and report.failed == 2
    assert events.latest_for(credentials.candidates()[0].credential_id) is None


async def test_growth_task_marks_session_dead_result_as_failed(repo):
    """session_dead 结果记为失败（调度器据此硬禁用凭证）。"""
    credentials, events = repo
    credential_id = credentials.add(provider="codebuddy",
                                    credential_data={"bearer_token": "t"})
    dead = GrowthResult(ok=False, session_dead=True, report="登录态已失效，请重新登录")
    provider = _StubGrowthProvider(result=dead)
    report = await GrowthTask(credentials, {"codebuddy": provider}, events).run_once()
    assert report.failed == 1 and report.succeeded == 0
    stored = events.latest_for(credential_id)
    assert stored["session_dead"] == 1 and stored["ok"] == 0


async def test_growth_task_uses_pacer_and_due_is_true(repo):
    """节流器每轮每凭证各取一次 turn；due() 恒真（节奏由 runner 间隔控制）。"""
    credentials, events = repo
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


async def test_growth_task_skips_vanished_credential_data(repo):
    """凭证在读取时消失 → 计入 skipped（不能抛异常中断整轮）。"""
    credentials, events = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    original = credentials.credential_data
    credentials.credential_data = lambda _cid: None       # type: ignore[method-assign]
    report = await GrowthTask(credentials, {"codebuddy": _StubGrowthProvider()},
                              events).run_once()
    credentials.credential_data = original                # type: ignore[method-assign]
    assert report.skipped == 1 and report.attempted == 0


async def test_growth_task_pacer_disabled_path(repo):
    """未装配 pacer 时不报错（老部署/测试路径）。"""
    credentials, events = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    report = await GrowthTask(credentials, {"codebuddy": _StubGrowthProvider()},
                              events, pacer=None).run_once()
    assert report.succeeded == 1


# --------------------------------------------------------------- runner 装配

def test_build_runner_wires_growth_task(repo):
    """build_runner 传入 events 仓库时装配成长中心；不传则不装配（无落库目标）。"""
    credentials, events = repo
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR="./data",
                        GROWTH_INTERVAL_MINUTES=30, GROWTH_IRREVERSIBLE_ACTIONS=False)
    runner = build_runner(credentials, {}, None, settings, growth_events=events)
    assert runner._growth is not None
    assert runner._growth._allow_irreversible is False
    assert runner._growth_interval == 1800
    assert build_runner(credentials, {}, None, settings)._growth is None


def test_build_runner_clamps_growth_interval(repo):
    """成长中心间隔下限 5 分钟：请求量比签到大一个量级，更密只会撞风控。"""
    credentials, events = repo
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR="./data",
                        GROWTH_INTERVAL_MINUTES=0)
    runner = build_runner(credentials, {}, None, settings, growth_events=events)
    assert runner._growth_interval == 300


async def test_runner_sync_growth_and_loop_registration(repo):
    """成长中心循环被注册进 asyncio 任务列表，_sync_growth 委托给任务对象。"""
    credentials, events = repo
    provider = _StubGrowthProvider()
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = GrowthTask(credentials, {"codebuddy": provider}, events)
    runner = TaskRunner(quota_probe=type("Q", (), {"run_once": staticmethod(_noop)})(),
                        checkin=type("C", (), {"due": staticmethod(lambda: False)})(),
                        growth=task, refresh=type("R", (), {"run_once": _noop})(),
                        retention=type("T", (), {"run_once": staticmethod(lambda: None)})())
    await runner._sync_growth()
    assert provider.calls == 1


async def _noop(*_args, **_kwargs):
    return None


# --------------------------------------------------------------- 管理台端点

def _admin_client(tmp_path):
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    return app, client


def test_growth_endpoints_run_and_history(tmp_path):
    """手动执行 + 历史查询；无该能力的渠道与不存在的凭证都返回 400。"""
    app, client = _admin_client(tmp_path)
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        def __init__(self) -> None:
            self.flags: list[bool] = []

        async def growth(self, _data, *, allow_irreversible: bool = True):
            self.flags.append(allow_irreversible)
            return GrowthResult(ok=True, credit=42.0, energy=7, streak_days=3,
                                report="领旅行礼物：+42 积分",
                                steps=[GrowthStep("领旅行礼物", StepStatus.DONE, "+42 积分")])

    stub = Stub()
    app.state.executor._deps.providers["codebuddy"] = stub
    with client:
        body = client.post(f"/api/credentials/{credential_id}/growth").json()
        assert body["ok"] is True and body["credit"] == 42.0 and body["streak_days"] == 3
        assert body["steps"] == [{"name": "领旅行礼物", "status": "done",
                                  "detail": "+42 积分", "credit": None}]
        assert stub.flags == [True]                 # 跟随配置开关

        history = client.get(f"/api/credentials/{credential_id}/growth").json()["events"]
        assert len(history) == 1 and history[0]["trigger"] == "manual"
        assert history[0]["report"] == "领旅行礼物：+42 积分"

        # 列表携带最近一次结果，界面不必再查一次 events
        listed = client.get("/api/credentials").json()["credentials"][0]
        assert listed["growth_last_result"] == "领旅行礼物：+42 积分"

        # TRAE 不支持成长中心 → 稳定可读的错误
        trae_id = app.state.credentials.add(provider="trae",
                                            credential_data={"accessToken": "t"})
        assert client.post(f"/api/credentials/{trae_id}/growth").status_code == 400
        assert client.get("/api/credentials/cred_missing/growth").status_code == 400
        assert client.post("/api/credentials/cred_missing/growth").status_code == 400


def test_growth_endpoint_respects_irreversible_setting(tmp_path):
    """配置关闭不可逆动作时，手动入口也必须跟着关（不能只挡定时任务）。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", GROWTH_IRREVERSIBLE_ACTIONS=False)
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        def __init__(self) -> None:
            self.flags: list[bool] = []

        async def growth(self, _data, *, allow_irreversible: bool = True):
            self.flags.append(allow_irreversible)
            return GrowthResult(ok=True, report="无可领取项")

    stub = Stub()
    app.state.executor._deps.providers["codebuddy"] = stub
    with client:
        client.post(f"/api/credentials/{credential_id}/growth")
    assert stub.flags == [False]


def test_growth_history_limit_and_empty(tmp_path):
    """历史查询：limit 生效，空历史返回空数组。"""
    app, client = _admin_client(tmp_path)
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})
    with client:
        assert client.get(f"/api/credentials/{credential_id}/growth").json() == {"events": []}
    for index in range(3):
        app.state.growth_events.record(
            credential_id=credential_id,
            result=GrowthResult(ok=True, report=f"第 {index} 轮"),
            now=1000 + index)
    with client:
        events = client.get(f"/api/credentials/{credential_id}/growth?limit=2").json()["events"]
    assert [event["report"] for event in events] == ["第 2 轮", "第 1 轮"]


def test_growth_repository_recent_clamps_limit(repo):
    """limit 下限保护：<=0 时仍返回至少一行（否则界面永远空着像坏了）。"""
    credentials, events = repo
    credential_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    events.record(credential_id=credential_id, result=GrowthResult(report="x"), now=1)
    assert len(events.recent(credential_id, limit=0)) == 1
    assert events.latest_for("cred_missing") is None


def test_growth_schema_migration_from_legacy_db(tmp_path):
    """老库升级：补 growth 列 + 建 growth_events 表，历史数据不丢。"""
    import sqlite3

    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, data_enc TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            health INTEGER, cooling_until INTEGER, err_count INTEGER NOT NULL DEFAULT 0,
            quota_remaining REAL, quota_total REAL, quota_cycle_end INTEGER,
            quota_probed_at INTEGER, created_at INTEGER NOT NULL, added_by TEXT)
    """)
    conn.execute("INSERT INTO credentials (id, provider, data_enc, created_at) "
                 "VALUES ('cred_old', 'codebuddy', 'x', 1)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    assert {"growth_last_run_at", "growth_last_result"} <= columns
    tables = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "growth_events" in tables
    # 历史凭证保留，新列为 NULL
    row = db.connect().execute(
        "SELECT growth_last_result, created_at FROM credentials WHERE id = 'cred_old'").fetchone()
    assert row["growth_last_result"] is None and row["created_at"] == 1
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    apply_schema(db.connect())                        # 二次执行不抛错
    db.close()


def test_growth_schema_index_exists(tmp_path):
    """growth_events 的 (credential_id, ts) 索引必须建出来（历史查询走它）。"""
    db = Database(tmp_path / "index.sqlite3")
    apply_schema(db.connect())
    indexes = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_growth_cred_ts" in indexes
    db.close()


async def test_provider_growth_delegates_to_runner_and_closes_pool():
    """provider.growth 走缓存客户端；aclose 释放 growth 连接池。"""
    from src.provider.codebuddy.client import _GROWTH_CACHE, _cached_growth

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_ENERGY:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 3}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    from src.provider.codebuddy.client import CodeBuddyClient

    client = CodeBuddyClient(endpoint="https://e",
                             short_client=_client(handler))
    provider = CodeBuddyProvider(client=client)
    result = await provider.growth({"bearer_token": "t"}, allow_irreversible=False)
    assert result.ok is True and result.energy == 3
    assert _cached_growth(client) is _cached_growth(client)
    assert id(client) in _GROWTH_CACHE
    await provider.aclose()
    assert id(client) not in _GROWTH_CACHE


# --------------------------------------------------------------- 未覆盖分支收尾

def test_dig_skips_none_and_non_dict_wrappers():
    """信封查找：值为 None 视为未命中继续找；非 dict 的包裹层跳过。"""
    assert dig({"a": None, "data": {"a": 5}}, "a") == 5
    assert dig({"data": [1, 2], "result": {"a": 7}}, "a") == 7
    assert dig({"data": {"a": None}}, "a") is None


def test_parse_tasks_skips_non_dict_entries():
    """列表中混入非对象条目时跳过，不能因此丢掉整份任务列表。"""
    tasks = parse_tasks({"tasks": ["junk", {"task_code": "t1", "title": "任务"}]})
    assert len(tasks) == 1 and tasks[0].task_code == "t1"


def test_streak_status_post_init_defaults_dates():
    """直接构造 StreakStatus 时 makeup_dates 默认空列表（不是 None）。"""
    from src.provider.codebuddy.growth import StreakStatus

    assert StreakStatus().makeup_dates == []
    assert StreakStatus(makeup_dates=["2026-09-01"]).makeup_dates == ["2026-09-01"]


async def test_growth_client_401_without_message_uses_generic_text():
    """401 但响应体没有 msg → 回落通用说明，方便排障一眼看出是登录问题。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401})

    client = CodeBuddyGrowth("https://e", client=_client(handler))
    with pytest.raises(GrowthRejected) as caught:
        await client.energy(CodeBuddyCredential(bearer_token="t"))
    assert caught.value.message == "credential rejected"
    await client.aclose()


async def test_message_of_ignores_non_string_and_non_dict_bodies():
    """失败消息提取：非字符串值/非对象体都跳过，最终回落空串。"""
    from src.provider.codebuddy.growth import _message_of

    assert _message_of(httpx.Response(500, json={"msg": 5})) == ""
    assert _message_of(httpx.Response(500, json=["x"])) == ""
    assert _message_of(httpx.Response(500, json={"message": "第二优先"})) == "第二优先"
    assert _message_of(httpx.Response(500, json={"error": "第三优先"})) == "第三优先"


async def test_runner_task_accept_failure_stops_on_session_dead():
    """任务 accept 返回 401 → 立即停止该轮（不再处理后续任务）。"""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t1", "title": "任务一", "accept_status": ""},
                {"task_code": "t2", "title": "任务二", "accept_status": ""}]}})
        return httpx.Response(401, json={"code": 401, "msg": "过期"})

    result = await _run(handler)
    assert result.session_dead is True
    assert calls.count(EP_TASK_ACCEPT) == 1          # 第一个任务 401 后不再打第二个


async def test_runner_redeem_unexpected_exception_is_attention():
    """连登兑换遇到非 HTTP 异常（网络中断）按需要关注处理。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {"starter_status": "ready"}})
        if request.url.path == EP_REDEEM:
            raise httpx.ConnectError("no route")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.ok is False
    assert any("连登兑换" in step.name and step.status == StepStatus.FAILED
               for step in result.steps)


async def test_runner_makeup_leftover_branch_and_missing_card_balance():
    """补登剩余天数提示只在卡还够时输出；回包没给剩余卡数时用本地递减兜底。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json={"code": 0, "data": {
                "streak": {"days": 1, "makeup_dates": ["2026-09-01", "2026-09-02"]},
                "makeup_cards": {"balance": 1}}})       # 只有 1 张卡
        if request.url.path == EP_MAKEUP_USE:
            return httpx.Response(200, json={"code": 0, "data": {}})   # 回包没给卡数
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    details = [step.detail for step in result.steps if step.name == "补登"]
    assert details[0] == "2026-09-01（剩 0 张卡）"     # 本地递减兜底
    # 卡已用完（1 - 1 = 0 不大于 MAKEUP_MAX_PER_RUN）→ 不提示还有可补天数
    assert not any("另有" in detail for detail in details)


# --------------------------------------------------------------- 最后的分支

async def test_growth_client_close_is_noop_without_pool():
    """没建立连接池时 aclose 是 no-op（不能因为没事干就报错）。"""
    client = CodeBuddyGrowth("https://e")
    await client.aclose()
    await client.aclose()


async def test_runner_skips_claimed_and_unfinished_tasks():
    """已领奖/锁定/已接单未完成三类任务都必须跳过（不重复请求上游）。"""
    accepts: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [
                {"task_code": "t_claimed", "title": "已领", "accept_status": "claimed"},
                {"task_code": "t_locked", "title": "锁定", "accept_status": "", "locked": True},
                {"task_code": "t_doing", "title": "进行中", "accept_status": "accepted",
                 "progress": {"current": 1, "target": 3}},
                {"task_code": "t_new", "title": "新任务", "accept_status": ""}]}})
        if request.url.path == EP_TASK_ACCEPT:
            accepts.append(_json.loads(request.content)["task_code"])
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert accepts == ["t_new"]                     # 只有未领取的任务被接单
    assert result.ok is True


async def test_runner_redeem_retry_non_session_failure_is_attention():
    """tier 换写法重试后仍是普通失败（500）→ 记失败但不断整轮。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "ready", "advanced_status": "ready"}})
        if request.url.path == EP_REDEEM:
            if isinstance(_json.loads(request.content)["tier"], int):
                return httpx.Response(400, json={"code": 400, "msg": "unknown tier"})
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.ok is False
    assert sum(1 for step in result.steps if "连登兑换" in step.name) == 2
    assert result.session_dead is False


async def test_runner_redeem_unexpected_error_continues_to_next_tier():
    """一个档位遇到网络异常后，下一档仍会被尝试（互不影响）。"""
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "ready", "advanced_status": "ready"}})
        if request.url.path == EP_REDEEM:
            tier = _json.loads(request.content)["tier"]
            tiers.append(tier)
            if tier == 7:
                raise httpx.ConnectError("no route")
            return httpx.Response(200, json={"code": 0, "data": {"credit": 50}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert tiers == [7, 14]                          # 第一档网络异常后仍尝试第二档
    assert result.credit == 50.0


async def test_runner_lottery_prize_missing_and_no_address_flag():
    """奖品字段全缺失 → 显示「未知」；没有实物标志就不追加提醒。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json=REDEEM_LOCKED)
        if request.url.path == EP_LOTTERY_CHANCES:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == EP_LOTTERY_DRAW:
            return httpx.Response(200, json={"code": 0, "data": {"need_address": False}})
        return httpx.Response(200, json={"code": 0, "data": {"affordable": 0}})

    result = await _run(handler)
    draw = next(step for step in result.steps if step.name == "开盲盒")
    assert draw.detail == "未知"
    assert "实物奖" not in draw.detail


async def test_runner_redeem_session_dead_stops_round():
    """连登兑换遇 401（非 tier 问题）→ 停止整轮，不再尝试下一档。"""
    tiers: list[object] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_STREAK:
            return httpx.Response(200, json=STREAK_BODY)
        if request.url.path == EP_REDEEM_SUMMARY:
            return httpx.Response(200, json={"code": 0, "data": {
                "starter_status": "ready", "advanced_status": "ready"}})
        if request.url.path == EP_REDEEM:
            tiers.append(_json.loads(request.content)["tier"])
            return httpx.Response(401, json={"code": 401, "msg": "过期"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    assert result.session_dead is True
    assert tiers == [7]                              # 401 后不再尝试进阶档


async def test_runner_depart_without_duration_omits_eta():
    """时长拿不到时不编造数字：只说「已出发」，不显示「? 小时后回」。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {
                "locations": [{"id": 1, "name": "咖啡馆"}]}})
        if request.url.path == EP_TRAVEL_DEPART:
            return httpx.Response(200, json={"code": 0, "data": {"ok": True}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _run(handler)
    step = next(step for step in result.steps if step.name == "派 Buddy")
    assert step.detail == "去咖啡馆"
    assert "?" not in step.detail
