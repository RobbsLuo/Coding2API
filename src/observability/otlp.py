"""OTLP/HTTP JSON 指标出口（零 SDK、零新容器）。

为什么手写 JSON 而不引 `opentelemetry-sdk`：本服务只导一组脱敏聚合指标，
官方 SDK 会带进 exporter / 资源检测 / 批量处理器一整套依赖，而 OTLP/HTTP
的 JSON 编码只有一层固定结构（`resourceMetrics → scopeMetrics → metrics`），
照 `metrics.py` 的「零依赖手写」风格实现更可控。`httpx` 已是运行时依赖，
不给部署增加新东西。

**硬边界：只导指标，不导内容。** 本仓不存提示词 / 回答 / system 指令，
因此以下 GenAI 语义约定字段**永不出现**在本模块生成的任何 payload 里：

- `gen_ai.input.messages`
- `gen_ai.output.messages`
- `gen_ai.system_instructions`

这些键连属性名都不该出现（避免下游把空值当成「采集到了但为空」）。测试
直接对整个序列化结果做字符串断言（见 `tests/api/test_otlp.py`）。

指标映射（OpenTelemetry GenAI 语义约定，只取本仓已有聚合口径）：

- `gen_ai.client.operation.duration`（histogram，s）：成功请求的端到端耗时，
  按 `gen_ai.provider.name` 分组；
- `gen_ai.server.time_to_first_token`（histogram，s）：成功请求的首字延迟，
  按 `gen_ai.provider.name` 分组；
- `gen_ai.client.token.usage`（histogram，{token}）：输入 / 输出 token，
  按 `gen_ai.request.model` 分组（小时汇总里模型维度没有渠道列）；
- `coding2api.gen_ai.errors`（sum，{request}）：进程内累计失败次数，按
  `gen_ai.provider.name` + `error.type` 分组。

失败只告警一次：同一段连续失败只在第一次写 WARNING，恢复成功后重新武装，
避免每 60 秒刷一行同样的网络错误。
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

#: OTLP resource 里的服务名：与 Prometheus 指标前缀同源，便于关联两套出口。
SERVICE_NAME = "coding2api"

#: instrumentation scope 名；与 SDK 自动生成的 scope 区分开。
SCOPE_NAME = "coding2api.otlp"

#: 周期导出下限（秒）。与配置层的 `OTLP_EXPORT_SECONDS` 下限保持一致。
MIN_EXPORT_SECONDS = 15

#: 明确禁止导出的内容字段（只用于注释与测试断言，不在 payload 里出现）。
FORBIDDEN_CONTENT_KEYS: tuple[str, ...] = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
)


def metrics_snapshot(stats_query: Any, collector: Any) -> dict[str, Any]:
    """采集一次导出快照（与 `/metrics` 完全同源，只读聚合计数）。

    放在模块级而不是 `create_app` 里的闭包：闭包体只在真正导出时才执行，
    单测无法直接覆盖（会一直挂在 `main.py` 的未覆盖行上）；抽出来后可
    注入替身直接断言四个键。`stats_query` / `collector` 只要求结构兼容，
    因此不 import 具体仓库类型（避免 observability ↔ stats 循环依赖）。
    """
    return {
        "requests": stats_query.metrics_provider_model(),
        "tokens": stats_query.metrics_provider_tokens(),
        "latency": stats_query.metrics_provider_latency(),
        "errors": collector.error_totals(),
    }


def _is_loopback_host(host: str) -> bool:
    """主机名是否是回环（`localhost` / `127.0.0.1` / `::1`）。

    用 `ipaddress` 判 IP 字面量而不是字符串比较：`127.0.0.2` 也是回环，
    而 `[::1]` 的方括号要先剥掉。
    """
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def endpoint_problem(endpoint: str) -> str | None:
    """OTLP 端点形态问题（None = 合法；空串 = 未配置，也返回 None）。

    规则（SSRF 收敛面）：只接受 `http(s)://`；非回环主机强制 https——导出
    请求会带 `OTLP_HEADERS` 里的采集端 token，明文 http 发到公网等于把凭证
    挂在链路上。回环允许 http，方便本机 collector（`localhost:4318`）。
    """
    text = (endpoint or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        # 未闭合的 IPv6 方括号等畸形 URL：urlsplit 直接抛，按非法端点处理
        # （启动期就该失败，而不是让每轮导出在构造 URL 时炸一次）。
        return f"OTLP_ENDPOINT 不是合法的 URL（当前 {endpoint!r}）"
    if parsed.scheme not in ("http", "https"):
        return f"OTLP_ENDPOINT 只接受 http/https（当前 {endpoint!r}）"
    if not parsed.hostname:
        return f"OTLP_ENDPOINT 缺少主机名（当前 {endpoint!r}）"
    if parsed.username or parsed.password:
        return "OTLP_ENDPOINT 不允许携带 userinfo（请改用 OTLP_HEADERS 传令牌）"
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        return ("OTLP_ENDPOINT 对非回环主机必须使用 https（明文 http 会让 "
                "OTLP_HEADERS 里的令牌裸奔）")
    return None


def normalize_endpoint(endpoint: str) -> str:
    """去掉首尾空白与末尾斜杠；拼接时只保留一个 `/v1/metrics`。"""
    return (endpoint or "").strip().rstrip("/")


def parse_otlp_headers(raw: str) -> dict[str, str]:
    """解析 `OTLP_HEADERS`（`k:v;k2:v2`）→ 请求头字典。

    宽容解析：空段、缺冒号、空键的片段直接跳过——采集端 token 里可能含
    `:`，按**第一个**冒号切分；值里的换行剥掉，防止头注入。
    """
    headers: dict[str, str] = {}
    for piece in (raw or "").split(";"):
        segment = piece.strip()
        if not segment or ":" not in segment:
            continue
        key, _, value = segment.partition(":")
        name = key.strip()
        if not name:
            continue
        headers[name] = value.strip().replace("\r", "").replace("\n", "")
    return headers


def _attr(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": str(value)}}


def _point_times(start_ns: int, now_ns: int) -> dict[str, str]:
    return {
        "startTimeUnixNano": str(start_ns),
        "timeUnixNano": str(now_ns),
    }


def _histogram(name: str, unit: str, points: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "unit": unit, "histogram": {"dataPoints": points}}


def _sum_metric(name: str, unit: str, points: list[dict[str, Any]],
                *, monotonic: bool = True) -> dict[str, Any]:
    # aggregationTemporality=2 是 CUMULATIVE：本仓聚合值来自永久保留的小时表，
    # 进程内错误计数重启归零，语义与 Prometheus counter 一致。
    return {
        "name": name,
        "unit": unit,
        "sum": {
            "dataPoints": points,
            "aggregationTemporality": 2,
            "isMonotonic": monotonic,
        },
    }


def _histogram_point(start_ns: int, now_ns: int, attrs: list[tuple[str, Any]],
                     *, count: int, value: float) -> dict[str, Any]:
    """单桶 histogram（`[0, +inf)`）：只需要 count / sum 就够算均值。

    本仓没有请求级直方图（小时汇总只存 sum/count），强行编造多桶边界会让
    分位数看起来精确、实际是假的。显式给一个桶，读到的 count/sum 与
    Prometheus 侧的 `_sum` / `_count` 完全同源。
    """
    return {
        "attributes": [_attr(key, item) for key, item in attrs],
        **_point_times(start_ns, now_ns),
        "count": str(max(count, 0)),
        "sum": round(value, 6),
        "bucketCounts": [str(max(count, 0))],
        "explicitBounds": [],
    }


def build_otlp_payload(
    *,
    requests: list[tuple[str, str, int, int]],
    tokens: list[tuple[str, int, int, int, int]],
    latency: list[tuple[str, float, float, int]],
    errors: list[tuple[str, str, int]],
    start_time_unix_nano: int,
    time_unix_nano: int,
    service_name: str = SERVICE_NAME,
) -> dict[str, Any]:
    """把聚合数据渲染成 OTLP/HTTP JSON（纯函数，可直接单测）。

    数据源与 `/metrics` 完全同源（`usage_hourly` 的按渠道 / 按 (渠道, 模型)
    聚合 + 进程内错误计数）：两边读数不会漂移，也不需要为导出再扫一遍明细表。

    维度取舍：小时汇总只有「按渠道的延迟/token」与「按 (渠道, 模型) 的请求数」，
    没有 (渠道, 模型) 的延迟/token 列；要给 duration / token 加模型标签就得去
    明细表做分位 join（重量级且只覆盖 90 天）。这里只在 `requests` 计数器上带
    `gen_ai.request.model`，duration / token 按 `gen_ai.provider.name` 聚合。
    """
    metrics: list[dict[str, Any]] = []

    duration_points = [
        _histogram_point(
            start_time_unix_nano, time_unix_nano,
            [("gen_ai.provider.name", provider),
             ("gen_ai.operation.name", "chat")],
            count=samples,
            value=(avg_latency / 1000.0) * samples,
        )
        for provider, avg_latency, _avg_ttfb, samples in latency if samples > 0
    ]
    if duration_points:
        metrics.append(_histogram("gen_ai.client.operation.duration", "s",
                                  duration_points))

    ttft_points = [
        _histogram_point(
            start_time_unix_nano, time_unix_nano,
            [("gen_ai.provider.name", provider),
             ("gen_ai.operation.name", "chat")],
            count=samples,
            value=(avg_ttfb / 1000.0) * samples,
        )
        for provider, _avg_latency, avg_ttfb, samples in latency if samples > 0
    ]
    if ttft_points:
        metrics.append(_histogram("gen_ai.server.time_to_first_token", "s",
                                  ttft_points))

    # 每渠道请求数：既作为 token.usage 直方图的观测次数（均值 = sum/count），
    # 也让「请求量」单独可读。reasoning / cached 不在 GenAI 语义约定里，不导。
    provider_requests: dict[str, int] = {}
    request_points: list[dict[str, Any]] = []
    for provider, model, total, ok_count in requests:
        provider_requests[provider] = provider_requests.get(provider, 0) + int(total)
        ok_value = max(int(ok_count), 0)
        error_value = max(int(total) - ok_value, 0)
        for status, value in (("ok", ok_value), ("error", error_value)):
            if value <= 0:
                continue
            request_points.append({
                "attributes": [_attr("gen_ai.provider.name", provider),
                               _attr("gen_ai.request.model", model),
                               _attr("status", status)],
                **_point_times(start_time_unix_nano, time_unix_nano),
                "asInt": str(value),
            })
    if request_points:
        metrics.append(_sum_metric("coding2api.gen_ai.requests", "{request}",
                                   request_points))

    token_points: list[dict[str, Any]] = []
    for provider, input_tokens, output_tokens, _reasoning, _cached in tokens:
        # 直方图只有一个 `[0, +inf)` 桶：小时汇总只有 sum/count，编造桶边界会
        # 让分位数看着精确、实际是假的（与 `metrics.py` 拒绝直方图的理由一致）。
        count = provider_requests.get(provider, 1) or 1
        for kind, value in (("input", input_tokens), ("output", output_tokens)):
            token_points.append(_histogram_point(
                start_time_unix_nano, time_unix_nano,
                [("gen_ai.provider.name", provider),
                 ("gen_ai.token.type", kind)],
                count=count, value=float(value)))
    if token_points:
        metrics.append(_histogram("gen_ai.client.token.usage", "{token}",
                                  token_points))

    error_points = [
        {
            "attributes": [_attr("gen_ai.provider.name", provider),
                           _attr("error.type", kind)],
            **_point_times(start_time_unix_nano, time_unix_nano),
            "asInt": str(max(count, 0)),
        }
        for provider, kind, count in errors
    ]
    if error_points:
        metrics.append(_sum_metric("coding2api.gen_ai.errors", "{request}",
                                   error_points))

    return {
        "resourceMetrics": [{
            "resource": {
                "attributes": [_attr("service.name", service_name)],
            },
            "scopeMetrics": [{
                "scope": {"name": SCOPE_NAME, "version": "1"},
                "metrics": metrics,
            }],
        }],
    }


async def _post_json(url: str, payload: dict[str, Any],
                     headers: dict[str, str]) -> Any:
    """默认投递：一次性 httpx client（周期 60s，复用连接池的收益可忽略）。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response


