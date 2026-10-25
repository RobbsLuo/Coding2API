"""A2：上游恢复时刻（`Retry-After`）如何覆盖冷却时长。

全部用注入的 `now` 断言，不 sleep、不依赖真实时间；端到端用例走 executor
（假 provider 抛带 `retry_at` 的 `UpstreamHTTPError`），只读库里落下的
`cooling_until`。
"""

from __future__ import annotations

import logging
import time

import pytest

from src.compat.openai.request import parse_chat_request
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import (
    MODEL_COOLDOWN_SECONDS,
    OTHER_COOLDOWN_SECONDS,
    PLAN_COOLDOWN_SECONDS,
    RETRY_AT_MAX_SECONDS,
    SOFT_COOLDOWN_SECONDS,
    Candidate,
    Scheduler,
    clamp_retry_at,
    next_credit_reset,
)
from src.provider.base import ErrKind, Event, EventKind
from src.provider.trae.client import UpstreamHTTPError

NOW = 1_000_000


def cand(cid: str = "c1", **kw) -> Candidate:
    base = {"credential_id": cid, "provider": "trae"}
    base.update(kw)
    return Candidate(**base)


# --------------------------------------------------------------- 钳制纯函数


def test_clamp_keeps_constant_when_no_hint():
    assert clamp_retry_at(None, NOW, 60) == (NOW + 60, False)


@pytest.mark.parametrize("retry_at", [0, NOW - 3600, NOW, NOW + 30, NOW + 59])
def test_clamp_falls_back_for_past_or_shorter_than_constant(retry_at):
    """`Retry-After: 0` / 过去时刻 / 比常量还短 → 一律回落常量。

    防的是「上游越是限流、轮询越密集」：照抄 0 会让冷却刚写下就过期。
    """
    assert clamp_retry_at(retry_at, NOW, 60) == (NOW + 60, False)


def test_clamp_adopts_hint_between_bounds():
    assert clamp_retry_at(NOW + 7200, NOW, 60) == (NOW + 7200, True)


def test_clamp_caps_hint_at_24h():
    """上界 24h：上游报「下个月恢复」也不该把一个号移出池子整整一个月。"""
    far = NOW + 90 * 86400
    assert clamp_retry_at(far, NOW, 60) == (NOW + RETRY_AT_MAX_SECONDS, True)
    assert clamp_retry_at(NOW + RETRY_AT_MAX_SECONDS, NOW, 60) == (
        NOW + RETRY_AT_MAX_SECONDS, True)


# ------------------------------------------------------------- 账号级冷却


def test_soft_cooldown_uses_upstream_moment():
    out = Scheduler().note_error(cand(), ErrKind.SOFT, NOW, retry_at=NOW + 7200)
    assert out.cooling_until == NOW + 7200
    assert out.err_count == 0                       # SOFT 仍不累计


def test_soft_cooldown_without_hint_unchanged():
    out = Scheduler().note_error(cand(), ErrKind.SOFT, NOW)
    assert out.cooling_until == NOW + SOFT_COOLDOWN_SECONDS


def test_soft_cooldown_past_hint_falls_back():
    out = Scheduler().note_error(cand(), ErrKind.SOFT, NOW, retry_at=NOW - 5)
    assert out.cooling_until == NOW + SOFT_COOLDOWN_SECONDS


def test_plan_cooldown_uses_upstream_moment():
    out = Scheduler().note_error(cand(), ErrKind.PLAN, NOW, retry_at=NOW + 86400)
    assert out.cooling_until == NOW + 86400
    assert out.cooling_until != NOW + PLAN_COOLDOWN_SECONDS
    assert out.err_count == 0


def test_credit_cooldown_uses_upstream_moment():
    """余额不足原本对齐次日 04:00；上游给了更晚的时刻则听上游（仍钳制 24h）。"""
    reset = next_credit_reset(NOW)
    out = Scheduler().note_error(cand(), ErrKind.CREDIT, NOW, retry_at=reset + 600)
    assert out.cooling_until == reset + 600
    # 早于次日 04:00 的上游时刻不采用（本地策略更保守）
    out = Scheduler().note_error(cand(), ErrKind.CREDIT, NOW, retry_at=NOW + 60)
    assert out.cooling_until == reset


def test_credit_cooldown_caps_far_upstream_moment():
    out = Scheduler().note_error(cand(), ErrKind.CREDIT, NOW,
                                 retry_at=NOW + 10 * 86400)
    assert out.cooling_until == NOW + RETRY_AT_MAX_SECONDS


