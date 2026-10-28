"""DSML 工具调用泄漏回读 + 坏参数 fail-closed。"""

from __future__ import annotations

import json

import pytest

from src.compat.openai.dsml import (
    DsmlStreamFilter,
    extract,
    is_valid_arguments,
    looks_like_marker,
)
from src.compat.openai.errors import InvalidToolArguments
from src.compat.openai.response import StreamTranslator, aggregate
from src.engine.sse import SSE_DONE
from src.provider.base import ErrKind, Event, EventKind

_B = "\uff5c"          # 全角竖线
_S = f"<{_B}{_B}DSML{_B}{_B}"     # 真实的双竖线开标记

_LEAK = (
    f"{_S}tool_calls>\n"
    f'{_S}invoke name="shell">\n'
    f'{_S}parameter name="command" string="true">Get-Content a.csv</{_B}{_B}'
    f'DSML{_B}{_B}parameter>\n'
    f"</{_B}{_B}DSML{_B}{_B}invoke>\n"
    f"</{_B}{_B}DSML{_B}{_B}tool_calls>"
)


def test_extract_single_quote_dialect():
    calls, visible = extract("前文" + _LEAK + "后文")
    assert visible == "前文后文"
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "shell"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "command": "Get-Content a.csv"}
    assert calls[0]["index"] == 0


def test_extract_single_bar_dialect():
    """单竖线形态（旧版标记）同样回读。"""
    text = (f'<{_B}DSML{_B}invoke name="read">'
            f'<{_B}DSML{_B}parameter name="path">a.txt</{_B}DSML{_B}parameter>'
            f'</{_B}DSML{_B}invoke>')
    calls, visible = extract(text)
    assert visible == ""
    assert calls[0]["function"]["name"] == "read"
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt"}


def test_extract_v4_nested_dialect():
    """V4 Flash：参数是嵌套自闭合 invoke，值在 string 属性里。"""
    text = (f"{_S}tool_calls>\n"
            f'{_S}invoke name="exec_command">\n'
            f'{_S}invoke name="cmd" string="echo alpha" />\n'
            f"</{_B}{_B}DSML{_B}{_B}invoke>\n"
            f"</{_B}{_B}DSML{_B}{_B}tool_calls>")
    calls, _ = extract(text)
    assert [c["function"]["name"] for c in calls] == ["exec_command"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"cmd": "echo alpha"}


def test_extract_number_parameter_is_json():
    text = (f'{_S}invoke name="f">'
            f'{_S}parameter name="n">42</{_B}{_B}DSML{_B}{_B}parameter>'
            f'</{_B}{_B}DSML{_B}{_B}invoke>')
    calls, _ = extract(text)
    assert json.loads(calls[0]["function"]["arguments"]) == {"n": 42}


def test_extract_multiple_calls_indexed_in_order():
    one = f'{_S}invoke name="a"></{_B}{_B}DSML{_B}{_B}invoke>'
    two = f'{_S}invoke name="b"></{_B}{_B}DSML{_B}{_B}invoke>'
    calls, _ = extract(one + two)
    assert [c["function"]["name"] for c in calls] == ["a", "b"]
    assert [c["index"] for c in calls] == [0, 1]


def test_extract_leaves_plain_text_untouched():
    assert extract("普通正文") == ([], "普通正文")


def test_extract_unclosed_marker_kept_in_text():
    text = "前缀" + _S + 'invoke name="x"'
    calls, visible = extract(text)
    assert calls == []
    assert visible == text


def test_extract_ignores_marker_without_name():
    text = f'{_S}invoke ></{_B}{_B}DSML{_B}{_B}invoke>'
    assert extract(text) == ([], text)


# ------------------------------------------------------------------ 流式过滤


def test_stream_filter_passes_plain_text_immediately():
    f = DsmlStreamFilter()
    assert f.feed("hello") == ("hello", [])


def test_stream_filter_buffers_across_chunks():
    f = DsmlStreamFilter()
    assert f.feed("前") == ("前", [])
    assert f.feed(_S) == ("", [])            # 标记开始，缓冲
    assert f.feed('invoke name="a"') == ("", [])
    visible, calls = f.feed(f'></{_B}{_B}DSML{_B}{_B}invoke>')
    assert visible == ""
    assert calls[0]["function"]["name"] == "a"


