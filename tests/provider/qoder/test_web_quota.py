"""Qoder 网页额度接口解析：逐包到期（`parse_web_quota`）+ 哨兵到期值。"""

from __future__ import annotations

import time

from src.provider.qoder.events import (
    EXPIRY_SENTINEL_MIN,
    parse_quota,
    parse_web_quota,
)

NOW = 1_794_420_000


def _pack(remaining: float, expires_at: int | None, *,
          limit: float = 100, used: float = 0,
          source: str = "RESOURCE_PACKAGE_SOURCE_BONUS",
          active: bool = True) -> dict:
    item: dict = {
        "id": "p",
        "limit_value": limit,
        "used_value": used,
        "remaining_value": remaining,
        "is_active": active,
        "source": source,
        "status": "ACTIVE",
    }
    item["expires_at"] = expires_at
    return item


def _container(summary: dict, detail: list[dict]) -> dict:
    return {"quota_summary": summary, "quota_detail": detail}


def _summary(limit: float, used: float, remaining: float) -> dict:
    return {"limit_value": limit, "used_value": used,
            "remaining_value": remaining, "usage_percentage": 0,
            "unit": "credits"}


REAL_SHAPE = {
    "user_id": "u-1",
    "quota_key": "big_model_credits",
    "status": "active",
    "plan_quota": _container(_summary(0, 0, 0), [
        _pack(0, 0, limit=0, source="PLAN"),
    ]),
    "resource_package_quota": _container(_summary(400, 45, 355), [
        _pack(100, 1_796_000_000),
        _pack(100, 1_795_000_000, used=50),
        _pack(0, 1_797_000_000, used=100),     # 已用完：展示保留、不进阶梯
        _pack(100, None),                       # 无到期：不进阶梯
        _pack(100, 1_798_000_000, active=False),  # 已失效：不进阶梯
        _pack(5, 253_402_214_400_000),             # 哨兵到期：按无到期处理
        "junk",                                     # 非对象条目：跳过
    ]),
    "dedicated_resource_package_quota": _container(_summary(0, 0, 0), []),
}


def test_parse_web_quota_expands_packages_and_builds_ladder():
    quota = parse_web_quota(REAL_SHAPE, now=NOW)

    assert quota is not None
    # 汇总按容器求和（不再叠加顶层 total_quota，否则翻倍）
    assert quota.total == 400
    assert quota.remaining == 355
    # 哨兵到期（9999-12-31）归成 None，不参与阶梯
    assert [p["end"] for p in quota.packages].count(None) == 3
    # 逐包明细：套餐内的 0 额包也保留（排查「这个容器到底有没有」要看得到）
    names = [p["name"] for p in quota.packages]
    assert names[0] == "套餐内积分"
    assert names.count("赠送积分") == 6
    # 到期阶梯只收「有效 + 有剩余 + 有到期」，并按到期升序
    assert quota.expiry_ladder == [(1_795_000_000, 100.0),
                                   (1_796_000_000, 100.0)]
    assert quota.cycle_end == 1_795_000_000
    assert quota.probed_at == NOW


def test_parse_web_quota_accepts_camel_case_aliases():
    payload = {
        "planQuota": {
            "quotaSummary": {"limitValue": 10, "usedValue": 2,
                             "remainingValue": 8},
            "quotaDetail": [{
                "limitValue": 10, "usedValue": 2, "remainingValue": 8,
                "expiresAt": 1_796_500_000_000, "isActive": True,
            }],
        },
    }
    quota = parse_web_quota(payload, now=NOW)
    assert quota is not None
    assert quota.total == 10 and quota.remaining == 8
    assert quota.expiry_ladder == [(1_796_500_000, 8.0)]
    # 未知 source 退回容器名
    assert quota.packages == [{"name": "套餐内积分", "total": 10.0,
                              "used": 2.0, "end": 1_796_500_000}]


def test_parse_web_quota_unknown_shape_returns_none():
    """容器全缺或形状变了 → None（调用方回退汇总接口），不能当成额度 0。"""
    assert parse_web_quota({}, now=NOW) is None
    assert parse_web_quota({"quotaKey": "big_model_credits"}, now=NOW) is None
    # detail 全不是对象也不臆造
    assert parse_web_quota(
        {"plan_quota": _container(_summary(0, 0, 0), [])}, now=NOW) is None
    # detail 不是数组（上游改形状）也按无明细处理，不抛错
    assert parse_web_quota(
        {"plan_quota": {"quota_summary": _summary(10, 0, 10),
                        "quota_detail": "oops"}}, now=NOW) is None


def test_parse_quota_treats_far_future_expiry_as_no_expiry():
    """9999-12-31 是上游「无到期」哨兵：不能当真实重置日显示。"""
    payload = {
        "userQuota": {"total": 100, "used": 40, "remaining": 60},
        "addOnQuota": {"total": 50, "used": 10, "remaining": 40},
        "expiresAt": 253_402_214_400_000,
    }
    quota = parse_quota(payload, now=NOW)
    assert quota.cycle_end is None
    assert all(p["end"] is None for p in quota.packages or [])

    real = parse_quota({**payload, "expiresAt": 1_795_000_000_000}, now=NOW)
    assert real.cycle_end == 1_795_000_000
    assert time.time() < EXPIRY_SENTINEL_MIN