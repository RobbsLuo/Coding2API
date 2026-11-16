"""出站隐私脱敏（`engine/privacy.py`）：规则解析、掩码、边界。"""

from __future__ import annotations

import copy

from src.engine import privacy


def test_no_rules_is_zero_copy_and_noop():
    body = {"messages": [{"role": "user", "content": "a@b.com"}]}
    out, hits = privacy.redact_payload(body, "")
    assert out is body and hits == 0
    out, hits = privacy.redact_payload(body, None)
    assert out is body and hits == 0


def test_parse_rules_orders_and_ignores_unknown():
    assert privacy.parse_rules("private_ip, Email, bogus,cn_mobile") == (
        "email", "cn_mobile", "private_ip")
    assert privacy.parse_rules("") == ()
    assert privacy.parse_rules(None) == ()


def test_email_and_cn_mobile_masked():
    text, hits = privacy.redact_text(
        "联系人 alice@example.com 手机 13800138000，备用 19912345678。", "email,cn_mobile")
    assert text == "联系人 [email] 手机 [phone]，备用 [phone]。"
    assert hits == 3


def test_cn_mobile_needs_digit_boundaries():
    """订单号里的 11 位片段不是手机号（前后紧贴数字时不匹配）。"""
    text, hits = privacy.redact_text("订单 9913800138000123 保持原样", "cn_mobile")
    assert text == "订单 9913800138000123 保持原样"
    assert hits == 0
    # 字母包着的仍是手机号（边界只认数字）
    assert privacy.redact_text("x13800138000y", "cn_mobile") == ("x[phone]y", 1)


def test_private_ip_only_rfc1918():
    text, hits = privacy.redact_text(
        "内网 10.1.2.3 / 192.168.0.9 / 172.16.4.4，公网 8.8.8.8 与 172.32.0.1", "private_ip")
    assert text == ("内网 [private-ip] / [private-ip] / [private-ip]，"
                    "公网 8.8.8.8 与 172.32.0.1")
    assert hits == 3


def test_url_credentials_rule_runs_before_email():
    """先吃 userinfo，否则邮箱规则会把 `user:pass@host` 拆掉。"""
    text, hits = privacy.redact_text(
        "连接 postgres://alice:p%40ss@db.internal:5432/app", "email,url_credentials")
    assert text == "连接 postgres://[credentials]@db.internal:5432/app"
    assert hits == 1


def test_payload_masks_messages_blocks_and_tool_descriptions():
    body = {
        "messages": [
            {"role": "user", "content": "写信给 a@b.com"},
            {"role": "system", "content": [
                {"type": "text", "text": "内网 10.0.0.1"},
                {"type": "image_url", "image_url": {"url": "a@b.com"}},
                {"type": "text", "text": 123},
            ]},
            "junk",
        ],
        "tools": [
            {"type": "function", "function": {
                "name": "f", "description": "联系 13800138000",
                "parameters": {"type": "object", "default": "a@b.com"}}},
            "junk",
        ],
    }
    out, hits = privacy.redact_payload(body, "email,cn_mobile,private_ip")
    assert hits == 3
    assert out["messages"][0]["content"] == "写信给 [email]"
    blocks = out["messages"][1]["content"]
    assert blocks[0]["text"] == "内网 [private-ip]"
    assert blocks[1]["image_url"]["url"] == "a@b.com"       # 非文本块不动
    assert blocks[2]["text"] == 123
    assert out["messages"][2] == "junk"
    function = out["tools"][0]["function"]
    assert function["description"] == "联系 [phone]"
    assert function["parameters"]["default"] == "a@b.com"   # schema 不动
    assert out["tools"][1] == "junk"


def test_payload_copy_does_not_mutate_input():
    """出站脱敏绝不改原文：`request.raw` 是粘性指纹与续写的输入。"""
    body = {"messages": [{"role": "user", "content": "a@b.com"}]}
    snapshot = copy.deepcopy(body)
    out, _ = privacy.redact_payload(body, "email")
    assert body == snapshot
    assert out["messages"][0]["content"] == "[email]"


def test_payload_ignores_broken_shapes():
    body = {"messages": "nope", "tools": [{"function": {"description": "a@b.com"}}]}
    out, hits = privacy.redact_payload(body, "email")
    assert hits == 1 and out["tools"][0]["function"]["description"] == "[email]"

    body = {"messages": [
        {"role": "user"},                       # 无 content
        {"role": "user", "content": None},
        {"role": "user", "content": {"a": 1}},
    ], "tools": [{"function": None}, {}, {"function": {"description": 5}}]}
    out, hits = privacy.redact_payload(body, "email")
    assert hits == 0 and out == body


def test_redaction_is_idempotent_on_placeholders():
    text, _ = privacy.redact_text("[email] [phone] [private-ip]", "email,cn_mobile,private_ip")
    assert text == "[email] [phone] [private-ip]"