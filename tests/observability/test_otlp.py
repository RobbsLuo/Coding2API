"""OTLP/HTTP JSON 指标出口（`src/observability/otlp.py`）。

覆盖四条硬约束：
1. 关 → 零出站（连 post 都不调）；
2. 开 → payload 结构符合 OTLP JSON，且**不含任何内容字段**；
3. 头部解析 `k:v;...`；
4. SSRF 校验各分支 + 导出失败不抛。
"""

from __future__ import annotations

import copy
import json

import pytest

from src.observability.otlp import (
    MIN_EXPORT_SECONDS,
    SCOPE_NAME,
    SERVICE_NAME,
    OtlpExporter,
    build_otlp_payload,
    endpoint_problem,
    metrics_snapshot,
    normalize_endpoint,
    parse_otlp_headers,
)

START = 1_700_000_000_000_000_000
NOW = START + 60_000_000_000

#: 明确禁止出现在任何 payload 里的内容字段（本仓不存提示词，导出即泄露）。
FORBIDDEN = ("gen_ai.input.messages", "gen_ai.output.messages",
             "gen_ai.system_instructions", "gen_ai.prompt", "gen_ai.completion")


def _payload(**overrides):
    base = {
        "requests": [("trae", "glm-5.2", 10, 8)],
        "tokens": [("trae", 1000, 500, 100, 200)],
        "latency": [("trae", 1200.0, 300.0, 8)],
        "errors": [("trae", "rate_limit", 2)],
        "start_time_unix_nano": START,
        "time_unix_nano": NOW,
    }
    base.update(overrides)
    return build_otlp_payload(**base)


def _metrics(payload: dict) -> dict[str, dict]:
    return {
        metric["name"]: metric
        for metric in payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    }


