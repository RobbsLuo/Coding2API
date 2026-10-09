"""价表查看（管理台「价表」页）：models.dev 刊例价快照，只读。

价表是统计页**成本估算**（PROPOSAL Q70）的输入：每个模型 `input` / `output` /
`cache_read`，单位 USD / 百万 token。本端点把当前生效价表连同快照保存时刻、
生效汇率一起交给前端只读展示。价表缺失时回空表（页面显示空态），不影响聊天。

表本身挂在 `app.state.price_table`（`main.lifespan` 同步回灌 + 后台刷新就地为
它换值），故这里从 request 取，而不是塞进 Services——与 model_catalog 快照
同类：是运行数据，不是仓储/引擎依赖。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .deps import Services, principal_from_request


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/pricing")
    async def get_pricing(request: Request,
                          _principal=Depends(principal_from_request)):
        """当前生效价表 + 元信息；仅要求登录（刊例价是公开数据，各角色可看）。"""
        state = request.app.state
        table: dict[str, tuple[float, float, float]] = state.price_table
        return {
            # 按模型 id 升序：结果确定，前端无需再排
            "models": [
                {"model": model, "input": price[0], "output": price[1],
                 "cache_read": price[2]}
                for model, price in sorted(table.items())
            ],
            "count": len(table),
            "currency": "USD",
            "usd_cny_rate": float(services.settings.usd_cny_rate),
            "saved_at": state.price_saved_at,
        }

    return router
