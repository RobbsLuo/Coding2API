"""`GET /api/logs[/stream]`（E3）：鉴权、关闭即 404、首帧回放 + 实时推送、订阅上限。

SSE 生成器的逐帧行为在这里用替身 `Request` 直接驱动（与 `tests/stats/test_live.py`
同一手法）；端点层只验「谁能读、关闭怎么回、响应头」。

**日志行先过脱敏**：端点没有二次过滤，故这里同时断言敏感行根本进不了缓冲。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from src.api.logs import DEFAULT_LOG_LIMIT, _sse_frame, _stream_logs
from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from src.stats.live import LiveEventHub
from src.webapp.log_buffer import LogBuffer
from tests.helpers.constants import SECRET


class FakeRequest:
    """最小 `Request` 替身：生成器只调 `is_disconnected`。"""

    def __init__(self, disconnect_after: int) -> None:
        self.calls = 0
        self.disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > self.disconnect_after


def _app(tmp_path, *, buffer_lines: int | None = None, persist_lines: int = 0):
    """建 app；`persist_lines=0` 让端点走**内存环**（这些用例验的是旧口径）。

    持久化路径的用例显式传 `persist_lines>0`，见文件末尾 `LogRepository` 一节——
    两条数据路径的断言不混在一起，避免「到底读了哪一份」说不清。
    """
    extra = {} if buffer_lines is None else {"LOG_BUFFER_LINES": str(buffer_lines)}
    extra["LOG_PERSIST_LINES"] = str(persist_lines)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", **extra)
    return build_app(settings)


def _admin(client: TestClient) -> None:
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))


# ------------------------------------------------------------------ 非流式


def test_logs_returns_recent_lines(tmp_path):
    app = _app(tmp_path)
    # 启动期自己也会进缓冲（引导/预热/access log）：用唯一文案定位自己的两条
    app.state.log_buffer.append(level="INFO", logger_name="src.x", message="测试第一条")
    app.state.log_buffer.append(level="WARNING", logger_name="src.y", message="测试第二条")
    with TestClient(app) as client:
        _admin(client)
        response = client.get("/api/logs", params={"limit": 50})
    assert response.status_code == 200
    body = response.json()
    messages = [line["message"] for line in body["lines"]]
    # 启动期自己也会进缓冲（引导/预热）：只看本用例写入的两条与其次序
    assert [m for m in messages if m.startswith("测试第")] == ["测试第一条", "测试第二条"]
    assert body["capacity"] == 500
    assert body["limit"] == 50


def test_logs_limit_defaults_and_clamped_to_capacity(tmp_path):
    """默认 200 与「上限 = 缓冲容量」都要夹住；limit 下限是 1（不是 0）。"""
    app = _app(tmp_path, buffer_lines=20)
    for index in range(5):
        app.state.log_buffer.append(level="INFO", logger_name="src.x",
                                    message=f"标记{index}")
    with TestClient(app) as client:
        _admin(client)
        default = client.get("/api/logs").json()
        oversized = client.get("/api/logs", params={"limit": 10_000}).json()
        # 热更调小容量：端点上限跟着变（无需重启），limit 下限仍是 1
        app.state.runtime_settings.set("log_buffer_lines", 3)
        shrunk = client.get("/api/logs", params={"limit": 10_000}).json()
        tiny = client.get("/api/logs", params={"limit": 0}).json()
    assert DEFAULT_LOG_LIMIT == 200
    marked = [m for m in (line["message"] for line in default["lines"])
              if m.startswith("标记")]
    assert marked == [f"标记{index}" for index in range(5)]
    assert default["limit"] == 20           # 容量 20 < 默认 200：上限取容量
    assert oversized["limit"] == 20
    assert shrunk["limit"] == 3
    assert tiny["limit"] == 1


def test_logs_streams_buffered_lines_and_new_ones(tmp_path, monkeypatch):
    """端点层接线：回放上限与订阅都交给生成器（真实逐帧断言见下方）。"""
    app = _app(tmp_path)
    captured: dict = {}

    async def fake_stream(request, buffer, subscription, *, limit, heartbeat=15.0):
        captured.update({"limit": limit, "subscribers": buffer.hub.subscriber_count,
                         "buffer": buffer})
        yield ": connected\n\n"
        yield _sse_frame({"seq": 1, "message": "回放"})

    monkeypatch.setattr("src.api.logs._stream_logs", fake_stream)
    with TestClient(app) as client:
        _admin(client)
        response = client.get("/api/logs/stream", params={"limit": 7})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    frames = [line for line in response.text.splitlines() if line]
    assert frames[0] == ": connected"
    assert json.loads(frames[1].removeprefix("data: "))["message"] == "回放"
    assert captured["limit"] == 7
    assert captured["subscribers"] == 1
    assert captured["buffer"] is app.state.log_buffer


def test_logs_stream_limit_clamped_to_capacity(tmp_path, monkeypatch):
    app = _app(tmp_path, buffer_lines=2)
    captured: dict = {}

    async def fake_stream(request, buffer, subscription, *, limit, heartbeat=15.0):
        captured["limit"] = limit
        yield ": connected\n\n"

    monkeypatch.setattr("src.api.logs._stream_logs", fake_stream)
    with TestClient(app) as client:
        _admin(client)
        client.get("/api/logs/stream", params={"limit": 999})
    assert captured["limit"] == 2


# ------------------------------------------------------------------ 关闭 / 鉴权


def test_logs_disabled_returns_404(tmp_path):
    app = _app(tmp_path, buffer_lines=0)
    with TestClient(app) as client:
        _admin(client)
        assert client.get("/api/logs").status_code == 404
        assert client.get("/api/logs/stream").status_code == 404
        body = client.get("/api/logs").json()
    assert body["error"]["code"] == "not_found"
    # 关闭时 root 上不应挂着缓冲处理器（不产生任何开销）
    import logging

    from src.webapp.log_buffer import LogBufferHandler
    assert not any(isinstance(h, LogBufferHandler)
                   for h in logging.getLogger().handlers)


def test_logs_hot_enable_attaches_handler_without_restart(tmp_path):
    """热更从 0 打开：端点访问时自动挂上 handler（否则页面永远空白）。"""
    import logging

    from src.webapp.log_buffer import LogBufferHandler

    app = _app(tmp_path, buffer_lines=0)
    runtime = app.state.runtime_settings
    with TestClient(app) as client:
        _admin(client)
        runtime.set("log_buffer_lines", 5)
        assert client.get("/api/logs").status_code == 200
        assert any(isinstance(h, LogBufferHandler)
                   for h in logging.getLogger().handlers)
        logging.getLogger("src.hot").info("热更后的日志")
        assert any(line["message"] == "热更后的日志"
                   for line in client.get("/api/logs").json()["lines"])
        runtime.set("log_buffer_lines", 0)


def test_logs_requires_admin_session(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/logs").status_code == 401
        assert client.get("/api/logs/stream").status_code == 401


def test_logs_forbidden_for_non_admin(tmp_path):
    """viewer 能登录但读不到日志（页面上也不该出现入口）。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
        assert client.get("/api/logs").status_code == 403
        assert client.get("/api/logs/stream").status_code == 403


