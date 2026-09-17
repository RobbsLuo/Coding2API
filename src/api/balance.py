"""余额查询端点（GET /v1/user/balance）。

余额属于共享凭证池而非个人：读 credentials 表里周期探测写回的
quota_remaining / quota_total（QuotaProbeTask 每 QUOTA_PROBE_MINUTES 刷新），
不实时打上游——快、且不会把上游探测接口打成大。

响应取 DeepSeek 余额接口的字段形状（is_available + balance_infos），
便于 Cherry Studio 等客户端解析；本项目没有货币单位，币种固定
"credits"（上游额度单位）。路径带 /v1 前缀，与 /v1/models 一致。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends

from .deps import Services, api_key_user


def _credential_usable(row: dict[str, Any]) -> bool:
    """与调度器 selectable 口径一致：软开关开启且未被硬禁用。

    冷却中的凭证额度仍然真实可用（额度是账户属性，冷却只是调度退避），
    因此计入余额；hard-disabled / 用户关闭的凭证不算。
    """
    return bool(row["enabled"]) and not bool(row["disabled"])


def _balance_rows(services: Services) -> Iterator[dict[str, Any]]:
    for row in services.credentials.list_all():
        if _credential_usable(row) and row["quota_total"] is not None:
            yield row


def balance_payload(services: Services) -> dict[str, Any]:
    """把凭证池探测缓存聚合成余额响应。"""
    per_provider: dict[str, dict[str, float]] = {}
    for row in _balance_rows(services):
        provider = row["provider"]
        bucket = per_provider.setdefault(
            provider, {"remaining": 0.0, "total": 0.0, "probed": 0})
        bucket["remaining"] += float(row["quota_remaining"] or 0.0)
        bucket["total"] += float(row["quota_total"])
        bucket["probed"] += 1

    total_remaining = sum(b["remaining"] for b in per_provider.values())
    total = sum(b["total"] for b in per_provider.values())
    # 池里没有带额度数据的凭证（从未探测成功/池为空）→ 未知而非 0，
    # 与健康度三态语义一致：探测失败是 unknown，不能当作耗尽。
    known = total > 0
    return {
        "is_available": known and total_remaining > 0,
        "balance_infos": [{
            "currency": "credits",
            "total_balance": f"{total_remaining:.2f}" if known else "0.00",
            "granted_balance": "0.00",       # 项目无赠送/充值之分，恒 0
            "topped_up_balance": "0.00",
        }],
        # ---- 扩展字段（DeepSeek schema 之外的补充，客户端会忽略）----
        "balance_known": known,
        "providers": [
            {"provider": provider, "remaining": bucket["remaining"],
             "total": bucket["total"], "credentials": bucket["probed"]}
            for provider, bucket in sorted(per_provider.items())
        ],
    }


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/user/balance")
    async def user_balance(_user: str = Depends(api_key_user)):
        return balance_payload(services)

    return router
