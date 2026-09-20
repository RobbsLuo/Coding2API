"""CodeBuddy 成长中心（逆向自 WorkBuddy 桌面端，2026-09 活动版本）。

端点全在 `/v2/activity/growth` 前缀下，鉴权头与聊天一致（Authorization +
X-User-Id + X-Domain）。协议要点（均为实测，勿凭直觉改）：

- 只读 5 个端点（travel/status、travel/config、tasks、streak、redeem/summary、
  lottery/chances、buddy/quota）；写 6 个（travel/claim、travel/depart、
  tasks/accept、makeup-cards/use、redeem、lottery/draw、buddy/open）
- 写操作**绝不重试**：超时可能发生在服务端已处理之后，重试等于重复提交
- 抽奖/兑换必须带 `client_token`（`u-<uuid>`），缺失直接 400；服务端只做幂等去重
- `/redeem` 的 tier 收的是**天数**（7/14/28），传档位名得到 400 unknown tier
- 4xx 绝大多数是业务规则（名额用完、未解锁、活动下线），属日常状态而非故障；
  5xx / 网络失败才是"需要人关注"的失败

本模块只负责请求与解析；重试/节奏/落库/开关在 tasks 层。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .client import build_headers
from .credential import CodeBuddyCredential
from .events import UpstreamProtocolViolation

GROWTH_PREFIX = "/v2/activity/growth"

EP_TRAVEL_STATUS = f"{GROWTH_PREFIX}/buddy/travel/status"
EP_TRAVEL_CONFIG = f"{GROWTH_PREFIX}/buddy/travel/config"
EP_TRAVEL_CLAIM = f"{GROWTH_PREFIX}/buddy/travel/claim"
EP_TRAVEL_DEPART = f"{GROWTH_PREFIX}/buddy/travel/depart"
EP_TASKS = f"{GROWTH_PREFIX}/tasks"
EP_TASK_ACCEPT = f"{GROWTH_PREFIX}/tasks/accept"
TASK_CLAIM_SUFFIX = "/claim"
EP_STREAK = f"{GROWTH_PREFIX}/streak"
EP_MAKEUP_USE = f"{GROWTH_PREFIX}/makeup-cards/use"
EP_REDEEM_SUMMARY = f"{GROWTH_PREFIX}/redeem/summary"
EP_REDEEM = f"{GROWTH_PREFIX}/redeem"
EP_LOTTERY_CHANCES = f"{GROWTH_PREFIX}/lottery/chances"
EP_LOTTERY_DRAW = f"{GROWTH_PREFIX}/lottery/draw"
EP_BUDDY_QUOTA = f"{GROWTH_PREFIX}/buddy/quota"
EP_BUDDY_OPEN = f"{GROWTH_PREFIX}/buddy/open"
EP_ENERGY = f"{GROWTH_PREFIX}/energy"


class GrowthRejected(Exception):
    """上游以非 2xx 拒绝了本次请求。

    4xx（业务规则）与 5xx（上游故障）在此合并携带状态码，由调用方决定是否算失败
    ——分层原因：只有 tasks 层才知道"名额用完"该不该计入失败。
    """

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        self.message = message
        super().__init__(f"growth http {status}: {message}" if message else f"growth http {status}")

    @property
    def needs_attention(self) -> bool:
        """是否属于"接口坏了/网络坏了"这类需要人处理的失败。

        4xx 是业务规则（今天名额用完了、活动没开始、任务没做完），是日常状态；
        把它算成失败会让定时任务天天报红，真正的故障反而被淹没。
        """
        return self.status >= 500 or self.status < 0


def client_token(prefix: str = "u") -> str:
    """活动接口要求的防重放 token；服务端只做幂等去重、不校验格式。"""
    return f"{prefix}-{uuid.uuid4()}"


def dig(obj: Any, key: str) -> Any:
    """在可能被 data/result/resp/response 包裹的响应里找字段（兼容信封结构）。"""
    if isinstance(obj, dict):
        if obj.get(key) is not None:
            return obj[key]
        for wrapper in ("data", "result", "resp", "response"):
            inner = obj.get(wrapper)
            if isinstance(inner, dict):
                found = dig(inner, key)
                if found is not None:
                    return found
    return None


def as_int(value: Any, default: int = 0) -> int:
    """宽松整数转换。OverflowError 必须捕获：json 的 Infinity 会走到这里。"""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _envelope(body: Any) -> dict[str, Any]:
    """校验 code=0 的信封并返回 data 对象。

    上游业务失败也是 HTTP 200 + code≠0，只看状态码会把失败当成功。
    """
    if not isinstance(body, dict):
        raise UpstreamProtocolViolation("growth response is not an object")
    code = body.get("code")
    if code != 0:
        message = body.get("msg")
        raise UpstreamProtocolViolation(
            f"growth rejected with code {code!r}: {message if isinstance(message, str) else ''}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise UpstreamProtocolViolation("growth response missing data object")
    return data


# ------------------------------------------------------------------ 解析


@dataclass(slots=True)
class TravelStatus:
    """Buddy 旅行状态。state: idle | traveling | arrived。"""

    state: str = "idle"
    record_id: Any = None
    reward_credit: float | None = None
    location_name: str = ""
    duration_hours: Any = None
    arrive_at: Any = None
    server_now: Any = None
    daily_limit_reached: bool = False


@dataclass(slots=True)
class TravelLocation:
    id: Any = None
    name: str = ""
    duration_hours_min: Any = None
    duration_hours_max: Any = None


@dataclass(slots=True)
class GrowthTaskItem:
    """活动任务。

    accept_status 是**五态**（2026-09 桌面端 H5 契约，勿按三态猜）：
    not_accepted | accepted | in_progress | completed | claimed

    `not_accepted` 是「未接单」而不是「已接单」——按三态直觉把它当非空字符串
    跳过，会让所有新任务永远既不接单也不领奖（实测有账号积压 650 积分未领）。
    进度只在接单之后才计分，所以接单是必做的一步。
    """

    task_code: Any = None
    title: str = ""
    accept_status: str = ""
    locked: bool = False
    progress_current: int = 0
    progress_target: int = 1
    reward_credit: float | None = None
    reward_energy: float | None = None

    @property
    def completed(self) -> bool:
        return self.progress_current >= (self.progress_target or 1)

    @property
    def needs_accept(self) -> bool:
        """是否需要接单：只有 not_accepted（空字符串是字段缺失，同样当未接单）。"""
        return not self.locked and self.accept_status in ("", "not_accepted")

    @property
    def needs_claim(self) -> bool:
        """是否需要领奖：只有 completed 才发奖，走独立端点。"""
        return not self.locked and self.accept_status == "completed"


def parse_travel_status(data: dict[str, Any]) -> TravelStatus:
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    state = data.get("state")
    return TravelStatus(
        state=state if isinstance(state, str) and state else "idle",
        record_id=data.get("record_id"),
        reward_credit=_finite_float(data.get("reward_credit")),
        location_name=str(location.get("name") or ""),
        duration_hours=data.get("duration_hours") or location.get("duration_hours"),
        arrive_at=data.get("arrive_at"),
        server_now=data.get("server_now"),
        daily_limit_reached=bool(data.get("daily_limit_reached")),
    )


def parse_travel_locations(data: dict[str, Any]) -> list[TravelLocation]:
    raw = data.get("locations")
    if not isinstance(raw, list):
        return []
    out: list[TravelLocation] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(TravelLocation(
                id=item.get("id"), name=str(item.get("name") or ""),
                duration_hours_min=item.get("duration_hours_min"),
                duration_hours_max=item.get("duration_hours_max")))
    return out


def parse_tasks(data: dict[str, Any]) -> list[GrowthTaskItem]:
    raw = data.get("tasks")
    if not isinstance(raw, list):
        return []
    out: list[GrowthTaskItem] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
        status = item.get("accept_status")
        out.append(GrowthTaskItem(
            task_code=item.get("task_code"),
            title=str(item.get("title") or item.get("task_code") or ""),
            accept_status=status if isinstance(status, str) else "",
            locked=bool(item.get("locked")),
            progress_current=as_int(progress.get("current")),
            # target 为 0/缺失时按 1：否则 0 >= 0 会被当成"已完成"而误领奖
            progress_target=as_int(progress.get("target"), 1) or 1,
            reward_credit=_finite_float(item.get("reward_credit")),
            reward_energy=_finite_float(item.get("reward_energy"))))
    return out


@dataclass(slots=True)
class StreakStatus:
    """连登状态。makeup_cards 兼容 {"balance": n} 与裸数字两种形状。"""

    days: int | None = None
    makeup_cards: int = 0
    makeup_dates: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.makeup_dates is None:
            self.makeup_dates = []


def parse_streak(data: dict[str, Any]) -> StreakStatus:
    streak = data.get("streak") if isinstance(data.get("streak"), dict) else {}
    cards = data.get("makeup_cards")
    balance = as_int(cards.get("balance")) if isinstance(cards, dict) else as_int(cards)
    # makeup_dates 实测在 streak 对象内部；顶层也兜一下（接口改版方向未知）
    dates = streak.get("makeup_dates")
    if not isinstance(dates, list):
        dates = data.get("makeup_dates")
    return StreakStatus(
        days=streak.get("days") if isinstance(streak.get("days"), int) else None,
        makeup_cards=balance,
        makeup_dates=[d for d in dates if isinstance(d, str)] if isinstance(dates, list) else [])


def parse_redeem_summary(data: dict[str, Any]) -> dict[str, str]:
    """三档连登兑换状态：starter/advanced/legendary → 状态字符串。

    字段缺失时**不返回**该档（而不是猜一个可兑换）：接口改版时不该对三档无脑 POST。
    """
    out: dict[str, str] = {}
    for tier in ("starter", "advanced", "legendary"):
        status = data.get(f"{tier}_status")
        if isinstance(status, str) and status:
            out[tier] = status
    return out


def parse_reward(body: Any) -> tuple[float | None, float | None]:
    """从写操作响应里挖实际发放的 credit / energy。

    列表里的 reward_credit 只是活动配置，与服务端这次实际发的可能不同；
    按响应值上报才不会虚报，挖不到时返回 None 由调用方决定回落。
    """
    credit = _finite_float(dig(body, "credit"))
    energy = _finite_float(dig(body, "energy"))
    return credit, energy


def parse_granted(body: Any) -> tuple[float | None, float | None]:
    """兑换响应专用：实发字段是 *_granted。

    先读 *_granted，读不到才回落到裸字段（接口改版方向未知，两边都兜）。
    """
    credit = _finite_float(dig(body, "credit_granted"))
    energy = _finite_float(dig(body, "energy_granted"))
    if credit is None and energy is None:
        return parse_reward(body)
    return credit, energy


# ------------------------------------------------------------------ 客户端


class CodeBuddyGrowth:
    """成长中心 HTTP 客户端。只做请求 + 解析，不做重试（写操作不可重放）。"""

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
        return build_headers(credential, self.endpoint)

    async def _request(self, method: str, path: str, credential: CodeBuddyCredential,
                       payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._http.request(
            method, f"{self.endpoint}{path}", json=payload,
            headers=self._headers(credential))
        if not 200 <= response.status_code < 300:
            # 401/403 由调用方据此判为 session 失效；诊断消息一律优先用上游原文，
            # 上游没给才回落一句通用说明（吞掉原文会让排障无从下手）
            message = _message_of(response)
            if not message and response.status_code in (401, 403):
                message = "credential rejected"
            raise GrowthRejected(response.status_code, message)
        try:
            body = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON growth response") from error
        return _envelope(body)

    # ---------------------------------------------------------- 只读

    async def travel_status(self, credential: CodeBuddyCredential) -> TravelStatus:
        return parse_travel_status(await self._request("GET", EP_TRAVEL_STATUS, credential))

    async def travel_locations(self, credential: CodeBuddyCredential) -> list[TravelLocation]:
        return parse_travel_locations(await self._request("GET", EP_TRAVEL_CONFIG, credential))

    async def tasks(self, credential: CodeBuddyCredential) -> list[GrowthTaskItem]:
        return parse_tasks(await self._request("GET", EP_TASKS, credential))

    async def streak(self, credential: CodeBuddyCredential) -> StreakStatus:
        return parse_streak(await self._request("GET", EP_STREAK, credential))

    async def redeem_summary(self, credential: CodeBuddyCredential) -> dict[str, str]:
        return parse_redeem_summary(await self._request("GET", EP_REDEEM_SUMMARY, credential))

    async def lottery_chances(self, credential: CodeBuddyCredential) -> int:
        return as_int(dig(await self._request("GET", EP_LOTTERY_CHANCES, credential), "balance"))

    async def buddy_quota(self, credential: CodeBuddyCredential) -> tuple[int, int, int]:
        """返回 (可用次数, 单次成本, 单次上限)。"""
        data = await self._request("GET", EP_BUDDY_QUOTA, credential)
        return (as_int(data.get("affordable")),
                as_int(data.get("cost_per_open")),
                as_int(data.get("max_open_count"), 1) or 1)

    async def energy(self, credential: CodeBuddyCredential) -> int | None:
        data = await self._request("GET", EP_ENERGY, credential)
        balance = data.get("balance")
        return as_int(balance) if balance is not None else None

    # ---------------------------------------------------------- 写入（绝不重试）

    async def claim_travel(self, credential: CodeBuddyCredential,
                           record_id: Any) -> tuple[float | None, float | None]:
        data = await self._request("POST", EP_TRAVEL_CLAIM, credential,
                                   {"record_id": record_id})
        return parse_reward(data)

    async def depart(self, credential: CodeBuddyCredential,
                     location_id: Any) -> dict[str, Any]:
        return await self._request("POST", EP_TRAVEL_DEPART, credential,
                                   {"location_id": location_id})

    async def accept_tasks(self, credential: CodeBuddyCredential,
                           task_codes: list[Any]) -> list[dict[str, Any]]:
        """批量接单。上游只收复数数组 {\"task_codes\": [...]}。

        旧的单数 {\"task_code\": x} 在新服务端一律 400 invalid request（2026-09 契约）；
        逐条结果在 data.results 里，接单失败（如 prerequisite not met）必须回原文，
        静默吞掉的话接口坏了也没人知道。
        """
        data = await self._request("POST", EP_TASK_ACCEPT, credential,
                                   {"task_codes": list(task_codes)})
        results = data.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        # 上游没给逐条结果时，整体成功即视为全部成功（不编造具体错误）
        return [{"task_code": code, "status": "ok"} for code in task_codes]

    async def claim_task(self, credential: CodeBuddyCredential,
                         task_code: Any) -> dict[str, Any]:
        """领奖：独立端点 POST /tasks/{task_code}/claim，body 空。

        旧版把 /tasks/accept 当领奖用（同样 400）。回包的 already_claimed 为真
        时不能重复计分。
        """
        return await self._request("POST", f"{EP_TASKS}/{task_code}{TASK_CLAIM_SUFFIX}",
                                   credential, {})

    async def use_makeup_card(self, credential: CodeBuddyCredential,
                              target_date: str) -> int | None:
        """补登一天。返回服务端给出剩余卡数（没给则 None）。"""
        data = await self._request("POST", EP_MAKEUP_USE, credential,
                                   {"target_date": target_date, "client_token": client_token()})
        cards = data.get("makeup_cards")
        if isinstance(cards, dict):
            return as_int(cards.get("balance"))
        # 字段缺失时返回 None 而不是 0：0 意味着「卡用完了」，与「上游没说」是两回事
        return as_int(cards) if cards is not None else None

    async def redeem(self, credential: CodeBuddyCredential,
                     tier: Any) -> tuple[float | None, float | None]:
        """兑换：tier 是**档位标识字符串**（"7d"/"14d"/"28d"）。

        实测传 "starter" / 7 / "7" 分别得到 unknown tier / invalid request，
        只有 "7d" 会 200（2026-09 issue #6）。权威来源是 GET /streak 的
        redemption_status.tiers[].tier。

        实发字段是 *_granted（credit_granted / energy_granted / cards_granted /
        chances_granted）；读 credit 恒为空，会把兑换所得全部漏计。
        """
        data = await self._request("POST", EP_REDEEM, credential,
                                   {"tier": tier, "client_token": client_token()})
        return parse_granted(data)

    async def draw_lottery(self, credential: CodeBuddyCredential) -> dict[str, Any]:
        return await self._request("POST", EP_LOTTERY_DRAW, credential,
                                   {"client_token": client_token()})

    async def open_buddy(self, credential: CodeBuddyCredential,
                         count: int) -> dict[str, Any]:
        return await self._request("POST", EP_BUDDY_OPEN, credential,
                                   {"count": count, "client_token": client_token()})


def _message_of(response: httpx.Response) -> str:
    """从失败响应里取一句人话；取不到就用状态码，绝不吞掉失败。"""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        for key in ("msg", "message", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def is_unknown_tier(error: GrowthRejected) -> bool:
    """连登兑换是否因「tier 这个值本身不认识」被拒。

    实测 /redeem 收的是天数（7/14/28），传档位名得到 400 unknown tier；这一点只在
    有限样本上验过，接口若改成只认档位名就会三档全废。这类 400 是参数校验阶段的
    拒绝、服务端没兑换任何东西，所以换个写法重试是安全的（`invalid request` 那种
    未解锁的业务拒绝不在此列，不能重试）。
    """
    if error.status != 400:
        return False
    message = error.message.lower()
    return "tier" in message and ("unknown" in message or "invalid" in message
                                  or "unsupported" in message)


def is_tier_locked(error: GrowthRejected) -> bool:
    """未解锁档位：403 + 「连续登录天数不足」——这是常态，不是故障。

    必须**先于** session 失效判定处理：401/403 一律当登录失效的话，未解锁档位会
    让整轮成长中心误报「登录态已失效」并直接中止（2026-09 issue #6 的教训）。
    """
    if error.status != 403:
        return False
    return "不足" in error.message