def test_stream_filter_split_inside_partial_marker():
    """标记前缀被切成两片（`<` + `|DSML|...`）也不能漏成正文。"""
    f = DsmlStreamFilter()
    assert f.feed("x<") == ("", [])
    visible, _ = f.feed(f"{_B}{_B}DSML{_B}{_B}invoke name=\"b\""
                        f"></{_B}{_B}DSML{_B}{_B}invoke>")
    assert visible == "x"


def test_stream_filter_flush_returns_incomplete_block():
    f = DsmlStreamFilter()
    assert f.feed("a" + _S + 'invoke name="x"') == ("", [])
    assert f.flush() == "a" + _S + 'invoke name="x"'
    assert f.flush() == ""


def test_stream_filter_flush_empty_when_clean():
    assert DsmlStreamFilter().flush() == ""


def test_stream_filter_empty_feed():
    assert DsmlStreamFilter().feed("") == ("", [])


def test_looks_like_marker_variants():
    assert looks_like_marker("文本" + _S) is True
    assert looks_like_marker("<") is True    # 可能是跨 chunk 标记的前半片
    assert looks_like_marker("<" + _B) is True
    assert looks_like_marker("DSML") is False


# ------------------------------------------------------- 出口集成（流式/聚合）


def _frames(frames: list[bytes]) -> list[dict]:
    out = []
    for frame in frames:
        if frame == SSE_DONE:
            continue
        out.append(json.loads(frame.decode().removeprefix("data: ").strip()))
    return out


def test_stream_translator_recovers_dsml_and_rewrites_finish():
    t = StreamTranslator("m")
    frames = list(t.translate(Event(kind=EventKind.CONTENT, content="看：" + _LEAK)))
    frames += list(t.translate(Event(kind=EventKind.FINISH, finish_reason="stop")))
    chunks = _frames(frames)
    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    assert deltas[0] == {"role": "assistant", "content": "看："}
    tool_delta = next(d for d in deltas if "tool_calls" in d)
    assert tool_delta["tool_calls"][0]["function"]["name"] == "shell"
    finish = next(c for c in chunks if c["choices"]
                  and c["choices"][0]["finish_reason"])
    assert finish["choices"][0]["finish_reason"] == "tool_calls"
    assert frames[-1] == SSE_DONE


def test_stream_translator_flushes_pending_text_on_finish():
    t = StreamTranslator("m")
    assert list(t.translate(Event(kind=EventKind.CONTENT,
                                  content="x" + _S + 'invoke name="a"'))) == []
    frames = list(t.translate(Event(kind=EventKind.FINISH, finish_reason="stop")))
    chunks = _frames(frames)
    assert chunks[0]["choices"][0]["delta"]["content"].startswith("x" + _S)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"


def test_stream_translator_keeps_non_stop_finish_reason():
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.CONTENT, content=_LEAK)))
    frames = _frames(list(t.translate(Event(kind=EventKind.FINISH,
                                           finish_reason="length"))))
    assert frames[-1]["choices"][0]["finish_reason"] == "length"


def test_aggregate_recovers_dsml_from_content():
    events = [
        Event(kind=EventKind.CONTENT, content="看：" + _LEAK),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ]
    message = aggregate(events, "m")["choices"][0]["message"]
    assert message["content"] == "看："
    assert message["tool_calls"][0]["function"]["name"] == "shell"
    assert aggregate(events, "m")["choices"][0]["finish_reason"] == "tool_calls"


def test_aggregate_merges_dsml_with_structured_calls():
    """已有结构化调用时，回读的调用接在后面，索引不撞。"""
    events = [
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "c1", "index": 0,
                           "function": {"name": "f", "arguments": '{"a": 1}'}}]),
        Event(kind=EventKind.CONTENT, content=_LEAK),
        Event(kind=EventKind.FINISH, finish_reason="tool_calls"),
    ]
    calls = aggregate(events, "m")["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["f", "shell"]
    assert [c["index"] for c in calls] == [0, 1]


def test_aggregate_fail_closed_on_bad_arguments():
    events = [
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "c1", "index": 0,
                           "function": {"name": "f", "arguments": "{bad"}}]),
        Event(kind=EventKind.FINISH, finish_reason="tool_calls"),
    ]
    with pytest.raises(InvalidToolArguments) as caught:
        aggregate(events, "m")
    assert caught.value.function == "f"
    assert caught.value.kind() is ErrKind.INVALID