def _serialized(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ------------------------------------------------------------------ 结构

def test_payload_matches_otlp_json_shape():
    payload = _payload()
    resource_metrics = payload["resourceMetrics"]
    assert len(resource_metrics) == 1
    resource = resource_metrics[0]["resource"]["attributes"]
    assert {"key": "service.name", "value": {"stringValue": SERVICE_NAME}} in resource
    scope_metrics = resource_metrics[0]["scopeMetrics"]
    assert scope_metrics[0]["scope"]["name"] == SCOPE_NAME
    metrics = _metrics(payload)
    assert set(metrics) == {
        "gen_ai.client.operation.duration",
        "gen_ai.server.time_to_first_token",
        "gen_ai.client.token.usage",
        "coding2api.gen_ai.requests",
        "coding2api.gen_ai.errors",
    }


def test_duration_and_ttfb_are_histograms_in_seconds():
    metrics = _metrics(_payload())
    duration = metrics["gen_ai.client.operation.duration"]
    assert duration["unit"] == "s"
    point = duration["histogram"]["dataPoints"][0]
    # 8 个成功样本 × 1200ms = 9.6s 总时长（OTLP 直方图是 sum + count）
    assert point["count"] == "8"
    assert point["sum"] == pytest.approx(9.6)
    assert point["bucketCounts"] == ["8"]
    assert point["explicitBounds"] == []
    attrs = {item["key"]: item["value"]["stringValue"] for item in point["attributes"]}
    assert attrs["gen_ai.provider.name"] == "trae"
    assert attrs["gen_ai.operation.name"] == "chat"

    ttfb = metrics["gen_ai.server.time_to_first_token"]["histogram"]["dataPoints"][0]
    assert ttfb["sum"] == pytest.approx(2.4)


def test_token_usage_histogram_uses_request_count_as_observations():
    metrics = _metrics(_payload())
    points = metrics["gen_ai.client.token.usage"]["histogram"]["dataPoints"]
    by_kind = {
        next(item["value"]["stringValue"] for item in point["attributes"]
             if item["key"] == "gen_ai.token.type"): point for point in points
    }
    # 输入 / 输出各一条，观测次数 = 该渠道请求数（均值 = sum / count）
    assert by_kind["input"]["count"] == "10"
    assert by_kind["input"]["sum"] == pytest.approx(1000.0)
    assert by_kind["output"]["sum"] == pytest.approx(500.0)
    # 不回退成 1（那会把「每请求 1000 token」读成「单次 100 token」）
    assert by_kind["output"]["count"] == "10"


def test_requests_sum_splits_ok_and_error_without_zero_samples():
    metrics = _metrics(_payload())
    points = metrics["coding2api.gen_ai.requests"]["sum"]["dataPoints"]
    statuses = {
        next(item["value"]["stringValue"] for item in point["attributes"]
             if item["key"] == "status"): point["asInt"] for point in points
    }
    assert statuses == {"ok": "8", "error": "2"}
    assert metrics["coding2api.gen_ai.requests"]["sum"]["isMonotonic"] is True
    assert metrics["coding2api.gen_ai.requests"]["sum"]["aggregationTemporality"] == 2

    # 全成功时**不**输出 error 样本（避免凭空造 0 值序列）
    only_ok = _metrics(_payload(requests=[("trae", "m", 5, 5)]))
    ok_points = only_ok["coding2api.gen_ai.requests"]["sum"]["dataPoints"]
    assert len(ok_points) == 1


def test_errors_counter_carries_controlled_error_type():
    metrics = _metrics(_payload())
    point = metrics["coding2api.gen_ai.errors"]["sum"]["dataPoints"][0]
    attrs = {item["key"]: item["value"]["stringValue"] for item in point["attributes"]}
    assert attrs == {"gen_ai.provider.name": "trae", "error.type": "rate_limit"}
    assert point["asInt"] == "2"


def test_families_are_omitted_when_sources_are_empty():
    """没有样本的家族整块不输出（不造空直方图）。"""
    metrics = _metrics(_payload(requests=[], tokens=[], latency=[], errors=[]))
    assert metrics == {}


def test_latency_skips_channels_without_success_samples():
    metrics = _metrics(_payload(latency=[("trae", 0.0, 0.0, 0)]))
    assert "gen_ai.client.operation.duration" not in metrics
    assert "gen_ai.server.time_to_first_token" not in metrics


def test_negative_counts_are_clamped():
    metrics = _metrics(_payload(requests=[("trae", "m", 3, 5)],
                               errors=[("trae", "rate_limit", -1)]))
    ok_points = metrics["coding2api.gen_ai.requests"]["sum"]["dataPoints"]
    assert [point["asInt"] for point in ok_points] == ["5"]
    assert metrics["coding2api.gen_ai.errors"]["sum"]["dataPoints"][0]["asInt"] == "0"


# ------------------------------------------------- 硬边界：绝不导出内容字段

def test_payload_never_contains_prompt_or_completion_content():
    """`gen_ai.*messages` / `system_instructions` 一个都不许出现。

    本仓不存提示词（PROPOSAL §6），这是导出层的硬边界：即使将来有人把
    消息正文塞进快照，这里也必须失败。断言覆盖序列化后的整份文本，而不是
    逐个已知字段——避免「新增字段时忘了这条约束」。
    """
    text = _serialized(_payload())
    for key in FORBIDDEN:
        assert key not in text, f"OTLP payload 泄露了内容字段 {key}"
    # 更狠一道：连 "messages" / "instructions" 这种子串都不该有
    lowered = text.lower()
    assert "messages" not in lowered
    assert "instructions" not in lowered


def test_exporter_payload_does_not_forward_unknown_snapshot_keys():
    """快照里混入 messages / instructions 也不会带到出口（白名单取字段）。"""
    contaminated = {
        "requests": [("trae", "m", 1, 1)],
        "tokens": [], "latency": [], "errors": [],
        "gen_ai.input.messages": [{"role": "user", "content": "secret"}],
        "system_instructions": "you are a helpful assistant",
    }
    exporter = OtlpExporter("", snapshot=lambda: contaminated)
    text = _serialized(exporter.build_payload())
    assert "secret" not in text
    assert "helpful assistant" not in text
    assert "messages" not in text.lower()


# --------------------------------------------------------------- SSRF 校验

@pytest.mark.parametrize("endpoint", [
    "",
    "   ",
    "http://127.0.0.1:4318",
    "http://localhost:4318",
    "http://[::1]:4318",
    "http://127.0.0.2:4318",          # 回环段里的其它地址
    "https://collector.example.com",
])
def test_endpoint_problem_accepts_legal_endpoints(endpoint):
    assert endpoint_problem(endpoint) is None


@pytest.mark.parametrize(("endpoint", "fragment"), [
    ("ftp://collector.example.com", "只接受 http/https"),
    ("file:///etc/passwd", "只接受 http/https"),
    ("collector.example.com:4318", "只接受 http/https"),
    ("https://", "缺少主机名"),
    ("http://", "缺少主机名"),
    ("http://user:pw@collector.example.com", "userinfo"),
    ("https://user@collector.example.com", "userinfo"),
    ("http://collector.example.com:4318", "必须使用 https"),
    ("http://10.0.0.5:4318", "必须使用 https"),
])
def test_endpoint_problem_rejects_dangerous_shapes(endpoint, fragment):
    problem = endpoint_problem(endpoint)
    assert problem is not None
    assert fragment in problem


def test_normalize_endpoint_trims_slashes_and_space():
    assert normalize_endpoint(" https://c.example.com/ ") == "https://c.example.com"
    assert normalize_endpoint("http://127.0.0.1:4318///") == "http://127.0.0.1:4318"
    assert normalize_endpoint("") == ""


def test_exporter_construction_rejects_bad_endpoint():
    with pytest.raises(ValueError, match="必须使用 https"):
        OtlpExporter("http://collector.example.com")
    with pytest.raises(ValueError, match="只接受 http/https"):
        OtlpExporter("file:///tmp/x")


def test_min_export_seconds_matches_config_floor():
    """常量与 `Settings` 校验的下限同源（改一处不改另一处会漂移）。"""
    assert MIN_EXPORT_SECONDS == 15


# -------------------------------------------------------------- 头部解析

@pytest.mark.parametrize(("raw", "expected"), [
    ("", {}),
    ("   ", {}),
    ("Authorization: Bearer abc", {"Authorization": "Bearer abc"}),
    # 值里含冒号：按**第一个**冒号切分
    ("X-Api-Key:a:b;X-Env: prod ", {"X-Api-Key": "a:b", "X-Env": "prod"}),
    # 空段 / 缺冒号 / 空键：skip 而不是抛
    (";X-Env:prod;;junk;:bad;Y:1", {"X-Env": "prod", "Y": "1"}),
    # 头注入：换行剥掉
    ("X-Test: a\r\nInjected: 1", {"X-Test": "aInjected: 1"}),
])
def test_parse_otlp_headers(raw, expected):
    assert parse_otlp_headers(raw) == expected


# -------------------------------------------------------------- 导出行为

class _Recorder:
    """记录每次投递的 (url, payload, headers)，可脚本化返回 / 抛错。"""

    def __init__(self, *, status: int = 200, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self.status = status
        self.error = error
        self.status_code = status

    async def __call__(self, url, payload, headers):
        self.calls.append((url, copy.deepcopy(payload), dict(headers)))
        if self.error is not None:
            raise self.error
        return self


def _exporter(*, endpoint="http://127.0.0.1:4318", headers="", recorder=None):
    recorder = recorder or _Recorder()
    exporter = OtlpExporter(endpoint, headers=headers,
                            snapshot=lambda: {"requests": [("trae", "m", 1, 1)]},
                            post=recorder, now=lambda: NOW)
    return exporter, recorder


@pytest.mark.asyncio
async def test_disabled_exporter_makes_zero_outbound_calls():
    """端点为空 = 关闭：`export_once` 不建连接、返回 False。"""
    exporter, recorder = _exporter(endpoint="")
    assert exporter.enabled is False
    assert await exporter.export_once() is False
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_enabled_exporter_posts_otlp_json_to_v1_metrics():
    exporter, recorder = _exporter(headers="Authorization: Bearer tok")
    assert exporter.enabled is True
    assert await exporter.export_once() is True
    url, payload, headers = recorder.calls[0]
    assert url == "http://127.0.0.1:4318/v1/metrics"
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == "Bearer tok"
    # payload 必须是可 JSON 序列化的 OTLP 结构（不是 protobuf）
    assert "resourceMetrics" in payload
    json.dumps(payload)


@pytest.mark.asyncio
async def test_export_failure_is_swallowed_and_warns_once(caplog):
    """连续失败只告警一次；恢复成功后重新武装。"""
    recorder = _Recorder(error=RuntimeError("collector down"))
    exporter, _ = _exporter(recorder=recorder)
    with caplog.at_level("WARNING"):
        assert await exporter.export_once() is False
        assert await exporter.export_once() is False
        assert await exporter.export_once() is False
    warnings = [line for line in caplog.text.splitlines() if "OTLP 指标导出失败" in line]
    assert len(warnings) == 1

    recorder.error = None
    assert await exporter.export_once() is True


@pytest.mark.asyncio
async def test_non_2xx_answer_counts_as_failure(caplog):
    exporter, _ = _exporter(recorder=_Recorder(status=503))
    with caplog.at_level("WARNING"):
        assert await exporter.export_once() is False
    assert "HTTP 503" in caplog.text


@pytest.mark.asyncio
async def test_warn_state_rearms_after_success_then_new_failure(caplog):
    """成功一次后再次失败要重新告警（不是永久静音）。"""
    recorder = _Recorder(error=RuntimeError("down"))
    exporter, _ = _exporter(recorder=recorder)
    with caplog.at_level("WARNING"):
        assert await exporter.export_once() is False
        recorder.error = None
        assert await exporter.export_once() is True
        recorder.error = RuntimeError("down again")
        assert await exporter.export_once() is False
    assert caplog.text.count("OTLP 指标导出失败") == 2


@pytest.mark.asyncio
async def test_default_transport_posts_with_httpx(monkeypatch):
    """未注入 `post` 时走 httpx（不引 SDK）。用 MockTransport 接住请求。"""
    import httpx

    from src.observability import otlp as module

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    exporter = OtlpExporter("http://127.0.0.1:4318",
                            snapshot=lambda: {"requests": [("trae", "m", 1, 1)]},
                            now=lambda: NOW)
    assert await exporter.export_once() is True
    assert str(seen[0].url) == "http://127.0.0.1:4318/v1/metrics"
    assert json.loads(seen[0].content)["resourceMetrics"]


def test_endpoint_problem_handles_malformed_url():
    """未闭合 IPv6 等畸形 URL：urlsplit 抛 ValueError → 返回问题而不是冒泡。"""
    problem = endpoint_problem("http://[::1")
    assert problem is not None
    assert "合法的 URL" in problem


def test_metrics_snapshot_reads_all_four_sources():
    """导出快照与 `/metrics` 同源：四个键各取一条聚合，缺一个就会静默少一类指标。"""
    calls: list[str] = []

    class _Query:
        def metrics_provider_model(self):
            calls.append("model")
            return [("trae", "m", 1, 1)]

        def metrics_provider_tokens(self):
            calls.append("tokens")
            return [("trae", 10, 20)]

        def metrics_provider_latency(self):
            calls.append("latency")
            return [("trae", 1.5, 0.2, 3)]

    class _Collector:
        def error_totals(self):
            calls.append("errors")
            return {"trae": {"rate_limit": 2}}

    snapshot = metrics_snapshot(_Query(), _Collector())
    assert set(snapshot) == {"requests", "tokens", "latency", "errors"}
    assert calls == ["model", "tokens", "latency", "errors"]
