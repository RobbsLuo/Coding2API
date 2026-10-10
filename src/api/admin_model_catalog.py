"""models.dev 模型目录（管理台「模型列表」页）：刊例价 + 明细元数据 + 能力分，只读。

数据来自 `https://models.dev/api.json`，由后台 `price_catalog` 任务周期抓取并
落盘。这里在同一次抓取里既取价格（统计页**成本估算**的输入：`input` /
`output` / `cache_read`，单位 USD / 百万 token），也取更详细的元数据——展示名 /
所属家族 / 上下文与输出上限 / 输入·输出模态 / 能力（附件·推理·工具调用·结构化
输出）/ 是否开放权重 / 知识截止 / 发布日期 / provider。目录缺失时回空表（页面
显示空态），不影响聊天与成本（成本另行读价表）。

**能力分**（2026-10 起）来自另一个后台任务 `benchmark_catalog`（Artificial
Analysis 指数，经 OpenRouter 公开接口，见 `src/benchmarks.py`），挂在
`app.state.model_benchmarks`。这里按**同一套匹配口径**（`model_match.lookup`：
候选键等值、唯一命中才采用）给每行补 `benchmarks` 字段——models.dev 的 id
（`tencent/hy3`）与本项目归一键（`hy3`）写法不同，靠的就是那层统一规则。
匹配不到的行**不带**该字段，页面显示 `—`；宁可不配也不错配。

目录挂在 `app.state.models_dev_catalog`（`main.lifespan` 同步回灌 + 后台刷新
就地为它换值），故这里从 request 取，而不是塞进 Services——与 model_catalog
快照同类：是运行数据，不是仓储/引擎依赖。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..benchmarks import BenchmarkTable
from ..model_match import lookup
from ..sorting import sort_rows
from .deps import Services, principal_from_request

# 模型目录排序白名单：API 键 → 取值函数（缺失值排最后）。
MODEL_CATALOG_SORT_KEYS = {
    "id": lambda row: row["id"],
    "name": lambda row: row["name"],
    "provider": lambda row: row["provider"],
    "context": lambda row: row["context"],
    "max_output": lambda row: row["max_output"],
    "knowledge": lambda row: row["knowledge"],
    "release_date": lambda row: row["release_date"],
    "input": lambda row: row["input"],
    "output": lambda row: row["output"],
    "cache_read": lambda row: row["cache_read"],
    "cache_write": lambda row: row["cache_write"],
}


def _with_benchmarks(rows: list[dict[str, Any]],
                     table: BenchmarkTable | None) -> list[dict[str, Any]]:
    """给目录行补能力分（匹配不到就不带该字段；表为空时全部原样返回）。"""
    if not table:
        return rows
    enriched: list[dict[str, Any]] = []
    for row in rows:
        score = lookup(table, row.get("id"), row.get("name"))
        enriched.append({**row, "benchmarks": score} if score is not None else row)
    return enriched


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/model-catalog")
    async def get_model_catalog(request: Request, sort: str | None = None,
                                order: str | None = None,
                                _principal=Depends(principal_from_request)):
        """当前生效模型目录 + 元信息；仅要求登录（models.dev 是公开数据，各角色可看）。"""
        state = request.app.state
        catalog: dict[str, dict] = state.models_dev_catalog
        # 默认按模型 id 升序：结果确定（前端无需再排），与历史行为一致。
        models = sort_rows([catalog[model] for model in sorted(catalog)], sort, order,
                           MODEL_CATALOG_SORT_KEYS, default_key="id", default_desc=False)
        models = _with_benchmarks(models, getattr(state, "model_benchmarks", None))
        return {
            "models": models,
            "count": len(catalog),
            "currency": "USD",
            "usd_cny_rate": float(services.settings.usd_cny_rate),
            # 目录快照时刻（models.dev）：页面显示「目录更新」。
            "saved_at": state.price_saved_at,
            # 能力分快照时刻：与目录快照分开记（两个后台任务、两个落盘文件），
            # 缺失即「本次启动以来没拉到过」，页面显示 —。
            "benchmark_saved_at": getattr(state, "benchmark_saved_at", None),
        }

    return router
