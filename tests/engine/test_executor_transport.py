"""执行引擎：上游**传输层**异常的分类、冷却与换号。

线上逃逸的 traceback 全是 `httpx.WriteTimeout` / `httpx.ConnectError` /
`httpx.ConnectTimeout`——它们没有 `kind()`，此前 `_classify` 返回 None，
异常直接上抛到 `stream_guarded`，表现为回一帧 `internal_error`、既不换号
也不冷却、还不记账。本文件锁住修复后的口径：传输层失败 = SOFT。

`ssl.SSLError` 同属这一类但**不是** `httpx.TransportError`（anyio 只转
`SSLEOFError`），2026-10-25 15:50 经代理的 `record layer failure` 就是这样
逃出去的，故单列成组用例。
"""

from __future__ import annotations

import ssl
import time

import httpx
import pytest

from src.compat.openai.request import parse_chat_request
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import Scheduler
from src.engine.sse import SSE_DONE
from src.provider.base import ErrKind, UpstreamTransportError
from tests.helpers.events import TRAE_GOOD


class TransportProvider:
    """按调用次数回放脚本的 trae 替身，并记录每次实际用的 accessToken。

    与 `tests/helpers/providers.py` 的 `ScriptedProvider` 同构（列表元素是
    异常实例时抛出），多记一份 token 是为了断言**是哪一张凭证**被冷却。
    """

    id = "trae"

    def __init__(self, script: list) -> None:
        self.script = script
        self.calls = 0
        self.tokens: list[str] = []

    async def stream_chat(self, credential_data, payload, model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        self.tokens.append(credential_data.get("accessToken", ""))
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    async def list_models(self, _credential_data):
        return []

    def import_credential(self, raw):
        return dict(raw)

    def classify(self, status, body=b""):
        return ErrKind.OTHER


def build_executor(credentials, script, **scheduler_kwargs):
    provider = TransportProvider(script)
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=credentials,
        scheduler=Scheduler(**scheduler_kwargs), default_model="glm-5.2"))
    return executor, provider


def add_credential(credentials, token: str) -> str:
    return credentials.add(provider="trae", credential_data={"accessToken": token})


def stream_request():
    return parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True})


def complete_request():
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}]})


def cooled_ids(credentials) -> set[str]:
    now = int(time.time())
    return {c.credential_id for c in credentials.candidates()
            if c.cooling_until is not None and c.cooling_until > now}


def test_upstream_transport_error_is_soft():
    """渠道客户端包装过的传输异常自带分类，不再依赖 executor 兜底。"""
    assert UpstreamTransportError("x").kind() is ErrKind.SOFT


async def test_stream_rotates_and_cools_on_connect_error(repo):
    """首个凭证 ConnectError → 换号成功，且只有它进入 SOFT 冷却。"""
    credentials, _db = repo
    # 选号在健康度/余额都相同时按 credential_id 兜底排序，先选哪张不固定：
    # 断言按「第一次实际用的 token」反查凭证，不能假定 first 一定先被选中
    ids = {token: add_credential(credentials, token) for token in ("first", "second")}
    executor, provider = build_executor(
        credentials, [[httpx.ConnectError("connect failed")], TRAE_GOOD])

    frames = [frame async for frame in executor.stream(stream_request())]

    assert provider.calls == 2
    assert frames[-1] == SSE_DONE
    assert b'"hi"' in b"".join(frames)
    # 被冷却的只有**实际失败的那张**（第一次调用用的 token 对应的凭证）
    failed_id = ids[provider.tokens[0]]
    assert cooled_ids(credentials) == {failed_id}
    cooled = next(c for c in credentials.candidates() if c.credential_id == failed_id)
    assert cooled.err_count == 0          # SOFT 不累计错误数
    assert provider.tokens[1] != provider.tokens[0]


