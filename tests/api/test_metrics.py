"""Prometheus `/metrics` 指标出口。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.metrics import CONTENT_TYPE, _escape, _is_loopback, _sample, render_metrics
from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from tests.helpers.auth import isolated_settings
from tests.helpers.constants import SECRET

# 回环对端：无令牌时只放行本机抓取（Prometheus 与被抓服务同机）。
LOOPBACK = ("127.0.0.1", 50000)
# 非回环对端：模拟跨机 / 经公网代理进来的匿名抓取。
REMOTE = ("203.0.113.9", 50000)


@pytest.fixture()
def client(tmp_path):
    """带 admin 会话 Cookie 的 TestClient，对端为回环地址。

    覆盖全局夹具：无 `METRICS_TOKEN` 时 `/metrics` 只对回环放行，而 TestClient
    默认对端是 `testclient`（非 IP 字面量，按非回环处理）。
    """
    settings = isolated_settings(tmp_path)
    app = build_app(settings)
    with TestClient(app, client=LOOPBACK) as test_client:
        test_client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        yield test_client


def _samples(text: str) -> dict[str, str]:
    """把 text 格式解析成 {样本行: 值}（忽略 HELP/TYPE 注释行）。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            out[name] = value
    return out


def _req(provider: str, model: str, status: str) -> str:
    return (f'coding2api_requests_total{{provider="{provider}",'
            f'model="{model}",status="{status}"}}')


def test_render_metrics_emits_all_families_with_types():
    text = render_metrics(
        requests=[("trae", "glm-5.2", 3, 2)],
        tokens=[("trae", 10, 20, 30, 5)],
        credentials={"total": 2, "ready": 2, "cooling": 0, "paused": 0, "disabled": 0},
        errors=[("trae", "rate_limit", 4)],
        task_failures=[("quota_probe", 1)],
    )
    assert "# TYPE coding2api_requests_total counter" in text
    assert "# TYPE coding2api_credentials gauge" in text
    assert text.endswith("\n")
    samples = _samples(text)
    # error 由 requests - ok_count 得出；ok 单独一条
    assert samples[_req("trae", "glm-5.2", "ok")] == "2"
    assert samples[_req("trae", "glm-5.2", "error")] == "1"
    for kind, value in (("input", "10"), ("output", "20"),
                        ("reasoning", "30"), ("cached", "5")):
        assert samples[f'coding2api_tokens_total{{provider="trae",type="{kind}"}}'] == value
    # 四态都输出（含 0），与 /healthz 同口径
    for state, value in (("ready", "2"), ("cooling", "0"),
                         ("paused", "0"), ("disabled", "0")):
        assert samples[f'coding2api_credentials{{state="{state}"}}'] == value
    assert samples['coding2api_upstream_errors_total{provider="trae",kind="rate_limit"}'] == "4"
    assert samples['coding2api_task_failures_total{task="quota_probe"}'] == "1"


def test_render_metrics_error_count_never_negative():
    """ok_count 大于 requests（老库脏数据）时不输出负计数。"""
    text = render_metrics(requests=[("trae", "m", 1, 5)], tokens=[],
                          credentials={}, errors=[], task_failures=[])
    assert 'status="error"} -4' not in text
    assert _samples(text)[_req("trae", "m", "error")] == "0"


def test_render_metrics_empty_sources_still_emit_type_lines():
    text = render_metrics(requests=[], tokens=[], credentials={}, errors=[],
                          task_failures=[])
    # 数据为空也保留 HELP/TYPE（Prometheus 允许空家族，客户端不报解析错）
    assert "# TYPE coding2api_requests_total counter" in text
    assert _samples(text)['coding2api_credentials{state="ready"}'] == "0"


def test_escape_covers_backslash_quote_and_newline():
    assert _escape('a"b') == 'a\\"b'
    assert _escape("a\\b") == "a\\\\b"
    assert _escape("a\nb") == "a\\nb"


