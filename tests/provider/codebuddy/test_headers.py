"""CodeBuddy 出站请求头：身份拼装、企业/个人上下文、客户端指纹变体。"""

from __future__ import annotations

from src.provider.codebuddy.client import CodeBuddyCredential, build_headers
from src.provider.codebuddy.headers import encode_department, host_of


def test_host_and_domain_derived_from_same_endpoint():
    headers = build_headers(CodeBuddyCredential(bearer_token="t"), "https://copilot.tencent.com")
    assert headers["Host"] == headers["X-Domain"] == "copilot.tencent.com"

def test_department_encoded_as_utf8_percent():
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", enterprise_id="e",
                            department_full_name="技术部/平台组"),
        "https://copilot.tencent.com")
    encoded = "%E6%8A%80%E6%9C%AF%E9%83%A8%2F%E5%B9%B3%E5%8F%B0%E7%BB%84"
    assert headers["X-Department-Info"] == encoded
    assert headers["X-Enterprise-Id"] == "e"

def test_account_uid_takes_priority_over_user_id():
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", user_id="user", account_uid="acct"),
        "https://copilot.tencent.com")
    assert headers["X-User-Id"] == "acct"

def test_quota_only_headers_drop_enterprise_context():
    """手动凭证的额度探测只切接口，不发企业上下文头（AGENTS.md 约束）。"""
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", enterprise_id="e",
                            department_full_name="技术部", user_id="u"),
        "https://copilot.tencent.com", quota_only=True)
    assert "X-Enterprise-Id" not in headers
    assert "X-Department-Info" not in headers
    assert "X-User-Id" not in headers

def test_host_of_and_encode_department_helpers():
    assert host_of("https://www.codebuddy.ai") == "www.codebuddy.ai"
    assert encode_department("") == ""

def test_auth_start_headers_mark_anonymous():
    from src.provider.codebuddy.headers import auth_start_headers

    headers = auth_start_headers("copilot.tencent.com")
    assert headers["X-No-Authorization"] == "true"
    assert "Authorization" not in headers

def test_chat_headers_carry_channel_identity():
    """聊天头必须带完整渠道特征（缺失触发 11128 渠道风控）。"""
    from src.provider.codebuddy.headers import generate_headers

    headers = generate_headers(
        endpoint="https://copilot.tencent.com", bearer_token="t",
        user_id="u1", enterprise_id="ent1")
    # 会话链路（每请求随机）
    assert headers["X-Conversation-ID"] != headers["X-Request-ID"]
    assert len(headers["X-Conversation-Request-ID"]) == 32
    # 渠道身份
    assert headers["X-Agent-Intent"] == "craft"
    assert headers["X-Agent-Purpose"] == "conversation"
    assert headers["X-IDE-Type"] == headers["X-IDE-Name"] == "CLI"
    assert headers["X-CodeBuddy-Request"] == "1"
    assert headers["X-Private-Data"] == "false"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    # OpenAI JS SDK 指纹
    assert headers["x-stainless-lang"] == "js"
    assert headers["x-stainless-runtime"] == "node"
    assert headers["x-stainless-retry-count"] == "0"
    # 企业租户
    assert headers["X-Tenant-Id"] == "ent1"

def test_quota_only_headers_carry_full_identity():
    """quota_only 只切换 URL；头部与聊天一致（原实现不区分，防渠道校验）。"""
    from src.provider.codebuddy.headers import generate_headers

    headers = generate_headers(
        endpoint="https://copilot.tencent.com", bearer_token="t", quota_only=True)
    assert "X-Conversation-ID" in headers
    assert headers["X-Agent-Intent"] == "craft"

def test_stainless_fingerprint_variants():
    """x-stainless-arch/os 的平台分支（headers 64-79）。"""
    from unittest import mock

    from src.provider.codebuddy import headers as h

    with mock.patch.object(h.platform, "machine", return_value="x86_64"):
        assert h._stainless_arch() == "x64"
    with mock.patch.object(h.platform, "machine", return_value="aarch64"):
        assert h._stainless_arch() == "arm64"
    with mock.patch.object(h.platform, "machine", return_value="riscv"):
        assert h._stainless_arch() == "riscv"
    with mock.patch.object(h.platform, "system", return_value="Darwin"):
        assert h._stainless_os() == "MacOS"
    with mock.patch.object(h.platform, "system", return_value="Windows"):
        assert h._stainless_os() == "Windows"
    with mock.patch.object(h.platform, "system", return_value="Linux"):
        assert h._stainless_os() == "Linux"
    with mock.patch.object(h.platform, "system", return_value="SunOS"):
        assert h._stainless_os() == "SunOS"


# ------------------------------------------------- 国际站指纹（2026-10-29）

def _jwt(claims: dict) -> str:
    import base64
    import json

    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def test_intl_profile_uses_current_cli_versions_and_extra_headers():
    """国际站换官方现行版本常量，并补会话/模型/链路头（缺一即自动化信号）。"""
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t"), "https://www.codebuddy.ai",
        intl=True, conversation_seed="sess-1", model="deepseek-v4.1-flash")
    assert headers["User-Agent"] == "CLI/2.178.0 CodeBuddy/2.178.0"
    assert headers["X-IDE-Version"] == "2.178.0"
    assert headers["X-Product-Version"] == "2.178.0"
    assert headers["x-stainless-package-version"] == "7.39.0"
    assert headers["x-stainless-runtime-version"] == "v26.7.0"
    assert headers["X-Trace-ID"] == headers["X-Request-ID"]
    assert headers["X-Session-ID"] == headers["X-Conversation-ID"]
    assert headers["X-Model-ID"] == "deepseek-v4.1-flash"


