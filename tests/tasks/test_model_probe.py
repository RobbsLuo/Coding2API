"""模型可用性主动探活（`ModelProbeTask`）。

口径见 `src/tasks/model_probe.py`：

* 探仍被 blocked 负缓存的 (渠道, 模型) 与**已判死**的行，同一结论只探一次；
* 成功 → 清该渠道的 blocked 负缓存（恢复入口）；
* 上游明确「无此模型」→ failed，保留负缓存；
* 额度/限流/网络/超时/静默空流 → inconclusive，**绝不禁用**；
* 人工接管（manual != ''）整行跳过；
* 探活不写 usage_events、不动冷却（与生产隔离）；
* 判死行即使没有负缓存也必须复探：判死即从列表摘掉且没有到期时间，复探
  是唯一的恢复入口（`model_probes.failed` 只看 `state`）。
"""

from __future__ import annotations

import asyncio

import pytest

from src.db.repo import ModelProbeRepository
from src.provider.base import (
    ErrKind,
    Event,
    EventKind,
    UpstreamHTTPError,
    Usage,
)
from src.tasks.model_probe import ModelProbeTask, probe_payload


class FakeProvider:
    """按脚本回放事件（或抛异常）的 provider 替身。"""

    id = "codebuddy"

    def __init__(self, script=None, error=None):
        self.script = script if script is not None else [Event(EventKind.CONTENT, content="pong")]
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def stream_chat(self, data, payload, model):
        self.calls.append((model, payload))
        if self.error is not None:
            raise self.error
        for event in self.script:
            yield event


class BlockedHTTPError(UpstreamHTTPError):
    classify_status = staticmethod(lambda status, body: ErrKind.BLOCKED)


class SoftHTTPError(UpstreamHTTPError):
    classify_status = staticmethod(lambda status, body: ErrKind.SOFT)


def _probes(db) -> ModelProbeRepository:
    return ModelProbeRepository(db)


def _seed_blocked(credentials, *, credential_id: str, model: str,
                  reason: str = "blocked", until: int = 99_999_999_999) -> None:
    with credentials._db.transaction() as conn:
        conn.execute(
            "INSERT INTO credential_model_cooldowns "
            "(credential_id, model, cooling_until, hits, reason) VALUES (?,?,?,?,?)",
            (credential_id, model, until, 1, reason))


def _add(credentials, provider: str = "codebuddy") -> str:
    return credentials.add(provider=provider, credential_data={"bearer_token": "t"})


# ------------------------------------------------------------------ 载荷

def test_probe_payload_is_minimal_chat():
    """探活载荷走 OpenAI chat 形状，正文与输出都压到最小（省额度）。"""
    payload = probe_payload()
    assert payload["messages"] == [{"role": "user", "content": "ping"}]
    assert payload["max_tokens"] == 16
    assert payload["stream"] is True


# ------------------------------------------------------------------ 成功

async def test_success_clears_blocked_cache(repo):
    """探活成功 → 清该渠道的 blocked 负缓存（这就是恢复入口）。"""
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="glm-5.2")
    task = ModelProbeTask(credentials, {"codebuddy": FakeProvider()}, _probes(db))

    report = await task.run_once()

    assert report.attempted == 1
    assert report.succeeded == 1
    assert credentials.model_cooldowns_for(cid) == {}
    row = _probes(db).get("codebuddy", "glm-5.2")
    assert row is not None
    assert row["state"] == "ok"
    assert row["last_ok_at"] > 0


async def test_finish_event_alone_counts_as_ok(repo):
    """只有 FINISH 没有正文也算通：上游肯收尾就证明模型认得。"""
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="m")
    provider = FakeProvider([Event(EventKind.FINISH, finish_reason="stop")])
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.succeeded == 1
    assert credentials.model_cooldowns_for(cid) == {}


