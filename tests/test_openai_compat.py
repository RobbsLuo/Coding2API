"""OpenAI 兼容出口（/v1/chat/completions）的流式收尾 usage 帧与非流式 usage 形状。

收尾 usage 帧是 OpenAI 标准形态的一部分：finish chunk 之后、`data: [DONE]`
之前补一帧 `choices: []` 的 usage。只从流里读 usage 的客户端（如 pi-ai 的
`openai-completions` provider，恒发 `stream_options.include_usage=true`）
没有这一帧就恒显示 token 统计为 0。
"""

from __future__ import annotations

import json

from src.compat.openai.response import StreamTranslator, aggregate, usage_payload
from src.engine.sse import SSE_DONE
from src.provider.base import Event, EventKind, Usage


def _payload(frame: bytes) -> dict:
    return json.loads(frame.decode()[6:])


def _chat(usage: Usage | None) -> list[dict]:
    """走完 content → usage → finish，返回解析后的数据帧（不含 [DONE]）。"""
    t = StreamTranslator("m")
    frames = list(t.translate(Event(kind=EventKind.CONTENT, content="hi")))
    if usage is not None:
        frames += list(t.translate(Event(kind=EventKind.USAGE, usage=usage)))
    frames += list(t.translate(Event(kind=EventKind.FINISH, finish_reason="stop")))
    return [_payload(f) for f in frames if f != SSE_DONE]


# ---------------------------------------------- 收尾 usage 帧


def test_finish_emits_usage_chunk_between_finish_and_done():
    """FINISH 路径：finish chunk → usage 帧（choices 为空）→ [DONE]。"""
    t = StreamTranslator("m")
    frames = list(t.translate(Event(kind=EventKind.CONTENT, content="hi")))
    frames += list(t.translate(Event(
        kind=EventKind.USAGE, usage=Usage(11, 7, reasoning_tokens=13, cached_tokens=5))))
    frames += list(t.translate(Event(kind=EventKind.FINISH, finish_reason="stop")))

    assert frames[-1] == SSE_DONE
    usage_frame = _payload(frames[-2])
    assert usage_frame["choices"] == []
    assert usage_frame["object"] == "chat.completion.chunk"
    assert usage_frame["model"] == "m"
    assert usage_frame["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 5},
        "completion_tokens_details": {"reasoning_tokens": 13},
    }
    # 用量帧夹在 finish chunk 与 [DONE] 之间
    assert _payload(frames[-3])["choices"][0]["finish_reason"] == "stop"


def test_finish_without_upstream_usage_emits_no_usage_chunk():
    """上游未报 usage 就不补帧：不发 0 占位冒充「已上报」。"""
    frames = _chat(None)
    assert len(frames) == 2                       # content + finish
    assert all("usage" not in f for f in frames)
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"


def test_finish_fallback_emits_usage_chunk():
    """上游断流（未发 FINISH）走 finish() 兜底：同样补 usage 帧。"""
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.USAGE, usage=Usage(3, 4))))
    frames = list(t.finish())

    assert frames[-1] == SSE_DONE
    assert _payload(frames[-2])["usage"]["prompt_tokens"] == 3
    assert frames.count(SSE_DONE) == 1


def test_finish_fallback_without_usage_emits_single_done():
    t = StreamTranslator("m")
    frames = list(t.finish())

    assert frames.count(SSE_DONE) == 1             # 兜底路径不重复发 [DONE]
    assert len(frames) == 2
    assert "usage" not in _payload(frames[0])


def test_multiple_usage_events_keep_the_last_reported():
    """续写会多次汇报 usage（TECHNICAL §3.4）：以最后一次为准。"""
    t = StreamTranslator("m")
    frames = list(t.translate(Event(kind=EventKind.USAGE, usage=Usage(10, 5))))
    frames += list(t.translate(Event(kind=EventKind.USAGE, usage=Usage(20, 8))))
    frames += list(t.translate(Event(kind=EventKind.FINISH, finish_reason="stop")))
    assert _payload(frames[-2])["usage"]["prompt_tokens"] == 20
    assert _payload(frames[-2])["usage"]["total_tokens"] == 28


def test_stream_usage_chunk_matches_non_streaming_shape():
    """流式收尾帧与非流式 aggregate() 的 usage 必须逐字段一致。"""
    usage = Usage(11, 7, reasoning_tokens=13, cached_tokens=5)
    streamed = _chat(usage)[-1]["usage"]
    non_streamed = aggregate(
        [Event(kind=EventKind.USAGE, usage=usage)], "m")["usage"]
    assert streamed == non_streamed


# ---------------------------------------------- usage_payload


def test_usage_payload_full():
    assert usage_payload(Usage(4, 6, reasoning_tokens=2, cached_tokens=3)) == {
        "prompt_tokens": 4,
        "completion_tokens": 6,
        "total_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }


def test_usage_payload_none_is_all_null():
    """上游未上报：全 None，total_tokens 也是 None（不拿 0 冒充已知）。"""
    assert usage_payload(None) == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "prompt_tokens_details": {"cached_tokens": None},
        "completion_tokens_details": {"reasoning_tokens": None},
    }


def test_usage_payload_partial_keeps_reported_zero():
    """部分缺省：已上报的 0 是有效值，未上报的仍是 None。"""
    payload = usage_payload(Usage(input_tokens=4))
    assert payload["prompt_tokens"] == 4
    assert payload["completion_tokens"] is None
    assert payload["total_tokens"] == 4             # 缺失按 0 参与求和
    assert payload["prompt_tokens_details"] == {"cached_tokens": None}
    assert payload["completion_tokens_details"] == {"reasoning_tokens": None}