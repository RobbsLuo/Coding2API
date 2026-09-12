"""CodeBuddy 凭证刷新与多账号切换（M1.5）。

AGENTS.md 约束：
- 刷新端点一旦返回轮换后的 refresh token，必须先按凭证代次原子持久化，再同步账号列表
- 账号同步失败或服务关闭时保留可恢复的 pending 状态
- 切换账号仅在规范 account_id 确实变化时才推进额度代次
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .credential import CodeBuddyCredential
from .events import UpstreamProtocolViolation
from .headers import EP_ACCOUNTS, EP_SWITCH_ENTERPRISE, EP_TOKEN_REFRESH, host_of
from .oauth import _expires_at


@dataclass(slots=True)
class Account:
    account_id: str
    nickname: str = ""
    account_type: str = ""
    enterprise_id: str = ""
    enabled: bool = False


@dataclass(slots=True)
class RefreshOutcome:
    """刷新结果：新凭证 + 是否需要固化（调用方负责持久化后再同步账号）。"""

    credential: CodeBuddyCredential
    refresh_token_rotated: bool = False
    accounts_pending: bool = False
    accounts: list[Account] | None = None


class CodeBuddyRefresh:
    def __init__(self, endpoint: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.endpoint = endpoint
        self._client = client

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _headers(self, credential: CodeBuddyCredential) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credential.bearer_token}",
            "Accept": "application/json, text/plain, */*",
            "X-Domain": credential.domain or host_of(self.endpoint),
        }

    async def refresh(self, credential: CodeBuddyCredential) -> RefreshOutcome:
        """刷新 access token。失败路径不改写原凭证字段（可重试）。"""
        if not credential.refresh_token:
            raise UpstreamProtocolViolation("no refresh token")
        headers = self._headers(credential) | {
            "X-Refresh-Token": credential.refresh_token,
            "X-Auth-Refresh-Source": "plugin",
        }
        try:
            response = await self._http.post(
                f"{self.endpoint}{EP_TOKEN_REFRESH}", json={}, headers=headers)
        except httpx.HTTPError as error:
            raise UpstreamProtocolViolation(
                f"refresh transport error: {type(error).__name__}") from error
        if response.status_code in (401, 403):
            raise UpstreamProtocolViolation("refresh unauthorized")
        if response.status_code >= 400:
            raise UpstreamProtocolViolation(f"refresh rejected with {response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON refresh response") from error
        if not isinstance(body, dict) or body.get("code") != 0:
            raise UpstreamProtocolViolation("refresh rejected")

        data = body.get("data")
        data = data if isinstance(data, dict) else {}
        token = data.get("accessToken")
        if not isinstance(token, str) or not token:
            raise UpstreamProtocolViolation("refresh_failed: no token in response")

        refreshed = CodeBuddyCredential(
            bearer_token=token,
            user_id=credential.user_id,
            account_uid=credential.account_uid,
            domain=credential.domain,
            enterprise_id=credential.enterprise_id,
            department_full_name=credential.department_full_name,
            refresh_token=str(data.get("refreshToken") or credential.refresh_token),
            expires_at=_expires_at(data) or credential.expires_at,
            auth_source=credential.auth_source,      # 刷新必须原样继承来源
            quota_probe_mode=credential.quota_probe_mode,
            nickname=credential.nickname,
        )
        # 上游未轮换 refresh token 时必须沿用旧值
        rotated = refreshed.refresh_token != credential.refresh_token
        accounts = await self._try_fetch_accounts(refreshed)
        return RefreshOutcome(credential=refreshed, refresh_token_rotated=rotated,
                              accounts_pending=accounts is None, accounts=accounts)

    async def list_accounts(self, credential: CodeBuddyCredential) -> list[Account]:
        accounts = await self._try_fetch_accounts(credential)
        if accounts is None:
            raise UpstreamProtocolViolation("accounts unavailable")
        return accounts

    async def _try_fetch_accounts(self, credential: CodeBuddyCredential) -> list[Account] | None:
        """账号列表是可选阶段：任何失败都记为 pending（可恢复），不阻断 token 刷新。"""
        try:
            response = await self._http.get(f"{self.endpoint}{EP_ACCOUNTS}",
                                            headers=self._headers(credential))
        except httpx.HTTPError:
            return None
        except Exception:  # noqa: BLE001 - 传输层实现差异不应导致刷新失败
            return None
        if response.status_code >= 400:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict) or body.get("code") != 0:
            return None
        data = body.get("data")
        raw_accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(raw_accounts, list):
            return None
        # 上游语义：只有 pluginEnabled=true 的账号可用（切换后复查也依赖这一点）
        return [account for account in (_to_account(item) for item in raw_accounts)
                if account is not None and account.enabled]

    async def switch_account(self, credential: CodeBuddyCredential,
                             account_id: str) -> CodeBuddyCredential:
        """切换当前账号（真实端点：POST /v2/plugin/login/enterprise[/{ent}]）。

        上游语义（来自 codebuddy2api 的 credential_refresh._perform_switch）：
        - 个人账号 → 无 enterprise_id 后缀的路径
        - 企业账号 → 路径带 enterprise_id，并额外带 X-Enterprise-Id / X-Tenant-Id
        - 响应体里带新的 access token，必须一并更新
        - 切换后重新拉账号列表，并确认目标账号 pluginEnabled=true
        """
        if not account_id:
            raise UpstreamProtocolViolation("account_id is required")
        accounts = await self.list_accounts(credential)
        target = next((a for a in accounts if a.account_id == account_id), None)
        if target is None:
            raise UpstreamProtocolViolation("account_not_found")

        is_personal = target.account_type == "personal"
        if not is_personal and not target.enterprise_id:
            raise UpstreamProtocolViolation("account_invalid")

        path = EP_SWITCH_ENTERPRISE
        headers = self._headers(credential) | {"X-Refresh-Token": credential.refresh_token}
        if not is_personal:
            path = f"{path}/{target.enterprise_id}"
            headers["X-Enterprise-Id"] = target.enterprise_id
            headers["X-Tenant-Id"] = target.enterprise_id

        try:
            response = await self._http.post(f"{self.endpoint}{path}", json={},
                                             headers=headers)
        except httpx.HTTPError as error:
            raise UpstreamProtocolViolation(
                f"switch transport error: {type(error).__name__}") from error
        try:
            body = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON switch response") from error
        if isinstance(body, dict) and body.get("code") == 10081:
            raise UpstreamProtocolViolation("ip_restricted")
        if response.status_code != 200 or not isinstance(body, dict) or body.get("code") != 0:
            raise UpstreamProtocolViolation("switch rejected")

        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        new_token = data.get("accessToken")
        new_refresh = data.get("refreshToken")

        # 切换后重新拉账号列表，确认目标账号仍然启用
        switched_credential = CodeBuddyCredential(
            bearer_token=new_token if isinstance(new_token, str) and new_token
            else credential.bearer_token,
            user_id=credential.user_id,
            account_uid=target.account_id,
            domain=str(data.get("domain") or credential.domain),
            enterprise_id="" if is_personal else target.enterprise_id,
            # 切到个人账号时必须清空企业上下文，禁止回退旧 enterprise_id
            department_full_name="" if is_personal else credential.department_full_name,
            refresh_token=(new_refresh if isinstance(new_refresh, str) and new_refresh
                           else credential.refresh_token),
            expires_at=_expires_at(data) or credential.expires_at,
            auth_source=credential.auth_source,
            quota_probe_mode=credential.quota_probe_mode,
            nickname=target.nickname,
        )
        enabled = await self.list_accounts(switched_credential)
        if not any(a.account_id == account_id for a in enabled):
            raise UpstreamProtocolViolation("account_missing")
        return switched_credential


def _to_account(raw: Any) -> Account | None:
    if not isinstance(raw, dict):
        return None
    account_id = raw.get("accountId") or raw.get("uid") or raw.get("id")
    if not isinstance(account_id, str) or not account_id:
        return None
    return Account(
        account_id=account_id,
        nickname=str(raw.get("nickname") or raw.get("name") or ""),
        account_type=str(raw.get("type") or ""),
        enterprise_id=str(raw.get("enterpriseId") or ""),
        enabled=raw.get("pluginEnabled") is True,
    )
