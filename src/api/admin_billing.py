"""月度账单：按人 / 按 Key 聚合 + CSV / JSONL 导出。

管理面端点（`require_operator`）：账单天然是池级数据，viewer 拿不到——
与明细导出同口径。时间窗按**服务器本地时区**的整月计算（运维看的「10 月账单」
就是本地 10 月，不是 UTC 10 月）。

三种格式共用同一份查询结果：

- `json`（默认）：`{month, start, end, users, keys, totals}`，前端页面直接渲染；
- `csv`：按人汇总，带 BOM（Excel 打开中文表头不乱码）；
- `jsonl`：该月**逐条明细**（一行一个 JSON 对象），便于下游账务系统二次聚合。
  明细只保留 90 天，很早的月份会得到空文件——这是保留策略，不是 bug。

导出同样写审计（与 `GET /api/stats/events?format=csv` 一致）：谁在什么时候
把哪个月的账单拉走了。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..audit.actions import ACTION_DATA_EXPORT
from ..auth.rbac import require_operator
from ..compat.openai.request import InvalidRequest
from ..stats.csv import CSV_BOM, csv_line
from .deps import Services, principal_from_request, request_ip

# 账单 CSV 的列（与前端表格列同源；顺序固定，下游按列名解析而不是按位置）
BILLING_HEADERS: tuple[str, ...] = (
    "username", "requests", "ok_count", "input_tokens", "output_tokens",
    "cached_tokens", "credit", "cost_cny", "success_rate",
)


def month_range(month: str) -> tuple[int, int]:
    """`YYYY-MM` → 本地时区 `[月初, 下月初)` 的 epoch 秒；格式错误抛 InvalidRequest。"""
    parts = month.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise InvalidRequest("month 必须是 YYYY-MM")
    try:
        year, mon = int(parts[0]), int(parts[1])
    except ValueError as error:
        raise InvalidRequest("month 必须是 YYYY-MM") from error
    # 4 位年份上限由格式校验保证（`len(parts[0]) == 4` → ≤ 9999），这里只挡下界
    if year < 1970 or not 1 <= mon <= 12:
        raise InvalidRequest("month 超出有效范围")
    nxt = (year + 1, 1) if mon == 12 else (year, mon + 1)
    start = int(time.mktime((year, mon, 1, 0, 0, 0, 0, 0, -1)))
    end = int(time.mktime((nxt[0], nxt[1], 1, 0, 0, 0, 0, 0, -1)))
    return start, end


def _cell(value: object) -> str:
    return "" if value is None else str(value)


def _users_csv(users: list[dict]) -> Iterator[str]:
    yield CSV_BOM + csv_line(list(BILLING_HEADERS))
    for row in users:
        yield csv_line([_cell(row.get(column)) for column in BILLING_HEADERS])


def _events_jsonl(rows: list[dict]) -> Iterator[str]:
    for row in rows:
        yield json.dumps(row, ensure_ascii=False) + "\n"


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/billing")
    async def billing(request: Request,
                      principal=Depends(principal_from_request),
                      month: str | None = None,
                      format: str = "json"):
        """月度账单（默认当月）。`format` ∈ json / csv / jsonl。"""
        require_operator(principal)
        if format not in ("json", "csv", "jsonl"):
            raise InvalidRequest(f"不支持的 format: {format!r}")
        now = int(time.time())
        current = time.strftime("%Y-%m", time.localtime(now))
        chosen = month or current
        start, end = month_range(chosen)
        if format == "jsonl":
            rows = services.stats_query.export_events(since=start, until=end)
            services.audit.record(
                actor=principal.username, action=ACTION_DATA_EXPORT,
                target="billing", detail=f"JSONL 账单 {chosen} {len(rows)} 行",
                ip=request_ip(request, services.settings))
            return StreamingResponse(
                _events_jsonl(rows), media_type="application/x-ndjson; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="billing-{chosen}.jsonl"',
                    "X-Export-Row-Count": str(len(rows)),
                    "Cache-Control": "no-store",
                })
        report = services.stats_query.billing(start=start, end=end)
        if format == "csv":
            services.audit.record(
                actor=principal.username, action=ACTION_DATA_EXPORT,
                target="billing",
                detail=f"CSV 账单 {chosen} {len(report['users'])} 人",
                ip=request_ip(request, services.settings))
            return StreamingResponse(
                _users_csv(report["users"]), media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="billing-{chosen}.csv"',
                    "Cache-Control": "no-store",
                })
        return {"month": chosen, **report}

    return router
