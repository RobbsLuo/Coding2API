"""Prometheus 指标出口：`GET /metrics`（零依赖手写 text exposition 格式）。

为什么不引 `prometheus_client`：本服务只暴露一组固定的小指标，官方客户端
会带进一整套注册表/多进程/GC 采集器（与本研究无关，还会带来版本升级面）。
同类项目（`workbuddy2api`）同样手写；格式本身只有几行规则。

口径与数据源（TECHNICAL §11.2）：
- `requests_total` / `tokens_total`：读**永久保留**的 `usage_hourly`（与总览、
  图表同源），因此是跨重启单调的累计值；明细表 `usage_events` 只留 90 天，
  拿它当 `_total` 会随裁剪下降，且 scrape 时全表 GROUP BY 正是本端点要避免的。
- `credentials`：`pool_counts()`（与 `/healthz` 同口径，四态互斥）。
- `upstream_errors_total`：`StatsCollector.error_totals()`（进程内计数器）。
  小时汇总没有 `error_type` 维度，明细表裁剪会让计数器下降，故用进程内累计；
  重启归零由 Prometheus 的 counter reset 语义吸收。
- `task_failures_total`：`TaskStatusStore.failures()`（进程内，重启归零）。

**禁用高基数标签**：不打 `credential_id` / `api_key_id` / `username` / `model`
以外的自由文本——一个账号一条时间序列会让 Prometheus 侧内存与查询都失控。
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..compat.openai.errors import error_payload
from .deps import Services

# Prometheus 文本格式版本；抓取方据此选择解析器。
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# 指标名前缀：本项目是单一服务的导出器，用固定前缀避免与同机其它导出器撞名。
PREFIX = "coding2api_"

# 凭证池四态的固定输出顺序：次序稳定便于文本比对与测试。
_CREDENTIAL_STATES = ("ready", "cooling", "paused", "disabled")


def _escape(value: str) -> str:
    """转义标签值：`\\` / `"` / 换行是 text 格式里唯三需要转义的字符。

    模型名允许出现 `"` 与 `\\`（`_safe_model` 只拦空白与控制字符），不转义会
    拼出非法样本行，整份响应被 Prometheus 丢弃。
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _sample(name: str, labels: list[tuple[str, str]], value: float | int) -> str:
    """拼一行样本：`name{key="value",...} value`；无标签时省略花括号。"""
    if not labels:
        return f"{name} {value}"
    rendered = ",".join(f'{key}="{_escape(label)}"' for key, label in labels)
    return f"{name}{{{rendered}}} {value}"


def render_metrics(
    *,
    requests: list[tuple[str, str, int, int]],
    tokens: list[tuple[str, int, int, int, int]],
    credentials: dict[str, int],
    errors: list[tuple[str, str, int]],
    task_failures: list[tuple[str, int]],
) -> str:
    """把各数据源的聚合结果渲染成 Prometheus text 格式（纯函数）。

    每个指标家族输出 `# HELP` + `# TYPE` + 样本行；样本次序由调用方保证稳定
    （数据源查询已排序），这里不再重排，保持输出可复现。
    """
    lines: list[str] = []

    lines.append(f"# HELP {PREFIX}requests_total 累计请求数（按渠道 / 模型 / 结果）。")
    lines.append(f"# TYPE {PREFIX}requests_total counter")
    for provider, model, total, ok_count in requests:
        labels = [("provider", provider), ("model", model)]
        lines.append(_sample(f"{PREFIX}requests_total", labels + [("status", "ok")],
                             ok_count))
        lines.append(_sample(f"{PREFIX}requests_total", labels + [("status", "error")],
                             max(total - ok_count, 0)))

    lines.append(f"# HELP {PREFIX}tokens_total 累计 token 数（按渠道 / 类别）。")
    lines.append(f"# TYPE {PREFIX}tokens_total counter")
    for provider, input_tokens, output_tokens, reasoning_tokens, cached_tokens in tokens:
        for kind, value in (("input", input_tokens), ("output", output_tokens),
                            ("reasoning", reasoning_tokens), ("cached", cached_tokens)):
            lines.append(_sample(f"{PREFIX}tokens_total",
                                 [("provider", provider), ("type", kind)], value))

    lines.append(f"# HELP {PREFIX}credentials 凭证池健康计数（四态互斥，与 /healthz 同口径）。")
    lines.append(f"# TYPE {PREFIX}credentials gauge")
    for state in _CREDENTIAL_STATES:
        lines.append(_sample(f"{PREFIX}credentials", [("state", state)],
                             int(credentials.get(state, 0))))

    lines.append(f"# HELP {PREFIX}upstream_errors_total 进程内累计失败请求数"
                 "（按渠道 / 失败原因；重启归零）。")
    lines.append(f"# TYPE {PREFIX}upstream_errors_total counter")
    for provider, kind, count in errors:
        lines.append(_sample(f"{PREFIX}upstream_errors_total",
                             [("provider", provider), ("kind", kind)], count))

    lines.append(f"# HELP {PREFIX}task_failures_total 后台任务进程内累计失败次数"
                 "（重启归零）。")
    lines.append(f"# TYPE {PREFIX}task_failures_total counter")
    for task, count in task_failures:
        lines.append(_sample(f"{PREFIX}task_failures_total", [("task", task)], count))

    return "\n".join(lines) + "\n"


def _token_matches(header: str, expected: str) -> bool:
    """Bearer 头校验（恒时比较；前缀大小写不敏感，Prometheus 发 `Bearer`）。

    编码成 bytes：`hmac.compare_digest` 对含非 ASCII 的 `str` 会抛 TypeError，
    而这里的输入完全来自外部请求头。
    """
    prefix = "bearer "
    if not header.lower().startswith(prefix):
        return False
    supplied = header[len(prefix):].strip()
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        """Prometheus 抓取端点。

        默认**不鉴权**——与同样裸暴露的 `/healthz` 同口径，且指标已禁用高基数
        标签（只有渠道 / 模型 / 状态等有限集合）。但端点会暴露渠道与模型清单、
        凭证池四态计数，对外部署可设 `METRICS_TOKEN` 收口：配置后必须带
        `Authorization: Bearer <token>`（缺失或错误一律 401，不回显差在哪）。
        """
        if not bool(services.settings.metrics_enabled):
            return JSONResponse(status_code=404,
                                content=error_payload("metrics disabled",
                                                      "invalid_request", 404))
        token = str(services.settings.metrics_token or "")
        if token and not _token_matches(
                request.headers.get("authorization", ""), token):
            return JSONResponse(status_code=401,
                                content=error_payload("metrics unauthorized",
                                                      "unauthorized", 401))
        stats = request.app.state.stats_collector
        runner = getattr(request.app.state, "task_runner", None)
        # 只报**已装配**的任务（与 /api/tasks 同源）：未装配的 key 恒 0 会
        # 制造「任务在跑但从不失败」的假象。task_status() 已按 TASK_SPECS 排序。
        task_failures = (
            [(item["key"], runner.status.failures(item["key"]))
             for item in runner.task_status()]
            if runner is not None else [])
        text = render_metrics(
            requests=services.stats_query.metrics_provider_model(),
            tokens=services.stats_query.metrics_provider_tokens(),
            credentials=services.credentials.pool_counts(),
            errors=stats.error_totals(),
            task_failures=task_failures,
        )
        return Response(content=text, headers={"Content-Type": CONTENT_TYPE})

    return router
