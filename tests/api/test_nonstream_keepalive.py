"""F5：非流式出口保活（`nonstream_keepalive_seconds`，默认 0 = 关闭）。

口径：

- **宽限期**：`grace` 秒内完成 → 普通 `JSONResponse`，状态码 / `Content-Length`
  / 字节内容与不开保活时**完全一致**，异常照常抛给异常处理器；
- **超时才提交 200**：转 chunked，先吐空白字节（RFC 8259 §2 允许 JSON 前导
  空白）再补正文；
- **默认关**：本部署 nginx `proxy_read_timeout 3600s` + frp TCP，链路上没有
  短空闲超时，实测 30 天 >60s 的成功请求 1741 个全部成功。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.responses import JSONResponse

from src.api.streaming import (
    JSON_KEEPALIVE_BYTES,
    LATE_FAILURE_PAYLOAD,
    respond_with_keepalive,
)


def _collect(response) -> list[bytes]:
    async def run():
        frames: list[bytes] = []
        async for chunk in response.body_iterator:
            frames.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        return frames

    return run()


async def test_fast_path_returns_plain_json_response():
    async def work():
        return {"ok": 1}

    response = await respond_with_keepalive(work, grace=5)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {"ok": 1}
    # 有 Content-Length：字节级与不开保活完全一致
    assert b"content-length" in b"".join(
        k.lower() + b": " + v for k, v in response.headers.raw)


async def test_fast_path_applies_convert():
    async def work():
        return {"ok": 1}

    response = await respond_with_keepalive(work, grace=5,
                                            convert=lambda d: {"wrapped": d})
    assert json.loads(bytes(response.body)) == {"wrapped": {"ok": 1}}


async def test_fast_path_releases_once():
    released: list[int] = []

    async def work():
        return {"ok": 1}

    await respond_with_keepalive(work, grace=5, release=lambda: released.append(1))
    assert released == [1]


async def test_fast_path_propagates_exception():
    """宽限期内失败：异常照常抛给上层，状态码仍能正确（400/503 不变）。"""

    async def work():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await respond_with_keepalive(work, grace=5)


async def test_fast_path_releases_on_exception():
    released: list[int] = []

    async def work():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await respond_with_keepalive(work, grace=5,
                                     release=lambda: released.append(1))
    assert released == [1]


async def test_slow_path_streams_whitespace_then_body():
    """超时后才转 chunked：先空白，再合法 JSON。"""
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {"ok": 1}

    response = await respond_with_keepalive(work, grace=0.01)
    assert not isinstance(response, JSONResponse)
    assert response.media_type == "application/json"
    assert response.headers["x-accel-buffering"] == "no"

    frames: list[bytes] = []
    async for chunk in response.body_iterator:
        frames.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        if len(frames) == 2:            # 先收到心跳，再放行上游
            gate.set()
    assert frames[0] == JSON_KEEPALIVE_BYTES
    assert frames[-1] == b'{"ok":1}'
    # 前导空白不影响解析：这是整套设计的合法性依据
    assert json.loads(b"".join(frames)) == {"ok": 1}


async def test_slow_path_applies_convert():
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {"ok": 1}

    response = await respond_with_keepalive(
        work, grace=0.01, convert=lambda d: {"wrapped": d})
    frames: list[bytes] = []
    async for chunk in response.body_iterator:
        frames.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        if len(frames) == 2:
            gate.set()
    assert json.loads(b"".join(frames)) == {"wrapped": {"ok": 1}}


async def test_slow_path_late_failure_yields_error_payload():
    """200 已提交后上游才失败：正文降级为可解析的错误体（改不回状态码）。"""
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        raise RuntimeError("late boom")

    response = await respond_with_keepalive(work, grace=0.01)
    frames: list[bytes] = []
    async for chunk in response.body_iterator:
        frames.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        if len(frames) == 2:
            gate.set()
    payload = json.loads(b"".join(frames))
    assert payload == LATE_FAILURE_PAYLOAD


async def test_slow_path_releases_when_done():
    released: list[int] = []
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {"ok": 1}

    response = await respond_with_keepalive(
        work, grace=0.01, release=lambda: released.append(1))
    assert released == []               # 流还没跑完，不能提前释放
    frames: list[bytes] = []
    async for chunk in response.body_iterator:
        frames.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        if len(frames) == 2:
            gate.set()
    assert released == [1]


async def test_slow_path_client_disconnect_cancels_work():
    """客户端断开（生成器提前关闭）：取消工作协程并释放租约，不留孤儿。"""
    cancelled: list[int] = []
    released: list[int] = []

    async def work():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        return {"ok": 1}

    response = await respond_with_keepalive(
        work, grace=0.01, release=lambda: released.append(1))
    iterator = response.body_iterator.__aiter__()
    assert await anext(iterator) == JSON_KEEPALIVE_BYTES
    await response.body_iterator.aclose()
    await asyncio.sleep(0)
    assert cancelled == [1]
    assert released == [1]


async def test_outer_cancel_cancels_work():
    """宽限期内被取消（客户端断开 / 上层取消）：工作协程一并收掉。"""
    cancelled: list[int] = []
    released: list[int] = []

    async def work():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        return {"ok": 1}

    task = asyncio.ensure_future(
        respond_with_keepalive(work, grace=10,
                               release=lambda: released.append(1)))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == [1]
    assert released == [1]


async def test_outer_cancel_without_release_is_silent():
    """宽限期内被取消且没传 release：只收掉工作协程，不额外做什么。"""
    cancelled: list[int] = []

    async def work():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        return {"ok": 1}

    task = asyncio.ensure_future(respond_with_keepalive(work, grace=10))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == [1]


async def test_slow_path_cancelled_work_still_raises(monkeypatch):
    """200 已提交后工作协程被取消：照常抛 CancelledError，不冒充上游失败。"""
    captured: dict[str, asyncio.Future] = {}
    real_ensure = asyncio.ensure_future

    def capture(coro, *args, **kwargs):
        future = real_ensure(coro, *args, **kwargs)
        captured.setdefault("task", future)
        return future

    monkeypatch.setattr(asyncio, "ensure_future", capture)
    released: list[int] = []
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {"ok": 1}

    response = await respond_with_keepalive(
        work, grace=0.01, release=lambda: released.append(1))
    iterator = response.body_iterator.__aiter__()
    assert await anext(iterator) == JSON_KEEPALIVE_BYTES
    captured["task"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await anext(iterator)
    assert released == [1]


def test_dumps_matches_json_response_rendering():
    """保活正文的序列化必须与 `JSONResponse.render` 逐字节一致。"""
    from src.api.streaming import _dumps

    payload = {"a": 1, "中文": "值", "b": [1, 2]}
    assert _dumps(payload) == JSONResponse(payload).render(payload)


def test_endpoint_uses_keepalive_when_enabled(tmp_path):
    """配置打开后非流式出口走保活包装；宽限期内完成仍返回普通 JSON。"""
    from fastapi.testclient import TestClient

    from src.config import Settings
    from src.main import build_app
    from tests.helpers.constants import SECRET
    from tests.helpers.events import TRAE_GOOD
    from tests.helpers.providers import ScriptedProvider

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", nonstream_keepalive_seconds=5)
    app = build_app(settings, providers={"trae": ScriptedProvider([TRAE_GOOD])})
    with TestClient(app) as client:
        app.state.credentials.add(provider="trae",
                                  credential_data={"accessToken": "a"})
        key = app.state.api_keys.create("root")["api_key"]
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hi"
    # 宽限期内完成：普通 JSONResponse，带 Content-Length（chunked 才没有）
    assert response.headers.get("content-length")
