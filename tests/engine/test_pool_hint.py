"""503「选不到号」成因拆解（`_no_pick_breakdown` / `_pool_hint`）。

判据顺序必须与 `_selectable` / `_choose` 一致：disabled → paused →
账号级冷却 → 模型级冷却 → 限额 → 在途 → 本轮试过 → 仍可选。
顺序一旦与选号不一致，「选不到」与「归到哪类」会自相矛盾。
"""

from __future__ import annotations

import time

from src.compat.openai.request import parse_chat_request
from src.engine.executor import Executor, ExecutorDeps, _with_pool_hint
from src.engine.inflight import InflightGuard
from src.engine.model_resolver import resolve
from src.engine.scheduler import Scheduler
from src.provider.base import Event, EventKind

_TARGET = resolve("glm-5.2@trae", "glm-5.2")


def _request():
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}]})


class _Provider:
    id = "trae"

    async def stream_chat(self, _credential_data, _payload, _model):
        yield Event(kind=EventKind.ERROR, error_message="boom")


def _executor(credentials, **deps) -> Executor:
    return Executor(ExecutorDeps(providers={"trae": _Provider()},
                                 credentials=credentials,
                                 scheduler=Scheduler(), default_model="glm-5.2",
                                 **deps))


def test_breakdown_counts_every_gate_in_selection_order(repo):
    credentials, db = repo
    disabled = credentials.add(provider="trae", credential_data={"accessToken": "d"})
    paused = credentials.add(provider="trae", credential_data={"accessToken": "p"})
    cooling = credentials.add(provider="trae", credential_data={"accessToken": "c"})
    model_cooled = credentials.add(provider="trae", credential_data={"accessToken": "m"})
    tried = credentials.add(provider="trae", credential_data={"accessToken": "t"})
    credentials.add(provider="trae", credential_data={"accessToken": "r"})

    now = int(time.time())
    db.connect().execute("UPDATE credentials SET disabled = 1 WHERE id = ?", (disabled,))
    credentials.set_enabled(paused, False)
    db.connect().execute("UPDATE credentials SET cooling_until = ? WHERE id = ?",
                         (now + 600, cooling))
    db.connect().execute(
        "INSERT INTO credential_model_cooldowns (credential_id, model, cooling_until,"
        " hits, reason) VALUES (?,?,?,?,?)", (model_cooled, "glm-5.2", now + 600, 1, "soft"))

    executor = _executor(credentials)
    counts = executor._no_pick_breakdown(_TARGET, {tried})
    assert counts == {"disabled": 1, "paused": 1, "cooling": 1,
                      "model_cooled": 1, "tried": 1, "selectable": 1}
    db.close()


def test_breakdown_counts_limits_and_inflight(repo):
    credentials, db = repo
    limited = credentials.add(provider="trae", credential_data={"accessToken": "l"})
    busy = credentials.add(provider="trae", credential_data={"accessToken": "b"})

    class Limits:
        def blocked(self, candidate, model):
            return candidate.credential_id == limited

    executor = _executor(credentials, limits=Limits(),
                         inflight=InflightGuard(lambda: 1))
    executor._deps.inflight.acquire(busy)

    counts = executor._no_pick_breakdown(_TARGET, set())
    assert counts == {"limits": 1, "inflight": 1}
    db.close()


def test_breakdown_uses_model_scope_for_model_cooldowns(repo):
    """模型级冷却按**渠道的原始模型名**登记：归一键命中不了它。"""
    credentials, db = repo
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    now = int(time.time())
    db.connect().execute(
        "INSERT INTO credential_model_cooldowns (credential_id, model, cooling_until,"
        " hits, reason) VALUES (?,?,?,?,?)",
        (credential_id, "kimi-k3-1", now + 600, 1, "model_rate_limited"))

    # 归一键 `kimi-k3` 在 trae 的原始名是 `kimi-k3-1`：冷却表按原始名登记
    executor = _executor(
        credentials,
        upstream_model_name=lambda provider, model: (
            "kimi-k3-1" if model == "kimi-k3" else model))
    counts = executor._no_pick_breakdown(resolve("kimi-k3@trae", "kimi-k3"), set())
    assert counts.get("model_cooled") == 1
    db.close()


def test_pool_hint_lists_only_non_zero_and_handles_empty_pool(repo):
    credentials, db = repo
    executor = _executor(credentials)
    # 空池：没有任何候选 → 空串（没有可拆解的东西）
    assert executor._pool_hint(_TARGET, set()) == ""

    credentials.add(provider="trae", credential_data={"accessToken": "a"},
                    now=int(time.time()) - 2)
    credentials.add(provider="trae", credential_data={"accessToken": "b"},
                    now=int(time.time()) - 1)
    hint = executor._pool_hint(_TARGET, set())
    assert hint.startswith("total=2 ")
    # 零项不出现在文本里（避免一串 0 噪声）
    assert "cooling=0" not in hint
    assert "selectable=2" in hint

    busy = credentials.candidates(["trae"])[0].credential_id
    hint = executor._pool_hint(_TARGET, {busy})
    assert "tried=1" in hint and "selectable=1" in hint
    db.close()


def test_with_pool_hint_keeps_text_when_hint_empty():
    assert _with_pool_hint("base", "") == "base"
    assert _with_pool_hint("base", "total=2 cooling=2") == "base (total=2 cooling=2)"


async def test_non_stream_503_carries_breakdown(repo, caplog):
    """全池冷却 → 503 文案带池子成因，且日志给出同一份拆解。"""
    import logging

    from src.engine.executor import NoHealthyCredential

    credentials, db = repo
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    db.connect().execute("UPDATE credentials SET cooling_until = ? WHERE id = ?",
                         (int(time.time()) + 600, credential_id))
    executor = _executor(credentials)
    with caplog.at_level(logging.WARNING):
        try:
            await executor.complete(_request())
        except NoHealthyCredential as error:
            message = str(error)
        else:  # pragma: no cover - 冷却中必定 503
            raise AssertionError("expected NoHealthyCredential")
    assert "total=1" in message and "cooling=1" in message
    assert any("选不到号" in record.getMessage() and "cooling=1" in record.getMessage()
               for record in caplog.records)
    db.close()


async def test_stream_503_carries_breakdown(repo):
    """流式路径同样带成因（未出帧时以错误帧收尾）。"""
    credentials, db = repo
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    db.connect().execute("UPDATE credentials SET cooling_until = ? WHERE id = ?",
                         (int(time.time()) + 600, credential_id))
    executor = _executor(credentials)
    frames = [frame async for frame in executor.stream(_request())]
    text = b"".join(frames).decode()
    assert "total=1" in text and "cooling=1" in text
    db.close()
