"""小浣熊签到 / 活跃上报 / 积分明细的纯函数。"""

from __future__ import annotations

from datetime import datetime

from src.provider.raccoon import activity as raccoon_activity

NOW = datetime.fromisoformat("2026-11-04T22:00:00+08:00")


def _grant(*, points=300, grant_at="2026-11-04T00:00:00+08:00",
           created_at="2026-11-04T21:41:38+08:00",
           expire_at="2026-11-10T23:59:59+08:00", biz_type="daily_grant"):
    return {"biz_type": biz_type, "points": points, "grant_at": grant_at,
            "created_at": created_at, "expire_at": expire_at}


def test_parse_iso_handles_offsets_naive_and_garbage():
    parsed = raccoon_activity.parse_iso("2026-11-04T21:41:38+08:00")
    assert parsed is not None and parsed.year == 2026
    # 带 Z 的 UTC 也认
    assert raccoon_activity.parse_iso("2026-11-04T13:41:38Z") is not None
    # 无时区按 +08:00 解释（上游全用这个时区）
    naive = raccoon_activity.parse_iso("2026-11-04T21:41:38")
    assert naive is not None and naive.utcoffset().total_seconds() == 8 * 3600
    for bad in (None, "", "  ", 123, "not-a-date"):
        assert raccoon_activity.parse_iso(bad) is None


def test_pick_today_grant_takes_today_only():
    items = [
        _grant(grant_at="2026-11-03T00:00:00+08:00"),
        _grant(),
        "junk",
        {"biz_type": "daily_grant", "points": -300},
        {"biz_type": "daily_expire", "points": -300},
        {"biz_type": "daily_grant", "points": 100,
         "grant_at": "2026-11-04T00:00:00+08:00"},
    ]
    row = raccoon_activity.pick_today_grant(items, NOW)
    # 同一天多条取点数最大的（正常发放 300 优先于补发 100）
    assert row is not None and row["points"] == 300


def test_pick_today_grant_returns_none_when_absent():
    assert raccoon_activity.pick_today_grant([], NOW) is None
    assert raccoon_activity.pick_today_grant(
        [_grant(grant_at="2026-11-03T00:00:00+08:00")], NOW) is None
    # points 为 bool / 字符串 / 0 都不算
    assert raccoon_activity.pick_today_grant(
        [{"biz_type": "daily_grant", "points": True,
          "grant_at": "2026-11-04T00:00:00+08:00"}], NOW) is None
    assert raccoon_activity.pick_today_grant(
        [{"biz_type": "daily_grant", "points": "300",
          "grant_at": "2026-11-04T00:00:00+08:00"}], NOW) is None


def test_pick_today_grant_falls_back_to_created_at():
    row = raccoon_activity.pick_today_grant(
        [{"biz_type": "daily_grant", "points": 300,
          "created_at": "2026-11-04T09:00:00+08:00"}], NOW)
    assert row is not None


def test_checkin_result_three_states():
    started = NOW.timestamp()
    # ① 本次触发的懒发放：created_at 在窗口内
    fresh = raccoon_activity.checkin_result(
        [_grant(created_at="2026-11-04T21:59:30+08:00")], started, now=NOW)
    assert fresh.ok and not fresh.already_checked_in and fresh.credit == 300
    assert "到期" in fresh.message

    # ② 今天早些时候已发：created_at 早于窗口
    early = raccoon_activity.checkin_result(
        [_grant(created_at="2026-11-04T08:00:00+08:00")], started, now=NOW)
    assert early.ok and early.already_checked_in
    assert "不是本次触发" in early.message

    # ③ 今日未发放：按失败报（可重试）
    missing = raccoon_activity.checkin_result([], started, now=NOW)
    assert not missing.ok and "尚未发放" in missing.message


def test_checkin_result_without_expire_date():
    result = raccoon_activity.checkin_result(
        [_grant(expire_at=None, created_at="2026-11-04T21:59:30+08:00")],
        NOW.timestamp(), now=NOW)
    assert "到期" not in result.message


def test_pick_active_grants_keeps_only_unexpired_and_unconsumed():
    items = [
        # 仍有效的两笔（不同到期日）
        _grant(grant_at="2026-11-04T00:00:00+08:00",
               expire_at="2026-11-10T23:59:59+08:00"),
        _grant(grant_at="2026-11-05T00:00:00+08:00",
               expire_at="2026-11-11T23:59:59+08:00"),
        # 已过期
        _grant(grant_at="2026-11-01T00:00:00+08:00",
               expire_at="2026-11-02T23:59:59+08:00"),
        # 缺 expire_at：无法判断有效期，不算「可用」
        _grant(grant_at="2026-11-06T00:00:00+08:00", expire_at=None),
        # 已被同一天同额度的 daily_expire 清零
        _grant(grant_at="2026-11-03T00:00:00+08:00",
               expire_at="2026-11-09T23:59:59+08:00"),
        {"biz_type": "daily_expire", "points": -300,
         "grant_at": "2026-11-03T00:00:00+08:00"},
        # 非 daily_grant / 非正点数
        {"biz_type": "reward_grant", "points": 1500,
         "expire_at": "2026-12-01T00:00:00+08:00"},
        {"biz_type": "daily_grant", "points": True,
         "expire_at": "2026-11-12T23:59:59+08:00"},
    ]
    active = raccoon_activity.pick_active_grants(items, NOW)
    assert [item["points"] for item in active] == [300, 300]
    # 按到期升序：11-10 在前、11-11 在后
    assert active[0]["expire_at"] < active[1]["expire_at"]


