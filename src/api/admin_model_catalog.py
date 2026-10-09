"""models.dev 模型目录（管理台「模型列表」页）：刊例价 + 明细元数据，只读。

数据来自 `https://models.dev/api.json`，由后台 `price_catalog` 任务周期抓取并
落盘。这里在同一次抓取里既取价格（统计页**成本估算**的输入：`input` /
`output` / `cache_read`，单位 USD / 百万 token），也取更详细的元数据——展示名 /
所属家族 / 上下文与输出上限 / 输入·输出模态 / 能力（附件·推理·工具调用·结构化
输出）/ 是否开放权重 / 知识截止 / 发布日期 / provider。目录缺失时回空表（页面
显示空态），不影响聊天与成本（成本另行读价表）。

目录挂在 `app.state.models_dev_catalog`（`main.lifespan` 同步回灌 + 后台刷新
就地为它换值），故这里从 request 取，而不是塞进 Services——与 model_catalog
快照同类：是运行数据，不是仓储/引擎依赖。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .deps import Services, principal_from_request


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/model-catalog")
    async def get_model_catalog(request: Request,
                                _principal=Depends(principal_from_request)):
        """当前生效模型目录 + 元信息；仅要求登录（models.dev 是公开数据，各角色可看）。"""
        state = request.app.state
        catalog: dict[str, dict] = state.models_dev_catalog
        return {
            # 按模型 id 升序：结果确定，前端无需再排
            "models": [catalog[model] for model in sorted(catalog)],
            "count": len(catalog),
            "currency": "USD",
            "usd_cny_rate": float(services.settings.usd_cny_rate),
            "saved_at": state.price_saved_at,
        }

    return router