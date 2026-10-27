"""上下文压缩（`src/engine/compress.py` + `src/api/context.py`）。

纯函数直测为主：估算口径、按上限裁剪、system 保留、tool_calls 成组、
单条超限截断。末尾两节覆盖「按渠道查窗口 + 压缩闭包」与 executor 选号后
接线（窗口按实际服务渠道取值，不再跨渠道取 min）。
"""

from __future__ import annotations

import pytest

from src.api.context import build_context_compressor, context_window_for
from src.compat.openai.request import ChatRequest
from src.engine.compress import (
    _content_text,
    _truncate_content,
    compress_messages,
    estimate_messages,
    estimate_tokens,
)
from src.provider.base import Model, Usage


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _system(text: str) -> dict:
    return {"role": "system", "content": text}


# --------------------------------------------------------------- token 估算

def test_estimate_tokens_weights_by_character_class():
    assert estimate_tokens("") == 0
    # 中文 0.55 / 数字 0.33 / 其他 0.25
    assert estimate_tokens("中") == 0.55
    assert estimate_tokens("1") == 0.33
    assert estimate_tokens("a") == 0.25
    assert estimate_tokens("中1a") == 0.55 + 0.33 + 0.25


def test_estimate_tokens_covers_fullwidth_and_cjk_punctuation():
    # 3000 段（CJK 标点）与 FF00 段（全角）同样按中文计权
    assert estimate_tokens("、") == 0.55
    assert estimate_tokens("Ａ") == 0.55


def test_content_text_handles_parts_and_placeholders():
    assert _content_text("x") == "x"
    assert _content_text(None) == ""
    assert _content_text(5) == "5"
    # 非文本 part 不再按固定 100 字符占位：只有能读出正文（text / image_url.url）
    # 的 part 才计入，其余记 0——固定占位会让图片会话永远不触发压缩。
    assert _content_text([{"text": "a"}, "b", {"image": 1}, {"text": 2}]) == "ab"
    # 既不是 dict 也不是 str 的条目直接跳过
    assert _content_text([1, "a"]) == "a"


def test_content_text_counts_image_url_bytes():
    """图片按 base64 正文长度计入，而不是被当成 0 / 固定占位。"""
    url = "data:image/png;base64," + "A" * 1000
    content = [{"type": "text", "text": "看图"},
               {"type": "image_url", "image_url": {"url": url}}]
    assert _content_text(content) == "看图" + url
    # 畸形 image_url（缺 url / url 非字符串）记 0，不抛异常
    assert _content_text([{"type": "image_url", "image_url": {"url": 5}}]) == ""
    assert _content_text([{"type": "image_url"}]) == ""


def test_estimate_messages_counts_reasoning_tools_and_ids():
    messages = [{
        "role": "assistant", "content": "a", "reasoning_content": "bb",
        "tool_call_id": "t1",
        "tool_calls": [{"function": {"name": "f", "arguments": "x"}}]}]
    # content 'a' + reasoning 'bb' + name 'f' + args 'x' + tool_call_id 't1'(0.33+0.25)
    expected = 0.25 + 0.5 + 0.25 + 0.25 + 0.58
    assert estimate_messages(messages) == expected


def test_estimate_messages_tolerates_non_dict_tool_calls():
    assert estimate_messages([{"role": "assistant", "tool_calls": [None, 5]}]) == 0


def test_truncate_content_short_is_unchanged():
    assert _truncate_content("short") == "short"
    # part 数组即使总长很短也原样返回（不降级）
    parts = [{"type": "text", "text": "x"}]
    assert _truncate_content(parts) is parts


# --------------------------------------------------------------- 裁剪

def test_compress_noop_when_under_limit():
    messages = [_user("hi")]
    assert compress_messages(messages, max_input_tokens=1000) is messages


def test_compress_noop_for_zero_or_negative_limit():
    messages = [_user("x" * 1000)]
    assert compress_messages(messages, max_input_tokens=0) is messages
    assert compress_messages(messages, max_input_tokens=-5) is messages


def test_compress_noop_when_budget_non_positive():
    # safety_ratio 极小 / reserve 很大都可能让预算 ≤ 0 → 不裁剪
    messages = [_user("x" * 100)]
    assert compress_messages(messages, max_input_tokens=10, reserve_for_output=100) is messages


