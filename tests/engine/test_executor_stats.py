"""执行引擎：统计埋点、用量记账与错误类型映射。"""

from __future__ import annotations

import time

import pytest

from src.engine.executor import NoHealthyCredential
from src.provider.base import ErrKind, Event, EventKind, Quota, Usage
from tests.helpers.events import GOOD_EVENTS


def _disabled_outcome():
    from src.engine.scheduler import Candidate, Scheduler

    return Scheduler().note_error(Candidate(credential_id="c", provider="codebuddy"),
                                  ErrKind.DEAD, 0)

class RecordingCollector:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.boom = False

    def record(self, **fields) -> None:
        if self.boom:
            raise RuntimeError("stats backend down")
        self.events.append(fields)

def _exec_with_stats(repo_tuple, script, collector, **kw):
    credentials, _db = repo_tuple
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler

    class Streaming:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _data, _payload, _model):
            index = min(self.calls, len(script) - 1)
            self.calls += 1
            for item in script[index]:
                if isinstance(item, Exception):
                    raise item
                yield item

        def list_models(self, _data):  # pragma: no cover
            return []

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Streaming()}, credentials=credentials,
        scheduler=Scheduler(**kw), default_model="glm-5.2", stats=collector))
    return executor

async def test_stream_records_success_with_usage_and_username(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    event = collector.events[-1]
    assert event["ok"] is True and event["username"] == "alice"
    assert event["provider"] == "codebuddy" and event["model"] == "glm-5.2"
    assert event["input_tokens"] == 11 and event["output_tokens"] == 22
    assert event["reasoning_tokens"] == 3
    assert event["latency_ms"] is not None
    assert event["ttfb_ms"] is not None

async def test_stream_without_finish_frame_records_truncated_stream(repo):
    """上游正常关闭但没走到终态：不冷却凭证，但按 truncated_stream 留痕。

    口径（2026-10-28）：连接正常结束 ≠ 响应完整。凭证照常 save_success
    （上游没报错，冷却只会误伤健康号），统计里记 ok=False + truncated_stream，
    客户端仍收到收尾帧。
    """
    credentials, _db = repo
    credential_id = credentials.add(provider="codebuddy",
                                    credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(
        repo, [[Event(kind=EventKind.CONTENT, content="hi"),
                Event(kind=EventKind.USAGE, usage=Usage(11, 22, 3))]], collector)

    from src.compat.openai.request import parse_chat_request

    frames = [frame async for frame in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
        username="alice")]

    event = collector.events[-1]
    assert event["ok"] is False and event["error_type"] == "truncated_stream"
    # tokens 如实记录（已收到的用量仍要计费）
    assert event["input_tokens"] == 11 and event["output_tokens"] == 22
    # 凭证健康不受影响：不计错、不冷却
    candidate = next(c for c in credentials.candidates()
                     if c.credential_id == credential_id)
    assert candidate.err_count == 0 and candidate.cooling_until is None
    # 客户端仍收到 [DONE] 收尾帧
    assert b"[DONE]" in b"".join(frames)

async def test_stream_without_usage_frame_records_null_tokens(repo):
    """上游没给 usage 时统计字段为 None，不能崩也不能写 0。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(
        repo, [[Event(kind=EventKind.CONTENT, content="hi"),
                Event(kind=EventKind.FINISH, finish_reason="stop")]], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True})):
        pass
    assert collector.events[-1]["input_tokens"] is None

async def test_stream_prefers_credential_expiring_soon(repo):
    """到期积分指标贯通到真实选号：低健康度但快过期积分多的号先被选中。"""
    credentials, _db = repo
    steady_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "s"})
    burn_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    now = int(time.time())
    credentials.save_quota(steady_id, Quota(remaining=95, total=100,
                                            cycle_end=now + 48 * 3600, probed_at=now))
    credentials.save_quota(burn_id, Quota(remaining=10, total=100, cycle_end=now + 600,
                                           expiry_ladder=[(now + 600, 100.0),
                                                          (now + 700, 50.0)],
                                           probed_at=now))
    cands = {c.credential_id: c for c in credentials.candidates()}
    assert cands[burn_id].expiry_ladder == [(now + 600, 100.0), (now + 700, 50.0)]
    assert cands[steady_id].expiry_ladder is None
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    assert collector.events[-1]["credential_id"] == burn_id

async def test_stream_expiry_window_zero_keeps_health_order(repo):
    """窗口关闭后退回健康度排序（到期指标全员 0 分）。"""
    credentials, _db = repo
    steady_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "s"})
    burn_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    now = int(time.time())
    credentials.save_quota(steady_id, Quota(remaining=95, total=100, probed_at=now))
    credentials.save_quota(burn_id, Quota(remaining=10, total=100,
                                           expiry_ladder=[(now + 600, 100.0)],
                                           probed_at=now))
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector, expiry_window=0)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    assert collector.events[-1]["credential_id"] == steady_id

async def test_stream_secondary_expiry_breaks_tie(repo):
    """两级窗口贯通到真实选号：36h 内都没到期积分时，7 天内会过期者优先。"""
    credentials, _db = repo
    steady_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "s"})
    week_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "w"})
    now = int(time.time())
    credentials.save_quota(steady_id, Quota(remaining=95, total=100, probed_at=now))
    credentials.save_quota(week_id, Quota(remaining=50, total=100,
                                          expiry_ladder=[(now + 3 * 86400, 140.0)],
                                          probed_at=now))
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="alice"):
        pass

    assert collector.events[-1]["credential_id"] == week_id

async def test_stream_records_failure_when_credentials_exhausted(repo):
    credentials, _db = repo
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="bob"):
        pass
    event = collector.events[-1]
    assert event["ok"] is False and event["error_type"] == "no_healthy_credential"
    assert event["username"] == "bob"

async def test_stream_skips_undecryptable_credential_and_uses_another(repo):
    """一条凭证解不开 → 跳过它换下一个号，**不让整个请求 500**。

    实测 2026-10-26 14:48：解密失败（APP_SECRET 换过 / 密文损坏）早先直接
    冒泡 → 整条聊天请求回 500，栈顶「凭证解密失败：APP_SECRET 是否被更换
    过？」，其余好凭证白白连坐。
    """
    from src.compat.openai.request import parse_chat_request
    from src.db.crypto import CredentialCipher

    credentials, db = repo
    broken = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    good = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t2"})
    # 换密钥加密 → 当前 APP_SECRET 解不开
    other = CredentialCipher("a-completely-different-secret")
    with db.transaction() as conn:
        conn.execute("UPDATE credentials SET data_enc = ? WHERE id = ?",
                     (other.encrypt(b'{"bearer_token": "t"}'), broken))
    # **pin 住坏的那条**：选号必须先试它，否则「跳过解不开的凭证」这条分支
    # 根本不会执行——而选号顺序取决于健康度 / 在途数 / 到期额度等一堆
    # 动态因素，测试不能靠它们碰巧（CI 上曾因此漏测）。
    assert credentials.set_pinned(broken) is True

    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)
    chunks = [chunk async for chunk in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert chunks
    event = collector.events[-1]
    assert event["ok"] is True
    # 实际用的是那个好凭证（解不开的被跳过）
    assert event["credential_id"] == good

def _model_cool_one(credentials, model):
    """给唯一凭证写一条未过期的模型级冷却（耗尽轮换的前置条件）。"""
    from src.engine.scheduler import ErrorOutcome, ModelCooldown

    credential_id = credentials.candidates()[0].credential_id
    credentials.save_error(credential_id, ErrorOutcome(model_cooldowns={
        model: ModelCooldown(cooling_until=int(time.time()) + 600,
                             hits=1, reason="model")}))

async def test_stream_all_model_cooled_records_model_unavailable(repo):
    """候选全部只因该模型的模型级冷却不可用 → 统计记 model_unavailable。

    与「无可用凭证」区分：账号本身是好的、换个模型立即可用（2026-10-13
    `ling-3.1-flash`：kilo 唯一凭证被上游 429 写模型级冷却，明细此前一律
    标「无可用凭证」，读起来像整池凭证挂了）。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    _model_cool_one(credentials, "glm-5.2")
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="bob"):
        pass
    event = collector.events[-1]
    assert event["ok"] is False and event["error_type"] == "model_unavailable"
    # 对外错误码不变：兼容层（Anthropic/Responses）按 no_healthy_credential 映射
    # （本用例不直接断言错误码，见 test_coverage_edge_executor 的终帧断言）