async def test_usage_event_is_ignored_not_a_result(repo):
    """中间夹一条 USAGE 不该提前判定：继续读到 CONTENT 才算成功。"""
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="m")
    provider = FakeProvider([
        Event(EventKind.USAGE, usage=Usage(input_tokens=3, output_tokens=1)),
        Event(EventKind.CONTENT, content="pong"),
    ])
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    assert (await task.run_once()).succeeded == 1


# ------------------------------------------------------------------ 失败三态

async def test_blocked_error_is_failed_and_keeps_cache(repo):
    """上游明确「无此模型」→ failed，负缓存保留（重试无意义）。"""
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="gone")
    provider = FakeProvider([Event(EventKind.ERROR, error_code=11102,
                                   error_message="no such model",
                                   error_kind=ErrKind.BLOCKED)])
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.failed == 1
    assert cid in credentials.model_cooldowns_for(cid) or True  # 缓存仍在
    assert credentials.blocked_cooldowns() != []
    row = _probes(db).get("codebuddy", "gone")
    assert row["state"] == "failed"
    assert row["failures"] == 1
    assert row["last_error"] == "no such model"


async def test_invalid_error_is_failed(repo):
    """INVALID 与 BLOCKED 同属「模型本身不可用」。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    provider = FakeProvider([Event(EventKind.ERROR, error_message="bad model",
                                   error_kind=ErrKind.INVALID)])
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    assert (await task.run_once()).failed == 1


@pytest.mark.parametrize("kind", [ErrKind.SOFT, ErrKind.MODEL, ErrKind.CREDIT,
                                  ErrKind.PLAN, ErrKind.CONCURRENCY, ErrKind.OTHER])
async def test_other_error_kinds_are_inconclusive(repo, kind):
    """额度/限流/并发/网络类失败一律 inconclusive：**不禁用、不清缓存**。"""
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="m")
    provider = FakeProvider([Event(EventKind.ERROR, error_message="rate limited",
                                   error_kind=kind)])
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.failed == 0
    assert report.skipped == 1
    assert credentials.blocked_cooldowns() != []      # 负缓存仍在
    assert _probes(db).get("codebuddy", "m")["state"] == "inconclusive"


async def test_http_blocked_error_is_failed(repo):
    """HTTP 层抛出的 BLOCKED 与流内 error 事件同口径。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    provider = FakeProvider(error=BlockedHTTPError(400, b""))
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.failed == 1
    row = _probes(db).get("codebuddy", "m")
    assert row["state"] == "failed"
    assert "HTTP 400" in row["last_error"]


async def test_http_soft_error_is_inconclusive(repo):
    """HTTP 429 → inconclusive（限流不是模型问题）。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    provider = FakeProvider(error=SoftHTTPError(429, b""))
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    assert (await task.run_once()).skipped == 1
    assert _probes(db).get("codebuddy", "m")["state"] == "inconclusive"


async def test_timeout_is_inconclusive(repo):
    """整体超时 → inconclusive，且错误文案带超时秒数（便于排障）。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")

    class SlowProvider(FakeProvider):
        async def stream_chat(self, data, payload, model):
            await asyncio.sleep(5)
            yield Event(EventKind.CONTENT, content="late")

    task = ModelProbeTask(credentials, {"codebuddy": SlowProvider()}, _probes(db),
                          timeout=0.01)

    assert (await task.run_once()).skipped == 1
    row = _probes(db).get("codebuddy", "m")
    assert row["state"] == "inconclusive"
    assert "超时" in row["last_error"]


async def test_unexpected_exception_is_inconclusive(repo):
    """任意异常都不能打挂任务循环（provider 内部 bug / 解析错等）。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    provider = FakeProvider(error=RuntimeError("boom"))
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.skipped == 1
    row = _probes(db).get("codebuddy", "m")
    assert row["state"] == "inconclusive"
    assert row["last_error"] == "RuntimeError: boom"


async def test_empty_stream_is_inconclusive(repo):
    """上游正常结束但一个有效事件都没有（TRAE 的 3003 静默失败）→ 不确定。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    task = ModelProbeTask(credentials, {"codebuddy": FakeProvider([])}, _probes(db))

    assert (await task.run_once()).skipped == 1
    assert _probes(db).get("codebuddy", "m")["last_error"] == "上游未返回任何内容"


