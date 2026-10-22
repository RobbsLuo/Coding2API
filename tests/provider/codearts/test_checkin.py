"""CodeArts 每日签到。"""

from __future__ import annotations

import json

import httpx

from src.provider.codearts import CodeArtsProvider
from src.provider.codearts import events as codearts_events
from tests.provider.codearts._support import client as _client
from tests.provider.codearts._support import cred as _cred


def _delivery(campaign: dict | None = None, *, extra_items: list | None = None) -> dict:
    items = list(extra_items or [])
    if campaign is not None:
        items.append(campaign)
    return {"data": {"items": items}}

def _campaign(**over) -> dict:
    base = {
        "campaignId": 1, "title": "每日签到领1000 积分", "type": "USER_LOGIN",
        "benefitAmount": 1000, "benefitUnit": "CREDIT",
        "claimable": True, "status": "ELIGIBLE", "pendingTotalAmount": 1000.0,
        "extra": {"endTime": "2026-12-30T16:00:00Z",
                  "triggerMode": "MANUAL_CLAIM", "consumePriority": 100},
    }
    base.update(over)
    return base

def test_events_daily_credit_campaign_picks_id_and_tolerates_shape():
    other = {"campaignId": 3, "title": "推荐码上有礼", "status": "ELIGIBLE"}
    data = _delivery(_campaign(), extra_items=[other])
    assert codearts_events.daily_credit_campaign(data)["title"] == "每日签到领1000 积分"
    # 只有别的活动 → None（不靠标题匹配，id 精确挑）
    assert codearts_events.daily_credit_campaign(_delivery(None, extra_items=[other])) is None
    # 形状退化一律 None，不抛（上游改版不该让签到任务炸）
    for bad in (None, {}, {"data": None}, {"data": []}, {"data": {"items": None}},
                {"data": {"items": []}}, {"data": {"items": [None, 5]}}):
        assert codearts_events.daily_credit_campaign(bad) is None
    # campaignId 是 bool 不算 id（_as_int 排除 bool）
    assert codearts_events.daily_credit_campaign(
        _delivery(None, extra_items=[{"campaignId": True}])) is None

def test_events_campaign_field_accessors_tolerate_missing():
    good = _campaign()
    assert codearts_events.campaign_status(good) == "ELIGIBLE"
    assert codearts_events.campaign_status({"status": "  CONFIRMED  "}) == "CONFIRMED"
    assert codearts_events.campaign_status({"status": 5}) == ""
    assert codearts_events.campaign_status(None) == ""
    assert codearts_events.campaign_claimable(good) is True
    assert codearts_events.campaign_claimable({"claimable": "true"}) is False
    assert codearts_events.campaign_claimable(None) is False
    assert codearts_events.campaign_credit(good) == 1000.0
    assert codearts_events.campaign_credit({"benefitAmount": True}) is None
    assert codearts_events.campaign_credit({"benefitAmount": "1000"}) is None
    assert codearts_events.campaign_credit(None) is None
    assert codearts_events.campaign_name(good) == "每日签到领1000 积分"
    assert codearts_events.campaign_name({"title": "  "}) == "每日签到领积分"
    assert codearts_events.campaign_name(None) == "每日签到领积分"

def test_events_campaign_end_epoch_parses_utc_and_degrades():
    # 2026-12-30T16:00:00Z == 2026-12-31 00:00 +08（活动截止，实测原文）
    assert codearts_events.campaign_end_epoch(_campaign()) == 1798646400
    # 无时区后缀按 UTC 处理
    assert codearts_events.campaign_end_epoch(
        {"extra": {"endTime": "2026-12-30T16:00:00"}}) == 1798646400
    # 带偏移量交给 fromisoformat
    assert codearts_events.campaign_end_epoch(
        {"extra": {"endTime": "2026-12-31T00:00:00+08:00"}}) == 1798646400
    # 畸形/缺失 → None（调用方据此「不按过期处理」，宁可多调一次也不漏签）
    for bad in (None, {}, {"extra": None}, {"extra": {}}, {"extra": {"endTime": None}},
                {"extra": {"endTime": ""}}, {"extra": {"endTime": 5}},
                {"extra": {"endTime": "not-a-date"}}):
        assert codearts_events.campaign_end_epoch(bad) is None