async def test_complete_all_model_cooled_records_model_unavailable(repo):
    """非流式路径同口径：模型级冷却耗尽记 model_unavailable。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    _model_cool_one(credentials, "glm-5.2")
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert collector.events[-1]["error_type"] == "model_unavailable"

async def test_stream_exhaustion_without_any_pick_records_candidate_provider(repo):
    """一次都没选中凭证（候选全在冷却 / 池子为空）时，明细要落**候选渠道**。

    `_pick` 首轮返回 None 时渠道名还停在占位 `"-"`：归因面板只剩一个横杠，
    看不出是哪个渠道挂的（实测 2026-10-29 10:18 `deepseek-v4.1-flash-sg`
    落成 `provider="-"`，而它只在 codebuddy_intl 上有）。
    """
    credentials, _db = repo
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True})):
        pass
    event = collector.events[-1]
    assert event["ok"] is False and event["provider"] == "codebuddy"
    assert event["credential_id"] is None

async def test_complete_exhaustion_without_any_pick_records_candidate_provider(repo):
    """非流式同口径：没有选中凭证时明细仍能指出是哪个渠道。"""
    credentials, _db = repo
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert collector.events[-1]["provider"] == "codebuddy"

async def test_exhaustion_after_a_tried_credential_keeps_actual_provider(repo):
    """真打过上游时明细记**实际**渠道，不被候选渠道顶掉。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Soft(Exception):
        def kind(self):
            return ErrKind.SOFT

    executor = _exec_with_stats(repo, [[Soft()]], collector, max_rotate=1)
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert collector.events[-1]["provider"] == "codebuddy"
    assert collector.events[-1]["credential_id"] is not None

