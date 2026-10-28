"""tool 输出确定性压缩（`src/engine/tool_output.py`）。"""

from __future__ import annotations

from src.engine.tool_output import (
    compress_tool_outputs,
    compress_tool_text,
)


def test_short_text_is_returned_untouched():
    text = "\x1b[32mok\x1b[0m"
    assert compress_tool_text(text, max_chars=100) is text
    # max_chars ≤ 0 视为关闭
    assert compress_tool_text("x" * 100, max_chars=0) == "x" * 100


def test_ansi_sequences_are_stripped_only_when_compressing():
    text = ("\x1b[1;31mERROR boom\x1b[0m\n" * 20)
    out = compress_tool_text(text, max_chars=10)
    assert "\x1b[" not in out
    assert "ERROR boom" in out


def test_consecutive_duplicate_lines_are_collapsed():
    text = "\n".join(["same line"] * 50) + "\n" + "tail"
    out = compress_tool_text(text, max_chars=60, head_lines=5, tail_lines=5)
    assert "same line (×50)" in out
    assert out.count("same line") == 1


def test_error_lines_survive_omission_window():
    noise = [f"step {index} ok" for index in range(500)]
    noise.insert(250, "Traceback (most recent call last):")
    noise.insert(251, "ValueError: boom")
    out = compress_tool_text("\n".join(noise), max_chars=300,
                             head_lines=2, tail_lines=2)
    assert "省略" in out
    assert "Traceback (most recent call last):" in out
    assert "ValueError: boom" in out
    # 头尾保留
    assert "step 0 ok" in out and "step 499 ok" in out


def test_hard_trim_keeps_within_budget():
    text = "a" * 5000
    out = compress_tool_text(text, max_chars=200)
    assert len(out) <= 200
    assert "省略" in out
    # 阈值小到装不下标记时退化为纯截断
    tiny = compress_tool_text(text, max_chars=5)
    assert tiny == "aaaaa"


def test_tail_zero_keeps_no_tail_window():
    text = "\n".join(f"line {index}" for index in range(100))
    out = compress_tool_text(text, max_chars=10, head_lines=3, tail_lines=0)
    assert "line 0" in out
    assert "line 99" not in out


def test_compress_tool_outputs_only_touches_long_tool_text():
    short = {"role": "tool", "content": "ok"}
    long_tool = {"role": "tool", "content": "\x1b[31mfail\x1b[0m\n" * 100}
    user = {"role": "user", "content": "fail" * 100}
    list_content = {"role": "tool",
                    "content": [{"type": "text", "text": "fail" * 100}]}
    messages = [short, long_tool, user, list_content]

    out = compress_tool_outputs(messages, max_chars=50)

    assert out is not messages
    assert out[0] is short
    assert out[1] is not long_tool and "\x1b[" not in out[1]["content"]
    assert out[2] is user
    assert out[3] is list_content  # part 数组不动
    # 入参不被修改
    assert "\x1b[" in long_tool["content"]


def test_compress_tool_outputs_returns_same_list_when_nothing_changes():
    messages = [{"role": "tool", "content": "ok"},
                {"role": "user", "content": "hi"}]
    assert compress_tool_outputs(messages, max_chars=100) is messages
