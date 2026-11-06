"""异常文本助手：空消息不能打出无因日志；带凭证形态的消息不得原样进日志。"""

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
    text = error_text(RuntimeError("x" * 500))
    assert text == "RuntimeError: " + "x" * 200 + "…"


def test_error_text_drops_message_that_looks_like_a_credential():
    """上游错误体可能回显鉴权料：命中脱敏规则时只留类名。

    `UpstreamHTTPError` 的消息带 `body_hint`（最长 2 KB 的上游响应体）。日志
    缓冲层的 `redact_or_drop` 会把整行丢掉，但那条正则漏了不少形态；在产生
    日志文本这一处先判，等于不让它进日志。
    """
    assert error_text(RuntimeError('{"access_token": "abc123"}')) == "RuntimeError"
    assert error_text(RuntimeError("Authorization: Bearer eyJhbGciOi.xx.yy")) == "RuntimeError"
    assert error_text(RuntimeError("api_key=sk-live-123")) == "RuntimeError"

    # 普通文本（上游业务码/文案）照常保留，排障信息不能一起丢
    assert error_text(RuntimeError("InferHub.001001020.406 not multimodal")) == (
        "RuntimeError: InferHub.001001020.406 not multimodal")