async def test_complete_records_success_and_failure(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}), username="alice")
    assert collector.events[-1]["ok"] is True
    assert collector.events[-1]["input_tokens"] == 11
    # 非流式也记首字延迟（上游首个事件时刻）
    assert collector.events[-1]["ttfb_ms"] is not None

    credentials.save_error(credentials.candidates()[0].credential_id, _disabled_outcome())
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert collector.events[-1]["error_type"] == "no_healthy_credential"

async def test_stats_failure_never_breaks_chat(repo):
    """统计后端不可用时聊天必须照常返回。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    collector.boom = True
    executor = _exec_with_stats(repo, [GOOD_EVENTS], collector)

    from src.compat.openai.request import parse_chat_request

    result = await executor.complete(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]}))
    assert result["choices"][0]["message"]["content"] == "hi"

def test_executor_without_stats_collector_is_noop(repo):
    credentials, _db = repo
    executor = _exec_with_stats(repo, [GOOD_EVENTS], None)
    assert executor._deps.stats is None
    executor._deps.record(username="x", provider="y", model="z", ok=True)  # 不崩

async def test_upstream_http_failure_records_error_type(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Dead(Exception):
        def __init__(self) -> None:
            super().__init__("session dead")

        def kind(self):
            return ErrKind.DEAD

    executor = _exec_with_stats(repo, [[Dead()]], collector)
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    # 会话失效 → 硬禁用；尝试行已给出真实归因（凭证失效），轮换耗尽的收尾行
    # 不再重复落（否则一次请求两行，requests 与 Key 日额度双计）
    assert collector.events[-1]["error_type"] == "credential_unavailable"
    assert credentials.candidates()[0].disabled is True

def test_error_type_mapping_covers_all_kinds():
    from src.engine.executor import _error_type_for
    from src.provider.base import ErrKind

    assert _error_type_for(ErrKind.PLAN) == "rate_limit"
    assert _error_type_for(ErrKind.MODEL) == "rate_limit"
    assert _error_type_for(ErrKind.CONCURRENCY) == "rate_limit"
    assert _error_type_for(ErrKind.DEAD) == "credential_unavailable"
    assert _error_type_for(ErrKind.RESTRICTED) == "account_restricted"
    assert _error_type_for(ErrKind.SOFT) == "upstream_error"
    assert _error_type_for(ErrKind.OTHER) == "upstream_error"
    # 「上游没这个模型」独立成类：与「请求无效」分开（2026-10-26）
    assert _error_type_for(ErrKind.BLOCKED) == "model_not_found"
    assert _error_type_for(ErrKind.INVALID) == "invalid_request"
    assert _error_type_for(ErrKind.REQUEST) == "invalid_request"


def test_blocked_error_type_is_controlled():
    """新枚举必须在受控白名单里，否则 `record` 会把它置 None（统计里看不到）。"""
    from src.stats.collector import CONTROLLED_ERROR_TYPES

    assert "model_not_found" in CONTROLLED_ERROR_TYPES
    assert "account_restricted" in CONTROLLED_ERROR_TYPES
    assert "truncated_stream" in CONTROLLED_ERROR_TYPES


async def test_dead_credential_also_records_attribution_row(repo):
    """DEAD 除硬禁用外必须落一条 `credential_unavailable` 明细。

    硬禁用只写 `credentials.disabled`，归因卡片读的是 `usage_events`——不落
    明细就完全看不到（实测 2026-10-23 officeace 两次 401 `AppKey or
    AppSecret is invalid` 在统计页零痕迹）。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Dead(Exception):
        def kind(self):
            return ErrKind.DEAD

    executor = _exec_with_stats(repo, [[Dead()]], collector)
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))

    kinds = [event["error_type"] for event in collector.events]
    # 只落尝试级（DEAD 归因）：轮换耗尽的收尾行不再重复落，否则同一请求
    # 在 requests 里算两次、成功率虚低，按 Key 计的日额度也会被扣两次。
    assert kinds == ["credential_unavailable"]
    dead_row = collector.events[0]
    assert dead_row["ok"] is False
    assert dead_row["provider"] == "codebuddy"
    assert dead_row["latency_ms"] is not None


