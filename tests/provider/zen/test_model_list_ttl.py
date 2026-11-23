"""模型列表缓存周期（P2-18）：provider 侧恒短于服务层 TTL。

历史问题：服务层 `MODEL_LIST_TTL_SECONDS` = 300s，而 CodeBuddy / Qoder 内部
各自还缓存 600s、Zen 缓存 1800s —— 服务层「到期重拉」拿到的仍是 provider
的旧缓存，实际刷新周期被拖长且与服务层口径倒挂。

收敛后的三层周期：

* 服务层 300s：决定「多久向上游要一次新列表」（唯一决定刷新节奏的一层）。
* CB / Qoder / Zen 清单缓存 60s：只挡并发与紧邻调用，恒 < 300s。
* Zen 探活 1800s：刻意比服务层长一档（探活是最贵的一步），**只给探活用**。
"""

from __future__ import annotations

import httpx
import pytest

from src.api.models import MODEL_LIST_TTL_SECONDS
from src.provider.codebuddy import client as cb_client
from src.provider.codebuddy.client import MODEL_CACHE_TTL_SECONDS as CB_TTL
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.qoder import client as qoder_client
from src.provider.qoder.client import MODEL_CACHE_TTL_SECONDS as QODER_TTL
from src.provider.zen import client as zen_client
from src.provider.zen.client import (
    EP_CHAT,
    EP_MODELS,
    MODEL_LIST_DEDUPE_SECONDS,
    PROBE_CACHE_TTL_SECONDS,
)
from tests.provider.qoder._support import cred
from tests.provider.zen._support import client as zen_http_client

# zen 的 `fetch_models` 由 conftest 的 autouse 夹具整体替换掉（避免启动预热真连
# 外网），要测它自己的缓存周期必须声明豁免；本文件全程用 MockTransport，不出网。
pytestmark = pytest.mark.real_upstream("zen")


class _FakeClock:
    """可推进的假时钟（替换模块里的 `time` 函数）。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------- 周期断言


def test_provider_ttls_are_within_service_layer_ttl():
    """CB / Qoder / Zen 清单缓存都不得长于服务层 TTL（倒挂即 P2-18 复现）。"""
    assert 0 < CB_TTL <= MODEL_LIST_TTL_SECONDS
    assert 0 < QODER_TTL <= MODEL_LIST_TTL_SECONDS
    assert 0 < MODEL_LIST_DEDUPE_SECONDS <= MODEL_LIST_TTL_SECONDS


def test_zen_probe_ttl_stays_longer_than_service_layer():
    """探活刻意比服务层长一档（最贵的一步），这条设计必须保留。"""
    assert PROBE_CACHE_TTL_SECONDS > MODEL_LIST_TTL_SECONDS


def test_zen_client_defaults_use_the_converged_ttls():
    """默认构造参数就是收敛后的周期（不只常量对，实际字段也得对）。"""
    client = zen_client.ZenClient(host="https://zen.test")
    assert client.models_cache_ttl == MODEL_LIST_DEDUPE_SECONDS
    assert client.probe_cache_ttl == PROBE_CACHE_TTL_SECONDS


# ------------------------------------------------- CodeBuddy：真实刷新周期


async def test_codebuddy_refetches_within_service_layer_ttl(monkeypatch):
    """CB 缓存 60s：服务层 300s 到期再拉时一定拿得到新数据。"""
    clock = _FakeClock()
    monkeypatch.setattr(cb_client.time, "time", lambda: clock.now)

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": 0, "data": {"models": [
            {"id": f"glm-5.{calls['n']}"}]}})

    client = cb_client.CodeBuddyClient(
        endpoint="https://cb.test",
        short_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    credential = CodeBuddyCredential(bearer_token="t")

    assert [m.id for m in await client.fetch_models(credential)] == ["glm-5.1"]

    # 紧邻调用仍去重（不重复打上游）
    await client.fetch_models(credential)
    assert calls["n"] == 1

    # 推进到服务层 TTL 之后再拉：必须真的重新请求上游
    clock.advance(MODEL_LIST_TTL_SECONDS)
    assert [m.id for m in await client.fetch_models(credential)] == ["glm-5.2"]
    assert calls["n"] == 2
    await client.aclose()


# ----------------------------------------------------- Qoder：真实刷新周期


async def test_qoder_refetches_within_service_layer_ttl(monkeypatch):
    """Qoder 缓存 60s：服务层 300s 到期再拉时同样拿得到新清单。"""
    clock = _FakeClock()
    monkeypatch.setattr(qoder_client.time, "time", lambda: clock.now)

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"chat": [{"key": f"m{calls['n']}"}]})

    client = qoder_client.QoderClient(
        host="https://openapi.test", gateway="https://gateway.test",
        short_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    assert [m.id for m in await client.fetch_models(cred())] == ["m1"]

    await client.fetch_models(cred())
    assert calls["n"] == 1                   # 缓存去重

    clock.advance(MODEL_LIST_TTL_SECONDS)
    assert [m.id for m in await client.fetch_models(cred())] == ["m2"]
    assert calls["n"] == 2
    await client.aclose()


# ------------------------------------------------ Zen：探活与清单分别计时


async def test_zen_list_refresh_does_not_forget_probe_cache(monkeypatch):
    """清单 60s 到期重拉，但判活结论仍在 1800s 内复用（保留刻意设计）。"""
    clock = _FakeClock()
    monkeypatch.setattr(zen_client.time, "monotonic", lambda: clock.now)

    list_calls = {"n": 0}
    probe_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_MODELS:
            list_calls["n"] += 1
            return httpx.Response(200, json={"data": [{"id": "a-free"}]})
        assert request.url.path == EP_CHAT
        probe_calls["n"] += 1
        return httpx.Response(200, text="")

    client = zen_http_client(handler, host="https://zen.test")

    await client.fetch_models()
    assert (list_calls["n"], probe_calls["n"]) == (1, 1)

    # 越过清单 TTL、远未到探活 TTL
    clock.advance(MODEL_LIST_DEDUPE_SECONDS + 1)
    await client.fetch_models()
    assert list_calls["n"] == 2              # 清单按短周期重拉
    assert probe_calls["n"] == 1             # 探活仍在长周期内

    # 越过探活 TTL 才重探
    clock.advance(PROBE_CACHE_TTL_SECONDS)
    await client.fetch_models()
    assert probe_calls["n"] == 2
    await client.aclose()
