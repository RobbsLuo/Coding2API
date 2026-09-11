"""CodeBuddy 上游客户端：聊天流、额度探测、凭证规范化。

M1b 范围（PROPOSAL §9）：bearer-only 手动凭证 + 聊天 + 个人版额度。
OAuth 轮询、企业额度、多账号切换在 M1.5。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...engine.sse import iter_frames
from ...provider.base import ErrKind, Event, Model, Quota
from . import events as cb_events
from .events import UpstreamProtocolViolation
from .headers import (
    CN_ENDPOINT,
    EP_CHAT,
    EP_ENTERPRISE_USAGE,
    EP_USER_RESOURCE,
    QUOTA_PRODUCT_CODE,
    QUOTA_RANGE_END,
    generate_headers,
    host_of,
)

STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
SHORT_TIMEOUT = httpx.Timeout(30.0)

DEFAULT_MODELS: tuple[str, ...] = ("glm-5.2", "deepseek-v4-pro")


@dataclass(slots=True)
class CodeBuddyCredential:
    """归一化凭证。bearer-only 手动凭证只有 bearer_token 也是合法的。"""

    bearer_token: str = ""
    user_id: str = ""
    account_uid: str = ""
    domain: str = ""
    enterprise_id: str = ""
    department_full_name: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    auth_source: str = "unknown"          # manual | oauth | unknown
    quota_probe_mode: str = "personal"    # personal | enterprise
    nickname: str = ""

    @property
    def is_oauth(self) -> bool:
        return self.auth_source == "oauth"

    def needs_refresh(self, skew_seconds: int, now: int | None = None) -> bool:
        """只有 OAuth 凭证参与刷新；bearer-only 手动凭证永不刷新（AGENTS.md）。"""
        if not self.is_oauth or not self.refresh_token or self.expires_at <= 0:
            return False
        current = int(now if now is not None else time.time())
        return current + skew_seconds >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "bearer_token": self.bearer_token, "user_id": self.user_id,
            "account_uid": self.account_uid, "domain": self.domain,
            "enterprise_id": self.enterprise_id,
            "department_full_name": self.department_full_name,
            "refresh_token": self.refresh_token, "expires_at": self.expires_at,
            "auth_source": self.auth_source, "quota_probe_mode": self.quota_probe_mode,
            "nickname": self.nickname,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CodeBuddyCredential:
        source = raw.get("auth_source")
        return cls(
            bearer_token=str(raw.get("bearer_token") or raw.get("bearerToken") or ""),
            user_id=str(raw.get("user_id") or ""),
            account_uid=str(raw.get("account_uid") or ""),
            domain=str(raw.get("domain") or ""),
            enterprise_id=str(raw.get("enterprise_id") or ""),
            department_full_name=str(raw.get("department_full_name") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            expires_at=int(raw.get("expires_at") or 0),
            auth_source=source if source in ("manual", "oauth") else "unknown",
            quota_probe_mode="enterprise"
            if raw.get("quota_probe_mode") == "enterprise" else "personal",
            nickname=str(raw.get("nickname") or ""),
        )


def parse_credential(raw: bytes | dict[str, Any], *,
                     auth_source: str = "manual") -> CodeBuddyCredential:
    """手动导入路径：接受 token / access_token / bearer_token 任一键名。

    auth_source 只能由可信创建入口显式写入：手动添加为 manual，OAuth 为 oauth。
    """
    if isinstance(raw, bytes):
        try:
            raw = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise UpstreamProtocolViolation("credential is not valid JSON") from error
    if not isinstance(raw, dict):
        raise UpstreamProtocolViolation("credential is not an object")

    token = raw.get("bearer_token") or raw.get("access_token") or raw.get("token")
    if not isinstance(token, str) or not token.strip():
        raise UpstreamProtocolViolation("credential missing bearer token")
    credential = CodeBuddyCredential.from_dict(raw)
    credential.bearer_token = token.strip()
    # AGENTS.md 约束：来源只能由可信创建入口显式写入。
    # - 字段缺失 → 由入口决定（手动导入 = manual）
    # - 字段存在但非法 → 保持 unknown，绝不能默认成 manual
    if "auth_source" not in raw:
        credential.auth_source = auth_source
    return credential


def build_headers(credential: CodeBuddyCredential, endpoint: str, *,
                  quota_only: bool = False) -> dict[str, str]:
    """头构造统一入口：X-Domain 与 Host 由同一 endpoint 派生。"""
    return generate_headers(
        endpoint=endpoint, bearer_token=credential.bearer_token,
        user_id=credential.user_id or None, account_uid=credential.account_uid or None,
        domain=credential.domain or None, enterprise_id=credential.enterprise_id or None,
        department_full_name=credential.department_full_name or None, quota_only=quota_only,
    )


class CodeBuddyClient:
    def __init__(
        self,
        *,
        endpoint: str = CN_ENDPOINT,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._stream_client = stream_client
        self._short_client = short_client

    @property
    def _stream(self) -> httpx.AsyncClient:
        if self._stream_client is None:
            self._stream_client = httpx.AsyncClient(timeout=STREAM_TIMEOUT, trust_env=False)
        return self._stream_client

    @property
    def _short(self) -> httpx.AsyncClient:
        if self._short_client is None:
            self._short_client = httpx.AsyncClient(timeout=SHORT_TIMEOUT, trust_env=False)
        return self._short_client

    async def aclose(self) -> None:
        for client in (self._stream_client, self._short_client):
            if client is not None:
                await client.aclose()

    # ------------------------------------------------------------- 聊天流

    async def stream_chat(self, credential: CodeBuddyCredential, payload: dict[str, Any],
                          model: str) -> AsyncIterator[Event]:
        """上游只支持流式；非流式由调用方聚合。"""
        body = dict(payload)
        body["model"] = model
        body["stream"] = True
        url = f"{self.endpoint}{EP_CHAT}"
        async with self._stream.stream(
            "POST", url, json=body, headers=build_headers(credential, self.endpoint),
        ) as response:
            if response.status_code >= 400:
                raw = await response.aread()
                raise UpstreamHTTPError(response.status_code, raw)
            async for frame in iter_frames(response.aiter_bytes()):
                for event in cb_events.parse_all_events(frame):
                    yield event

    # ------------------------------------------------------------- 额度

    async def fetch_quota(self, credential: CodeBuddyCredential) -> Quota:
        """个人版 /v2/billing/meter/get-user-resource；企业版另走一个接口。"""
        if credential.quota_probe_mode == "enterprise" and credential.is_oauth:
            return await self._fetch_enterprise_quota(credential)
        return await self._fetch_personal_quota(credential)

    async def _fetch_personal_quota(self, credential: CodeBuddyCredential) -> Quota:
        now = time.localtime()
        payload = {
            "PageNumber": 1,
            "PageSize": 200,
            "ProductCode": QUOTA_PRODUCT_CODE,
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": time.strftime("%Y-%m-%d %H:%M:%S", now),
            "PackageEndTimeRangeEnd": QUOTA_RANGE_END,
        }
        data = await self._post_json(f"{self.endpoint}{EP_USER_RESOURCE}", payload,
                                     credential, quota_only=True)
        if "Accounts" not in data:
            raise UpstreamProtocolViolation("quota response missing Accounts")
        accounts = data.get("Accounts")
        if accounts is None:                    # null = 探测成功但无个人版额度
            accounts = []
        if not isinstance(accounts, list):
            raise UpstreamProtocolViolation("quota Accounts is not an array")
        total = 0.0
        remaining = 0.0
        cycle_end: int | None = None
        for account in accounts:
            if not isinstance(account, dict) or account.get("Status") != 0:
                continue
            package_total = _cycle_capacity(account, "CycleCapacitySize")
            package_remaining = _cycle_capacity(account, "CycleCapacityRemain")
            if package_total <= 0:
                continue
            total += package_total
            remaining += package_remaining
            cycle_end = _cycle_end_epoch(account.get("CycleEndTime")) or cycle_end
        return Quota(remaining=remaining, total=total, cycle_end=cycle_end,
                     probed_at=int(time.time()))

    async def _fetch_enterprise_quota(self, credential: CodeBuddyCredential) -> Quota:
        data = await self._post_json(f"{self.endpoint}{EP_ENTERPRISE_USAGE}", {}, credential)
        used = _number(data.get("credit"))
        total = _number(data.get("limitNum"))
        if total is None:
            raise UpstreamProtocolViolation("enterprise quota missing limitNum")
        return Quota(remaining=max(0.0, total - (used or 0.0)), total=total,
                     probed_at=int(time.time()))

    async def _post_json(self, url: str, payload: dict[str, Any],
                         credential: CodeBuddyCredential, *,
                         quota_only: bool = False) -> dict[str, Any]:
        headers = build_headers(credential, self.endpoint, quota_only=quota_only)
        response = await self._short.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code, response.content)
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation(f"non-JSON response from {url}") from error
        if not isinstance(data, dict):
            raise UpstreamProtocolViolation(f"unexpected response shape from {url}")
        return data


def _cycle_capacity(account: dict[str, Any], field: str) -> float:
    """优先 *Precise 字段（AGENTS.md：额度以 Precise 为准）。"""
    precise = account.get(f"{field}Precise")
    value = precise if precise is not None else account.get(field)
    return _number(value) or 0.0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _cycle_end_epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(value, fmt)))
        except ValueError:
            continue
    return None


class UpstreamHTTPError(Exception):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"upstream http {status}")
        self.status = status
        self.body = body

    def kind(self) -> ErrKind:
        return cb_events.classify_status(self.status, self.body)


@dataclass(slots=True)
class CodeBuddyProvider:
    """Provider 协议实现（M1b：bearer-only + 聊天 + 个人额度）。"""

    client: CodeBuddyClient = field(default_factory=CodeBuddyClient)

    id: str = "codebuddy"

    def import_credential(self, raw: dict) -> dict:
        return parse_credential(raw, auth_source="manual").to_dict()

    def classify(self, status: int, body: bytes) -> ErrKind:
        return cb_events.classify_status(status, body)

    async def probe_quota(self, credential_data: dict) -> Quota:
        return await self.client.fetch_quota(CodeBuddyCredential.from_dict(credential_data))

    def list_models(self, _credential_data: dict) -> list[Model]:
        return [Model(id=mid) for mid in DEFAULT_MODELS]

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        credential = CodeBuddyCredential.from_dict(credential_data)
        async for event in self.client.stream_chat(credential, payload, model):
            yield event

    def host(self) -> str:
        return host_of(self.client.endpoint)