def test_checkin_result_uses_active_grant_when_today_missing():
    """今天没有新发放，但还有未过期积分 → 视为已签，不再报失败。"""
    items = [_grant(grant_at="2026-11-02T00:00:00+08:00",
                    expire_at="2026-11-10T23:59:59+08:00"),
             _grant(grant_at="2026-11-03T00:00:00+08:00",
                    expire_at="2026-11-11T23:59:59+08:00")]
    result = raccoon_activity.checkin_result(items, NOW.timestamp(), now=NOW)
    assert result.ok and result.already_checked_in
    assert result.credit == 600
    assert "今日额度可用 600" in result.message
    assert "本次未产生新发放" in result.message
    # 22:00 已过宽限，直接封到当日结束（seal_until=None）
    assert result.seal_until is None


def test_checkin_result_active_grant_before_grace_delays_seal():
    """凌晨 00:10 看到旧积分：先算已签，但封账只到 01:30，等账单同步后再核。"""
    early = datetime.fromisoformat("2026-11-05T00:10:00+08:00")
    items = [_grant(grant_at="2026-11-04T00:00:00+08:00",
                    expire_at="2026-11-10T23:59:59+08:00")]
    result = raccoon_activity.checkin_result(items, early.timestamp(), now=early)
    assert result.ok and result.already_checked_in
    grace = raccoon_activity.grant_grace_at(early)
    assert result.seal_until == int(grace.timestamp())


def test_checkin_result_missing_before_grace_sets_retry_at():
    early = datetime.fromisoformat("2026-11-05T00:10:00+08:00")
    result = raccoon_activity.checkin_result([], early.timestamp(), now=early)
    assert not result.ok
    grace = raccoon_activity.grant_grace_at(early)
    assert result.retry_at == int(grace.timestamp())


def test_checkin_result_missing_after_grace_has_no_retry_at():
    assert raccoon_activity.checkin_result(
        [], NOW.timestamp(), now=NOW).retry_at is None


def test_grant_grace_at_is_cst_0130():
    early = datetime.fromisoformat("2026-11-05T00:10:00+08:00")
    grace = raccoon_activity.grant_grace_at(early)
    assert grace.isoformat() == "2026-11-05T01:30:00+08:00"
    # UTC 输入也归一成 CST
    utc = datetime.fromisoformat("2026-11-04T16:10:00+00:00")
    assert raccoon_activity.grant_grace_at(utc).isoformat() == \
        "2026-11-05T01:30:00+08:00"


def test_quota_packages_maps_four_pools():
    packages = raccoon_activity.quota_packages({
        "daily_points": 274, "reward_points": 6000,
        "monthly_points": 0, "topup_points": 100, "topup_frozen": True,
    })
    assert [p["name"] for p in packages] == ["每日额度", "奖励积分", "充值积分"]
    # 冻结的充值池照常列出，但标不可用
    assert packages[-1]["usable"] is False
    assert packages[0]["usable"] is True


def test_quota_packages_expands_grants_with_expiry_and_fifo_usage():
    """按发放流水逐笔展开：每笔带真实到期；每日池的消耗从最早到期那笔扣。"""
    items = [
        _grant(points=300, grant_at="2026-11-04T00:00:00+08:00",
               expire_at="2026-11-10T23:59:59+08:00"),
        _grant(points=300, grant_at="2026-11-03T00:00:00+08:00",
               expire_at="2026-11-09T23:59:59+08:00"),
        _grant(points=1500, grant_at=None,
               created_at="2026-11-02T15:58:54+08:00",
               expire_at=None, biz_type="reward_grant"),
        # 0 点奖励（上游偶发）：不进明细
        _grant(points=0, grant_at=None,
               created_at="2026-11-02T15:58:55+08:00",
               expire_at=None, biz_type="reward_grant"),
        # 奖励也可能带到期（活动积分）：照常展示到期
        _grant(points=200, grant_at=None,
               created_at="2026-11-01T09:30:00+08:00",
               expire_at="2026-12-01T23:59:59+08:00",
               biz_type="reward_grant"),
        # 已过期：不进明细
        _grant(points=300, grant_at="2026-10-01T00:00:00+08:00",
               expire_at="2026-10-07T23:59:59+08:00"),
    ]
    packages = raccoon_activity.quota_packages(
        {"daily_points": 450, "reward_points": 1500}, items, NOW)
    daily = [p for p in packages if p["name"].startswith("每日积分")]
    assert [p["name"] for p in daily] == ["每日积分 11-03", "每日积分 11-04"]
    # 已消耗 150：最早到期那笔先扣
    assert [p["total"] - p["used"] for p in daily] == [150, 300]
    assert daily[0]["end"] == 1794239999        # 2026-11-09T23:59:59+08:00
    reward = [p for p in packages if p["name"].startswith("奖励积分")]
    assert reward[0]["total"] == 1500 and reward[0]["end"] is None


