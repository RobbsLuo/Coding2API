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


def test_quota_packages_ignores_bad_values():
    assert raccoon_activity.quota_packages({}) == []
    assert raccoon_activity.quota_packages(
        {"daily_points": True, "reward_points": "300", "monthly_points": -5}) == []


def test_expiry_ladder_from_bills_sorts_and_filters():
    items = [
        _grant(expire_at="2026-11-10T23:59:59+08:00"),
        _grant(grant_at="2026-11-05T00:00:00+08:00",
               expire_at="2026-11-11T23:59:59+08:00"),
        # 已过期
        _grant(grant_at="2026-11-01T00:00:00+08:00",
               expire_at="2026-11-02T23:59:59+08:00"),
        # 非 daily_grant 不参与
        {"biz_type": "reward_grant", "points": 1500,
         "expire_at": "2026-12-01T00:00:00+08:00"},
        # 缺 expire_at
        _grant(grant_at="2026-11-06T00:00:00+08:00", expire_at=None),
    ]
    ladder = raccoon_activity.expiry_ladder_from_bills(items, NOW)
    assert ladder is not None
    assert [points for _, points in ladder] == [300, 300]
    # 按到期升序
    assert ladder[0][0] < ladder[1][0]


def test_expiry_ladder_skips_already_expired_pairs():
    """已被 daily_expire 清零的那笔不再算「即将到期」。"""
    items = [
        _grant(grant_at="2026-11-03T00:00:00+08:00",
               expire_at="2026-11-09T23:59:59+08:00"),
        {"biz_type": "daily_expire", "points": -300,
         "grant_at": "2026-11-03T00:00:00+08:00"},
    ]
    assert raccoon_activity.expiry_ladder_from_bills(items, NOW) is None


def test_expiry_ladder_returns_none_when_empty():
    assert raccoon_activity.expiry_ladder_from_bills([], NOW) is None
    assert raccoon_activity.expiry_ladder_from_bills(["junk"], NOW) is None


def test_expiry_ladder_ignores_non_positive_and_bad_grants():
    """`daily_grant` 但点数非正 / 非数值 → 不进阶梯（防御上游畸形数据）。"""
    items = [
        {"biz_type": "daily_grant", "points": 0,
         "expire_at": "2026-11-10T23:59:59+08:00"},
        {"biz_type": "daily_grant", "points": True,
         "expire_at": "2026-11-10T23:59:59+08:00"},
    ]
    assert raccoon_activity.expiry_ladder_from_bills(items, NOW) is None


def test_expired_keys_ignores_bad_points():
    """`daily_expire` 点数非数值 → 跳过，不影响其余配对。"""
    items = [
        _grant(grant_at="2026-11-03T00:00:00+08:00",
               expire_at="2026-11-09T23:59:59+08:00"),
        {"biz_type": "daily_expire", "points": "bad",
         "grant_at": "2026-11-03T00:00:00+08:00"},
        {"biz_type": "daily_expire", "points": True,
         "grant_at": "2026-11-03T00:00:00+08:00"},
    ]
    # 坏点数不算「已清零」，那笔 grant 仍在阶梯里
    ladder = raccoon_activity.expiry_ladder_from_bills(items, NOW)
    assert ladder is not None and ladder[0][1] == 300


def test_heartbeat_headers_include_required_client_headers():
    headers = raccoon_activity.heartbeat_headers("dev-abc")
    # 缺 x-client-device-id 上游回 100002 params_invalid_error
    assert headers["x-client-device-id"] == "dev-abc"
    assert headers["x-client-platform"] == "web"
    assert headers["Content-Type"] == "application/json"