def test_other_threshold_uses_upstream_moment():
    """累计到阈值时的中冷却同样可被上游时刻覆盖；累计计数逻辑不变。"""
    out = Scheduler().note_error(cand(err_count=2), ErrKind.OTHER, NOW,
                                 retry_at=NOW + 3600)
    assert out.cooling_until == NOW + 3600
    assert out.err_count == 0
    # 未到阈值：只累计，不产生冷却（也不看 retry_at）
    out = Scheduler().note_error(cand(err_count=0), ErrKind.OTHER, NOW,
                                 retry_at=NOW + 3600)
    assert out.cooling_until is None and out.err_count == 1
    assert out.cooling_until != NOW + OTHER_COOLDOWN_SECONDS


def test_dead_and_request_ignore_retry_at():
    assert Scheduler().note_error(cand(), ErrKind.DEAD, NOW,
                                  retry_at=NOW + 3600).disabled is True
    out = Scheduler().note_error(cand(err_count=1), ErrKind.REQUEST, NOW,
                                 retry_at=NOW + 3600)
    assert out.cooling_until is None and out.err_count == 1


# --------------------------------------------------------- 模型级冷却作用域


def test_model_cooldown_uses_upstream_moment():
    out = Scheduler().note_error(cand(), ErrKind.MODEL, NOW, model="m",
                                 retry_at=NOW + 3 * 3600)
    assert out.cooling_until is None                 # 不动账号级
    assert out.model_cooldowns["m"].cooling_until == NOW + 3 * 3600
    assert out.model_cooldowns["m"].reason == "model"


def test_model_cooldown_short_hint_falls_back_to_constant():
    out = Scheduler().note_error(cand(), ErrKind.MODEL, NOW, model="m",
                                 retry_at=NOW + 5)
    assert out.model_cooldowns["m"].cooling_until == NOW + MODEL_COOLDOWN_SECONDS


def test_blocked_cooldown_uses_upstream_moment():
    """负缓存本是 6h 起；上游给更晚时刻则听上游（同样双端钳制）。"""
    out = Scheduler().note_error(cand(), ErrKind.BLOCKED, NOW, model="m",
                                 retry_at=NOW + 7 * 3600)
    assert out.model_cooldowns["m"].cooling_until == NOW + 7 * 3600
    assert out.model_cooldowns["m"].reason == "blocked"
    # 比 6h 常量还短的上游时刻回落常量（负缓存不能被上游缩短）
    out = Scheduler().note_error(cand(), ErrKind.BLOCKED, NOW, model="m",
                                 retry_at=NOW + 9000)
    assert out.model_cooldowns["m"].cooling_until == NOW + 6 * 3600


def test_concurrency_cooldown_can_be_extended_by_upstream():
    """并发打满本是固定 60s 不翻倍；上游明确说更晚恢复时按上游（仍 24h 封顶）。"""
    out = Scheduler().note_error(cand(), ErrKind.CONCURRENCY, NOW, model="m",
                                 retry_at=NOW + 300)
    assert out.model_cooldowns["m"].cooling_until == NOW + 300
    out = Scheduler().note_error(cand(), ErrKind.CONCURRENCY, NOW, model="m")
    assert out.model_cooldowns["m"].cooling_until == NOW + 60


def test_model_scoped_without_model_degrades_to_account_soft():
    out = Scheduler().note_error(cand(), ErrKind.MODEL, NOW, retry_at=NOW + 1200)
    assert out.model_cooldowns is None
    assert out.cooling_until == NOW + 1200


# ------------------------------------------------------------------ 日志口径


def test_adopted_moment_is_logged(caplog):
    with caplog.at_level(logging.INFO, logger="src.engine.scheduler"):
        Scheduler().note_error(cand(), ErrKind.SOFT, NOW, retry_at=NOW + 7200)
    assert any("采用上游时刻" in record.getMessage() for record in caplog.records)


def test_adopted_moment_log_says_account_scope_for_soft(caplog):
    """账号级分类即使传了模型名，日志也必须写「账号」。

    实测 zen 免费层 429（SOFT）落的是账号级 `cooling_until`，旧日志却打
    「模型 glm-5.3-flash-free」，读起来像模型级限流，排查时误判。
    """
    with caplog.at_level(logging.INFO, logger="src.engine.scheduler"):
        Scheduler().note_error(cand(), ErrKind.SOFT, NOW, model="glm-5.3-flash-free",
                               retry_at=NOW + 7200)
    message = next(r.getMessage() for r in caplog.records if "采用上游时刻" in r.getMessage())
    assert "作用域 账号" in message
    assert "glm-5.3-flash-free" not in message