async def test_client_fetch_checkin_status_uses_ops_path_without_prefix():
    """`/v1/ops/*` 不带 `snap-manager` 前缀——带上会 APIG.0101（踩坑根因）。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_delivery(_campaign()))

    client = _client(handler)
    campaign = await client.fetch_checkin_status(_cred())
    assert campaign is not None and campaign["campaignId"] == 1
    assert seen == ["https://snap.test/v1/ops/delivery?channel=IDE"]
    await client.aclose()

async def test_client_claim_daily_credit_calls_claim_then_confirm():
    """claim 后必须 confirm：上游 `CLAIMED` 是「已领未确认」而非终态。"""
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        if request.url.path == "/v1/ops/claim":
            return httpx.Response(200, json={"code": 0, "message": "ok"})
        return httpx.Response(200, json={"code": 0, "message": "ok",
                                         "data": {"status": "CONFIRMED"}})

    client = _client(handler)
    code, message = await client.claim_daily_credit(_cred())
    assert (code, message) == (0, "ok")
    assert [path for path, _ in calls] == ["/v1/ops/claim", "/v1/ops/confirm"]
    # idempotentKey 格式由前端 JS 定死：claim_<campaignId>_<毫秒时间戳>
    claim_body = calls[0][1]
    assert claim_body["campaignId"] == 1 and claim_body["channel"] == "IDE"
    assert claim_body["idempotentKey"].startswith("claim_1_")
    assert claim_body["idempotentKey"].rsplit("_", 1)[1].isdigit()
    assert calls[1][1] == {"campaignId": 1}
    await client.aclose()

async def test_client_claim_daily_credit_business_error_skips_confirm():
    """业务码非 0（如 40001 未到刷新时间）走 HTTP 200，不能只看状态码。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"code": 40001,
                                         "message": "尚未到达权益刷新时间，暂时无法领取"})

    client = _client(handler)
    code, message = await client.claim_daily_credit(_cred())
    assert code == 40001 and "刷新时间" in message
    assert calls == ["/v1/ops/claim"]        # 失败不 confirm
    await client.aclose()

async def test_client_claim_daily_credit_missing_code_is_failure():
    """`code` 缺失/非 int → 判失败，且用兜底文案（不把 None 当成功）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": ""})

    client = _client(handler)
    assert await client.claim_daily_credit(_cred()) == (-1, "领取失败")
    await client.aclose()

async def test_client_confirm_daily_credit_swallows_upstream_failure():
    """confirm 失败返回 False（不抛），由 checkin 的 CLAIMED 分支决定是否封账。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error_msg": "boom"})

    client = _client(handler)
    assert await client._confirm_daily_credit(_cred()) is False
    await client.aclose()