class OtlpExporter:
    """OTLP/HTTP JSON 导出器（进程内单例，由 TaskRunner 周期调用）。

    `endpoint` 为空 = 关闭：`export_once()` 直接返回 False，**不建连接**。
    `post` 可注入（测试用替身验证「零出站 / payload 结构 / 失败不抛」）。
    """

    def __init__(
        self,
        endpoint: str = "",
        *,
        headers: str = "",
        snapshot: Callable[[], dict[str, Any]] | None = None,
        post: Callable[[str, dict[str, Any], dict[str, str]],
                       Awaitable[Any]] | None = None,
        service_name: str = SERVICE_NAME,
        now: Callable[[], int] = time.time_ns,
    ) -> None:
        problem = endpoint_problem(endpoint)
        if problem is not None:
            raise ValueError(problem)
        self._endpoint = normalize_endpoint(endpoint)
        self._headers = parse_otlp_headers(headers)
        self._snapshot = snapshot or (lambda: {})
        self._post = post or _post_json
        self._service_name = service_name
        self._now = now
        self._start_ns = now()
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self._endpoint)

    def build_payload(self) -> dict[str, Any]:
        data = self._snapshot()
        return build_otlp_payload(
            requests=list(data.get("requests") or []),
            tokens=list(data.get("tokens") or []),
            latency=list(data.get("latency") or []),
            errors=list(data.get("errors") or []),
            start_time_unix_nano=self._start_ns,
            time_unix_nano=self._now(),
            service_name=self._service_name,
        )

    async def export_once(self) -> bool:
        """导出一轮；关闭时零出站，任何失败都吞掉并只告警一次。"""
        if not self._endpoint:
            return False
        url = f"{self._endpoint}/v1/metrics"
        headers = {"Content-Type": "application/json", **self._headers}
        try:
            response = await self._post(url, self.build_payload(), headers)
            status = int(getattr(response, "status_code", 200))
            if not 200 <= status < 300:
                raise RuntimeError(f"collector answered HTTP {status}")
        except Exception as error:  # noqa: BLE001 - 后台导出不能拖垮服务
            if not self._warned:
                logger.warning("OTLP 指标导出失败（连续失败只告警一次）: %s", error)
                self._warned = True
            return False
        self._warned = False
        return True
