"""覆盖率收尾（executor/main 分支）：轮换预算、冷却耗尽、不可用文案与补帧。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.compat.openai.request import parse_chat_request
from src.config import Settings
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import Scheduler
from src.main import build_app
from src.provider.base import ErrKind, Event, EventKind, Model, Usage
from src.provider.trae.events import UpstreamProtocolViolation
from tests.helpers.constants import SECRET
from tests.helpers.paths import tmpdir as _tmpdir


class _Provider:
    id = "trae"

    def __init__(self, script=None):
        self.script = script or []
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    def list_models(self, _credential_data):
        return [Model(id="glm-5.2")]

    def import_credential(self, raw):
        if not raw.get("accessToken"):
            raise UpstreamProtocolViolation("credential missing accessToken")
        return dict(raw)

GOOD = [Event(kind=EventKind.CONTENT, content="hi"),
        Event(kind=EventKind.FINISH, finish_reason="stop")]

def _repo(tmp_path):
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository

    db = Database(tmp_path / "c.sqlite3")
    apply_schema(db.connect())
    return CredentialRepository(db, CredentialCipher(SECRET)), db

def _executor(credentials, provider, **kw):
    return Executor(ExecutorDeps(providers={"trae": provider}, credentials=credentials,
                                 scheduler=Scheduler(**kw), default_model="glm-5.2"))

class SoftError(Exception):
    def __init__(self) -> None:
        super().__init__("upstream soft rate limit")

    def kind(self):  # noqa: ANN201
        return ErrKind.SOFT

class PlanError(Exception):
    def __init__(self) -> None:
        super().__init__("upstream plan exhausted")

    def kind(self):  # noqa: ANN201
        return ErrKind.PLAN

def _client(tmp_path, provider=None):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": provider or _Provider([GOOD])})
    return app, TestClient(app)

def _outcome_disabled():
    from src.engine.scheduler import Scheduler
    from src.provider.base import ErrKind

    return Scheduler().note_error(
        __import__("src.engine.scheduler", fromlist=["Candidate"]).Candidate(
            credential_id="c", provider="trae"), ErrKind.DEAD, 0)

def _model_cool(credentials, credential_id, model, *, reason="model"):
    """给某凭证写入一个未过期的模型级冷却条目。"""
    import time

    from src.engine.scheduler import ErrorOutcome, ModelCooldown

    entry = ModelCooldown(cooling_until=int(time.time()) + 600, hits=1, reason=reason)
    credentials.save_error(credential_id, ErrorOutcome(model_cooldowns={model: entry}))


async def test_stream_error_then_second_credential_succeeds(tmp_path):
    """第一次 SOFT 失败 → 换号 → 成功；覆盖 rotate 后继续的分支。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    executor = _executor(credentials, _Provider([[SoftError()], GOOD]))
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"hi" in chunks[0]
    db.close()

async def test_stream_unknown_error_propagates(tmp_path):
    """不认识的上游异常必须原样抛出（不静默、不误判为冷却）。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    executor = _executor(credentials, _Provider([[RuntimeError("boom")]]))
    with pytest.raises(RuntimeError):
        [c async for c in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    db.close()

async def test_stream_inline_error_frame_classified_as_plan(tmp_path):
    """流内 1005（带 error_kind）→ PLAN 长冷却。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    provider = _Provider([[Event(kind=EventKind.ERROR, error_code=1005,
                                 error_kind=ErrKind.PLAN)],
                          [Event(kind=EventKind.ERROR, error_code=500)]])
    executor = _executor(credentials, provider)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"no_healthy_credential" in chunks[-1]
    assert credentials.candidates()[0].cooling_until is not None
    db.close()

async def test_complete_rotates_then_exhausts(tmp_path):
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    executor = _executor(credentials, _Provider([[PlanError()]]))
    with pytest.raises(NoHealthyCredential) as caught:
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    # 只带受控标识（异常类名），不回显上游正文
    assert "PlanError" in str(caught.value) and "exhausted" not in str(caught.value)
    db.close()

