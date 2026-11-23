"""小浣熊上游客户端 + Provider 实现。

标准 OpenAI 协议，只有三处渠道私有：Bearer 是网页会话 JWT、免费档看
`billing_effective_multiplier`、未知模型会静默回落（用上游回显的 `model`
兜住）。额度按请求扣积分（每笔 1 分，与 token 量无关）。
"""

from __future__ import annotations

import contextlib
import copy
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...engine.sse import iter_frames
from ...provider import base
from ...provider.base import ActivityResult, CheckinResult, ErrKind, Event, Model, Quota
from ...tasks.pacer import paced
from ..http_json import json_or_raise
from ..lazy_clients import LazyClients
from . import activity as raccoon_activity
from . import credential as raccoon_credential
from . import events as raccoon_events
from . import login as raccoon_login
from .credential import RaccoonCredential

#: 上游主机（网页与 API 同域）
RACCOON_HOST = "https://xiaohuanxiong.com"
EP_CHAT = "/api/web/llm/v2/chat/completions"
EP_MODELS = "/api/web/llm/v2/model_catalog"
EP_BALANCE = "/api/web/points/v1/balance"
EP_BILLS = "/api/web/points/v2/bills"
#: 流水过滤口径：`all` 混消费；`income` 只留发放（签到/到期阶梯用后者）
BILL_TYPE_ALL = "all"
BILL_TYPE_INCOME = "income"
EP_HEARTBEAT = "/api/web/auth/v1/devices_current_heartbeat"
#: 网页会话级「页面访问」：每日积分延期/账单物化的触发点（心跳不算）
EP_USER_INFO = "/api/web/auth/v1/user_info"
#: 密码登录（**现行**）：`{nation_code, phone, password}`，后两者 AES-CFB 加密
#: （见 `login.py`）。`nation_code` 带 `+` 前缀（`"+86"`），与旧接口不同。
#:
#: 2026-11-19 更正：浏览器前端早已改用 IAM 登录链路，旧的
#: `POST /api/web/auth/v1/login_with_password` 虽然仍回 200 并给出可用 token，
#: 但**不再补发当天 `daily_grant`**——这是「按需登录成功却依然没积分」的根因。
EP_LOGIN = "/api/iam/v1/auth/login/password/phone"
#: IAM 登录后的**必需**一步：不确认的 token 打任何 `/api/web/*` 都回
#: `400 200901 iam_confirm_required`（实测 2026-11-19）。浏览器在拿到 token 后
#: 立刻发这一枪，之后才去 `user_info`。
EP_IAM_CONFIRM = "/api/web/auth/v1/iam/confirm"
#: 设备绑定：浏览器登录后立刻调用，把**新会话**绑到当前设备上。不绑的会话是
#: `session_without_bound_device`（`trusted=false`），心跳恒 400，也不能延期。
EP_BIND_DEVICE = "/api/web/auth/v1/devices_current_bind"
#: 网页应用首页（`/work` 产品）。积分**真正入账**发生在这个页面启动时：
#: 2026-11-19 实测 0347e5 用 IAM 登录后 7 分钟仍无 `daily_grant`，一打开
#: `/work/home` 就在 09:48:16 到账；834993 同样在页面加载 6 秒后到账。
#: 只打 `user_info`/`balance` 复现不出来（见 `activity.py` 模块文档）。
EP_PAGE_HOME = "/work/home?lang=zh"
#: 页面启动时前端依次拉的几个接口，按浏览器实测顺序串起来（都幂等只读）。
EP_AGREEMENT_CHECK = "/api/iam/v1/products/work/agreement/check"
EP_OFFICE_SETTING = "/api/web/office/v3/setting_info"
EP_ONLINE_ACTIVITY = "/api/web/office/v3/online_activities/current"
#: 活动上报（`event_name=activity`），前端每次页面加载都打；`machine_id` 是
#: 官方 bundle 里写死的常量，照抄即可（服务端按它识别「真人客户端」）。
EP_METRICS = "/api/web/b/v1/m"
PAGE_MACHINE_ID = ("simulator-f31d2afc795804bbff66711fdc50fd44228798698e70"
                   "438c70d4ba1501b248b8")

#: 官方 LLM 子进程的 User-Agent（openai SDK 形态）；不带也不影响调用，
#: 带上只是为了不把自己标成脚本——本渠道没有其它指纹伪装。
OFFICIAL_UA = "AsyncOpenAI/Python 2.8.0"
#: 登录请求单独用浏览器 UA：这是网页表单的真实来源，用 SDK UA 提交登录态
#: 没有先例，换来的是额外的风控风险，不值得省。
LOGIN_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")


