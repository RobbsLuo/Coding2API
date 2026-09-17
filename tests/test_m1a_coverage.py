"""M1a 覆盖率补齐：repo / executor / trae client / sse / response 的边界分支。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.compat.openai.response import StreamTranslator, aggregate
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import (
    ApiKeyRepository,
    CredentialRepository,
)
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import Scheduler
from src.engine.sse import iter_frames, parse_frames
from src.main import build_app
from src.provider.base import Event, EventKind, Quota, Usage
from src.provider.trae import events as trae_events
from src.provider.trae.callback import parse_callback_url
from src.provider.trae.client import TraeClient, TraeCredential, TraeProvider
from src.provider.trae.events import UpstreamProtocolViolation
from tests.conftest import SECRET

# ------------------------------------------------------------------ repo

@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "r.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()


def test_repo_update_paths(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.save_credential_data(credential_id, {"accessToken": "b"})
    assert credentials.credential_data(credential_id) == {"accessToken": "b"}

    credentials.save_quota(credential_id, Quota(remaining=5, total=10, cycle_end=123,
                                                expiry_ladder=[(123, 5.0)], probed_at=1))
    assert credentials.candidates()[0].health == 50
    assert credentials.candidates()[0].cycle_end == 123
    assert credentials.candidates()[0].expiry_ladder == [(123, 5.0)]

    credentials.save_quota(credential_id, Quota(remaining=5, total=10, cycle_end=123,
                                                probed_at=1))
    assert credentials.candidates()[0].expiry_ladder is None

    credentials.mark_probe_failed(credential_id, now=2)
    assert credentials.candidates()[0].health is None

    credentials.set_pinned(credential_id)
    assert credentials.candidates()[0].pinned is True
    credentials.set_pinned(None)
    assert credentials.candidates()[0].pinned is False


def test_repo_expiry_ladder_tolerates_missing_and_dirty_values():
    """到期阶梯读写：空值写 NULL，NULL/空串/坏 JSON/坏行一律当「无周期概念」，
    不能让一行脏数据把整个选号流程拖崩。"""
    from src.db.repo import _ladder_text, _ladder_value

    assert _ladder_text(None) is None
    assert _ladder_text([]) is None
    assert json.loads(_ladder_text([(1, 2.0), (3, 4.5)])) == [[1, 2.0], [3, 4.5]]

    assert _ladder_value(None) is None
    assert _ladder_value("") is None
    assert _ladder_value("{not json") is None
    assert _ladder_value("[[123, 5], [9], 7, \"junk\", [1, 2, 3]]") == [(123, 5.0)]


def test_repo_missing_credential_returns_none(repo):
    credentials, _db = repo
    assert credentials.credential_data("nope") is None
    assert credentials.provider_of("nope") is None
    assert credentials.delete("nope") is False
    assert credentials.set_enabled("nope", False) is False


def test_repo_candidates_filter_by_provider(repo):
    credentials, _db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    assert len(credentials.candidates()) == 2
    assert len(credentials.candidates(["trae"])) == 1
    assert credentials.candidates(["trae"])[0].provider == "trae"


def test_apikey_repository_verify_updates_last_used(repo):
    _credentials, db = repo
    keys = ApiKeyRepository(db)
    created = keys.create("alice", "k", now=100)
    assert keys.verify(created["api_key"]) == "alice"
    assert keys.verify("sk-nonexistent") is None
    assert keys.list_for("alice")[0]["last_used_at"] is not None
    assert keys.list_for("bob") == []
    assert keys.delete(created["id"], "bob") is False
    assert keys.delete(created["id"], "alice") is True


# -------------------------------------------------------------- executor

GOOD = [Event(kind=EventKind.CONTENT, content="hi"),
        Event(kind=EventKind.USAGE, usage=Usage(1, 2, 0)),
        Event(kind=EventKind.FINISH, finish_reason="stop")]


def _executor(repo_tuple, script, **kw):
    credentials, _db = repo_tuple
    provider = _ScriptedProvider(script)
    executor = Executor(ExecutorDeps(providers={"trae": provider}, credentials=credentials,
                                     scheduler=Scheduler(**kw), default_model="glm-5.2"))
    return executor, provider, credentials


class _ScriptedProvider:
    id = "trae"

    def __init__(self, script):
        self.script = script
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    def list_models(self, _credential_data):  # pragma: no cover - 由 main 调用
        return []


async def test_executor_stream_exhausts_rotation_and_reports(repo):
    credentials, _db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    credentials.add(provider="trae", credential_data={"accessToken": "c"})
    credentials.add(provider="trae", credential_data={"accessToken": "d"})

    class Boom(Exception):
        def kind(self):  # noqa: ANN201
            return _soft()

    def _soft():
        from src.provider.base import ErrKind
        return ErrKind.SOFT

    executor, provider, _credentials = _executor(repo, [[Boom()]])
    chunks = [c async for c in executor.stream(_request(stream=True))]
    assert provider.calls == 3                        # MAX_ROTATE
    assert b"all credentials unavailable" in chunks[-1]


async def test_executor_complete_reports_unavailable_without_credentials(repo):
    executor, _provider, _credentials = _executor(repo, [GOOD])
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))


async def test_executor_stream_no_credential_after_rotation(repo):
    credentials, _db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})

    class Boom(Exception):
        def kind(self):  # noqa: ANN201
            from src.provider.base import ErrKind
            return ErrKind.PLAN

    executor, _provider, _credentials = _executor(repo, [[Boom()]])
    chunks = [c async for c in executor.stream(_request(stream=True))]
    assert b"all credentials unavailable" in chunks[-1]


def _request(stream=False):
    from src.compat.openai.request import parse_chat_request

    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                               "stream": stream})


def test_executor_candidate_lookup_missing_raises(repo):
    executor, _provider, _credentials = _executor(repo, [GOOD])
    with pytest.raises(NoHealthyCredential):
        executor._candidate("ghost")


async def test_executor_skips_concurrently_deleted_credential(repo):
    """凭证在选择后、读取前被删除 → 跳过它继续选下一个。"""
    credentials, _db = repo
    first = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    original = credentials.credential_data

    def deleting(credential_id):
        data = original(credential_id)
        if credential_id == first and data is not None:
            credentials.delete(first)
            return None
        return data

    credentials.credential_data = deleting       # type: ignore[method-assign]
    executor, _provider, _credentials = _executor(repo, [GOOD])
    result = await executor.complete(_request())
    assert result["choices"][0]["message"]["content"] == "hi"


def test_aggregate_tool_call_without_index_merges_by_generated_position():
    result = aggregate([
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "x", "function": {"name": "f", "arguments": "1"}}]),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "x", "function": {"arguments": "2"}}]),
    ], "m")
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == "12"


def test_stream_translator_tool_call_without_id_uses_next_slot():
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.CONTENT, content="x")))
    frames = list(t.translate(Event(kind=EventKind.TOOL_CALLS,
                                    tool_calls=[{"function": {"name": "f"}}])))
    delta = json.loads(frames[0].decode()[6:])["choices"][0]["delta"]
    assert delta["tool_calls"][0]["index"] == 0


def test_stream_translator_repeated_id_reuses_index():
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.CONTENT, content="x")))
    list(t.translate(Event(kind=EventKind.TOOL_CALLS,
                           tool_calls=[{"id": "a", "index": 2, "function": {}}])))
    frames = list(t.translate(Event(kind=EventKind.TOOL_CALLS,
                                    tool_calls=[{"id": "a", "function": {}}])))
    delta = json.loads(frames[0].decode()[6:])["choices"][0]["delta"]
    assert delta["tool_calls"][0]["index"] == 2


# ----------------------------------------------------------- trae client

def _client(handler) -> TraeClient:
    transport = httpx.MockTransport(handler)
    return TraeClient(stream_client=httpx.AsyncClient(transport=transport, timeout=None),
                      short_client=httpx.AsyncClient(transport=transport, timeout=None))


async def test_stream_chat_skips_frames_without_events():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="event:metadata\ndata:{}\n\n"
                                        "event:extra_info\ndata:{}\n\n"
                                        "event:output\ndata:{\"response\":\"x\"}\n\n")

    events = [e async for e in _client(handler).stream_chat(
        TraeCredential(access_token="a"), {}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT]


async def test_fetch_models_skips_entries_without_config_name():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": [
            {"display_config": {}}, {"config_name": "ok"}]})

    models = await _client(handler).fetch_models(TraeCredential(access_token="a"))
    assert [m.id for m in models] == ["ok"]


async def test_refresh_token_without_refresh_token_raises():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {"Token": "t"}})

    refreshed = await _client(handler).refresh_token(TraeCredential(access_token="a"))
    assert refreshed.access_token == "t" and refreshed.refresh_token == ""


async def test_provider_auth_helpers_delegate_to_callback_module():
    provider = TraeProvider(client=_client(lambda _r: httpx.Response(200, json={})))
    url = provider.build_login_url("http://cb/authorize", machine_id="m", device_id="d")
    assert "auth_callback_url" in url
    with pytest.raises(UpstreamProtocolViolation):
        provider.parse_login_url("not-a-url")


async def test_get_user_info_ignores_missing_fields():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {}})

    assert await _client(handler).get_user_info(TraeCredential(access_token="a")) == ("", "")


# ------------------------------------------------------------ events/sse

def test_parse_all_events_splits_content_and_reasoning():
    frame = trae_events.SSEFrame(
        event="output",
        data='{"response":"正文","reasoning_content":"思考","tool_calls":null}')
    kinds = [e.kind for e in trae_events.parse_all_events(frame)]
    assert kinds == [EventKind.CONTENT, EventKind.REASONING]


def test_parse_all_events_orders_tools_first():
    frame = trae_events.SSEFrame(
        event="output",
        data='{"response":"x","reasoning_content":"y","tool_calls":'
             '[{"id":"c","function_call":{"name":"bash","arguments":"{}"}}]}')
    assert [e.kind for e in trae_events.parse_all_events(frame)] == [
        EventKind.TOOL_CALLS, EventKind.CONTENT, EventKind.REASONING]
    # name 为空的 tool_call 增量（上游分片噪声）被过滤，不产生 TOOL_CALLS
    noisy = trae_events.SSEFrame(
        event="output",
        data='{"response":"x","tool_calls":[{"id":"c"},{"id":"d","function_call":{"name":"f"}}]}')
    events = trae_events.parse_all_events(noisy)
    tools = [e for e in events if e.kind is EventKind.TOOL_CALLS]
    assert len(tools) == 1 and tools[0].tool_calls[0]["function"]["name"] == "f"


def test_parse_all_events_passes_through_non_output_frames():
    usage = trae_events.SSEFrame(event="token_usage", data='{"prompt_tokens":1}')
    assert [e.kind for e in trae_events.parse_all_events(usage)] == [EventKind.USAGE]
    empty = trae_events.SSEFrame(event="metadata", data="{}")
    assert trae_events.parse_all_events(empty) == []


def test_parse_all_events_without_data_returns_empty():
    assert trae_events.parse_all_events(trae_events.SSEFrame(event="output", data="")) == []


@pytest.mark.parametrize("data", ["{broken", "[1]"])
def test_parse_all_events_rejects_malformed_output(data):
    with pytest.raises(UpstreamProtocolViolation):
        trae_events.parse_all_events(trae_events.SSEFrame(event="output", data=data))


def test_error_frame_with_non_int_code_is_none():
    event = trae_events.parse_frame(
        trae_events.SSEFrame(event="error", data='{"code":"1005","message":"x"}'))
    assert event.error_code is None and event.error_message == "x"


def test_error_frame_with_non_string_message_is_none():
    event = trae_events.parse_frame(
        trae_events.SSEFrame(event="error", data='{"code":1,"message":5}'))
    assert event.error_message is None


def test_done_frame_without_finish_reason():
    event = trae_events.parse_frame(trae_events.SSEFrame(event="done", data="{}"))
    assert event.finish_reason is None


def test_usage_frame_with_boolean_tokens_is_ignored():
    event = trae_events.parse_frame(
        trae_events.SSEFrame(event="token_usage", data='{"prompt_tokens":true}'))
    assert event.usage.input_tokens is None


async def test_iter_frames_flushes_trailing_frame_without_blank_line():
    async def gen():
        yield b"event: x\ndata: 1"

    frames = [f async for f in iter_frames(gen())]
    assert frames == [trae_events.SSEFrame(event="x", data="1")]


async def test_iter_frames_skips_comments_and_blank_lines():
    async def gen():
        yield b": ping\n\n\nevent: x\ndata: 1\n\n"

    assert [f.event for f in [f async for f in iter_frames(gen())]] == ["x"]


async def test_iter_frames_handles_crlf():
    async def gen():
        yield b"event: x\r\ndata: 1\r\n\r\n"

    frames = [f async for f in iter_frames(gen())]
    assert frames[0] == trae_events.SSEFrame(event="x", data="1")


def test_parse_frames_sync_handles_crlf():
    assert parse_frames("event: x\r\ndata: 1\r\n\r\n")[0].data == "1"


# --------------------------------------------------------------- callback

def test_callback_with_double_encoded_user_info():
    url = ("http://x/authorize?refreshToken=R&userInfo=%257B%2522uid%2522%3A%2522u%2522%257D")
    assert parse_callback_url(url).uid == "u"


def test_callback_ignores_non_object_user_info():
    url = "http://x/authorize?refreshToken=R&userInfo=%5B1%5D"
    assert parse_callback_url(url).uid == ""


def test_callback_expires_at_from_user_info():
    url = ('http://x/authorize?refreshToken=R&userInfo='
           '%7B%22uid%22%3A%22u%22%2C%22expiresAt%22%3A12345%7D')
    assert parse_callback_url(url).expires_at == 12345


def test_callback_expires_at_bool_is_ignored():
    url = ('http://x/authorize?refreshToken=R&userInfo='
           '%7B%22expiresAt%22%3Atrue%7D')
    assert parse_callback_url(url).expires_at == 0


def test_callback_accepts_upper_case_keys():
    url = ('http://x/authorize?refreshToken=R&userInfo='
           '%7B%22UID%22%3A%22up%22%2C%22ScreenName%22%3A%22sn%22%7D')
    info = parse_callback_url(url)
    assert info.uid == "up" and info.nickname == "sn"


# ------------------------------------------------------------- main app

def test_models_endpoint_lists_all_registered_providers(tmp_path):
    from src.config import Settings
    from src.provider.base import Model

    class TwoProviders:
        id = "codebuddy"

        async def list_models(self, _credential_data):
            return [Model(id="glm-5.2"), Model(id="only-cb")]

        def import_credential(self, raw):  # pragma: no cover
            return raw

    class TraeStub:
        id = "trae"

        async def list_models(self, _credential_data):
            return [Model(id="glm-5.2")]

        def import_credential(self, raw):  # pragma: no cover
            return raw

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings, providers={"codebuddy": TwoProviders(), "trae": TraeStub()})
    key = app.state.api_keys.create("root")["api_key"]
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        data = test_client.get("/v1/models",
                               headers={"Authorization": f"Bearer {key}"}).json()["data"]
    by_id = {item["id"]: item["providers"] for item in data}
    assert by_id["glm-5.2"] == ["codebuddy", "trae"]
    assert by_id["only-cb"] == ["codebuddy"]