async def test_complete_takes_credit_from_last_usage_frame(tmp_path):
    """上游每帧都带 usage 时，记账取**最后一个** USAGE 事件。

    CodeArts v2 实测每个 chunk 都带 usage：前置帧是 0/0 占位、只有收尾帧是真值
    （`{in:35, out:99}`）。取第一个 USAGE 会把积分记成空/0，统计页于是出现
    「有 token 却没积分」的记录。末尾优先同时与 `aggregate()` 和流式路径
    （`translator.usage`）同语义：落库的 usage 与对外响应的 usage 同源。
    """
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})

    class _Stats:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        def record(self, **fields) -> None:  # noqa: ANN003
            self.rows.append(fields)

    stats = _Stats()
    provider = _Provider([[
        Event(kind=EventKind.USAGE, usage=Usage()),                  # 空占位帧
        Event(kind=EventKind.CONTENT, content="hi"),
        Event(kind=EventKind.USAGE, usage=Usage(input_tokens=0, output_tokens=0)),
        Event(kind=EventKind.USAGE, usage=Usage(input_tokens=35, output_tokens=99,
                                                 credit=0.0134, credit_estimated=True)),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ]])
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=stats))
    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert len(stats.rows) == 1
    assert stats.rows[0]["credit"] == 0.0134
    assert stats.rows[0]["credit_estimated"] is True
    assert stats.rows[0]["input_tokens"] == 35
    db.close()

def test_forbidden_handler_returns_403(tmp_path):
    app, client = _client(tmp_path)
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    with client:
        response = client.post("/api/credentials", json={"provider": "trae",
                                                         "credential": {}})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"

def test_unknown_model_provider_returns_400(tmp_path):
    app, client = _client(tmp_path)
    key = app.state.api_keys.create("root")["api_key"]
    with client:
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"messages": [{"role": "user", "content": "x"}],
                                     "model": "glm-5.2@nope"})
    assert response.status_code == 400

def test_no_provider_for_model_returns_400(tmp_path):
    """模型强制指定到未注册的 provider → 400。"""
    app, client = _client(tmp_path, provider=_Provider([GOOD]))
    key = app.state.api_keys.create("root")["api_key"]
    with client:
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"messages": [{"role": "user", "content": "x"}],
                                     "model": "glm-5.2@codebuddy"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

def test_no_healthy_credential_returns_503(tmp_path):
    app, client = _client(tmp_path, provider=_Provider([GOOD]))
    key = app.state.api_keys.create("root")["api_key"]
    with client:
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"messages": [{"role": "user", "content": "x"}]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_healthy_credential"

def test_streaming_endpoint_returns_sse(tmp_path):
    app, client = _client(tmp_path)
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    with client:
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"messages": [{"role": "user", "content": "hi"}],
                                     "stream": True})
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert "data: [DONE]" in response.text

def test_toggle_missing_credential_returns_400(tmp_path):
    app, client = _client(tmp_path)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        response = client.post("/api/credentials/ghost/toggle", json={"enabled": False})
    assert response.status_code == 400

# ------------------------------------------------- 最后一批边界分支