def prepare_body(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """下游请求体 → 上游请求体：深拷 messages、写死 model、强制 stream。

    `messages` 必须深拷贝：下游可能原地改写，不能穿透回引擎持有的
    `request.raw`（会话粘性指纹与续写都读它）。非流式由引擎聚合，故 `stream`
    无条件置 true——与其余渠道同口径，少一条分支。
    """
    body = dict(payload)
    body["messages"] = copy.deepcopy(payload.get("messages"))
    base.normalize_developer_role(body["messages"])
    body["model"] = model
    body["stream"] = True
    return body


def request_headers(token: str, *, accept: str = "text/event-stream") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": accept,
        "User-Agent": OFFICIAL_UA,
    }


class UpstreamHTTPError(base.UpstreamHTTPError):
    """上游非 2xx；`kind()` 走本渠道的状态码规则。"""

    classify_status = staticmethod(raccoon_events.classify_status)


class UpstreamProtocolViolation(base.UpstreamProtocolViolation):
    """上游返回了本渠道约定之外的东西（模型回显不一致等）。"""


class LoginRejected(base.UpstreamReloginRequired):
    """密码登录被上游拒绝（账号密码错 / 格式不符）。

    继承 `UpstreamReloginRequired` 是刻意的：这类失败**重试无意义**，再试只会
    累积风控与锁定风险。签到任务捕获它之后把凭证标成「需重新登录」并停手，
    与「刷新令牌已作废」同一处置（见 `tasks/refresh.py` 的同名分支）。
    """


