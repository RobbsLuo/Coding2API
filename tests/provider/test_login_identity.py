"""登录合并键（`provider/login_identity.py`）与合并落库策略。"""

from __future__ import annotations

from src.provider.codearts import CodeArtsProvider
from src.provider.login_identity import (
    LOGIN_IDENTITIES,
    codearts_login_identity,
    login_identity_for,
    officeace_login_identity,
)
from src.provider.officeace import OfficeAceProvider


def test_registry_declares_only_mergeable_channels():
    assert set(LOGIN_IDENTITIES) == {"codearts", "officeace"}


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


def test_officeace_identity_prefers_account_then_principal():
    assert officeace_login_identity({"account_id": "a"}) == "a"
    assert officeace_login_identity({"accountId": "a"}) == "a"
    assert officeace_login_identity({"principal_id": "p"}) == "p"
    assert officeace_login_identity({"account_id": "", "principalId": "p"}) == "p"
    assert officeace_login_identity({}) == ""


def test_login_identity_for_rejects_unknown_channel_and_bad_payload():
    assert login_identity_for("trae", {"accessToken": "t"}) == ""
    assert login_identity_for("codearts", {"uid": "u1"}) == "u1"
    # 非 dict 的畸形凭证不该抛（登录路径不能因它整体失败）
    assert login_identity_for("codearts", None) == ""  # type: ignore[arg-type]
    assert login_identity_for("officeace", "nope") == ""  # type: ignore[arg-type]


def test_provider_facade_delegates_to_shared_identity():
    assert CodeArtsProvider().login_identity({"uid": "u1"}) == "u1"
    assert OfficeAceProvider().login_identity({"account_id": "a"}) == "a"
    # 与注册表同源：脚本与登录路径不会各判一套
    assert CodeArtsProvider().login_identity({"uid": "x"}) == login_identity_for(
        "codearts", {"uid": "x"})