def test_compress_keeps_system_and_recent_drops_oldest():
    messages = [
        _system("sys"),
        _user("old " * 200),
        _user("mid " * 200),
        _user("new"),
    ]
    result = compress_messages(messages, max_input_tokens=200, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    assert result[0] == _system("sys")          # system 永远保留
    assert result[-1] == _user("new")           # 最近一条永远保留
    assert len(result) < len(messages)          # 老消息被丢
    assert _user("old " * 200) not in result


def test_compress_backfill_keeps_older_small_message_when_it_fits():
    # 最近一条很大但不至于超限，前面有一条小消息也应能留下
    messages = [_user("small"), _user("x" * 40)]
    result = compress_messages(messages, max_input_tokens=20, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    assert result == messages                    # 两条都在预算内 → 原样
    tighter = compress_messages(messages, max_input_tokens=11, reserve_for_output=0,
                                min_keep_messages=1, safety_ratio=1.0)
    assert tighter == [_user("x" * 40)]          # 只能容下最近一条


def test_compress_keeps_only_contiguous_suffix():
    """保留集合不许带洞：跳过装不下的大消息去捞更老的小消息，前缀就复用不了。"""
    messages = [_user("small"), _user("b" * 400), _user("new")]
    result = compress_messages(messages, max_input_tokens=8, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    # 装不下 "b"*400 就停：更老的 "small" 不再被捞进来
    assert result == [_user("new")]


def test_compress_keeps_at_least_min_keep_messages_even_when_over_budget():
    messages = [_user("a" * 100), _user("b" * 100), _user("c" * 100)]
    result = compress_messages(messages, max_input_tokens=1, reserve_for_output=0,
                               min_keep_messages=2, safety_ratio=1.0)
    # 最近两条无条件保留（哪怕超预算），最老一条被丢
    assert result == messages[-2:]


def test_compress_keeps_tool_calls_and_results_together():
    messages = [
        _user("old " * 300),
        {"role": "assistant", "tool_calls": [
            {"id": "t1", "function": {"name": "f", "arguments": ""}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "res"},
        _user("new"),
    ]
    result = compress_messages(messages, max_input_tokens=60, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    # assistant(tool_calls) 与其 tool 结果要么都在、要么都不在
    has_call = any(m.get("tool_calls") for m in result)
    has_result = any(m.get("role") == "tool" for m in result)
    assert has_call == has_result


def test_compress_truncates_oversized_single_message():
    huge = "x" * 100_000
    result = compress_messages([_system("s"), _user(huge)],
                               max_input_tokens=200, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    # 单条自己就超限：内容被截断并带标记，而不是整条丢
    body = result[-1]["content"]
    assert len(body) < len(huge)
    assert "内容过长已截断" in body


def test_compress_truncates_part_array_content():
    huge_parts = [{"type": "text", "text": "x" * 100_000}]
    result = compress_messages([_user(huge_parts)],
                               max_input_tokens=200, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    assert isinstance(result[-1]["content"], str)
    assert "内容过长已截断" in result[-1]["content"]


def test_compress_never_truncates_image_parts():
    """含图片的 content 不截断：截 base64 只会得到无效 data URL，
    上游照样 400；原样保留交给上游裁决。"""
    url = "data:image/png;base64," + "A" * 100_000
    parts = [{"type": "text", "text": "看图"},
             {"type": "image_url", "image_url": {"url": url}}]
    result = compress_messages([_user(parts)],
                               max_input_tokens=200, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    assert result[-1]["content"] is parts


def test_image_part_counts_toward_budget():
    """图片按 base64 正文长度计入估算：大图必须能触发压缩，而不是被当成 0。"""
    url = "data:image/png;base64," + "A" * 100_000
    messages = [_system("s"), _user([{"type": "image_url",
                                      "image_url": {"url": url}}])]
    assert estimate_messages(messages) > estimate_messages([_system("s")])


# --------------------------------------------------------- 目录查上限 / 接线

class _CacheStub:
    """model_list_cache / model_aliases 的最小替身（与 Services 同形状）。"""

    def __init__(self, cache=None, aliases=None):
        self.model_list_cache = cache or {}
        self.model_aliases = aliases or {}


class _SettingsStub:
    def __init__(self, *, enabled=True, reserve=0, min_keep=4, ratio=1.0,
                 prefix_debug=False, sticky=True):
        self.context_compress_enabled = enabled
        self.context_compress_reserve_tokens = reserve
        self.context_compress_min_keep_messages = min_keep
        self.context_compress_safety_ratio = ratio
        self.context_compress_sticky_enabled = sticky
        self.prefix_debug_enabled = prefix_debug


def test_context_window_looks_up_by_normalized_and_raw_name():
    cache = {"codebuddy": {"glm-5.2": Model(id="glm-5.2", max_input_tokens=8000)}}
    aliases = {"codebuddy": {"glm-5.2": "GLM-5.2-raw"}}
    assert context_window_for(cache, aliases, "codebuddy", "glm-5.2") == 8000
    assert context_window_for(cache, aliases, "codebuddy", "glm-5.2@codebuddy") == 8000


def test_context_window_looks_up_by_raw_id_when_alias_points_to_it():
    cache = {"codebuddy": {"glm-5.2-raw": Model(id="x", max_input_tokens=4096)}}
    aliases = {"codebuddy": {"glm-5.2": "glm-5.2-raw"}}
    assert context_window_for(cache, aliases, "codebuddy", "glm-5.2") == 4096


def test_context_window_is_per_provider_not_minimum():
    """B 修订核心：窗口按渠道各自取值，不再跨渠道取 min。

    回归场景：deepseek-v4.1-flash 挂 qoder（180K，经同义词归一并入）与
    codebuddy（1M）；min 口径会把 codebuddy 的会话按 180K 反复误裁，前缀
    不稳定击穿上游前缀缓存。
    """
    cache = {
        "codebuddy": {"m": Model(id="m", max_input_tokens=1000000)},
        "qoder": {"dfmodel": Model(id="dfmodel", max_input_tokens=180000)}}
    aliases = {"codebuddy": {"m": "m"}, "qoder": {"m": "dfmodel"}}
    assert context_window_for(cache, aliases, "codebuddy", "m") == 1000000
    assert context_window_for(cache, aliases, "qoder", "m") == 180000
    # 未登记该模型的渠道：未知，None（不压缩）
    assert context_window_for(cache, aliases, "trae", "m") is None


def test_context_window_ignores_unknown_and_invalid_values():
    cache = {"codebuddy": {"m": Model(id="m", max_input_tokens=0),
                           "n": Model(id="n", max_input_tokens=None)}}
    aliases = {"codebuddy": {"m": "m", "n": "n"}}
    assert context_window_for(cache, aliases, "codebuddy", "m") is None
    assert context_window_for(cache, aliases, "codebuddy", "n") is None
    assert context_window_for(cache, aliases, "codebuddy", "missing") is None
    assert context_window_for(cache, aliases, "codebuddy", "") is None


def test_context_window_raises_trae_to_measured_floor():
    """TRAE 声明值严重低估（200K/232K，实测约 1M），压缩预算按实测下限抬。

    回归场景：用户发 30 万 token 上下文，上游本可处理，但按声明值算的压缩
    预算只有 217K → 本地静默压掉真实内容。
    """
    cache = {"trae": {"seed-code-pro-0430": Model(id="x", max_input_tokens=232768),
                      "glm-5.2": Model(id="y", max_input_tokens=200000)}}
    aliases = {"trae": {"seed-code-pro-0430": "seed-code-pro-0430",
                        "glm-5.2": "glm-5.2"}}
    assert context_window_for(cache, aliases, "trae", "seed-code-pro-0430") == 1_048_576
    assert context_window_for(cache, aliases, "trae", "glm-5.2") == 1_048_576


def test_context_window_floor_never_lowers_a_larger_declared_value():
    """下限只抬不压：声明值本来就比实测下限大时，保留声明值。"""
    cache = {"trae": {"big": Model(id="b", max_input_tokens=2_000_000)}}
    assert context_window_for(cache, {"trae": {"big": "big"}}, "trae", "big") == 2_000_000


def test_context_window_floor_is_channel_scoped():
    """抬升只对登记过的渠道生效，不污染其它渠道的预算。"""
    cache = {"codebuddy": {"m": Model(id="m", max_input_tokens=232768)}}
    assert context_window_for(cache, {"codebuddy": {"m": "m"}},
                              "codebuddy", "m") == 232768


def test_context_window_floor_still_requires_known_model():
    """未知模型不因下限表而被凭空赋予窗口（否则会对未知模型乱压）。"""
    assert context_window_for({"trae": {}}, {"trae": {}}, "trae", "glm-5.2") is None
    assert context_window_for({}, {}, "trae", "glm-5.2") is None


def test_compressor_disabled_is_noop():
    cache = {"codebuddy": {"m": Model(id="m", max_input_tokens=1)}}
    compress = build_context_compressor(
        cache, {"codebuddy": {"m": "m"}}, _SettingsStub(enabled=False))
    payload = {"messages": [_user("x" * 1000)]}
    assert compress("codebuddy", "m", payload) is payload


def test_compressor_skips_when_window_unknown_or_messages_missing():
    compress = build_context_compressor({}, {}, _SettingsStub())
    payload = {"messages": [_user("x")]}
    assert compress("codebuddy", "m", payload) is payload
    # 有上限但 raw 里没有 messages（非 chat 形状）：安全跳过
    cache = {"codebuddy": {"m": Model(id="m", max_input_tokens=1)}}
    compress2 = build_context_compressor(
        cache, {"codebuddy": {"m": "m"}}, _SettingsStub())
    payload2 = {}
    assert compress2("codebuddy", "m", payload2) is payload2


def test_compressor_under_budget_is_noop():
    messages = [_user("short")]
    cache = {"codebuddy": {"m": Model(id="m", max_input_tokens=100000)}}
    compress = build_context_compressor(
        cache, {"codebuddy": {"m": "m"}}, _SettingsStub())
    payload = {"messages": messages}
    assert compress("codebuddy", "m", payload) is payload


def test_compressor_uses_per_channel_window_and_returns_fresh_payload():
    """同一 payload 按渠道压出不同结果，且不原地改入参（换号重压契约）。"""
    messages = [_user("old " * 500), _user("new")]
    cache = {
        "codebuddy": {"m": Model(id="m", max_input_tokens=100000)},
        "qoder": {"m": Model(id="m", max_input_tokens=50)}}
    compress = build_context_compressor(
        cache, {"codebuddy": {"m": "m"}, "qoder": {"m": "m"}},
        _SettingsStub(min_keep=1))
    payload = {"messages": messages, "model": "m"}
    # 大窗口渠道：未超限，原样
    assert compress("codebuddy", "m", payload) is payload
    # 小窗口渠道：裁剪；入参 payload/messages 均未被改（可再压一次）
    out = compress("qoder", "m", payload)
    assert out is not payload
    assert out["messages"] == [_user("new")]
    assert payload["messages"] is messages
    assert compress("qoder", "m", payload)["messages"] == [_user("new")]


# ------------------------------------------------------------- executor 接线

class _FakeCredentials:
    provider = "codebuddy"

    def candidates(self, providers=None, *, selectable_only=False):
        from src.engine.scheduler import Candidate

        return [Candidate(credential_id="c1", provider=self.provider)]

    def provider_of(self, credential_id):
        return self.provider

    def credential_data(self, credential_id):
        return {"accessToken": "a"}

    def save_success(self, credential_id, *, model=None):
        pass


class _Scheduler:
    def select(self, candidates, tried, now):
        for candidate in candidates:
            if candidate.credential_id not in tried:
                return candidate.credential_id
        return None

    def should_rotate(self, tried):
        return False


def _big_request():
    messages = [_user("old " * 500), _user("new")]
    return ChatRequest(model="m", messages=messages, stream=True,
                       raw={"messages": messages})


# --------------------------------------------------------- 粘性裁剪（默认开）

def test_sticky_compressor_keeps_prefix_across_turns():
    """追加消息后前缀不变——这才是上游缓存能命中的前提。"""
    cache = {"qoder": {"m": Model(id="m", max_input_tokens=400)}}
    compress = build_context_compressor(
        cache, {"qoder": {"m": "m"}}, _SettingsStub(min_keep=1, ratio=1.0, reserve=0))
    history = [_user(f"{index}-" + "x" * 400) for index in range(20)]
    first = compress("qoder", "m", {"messages": history})["messages"]
    second = compress("qoder", "m", {"messages": [*history, _user("next")]})["messages"]
    assert second[:len(first)] == first
    assert len(second) == len(first) + 1


def test_non_sticky_compressor_rewrites_prefix_every_turn():
    """对照：关掉粘性就是旧行为——每轮窗口边界都跟着新消息前移。"""
    cache = {"qoder": {"m": Model(id="m", max_input_tokens=400)}}
    compress = build_context_compressor(
        cache, {"qoder": {"m": "m"}},
        _SettingsStub(min_keep=1, ratio=1.0, reserve=0, sticky=False))
    history = [_user(f"{index}-" + "x" * 400) for index in range(20)]
    first = compress("qoder", "m", {"messages": history})["messages"]
    second = compress("qoder", "m",
                      {"messages": [*history, _user("y" * 400)]})["messages"]
    assert second[:len(first)] != first


# ------------------------------------------------------------- executor 接线


@pytest.mark.asyncio
async def test_executor_compresses_after_pick_with_provider_window():
    """executor 在选号后按该渠道窗口压缩，上游收到的是压缩后的载荷。"""
    from src.engine.executor import Executor, ExecutorDeps
    from src.provider.base import Event, EventKind

    seen = []

    class Provider:
        id = "codebuddy"

        async def stream_chat(self, credential_data, payload, model):
            seen.append(payload)
            yield Event(kind=EventKind.CONTENT, content="ok")

    cache = {"codebuddy": {"m": Model(id="m", max_input_tokens=50)}}
    compress = build_context_compressor(
        cache, {"codebuddy": {"m": "m"}}, _SettingsStub(min_keep=1))
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), context_compress=compress))
    request = _big_request()
    async for _frame in executor.stream(request, username="alice"):
        pass
    assert len(seen) == 1
    assert seen[0]["messages"] == [_user("new")]
    # 请求原文未被改：affinity 指纹与后续换号重压都读它
    assert request.raw["messages"] == [_user("old " * 500), _user("new")]


@pytest.mark.asyncio
async def test_executor_without_compressor_sends_raw_payload():
    """未装配压缩（老调用方/测试）：原样转发。"""
    from src.engine.executor import Executor, ExecutorDeps
    from src.provider.base import Event, EventKind

    seen = []

    class Provider:
        id = "codebuddy"

        async def stream_chat(self, credential_data, payload, model):
            seen.append(payload)
            yield Event(kind=EventKind.CONTENT, content="ok")

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler()))
    request = _big_request()
    async for _frame in executor.stream(request, username="alice"):
        pass
    # 未压缩时上游收到的就是原文 messages（_with_model 只补 model 键的浅拷）
    assert seen[0]["messages"] is request.raw["messages"]


# ------------------------------------------------------------ 前缀诊断接线

def test_compressor_logs_trim_when_prefix_debug_enabled(caplog):
    """压缩每轮裁一条 = 前缀边界每轮前移：诊断开启时必须留下证据。"""
    messages = [_user("old " * 500), _user("new")]
    cache = {"codebuddy": {"m": Model(id="m", max_input_tokens=50)}}
    compress = build_context_compressor(
        cache, {"codebuddy": {"m": "m"}}, _SettingsStub(min_keep=1, prefix_debug=True))
    with caplog.at_level("INFO"):
        compress("codebuddy", "m", {"messages": messages})
    lines = [line for line in caplog.text.splitlines() if "前缀诊断·压缩" in line]
    assert len(lines) == 1
    assert "provider=codebuddy" in lines[0]
    assert "window=50" in lines[0]
    assert "messages=2→1" in lines[0]


def test_compressor_logs_nothing_when_prefix_debug_disabled(caplog):
    messages = [_user("old " * 500), _user("new")]
    cache = {"codebuddy": {"m": Model(id="m", max_input_tokens=50)}}
    compress = build_context_compressor(
        cache, {"codebuddy": {"m": "m"}}, _SettingsStub(min_keep=1))
    with caplog.at_level("INFO"):
        compress("codebuddy", "m", {"messages": messages})
    assert "前缀诊断" not in caplog.text


@pytest.mark.asyncio
async def test_executor_reports_prefix_after_compression():
    """诊断报告的是**发往上游**的那份载荷（压缩后），不是客户端原文。"""
    from src.engine.executor import Executor, ExecutorDeps
    from src.provider.base import Event, EventKind

    reports = []

    class Provider:
        id = "codebuddy"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="ok")

    cache = {"codebuddy": {"m": Model(id="m", max_input_tokens=50)}}
    compress = build_context_compressor(
        cache, {"codebuddy": {"m": "m"}}, _SettingsStub(min_keep=1))
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), context_compress=compress,
        prefix_debug=lambda *args: reports.append(args)))
    async for _frame in executor.stream(_big_request(), username="alice"):
        pass
    assert len(reports) == 1
    credential_id, provider_id, model, payload = reports[0]
    assert (credential_id, provider_id, model) == ("c1", "codebuddy", "m")
    assert payload["messages"] == [_user("new")]


@pytest.mark.asyncio
async def test_executor_reports_prefix_on_nonstream_path():
    """非流式聚合路径同样上报（两条主链各一次，不重复报同一轮）。"""
    from src.engine.executor import Executor, ExecutorDeps
    from src.provider.base import Event, EventKind

    reports = []

    class Provider:
        id = "codebuddy"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.USAGE,
                        usage=Usage(input_tokens=1, output_tokens=1))

    messages = [_user("old " * 500), _user("new")]
    request = ChatRequest(model="m", messages=messages, stream=False,
                          raw={"messages": messages})
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), prefix_debug=lambda *args: reports.append(args)))
    await executor.complete(request, username="alice")
    assert len(reports) == 1
    assert reports[0][3]["messages"] is messages