async def test_client_checkin_state_machine_branches():
    """四种 status + 活动过期 + 不存在，各自归一（CheckinTask 直接消费）。"""
    # ELIGIBLE 且 claimable → claim + confirm，带回 1000 积分
    def eligible(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/delivery":
            return httpx.Response(200, json=_delivery(_campaign()))
        return httpx.Response(200, json={"code": 0, "message": "ok"})

    client = _client(eligible)
    result = await client.checkin(_cred())
    assert result.ok and not result.already_checked_in and result.credit == 1000.0
    await client.aclose()

    # CLAIMED（已领未确认）→ 只补 confirm，算成功
    def claimed(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/delivery":
            return httpx.Response(200, json=_delivery(
                _campaign(status="CLAIMED", claimable=False)))
        return httpx.Response(200, json={"code": 0, "message": "ok"})

    client = _client(claimed)
    result = await client.checkin(_cred())
    assert result.ok and not result.already_checked_in and result.message == "已补确认签到"
    await client.aclose()

    # CLAIMED 但 confirm 失败 → ok=False，否则 CheckinTask 当日封账不再补
    def claimed_confirm_fail(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/delivery":
            return httpx.Response(200, json=_delivery(
                _campaign(status="CLAIMED", claimable=False)))
        return httpx.Response(500, json={"error_msg": "boom"})

    client = _client(claimed_confirm_fail)
    result = await client.checkin(_cred())
    assert result.ok is False and "确认失败" in result.message
    await client.aclose()

    # CONFIRMED / CONSUMED → 已签，不碰写接口
    for status in ("CONFIRMED", "CONSUMED"):
        def done(request: httpx.Request, _s=status) -> httpx.Response:
            assert request.url.path == "/v1/ops/delivery"   # 只读，无写调用
            return httpx.Response(200, json=_delivery(
                _campaign(status=_s, claimable=False)))

        client = _client(done)
        result = await client.checkin(_cred())
        assert result.ok and result.already_checked_in
        await client.aclose()

    # 活动不存在 → 不是错误（否则后台任务每 10 分钟刷失败日志）
    client = _client(lambda _r: httpx.Response(200, json=_delivery(None)))
    result = await client.checkin(_cred())
    assert result.ok and "未开放" in result.message
    await client.aclose()

    # 活动已过期 → 不是错误
    expired = _campaign(extra={"endTime": "2020-01-01T00:00:00Z"})
    client = _client(lambda _r: httpx.Response(200, json=_delivery(expired)))
    result = await client.checkin(_cred(), now=1_700_000_000)
    assert result.ok and "已结束" in result.message
    await client.aclose()

    # ELIGIBLE 但 claimable=false（未到刷新时间）→ 失败，交给下一轮重试
    not_yet = _campaign(claimable=False)
    client = _client(lambda _r: httpx.Response(200, json=_delivery(not_yet)))
    result = await client.checkin(_cred())
    assert result.ok is False and result.message == "当前不可领取"
    await client.aclose()

    # 未知 status → 失败重试（漏签 1000 积分代价高于多刷日志）
    weird = _campaign(status="SOMETHING_NEW")
    client = _client(lambda _r: httpx.Response(200, json=_delivery(weird)))
    result = await client.checkin(_cred())
    assert result.ok is False
    await client.aclose()

async def test_client_checkin_claim_business_error_is_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/delivery":
            return httpx.Response(200, json=_delivery(_campaign()))
        return httpx.Response(200, json={"code": 40001, "message": "尚未到达权益刷新时间"})

    client = _client(handler)
    result = await client.checkin(_cred())
    assert result.ok is False and result.code == 40001
    await client.aclose()

async def test_provider_checkin_status_and_scope():
    provider = CodeArtsProvider(client=_client(
        lambda _r: httpx.Response(200, json=_delivery(_campaign()))))
    status = await provider.checkin_status(_cred().to_dict())
    assert status == {"active": True, "today_checked_in": False,
                      "today_credit": 1000.0, "activity_name": "每日签到领1000 积分"}
    # uid 隔离；身份未知回落凭证 ID（空串，绝不让两个未知账号共享 scope）
    assert provider.checkin_scope(_cred(uid="u9").to_dict()) == "codearts|u9"
    assert provider.checkin_scope(_cred(uid="").to_dict()) == ""
    await provider.aclose()

async def test_provider_checkin_status_inactive_when_campaign_absent():
    provider = CodeArtsProvider(client=_client(
        lambda _r: httpx.Response(200, json=_delivery(None))))
    status = await provider.checkin_status(_cred().to_dict())
    assert status["active"] is False and status["today_checked_in"] is False
    assert status["today_credit"] is None
    await provider.aclose()

async def test_provider_checkin_delegates_to_client():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/delivery":
            return httpx.Response(200, json=_delivery(_campaign()))
        return httpx.Response(200, json={"code": 0, "message": "ok"})

    provider = CodeArtsProvider(client=_client(handler))
    result = await provider.checkin(_cred().to_dict())
    assert result.ok and result.credit == 1000.0
    await provider.aclose()