def test_sample_omits_braces_without_labels():
    assert _sample("m", [], 3) == "m 3"
    assert _sample("m", [("k", "v")], 1) == 'm{k="v"} 1'


def test_metrics_endpoint_is_unauthenticated_and_text_plain(client):
    """无令牌 + 回环对端：不带任何凭证也能抓取（同机 Prometheus 的常态）。"""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    assert "# TYPE coding2api_credentials gauge" in response.text
    # 不带 Cookie 的裸请求同样 200（`client` 夹具自带 admin Cookie，这里显式清掉）
    client.cookies.clear()
    assert client.get("/metrics").status_code == 200


def test_metrics_without_token_rejects_non_loopback_peer(tmp_path):
    """无令牌 + 非回环对端 → 401（匿名拿不到渠道/模型清单与凭证池计数）。"""
    app = build_app(isolated_settings(tmp_path))
    with TestClient(app, client=REMOTE) as http:
        response = http.get("/metrics")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert body["error"]["message"] == "metrics unauthorized"


def test_metrics_without_token_treats_unparsable_peer_as_remote(tmp_path):
    """对端地址解析不出来（TestClient 默认 `testclient`）按非回环处理。"""
    app = build_app(isolated_settings(tmp_path))
    with TestClient(app) as http:                  # 默认对端 = ("testclient", 50000)
        assert http.get("/metrics").status_code == 401


@pytest.mark.parametrize(("host", "expected"), [
    ("127.0.0.1", True),
    ("127.9.9.9", True),
    ("::1", True),
    ("203.0.113.9", False),
    ("not-an-ip", False),
])
def test_is_loopback_parses_peer_host(host, expected):
    class Client:
        pass

    peer = Client()
    peer.host = host
    request = Client()
    request.client = peer
    assert _is_loopback(request) is expected


def test_is_loopback_without_peer_is_false():
    """ASGI 允许 `request.client` 为 None（UDS / 测试替身）：拿不到对端即不放行。"""
    class Request:
        client = None

    assert _is_loopback(Request()) is False


def test_metrics_reflects_pool_counts_and_usage(tmp_path):
    settings = isolated_settings(tmp_path)
    app = build_app(settings)
    collector = app.state.stats_collector
    collector.record(username="root", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=7, output_tokens=3)
    collector.record(username="root", provider="trae", model="glm-5.2", ok=False,
                     error_type="rate_limit")
    with TestClient(app, client=LOOPBACK) as http:
        samples = _samples(http.get("/metrics").text)
    # 种子 zen / kilo 两条虚拟凭证 → ready=2
    assert samples['coding2api_credentials{state="ready"}'] == "2"
    assert samples[_req("trae", "glm-5.2", "ok")] == "1"
    assert samples[_req("trae", "glm-5.2", "error")] == "1"
    assert samples['coding2api_tokens_total{provider="trae",type="input"}'] == "7"
    assert samples['coding2api_upstream_errors_total{provider="trae",kind="rate_limit"}'] == "1"
    # 任务失败家族随已装配任务出现（lifespan 已建 runner）
    assert any(key.startswith('coding2api_task_failures_total{task="') for key in samples)


def test_metrics_disabled_returns_404(tmp_path):
    settings = isolated_settings(tmp_path, METRICS_ENABLED="false")
    app = build_app(settings)
    with TestClient(app, client=LOOPBACK) as http:
        response = http.get("/metrics")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invalid_request"


def test_metrics_without_runner_omits_task_family(tmp_path):
    """未进 lifespan（无 runner）时不 500，任务家族留空。"""
    settings = isolated_settings(tmp_path)
    app = build_app(settings)
    http = TestClient(app, client=LOOPBACK)
    response = http.get("/metrics")
    assert response.status_code == 200
    assert "coding2api_task_failures_total{" not in response.text