def test_quota_packages_adds_pool_rows_without_grant_coverage():
    """池里的可用量比流水多（同步延迟）：补一条无到期余额，合计仍与池值一致。"""
    items = [_grant(points=300, grant_at="2026-11-04T00:00:00+08:00")]
    packages = raccoon_activity.quota_packages(
        {"daily_points": 800, "monthly_points": 50}, items, NOW)
    daily = next(p for p in packages if p["name"].startswith("每日积分 11-04"))
    assert daily["total"] - daily["used"] == 300
    rest = next(p for p in packages if p["name"].startswith("每日积分（流水未覆盖）"))
    assert rest["total"] == 500 and rest["end"] is None
    assert any(p["name"] == "月度积分" for p in packages)


def test_quota_packages_treats_missing_pool_as_grant_sum():
    """余额没给池字段：按发放额合计，不把所有发放都算成已用完。"""
    packages = raccoon_activity.quota_packages(
        {}, [_grant(points=300)], NOW)
    assert packages[0]["total"] - packages[0]["used"] == 300


def test_quota_packages_lists_uncovered_pool_balances():
    """流水取不到（None）或没有任何发放 → 退回四池口径。"""
    balance = {"daily_points": 300, "reward_points": 1500}
    assert [p["name"] for p in raccoon_activity.quota_packages(balance, None)] == \
        ["每日额度", "奖励积分"]
    assert [p["name"] for p in raccoon_activity.quota_packages(
        balance, [{"biz_type": "desktop", "points": -1}], NOW)] == \
        ["每日额度", "奖励积分"]


def test_quota_packages_ignores_bad_values():
    assert raccoon_activity.quota_packages({}) == []
    assert raccoon_activity.quota_packages(
        {"daily_points": True, "reward_points": "300", "monthly_points": -5}) == []


def test_ladder_from_packages_uses_remaining_and_sorts():
    """阶梯与明细同源：取 total-used（不是发放额），按到期升序，跳过无到期/已用完。"""
    ladder = raccoon_activity.ladder_from_packages([
        {"name": "每日积分 11-09", "total": 300.0, "used": 0.0,
         "end": 1794326399, "usable": True},
        {"name": "每日积分 11-04", "total": 300.0, "used": 126.0,
         "end": 1794239999, "usable": True},
        {"name": "奖励积分 11-05", "total": 1500.0, "used": 0.0,
         "end": None, "usable": True},
        {"name": "每日积分 10-01", "total": 300.0, "used": 300.0,
         "end": 1790000000, "usable": True},
    ])
    assert ladder == [(1794239999, 174.0), (1794326399, 300.0)]


def test_ladder_from_packages_none_when_no_expiry():
    assert raccoon_activity.ladder_from_packages([]) is None
    assert raccoon_activity.ladder_from_packages([
        {"name": "奖励积分", "total": 100.0, "used": 0.0, "end": None},
    ]) is None


def test_pick_active_grants_ignores_malformed_expire_rows():
    """`daily_expire` 点数非数值 → 跳过，不把对应发放误判为已清零。"""
    items = [
        _grant(grant_at="2026-11-03T00:00:00+08:00",
               expire_at="2026-11-09T23:59:59+08:00"),
        {"biz_type": "daily_expire", "points": "bad",
         "grant_at": "2026-11-03T00:00:00+08:00"},
        {"biz_type": "daily_expire", "points": True,
         "grant_at": "2026-11-03T00:00:00+08:00"},
    ]
    active = raccoon_activity.pick_active_grants(items, NOW)
    assert len(active) == 1 and active[0]["points"] == 300


def test_heartbeat_headers_include_required_client_headers():
    headers = raccoon_activity.heartbeat_headers("dev-abc")
    # 缺 x-client-device-id 上游回 100002 params_invalid_error
    assert headers["x-client-device-id"] == "dev-abc"
    assert headers["x-client-platform"] == "web"
    assert headers["Content-Type"] == "application/json"