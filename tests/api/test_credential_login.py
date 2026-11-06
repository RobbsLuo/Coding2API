"""OAuth 登录落库：同账号重新登录合并既有凭证。"""

from __future__ import annotations

import asyncio

from src.api.credential_login import save_login_credential
from src.provider.base import AuthResult, Quota


class _Provider:
    """只实现入口用到的方法（提交后由 `schedule_probe` 调 probe）。"""

    id = "codearts"

    async def probe_quota(self, _data: dict) -> Quota:
        return Quota(remaining=1, total=1, probed_at=1)


def _services(admin_client) -> object:
    app, _client = admin_client
    app.state.services.registry["codearts"] = _Provider()
    return app.state.services


async def test_save_login_credential_merges_same_identity(admin_client):
    services = _services(admin_client)
    old_id = services.credentials.add(
        provider="codearts", credential_data={"uid": "u1", "token": "old"},
        nickname="old")

    credential_id, merged = await save_login_credential(
        services, provider_id="codearts",
        credential_data={"uid": "u1", "token": "new"},
        nickname="new", added_by="root")

    assert merged is True and credential_id == old_id
    assert services.credentials.credential_data(old_id) == {"uid": "u1", "token": "new"}
    assert [row["id"] for row in services.credentials.list_all()
            if row["provider"] == "codearts"] == [old_id]


async def test_save_login_credential_adds_for_unsupported_or_unknown_identity(
        admin_client):
    services = _services(admin_client)

    # 渠道没有声明身份（TRAE 等）→ 两次都新增
    first, merged = await save_login_credential(
        services, provider_id="trae", credential_data={"accessToken": "t"},
        nickname="n", added_by="root")
    assert merged is False
    second, merged = await save_login_credential(
        services, provider_id="trae", credential_data={"accessToken": "t"},
        nickname="n", added_by="root")
    assert merged is False and second != first

    # 声明了身份但这次凭证身份为空（token 响应没带 uid）→ 不合并
    blank, merged = await save_login_credential(
        services, provider_id="codearts", credential_data={"access_key_id": "AK"},
        nickname="n", added_by="root")
    assert merged is False

    # 有身份但没有既有凭证 → 新增
    fresh, merged = await save_login_credential(
        services, provider_id="codearts", credential_data={"uid": "nobody"},
        nickname="n", added_by="root")
    assert merged is False and fresh != blank


async def test_save_login_credential_merges_when_only_fallback_identity_matches(admin_client):
    """身份补全失败、只能给出兜底键时，仍然合并到老行（不新增重复凭证）。

    老行有 `uid`，这次登录的 token 响应没带 uid（`_fill_identity` 网络失败），
    只能推出 `domain_id|user_name`。单键比较必然错开 → 重复行。
    """
    services = _services(admin_client)
    old_id = services.credentials.add(
        provider="codearts",
        credential_data={"uid": "u1", "domain_id": "d", "user_name": "n"},
        nickname="old")

    credential_id, merged = await save_login_credential(
        services, provider_id="codearts",
        credential_data={"domain_id": "d", "user_name": "n", "token": "new"},
        nickname="new", added_by="root")

    assert merged is True and credential_id == old_id
    assert [row["id"] for row in services.credentials.list_all()
            if row["provider"] == "codearts"] == [old_id]


async def test_save_login_credential_serializes_concurrent_same_account_logins(
        admin_client):
    """同一渠道并发登录串行化：不会各查到 None 再各插一行。

    没有渠道锁时两个 coroutine 都会在 `find_login_credential` 里读到空库，
    然后各走 `add()` —— 池子里两条同账号凭证（本功能要消除的重复）。
    """
    services = _services(admin_client)

    first, second = await asyncio.gather(
        save_login_credential(
            services, provider_id="codearts",
            credential_data={"uid": "u1", "token": "a"},
            nickname="n", added_by="root"),
        save_login_credential(
            services, provider_id="codearts",
            credential_data={"uid": "u1", "token": "b"},
            nickname="n", added_by="root"))

    rows = [row for row in services.credentials.list_all()
            if row["provider"] == "codearts"]
    assert len(rows) == 1
    # 先落库的那次新增，后一次命中同一条 → 合并
    assert {first[0], second[0]} == {rows[0]["id"]}
    assert sorted([first[1], second[1]]) == [False, True]


async def test_save_login_credential_falls_back_when_merge_row_vanished(admin_client):
    """查到 id 到加锁更新之间被删除 → 不能报成功，退回新增。"""
    services = _services(admin_client)
    original = services.credentials.find_login_credential

    services.credentials.find_login_credential = lambda *_a, **_kw: "cred_ghost"
    try:
        credential_id, merged = await save_login_credential(
            services, provider_id="codearts", credential_data={"uid": "u1"},
            nickname="n", added_by="root")
    finally:
        services.credentials.find_login_credential = original
    assert merged is False
    assert credential_id != "cred_ghost"
    assert services.credentials.provider_of(credential_id) == "codearts"


def test_upstream_poll_merges_relogin_and_revives_credential(admin_client):
    """两次登录同一 uid：第二次复用原 id（而不是新增），并解除硬禁用。"""
    app, client = admin_client
    app.state.services.registry["codearts"] = _Provider()
    credential_id = app.state.credentials.add(
        provider="codearts", credential_data={"uid": "u1", "token": "old"})
    app.state.credentials.mark_relogin_required(credential_id, "refresh token revoked")

    current = {"token": "old"}

    class _OAuth:
        async def poll(self, _state, _username):
            return AuthResult(credential_data={"uid": "u1", "token": current["token"]},
                              nickname="nick")

    app.state.upstream_auth["codearts"] = _OAuth()

    first = client.post("/api/auth/upstream/poll",
                        json={"provider": "codearts", "state": "s1"}).json()
    assert first == {"status": "success", "credential_id": credential_id,
                     "merged": True}
    rows = [row for row in app.state.credentials.list_all()
            if row["provider"] == "codearts"]
    assert len(rows) == 1
    assert rows[0]["disabled"] == 0 and rows[0]["disabled_reason"] is None
    assert app.state.credentials.credential_data(credential_id)["token"] == "old"

    # 再登录一次（token 轮换）→ 仍是同一条凭证，内容更新
    current["token"] = "new"
    second = client.post("/api/auth/upstream/poll",
                         json={"provider": "codearts", "state": "s2"}).json()
    assert second["credential_id"] == credential_id and second["merged"] is True
    assert len([row for row in app.state.credentials.list_all()
                if row["provider"] == "codearts"]) == 1
    assert app.state.credentials.credential_data(credential_id)["token"] == "new"