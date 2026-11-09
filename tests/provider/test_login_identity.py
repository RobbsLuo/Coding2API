"""登录合并键（`provider/login_identity.py`）与合并落库策略。"""

from __future__ import annotations

from src.provider.codearts import CodeArtsProvider
from src.provider.login_identity import (
    LOGIN_IDENTITIES,
    LOGIN_IDENTITY_SETS,
    codearts_login_identities,
    codearts_login_identity,
    login_identities_for,
    login_identity_for,
    officeace_login_identities,
    officeace_login_identity,
)
from src.provider.officeace import OfficeAceProvider


def test_registry_declares_only_mergeable_channels():
    assert set(LOGIN_IDENTITIES) == {"codearts", "officeace", "lingxi"}
    # 首选身份与别名集合必须覆盖同一批渠道，否则「能合并」与「找得到」会错开
    assert set(LOGIN_IDENTITY_SETS) == set(LOGIN_IDENTITIES)


def test_codearts_identity_prefers_uid_then_domain_and_name():
    assert codearts_login_identity({
        "uid": "u1", "domain_id": "d", "user_name": "n"}) == "u1"
    # camelCase 落盘形态同样认（历史凭证）
    assert codearts_login_identity({"userId": "u2"}) == "u2"
    assert codearts_login_identity({
        "uid": "", "domainId": "d", "userName": "n"}) == "d|n"
    assert codearts_login_identity({"uid": "", "domain_id": "d"}) == ""
    assert codearts_login_identity({}) == ""
    assert codearts_login_identity({"uid": "  "}) == ""


def test_codearts_identity_set_holds_all_aliases():
    """别名集合让「身份补全失败」的登录仍能命中老行。

    老行只有 `uid`、新登录只有 `domain_id|user_name`（`_fill_identity` 网络
    失败），单键比较永远不等 → 会新增重复凭证；交集判定才能合并。
    """
    # 两个别名都给
    assert codearts_login_identities(
        {"uid": "u1", "domain_id": "d", "user_name": "n"}) == {"u1", "d|n"}
    # 只有 uid（老行形态）
    assert codearts_login_identities({"uid": "u1", "domain_id": "d"}) == {"u1"}
    # 只有兜底键（身份补全失败的登录形态）
    assert codearts_login_identities(
        {"domain_id": "d", "user_name": "n"}) == {"d|n"}
    assert codearts_login_identities({}) == frozenset()
    assert codearts_login_identities({"uid": "  ", "domain_id": " "}) == frozenset()


def test_officeace_identity_prefers_account_then_principal():
    assert officeace_login_identity({"account_id": "a"}) == "a"
    assert officeace_login_identity({"accountId": "a"}) == "a"
    assert officeace_login_identity({"principal_id": "p"}) == "p"
    assert officeace_login_identity({"account_id": "", "principalId": "p"}) == "p"
    assert officeace_login_identity({}) == ""


def test_officeace_identity_set_holds_all_aliases():
    assert officeace_login_identities(
        {"account_id": "a", "principal_id": "p"}) == {"a", "p"}
    assert officeace_login_identities({"accountId": "a"}) == {"a"}
    assert officeace_login_identities({}) == frozenset()


def test_login_identity_for_rejects_unknown_channel_and_bad_payload():
    assert login_identity_for("trae", {"accessToken": "t"}) == ""
    assert login_identity_for("codearts", {"uid": "u1"}) == "u1"
    # 非 dict 的畸形凭证不该抛（登录路径不能因它整体失败）
    assert login_identity_for("codearts", None) == ""  # type: ignore[arg-type]
    assert login_identity_for("officeace", "nope") == ""  # type: ignore[arg-type]


def test_login_identities_for_rejects_unknown_channel_and_bad_payload():
    assert login_identities_for("trae", {"accessToken": "t"}) == frozenset()
    assert login_identities_for("codearts", {"uid": "u1"}) == {"u1"}
    # 非 dict 的畸形凭证不该抛（登录路径不能因它整体失败）
    assert login_identities_for("codearts", None) == frozenset()  # type: ignore[arg-type]
    assert login_identities_for("officeace", "nope") == frozenset()  # type: ignore[arg-type]


def test_login_identities_for_drops_non_string_aliases():
    """畸形 getter 返回值（非 iterable / 字符串 / 含非字符串项）一律安全退化。"""
    from src.provider import login_identity as module

    original_sets = dict(module.LOGIN_IDENTITY_SETS)
    try:
        module.LOGIN_IDENTITY_SETS["weird"] = lambda _data: "u1"        # 字符串不算集合
        module.LOGIN_IDENTITY_SETS["scalar"] = lambda _data: 42          # 非 iterable
        module.LOGIN_IDENTITY_SETS["mixed"] = lambda _data: {"u1", "", None, 5}
        module.LOGIN_IDENTITY_SETS["boom"] = _raiser
        assert module.login_identities_for("weird", {}) == frozenset()
        assert module.login_identities_for("scalar", {}) == frozenset()
        assert module.login_identities_for("mixed", {}) == {"u1"}
        # getter 抛错 → 空集（畸形凭证不影响登录）
        assert module.login_identities_for("boom", {}) == frozenset()
    finally:
        module.LOGIN_IDENTITY_SETS.clear()
        module.LOGIN_IDENTITY_SETS.update(original_sets)


def _raiser(_data):
    raise ValueError("malformed credential")


def test_provider_facade_delegates_to_shared_identity():
    assert CodeArtsProvider().login_identity({"uid": "u1"}) == "u1"
    assert OfficeAceProvider().login_identity({"account_id": "a"}) == "a"
    # 与注册表同源：脚本与登录路径不会各判一套
    assert CodeArtsProvider().login_identity({"uid": "x"}) == login_identity_for(
        "codearts", {"uid": "x"})
