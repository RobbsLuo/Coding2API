"""流式收尾断开的记账口径（覆盖 `stream()` 的 done_sent 分支）。

真实场景（opencode2）：SSE 的 `[DONE]` 发出到结束帧 `more_body=False` 之间
有一拍竞态，框架会把收尾断开当中途断开并关闭生成器；此时响应内容已完整
送达，必须按**成功**记账而不是 `client_disconnect`。

本文件把这条分支固定在 `tests/engine/` 内（回归套件里另有一份等价用例），
使「只跑 tests/engine」时该分支也被计入覆盖率。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.compat.openai.request import ChatRequest
from src.engine.executor import Executor, ExecutorDeps
from src.engine.scheduler import Candidate, Scheduler
from src.provider.base import Event, EventKind, Usage


class _Credentials:
    """单凭证替身：记录 `save_success`，其余方法本用例不调用。"""

    def __init__(self) -> None:
        self.successes: list[str] = []

    def candidates(self, providers=None, *, selectable_only=False, model=None):
        return [Candidate(credential_id="c1", provider="trae")]

    def provider_of(self, _credential_id):
        return "trae"

    def credential_data(self, _credential_id):
        return {"accessToken": "a"}

    def save_success(self, credential_id, *, model=None):
        self.successes.append(credential_id)

    def save_error(self, _credential_id, _outcome):  # pragma: no cover - 本用例不调用
        raise AssertionError("save_error 不该被调用")

    def save_quota(self, *_args, **_kwargs):  # pragma: no cover - 本用例不调用
        raise AssertionError("save_quota 不该被调用")


class _Stats:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, **fields):
        self.rows.append(fields)


def _request() -> ChatRequest:
    return ChatRequest(model="glm-5.2", messages=[{"role": "user", "content": "hi"}],
                       stream=True, raw={"messages": [{"role": "user", "content": "hi"}]})


@dataclass
class _Provider:
    id: str = "trae"

    async def stream_chat(self, _credential_data, _payload, _model):
        yield Event(kind=EventKind.CONTENT, content="hello")
        yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=7, output_tokens=3))
        yield Event(kind=EventKind.FINISH, finish_reason="stop")


async def test_disconnect_after_done_is_recorded_as_success():
    """`[DONE]` 之后断开：按成功记账（ok=True + save_success），不记 client_disconnect。"""
    credentials = _Credentials()
    stats = _Stats()
    executor = Executor(ExecutorDeps(providers={"trae": _Provider()},
                                     credentials=credentials,
                                     scheduler=Scheduler(), stats=stats))

    stream = executor.stream(_request(), username="alice")
    while True:
        frame = await anext(stream)
        if b"[DONE]" in frame:
            await stream.aclose()          # 收尾断开：触发 GeneratorExit 分支
            break

    assert credentials.successes == ["c1"]
    assert len(stats.rows) == 1
    row = stats.rows[0]
    assert row["ok"] is True
    assert "error_type" not in row
    assert (row["username"], row["provider"], row["credential_id"]) == (
        "alice", "trae", "c1")
    assert (row["input_tokens"], row["output_tokens"]) == (7, 3)