class RaccoonClient(LazyClients):
    """上游 HTTP 客户端。host 可覆盖，便于测试与灰度。"""

    def __init__(
        self,
        *,
        host: str = RACCOON_HOST,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
        proxy: str | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self._stream_client = stream_client
        self._short_client = short_client
        self._proxy = proxy

    async def stream_chat(self, payload: dict[str, Any], model: str,
                          token: str) -> AsyncIterator[Event]:
        """POST chat/completions 并逐事件产出；非 2xx 抛 UpstreamHTTPError。

        **模型回显校验**：上游对未知模型名静默回落默认模型（实测 2026-11-04），
        首帧带 `model` 时立即比对，不一致抛协议违规——宁可这次请求失败，也不让
        客户端「问 A 答 B」。
        """
        body = prepare_body(payload, model)
        expected = model.strip().lower()
        async with self._stream.stream(
            "POST", f"{self.host}{EP_CHAT}", json=body,
            headers=request_headers(token),
        ) as response:
            if response.status_code >= 400:
                raw = await base.read_body_bounded(response)
                raise UpstreamHTTPError(response.status_code, raw,
                                        **base.retry_hints(response.headers))
            async for frame in iter_frames(response.aiter_bytes()):
                for event in raccoon_events.parse_frame(frame):
                    echoed = event.model
                    if isinstance(echoed, str) and echoed.strip() \
                            and echoed.strip().lower() != expected:
                        raise UpstreamProtocolViolation(
                            f"model echo mismatch: asked {model!r}, "
                            f"upstream returned {echoed!r}")
                    yield event

    async def fetch_models(self, token: str) -> list[Model]:
        """GET 模型目录：一层的 `categories[].models[]`，按 id 去重。"""
        response = await self._short.get(
            f"{self.host}{EP_MODELS}",
            headers=request_headers(token, accept="application/json"))
        data = await json_or_raise(
            response, UpstreamHTTPError, "models",
            message="non-JSON models response",
            violation_cls=UpstreamProtocolViolation)
        payload = data.get("data") if isinstance(data, dict) else None
        categories = payload.get("categories") if isinstance(payload, dict) else None
        if not isinstance(categories, list):
            raise UpstreamProtocolViolation("models response missing categories")
        models: dict[str, Model] = {}
        for category in categories:
            if not isinstance(category, dict):
                continue
            items = category.get("models")
            if not isinstance(items, list):
                continue
            for item in items:
                model = _model_from_item(item)
                if model is not None:
                    models.setdefault(model.id.lower(), model)
        if not models:
            raise UpstreamProtocolViolation("models api returned no models")
        return list(models.values())

    async def fetch_quota(self, token: str) -> Quota:
        """GET 积分余额 + 流水 → 额度（含四个池的明细与逐笔到期阶梯）。

        上游只给「当前可用」一个总数，没有总量/消费口径；`total=remaining`
        （健康度恒 100 直到 0）。**不编造**其它来源：日额度耗尽与余额耗尽是
        两回事，猜错会让调度把备用渠道误判为已耗尽。

        明细（`packages`）来自 balance 的四个池，逐笔到期（`expiry_ladder`）
        来自流水的 `daily_grant`——两者口径不同，不能互相换算，故分开取。
        流水失败**不影响**余额结论（余额是本接口的主要产物，到期阶梯只是
        调度排序的加分项）：流水拿不到就退化成「无阶梯」。
        """
        response = await self._short.get(
            f"{self.host}{EP_BALANCE}",
            headers=request_headers(token, accept="application/json"))
        data = await json_or_raise(
            response, UpstreamHTTPError, "balance",
            message="non-JSON balance response",
            violation_cls=UpstreamProtocolViolation)
        payload = data.get("data") if isinstance(data, dict) else None
        points = payload.get("available_points") if isinstance(payload, dict) else None
        if not isinstance(points, (int, float)) or isinstance(points, bool):
            raise UpstreamProtocolViolation("balance response missing available_points")
        remaining = float(points)
        now = int(time.time())
        # 流水只取一次：明细（逐笔发放 + 到期）与到期阶梯同一份数据，避免
        # 两个请求之间上游又发了一笔导致面板与阶梯对不上。
        items = await self._fetch_income(token)
        packages = raccoon_activity.quota_packages(payload, items)
        # 阶梯与明细同源：组头「窗口内到期」按明细里每笔的**剩余**算，
        # 不再用「发放额」（那样会把已消耗的部分也算成将要过期）。
        ladder = raccoon_activity.ladder_from_packages(packages)
        return Quota(remaining=remaining, total=remaining, packages=packages or None,
                     expiry_ladder=ladder, probed_at=now)

    async def _fetch_income(self, token: str) -> list[Any] | None:
        """发放流水（`type=income`）；失败返回 None（不阻断额度探测）。"""
        try:
            return await self.fetch_bills(token, bill_type=BILL_TYPE_INCOME)
        except (UpstreamHTTPError, UpstreamProtocolViolation):
            return None

    async def fetch_bills(self, token: str,
                          limit: int = raccoon_activity.BILL_LOOKBACK,
                          bill_type: str = BILL_TYPE_ALL) -> list[Any]:
        """GET 积分流水（最近 `limit` 条）。

        `type` 是上游的**服务端过滤**：`all` 混着逐次调用的消费流水，
        `income` 只留发放（`daily_grant` / `reward_grant`）。签到与到期阶梯
        必须用 `income`——`all` 里消费占绝大多数，活跃账号的每日发放会被
        挤出条数窗口（实测 2026-11-10：055920 的 11-09 那笔排在第 62 条，
        60 条窗口里一条 grant 都看不到，于是误报「今日尚未发放」）。
        """
        response = await self._short.get(
            f"{self.host}{EP_BILLS}",
            params={"paging.limit": limit, "type": bill_type},
            headers=request_headers(token, accept="application/json"))
        data = await json_or_raise(
            response, UpstreamHTTPError, "bills",
            message="non-JSON bills response",
            violation_cls=UpstreamProtocolViolation)
        payload = data.get("data") if isinstance(data, dict) else None
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise UpstreamProtocolViolation("bills response missing items")
        return items

    async def visit(self, token: str) -> dict[str, Any]:
        """GET 网页会话 `user_info`：模拟一次页面访问（幂等、只读）。

        为什么仍然要它：**不是为了签出积分**（2026-11-18 复测：只拿旧 token 打
        `user_info` 不会补发 `daily_grant`，见 `activity.py` 模块文档），而是为了
        拿到 `trusted_device`——会话是否已被上游判为不可信、需不需要重登，只有
        这个接口会说。另有一个副作用：刷新设备/会话的 `last_seen_at`，让会话
        看起来仍是活的。积分补发只发生在**新登录会话**（`login()`）。
        """
        response = await self._short.get(
            f"{self.host}{EP_USER_INFO}",
            headers=request_headers(token, accept="application/json"))
        data = await json_or_raise(
            response, UpstreamHTTPError, "user_info",
            message="non-JSON user_info response",
            violation_cls=UpstreamProtocolViolation)
        payload = data.get("data") if isinstance(data, dict) else None
        return payload if isinstance(payload, dict) else {}

    async def heartbeat(self, token: str, device_id: str) -> dict[str, Any]:
        """POST 设备心跳（网页端真实接口，幂等）。

        缺 `x-client-*` 头族会回 `100002 params_invalid_error` /
        `param client headers required invalid`——实测 2026-11-04。
        """
        headers = request_headers(token, accept="application/json")
        headers.update(raccoon_activity.heartbeat_headers(device_id))
        response = await self._short.post(
            f"{self.host}{EP_HEARTBEAT}", headers=headers)
        data = await json_or_raise(
            response, UpstreamHTTPError, "heartbeat",
            message="non-JSON heartbeat response",
            violation_cls=UpstreamProtocolViolation)
        payload = data.get("data") if isinstance(data, dict) else None
        return payload if isinstance(payload, dict) else {}

    async def bind_device(self, token: str, device_id: str) -> dict[str, Any] | None:
        """把当前会话绑定到一台设备（浏览器登录后立刻做的动作，幂等）。

        为什么登录后必须绑：实测新登录的会话是 `session_without_bound_device`
        （`needs_bind=true`, `trusted=false`），此时心跳恒回
        `400 200811 device_bind_required`，且「当天登录后延期 7 天」的判定也
        不成立——浏览器正是靠这一步把会话变成 trusted。

        失败返回 None 而不是抛：绑定只影响「会话可信」，登录本身已经成功
        （token 能聊天、能触发当天补发），不该因为绑定失败把登录整体判负。
        """
        headers = request_headers(token, accept="application/json")
        headers.update({
            "x-client-device-id": device_id,
            "x-client-platform": "web",
            "x-client-version": "156.0.0.0",
        })
        payload = {
            "application": "web",
            "client_device_id": device_id,
            "client_device_id_source": device_id,
            "client_platform": "web",
            "client_version": "156.0.0.0",
            "device_name": "Chrome",
            "os": "macOS",
            "os_version": "10.15.7",
            "push_permission": "granted",
        }
        try:
            response = await self._short.post(
                f"{self.host}{EP_BIND_DEVICE}", json=payload, headers=headers)
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        body = data.get("data") if isinstance(data, dict) else None
        return body if isinstance(body, dict) else {}

    async def login(self, phone: str, password: str) -> dict[str, Any]:
        """手机号 + 密码登录 → `{access_token, refresh_token}`（新会话）。

        走**现行 IAM 链路**：`POST /api/iam/v1/auth/login/password/phone` →
        `POST /api/web/auth/v1/iam/confirm`。加密口径见 `login.py`。

        为什么必须换链路（2026-11-19 实测，勿回退）：旧接口
        `login_with_password` 仍可用、仍发 token，但服务端**不再补发当天
        `daily_grant`**；换成 IAM 登录 + confirm 后同样的账号当天就能拿到 300。
        这解释了此前「自动登录成功、签到仍然没积分」。

        `refresh_token` 在网页会话下与 access 同源（实测 `POST /refresh` 原样
        换回同一张），故只取 `access_token`，不落 refresh。

        抛错口径：

        * 密码短于上游下限 → `LoginRejected`（本地拦，省一次必然被拒的请求）；
        * `401 AUTH_ACCOUNT_OR_PASSWORD_INVALID` / `400 COMMON_INVALID_ARGUMENT`
          → `LoginRejected`：**重试无意义**，只会累积风控与锁定风险；
        * 其余 4xx/5xx → `UpstreamHTTPError`（网络/上游故障，可稍后再试）。
        """
        if len(password) < raccoon_login.MIN_PASSWORD_LENGTH:
            raise LoginRejected(
                f"登录密码少于 {raccoon_login.MIN_PASSWORD_LENGTH} 位，"
                "上游不接受（密码错误或未设置密码请改用网页登录后重贴凭据）")
        body = {
            "nation_code": raccoon_login.NATION_CODE,
            "phone": raccoon_login.encrypt_param(
                raccoon_login.normalize_phone(phone)),
            "password": raccoon_login.encrypt_param(password),
        }
        response = await self._short.post(
            f"{self.host}{EP_LOGIN}", json=body,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "Accept-Language": "zh-Hans",
                     "Origin": self.host,
                     "Referer": f"{self.host}/login?lang=zh",
                     "User-Agent": LOGIN_UA})
        raw = await base.read_body_bounded(response)
        if response.status_code >= 400:
            # 账号密码错与参数格式错都属于「这张凭证自愈不了」，不能当网络故障
            # 反复重试——上游对连续失败会加验证码甚至锁号。
            lowered = raw.lower()
            if b"auth_account_or_password_invalid" in lowered \
                    or b"common_invalid_argument" in lowered:
                raise LoginRejected(
                    "上游拒绝登录（手机号或密码错误/格式不符）：请核对凭据里的 "
                    f"phone/password，或改用网页登录后重贴 token（原始响应：{raw[:160]!r}）")
            raise UpstreamHTTPError(response.status_code, raw)
        data = await json_or_raise(
            response, UpstreamHTTPError, "login",
            message="non-JSON login response",
            violation_cls=UpstreamProtocolViolation)
        payload = data.get("data") if isinstance(data, dict) else None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise UpstreamProtocolViolation("login response missing access_token")
        # IAM 确认是**必需**的：不确认的 token 打 `/api/web/*` 全 400
        # `200901 iam_confirm_required`，等于换了一张不能用的会话。
        await self.confirm_iam(token)
        return payload

    async def page_boot(self, token: str, device_id: str) -> dict[str, Any] | None:
        """模拟浏览器打开 `/work/home` 的启动序列 —— 每日积分的**实际入账点**。

        为什么登录之外还要这一步（2026-11-19 实测，勿删）：拿 IAM 登录得到的新
        会话只打 `user_info` / `balance`，当天 `daily_grant` 依然不来；把浏览器
        那个页面加载（首页 HTML + 一组启动 XHR + 活动上报）走一遍，积分就在几秒
        内入账。也就是说上游把「补发」挂在**页面启动**这一事件上，而不是登录本
        身——只做 login + bind 的旧实现因此长期「登录成功但没积分」。

        全部幂等只读（`b/v1/m` 只是埋点）。**尽力而为**：每一步的失败都吞掉，
        因为签到判定仍以流水为准，这里失败最多是这一轮少到一笔，不该把登录整体
        判负。返回绑定响应（拿得到时），供调用方写回设备 id。
        """
        headers = {**request_headers(token, accept="application/json"),
                   "Cookie": f"xhx_token={token}",
                   "Accept-Language": "zh-CN,zh;q=0.9",
                   "X-Client-Platform": "web",
                   "X-Client-Version": "156.0.0.0",
                   "X-Client-Device-ID": device_id,
                   "X-Raccoon-Language": "zh",
                   "Origin": self.host,
                   "Referer": f"{self.host}/work/home?lang=zh"}
        # ① 首页 HTML：真正的「页面加载」事件（SSR 侧才看得到浏览器会话）
        with contextlib.suppress(httpx.HTTPError):
            await self._short.get(f"{self.host}{EP_PAGE_HOME}",
                                  headers={**headers, "Accept": "text/html"})
        # ② 页面启动时的接口序列（顺序与浏览器实测一致）
        for path in (EP_AGREEMENT_CHECK, EP_OFFICE_SETTING, EP_ONLINE_ACTIVITY):
            with contextlib.suppress(httpx.HTTPError):
                await self._short.get(f"{self.host}{path}", headers=headers)
        # ③ 绑定 + 心跳：把新会话变成 trusted，同日才认「登录过」
        bound = await self.bind_device(token, device_id)
        if isinstance(bound, dict) and isinstance(bound.get("device_id"), str) \
                and bound["device_id"]:
            device_id = bound["device_id"]
        with contextlib.suppress(httpx.HTTPError):
            await self.heartbeat(token, device_id)
        # ④ 活动上报：`machine_id` 是官方 bundle 写死的常量
        with contextlib.suppress(httpx.HTTPError):
            await self._short.post(
                f"{self.host}{EP_METRICS}",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "common_header": {"client_agent": LOGIN_UA,
                                      "machine_id": PAGE_MACHINE_ID},
                    "metrics": [{"metric_type": "event", "event": {
                        "event_name": "activity",
                        "event_params": {"id": "1", "name": "activity",
                                         "prompt": str(int(time.time() * 1000))}}}],
                })
        return bound

    async def confirm_iam(self, token: str) -> dict[str, Any]:
        """IAM 登录后确认（浏览器同款第二步，幂等）。

        不调这一步的会话对 `/api/web/*` 一律不可用（`400 200901
        iam_confirm_required`），签到/绑定/流水全部读不到。失败抛
        `UpstreamHTTPError`：确认不成功说明这次登录拿不到可用会话，调用方
        应把它当登录失败处理，而不是把半张会话落库。
        """
        response = await self._short.post(
            f"{self.host}{EP_IAM_CONFIRM}",
            json={"registration_attribution": {"registration_source": "web"}},
            headers={**request_headers(token, accept="application/json"),
                     "Origin": self.host,
                     "Referer": f"{self.host}/"})
        data = await json_or_raise(
            response, UpstreamHTTPError, "iam/confirm",
            message="non-JSON iam/confirm response",
            violation_cls=UpstreamProtocolViolation)
        return data if isinstance(data, dict) else {}