def test_metrics_token_is_optional_and_enforced_when_set(tmp_path):
    """空令牌时回环放行；设了令牌就必须带对的 Bearer（对端无关）。"""
    public = build_app(isolated_settings(tmp_path))
    with TestClient(public, client=LOOPBACK) as http:
        assert http.get("/metrics").status_code == 200

    guarded = build_app(isolated_settings(tmp_path, METRICS_TOKEN="s3cret"))
    with TestClient(guarded, client=LOOPBACK) as http:
        assert http.get("/metrics").status_code == 401
        assert http.get("/metrics", headers={
            "Authorization": "Bearer wrong"}).status_code == 401
        assert http.get("/metrics", headers={
            "Authorization": "Basic s3cret"}).status_code == 401
        ok = http.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert "coding2api_credentials" in ok.text

    # 有令牌时对端不影响判定：跨机带对令牌同样 200
    with TestClient(guarded, client=REMOTE) as http:
        assert http.get("/metrics", headers={
            "Authorization": "Bearer s3cret"}).status_code == 200
        assert http.get("/metrics").status_code == 401


def test_metrics_token_header_is_case_insensitive_and_trimmed(tmp_path):
    """Prometheus 发 `Bearer`，但代理/客户端可能改大小写或带尾随空格。"""
    app = build_app(isolated_settings(tmp_path, METRICS_TOKEN="tok"))
    with TestClient(app, client=LOOPBACK) as http:
        response = http.get("/metrics", headers={
            "Authorization": "bearer   tok   "})
    assert response.status_code == 200


def test_metrics_token_disabled_still_wins(tmp_path):
    """关闭优先于鉴权：关了就是 404，不暴露「这里有东西但要令牌」。"""
    app = build_app(isolated_settings(tmp_path, METRICS_ENABLED="false",
                                      METRICS_TOKEN="tok"))
    with TestClient(app, client=LOOPBACK) as http:
        assert http.get("/metrics").status_code == 404


def test_metrics_enabled_is_hot_updatable(tmp_path):
    """关闭后立即 404、恢复默认后立即 200（无需重启）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    runtime = app.state.runtime_settings
    with TestClient(app, client=LOOPBACK) as http:
        assert http.get("/metrics").status_code == 200
        runtime.set("metrics_enabled", False)
        assert http.get("/metrics").status_code == 404
        runtime.reset("metrics_enabled")
        assert http.get("/metrics").status_code == 200


def test_render_metrics_latency_and_inflight_families():
    """延迟/在途两个后加家族：缺省不输出，给了才输出（旧调用方逐字节不变）。"""
    base = render_metrics(requests=[], tokens=[], credentials={}, errors=[],
                          task_failures=[])
    assert "coding2api_latency_ms_avg" not in base
    assert "coding2api_inflight" not in base

    text = render_metrics(requests=[], tokens=[], credentials={}, errors=[],
                          task_failures=[],
                          latency=[("trae", 1234.567, 456.78, 9)],
                          inflight=3)
    samples = _samples(text)
    assert samples['coding2api_latency_ms_avg{provider="trae"}'] == "1234.6"
    assert samples['coding2api_ttfb_ms_avg{provider="trae"}'] == "456.8"
    assert samples['coding2api_latency_samples{provider="trae"}'] == "9"
    assert samples["coding2api_inflight"] == "3"
    assert "# TYPE coding2api_inflight gauge" in text


def test_metrics_latency_skips_provider_without_ok_samples(tmp_path):
    """没有成功样本的渠道不输出平均值——报 0 会被读成「延迟为零」。"""
    settings = isolated_settings(tmp_path)
    app = build_app(settings)
    collector = app.state.stats_collector
    collector.record(username="root", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=7, output_tokens=3, latency_ms=900, ttfb_ms=300)
    collector.record(username="root", provider="kilo", model="glm-5.2", ok=False,
                     error_type="rate_limit")
    with TestClient(app, client=LOOPBACK) as client:
        text = client.get("/metrics").text
    samples = _samples(text)
    assert 'coding2api_latency_ms_avg{provider="trae"}' in samples
    assert 'coding2api_latency_ms_avg{provider="kilo"}' not in samples
    assert samples["coding2api_inflight"] == "0"