async def test_complete_inline_error_frame_rotates(tmp_path):
    """聚合路径遇到流内错误事件 → 冷却 + 换号（executor 127-130）。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    provider = _Provider([[Event(kind=EventKind.ERROR, error_code=500)], GOOD])
    executor = _executor(credentials, provider)
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"
    assert provider.calls == 2
    db.close()

async def test_complete_exhausts_rotation_without_last_error(tmp_path):
    """换号次数用尽且无 last_error 时的消息分支（executor 135）。

    路径：唯一凭证被硬禁用 → 选不到号 → 无 last_error 的 503 消息。
    文案末尾追加池子成因拆解（`disabled=1`），见 `_pool_hint`。
    """
    credentials, db = _repo(tmp_path)
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.save_error(credential_id, _outcome_disabled())
    executor = _executor(credentials, _Provider([GOOD]))
    with pytest.raises(NoHealthyCredential) as caught:
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert str(caught.value) == "all credentials unavailable (total=1 disabled=1)"
    db.close()

async def test_pick_returns_none_when_all_cooling(tmp_path):
    """_pick 在候选全部冷却时返回 None（executor 154-155）。"""
    import time

    from src.engine.model_resolver import resolve
    from src.engine.scheduler import Scheduler
    from src.provider.base import ErrKind

    credentials, db = _repo(tmp_path)
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    candidate = credentials.candidates()[0]
    credentials.save_error(
        credential_id, Scheduler().note_error(candidate, ErrKind.PLAN, int(time.time())))
    executor = _executor(credentials, _Provider([GOOD]))
    assert await executor._pick(resolve("glm-5.2", "glm-5.2"), set()) is None
    db.close()

def test_unavailable_text_prefers_model_scoped_wording(tmp_path):
    """所有候选都因该模型的模型级冷却而不可用时，文案指向模型而非凭证。"""
    from src.engine.model_resolver import resolve

    credentials, db = _repo(tmp_path)
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    _model_cool(credentials, credential_id, "glm-5.2")
    executor = Executor(ExecutorDeps(
        providers={"trae": _Provider([GOOD])}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2",
        model_suggestions=lambda _name: ["other-model"]))
    target = resolve("glm-5.2", "glm-5.2")
    assert executor._all_model_cooled(target) is True
    # 带 last_error：只附受控标识（异常类名），不回显上游正文
    text = executor._unavailable_text(target, RuntimeError("boom"))
    assert "temporarily unavailable on upstream" in text
    assert "RuntimeError" in text
    assert "boom" not in text
    # 无 last_error：不带冒号细节；建议始终附上
    text = executor._unavailable_text(target, None)
    assert "temporarily unavailable on upstream" in text
    assert "all credentials unavailable" not in text
    assert "other-model" in text
    db.close()

def test_unavailable_text_falls_back_for_account_level(tmp_path):
    """账号级冷却 / 无候选 / 还有可用候选时，回落到通用「凭证不可用」文案。"""
    from src.engine.model_resolver import resolve

    credentials, db = _repo(tmp_path)
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    target = resolve("glm-5.2", "glm-5.2")
    executor = _executor(credentials, _Provider([GOOD]))

    # 还有可用候选
    assert executor._all_model_cooled(target) is False
    assert "all credentials unavailable" in executor._unavailable_text(target, None)

    # 账号级冷却（模型级视角不可选，但不是模型故障）
    import time as _time

    from src.engine.scheduler import Scheduler
    credentials.save_error(
        credential_id, Scheduler().note_error(credentials.candidates()[0],
                                              ErrKind.SOFT, int(_time.time())))
    assert executor._all_model_cooled(target) is False
    db.close()

def test_unavailable_text_no_candidates(tmp_path):
    """没有任何凭证时不算模型故障（复用通用文案）。"""
    from src.engine.model_resolver import resolve

    credentials, db = _repo(tmp_path)
    executor = _executor(credentials, _Provider([GOOD]))
    assert executor._all_model_cooled(resolve("glm-5.2", "glm-5.2")) is False
    db.close()

async def test_stream_model_level_fault_message(tmp_path):
    """上游模型级故障耗尽轮换 → 503 文案指明是模型暂时不可用（非凭证）。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    executor = _executor(credentials, _Provider([[
        Event(kind=EventKind.ERROR, error_code=429,
              error_message="[FAIL]node:x Execution failed", error_kind=ErrKind.MODEL)]]))
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"temporarily unavailable on upstream" in chunks[-1]
    db.close()

async def test_complete_model_level_fault_message(tmp_path):
    """非流式同样给出模型级文案。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    executor = _executor(credentials, _Provider([[
        Event(kind=EventKind.ERROR, error_code=429,
              error_message="[FAIL]node:x Execution failed", error_kind=ErrKind.MODEL)]]))
    with pytest.raises(NoHealthyCredential) as caught:
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert "temporarily unavailable on upstream" in str(caught.value)
    db.close()

def test_event_kind_helper_prefers_event_and_falls_back_to_other():
    """流内错误分类的单一来源是 Event.error_kind：provider 解析时定好，
    executor 不再维护第二份 code→kind 映射（两份必然漂移）；缺失回落 OTHER。"""
    from src.engine.executor import _event_kind

    assert _event_kind(Event(kind=EventKind.ERROR,
                             error_code=1005, error_kind=ErrKind.PLAN)) is ErrKind.PLAN
    assert _event_kind(Event(kind=EventKind.ERROR, error_code=500)) is ErrKind.OTHER

async def test_complete_rotation_exhausted_after_classified_error(tmp_path):
    """max_rotate=1：一次失败后轮换额度即用尽 → 末尾的 503 抛出分支。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    executor = _executor(credentials, _Provider([[SoftError()]]), max_rotate=1)
    with pytest.raises(NoHealthyCredential) as caught:
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert "SoftError" in str(caught.value) and "rate limit" not in str(caught.value)
    db.close()

