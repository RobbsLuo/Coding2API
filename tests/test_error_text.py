"""异常文本助手：空消息不能打出无因日志。"""

from __future__ import annotations

from src.error_text import error_text


def test_error_text_keeps_type_and_message():
    error = ValueError("bad value")
    assert error_text(error) == "ValueError: bad value"


def test_error_text_uses_type_name_when_message_empty():
    """`TimeoutError` / 部分 `ConnectError` 的 str 为空 → 至少留类名。"""
    assert error_text(TimeoutError()) == "TimeoutError"
    assert error_text(ConnectionError()) == "ConnectionError"


def test_error_text_collapses_whitespace_and_ignores_blank_message():
    assert error_text(RuntimeError("  bad\n\t value  ")) == "RuntimeError: bad value"
    assert error_text(RuntimeError("   ")) == "RuntimeError"


def test_error_text_truncates_long_message():
    text = error_text(RuntimeError("x" * 500), limit=10)
    assert text == "RuntimeError: xxxxxxxxxx…"