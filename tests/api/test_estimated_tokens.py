"""转发前 TPM 预估（`api.endpoints.estimated_tokens`）。

真实用量要等响应结束才知道，而 TPM 必须在转发前裁决。客户端不传输出上限时
早先按 `output=0` 计，于是不带上限的客户端在 TPM 口径里只算输入——同一分钟
可无限产 token，限流等于没有。现在缺省回落 `DEFAULT_OUTPUT_TOKEN_ESTIMATE`。
"""

from __future__ import annotations

import pytest

from src.api.endpoints import DEFAULT_OUTPUT_TOKEN_ESTIMATE, estimated_tokens
from src.compat.openai.request import ChatRequest, parse_chat_request

# 单条 `hi` 的启发式输入估算（`engine.compress.estimate_messages` = 0.5 → int() = 0）
_INPUT = 0


def _request(**extra) -> ChatRequest:
    """直接构造 `ChatRequest`：绕过解析层校验，覆盖「畸形上限」分支。

    走 `parse_chat_request` 时 `0` / 负数 / 非 int 会在入口 400，函数根本见不到
    这些值；但 `estimated_tokens` 仍要自己防御（它也被 Playground 直接调用）。
    """
    raw = {"messages": [{"role": "user", "content": "hi"}], **extra}
    return ChatRequest(model="glm-5.2", messages=raw["messages"], stream=False,
                       raw=raw)


def test_default_output_estimate_is_conservative_placeholder():
    assert DEFAULT_OUTPUT_TOKEN_ESTIMATE == 1024


def test_without_output_limit_uses_default_estimate():
    """不传上限 → 输入 + 1024（保守占位，宁可少放一个请求）。"""
    assert estimated_tokens(_request()) == _INPUT + DEFAULT_OUTPUT_TOKEN_ESTIMATE
    # 显式 null 与「没传」同口径（OpenAI 客户端会把未设置写成 null）
    assert estimated_tokens(_request(max_tokens=None)) == (
        _INPUT + DEFAULT_OUTPUT_TOKEN_ESTIMATE)


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens"])
def test_explicit_output_limit_is_used(key):
    assert estimated_tokens(_request(**{key: 4096})) == _INPUT + 4096


def test_larger_of_two_limits_wins():
    """两个上限同时给时取大者（与旧口径一致的 `max(...)` 语义）。"""
    assert estimated_tokens(_request(max_tokens=100,
                                     max_completion_tokens=900)) == _INPUT + 900
    assert estimated_tokens(_request(max_tokens=900,
                                     max_completion_tokens=100)) == _INPUT + 900


@pytest.mark.parametrize("bad", [0, -1, -4096, "4096", 3.5, True, False])
def test_invalid_output_limit_falls_back_to_default(bad):
    """0 / 负数 / 非 int / bool 都回落默认值，绝不让预估变小。"""
    assert estimated_tokens(_request(max_tokens=bad)) == (
        _INPUT + DEFAULT_OUTPUT_TOKEN_ESTIMATE)


def test_parsed_request_without_limit_also_gets_default():
    """走真实解析链路（API 的常规路径）同样带上默认输出估算。"""
    parsed = parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                                 "model": "glm-5.2"})
    assert estimated_tokens(parsed) == _INPUT + DEFAULT_OUTPUT_TOKEN_ESTIMATE
