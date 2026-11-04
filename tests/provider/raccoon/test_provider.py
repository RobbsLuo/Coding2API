"""小浣熊 Provider：导入、额度、目录缓存、未知模型防护、节流。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from src.provider.base import ErrKind, Event, EventKind, Model, Quota
from src.provider.raccoon.client import (
    RaccoonProvider,
    UpstreamHTTPError,
    UpstreamProtocolViolation,
)


class FakeClient:
    """最小替身：只实现 provider 用到的三个方法（本用例不覆盖未调用项）。"""

    def __init__(self, *, models: list[Model] | None = None,
                 quota: Quota | None = None, events: list[Event] | None = None,
                 error: Exception | None = None) -> None:
        self.models = models or []
        self.quota = quota or Quota(remaining=1, total=1)
        self.events = events or []
        self.error = error
        self.calls: list[tuple[Any, ...]] = []

    async def fetch_models(self, token: str) -> list[Model]:
        self.calls.append(("models", token))
        return self.models

    async def fetch_quota(self, token: str) -> Quota:
        self.calls.append(("quota", token))
        if self.error is not None:
            raise self.error
        return self.quota

    async def stream_chat(self, payload: dict, model: str,
                          token: str) -> AsyncIterator[Event]:
        self.calls.append(("chat", model, token, payload))
        for event in self.events:
            yield event

    async def aclose(self) -> None:
        self.calls.append(("aclose",))


TOKEN = "tok"
DATA = {"access_token": TOKEN, "uid": "u", "nickname": "n", "expires_at": 5}


def test_import_credential_normalizes_and_extracts_uid():
    provider = RaccoonProvider(client=FakeClient())
    jwt = "a.b.c"
    stored = provider.import_credential({"access_token": jwt})
    assert stored["access_token"] == jwt
    # `a.b.c` 不是合法 JWT claims → uid 留空（不猜）
    assert stored["uid"] == ""


def test_import_credential_rejects_empty_token():
    provider = RaccoonProvider(client=FakeClient())
    with pytest.raises(UpstreamProtocolViolation, match="missing access token"):
        provider.import_credential({"cookie": "a=1"})


def test_credential_expires_at():
    provider = RaccoonProvider(client=FakeClient())
    assert provider.credential_expires_at(DATA) == 5
    assert provider.credential_expires_at({}) == 0


def test_classify_delegates_to_events():
    provider = RaccoonProvider(client=FakeClient())
    assert provider.classify(401, b"") is ErrKind.DEAD


async def test_probe_quota_without_token_is_unknown_not_exhausted():
    provider = RaccoonProvider(client=FakeClient())
    quota = await provider.probe_quota({})
    assert quota.probe_failed is True
    assert quota.total is None


async def test_probe_quota_reads_upstream():
    fake = FakeClient(quota=Quota(remaining=12, total=12))
    provider = RaccoonProvider(client=fake)
    quota = await provider.probe_quota(DATA)
    assert quota.remaining == 12
    assert fake.calls == [("quota", TOKEN)]


async def test_list_models_requires_token_and_updates_known_models():
    provider = RaccoonProvider(client=FakeClient())
    with pytest.raises(UpstreamProtocolViolation, match="missing token"):
        await provider.list_models({})

    fake = FakeClient(models=[Model(id="sn-GLM-5-3"), Model(id="raccoon-090dd3")])
    provider = RaccoonProvider(client=fake)
    models = await provider.list_models(DATA)
    assert [m.id for m in models] == ["sn-GLM-5-3", "raccoon-090dd3"]
    assert provider.known_models == {"sn-glm-5-3", "raccoon-090dd3"}


async def test_stream_chat_requires_token():
    provider = RaccoonProvider(client=FakeClient())
    with pytest.raises(UpstreamProtocolViolation, match="missing token"):
        [e async for e in provider.stream_chat({}, {"messages": []}, "m")]


async def test_stream_chat_rejects_model_missing_from_catalog_before_upstream():
    fake = FakeClient()
    provider = RaccoonProvider(client=fake)
    provider.known_models = {"sn-glm-5-3"}
    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in provider.stream_chat(DATA, {"messages": []}, "raccoon-090dd3")]
    assert caught.value.status == 404
    assert caught.value.kind() is ErrKind.INVALID
    # 目录里没有 → 一次上游请求都不发
    assert fake.calls == []


async def test_stream_chat_allows_known_model_and_passes_through():
    fake = FakeClient(events=[Event(kind=EventKind.CONTENT, content="hi")])
    provider = RaccoonProvider(client=fake)
    provider.known_models = {"sn-glm-5-3"}
    events = [e async for e in provider.stream_chat(DATA, {"messages": []},
                                                    "SN-GLM-5-3")]
    assert [e.kind for e in events] == [EventKind.CONTENT]
    assert fake.calls[0][0] == "chat"
    assert fake.calls[0][1] == "SN-GLM-5-3"


async def test_stream_chat_passes_through_when_catalog_not_loaded():
    """目录还没拉过（已知集为空）时放行：真实保护靠上游回显的 model。"""
    fake = FakeClient(events=[Event(kind=EventKind.CONTENT, content="x")])
    provider = RaccoonProvider(client=fake)
    events = [e async for e in provider.stream_chat(DATA, {"messages": []}, "any")]
    assert [e.kind for e in events] == [EventKind.CONTENT]


async def test_provider_aclose_closes_client():
    fake = FakeClient()
    provider = RaccoonProvider(client=fake)
    await provider.aclose()
    assert fake.calls == [("aclose",)]


async def test_stream_chat_uses_pacer():
    """pacer 必须被取用并释放；替身只实现 wait_turn/release（与生产同接口）。"""

    class PacerSpy:
        def __init__(self) -> None:
            self.keys: list[str] = []
            self.released: list[str] = []

        async def wait_turn(self, key: Any = None) -> None:
            self.keys.append(str(key))

        def release(self, key: Any = None) -> None:
            self.released.append(str(key))

    spy = PacerSpy()
    provider = RaccoonProvider(client=FakeClient(
        events=[Event(kind=EventKind.CONTENT, content="x")]), pacer=spy)
    events = [e async for e in provider.stream_chat(DATA, {"messages": []}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT]
    assert spy.keys == ["u"]          # 按凭证 uid 分桶
    assert spy.released == ["u"]