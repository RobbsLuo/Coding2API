"""CodeBuddy 每日签到（M1.5）。

AGENTS.md 约束：
- 只有上游响应 code=0 且 data.credit 是非布尔有限数值才算成功
- code=null 的未成功异常不阻止当天后续启动补偿
- 按「系统用户 + API endpoint + X-User-Id」隔离，同上游账号的凭证共享记录与并发锁
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx

from .client import build_headers
from .credential import CodeBuddyCredential
from .events import UpstreamProtocolViolation
from .headers import EP_DAILY_CHECKIN


@dataclass(slots=True)
class CheckinResult:
    ok: bool
    credit: float | None = None
    code: int | None = None
    message: str = ""
    already_checked_in: bool = False


def parse_checkin_response(body: Any) -> CheckinResult:
    """严格按上游语义解析：code=0 且 credit 为有限数值才算成功。"""
    if not isinstance(body, dict):
        raise UpstreamProtocolViolation("checkin response is not an object")
    raw_code = body.get("code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    data = body.get("data")
    data = data if isinstance(data, dict) else {}
    credit = data.get("credit")
    valid_credit = (isinstance(credit, (int, float)) and not isinstance(credit, bool)
                    and math.isfinite(float(credit)))
    message = body.get("msg")
    message = message if isinstance(message, str) else ""

    if code == 0 and valid_credit:
        return CheckinResult(ok=True, credit=float(credit), code=0, message=message)
    already = code == 0 and not valid_credit
    return CheckinResult(ok=False, credit=None, code=code, message=message,
                         already_checked_in=already)


def checkin_scope_key(endpoint: str, user_id: str) -> str:
    """签到隔离键：endpoint 与 X-User-Id 都参与（同账号多凭证共享）。"""
    return f"{endpoint.strip().rstrip('/')}|{user_id.strip()}"


class CodeBuddyCheckin:
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

    async def claim(self, credential: CodeBuddyCredential) -> CheckinResult:
        response = await self._http.post(
            f"{self.endpoint}{EP_DAILY_CHECKIN}", json={},
            headers=build_headers(credential, self.endpoint))
        if response.status_code >= 400:
            raise UpstreamProtocolViolation(f"checkin rejected with {response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON checkin response") from error
        return parse_checkin_response(body)
