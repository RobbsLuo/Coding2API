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
import ipaddress

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from ..auth.rbac import require_operator
from ..compat.openai.errors import error_payload
from .context import COMPRESS_STATS
from .deps import Services, principal_from_request

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
    latency: list[tuple[str, float, float, int]] | None = None,
    inflight: int | None = None,
    compress: dict[str, int] | None = None,
    stats_write_failures: int | None = None,
    model_list_degraded: list[str] | None = None,
) -> str:
    """把各数据源的聚合结果渲染成 Prometheus text 格式（纯函数）。

    每个指标家族输出 `# HELP` + `# TYPE` + 样本行；样本次序由调用方保证稳定
    （数据源查询已排序），这里不再重排，保持输出可复现。

    `latency` / `inflight` / `compress` / `model_list_degraded` 为后加的可选
    输入：缺省时不输出对应家族（旧调用方与测试逐字节不变）。
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

    if latency:
        # 平均值而非直方图：桶边界会随渠道/模型差异失真（见 StatsQuery 同函数注释）
        lines.append(f"# HELP {PREFIX}latency_ms_avg 成功请求的平均端到端延迟（毫秒）。")
        lines.append(f"# TYPE {PREFIX}latency_ms_avg gauge")
        lines.append(f"# HELP {PREFIX}ttfb_ms_avg 成功请求的平均首字延迟（毫秒）。")
        lines.append(f"# TYPE {PREFIX}ttfb_ms_avg gauge")
        lines.append(f"# HELP {PREFIX}latency_samples 参与平均值的成功请求数。")
        lines.append(f"# TYPE {PREFIX}latency_samples gauge")
        for provider, avg_latency, avg_ttfb, samples in latency:
            labels = [("provider", provider)]
            lines.append(_sample(f"{PREFIX}latency_ms_avg", labels, round(avg_latency, 1)))
            lines.append(_sample(f"{PREFIX}ttfb_ms_avg", labels, round(avg_ttfb, 1)))
            lines.append(_sample(f"{PREFIX}latency_samples", labels, samples))

    lines.append(f"# HELP {PREFIX}credentials 凭证池健康计数（四态互斥，与 /healthz 同口径）。")
    lines.append(f"# TYPE {PREFIX}credentials gauge")
    for state in _CREDENTIAL_STATES:
        lines.append(_sample(f"{PREFIX}credentials", [("state", state)],
                             int(credentials.get(state, 0))))

    if inflight is not None:
        # 在途是瞬时值：排障时「卡住」表现为它持续不降，比看日志快
        lines.append(f"# HELP {PREFIX}inflight 当前在途请求数（凭证租约合计）。")
        lines.append(f"# TYPE {PREFIX}inflight gauge")
        lines.append(_sample(f"{PREFIX}inflight", [], int(inflight)))

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

    if model_list_degraded is not None:
        # 「渠道模型表来自静态兜底」是**看得见**的静默故障：provider 内部把
        # 动态拉取失败转成静态表并照常返回，只靠日志（还会被退避压成 INFO）
        # 时，管理台完全看不出这个渠道一直没拿到上游真值（issue #56）。
        lines.append(f"# HELP {PREFIX}model_list_degraded 当前模型表来自静态兜底"
                     "（非上游真值）的渠道，1=降级。")
        lines.append(f"# TYPE {PREFIX}model_list_degraded gauge")
        degraded = set(model_list_degraded)
        for provider in sorted(degraded):
            lines.append(_sample(f"{PREFIX}model_list_degraded",
                                 [("provider", provider)], 1))

    if stats_write_failures is not None:
        # 统计写入失败是**看得见**的静默故障：`record()` 把异常吞掉以免拖垮
        # 聊天响应，没有这个计数器时「用量丢了」在进程外完全不可见。
        lines.append(f"# HELP {PREFIX}stats_write_failures_total 统计写入失败的行数"
                     "（含重试前的每次失败；重启归零）。")
        lines.append(f"# TYPE {PREFIX}stats_write_failures_total counter")
        lines.append(_sample(f"{PREFIX}stats_write_failures_total", [],
                             int(stats_write_failures)))

    if compress is not None:
        # 压缩收益与缓存命中率必须**同屏**：压缩省下的 token 只有在「前缀缓存
        # 还命中」时才是净收益（每次裁剪都会让前缀边界前移）。这两个家族放在
        # 同一份响应里，抓取端与统计页才能并排读。
        lines.append(f"# HELP {PREFIX}compress_triggers_total 压缩实际改写载荷的次数"
                     "（tool 输出压缩 + 上下文裁剪；重启归零）。")
        lines.append(f"# TYPE {PREFIX}compress_triggers_total counter")
        lines.append(_sample(f"{PREFIX}compress_triggers_total", [],
                             int(compress.get("triggers", 0))))
        lines.append(f"# HELP {PREFIX}compress_tool_triggers_total 其中由 tool 输出"
                     "压缩触发的次数。")
        lines.append(f"# TYPE {PREFIX}compress_tool_triggers_total counter")
        lines.append(_sample(f"{PREFIX}compress_tool_triggers_total", [],
                             int(compress.get("tool_triggers", 0))))
        lines.append(f"# HELP {PREFIX}compress_context_trims_total 其中由上下文裁剪"
                     "触发的次数（每次裁剪都会前移前缀边界，可能压低缓存命中率）。")
        lines.append(f"# TYPE {PREFIX}compress_context_trims_total counter")
        lines.append(_sample(f"{PREFIX}compress_context_trims_total", [],
                             int(compress.get("context_trims", 0))))
        lines.append(f"# HELP {PREFIX}compress_chars_saved_total 省下的字符数"
                     "（整份 messages 字符串 content 长度差；重启归零）。")
        lines.append(f"# TYPE {PREFIX}compress_chars_saved_total counter")
        lines.append(_sample(f"{PREFIX}compress_chars_saved_total", [],
                             int(compress.get("chars_saved", 0))))
        lines.append(f"# HELP {PREFIX}compress_tokens_saved_total 省下的 token 估算"
                     "（字符数 × 0.25 下界系数，仅用于量级判断）。")
        lines.append(f"# TYPE {PREFIX}compress_tokens_saved_total counter")
        lines.append(_sample(f"{PREFIX}compress_tokens_saved_total", [],
                             int(compress.get("tokens_saved", 0))))
        lines.append(f"# HELP {PREFIX}compress_unchanged_total 调用了压缩但载荷未变"
                     "的次数（开关开着却无收益的证据）。")
        lines.append(f"# TYPE {PREFIX}compress_unchanged_total counter")
        lines.append(_sample(f"{PREFIX}compress_unchanged_total", [],
                             int(compress.get("unchanged", 0))))

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


def _is_loopback(request: Request) -> bool:
    """对端是否回环地址（无令牌时的唯一放行条件）。

    解析失败按**不放行**处理：`request.client` 在 ASGI 里允许为 None（如 UDS
    或测试替身），拿不到可信对端就当外部访问，宁可 401 也不裸暴露。
    """
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        """Prometheus 抓取端点。

        **未配置 `METRICS_TOKEN` 时只放行回环对端**：端点会暴露渠道与模型清单、
        凭证池四态计数、延迟与失败分布，匿名裸暴露等于把侦察图送到公网。同机
        抓取（Prometheus 与被抓服务共机、或经 sidecar 转发）从 `127.0.0.1` /
        `::1` 来，天然放行；跨机抓取请显式设 `METRICS_TOKEN`，配置后必须带
        `Authorization: Bearer <token>`（缺失或错误一律 401，不回显差在哪）。
        对端地址解析不出来（`request.client` 为空 / 非 IP 字面量）按**非回环**
        处理——拿不到可信对端就不能当内网。
        """
        if not bool(services.settings.metrics_enabled):
            return JSONResponse(status_code=404,
                                content=error_payload("metrics disabled",
                                                      "invalid_request", 404))
        token = str(services.settings.metrics_token or "")
        if token:
            if not _token_matches(request.headers.get("authorization", ""), token):
                return JSONResponse(status_code=401,
                                    content=error_payload("metrics unauthorized",
                                                          "unauthorized", 401))
        elif not _is_loopback(request):
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
            latency=services.stats_query.metrics_provider_latency(),
            # 在途从调度器的租约跟踪器现读：它是「卡住」的唯一实时信号
            inflight=(services.inflight.total()
                      if services.inflight is not None else 0),
            # 压缩收益与上面同一份响应：省下的 token 要跟命中率并排看
            compress=_compress_snapshot(request),
            # 统计写失败：`record()` 吞异常是刻意的（不能拖垮聊天响应），
            # 所以这条必须出进程，否则「用量丢了」只在日志里留一行 WARNING。
            stats_write_failures=stats.write_failures(),
            # 模型表降级的渠道：静态兜底在 provider 内部是「正常返回」，
            # 不出这个 gauge 时外部完全看不到（issue #56 第三条）。
            model_list_degraded=sorted(
                pid for pid, flag in services.model_list_degraded.items() if flag),
        )
        return Response(content=text, headers={"Content-Type": CONTENT_TYPE})

    @router.get("/api/stats/compression")
    async def compression_stats(request: Request,
                                principal=Depends(principal_from_request),
                                since: int | None = None,
                                until: int | None = None):
        """压缩收益 + 开关状态（统计页「压缩」面板的数据源）。

        这里**只回计数与开关**，不含任何用户/凭证维度：它是运维判断「该开还是
        该关」的进程级读数，任何已登录用户读到的事实相同。前端把这份数据与
        同窗口的 `/api/stats/overview`（缓存命中率）并排展示——省下的 token
        只有在缓存仍命中时才是净收益。

        `since` / `until`：与其它统计端点同口径的半开小时窗 `[since, until)`，
        只汇总落在窗口内的小时桶；两端都不给 → 本次进程运行的终身累计
        （「全部」）。不传窗口就等于旧行为，`/metrics` 始终读终身累计。

        刻意不做成 `/metrics` 的替代品，也不新增重型聚合：数据来自压缩闭包里的
        进程内计数器，按小时桶求和（最多 90 天 × 24 桶），O(桶数)。

        仅 operator+：计数是**池级/进程级**的，viewer 的个人视图做不了「与我的
        命中率同窗口」的对比；给出全局数只会让两边口径错位。
        """
        require_operator(principal)
        settings = services.settings
        return {
            "stats": _compress_snapshot(request, since=since, until=until),
            "context_compress_enabled": bool(settings.context_compress_enabled),
            "tool_output_compress_enabled": bool(settings.tool_output_compress_enabled),
        }

    return router


def _compress_snapshot(request: Request, *, since: int | None = None,
                       until: int | None = None) -> dict[str, int]:
    """压缩计数器快照：优先读 app.state 注入的实例（测试/多实例部署可隔离），
    未装配时回落到进程级单例（与 `build_context_compressor` 默认值同一对象）。

    `since` / `until` 透传 `CompressStats.as_dict`：给了就只汇总窗口内的小时桶。
    """
    stats = getattr(request.app.state, "compress_stats", None)
    return (stats if stats is not None else COMPRESS_STATS).as_dict(
        since=since, until=until)
