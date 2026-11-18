"""OpenAI chat 入站请求：`response_format` 语义收口（与 Responses 出口共用）。

此前 chat 入站整包透传，畸形/未知的 `response_format` 会一路打到上游、按 4xx
冷却凭证；这里固定「放行三种形状、其余显式 400、未知字段丢弃」的口径。
"""

from __future__ import annotations

import pytest

from src.compat.openai.request import InvalidRequest, normalize_response_format, parse_chat_request

MESSAGES = [{"role": "user", "content": "hi"}]


def _parse(**overrides) -> dict:
    body = {"messages": MESSAGES, "model": "m"}
    body.update(overrides)
    return parse_chat_request(body).raw


@pytest.mark.parametrize("kind", ["text", "json_object"])
def test_simple_types_are_normalized(kind):
    """`text` / `json_object` 只留 type：多余字段既非规范也无上游认。"""
    raw = _parse(response_format={"type": kind, "name": "noise"})
    assert raw["response_format"] == {"type": kind}

def test_json_schema_keeps_known_fields_only():
    raw = _parse(response_format={
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": {"type": "object"},
                        "description": "d", "strict": True, "bogus": 1},
    })
    assert raw["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": {"type": "object"},
                        "description": "d", "strict": True},
    }

def test_json_schema_optional_fields_are_omitted():
    """`description` / `strict` 可选：非预期类型直接丢，不伪造。"""
    normalized = normalize_response_format({
        "type": "json_schema",
        "json_schema": {"name": "n", "schema": {}, "description": 5, "strict": "yes"},
    })
    assert normalized == {"type": "json_schema",
                          "json_schema": {"name": "n", "schema": {}}}

def test_absent_response_format_stays_absent():
    assert "response_format" not in _parse()
    # 显式 null 原样保留（与其它透传字段一致）：上游按「未指定」处理
    assert _parse(response_format=None)["response_format"] is None

@pytest.mark.parametrize("value, message", [
    ("json", "response_format must be an object"),
    ({"type": "audio"}, "response_format.type 'audio' is not supported"),
    ({"type": "json_schema"}, "response_format.json_schema must be an object"),
    ({"type": "json_schema", "json_schema": []},
     "response_format.json_schema must be an object"),
    ({"type": "json_schema", "json_schema": {"schema": {}}},
     "response_format.name must be a non-empty string"),
    ({"type": "json_schema", "json_schema": {"name": "", "schema": {}}},
     "response_format.name must be a non-empty string"),
    ({"type": "json_schema", "json_schema": {"name": "n"}},
     "response_format.schema is required"),
])
def test_invalid_shapes_raise_400(value, message):
    with pytest.raises(InvalidRequest, match=message):
        _parse(response_format=value)

def test_prefix_only_changes_message_path():
    """Responses 侧传 `text.format` 保持既有报错文案，校验逻辑完全一致。"""
    with pytest.raises(InvalidRequest, match=r"text\.format\.type"):
        normalize_response_format({"type": "nope"}, prefix="text.format")
    assert normalize_response_format({"type": "text"}, prefix="text.format") == {
        "type": "text"}


# ----------------------------------------------------------------- A3：`n` 口径
#
# 出口结构上只能产生单个 choice（`provider/openai_chunk.py::first_choice` 只取
# choices[0]，`compat/openai/response.py` 恒单 choice），所以 `n` 只接受 1；
# 其余显式 400，而不是静默只回一个候选。


def test_n_one_is_accepted_and_passed_through():
    assert _parse(n=1)["n"] == 1


def test_n_absent_is_accepted():
    """缺省即 1（OpenAI 默认），不得因为收紧而变成必填。"""
    assert "n" not in _parse()


@pytest.mark.parametrize("value", [0, 2, 3, 8, 99])
def test_n_other_than_one_is_400(value):
    with pytest.raises(InvalidRequest, match="n must be 1"):
        _parse(n=value)


@pytest.mark.parametrize("value", [True, False, "1", 1.0, None])
def test_n_non_integer_is_400(value):
    """bool / 字符串 / 浮点 / null 都不是合法的 `n`（不能悄悄当成 1）。"""
    with pytest.raises(InvalidRequest, match="n must be an integer"):
        _parse(n=value)


def test_n_error_message_explains_structural_limit():
    """文案要说清「不是范围问题，是结构不支持」，否则用户会以为调到 2 就好。"""
    with pytest.raises(InvalidRequest) as caught:
        _parse(n=2)
    assert "single choice" in str(caught.value)
    assert "n=2" in str(caught.value)


@pytest.mark.parametrize("role", ["system", "user", "assistant", "tool", "developer"])
def test_known_roles_are_accepted(role):
    """协议全集放行；`developer` 也是合法入站值（出站再归一为 system）。"""
    assert _parse(messages=[{"role": role, "content": "x"}])["messages"][0]["role"] == role


@pytest.mark.parametrize("role", ["", " ", "robot", "Developer", "0", "SYSTEM"])
def test_unknown_roles_are_400_at_the_edge(role):
    """空串 / 拼错 / 大小写不符的角色在上游必被拒，入口就地 400。

    CodeArts 对空角色回 `001001044 Message role cannot empty`、qoder 回
    `unknown variant`——都是一次请求在每个号上各撞一次才失败，还会落一条
    `invalid_request` 明细。入口拦下后：不发上游、不冷却凭证、不记失败。
    """
    with pytest.raises(InvalidRequest, match=r"messages\[0\]\.role must be one of"):
        _parse(messages=[{"role": role, "content": "x"}])


def test_role_whitelist_reports_index_and_allowed_values():
    """报错要指出**哪一条**消息、以及允许哪些值（多轮里定位不到就无法自查）。"""
    with pytest.raises(InvalidRequest) as caught:
        _parse(messages=[{"role": "user", "content": "hi"},
                         {"role": "", "content": "boom"}])
    text = str(caught.value)
    assert "messages[1].role" in text
    for role in ("system", "user", "assistant", "tool", "developer"):
        assert role in text


def test_non_string_role_keeps_its_own_message():
    """非字符串角色仍报「必须是字符串」：与「取值不合法」是两个不同缺陷。"""
    with pytest.raises(InvalidRequest, match="must be a string"):
        _parse(messages=[{"role": None, "content": "x"}])