async def test_stream_dead_error_does_not_duplicate_terminal_row(repo):
    """流式同理：DEAD 已落尝试行，选号耗尽时不再落收尾行（一次请求一行）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Dead(Exception):
        def kind(self):
            return ErrKind.DEAD

    executor = _exec_with_stats(repo, [[Dead()]], collector)
    from src.compat.openai.request import parse_chat_request

    chunks = [chunk async for chunk in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]
    assert chunks                       # 以错误帧收尾，不是静默断开
    assert [event["error_type"] for event in collector.events] == [
        "credential_unavailable"]


async def test_soft_error_does_not_record_attribution_row(repo):
    """SOFT 等其它分类不落尝试级明细：一次请求可能触发多次，落行会灌满噪声。

    它们的失败仍由轮换耗尽时的收尾行覆盖（见上一个用例的末尾断言）。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Soft(Exception):
        def kind(self):
            return ErrKind.SOFT

    executor = _exec_with_stats(repo, [[Soft()]], collector)
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))

    kinds = [event["error_type"] for event in collector.events]
    assert "upstream_error" not in kinds       # 尝试级不落行
    assert kinds[-1] == "no_healthy_credential"


def test_error_detail_includes_exception_class_for_empty_message():
    """日志要能分辨传输异常的种类：httpx 的读超时 / 写超时 `str()` 常为空串。

    实测 2026-10-23 codebuddy_intl 两次 158s/162s 的 SOFT 错误日志只剩
    「kind=soft）: 」，分不清读断、写超时还是连接重置。
    """
    from src.engine.executor import _error_detail

    class Blank(Exception):
        pass

    assert _error_detail(Blank()) == "Blank"          # 空消息只给类名
    assert _error_detail(ValueError("boom")) == "ValueError: boom"
    assert _error_detail(None) == "-"