def test_intl_conversation_id_is_stable_uuid_v1_per_session():
    """同一会话每轮同一个 id（官方 CLI 形态）；不同会话互不相同。"""
    first = build_headers(CodeBuddyCredential(bearer_token="t"),
                          "https://www.codebuddy.ai", intl=True,
                          conversation_seed="conv-a")
    second = build_headers(CodeBuddyCredential(bearer_token="t"),
                           "https://www.codebuddy.ai", intl=True,
                           conversation_seed="conv-a")
    other = build_headers(CodeBuddyCredential(bearer_token="t"),
                          "https://www.codebuddy.ai", intl=True,
                          conversation_seed="conv-b")
    assert first["X-Conversation-ID"] == second["X-Conversation-ID"]
    assert other["X-Conversation-ID"] != first["X-Conversation-ID"]
    # v1 的版本位在第三段首位
    assert first["X-Conversation-ID"].split("-")[2][0] == "1"
    assert len(first["X-Conversation-ID"]) == 36


def test_intl_without_seed_falls_back_to_random_conversation_id():
    a = build_headers(CodeBuddyCredential(bearer_token="t"),
                      "https://www.codebuddy.ai", intl=True)
    b = build_headers(CodeBuddyCredential(bearer_token="t"),
                      "https://www.codebuddy.ai", intl=True)
    assert a["X-Conversation-ID"] != b["X-Conversation-ID"]
    assert "X-Model-ID" not in a


def test_cn_profile_stays_byte_identical():
    """国内站正在稳定工作：版本常量与「逐请求随机」的行为都不动。"""
    headers = build_headers(CodeBuddyCredential(bearer_token="t"),
                            "https://copilot.tencent.com")
    assert headers["User-Agent"] == "CLI/2.107.0 CodeBuddy/2.107.0"
    assert headers["x-stainless-package-version"] == "6.25.0"
    for extra in ("X-Session-ID", "X-Trace-ID", "X-Product-Version", "X-Model-ID"):
        assert extra not in headers
    again = build_headers(CodeBuddyCredential(bearer_token="t"),
                          "https://copilot.tencent.com")
    assert again["X-Conversation-ID"] != headers["X-Conversation-ID"]


def test_intl_user_id_falls_back_to_jwt_sub():
    """本地 intl 凭证的 user_id/account_uid 都是空串；官方 CLI 每请求都发 uid。"""
    token = _jwt({"sub": "3455d334-5b72-49f9", "iss": "https://www.codebuddy.ai"})
    headers = build_headers(CodeBuddyCredential(bearer_token=token),
                            "https://www.codebuddy.ai", intl=True)
    assert headers["X-User-Id"] == "3455d334-5b72-49f9"
    # 显式 uid 优先
    explicit = build_headers(
        CodeBuddyCredential(bearer_token=token, account_uid="acct"),
        "https://www.codebuddy.ai", intl=True)
    assert explicit["X-User-Id"] == "acct"
    # 国内站不回退：历史形态逐字节不变
    assert "X-User-Id" not in build_headers(CodeBuddyCredential(bearer_token=token),
                                            "https://copilot.tencent.com")


def test_intl_quota_only_headers_skip_identity():
    headers = build_headers(
        CodeBuddyCredential(bearer_token=_jwt({"sub": "u"})),
        "https://www.codebuddy.ai", intl=True, quota_only=True)
    assert "X-User-Id" not in headers
    assert headers["X-Session-ID"]  # 指纹头照发，只有身份头在 quota_only 下不发


def test_uid_from_token_rejects_malformed_input():
    from src.provider.codebuddy.headers import uid_from_token

    assert uid_from_token("") == ""
    assert uid_from_token("only.two") == ""
    assert uid_from_token("a.!!!.c") == ""
    assert uid_from_token(_jwt(["not", "an", "object"])) == ""
    assert uid_from_token(_jwt({"sub": 42})) == ""
    assert uid_from_token(_jwt({"iss": "x"})) == ""
    assert uid_from_token(_jwt({"sub": "u-1"})) == "u-1"


def test_conversation_uuid_cache_is_bounded(monkeypatch):
    """条目上限：超量整体清空（会话 id 只是指纹，清空只影响一次）。"""
    from src.provider.codebuddy import headers as hdrs

    monkeypatch.setattr(hdrs, "_CONVERSATION_CREATED_AT", {})
    monkeypatch.setattr(hdrs, "_CONVERSATION_CACHE_MAX", 2)
    hdrs.conversation_uuid("a")
    hdrs.conversation_uuid("b")
    assert len(hdrs._CONVERSATION_CREATED_AT) == 2
    hdrs.conversation_uuid("c")
    assert list(hdrs._CONVERSATION_CREATED_AT) == ["c"]