def _model_from_item(item: Any) -> Model | None:
    """目录条目 → 中立 Model；缺 id 的条目跳过。

    倍率取 `billing_effective_multiplier`（含限时优惠后的**实际**倍率）：
    0 就是免费（调度器会优先选），非 0 透传。上下文/输出上限上游不提供，
    留 None——**不猜**（本仓压缩只在有上限时才裁剪）。
    """
    if not isinstance(item, dict):
        return None
    model_id = item.get("name") or item.get("model_name")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    tags = item.get("tags")
    tag_set = {str(tag).lower() for tag in tags} if isinstance(tags, list) else set()
    rate = item.get("billing_effective_multiplier")
    return Model(
        id=model_id.strip(),
        name=model_id.strip(),
        credit_rate=(float(rate)
                     if isinstance(rate, (int, float)) and not isinstance(rate, bool)
                     else None),
        supports_images=("vision" in tag_set) if tag_set else None,
        supports_tool_call=True,
        supports_reasoning=("reasoning" in tag_set) if tag_set else None,
    )


@dataclass(slots=True)
class RaccoonProvider:
    """Provider 协议实现。

    登录轨道不实现：凭证来自浏览器会话，只能由用户从 DevTools 复制后经
    「JSON 导入」录入（见 `credential.py`）。因此这里没有 `start_auth` /
    `poll_auth` / `complete_callback`——写了也只是永远不被调用的死代码；
    与 Zen/Kilo 的「按方法缺失自然跳过」同一分工。
    """

    client: RaccoonClient = field(default_factory=RaccoonClient)
    pacer: Any | None = None

    id: str = "raccoon"
    # 最近一次目录里的模型（小写）；用于发请求前的廉价校验。空集表示还没拉过
    # 目录，放行（真实保护靠上游回显的 `model`）。`init=False` 让它不参与构造。
    known_models: set[str] = field(default_factory=set, init=False)

    def import_credential(self, raw: dict) -> dict:
        """导入入参 → 落库形态；token 为空直接报错（不让空凭证进池）。"""
        credential = RaccoonCredential.from_import(raw)
        if not credential.access_token:
            raise UpstreamProtocolViolation(
                "missing access token: paste the xhx_token cookie value "
                "or {\"access_token\": \"...\"}")
        return credential.to_dict()

    def credential_expires_at(self, credential_data: dict) -> int:
        """JWT `exp`：供管理台提示「30 天后要重新取」；0 = 未知。"""
        return RaccoonCredential.from_dict(credential_data).expires_at

    def set_login_secret(self, credential_data: dict, phone: str,
                         password: str) -> dict:
        """写入/清除按需登录用的手机号与密码（管理台入口在
        `PUT /api/credentials/{id}/login-secret`）。

        两个都空 = 清除（按需登录随之停用，签到回落到「需人工重登」提示）。
        只动这两个字段，其余凭证内容原样保留——运营改密码不该把 token 冲掉。
        """
        data = dict(credential_data)
        data["phone"] = phone
        data["password"] = password
        return data

    def classify(self, status: int, body: bytes) -> ErrKind:
        return raccoon_events.classify_status(status, body)

    async def probe_quota(self, credential_data: dict) -> Quota:
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return Quota(probe_failed=True, probed_at=int(time.time()))
        return await self.client.fetch_quota(credential.access_token)

    # ---------------------------------------------------------- 签到 / 活跃

    async def checkin(self, credential_data: dict) -> CheckinResult:
        """「签到」= 页面访问 + （未到账时）页面启动 + 核对流水。

        上游没有领取接口（见 `activity.py` 模块文档），故本方法是**幂等只读**
        的组合：`user_info`（顺带读会话信任状态）→ bills 先看今天有没有 →
        没有就走一遍 `page_boot`（浏览器打开 `/work/home` 的启动序列，每日积分
        的**实际入账点**）→ 再读 bills 核对。只认流水，不看 HTTP 200——否则会
        重演「界面显示签到成功、积分没到」。

        **不能把「账户里还有旧积分」当今天已签**：上游当天没登录就不会补发
        `daily_grant`（2026-11-18 复测，见 `activity.py`）。旧实现这么做，导致
        缺当天登录的号连续两天报「今日额度可用 300」的假绿。

        页面访问失败不阻断核对：签到本身还带了余额与流水两个请求，只要流水能
        读出来就照样能判定；失败原因会经 `user_info` 的 `trusted_device` 反映。
        """
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return CheckinResult(ok=False, message="凭证缺少 token")
        started = time.time()
        # ① 页面级访问，同时拿到会话信任状态（需重登的唯一机器可读信号）
        user_info: dict[str, Any] | None = None
        with contextlib.suppress(UpstreamHTTPError, UpstreamProtocolViolation):
            user_info = await self.client.visit(credential.access_token)
        # ② 先看今天到底有没有到账：已有就直接判定，不白走一遍页面启动
        #    （页面启动有 5~6 个请求，签到每小时一轮，不该每轮都打）
        items: list[Any] | None = None
        with contextlib.suppress(UpstreamHTTPError, UpstreamProtocolViolation):
            items = await self.client.fetch_bills(credential.access_token,
                                                  bill_type=BILL_TYPE_INCOME)
        if items is not None and raccoon_activity.pick_today_grant(
                items, raccoon_activity.now_cst()) is not None:
            return raccoon_activity.checkin_result(
                items, started, user_info=user_info)
        # ③ 还没到账 → 补一遍**页面启动**（每日积分的实际入账点，见
        #    `client.page_boot`）。这一步而不是「重新登录」才是当天已登录却没
        #    拿到积分时的正解：token 有效时新登录未必被上游当作「又一天登录」。
        if not raccoon_activity.session_needs_relogin(user_info) \
                and credential.uid:
            with contextlib.suppress(httpx.HTTPError):
                await self.client.page_boot(
                    credential.access_token,
                    raccoon_credential.derive_device_id(credential.uid))
        # ④ 触达积分服务并重读流水核对（必须用 `income` 过滤——`all` 里消费
        #    流水占绝大多数，活跃账号的每日发放会被挤出条数窗口）。
        await self.client.fetch_quota(credential.access_token)
        try:
            items = await self.client.fetch_bills(credential.access_token,
                                                  bill_type=BILL_TYPE_INCOME)
        except UpstreamHTTPError as error:
            return CheckinResult(ok=False, message=f"流水查询失败（HTTP {error.status}）")
        except UpstreamProtocolViolation:
            return CheckinResult(ok=False, message="流水响应格式异常")
        return raccoon_activity.checkin_result(items, started, user_info=user_info)

    async def checkin_status(self, credential_data: dict) -> dict:
        """只读签到状态：「今天到底有没有发过积分」。

        与 `checkin` 的区别：这里不触发任何访问，只读流水。判据与 `checkin`
        **同一口径**（`pick_today_grant`）——今天没有 `daily_grant` 就是未签，
        绝不拿「账户里还有旧积分」冒充今天已签（旧实现这么干，见 `activity.py`）。
        """
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return {"active": False, "today_checked_in": False}
        items = await self.client.fetch_bills(credential.access_token,
                                              bill_type=BILL_TYPE_INCOME)
        row = raccoon_activity.pick_today_grant(items, raccoon_activity.now_cst())
        credit = float(row.get("points") or 0) if row is not None else None
        return {
            "active": True,
            "today_checked_in": credit is not None,
            "today_credit": credit,
            "activity_name": "每日积分（当天网页登录后发放）",
        }

    def checkin_scope(self, credential_data: dict) -> str:
        """签到隔离键：uid；身份未知返回空串（调用方回落凭证 ID）。

        与 CodeArts / Qoder 同取向：返回空串而非 `raccoon|`——后者会让同渠道
        所有身份未知的凭证算出同一 scope，`CheckinTask` 的 seen 集合只跑第一个。
        """
        uid = RaccoonCredential.from_dict(credential_data).uid
        return f"raccoon|{uid}" if uid else ""

    async def relogin(self, credential_data: dict) -> dict[str, Any] | None:
        """按需登录：手机号 + 密码换一份**新会话**凭据（没有登录凭据时返回 None）。

        为什么需要它：上游只在**当天第一次登录**时补发每日 300 分，而旧 token
        无论怎么访问都不会触发（见 `activity.py`）。`RefreshTask` 的 refresh
        轨道在这里没用——`/refresh` 只是原样换回同一张 token（sid 与 iat 都不变，
        实测），产生不了「新登录」这一事件。所以只能真登录。

        **登录链路必须是 IAM 那条**（`client.login` 内部已含 `iam/confirm`）：
        旧的 `login_with_password` 能换 token 但不再补发当天积分，2026-11-19
        三大号「自动登录成功、签到仍无积分」就是栽在这上面。

        返回的 dict 沿用落库形态并保留 `phone` / `password`（否则下一次按需登录
        就没凭据了）；`uid` / `nickname` / `expires_at` 从新 token 的 claims 重解。

        `LoginRejected`（账号密码错/格式不符）向上抛：调用方应把凭证标成
        「需重新登录」并停止重试，避免连续失败把账号打进风控或锁号。
        """
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.phone or not credential.password:
            return None
        payload = await self.client.login(credential.phone, credential.password)
        refreshed = RaccoonCredential.from_import({
            "access_token": payload["access_token"],
            "phone": credential.phone,
            "password": credential.password,
            "nickname": credential.nickname,
        })
        # 登录后走一遍**页面启动**（浏览器同款流程）：绑设备 + 首页加载 + 活动
        # 上报。只 bind 不加载页面的会话 trusted 虽为 true，当天积分仍不到账
        # （2026-11-19 实测）；页面启动失败不判登录失败（token 已可用）。
        if refreshed.uid:
            await self.client.page_boot(
                refreshed.access_token,
                raccoon_credential.derive_device_id(refreshed.uid))
        return refreshed.to_dict()

    async def activity(self, credential_data: dict) -> ActivityResult:
        """活跃上报：页面访问 + 设备心跳（都幂等、只读）。

        心跳只刷新设备 `last_seen_at`；`user_info` 的 `trusted_device` 才是会话
        状态的真相（`needs_relogin` / `device_bind_state`）。进入
        `device_session_mismatch` 后心跳恒回 `400 200811 device_bind_required`，
        而该状态**无法自愈**（`devices_current_bind` 同样 400、`refresh` 只换同一
        张 token）——只能让用户在网页重新登录后重新导入，故这里点名说出来，
        而不是回一句「心跳失败（HTTP 400）」。
        """
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            return ActivityResult(ok=False, message="凭证缺少 token")
        token = credential.access_token
        visit_error: str | None = None
        user_info: dict[str, Any] | None = None
        try:
            user_info = await self.client.visit(token)
        except UpstreamHTTPError as error:
            visit_error = f"页面访问失败（HTTP {error.status}）"
        except UpstreamProtocolViolation:
            visit_error = "页面访问响应格式异常"
        relogin = raccoon_activity.session_needs_relogin(user_info)
        if relogin:
            # 心跳在这种会话下必然 400：不再白发一次，直接报可操作原因。
            # 走到这里说明 visit 成功并拿到了 trusted_device，故没有 visit_error
            # 可拼（visit 抛异常时 user_info 为空，relogin 也必为空）。
            return ActivityResult(ok=False, user_id=credential.uid, message=relogin)
        try:
            payload = await self.client.heartbeat(
                token, raccoon_credential.derive_device_id(credential.uid))
        except UpstreamHTTPError as error:
            message = f"心跳失败（HTTP {error.status}）"
            if raccoon_activity.heartbeat_needs_relogin(error.body):
                message += f"；{raccoon_activity.RELOGIN_HINT}"
            if visit_error:
                message += f"；{visit_error}"
            return ActivityResult(ok=False, message=message)
        except UpstreamProtocolViolation:
            message = "心跳响应格式异常"
            if visit_error:
                message += f"；{visit_error}"
            return ActivityResult(ok=False, message=message)
        seen = payload.get("last_seen_at")
        trusted = payload.get("trusted_device")
        detail = "设备心跳已上报"
        if isinstance(seen, str) and seen:
            detail += f"（上游 last_seen_at={seen}）"
        if trusted is False:
            detail += f"；{raccoon_activity.RELOGIN_HINT}"
        if visit_error:
            return ActivityResult(ok=False, user_id=credential.uid,
                                  message=f"{visit_error}；{detail}")
        return ActivityResult(ok=True, message=f"页面访问已上报；{detail}",
                              user_id=credential.uid)

    def activity_scope(self, credential_data: dict) -> str:
        """活跃上报隔离键：uid（同账号多凭证共享「每天一次」）。"""
        uid = RaccoonCredential.from_dict(credential_data).uid
        return f"raccoon|{uid}" if uid else ""

    async def list_models(self, credential_data: dict) -> list[Model]:
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            raise UpstreamProtocolViolation("raccoon credential missing token")
        models = await self.client.fetch_models(credential.access_token)
        self.known_models = {model.id.lower() for model in models}
        return models

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        credential = RaccoonCredential.from_dict(credential_data)
        if not credential.access_token:
            raise UpstreamProtocolViolation("raccoon credential missing token")
        if self.known_models and model.strip().lower() not in self.known_models:
            # 目录里没有的模型：上游会静默回落默认模型并 200。这里按「模型不存在」
            # 处理（400/INVALID），不进上游也不冷却凭证。
            raise UpstreamHTTPError(
                404, b'{"error":{"message":"model not found in raccoon catalog"}}')
        key = credential.uid or "raccoon"
        async with paced(self.pacer, key):
            async for event in self.client.stream_chat(
                    payload, model, credential.access_token):
                yield event

    async def aclose(self) -> None:
        await self.client.aclose()