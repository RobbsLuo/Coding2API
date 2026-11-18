"""`developer` 角色归一：`provider.base.normalize_developer_role` 与各渠道出站体。

OpenAI 在 chat 协议里补了 `developer`（PI 一类客户端对 reasoning 模型会把
system 转成 developer），与 `system` 语义等价。各渠道对它的容忍度不同，且
失败形态都不直观：qoder 400 未知变体、codearts 流内 `001001044`、codebuddy
11128 渠道风控、trae 静默空流。归一必须收口在一处，否则新增渠道会再漏一遍。
"""

from __future__ import annotations

from src.provider.base import normalize_developer_role


def test_normalize_developer_role_rewrites_and_counts():
    messages = [{"role": "developer", "content": "sys"},
                {"role": "user", "content": "hi"},
                {"role": "developer", "content": "sys2"}]
    assert normalize_developer_role(messages) == 2
    assert [m["role"] for m in messages] == ["system", "user", "system"]
    # 幂等：再跑一次无改写
    assert normalize_developer_role(messages) == 0


def test_normalize_developer_role_tolerates_odd_shapes():
    """非 list / 非 dict 条目原样跳过：形状交给上游裁决，本层只改角色变体。"""
    assert normalize_developer_role("nope") == 0
    assert normalize_developer_role(None) == 0
    assert normalize_developer_role([None, "junk", {"role": "user"}]) == 0
    # 已经是 system 的不动
    assert normalize_developer_role([{"role": "system"}]) == 0


def test_every_provider_body_normalizes_developer_role():
    """六条 OpenAI 形状渠道的出站体都把 developer 归一为 system。

    只断言「出站体里没有 developer」+「原文未被穿透改写」：具体字段排布各渠道
    不同（zen 注入门禁工具、officeace 改写 max_tokens、lingxi 走 agent-runtime
    形状），但这一条必须全部成立。
    """
    from src.provider.codearts import client as codearts_client
    from src.provider.kilo import client as kilo_client
    from src.provider.lingxi import client as lingxi_client
    from src.provider.officeace import client as officeace_client
    from src.provider.raccoon import client as raccoon_client
    from src.provider.zen import client as zen_client

    payload = {"messages": [{"role": "developer", "content": "sys"},
                            {"role": "user", "content": "hi"}]}

    bodies = [
        codearts_client.prepare_body(payload, "m", chat_id="c")["messages"],
        raccoon_client.prepare_body(payload, "m")["messages"],
        kilo_client.prepare_body(payload, "m")["messages"],
        officeace_client.prepare_body(payload, "m")[0]["messages"],
        zen_client.prepare_body(payload, "m")[0]["messages"],
        lingxi_client.build_body(payload, "m")["messages"],
    ]
    for messages in bodies:
        assert [m["role"] for m in messages] == ["system", "user"]
    # 归一作用于深拷贝：引擎持有的原文必须还是 developer（会话粘性指纹读它）
    assert payload["messages"][0]["role"] == "developer"


def test_qoder_wrapper_delegates_to_shared_helper():
    """qoder 保留薄封装（内部调用点不动的代价最小），行为与共用实现一致。"""
    from src.provider.qoder.client import _normalize_developer_role

    messages = [{"role": "developer", "content": "sys"}]
    assert _normalize_developer_role(messages) == 1
    assert messages[0]["role"] == "system"
    assert _normalize_developer_role([]) == 0