def test_aggregate_keeps_valid_arguments_bytes():
    """合法参数原样透传（不重新序列化，空格/键序保持不变）。"""
    raw = '{"b": 2,  "a": 1}'
    events = [
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "c1", "index": 0,
                           "function": {"name": "f", "arguments": raw}}]),
        Event(kind=EventKind.FINISH, finish_reason="tool_calls"),
    ]
    message = aggregate(events, "m")["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["arguments"] == raw


@pytest.mark.parametrize("arguments,valid", [
    (None, True),
    ("", True),
    ("{}", True),
    ('{"a": 1}', True),
    ("{bad", False),
    ("[1, 2]", False),
    ("42", False),
    (42, False),
])
def test_is_valid_arguments(arguments, valid):
    assert is_valid_arguments(arguments) is valid


# --------------------------------------------------- 方言细节 / 过滤分支覆盖


def test_extract_self_closing_parameter_uses_attribute():
    text = (f'{_S}invoke name="f">'
            f'{_S}parameter name="p" string="raw" />'
            f'</{_B}{_B}DSML{_B}{_B}invoke>')
    calls, _ = extract(text)
    assert json.loads(calls[0]["function"]["arguments"]) == {"p": "raw"}


def test_extract_plain_string_parameter_kept_verbatim():
    """非 `string="true"` 且不是合法 JSON 的值：原样当字符串，不丢参数。"""
    text = (f'{_S}invoke name="f">'
            f'{_S}parameter name="p">hello world</{_B}{_B}DSML{_B}{_B}parameter>'
            f'</{_B}{_B}DSML{_B}{_B}invoke>')
    calls, _ = extract(text)
    assert json.loads(calls[0]["function"]["arguments"]) == {"p": "hello world"}


def test_extract_parameter_without_name_skipped():
    text = (f'{_S}invoke name="f">'
            f'{_S}parameter string="x">ignored</{_B}{_B}DSML{_B}{_B}parameter>'
            f'</{_B}{_B}DSML{_B}{_B}invoke>')
    calls, _ = extract(text)
    assert json.loads(calls[0]["function"]["arguments"]) == {}


def test_extract_nested_invoke_without_name_skipped():
    text = (f'{_S}invoke name="f">'
            f'{_S}invoke string="v" />'
            f'</{_B}{_B}DSML{_B}{_B}invoke>')
    calls, _ = extract(text)
    assert json.loads(calls[0]["function"]["arguments"]) == {}


def test_extract_top_level_self_closing_without_string():
    text = f'{_S}invoke name="f" />'
    calls, _ = extract(text)
    assert json.loads(calls[0]["function"]["arguments"]) == {}


def test_stream_filter_partial_letter_prefix_buffered():
    """`<DS` 这种字母前缀（非竖线形态）也要缓冲，不能当正文漏出去。"""
    f = DsmlStreamFilter()
    assert f.feed("a<DS") == ("", [])
    visible, _ = f.feed("MLx")
    assert visible == "a<DSMLx"


def test_stream_filter_opens_buffer_then_releases_plain_text():
    f = DsmlStreamFilter()
    assert f.feed("<") == ("", [])
    assert f.feed("plain") == ("<plain", [])


def test_extract_nested_closed_invoke_is_not_a_parameter():
    """body 里的完整嵌套调用（有闭合标签）是调用而不是参数，不当参数收录。"""
    text = (f'{_S}invoke name="outer">'
            f'{_S}invoke name="inner"></{_B}{_B}DSML{_B}{_B}invoke>'
            f'</{_B}{_B}DSML{_B}{_B}invoke>')
    calls, _ = extract(text)
    assert [call["function"]["name"] for call in calls] == ["outer"]
    assert json.loads(calls[0]["function"]["arguments"]) == {}


def test_stream_filter_self_closed_marker_releases_text():
    """无 name 的自闭合 invoke 解析不出调用：标记已闭合，正文照常放行。"""
    f = DsmlStreamFilter()
    visible, calls = f.feed(f"{_S}invoke />junk")
    assert calls == []
    assert "junk" in visible