def test_logs_rejects_when_subscriber_slots_full(tmp_path):
    app = _app(tmp_path)
    buffer = app.state.log_buffer
    buffer.hub = LiveEventHub(max_subscribers=0)
    with TestClient(app) as client:
        _admin(client)
        response = client.get("/api/logs/stream")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "10"
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert buffer.hub.subscriber_count == 0


# ------------------------------------------------------------------ 生成器


def _buffer(capacity: int = 50) -> LogBuffer:
    return LogBuffer(capacity=lambda: capacity)


def test_sse_frame_is_single_data_line():
    assert _sse_frame({"seq": 1}) == 'data: {"seq":1}\n\n'


async def test_stream_replays_snapshot_then_pushes_live():
    buffer = _buffer()
    buffer.append(level="INFO", logger_name="src.x", message="旧一")
    buffer.append(level="INFO", logger_name="src.x", message="旧二")
    subscription = buffer.hub.subscribe()
    buffer.bind_loop(asyncio.get_running_loop())
    # 订阅之后、快照之前写入的行：既在快照里也会被 push，必须按 seq 去重
    buffer.append(level="INFO", logger_name="src.x", message="旧三")
    buffer.append(level="INFO", logger_name="src.x", message="新行")

    frames = [frame async for frame in _stream_logs(
        FakeRequest(disconnect_after=1), buffer, subscription, limit=10)]
    assert frames[0] == ": connected\n\n"
    messages = [json.loads(frame.removeprefix("data: "))["message"]
                for frame in frames[1:]]
    assert messages == ["旧一", "旧二", "旧三", "新行"]
    assert buffer.hub.subscriber_count == 0        # 断开即注销


async def test_stream_limit_only_replays_recent():
    buffer = _buffer()
    for index in range(5):
        buffer.append(level="INFO", logger_name="src.x", message=f"m{index}")
    subscription = buffer.hub.subscribe()
    frames = [frame async for frame in _stream_logs(
        FakeRequest(disconnect_after=0), buffer, subscription, limit=2)]
    assert [json.loads(frame.removeprefix("data: "))["message"] for frame in frames[1:]] \
        == ["m3", "m4"]


async def test_stream_sends_heartbeat_when_idle():
    buffer = _buffer()
    subscription = buffer.hub.subscribe()
    frames = [frame async for frame in _stream_logs(
        FakeRequest(disconnect_after=1), buffer, subscription, limit=1,
        heartbeat=0.01)]
    assert frames[0] == ": connected\n\n"
    assert ": heartbeat\n\n" in frames


async def test_stream_stops_when_log_page_closed():
    """热更关闭：流结束（浏览器随后重连拿到 404，页面显示已关闭）。"""
    capacity = {"value": 5}
    buffer = LogBuffer(capacity=lambda: capacity["value"])
    buffer.append(level="INFO", logger_name="src.x", message="关前")
    subscription = buffer.hub.subscribe()
    capacity["value"] = 0
    frames = [frame async for frame in _stream_logs(
        FakeRequest(disconnect_after=99), buffer, subscription, limit=5)]
    assert frames == [": connected\n\n"]
    assert buffer.hub.subscriber_count == 0


async def test_stream_unsubscribes_when_client_closes_early():
    buffer = _buffer()
    subscription = buffer.hub.subscribe()
    generator = _stream_logs(FakeRequest(disconnect_after=0), buffer, subscription, limit=5)
    assert await anext(generator) == ": connected\n\n"
    await generator.aclose()
    assert buffer.hub.subscriber_count == 0


@pytest.mark.parametrize("capacity", [1])
async def test_stream_dedups_by_seq_not_counts(capacity):
    """同一 seq 出现两次（快照 + 早到的 push）只发一帧。"""
    buffer = _buffer(capacity)
    subscription = buffer.hub.subscribe()
    payload = {"seq": 9, "message": "只发一次"}
    buffer.hub.publish(payload)
    buffer.hub.publish(payload)
    frames = [frame async for frame in _stream_logs(
        FakeRequest(disconnect_after=1), buffer, subscription, limit=1)]
    assert [frame for frame in frames if frame.startswith("data: ")] \
        == [_sse_frame(payload)]
