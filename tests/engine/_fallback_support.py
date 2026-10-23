"""fallback 测试共享替身与装配助手。

`test_fallback.py` / `test_fallback_stream.py` 原先各自复制了一份事件常量、
脚本化 provider 与 executor 装配函数（逐字节等价）；这里收敛成一份。
"""

from __future__ import annotations

from src.engine.executor import Executor, ExecutorDeps
from src.engine.scheduler import Scheduler
from src.provider.base import ErrKind, Event, EventKind
from tests.helpers.events import GOOD

__all__ = [
    "GOOD",
    "INVALID",
    "HttpInvalid",
    "ModelAwareProvider",
    "RaisingProvider",
    "ScriptedProvider",
    "add_credential",
    "aliases",
    "make_executor",
]

INVALID = [Event(kind=EventKind.ERROR, error_code="11102",
                 error_message="service info not found", error_kind=ErrKind.INVALID)]


class HttpInvalid(Exception):
    """HTTP 级请求无效（如 400 模型不存在）。

    真实 provider 的 `UpstreamHTTPError` 带 `kind()`：同样是 INVALID，但走
    executor 的 `except` 分支而不是流内事件分支，两条路径的收尾逻辑不同。
    """

    def kind(self) -> ErrKind:
        return ErrKind.INVALID


class RaisingProvider:
    """首次取流就抛出指定异常（模拟 HTTP 非 2xx 在建立流时失败）。"""

    def __init__(self, provider_id: str, error: Exception) -> None:
        self.id = provider_id
        self.error = error
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        self.calls += 1
        raise self.error
        yield  # 让本方法成为异步生成器：异常在首次 anext 时抛出

    def list_models(self, _credential_data):
        return []


class ScriptedProvider:
    """每次调用按顺序回放 `script` 里的事件列表（越界后重复最后一个）。"""

    def __init__(self, provider_id: str, script: list[list[Event]] | None = None) -> None:
        self.id = provider_id
        self.script = script or [GOOD]
        self.calls = 0
        self.models_seen: list[str] = []

    async def stream_chat(self, _credential_data, _payload, model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        self.models_seen.append(model)
        for event in self.script[index]:
            yield event

    def list_models(self, _credential_data):
        return []


class ModelAwareProvider:
    """只接受白名单里的模型，其余回 INVALID（贴近真实上游）。"""

    def __init__(self, provider_id: str, good_models: set[str]) -> None:
        self.id = provider_id
        self.good_models = {m.lower() for m in good_models}
        self.calls = 0
        self.models_seen: list[str] = []

    async def stream_chat(self, _credential_data, _payload, model):
        self.calls += 1
        self.models_seen.append(model)
        for event in GOOD if model.lower() in self.good_models else INVALID:
            yield event

    def list_models(self, _credential_data):
        return []


def add_credential(credentials, provider: str, token: str) -> None:
    credentials.add(provider=provider, credential_data={"bearer_token": token})


def aliases(primary_model: str, fallback_model: str) -> dict[str, dict[str, str]]:
    return {"codebuddy": {primary_model.lower(): primary_model},
            "trae": {fallback_model.lower(): fallback_model}}


def make_executor(providers, credentials, groups, aliases=None, **scheduler_kw):
    return Executor(ExecutorDeps(
        providers=providers, credentials=credentials,
        scheduler=Scheduler(**scheduler_kw), default_model="glm-5.2",
        model_aliases=aliases or {}, fallback_groups=groups))