async def test_stream_all_write_timeout_ends_with_no_healthy_credential(repo):
    """全部凭证都写请求体超时 → 终帧是 no_healthy_credential，不是 internal_error。"""
    credentials, _db = repo
    add_credential(credentials, "a")
    add_credential(credentials, "b")
    executor, provider = build_executor(
        credentials, [[httpx.WriteTimeout("write timed out")]])

    frames = [frame async for frame in executor.stream(stream_request())]
    joined = b"".join(frames)

    assert provider.calls == 2
    assert b"no_healthy_credential" in joined
    assert b"internal_error" not in joined
    assert SSE_DONE not in frames[-1:]


async def test_complete_rotates_on_connect_timeout(repo):
    """非流式：ConnectTimeout 也走换号重试。"""
    credentials, _db = repo
    add_credential(credentials, "first")
    add_credential(credentials, "second")
    executor, provider = build_executor(
        credentials, [[httpx.ConnectTimeout("connect timed out")], TRAE_GOOD])

    result = await executor.complete(complete_request())

    assert provider.calls == 2
    assert result["choices"][0]["message"]["content"] == "hi"


async def test_complete_all_transport_errors_raise_no_healthy_credential(repo):
    """非流式：全部传输失败 → NoHealthyCredential（503），不是裸异常。"""
    credentials, _db = repo
    add_credential(credentials, "a")
    add_credential(credentials, "b")
    executor, provider = build_executor(
        credentials, [[httpx.ConnectTimeout("connect timed out")]])

    with pytest.raises(NoHealthyCredential):
        await executor.complete(complete_request())
    assert provider.calls == 2
    assert len(cooled_ids(credentials)) == 2


def _ssl_record_layer_failure() -> ssl.SSLError:
    """复刻实测异常：TLS record layer failure（anyio 原样上抛，不转 BrokenResource）。

    2026-10-25 15:50 两次 codebuddy_intl 经 7897 代理时就是这个值；它是
    `OSError` 子类但**不是** `httpx.TransportError`，此前 `_classify` 返回
    None，异常逃到 `stream_guarded` 变成一帧 `internal_error`。
    """
    return ssl.SSLError(1, "[SSL] record layer failure (_ssl.c:2580)")


def test_ssl_error_is_not_httpx_transport_error():
    """前提校验：这条异常必须**不是** TransportError，否则下面的用例测不到修复。"""
    assert not isinstance(_ssl_record_layer_failure(), httpx.TransportError)


async def test_stream_rotates_and_cools_on_ssl_error(repo):
    """TLS 中断 → 换号重试（不是 internal_error），失败的那张进 SOFT 冷却。"""
    credentials, _db = repo
    ids = {token: add_credential(credentials, token) for token in ("first", "second")}
    executor, provider = build_executor(
        credentials, [[_ssl_record_layer_failure()], TRAE_GOOD])

    frames = [frame async for frame in executor.stream(stream_request())]
    joined = b"".join(frames)

    assert provider.calls == 2
    assert frames[-1] == SSE_DONE
    assert b'"hi"' in joined
    assert b"internal_error" not in joined
    failed_id = ids[provider.tokens[0]]
    assert cooled_ids(credentials) == {failed_id}


async def test_stream_all_ssl_errors_end_with_no_healthy_credential(repo):
    """全部凭证都 TLS 中断 → no_healthy_credential 终帧，而不是 internal_error。"""
    credentials, _db = repo
    add_credential(credentials, "a")
    add_credential(credentials, "b")
    executor, provider = build_executor(
        credentials, [[_ssl_record_layer_failure()]])

    frames = [frame async for frame in executor.stream(stream_request())]
    joined = b"".join(frames)

    assert provider.calls == 2
    assert b"no_healthy_credential" in joined
    assert b"internal_error" not in joined


async def test_complete_rotates_on_ssl_error(repo):
    """非流式：TLS 中断同样走换号重试。"""
    credentials, _db = repo
    add_credential(credentials, "first")
    add_credential(credentials, "second")
    executor, provider = build_executor(
        credentials, [[_ssl_record_layer_failure()], TRAE_GOOD])

    result = await executor.complete(complete_request())

    assert provider.calls == 2
    assert result["choices"][0]["message"]["content"] == "hi"