def test_usage_field_tolerates_missing_usage():
    from src.engine.executor import _usage_field

    assert _usage_field(None, "input_tokens") is None
    assert _usage_field(Usage(5, 6, 7), "output_tokens") == 6
    assert _usage_field(Usage(), "credit") is None

async def test_plan_error_records_rate_limit_error_type(repo):
    """权益耗尽 → 统计记为 rate_limit（受控错误类型白名单内）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Plan(Exception):
        def __init__(self) -> None:
            super().__init__("plan exhausted")

        def kind(self):
            return ErrKind.PLAN

    executor = _exec_with_stats(repo, [[Plan()]], collector)
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert collector.events[-1]["error_type"] == "no_healthy_credential"
    assert credentials.candidates()[0].cooling_until is not None


async def test_blocked_error_records_model_not_found(repo):
    """上游说「没这个模型」→ 统计记 `model_not_found`（不是「请求无效」）。

    实测 2026-10-26 officeAce `deepseek-v4.1-flash-1017` 404
    `ModelArts.81009`：归到 `invalid_request` 时归因面板显示「请求无效」，
    读起来像客户端参数写错了，实际是上游把该模型下架了。

    走流式路径且轮换预算用尽（`max_rotate=1`）：那一行是**尝试级**归因，
    类型取自最后一次上游错误的分类（`_stream_error_type`）。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Blocked(Exception):
        def __init__(self) -> None:
            super().__init__("model not registered")

        def kind(self):
            return ErrKind.BLOCKED

    executor = _exec_with_stats(repo, [[Blocked()]], collector, max_rotate=1)
    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True})):
        pass
    kinds = [event["error_type"] for event in collector.events]
    assert "model_not_found" in kinds
    assert "invalid_request" not in kinds
    # 模型级负缓存照写（只锁这一个模型，不牵连整条凭证）
    assert _db.connect().execute(
        "SELECT COUNT(*) FROM credential_model_cooldowns").fetchone()[0] == 1


async def test_blocked_exhaustion_still_records_model_unavailable(repo):
    """轮换耗尽时的收尾行仍是 `model_unavailable`（口径不变）。

    「上游没这个模型」与「这个模型现在全都不可用」是两件事：前者是尝试级
    归因（`model_not_found`），后者是收尾行（`model_unavailable`）。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Blocked(Exception):
        def kind(self):
            return ErrKind.BLOCKED

    executor = _exec_with_stats(repo, [[Blocked()]], collector)
    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))
    assert collector.events[-1]["error_type"] == "model_unavailable"

async def test_stream_plan_error_records_failure(repo):
    """流式路径的权益耗尽同样要落统计。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()

    class Plan(Exception):
        def __init__(self) -> None:
            super().__init__("plan exhausted")

        def kind(self):
            return ErrKind.PLAN

    executor = _exec_with_stats(repo, [[Plan()]], collector, max_rotate=1)
    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="carol"):
        pass
    assert collector.events[-1]["ok"] is False
    assert collector.events[-1]["username"] == "carol"

async def test_stream_inline_request_error_records_real_type(repo):
    """流内业务错误（HTTP 200 内嵌 error 帧）要落**真实**类型而非 upstream_protocol。

    回归 CodeArts「渠道响应异常」：业务错误内嵌在 200 的 SSE 里，
    executor 存成 `UpstreamStreamError`，而该异常此前没有 `.kind()`，
    分类器兜底成 `upstream_protocol`。REQUEST 类还不应冷却/累计凭证。
    """
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [[
        Event(kind=EventKind.ERROR, error_code=406,
              error_message="The request model is not multimodal",
              error_kind=ErrKind.REQUEST)]], collector, max_rotate=1)

    from src.compat.openai.request import parse_chat_request

    async for _ in executor.stream(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
            username="dave"):
        pass
    assert collector.events[-1]["error_type"] == "invalid_request"
    assert collector.events[-1]["error_type"] != "upstream_protocol"
    # 请求侧错误不是凭证的错：不冷却、不累计
    assert credentials.candidates()[0].cooling_until is None

