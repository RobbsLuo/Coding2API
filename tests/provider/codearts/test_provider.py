"""CodeArtsProvider 委托与导入。"""

from __future__ import annotations

import httpx
import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.codearts import CodeArtsProvider, credential_key
from src.provider.codearts import events as codearts_events
from src.provider.codearts.client import UpstreamHTTPError
from tests.helpers.http import sse_text as _sse
from tests.provider.codearts._support import client as _client
from tests.provider.codearts._support import cred as _cred
from tests.provider.codearts._support import jwk as _jwk
from tests.provider.codearts._support import token_response as _token_response


def test_provider_id_classify_credential_from_and_key():
    provider = CodeArtsProvider()
    assert provider.id == "codearts"
    assert provider.classify(401, b"") is ErrKind.DEAD
    credential = provider.credential_from({"access_key_id": "AK", "uid": "u"})
    assert credential.access_key_id == "AK"
    from src.tasks.pacer import stable_key
    assert credential_key(credential) == stable_key("codearts", "u")
    assert credential_key(_cred(uid="")) == stable_key("codearts", "AK")

def test_provider_import_credential_validates():
    provider = CodeArtsProvider()
    valid = provider.import_credential({"access_key_id": "AK",
                                        "secret_access_key": "SK"})
    assert valid["access_key_id"] == "AK"
    # dpop_private_jwk 可缺（手工凭证无从刷新）
    assert valid["dpop_private_jwk"] == {}
    with_jwk = provider.import_credential({
        "access_key_id": "AK", "secret_access_key": "SK",
        "dpop_private_jwk": _jwk()})
    assert with_jwk["dpop_private_jwk"]["kty"] == "EC"

def test_provider_import_credential_rejects_bad_input():
    provider = CodeArtsProvider()
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="not an object"):
        provider.import_credential("nope")               # type: ignore[arg-type]
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="missing"):
        provider.import_credential({"access_key_id": "AK"})
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="dpop_private_jwk"):
        provider.import_credential({"access_key_id": "AK", "secret_access_key": "SK",
                                    "dpop_private_jwk": {"kty": "RSA"}})

async def test_provider_list_models_refresh_and_probe_quota():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_MODEL_BUILTIN:
            return httpx.Response(200, json={"builtinModels": [{"id": "m1"}]})
        if request.url.host == "sts.test":
            return httpx.Response(200, json=_token_response())
        return httpx.Response(200, json={
            "metrics": [{"name": "usageTotalPackageCredit",
                         "package_credit_amount": 100.0,
                         "package_credit_remain": 9.0}]})

    provider = CodeArtsProvider(client=_client(handler))
    models = await provider.list_models({})
    assert [m.id for m in models] == ["m1"]
    refreshed = await provider.refresh({"access_key_id": "AK", "secret_access_key": "SK",
                                        "refresh_token": "rt", "dpop_private_jwk": _jwk()})
    assert refreshed["access_key_id"] == "AK2"
    assert (await provider.probe_quota({})).remaining == pytest.approx(9.0)
    await provider.aclose()

async def test_provider_probe_quota_hits_only_statistics_endpoint():
    """额度探测只打一个端点（`/snap-manager/v1/statistics/plugin`），带 PromptCenter 头。

    福利渠道下线后不再有第二本账（每日 token 池 / 福利目录）：探测是纯只读，
    不触发 claim、不刷新一次性 refresh_token。
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("Agent-Type", "")))
        return httpx.Response(200, json={
            "metrics": [{"name": "usageTotalPackageCredit",
                         "package_credit_amount": 100.0,
                         "package_credit_remain": 9.0}]})

    provider = CodeArtsProvider(client=_client(handler))
    quota = await provider.probe_quota(
        {"uid": "u1", "access_key_id": "AK", "secret_access_key": "SK"})
    assert quota.remaining == pytest.approx(9.0) and quota.total == pytest.approx(100.0)
    assert calls == [(codearts_events.EP_STATISTICS_PLUGIN, "PromptCenter")]
    await provider.aclose()

async def test_provider_probe_quota_propagates_upstream_failure():
    """积分接口失败直接上抛（不再降级吞掉合并第二本账）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"down")

    provider = CodeArtsProvider(client=_client(handler))
    with pytest.raises(UpstreamHTTPError):
        await provider.probe_quota(
            {"uid": "u1", "access_key_id": "AK", "secret_access_key": "SK"})
    await provider.aclose()

async def test_provider_stream_chat_pacer_pairing_and_release_on_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse({"text": "hi"}, {"text": "[DONE]"}))

    class Pacer:
        def __init__(self) -> None:
            self.turns = 0
            self.released = 0

        async def wait_turn(self, key=None) -> None:
            self.turns += 1

        def release(self, key=None) -> None:
            self.released += 1

    pacer = Pacer()
    provider = CodeArtsProvider(client=_client(handler), pacer=pacer)
    events = [e async for e in provider.stream_chat(
        {"access_key_id": "AK", "secret_access_key": "SK"},
        {"messages": []}, "m")]
    assert any(e.kind is EventKind.CONTENT for e in events)
    assert pacer.turns == 1 and pacer.released == 1
    await provider.aclose()

    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    pacer2 = Pacer()
    error_provider = CodeArtsProvider(client=_client(boom), pacer=pacer2)
    with pytest.raises(UpstreamHTTPError):
        [e async for e in error_provider.stream_chat(
            {"access_key_id": "AK", "secret_access_key": "SK"}, {}, "m")]
    assert pacer2.released == 1                        # 出错也要归还名额
    await error_provider.aclose()

    plain = CodeArtsProvider(client=_client(handler))
    _ = [e async for e in plain.stream_chat(
        {"access_key_id": "AK", "secret_access_key": "SK"}, {}, "m")]
    await plain.aclose()
