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

from ...provider.base import CheckinResult
from .client import build_headers
from .credential import CodeBuddyCredential
from .events import UpstreamProtocolViolation
from .headers import EP_CHECKIN_STATUS, EP_DAILY_CHECKIN


@dataclass(slots=True)
class CheckinStatus:
    """签到活动状态（展示用）。全部字段可缺失：上游改版时不该因此报错。"""

    active: bool = False
    today_checked_in: bool = False
    streak_days: int | None = None
    today_credit: float | None = None
    total_credits: float | None = None
    activity_name: str = ""
    is_streak_day: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active, "today_checked_in": self.today_checked_in,
            "streak_days": self.streak_days, "today_credit": self.today_credit,
            "total_credits": self.total_credits, "activity_name": self.activity_name,
            "is_streak_day": self.is_streak_day,
        }


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
    # 上游把「已签到」返回成 HTTP 400 + code=10001；这也算成功
    if code is not None and "已签到" in message:
        return CheckinResult(ok=True, credit=None, code=code, message=message,
                             already_checked_in=True)
    return CheckinResult(ok=False, credit=None, code=code, message=message,
                         already_checked_in=False)


def parse_checkin_status(body: Any) -> CheckinStatus:
    """解析签到活动状态接口；形状不符抛 UpstreamProtocolViolation（不静默当未签到）。

    上游把签到状态包在 data 里（active / today_checked_in / streak_days /
    today_credit / total_credits 等），code=0 才算有效响应。
    """
    if not isinstance(body, dict):
        raise UpstreamProtocolViolation("checkin status response is not an object")
    code = body.get("code")
    if code != 0:
        raise UpstreamProtocolViolation(f"checkin status rejected with code {code!r}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise UpstreamProtocolViolation("checkin status missing data object")
    return CheckinStatus(
        active=bool(data.get("active")),
        today_checked_in=bool(data.get("today_checked_in")),
        streak_days=_opt_int(data.get("streak_days")),
        today_credit=_opt_float(data.get("today_credit")),
        total_credits=_opt_float(data.get("total_credits")),
        activity_name=str(data.get("activity_name") or ""),
        is_streak_day=bool(data.get("is_streak_day")),
    )


def _opt_int(value: Any) -> int | None:
    """可缺失的整数字段：非数值/布尔/非有限值一律按缺失处理，绝不抛异常。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value)


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return float(value)


def checkin_scope_key(endpoint: str, user_id: str) -> str:
    """签到隔离键：endpoint 归一化后与账号身份拼装。

    身份为空时必须返回空串（让调用方回落到 credential_id）：OAuth 路径拿不到
    account_uid / user_id 时（上游账号接口未回填），两个不同账号会算出同一个
    scope，CheckinTask 的 seen 集合会把第二个账号整个跳过——表现为「只有第一个
    凭证被自动签到」，且没有任何报错。
    """
    normalized_endpoint = endpoint.strip().rstrip("/")
    normalized_user = user_id.strip()
    if not normalized_user:
        return ""
    return f"{normalized_endpoint}|{normalized_user}"


class CodeBuddyCheckin:
    def __init__(self, endpoint: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.endpoint = endpoint
        self._client = client

    async def fetch_status(self, credential: CodeBuddyCredential) -> CheckinStatus:
        """查询签到活动状态：连续天数 / 今日是否已签 / 今日与累计积分。

        上游对「已签到」的领取请求返回 code=10001，只看领取结果无法区分「刚签」
        与「今天已签过」，也拿不到连续天数——这些只有本接口给。
        """
        response = await self._http.post(
            f"{self.endpoint}{EP_CHECKIN_STATUS}", json={},
            headers=build_headers(credential, self.endpoint))
        if response.status_code in (401, 403):
            raise UpstreamProtocolViolation("checkin status unauthorized: credential rejected")
        if response.status_code >= 500:
            raise UpstreamProtocolViolation(
                f"checkin status rejected with {response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON checkin status response") from error
        return parse_checkin_status(body)

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def claim(self, credential: CodeBuddyCredential) -> CheckinResult:
        """上游把「已签到」也返回成 HTTP 400 + code=10001。

        这不是错误——重复签到是日常操作，报错会让用户以为签到坏了
        （原项目同样把「已签到」视为成功）。

        领取成功后顺带回查一次状态，把连续天数/今日积分带回来（前端要显示）；
        回查失败不影响「已领取」这个事实，只返回不带状态的成功结果。
        """
        response = await self._http.post(
            f"{self.endpoint}{EP_DAILY_CHECKIN}", json={},
            headers=build_headers(credential, self.endpoint))
        if response.status_code in (401, 403):
            raise UpstreamProtocolViolation("checkin unauthorized: credential rejected")
        if response.status_code >= 500:
            raise UpstreamProtocolViolation(
                f"checkin rejected with {response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON checkin response") from error
        result = parse_checkin_response(body)
        if result.ok:
            result.status = await self._status_or_none(credential)
        return result

    async def _status_or_none(self, credential: CodeBuddyCredential) -> CheckinStatus | None:
        """回查状态；任何失败都返回 None（状态是锦上添花，不能推翻已完成的领取）。"""
        try:
            return await self.fetch_status(credential)
        except Exception:  # noqa: BLE001 - 展示字段绝不影响签到结论
            return None