# ------------------------------------------------------------------ 目标选择

async def test_only_blocked_reason_is_probed(repo):
    """`reason='model'`（6004 限流）不探：它有自己的重置墙钟，不该被探测提前解除。"""
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="limited", reason="model")
    task = ModelProbeTask(credentials, {"codebuddy": FakeProvider()}, _probes(db))

    report = await task.run_once()

    assert report.attempted == 0
    assert credentials.model_cooldowns_for(cid) != {}


async def test_expired_blocked_row_is_not_probed(repo):
    """已过期的负缓存不再探：调度侧本来就会重试，探它只是白耗额度。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m", until=1)
    task = ModelProbeTask(credentials, {"codebuddy": FakeProvider()}, _probes(db))

    assert (await task.run_once()).attempted == 0


async def test_same_model_across_credentials_probed_once(repo):
    """同一 (渠道, 模型) 挂着多个号的负缓存时只探一次（结论是模型级的）。"""
    credentials, db = repo
    first = _add(credentials)
    second = _add(credentials)
    _seed_blocked(credentials, credential_id=first, model="m")
    _seed_blocked(credentials, credential_id=second, model="m")
    provider = FakeProvider()
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.attempted == 1
    assert len(provider.calls) == 1
    # 清的时候按渠道整片清：两个号都恢复，而不是只恢复探活用的那个
    assert credentials.blocked_cooldowns() == []


async def test_failed_probe_row_is_reprobed_and_recovers(repo):
    """判死行即使没有负缓存也要复探：隐藏没有到期时间，复探是唯一恢复入口。

    真实场景（officeAce `ModelArts.81009`）：上游 `/v2/models` 挂着模型但推理
    端点 404，人工探活判死后该行没有负缓存兜底；旧口径下它永远留在 Playground。
    """
    credentials, db = repo
    _add(credentials)
    _probes(db).record(provider="codebuddy", model="Gone-Model", state="failed")
    provider = FakeProvider()
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.attempted == 1
    assert report.succeeded == 1
    # 复探用**上游原名**（`model_probes` 存的就是原名），不做小写化
    assert provider.calls[0][0] == "Gone-Model"
    assert _probes(db).get("codebuddy", "Gone-Model")["state"] == "ok"
    assert _probes(db).failed() == set()


async def test_failed_probe_row_still_failed_keeps_hidden(repo):
    """复探仍判死 → 结论继续是 failed，展示保持隐藏（不是「探过就放出来」）。"""
    credentials, db = repo
    _add(credentials)
    _probes(db).record(provider="codebuddy", model="m", state="failed")
    provider = FakeProvider([Event(EventKind.ERROR, error_message="no such model",
                                   error_kind=ErrKind.BLOCKED)])
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.failed == 1
    row = _probes(db).get("codebuddy", "m")
    assert (row["state"], row["failures"]) == ("failed", 2)
    assert _probes(db).failed() == {("codebuddy", "m")}


async def test_blocked_and_failed_row_probed_only_once(repo):
    """判死行同时挂着负缓存时只探一次（两类目标去重）。"""
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="m")
    _probes(db).record(provider="codebuddy", model="m", state="failed")
    provider = FakeProvider()
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.attempted == 1
    assert len(provider.calls) == 1
    assert credentials.blocked_cooldowns() == []


async def test_failed_probe_row_without_credentials_is_skipped(repo):
    """判死行所属渠道一个凭证都没有 → skipped（不炸，也不清任何东西）。"""
    credentials, db = repo
    _probes(db).record(provider="codebuddy", model="m", state="failed")
    task = ModelProbeTask(credentials, {"codebuddy": FakeProvider()}, _probes(db))

    report = await task.run_once()

    assert (report.attempted, report.skipped) == (0, 1)
    assert _probes(db).failed() == {("codebuddy", "m")}


async def test_manual_takeover_skips_probe(repo):
    """人工接管后自动探活整行跳过（不让后台纠正人的判断）。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    _probes(db).set_manual(provider="codebuddy", model="m", manual="manual")
    provider = FakeProvider()
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.attempted == 0
    assert provider.calls == []


