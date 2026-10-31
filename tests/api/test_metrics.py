"""Prometheus `/metrics` 指标出口。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.metrics import CONTENT_TYPE, _escape, _sample, render_metrics
from src.config import Settings
from src.main import build_app
from tests.helpers.auth import isolated_settings
from tests.helpers.constants import SECRET


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
    """与 /healthz 同口径：不带任何凭证也能抓取。"""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    assert "# TYPE coding2api_credentials gauge" in response.text
    # 不带 Cookie 的裸请求同样 200（`client` 夹具自带 admin Cookie，这里显式清掉）
    client.cookies.clear()
    assert client.get("/metrics").status_code == 200


def test_metrics_reflects_pool_counts_and_usage(tmp_path):
    settings = isolated_settings(tmp_path)
    app = build_app(settings)
    collector = app.state.stats_collector
    collector.record(username="root", provider="trae", model="glm-5.2", ok=True,
                     input_tokens=7, output_tokens=3)
    collector.record(username="root", provider="trae", model="glm-5.2", ok=False,
                     error_type="rate_limit")
    with TestClient(app) as http:
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
    with TestClient(app) as http:
        response = http.get("/metrics")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invalid_request"


def test_metrics_without_runner_omits_task_family(tmp_path):
    """未进 lifespan（无 runner）时不 500，任务家族留空。"""
    settings = isolated_settings(tmp_path)
    app = build_app(settings)
    http = TestClient(app)
    response = http.get("/metrics")
    assert response.status_code == 200
    assert "coding2api_task_failures_total{" not in response.text


def test_metrics_token_is_optional_and_enforced_when_set(tmp_path):
    """空令牌 = 保持公开（与 /healthz 同口径）；设了就必须带对的 Bearer。"""
    public = build_app(isolated_settings(tmp_path))
    with TestClient(public) as http:
        assert http.get("/metrics").status_code == 200

    guarded = build_app(isolated_settings(tmp_path, METRICS_TOKEN="s3cret"))
    with TestClient(guarded) as http:
        assert http.get("/metrics").status_code == 401
        assert http.get("/metrics", headers={
            "Authorization": "Bearer wrong"}).status_code == 401
        assert http.get("/metrics", headers={
            "Authorization": "Basic s3cret"}).status_code == 401
        ok = http.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert "coding2api_credentials" in ok.text


def test_metrics_token_header_is_case_insensitive_and_trimmed(tmp_path):
    """Prometheus 发 `Bearer`，但代理/客户端可能改大小写或带尾随空格。"""
    app = build_app(isolated_settings(tmp_path, METRICS_TOKEN="tok"))
    with TestClient(app) as http:
        response = http.get("/metrics", headers={
            "Authorization": "bearer   tok   "})
    assert response.status_code == 200


def test_metrics_token_disabled_still_wins(tmp_path):
    """关闭优先于鉴权：关了就是 404，不暴露「这里有东西但要令牌」。"""
    app = build_app(isolated_settings(tmp_path, METRICS_ENABLED="false",
                                      METRICS_TOKEN="tok"))
    with TestClient(app) as http:
        assert http.get("/metrics").status_code == 404


def test_metrics_enabled_is_hot_updatable(tmp_path):
    """关闭后立即 404、恢复默认后立即 200（无需重启）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    runtime = app.state.runtime_settings
    with TestClient(app) as http:
        assert http.get("/metrics").status_code == 200
        runtime.set("metrics_enabled", False)
        assert http.get("/metrics").status_code == 404
        runtime.reset("metrics_enabled")
        assert http.get("/metrics").status_code == 200