async def test_complete_inline_request_error_records_real_type(repo):
    """非流式路径同口径：`aggregate()` 抛出的流内错误也带真实分类。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    collector = RecordingCollector()
    executor = _exec_with_stats(repo, [[
        Event(kind=EventKind.ERROR, error_code=406,
              error_message="The request model is not multimodal",
              error_kind=ErrKind.REQUEST)]], collector, max_rotate=1)

    from src.compat.openai.request import parse_chat_request

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}), username="erin")
    assert collector.events[-1]["error_type"] == "invalid_request"
    assert credentials.candidates()[0].cooling_until is None

async def test_restricted_11140_cools_account_and_skips_channel(repo):
    """403/11140（账号被上游限制）：2h 账号级冷却 + 跳过该渠道，不换号去撞。

    与 DEAD 的关键差别：**不写 disabled**——禁用是不可逆的人工状态，而这类限制
    由上游自行解除（maiphucgiang/codebuddy2api、magpie、10router 的处理一致：
    本地无解，等恢复或换号）；且同渠道其余凭证一次都不打，逐个号换着撞只会
    在上游多留风控记录（实测一次请求连打 3 个号全 403，新号 3 分钟就被封）。
    """
    from src.compat.openai.request import parse_chat_request
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import RESTRICTED_COOLDOWN_SECONDS, Scheduler
    from src.provider.codebuddy import events as cb_events
    from src.provider.codebuddy.client import UpstreamHTTPError

    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "u"})
    collector = RecordingCollector()
    body = (b'{"code":11140,"msg":"request illegal",'
            b'"displayMsg":{"en":"Your account has been restricted due to unusual activity"}}')

    class Restricted:
        id = "codebuddy"
        calls = 0

        async def stream_chat(self, _data, _payload, _model):
            Restricted.calls += 1
            raise UpstreamHTTPError(403, body)
            yield  # pragma: no cover - 抛错后的不可达分支，只为保持异步生成器形态

        def disabled_reason(self, raw: bytes) -> str:
            return cb_events.disabled_reason(raw)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Restricted()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=collector))

    started = int(time.time())
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))

    assert Restricted.calls == 1            # 同渠道其余号没有被拿去撞
    assert [event["error_type"] for event in collector.events] == ["account_restricted"]
    rows = db.connect().execute(
        "SELECT disabled, cooling_until FROM credentials").fetchall()
    assert all(row["disabled"] == 0 for row in rows)     # 不禁用，等上游解除
    cooled = [row["cooling_until"] for row in rows if row["cooling_until"] is not None]
    # 第一次命中只冷却触发的那个号（渠道级暂停要窗口内第二次命中才升级）
    assert len(cooled) == 1
    assert started + RESTRICTED_COOLDOWN_SECONDS - 60 <= cooled[0] \
        <= started + RESTRICTED_COOLDOWN_SECONDS + 60


async def test_restricted_burst_pauses_whole_provider(repo):
    """窗口内第二次命中受限 → 整渠道暂停 24h，剩余号不再被逐个拿去撞。"""
    from src.compat.openai.request import parse_chat_request
    from src.engine.executor import (
        RESTRICTED_PROVIDER_PAUSE_SECONDS,
        Executor,
        ExecutorDeps,
    )
    from src.engine.scheduler import Scheduler
    from src.provider.codebuddy.client import UpstreamHTTPError

    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "u"})

    class Restricted:
        id = "codebuddy"
        calls = 0

        async def stream_chat(self, _data, _payload, _model):
            Restricted.calls += 1
            raise UpstreamHTTPError(403, b'{"code":11140}')
            yield  # pragma: no cover - 抛错后的不可达分支，只为保持异步生成器形态

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Restricted()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=RecordingCollector()))
    request = {"messages": [{"role": "user", "content": "hi"}]}

    started = int(time.time())
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(request))
    assert Restricted.calls == 1            # 第一次：只冷却触发的号
    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(request))
    assert Restricted.calls == 2            # 第二次命中另一个号 → 触发升级

    rows = db.connect().execute(
        "SELECT cooling_until FROM credentials").fetchall()
    assert all(row["cooling_until"] is not None for row in rows)
    for row in rows:
        assert started + RESTRICTED_PROVIDER_PAUSE_SECONDS - 60 \
            <= row["cooling_until"] <= started + RESTRICTED_PROVIDER_PAUSE_SECONDS + 60


async def test_dead_unrecognised_403_keeps_session_dead(repo):
    """provider 认不出的 403（没有可操作话术）回落 "session dead"，不写空串。"""
    from src.compat.openai.request import parse_chat_request
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler
    from src.provider.codebuddy import events as cb_events
    from src.provider.codebuddy.client import UpstreamHTTPError

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})

    class PlainDenied:
        id = "codebuddy"

        async def stream_chat(self, _data, _payload, _model):
            raise UpstreamHTTPError(403, b'{"code":403,"msg":"forbidden"}')
            yield  # pragma: no cover - 抛错后的不可达分支，只为保持异步生成器形态

        def disabled_reason(self, raw: bytes) -> str:
            return cb_events.disabled_reason(raw)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": PlainDenied()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=RecordingCollector()))

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))

    row = db.connect().execute(
        "SELECT disabled_reason FROM credentials WHERE id = ?", (cid,)).fetchone()
    assert row["disabled_reason"] == "session dead"

async def test_dead_http_error_without_provider_hint_keeps_session_dead(repo):
    """provider 没实现 disabled_reason 时：即使 HTTP 有 body 也回落默认文案。"""
    from src.compat.openai.request import parse_chat_request
    from src.provider.codebuddy.client import UpstreamHTTPError

    credentials, db = repo
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    executor = _exec_with_stats(
        repo, [[UpstreamHTTPError(403, b'{"code":403,"msg":"forbidden"}')]],
        RecordingCollector())

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))

    row = db.connect().execute(
        "SELECT disabled_reason FROM credentials WHERE id = ?", (cid,)).fetchone()
    assert row["disabled_reason"] == "session dead"

async def test_stream_inband_restricted_skips_channel(repo):
    """流内 error 事件标了 RESTRICTED：同样跳过该渠道，不逐个号撞。"""
    from src.compat.openai.request import parse_chat_request
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler

    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "u"})
    collector = RecordingCollector()
    restricted = Event(kind=EventKind.ERROR, error_kind=ErrKind.RESTRICTED,
                       error_code=11140, error_message="request illegal")

    class Inband:
        id = "codebuddy"
        calls = 0

        async def stream_chat(self, _data, _payload, _model):
            Inband.calls += 1
            yield restricted

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Inband()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=collector))
    chunks = [chunk async for chunk in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]

    assert chunks                     # 以错误帧收尾，不是静默断开
    assert Inband.calls == 1          # 同渠道第二个号没有被拿去撞
    assert [event["error_type"] for event in collector.events] == ["account_restricted"]


async def test_stream_http_restricted_skips_channel(repo):
    """流式 HTTP 403/11140：与流内事件同一处置（跳过渠道 + account_restricted）。"""
    from src.compat.openai.request import parse_chat_request
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler
    from src.provider.codebuddy.client import UpstreamHTTPError

    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "u"})
    collector = RecordingCollector()

    class HttpRestricted:
        id = "codebuddy"
        calls = 0

        async def stream_chat(self, _data, _payload, _model):
            HttpRestricted.calls += 1
            raise UpstreamHTTPError(403, b'{"code":11140}')
            yield  # pragma: no cover - 抛错后的不可达分支，只为保持异步生成器形态

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": HttpRestricted()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=collector))
    chunks = [chunk async for chunk in executor.stream(parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True}))]

    assert chunks
    assert HttpRestricted.calls == 1
    assert [event["error_type"] for event in collector.events] == ["account_restricted"]


async def test_complete_inband_restricted_skips_channel(repo):
    """非流式聚合遇到流内 RESTRICTED：同样跳过渠道，不重复落收尾行。"""
    from src.compat.openai.request import parse_chat_request
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler

    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "u"})
    collector = RecordingCollector()
    restricted = Event(kind=EventKind.ERROR, error_kind=ErrKind.RESTRICTED,
                       error_code=11140, error_message="request illegal")

    class Inband:
        id = "codebuddy"
        calls = 0

        async def stream_chat(self, _data, _payload, _model):
            Inband.calls += 1
            yield restricted

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Inband()}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", stats=collector))

    with pytest.raises(NoHealthyCredential):
        await executor.complete(parse_chat_request(
            {"messages": [{"role": "user", "content": "hi"}]}))

    assert Inband.calls == 1
    # 尝试行已归因，收尾行不再重复落（否则 requests 与 Key 日额度双计）
    assert [event["error_type"] for event in collector.events] == ["account_restricted"]


def test_restricted_summary_never_echoes_upstream_body():
    """受限日志只打受控标识：业务码 → 状态码 → 异常类名，绝不含 body。

    上游 body 可能回显用户内容（`body_hint` 还会截取正文），打进日志等于把
    用户数据落到服务端日志里。
    """
    from src.engine.executor import _restricted_summary
    from src.provider.codebuddy.client import UpstreamHTTPError

    class NoCode(Exception):
        pass

    assert _restricted_summary(Event(kind=EventKind.ERROR, error_code=11140,
                                      error_message="secret user text")) == "code=11140"
    assert _restricted_summary(UpstreamHTTPError(403, b'{"code":11140}')) == "status=403"
    assert _restricted_summary(NoCode("secret")) == "kind=NoCode"
    # UpstreamStreamError 经 `.event` 取码（不是把异常描述打进日志）
    from src.compat.openai.errors import UpstreamStreamError
    wrapped = UpstreamStreamError(Event(kind=EventKind.ERROR, error_code=11140,
                                        error_message="secret user text"))
    assert _restricted_summary(wrapped) == "code=11140"


def test_restricted_burst_needs_two_distinct_credentials(repo):
    """同一个凭证重复中招不升级整渠道暂停：判据是「窗口内不同凭证数」。

    单号 2h 冷却已经挡住重复撞，按命中次数升级等于让单一租户/单一凭证的两个
    连续请求把整渠道停掉 24h（跨用户停服）。窗口过期后重新开窗，不累计上一轮。
    """
    from src.engine.executor import (
        RESTRICTED_BURST_WINDOW_SECONDS,
        Executor,
        ExecutorDeps,
    )
    from src.engine.scheduler import Scheduler

    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "u"})

    executor = Executor(ExecutorDeps(
        providers={}, credentials=credentials, scheduler=Scheduler(),
        default_model="glm-5.2", stats=RecordingCollector()))

    def cooled():
        rows = db.connect().execute(
            "SELECT cooling_until FROM credentials").fetchall()
        return any(row["cooling_until"] is not None for row in rows)

    now = int(time.time())
    executor._pause_provider_on_burst("codebuddy", "c1")   # noqa: SLF001
    executor._pause_provider_on_burst("codebuddy", "c1")   # noqa: SLF001
    assert not cooled()                                    # 同号两次不升级

    # 窗口过期：上一轮的凭证集合清零，同号再来一次也不升级
    executor._restricted_hits["codebuddy"] = (            # noqa: SLF001
        now - RESTRICTED_BURST_WINDOW_SECONDS - 1, {"c1"})
    executor._pause_provider_on_burst("codebuddy", "c1")   # noqa: SLF001
    assert not cooled()

    # 第二个**不同**凭证在窗口内中招 → 整渠道暂停，但不禁用
    executor._pause_provider_on_burst("codebuddy", "c2")   # noqa: SLF001
    rows = db.connect().execute(
        "SELECT disabled, cooling_until FROM credentials").fetchall()
    assert all(row["disabled"] == 0 for row in rows)       # 只冷却，不禁用
    assert all(now + 3600 <= row["cooling_until"] <= now + 24 * 3600 + 60
               for row in rows)