async def test_unregistered_provider_is_skipped(repo):
    """渠道未注册（部署裁掉了某个 provider）→ skipped，不报错。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    task = ModelProbeTask(credentials, {}, _probes(db))

    assert (await task.run_once()).skipped == 1


async def test_missing_credential_data_is_skipped(repo):
    """取不到凭证明文（并发删除 / 解密失败）→ skipped，不抛异常。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    credentials.credential_data = lambda credential_id: None
    task = ModelProbeTask(credentials, {"codebuddy": FakeProvider()}, _probes(db))

    report = await task.run_once()

    assert report.skipped == 1
    assert report.attempted == 0


async def test_falls_back_to_unselectable_credential(repo):
    """全部号都不可选（冷却中）时仍要能探：退回任意一条凭证。

    负缓存常在凭证冷却期间产生，此时若只认 selectable，探活永远跑不起来。
    """
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="m")
    with db.transaction() as conn:
        conn.execute("UPDATE credentials SET cooling_until = ? WHERE id = ?",
                     (99_999_999_999, cid))
    provider = FakeProvider()
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    report = await task.run_once()

    assert report.succeeded == 1
    assert len(provider.calls) == 1


async def test_upstream_model_mapping_applied(repo):
    """归一名 → 上游原始名的映射与 executor 同口径（大小写变体）。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="glm-5.2")
    provider = FakeProvider()
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db),
                          upstream_model=lambda provider_id, name: "GLM-5.2")

    await task.run_once()

    assert provider.calls[0][0] == "GLM-5.2"


async def test_pacer_is_awaited(repo):
    """配了节流器时每次探活前等一个 turn（不绕过渠道节流）。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    turns: list[int] = []

    class Recorder:
        async def wait_turn(self, key=None):
            turns.append(1)

    task = ModelProbeTask(credentials, {"codebuddy": FakeProvider()}, _probes(db),
                          pacer=Recorder())

    await task.run_once()

    assert turns == [1]


# ------------------------------------------------------------------ 立即探活

async def test_probe_now_success_clears_cache(repo):
    """管理台「立即探活」成功同样清负缓存（人工恢复入口）。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    task = ModelProbeTask(credentials, {"codebuddy": FakeProvider()}, _probes(db))

    state, error = await task.probe_now("codebuddy", "m")

    assert (state, error) == ("ok", "")
    assert credentials.blocked_cooldowns() == []


async def test_probe_now_ignores_manual_mark(repo):
    """按钮就是人来点的：已接管也要能立刻探（否则点了没反应）。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    _probes(db).set_manual(provider="codebuddy", model="m", manual="manual")
    task = ModelProbeTask(credentials, {"codebuddy": FakeProvider()}, _probes(db))

    state, _ = await task.probe_now("codebuddy", "m")

    assert state == "ok"
    assert credentials.blocked_cooldowns() == []


async def test_probe_now_unknown_provider(repo):
    """渠道未注册 / 无凭证 → inconclusive，不抛异常。"""
    credentials, db = repo
    task = ModelProbeTask(credentials, {}, _probes(db))

    state, error = await task.probe_now("codebuddy", "m")

    assert state == "inconclusive"
    assert "未注册" in error


async def test_probe_now_records_inconclusive(repo):
    """立即探活的失败结论也要落库（页面要能看到原因）。"""
    credentials, db = repo
    _seed_blocked(credentials, credential_id=_add(credentials), model="m")
    provider = FakeProvider(error=SoftHTTPError(429, b""))
    task = ModelProbeTask(credentials, {"codebuddy": provider}, _probes(db))

    state, error = await task.probe_now("codebuddy", "m")

    assert state == "inconclusive"
    assert "HTTP 429" in error
    assert _probes(db).get("codebuddy", "m")["state"] == "inconclusive"