def test_adopted_moment_log_says_model_scope_for_model_kind(caplog):
    """模型级分类写模型名，与 `credential_model_cooldowns` 的写入位置一致。"""
    with caplog.at_level(logging.INFO, logger="src.engine.scheduler"):
        Scheduler().note_error(cand(), ErrKind.MODEL, NOW, model="deepseek-v4.1-flash",
                               retry_at=NOW + 7200)
    message = next(r.getMessage() for r in caplog.records if "采用上游时刻" in r.getMessage())
    assert "模型 deepseek-v4.1-flash" in message
    assert "作用域 账号" not in message


def test_adopted_moment_log_says_account_when_model_scoped_kind_lacks_model(caplog):
    """MODEL 无模型名时已退化为账号级软冷却，日志不能还写模型级。"""
    with caplog.at_level(logging.INFO, logger="src.engine.scheduler"):
        Scheduler().note_error(cand(), ErrKind.MODEL, NOW, retry_at=NOW + 7200)
    message = next(r.getMessage() for r in caplog.records if "采用上游时刻" in r.getMessage())
    assert "作用域 账号" in message


def test_fallback_is_not_logged(caplog):
    with caplog.at_level(logging.INFO, logger="src.engine.scheduler"):
        Scheduler().note_error(cand(), ErrKind.SOFT, NOW)
    assert not [r for r in caplog.records if "采用上游时刻" in r.getMessage()]


# ------------------------------------------------------- 端到端：429 + Retry-After


class _RetryProvider:
    id = "trae"

    def __init__(self, retry_at: int | None, *, always: bool = False) -> None:
        self.retry_at = retry_at
        self.always = always
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        self.calls += 1
        if self.always or self.calls == 1:
            raise UpstreamHTTPError(429, b'{"error":"slow down"}',
                                    retry_at=self.retry_at,
                                    retry_after_seconds=7200.0)
        yield Event(kind=EventKind.CONTENT, content="hi")
        yield Event(kind=EventKind.FINISH, finish_reason="stop")


async def test_executor_adopts_retry_after_into_credential_cooldown(repo):
    """假上游 429 + `Retry-After: 7200` → 该凭证冷却约 2h（用 now 断言，不 sleep）。"""
    credentials, db = repo
    first = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    second = credentials.add(provider="trae", credential_data={"accessToken": "b"})
    before = int(time.time())
    provider = _RetryProvider(retry_at=before + 7200)
    executor = Executor(ExecutorDeps(providers={"trae": provider},
                                     credentials=credentials, scheduler=Scheduler(),
                                     default_model="glm-5.2"))
    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"
    assert provider.calls == 2
    row = db.connect().execute(
        "SELECT id, cooling_until FROM credentials WHERE cooling_until IS NOT NULL"
    ).fetchall()
    assert len(row) == 1
    cooled_id, cooling_until = row[0]["id"], row[0]["cooling_until"]
    # 被冷却的必须是第一个被尝试的凭证；具体是哪一个取决于调度排序
    assert cooled_id in (first, second)
    assert before + 7000 <= cooling_until <= int(time.time()) + 7200
    db.close()


async def test_executor_without_header_keeps_constant_cooldown(repo):
    """无 `Retry-After` 时行为与今天逐字节一致：SOFT 仍是 60s。"""
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    before = int(time.time())
    provider = _RetryProvider(retry_at=None)
    executor = Executor(ExecutorDeps(providers={"trae": provider},
                                     credentials=credentials, scheduler=Scheduler(),
                                     default_model="glm-5.2"))
    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    cooling_until = db.connect().execute(
        "SELECT cooling_until FROM credentials WHERE cooling_until IS NOT NULL"
    ).fetchone()["cooling_until"]
    assert before + SOFT_COOLDOWN_SECONDS <= cooling_until <= (
        int(time.time()) + SOFT_COOLDOWN_SECONDS)
    db.close()


async def test_executor_all_credentials_rate_limited_still_503(repo):
    """所有候选都被上游时刻冷却后仍按原路径 503（不因新参数变成死循环）。"""
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})

    executor = Executor(ExecutorDeps(
        providers={"trae": _RetryProvider(int(time.time()) + 7200, always=True)},
        credentials=credentials, scheduler=Scheduler(max_rotate=1),
        default_model="glm-5.2"))
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    db.close()