async def test_stream_rotation_continues_while_budget_remains(tmp_path):
    """流式路径：第一次失败但仍有轮换额度 → 继续循环（executor 92→94）。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    executor = _executor(credentials, _Provider([[SoftError()], GOOD]), max_rotate=5)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"hi" in chunks[0]
    db.close()

async def test_executor_skips_concurrently_deleted_credential(tmp_path):
    """凭证在选择后、读取前被删除 → 跳过它继续选下一个（executor 154-155）。

    用计数器强制第一次读取返回 None，不依赖调度器的挑选顺序。
    """
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    original = credentials.credential_data
    calls = {"n": 0}

    def flaky(credential_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None                       # 模拟并发删除
        return original(credential_id)

    credentials.credential_data = flaky        # type: ignore[method-assign]
    executor = _executor(credentials, _Provider([GOOD]))
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"
    assert calls["n"] >= 2
    db.close()

async def test_stream_rotation_budget_exhausted_with_last_error(tmp_path):
    """流式路径：max_rotate=1 且已记录 last_error → 90-94 的 503 分支。"""
    credentials, db = _repo(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    executor = _executor(credentials, _Provider([[SoftError()]]), max_rotate=1)
    chunks = [c async for c in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert b"SoftError" in chunks[-1] and b"rate limit" not in chunks[-1]
    db.close()

async def test_stream_finish_frames_yielded_when_upstream_never_sent_done():
    """上游没发 done 就正常结束 → finish() 补帧（executor 147-148）。"""
    from src.provider.base import Event as E
    from src.provider.base import EventKind as EK
    from tests.api.test_chat import _RejectProvider, _request

    class _NoDoneProvider(_RejectProvider):
        """正常输出 content 但不发 FINISH。"""

        async def stream_chat(self, _credential_data, _payload, _model):
            self.calls += 1
            yield E(kind=EK.CONTENT, content="ok")

    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler

    db = Database(str(_tmpdir() / "nd.sqlite3"))
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))
    repo.add(provider="trae", credential_data={"accessToken": "t"})
    executor = Executor(ExecutorDeps(
        providers={"trae": _NoDoneProvider("trae", [E(kind=EK.CONTENT, content="ok")])},
        credentials=repo, scheduler=Scheduler(), default_model="glm-5.2"))

    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    assert any(b"[DONE]" in f for f in frames)     # finish() 补的 DONE 已发出
    db.close()

def test_stream_error_4001_is_invalid_not_other():
    """TRAE 流内 4001 → INVALID 的分类在解析时定（见 test_trae_parse_attaches_error_kind），
    executor 侧消费 Event.error_kind；此处守的是 missing 时回落 OTHER。"""
    from src.engine.executor import _event_kind
    from src.provider.base import ErrKind, Event, EventKind

    assert _event_kind(Event(kind=EventKind.ERROR, error_code=4001)) is ErrKind.OTHER
    assert _event_kind(Event(kind=EventKind.ERROR, error_code=4001,
                             error_kind=ErrKind.INVALID)) is ErrKind.INVALID

def test_toggle_rejects_non_boolean_enabled(tmp_path):
    """`{"enabled": "false"}` 必须 400。

    `bool("false") is True`，放过它会把「停用」静默执行成「启用」——写操作
    语义反转，管理员看不到任何报错。
    """
    app, client = _client(tmp_path)
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credential_id = app.state.credentials.candidates()[0].credential_id
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        for bad in ("false", "0", 0, 1, None):
            response = client.post(f"/api/credentials/{credential_id}/toggle",
                                   json={"enabled": bad})
            assert response.status_code == 400, bad
        assert client.post(f"/api/credentials/{credential_id}/toggle",
                           json={"enabled": False}).status_code == 200
