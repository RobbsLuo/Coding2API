"""WPS 灵犀上游客户端（桌面端 agent-runtime 通路）。

三条请求做成一次对话（取证：magpie-wps-lingxi 1.3.31 / MiraiHub）：

1. `POST /api/aioffice/v1/cowork/sessions` → `session_id`
2. `POST /api/public/v1/credits/runs` → `run_id`（**按 run 计费**：一个 run
   内多次调用只扣一次智点，故 run 按「账号 × 模型」缓存 1 小时、不滑动续期）
3. `POST /api/public/v1/agent-runtime/chat/completions`（自定义 SSE）

账号面（profile / plans / balance / tasks）走同一 base；`lingxi.wps.cn` 不通
时回落 `lingxi.kdocs.cn`（kdocs 账号的集群）。

鉴权只有 `Cookie: wps_sid=…`，再追加三段桌面客户端标记（`x-kso-*`）。
没有 refresh 接口 —— cookie 过期只能重新粘贴，真正的判据是上游 401。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ...provider import base
from ...provider.base import (
    CheckinResult,
    Event,
    EventKind,
    Model,
    Quota,
    UpstreamProtocolViolation,
)
from ..lazy_clients import LazyClients
from . import events as lingxi_events
from .credential import APP_NAME, APP_VERSION, DEVICE_ID, LingxiCredential

#: 账号面 / 对话面主域与回落域（顺序即尝试顺序）
BASE = "https://lingxi.wps.cn"
FALLBACK_BASE = "https://lingxi.kdocs.cn"

EP_PROFILE = "/api/public/v1/account/profile"
EP_PLANS = "/api/aioffice/v1/sessions/plans"
EP_BALANCE = "/api/public/v1/credits/balance"
EP_TASKS = "/api/public/v1/tasks"
EP_CLAIM = "/api/public/v1/tasks/%s/claim"
EP_SESSIONS = "/api/aioffice/v1/cowork/sessions"
EP_RUNS = "/api/public/v1/credits/runs"
EP_CHAT = "/api/public/v1/agent-runtime/chat/completions"

#: run 缓存时长：上游按 run 计费，一个 run 内多次调用只扣一次；不滑动续期。
RUN_TTL_SECONDS = 3600
#: 对话总时长上限：推理模型可能很久（与参考实现同口径）
CHAT_TIMEOUT = 600.0
#: 账号面短请求超时
API_TIMEOUT = 30.0
#: 上游客户端类型：桌面端（原生 tools / tool_calls 只在这条通路上有）
CLIENT_TYPE = "lingxi_claw"
CLIENT_LANG = "zh-CN"
#: 临时积分窗口（天）：余额里「快到期」的那部分
TEMP_CREDIT_DAYS = 15

#: 模型目录不可达时的内置快照（与客户端 1.3.31 的 14 个下拉项一致）
DEFAULT_MODELS: tuple[dict[str, Any], ...] = (
    {"id": "auto", "name": "自动推荐 (Auto)", "rate": 1.0,
     "context": 1048576, "vision": False, "reasoning": True},
    {"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash", "rate": 0.12,
     "context": 1048576, "vision": True, "reasoning": True},
    {"id": "deepseek-v4-flash-0731", "name": "DeepSeek V4 Flash", "rate": 0.09,
     "context": 1048576, "vision": False, "reasoning": True},
    {"id": "deepseek-v4-pro-0813", "name": "DeepSeek V4 Pro", "rate": 0.40,
     "context": 1048576, "vision": False, "reasoning": True},
    {"id": "kimi-k3", "name": "Kimi-K3", "rate": 1.60,
     "context": 1048576, "vision": True, "reasoning": True},
    {"id": "GLM-5.3", "name": "GLM-5.3", "rate": 0.75,
     "context": 1048576, "vision": False, "reasoning": True},
    {"id": "GLM-5.3-Flash", "name": "GLM-5.3-Flash", "rate": 0.11,
     "context": 1048576, "vision": True, "reasoning": True},
    {"id": "qwen3.8_max", "name": "Qwen3.8-Max", "rate": 0.80,
     "context": 1048576, "vision": True, "reasoning": True},
    {"id": "qwen3.8_flash", "name": "Qwen3.8-Flash", "rate": 0.08,
     "context": 1048576, "vision": True, "reasoning": True},
    {"id": "minimax_m3", "name": "MiniMax-M3", "rate": 0.20,
     "context": 1048576, "vision": True, "reasoning": False},
    {"id": "doubao_seed_2_1_pro_260628", "name": "Doubao-Seed-2.1 Pro",
     "rate": 0.50, "context": 262144, "vision": True, "reasoning": True},
    {"id": "mimo-v2.6-pro", "name": "Xiaomi MiMo-V2.6 Pro", "rate": 0.10,
     "context": 1048576, "vision": True, "reasoning": False},
    {"id": "plan_pro", "name": "Pro 模式 (深度推理)", "rate": 1.0,
     "context": 1048576, "vision": False, "reasoning": True},
    {"id": "plan_max", "name": "Max 模式 (复杂任务)", "rate": 1.0,
     "context": 1048576, "vision": False, "reasoning": True},
)

#: 工具 schema 压平的最大深度（防止递归 $ref 打爆栈）
_MAX_SCHEMA_DEPTH = 24


class UpstreamHTTPError(base.UpstreamHTTPError):
    """上游非 2xx；`kind()` 走本渠道的状态码规则。"""

    classify_status = staticmethod(lingxi_events.classify_status)


class RunExpired(RuntimeError):
    """run / 会话在上游侧失效（`SQLNoRows` /「会话不存在」）：重建一次即可。

    只在**首个有效帧之前**抛出：流已经开始就无法回滚已下发的内容。
    """


class LingxiClient(LazyClients):
    """上游 HTTP 客户端；host 可覆盖（测试与灰度）。"""

    def __init__(
        self,
        *,
        host: str = BASE,
        fallback_host: str = FALLBACK_BASE,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
        proxy: str | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.fallback_host = fallback_host.rstrip("/")
        self._stream_client = stream_client
        self._short_client = short_client
        self._proxy = proxy
        # run 缓存与单飞锁：同一 (账号, 模型) 并发时只建一个 run（多建多扣智点）
        self._runs: dict[str, dict[str, Any]] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._catalog: dict[str, Any] | None = None

    # ------------------------------------------------------------ 基础设施

    def bases(self) -> tuple[str, ...]:
        """账号面 / 对话面的域名回落顺序（去重，主域在前）。"""
        if self.fallback_host == self.host:
            return (self.host,)
        return (self.host, self.fallback_host)

    def headers(self, credential: LingxiCredential, *,
                accept: str = "application/json") -> dict[str, str]:
        """账号面请求头（Cookie + 桌面客户端标记）。"""
        return {
            "Cookie": credential.cookie_header(),
            "Content-Type": "application/json",
            "Accept": accept,
            "X-Client-Type": CLIENT_TYPE,
            "X-Client-Lang": CLIENT_LANG,
            "X-Cc-Agent-Type": CLIENT_TYPE,
            "X-Request-Id": uuid.uuid4().hex,
            "x-kso-app-name": APP_NAME,
            "x-kso-app-version": APP_VERSION,
            "x-kso-device-id": DEVICE_ID,
        }

    async def _json(self, method: str, url: str, *,
                    headers: dict[str, str],
                    body: dict[str, Any] | None = None,
                    timeout: float = API_TIMEOUT) -> dict[str, Any]:
        """短请求 → JSON（非 2xx 抛 `UpstreamHTTPError`，传输错误单独归类）。"""
        try:
            response = await self._short.request(
                method, url, headers=headers, json=body, timeout=timeout)
        except httpx.TransportError as error:
            raise base.UpstreamTransportError(str(error)) from error
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response),
                                    **base.retry_hints(response.headers))
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("non-JSON response") from error
        if not isinstance(data, dict):
            raise UpstreamProtocolViolation("response is not an object")
        return data

    async def _account_json(self, credential: LingxiCredential, path: str,
                            *, body: dict[str, Any] | None = None,
                            method: str = "GET") -> dict[str, Any]:
        """账号面 GET/POST，主域失败回落一次。

        **401 也回落**：kdocs 账号的 cookie 只在 `lingxi.kdocs.cn` 集群有效，
        主域回 401 不代表凭据失效（参考实现同口径：除审核拦截外都换域重试）。
        两个域都失败才上抛最后一个错误。
        """
        last: Exception | None = None
        for base_host in self.bases():
            try:
                return await self._json(method, base_host + path,
                                        headers=self.headers(credential),
                                        body=body)
            except (UpstreamHTTPError, base.UpstreamTransportError,
                    UpstreamProtocolViolation) as error:
                last = error
        if last is not None:
            raise last
        raise UpstreamProtocolViolation("灵犀没有可用的账号域")  # pragma: no cover

    # ---------------------------------------------------------- 账号面

    async def fetch_profile(self, credential: LingxiCredential) -> dict[str, Any]:
        """GET 账号资料：`data.id` / `data.name`（身份补全与昵称）。"""
        data = await self._account_json(credential, EP_PROFILE)
        payload = data.get("data")
        return payload if isinstance(payload, dict) else {}

    async def fetch_balance(self, credential: LingxiCredential) -> dict[str, Any]:
        """GET 智点余额（`total_balance` 为主，`bonus_credits[]` 带到期时刻）。"""
        data = await self._account_json(credential, EP_BALANCE)
        payload = data.get("data")
        return payload if isinstance(payload, dict) else {}

    async def probe_quota(self, credential: LingxiCredential) -> Quota:
        """余额 → 中立额度。积分就是主账，不做 token 换算。

        `total` 取「余额 与 赠送总额 的较大者」：上游只给剩余，没给已消耗，
        因此健康度是**近似**比例（参考实现同口径，界面只展示剩余与批次）。
        """
        payload = await self.fetch_balance(credential)
        remaining = _num(payload.get("total_balance"))
        if remaining is None:
            remaining = _num(payload.get("credits_balance"))
        if remaining is None:
            return Quota(probe_failed=True, probed_at=int(time.time()))
        batches = payload.get("bonus_credits")
        items = batches if isinstance(batches, list) else []
        summary = payload.get("bonus_summary")
        summary = summary if isinstance(summary, dict) else {}
        bonus_total = _num(summary.get("total_value")) or 0.0
        ladder: list[tuple[int, float]] = []
        packages: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            expire = _parse_expire(item.get("expire_time"))
            balance = _num(item.get("balance")) or 0.0
            if expire <= 0 or balance <= 0:
                continue
            packages.append({"name": "赠送智点", "total": balance,
                             "used": 0, "end": expire})
            if expire - time.time() <= TEMP_CREDIT_DAYS * 86400:
                ladder.append((expire, balance))
        ladder.sort()
        now = int(time.time())
        return Quota(
            remaining=remaining,
            total=max(remaining, bonus_total),
            expiry_ladder=ladder or None,
            packages=packages or None,
            probed_at=now,
        )

    async def fetch_models(self, credential: LingxiCredential) -> list[Model]:
        """GET 模型目录（`plans` + `models`）；失败回落内置快照。

        目录失败**不是**凭证故障（返回空列表由调用方回落），但 401 必须抛出：
        那是登录态失效，不能拿快照假装还活着。
        """
        try:
            data = await self._account_json(credential, EP_PLANS)
        except UpstreamHTTPError as error:
            if error.status in (401, 402):
                raise
            return _snapshot_models()
        except (base.UpstreamTransportError, UpstreamProtocolViolation):
            return _snapshot_models()
        payload = data.get("data")
        payload = payload if isinstance(payload, dict) else {}
        models: list[Model] = []
        seen: set[str] = set()
        for group, multiplier in (("plans", None), ("models", True)):
            items = payload.get(group)
            if not isinstance(items, list):
                continue
            for item in items:
                model = _model_of(item, multiplier)
                if model is None or model.id in seen:
                    continue
                seen.add(model.id)
                models.append(model)
        return models or _snapshot_models()

    async def fetch_tasks(self, credential: LingxiCredential) -> list[dict[str, Any]]:
        """GET 任务中心（签到 / 一次性任务）。"""
        data = await self._account_json(credential, EP_TASKS)
        payload = data.get("data")
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        return [item for item in tasks if isinstance(item, dict)] \
            if isinstance(tasks, list) else []

    async def claim_task(self, credential: LingxiCredential, key: str,
                         date: str = "") -> dict[str, Any]:
        """POST 领取任务；`date` 是签到窗口日期（上游要求 `YYYY-MM-DD`）。"""
        path = EP_CLAIM % key
        if date:
            path += "?date=" + date
        return await self._account_json(credential, path, body={}, method="POST")

    async def checkin(self, credential: LingxiCredential) -> CheckinResult:
        """每日签到：奖励按**领前/领后余额差**算。

        上游对重复领取回 200 假成功（`result=ok` 但余额不动），因此不能只看
        HTTP/result；余额差为 0 时读任务状态判定「今日已签」。
        """
        before = None
        try:
            payload = await self.fetch_balance(credential)
            before = _num(payload.get("total_balance"))
        except UpstreamHTTPError as error:
            if error.status == 401:
                return CheckinResult(ok=False, message="登录态已失效，请重新粘贴 Cookie")
        tasks = await self.fetch_tasks(credential)
        daily = next((item for item in tasks
                      if str(item.get("task_key") or "") == "daily_check_in"), None)
        if daily is None:
            return CheckinResult(ok=False, message="上游任务中心里没有每日签到")
        status = str(daily.get("status") or "")
        if status == "claimed":
            return CheckinResult(ok=True, already_checked_in=True,
                                 message="今日已签到", status=daily)
        claim = await self.claim_task(credential, "daily_check_in",
                                      _claim_date(daily))
        result = str(claim.get("result") or "")
        if result and result.lower() != "ok":
            return CheckinResult(ok=False, message=str(claim.get("hint")
                                                       or lingxi_events.text_of(claim)))
        credit: float | None = None
        try:
            after = _num((await self.fetch_balance(credential)).get("total_balance"))
        except UpstreamHTTPError:
            after = None
        if before is not None and after is not None:
            credit = max(0.0, after - before)
        if credit is None or credit <= 0:
            credit = _reward_of(claim, daily)
        if credit and credit > 0:
            return CheckinResult(ok=True, credit=credit, message="签到成功",
                                 status=daily)
        return CheckinResult(ok=True, already_checked_in=True,
                             message="今日已签到（上游幂等，余额未变化）",
                             status=daily)

    async def checkin_status(self, credential: LingxiCredential) -> dict[str, Any]:
        """只读签到状态（管理台展示）。"""
        tasks = await self.fetch_tasks(credential)
        daily = next((item for item in tasks
                      if str(item.get("task_key") or "") == "daily_check_in"), None)
        if daily is None:
            return {"active": False, "today_checked_in": False}
        status = str(daily.get("status") or "")
        extra = _dict(daily.get("extra"))
        return {
            "active": True,
            "today_checked_in": status == "claimed",
            "streak_days": _num(extra.get("consecutive_days")),
            "today_credit": _num(extra.get("today_reward")),
            "activity_name": "每日签到（智点）",
        }

    # ------------------------------------------------------------ 对话通路

    def _run_key(self, credential: LingxiCredential, model: str) -> str:
        return f"{credential.identity()}|{model}"

    def forget_run(self, credential: LingxiCredential, model: str) -> None:
        """丢弃缓存的 run（上游说会话不存在时调用）。"""
        self._runs.pop(self._run_key(credential, model), None)

    def _cached_run(self, key: str) -> dict[str, Any] | None:
        cached = self._runs.get(key)
        if cached is None:
            return None
        if time.time() - float(cached.get("at") or 0) >= RUN_TTL_SECONDS:
            self._runs.pop(key, None)
            return None
        return cached

    async def ensure_run(self, credential: LingxiCredential, model: str,
                         messages: list[Any] | None = None) -> dict[str, Any]:
        """拿一个可用 run：命中缓存（1 小时内）就用，否则建 会话 + 领 run。

        并发单飞：同一 (账号, 模型) 的并发请求共享同一把锁 —— 各建一个 run
        会各扣一次智点。锁内重读缓存，避免等锁期间别人已经建好。
        """
        key = self._run_key(credential, model)
        cached = self._cached_run(key)
        if cached is not None:
            return cached
        lock = self._run_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cached_run(key)
            if cached is not None:
                return cached
            run = await self._create_run(credential, model, messages or [])
            self._runs[key] = run
            return run

    async def _create_run(self, credential: LingxiCredential, model: str,
                          messages: list[Any]) -> dict[str, Any]:
        """建会话 + 领 run，主域失败回落一次。"""
        last: Exception | None = None
        for base_host in self.bases():
            try:
                session_id = await self._create_session(credential, base_host)
                run = await self._create_credit_run(credential, base_host,
                                                    session_id, model, messages)
            except (UpstreamHTTPError, base.UpstreamTransportError,
                    UpstreamProtocolViolation) as error:
                # 401/402 也换域：kdocs 账号只在自己的集群上认这份 cookie
                last = error
                continue
            run.update({"base": base_host, "at": time.time()})
            return run
        if last is not None:
            raise last
        # 两轮回落要么成功要么都抛异常，循环结束必然有 last（按构造不可达）
        raise UpstreamProtocolViolation(  # pragma: no cover
            "灵犀没有可用的对话域")

    async def _create_session(self, credential: LingxiCredential,
                              base_host: str) -> str:
        body = {"session_type": "cowork_client", "agent_version": 2,
                "env": {"device_id": DEVICE_ID,
                        "device_name": "coding2api",
                        "work_space_dir": "coding2api"}}
        data = await self._json("POST", base_host + EP_SESSIONS,
                                headers=self.headers(credential, accept="application/json"),
                                body=body)
        session_id = str(_dict(data.get("data")).get("session_id") or "").strip()
        if not session_id:
            raise UpstreamProtocolViolation("灵犀未返回 session_id")
        return session_id

    async def _create_credit_run(self, credential: LingxiCredential, base_host: str,
                                 session_id: str, model: str,
                                 messages: list[Any]) -> dict[str, Any]:
        body = {"client_request_id": str(int(time.time() * 1000)),
                "session_id": session_id,
                "model_provider": "lingxi",
                "model_id": model,
                "agent_input": {"text": last_user_text(messages)[:60],
                                "files": [], "file_ids": [],
                                "expect_model": model, "refs": [],
                                "permission_mode": "default",
                                "current_mode": "agent",
                                "browser_plugin_enabled": False}}
        data = await self._json("POST", base_host + EP_RUNS,
                                headers=self.headers(credential, accept="application/json"),
                                body=body)
        payload = _dict(data.get("data"))
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            raise UpstreamProtocolViolation("灵犀未返回 run_id")
        group_id = str(payload.get("group_id") or "").strip() or run_id
        return {"session_id": session_id, "run_id": run_id, "group_id": group_id}

    async def stream_chat(self, credential: LingxiCredential, payload: dict[str, Any],
                          model: str) -> AsyncIterator[Event]:
        """跑完三步并逐帧产出中立事件；非 2xx 抛 `UpstreamHTTPError`。

        `RunExpired` 只在首帧之前抛出（流已开始就无法回滚）；调用方据此丢弃
        缓存的重建一次。
        """
        run = await self.ensure_run(credential, model, payload.get("messages"))
        body = build_body(payload, model)
        headers = self.headers(credential, accept="text/event-stream")
        headers["x-cc-session-id"] = run["session_id"]
        headers["x-cc-run-id"] = run["run_id"]
        url = f"{run['base']}{EP_CHAT}"
        started = False
        try:
            async with self._stream.stream("POST", url, json=body,
                                           headers=headers,
                                           timeout=CHAT_TIMEOUT) as response:
                if response.status_code >= 400:
                    raw = await base.read_body_bounded(response)
                    raise UpstreamHTTPError(response.status_code, raw,
                                            **base.retry_hints(response.headers))
                async for line in response.aiter_lines():
                    frame = _frame_of(line)
                    if frame is None:
                        continue
                    if frame == {}:
                        return
                    error = lingxi_events.frame_error(frame)
                    if error is not None:
                        code, message = error
                        if not started and lingxi_events.is_session_expired(message):
                            raise RunExpired(message)
                        yield Event(kind=EventKind.ERROR, error_message=message,
                                    error_kind=lingxi_events.classify_code(code, message))
                        return
                    events = lingxi_events.parse_frame(frame)
                    if events:
                        started = True
                        for event in events:
                            yield event
                    if any(event.kind == EventKind.FINISH for event in events):
                        return
        except httpx.TransportError as error:
            raise base.UpstreamTransportError(str(error)) from error
        # 上游在 finish_reason 之前断流：如实报错，不伪造成功收尾
        raise UpstreamProtocolViolation(
            "灵犀上游流在完成事件之前结束（响应被截断）")

    async def aclose(self) -> None:
        await super().aclose()


# ---------------------------------------------------------------- 请求体


def last_user_text(messages: list[Any]) -> str:
    """最后一条 user 文本（上游用它做 run 标题）。"""
    for item in reversed(messages or []):
        if not isinstance(item, dict) or str(item.get("role")) != "user":
            continue
        content = item.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(str(block.get("text") or "")
                           for block in content if isinstance(block, dict))
        text = text.strip()
        if text:
            return text[:60]
    return "灵犀会话"


def normalize_messages(messages: Any) -> list[dict[str, Any]]:
    """入站消息归一：丢弃**孤立 tool 消息**（没有紧邻声明其 id 的 assistant）。

    上游对这类历史一律 400；与 Qoder 同一口径 —— 只丢弃、不重排、不改
    content。`assistant.tool_calls` 需与紧随其后的 `tool` 结果配对。
    """
    if not isinstance(messages, list):
        return []
    out: list[dict[str, Any]] = []
    declared: set[str] = set()
    for item in messages:
        if not isinstance(item, dict):
            continue
        message = dict(item)
        role = str(message.get("role") or "")
        if role == "assistant":
            calls = message.get("tool_calls")
            declared = set()
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, dict) and isinstance(call.get("id"), str):
                        declared.add(call["id"])
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in declared:
                continue
            declared.discard(call_id)
        else:
            declared = set()
        out.append(message)
    return out


def build_body(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """OpenAI 入站 → 上游 agent-runtime 请求体。

    `max_tokens` 必须包进 `base_llm_arguments`（顶层上游不理）；
    `reasoning_effort` 顶层与 `llm_arguments` 各写一份；工具 schema 必须先
    压平（`anyOf` / `$ref` / 数组型 `type` 会让整轮失败）。
    """
    body: dict[str, Any] = {"stream": True, "model": model,
                            "messages": normalize_messages(
                                copy.deepcopy(payload.get("messages")))}
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        usable = [tool for tool in (_tool_of(item) for item in tools
                                    if isinstance(item, dict)) if tool is not None]
        if usable:
            body["tools"] = usable
            choice = _tool_choice_of(payload.get("tool_choice"))
            if choice is not None:
                body["tool_choice"] = choice
    temperature = payload.get("temperature")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        body["temperature"] = temperature
    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) \
            and max_tokens > 0:
        body["base_llm_arguments"] = {"max_tokens": max_tokens}
    effort = payload.get("reasoning_effort")
    if isinstance(effort, str) and effort.strip():
        body["reasoning_effort"] = effort.strip()
        body["llm_arguments"] = {"reasoning_effort": effort.strip()}
    return body


def _tool_of(tool: dict[str, Any]) -> dict[str, Any] | None:
    """单个工具定义：只保留 `type=function` 且带 function 的条目。

    上游只支持函数工具；其它类型（`web_search` 之类）原样发过去会让整轮
    回一句「AI服务异常」，故直接丢弃 —— 少一个内置工具，比整轮失败好。
    """
    if str(tool.get("type") or "") != "function":
        return None
    function = tool.get("function")
    if not isinstance(function, dict):
        return None
    out = dict(tool)
    piece = dict(function)
    piece["parameters"] = sanitize_schema(piece.get("parameters"))
    out["function"] = piece
    return out


def _tool_choice_of(choice: Any) -> str | None:
    """`tool_choice` → 上游可接受的值。

    实测（2026-11-09）：字符串 `auto` / `none` / `required` 全部接受，
    **对象形态一律 400 `InvalidArgument`**（上游不支持点名具体函数）。
    故对象按语义降级成 `required`（仍保证「必须调用工具」），非法值丢弃。
    """
    if isinstance(choice, str):
        return choice.strip() or None
    if isinstance(choice, dict):
        return "required"
    return None


def sanitize_schema(schema: Any, defs: dict[str, Any] | None = None,
                    seen: frozenset[str] = frozenset(),
                    depth: int = 0) -> dict[str, Any]:
    """把任意 JSON Schema 压成上游能吃的朴素子集。

    上游只认最朴素的形状：出现 `anyOf` / `oneOf` / `$ref` 就整轮回
    「AI服务异常」，`type` 用数组则 400。故：

    * `$ref` 展开本地指针（循环引用降级成 string）；
    * `anyOf` / `oneOf` 选最「具体」的一支（object > array > 标量）；
    * `type` 数组取第一个非 null；
    * `properties` / `items` 递归压平，其余元数据（description/enum/required）
      保留。
    """
    if depth > _MAX_SCHEMA_DEPTH:
        return {"type": "string"}
    if not isinstance(schema, dict):
        return {"type": _type_of(schema)}
    defs = defs if isinstance(defs, dict) else {}
    local = schema.get("$defs")
    if isinstance(local, dict):
        defs = {**defs, **local}
    node = {key: value for key, value in schema.items() if key != "$defs"}
    definition = node.pop("definitions", None)
    if isinstance(definition, dict):
        defs = {**defs, **definition}

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in seen:
            return {"type": "string"}
        target: Any = defs
        for token in ref[2:].replace("~1", "/").replace("~0", "~").split("/"):
            if isinstance(target, dict) and token in target:
                target = target[token]
        if target is None:  # pragma: no cover - 指针遍历不会产出 None
            return {"type": "string"}
        return sanitize_schema(target, defs, seen | {ref}, depth + 1)

    for key in ("anyOf", "oneOf"):
        branches = node.pop(key, None)
        if isinstance(branches, list) and branches:
            candidates = [item for item in branches if isinstance(item, dict)]
            if not candidates:
                return {"type": "string"}
            chosen = max(candidates, key=_branch_score)
            merged = sanitize_schema(chosen, defs, seen, depth + 1)
            for extra in ("description", "title", "default"):
                if extra in node and extra not in merged:
                    merged[extra] = node[extra]
            return merged

    kind = node.get("type")
    if isinstance(kind, list):
        kinds = [item for item in kind if isinstance(item, str) and item != "null"]
        kind = kinds[0] if kinds else "string"
    node["type"] = kind if isinstance(kind, str) else "string"

    properties = node.get("properties")
    if isinstance(properties, dict):
        node["properties"] = {
            name: sanitize_schema(value, defs, seen, depth + 1)
            for name, value in properties.items()}
    items = node.get("items")
    if isinstance(items, dict):
        node["items"] = sanitize_schema(items, defs, seen, depth + 1)
    for key in ("allOf", "not", "if", "then", "else", "$id", "$schema"):
        node.pop(key, None)
    return node


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    return "string"


def _branch_score(node: dict[str, Any]) -> tuple[int, int]:
    rank = {"object": 3, "array": 2}.get(str(node.get("type") or ""), 1)
    return (rank, len(node))


def _frame_of(line: Any) -> dict[str, Any] | None:
    """一行 → 帧 dict；`None` 表示空行/注释/坏行，`{}` 表示 `[DONE]`。

    上游**每帧是单行** `data:{…}`（有时连着多行 `data:`），不能用「空行分帧」
    的通用 SSE 解析器 —— 那会把多帧吞成一帧。
    """
    if line is None:
        return None
    text = str(line).strip()
    if not text or text.startswith(":") or text.startswith("event:") \
            or text.startswith("id:"):
        return None
    if text.startswith("data:"):
        text = text[5:].strip()
    if not text:
        return None
    if text == "[DONE]":
        return {}
    try:
        frame = json.loads(text)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


def _snapshot_models() -> list[Model]:
    return [Model(id=item["id"], name=item["name"],
                  credit_rate=float(item["rate"]),
                  max_input_tokens=item["context"],
                  max_output_tokens=65536,
                  supports_images=bool(item["vision"]),
                  supports_tool_call=True,
                  supports_reasoning=bool(item["reasoning"]))
            for item in DEFAULT_MODELS]


def _model_of(item: Any, multiplier: bool | None) -> Model | None:
    """目录条目 → 中立 Model；缺 key 的条目跳过。"""
    if not isinstance(item, dict):
        return None
    model_id = item.get("key")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    efforts = item.get("reasoning_efforts")
    return Model(
        id=model_id.strip(),
        name=str(item.get("name") or model_id).strip(),
        credit_rate=(_multiplier_of(item) if multiplier else None),
        max_input_tokens=_int(item.get("max_context_tokens")),
        max_output_tokens=_int(item.get("max_new_tokens")),
        supports_images=bool(item.get("support_multimodal")),
        supports_tool_call=True,
        supports_reasoning=(bool(efforts) if isinstance(efforts, list)
                            else (False if multiplier else None)),
    )


def _multiplier_of(item: dict[str, Any]) -> float | None:
    """目录条目 → 倍率（兼容 `multiplier` / `credit_rate` / 嵌套 `credit`）。

    上游 1.3.31 的 `models[]` 用 `multiplier`，值是 `"～0.12x"` 这类带标记
    字符串（实测 2026-11-09：全角波浪线 + 数值 + `x`，`plans[]` 同字段为空串）；
    不同版本/灰度也见过数值与 `credit.multiplier` 的嵌套形态，依次尝试，
    取不到返回 None（不显示倍率，不猜）。
    """
    for key in ("multiplier", "credit_rate", "rate"):
        rate = _rate(item.get(key))
        if rate is not None:
            return rate
    credit = item.get("credit")
    return _rate(credit.get("multiplier")) if isinstance(credit, dict) else None


def _reward_of(claim: dict[str, Any], daily: dict[str, Any]) -> float | None:
    payload = _dict(claim.get("data"))
    extra = _dict(payload.get("extra"))
    for value in (extra.get("today_reward"), payload.get("reward_amount"),
                  daily.get("reward_amount")):
        number = _num(value)
        if number is not None:
            return number
    return None


def _claim_date(daily: dict[str, Any]) -> str:
    """签到窗口日期：任务里给了就用，否则用本地今天（上游按 UTC+8 换日）。"""
    extra = _dict(daily.get("extra"))
    for key in ("claim_date", "date", "today"):
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _parse_expire(value: Any) -> int:
    """`YYYY-MM-DD HH:mm[:ss]`（UTC+8）→ epoch 秒；解析不了返回 0。"""
    text = str(value or "").strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1]
    candidate = text.replace("T", " ")
    if candidate.endswith("+08:00"):
        candidate = candidate[:-6]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        return int(parsed.replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
    return 0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


#: 倍率文本：上游给的是带标记的字符串（`0.12x`、`0.75倍`、`×1.6`），
#: 也可能直接是数。只认「一个数字 + 可选倍率标记」，其余一律 None —— 宁可
#: 不显示倍率，也不把 `模式` 这类文案错认成数字。
_RATE_PATTERN = re.compile(
    r"^[~～〜≈约×xX]*\s*([0-9]+(?:\.[0-9]+)?)\s*(?:x|×|倍|倍率)?\s*$", re.IGNORECASE)


def _rate(value: Any) -> float | None:
    """倍率 → float；`"0.12x"` / `"0.75倍"` / `"×1.6"` / `0.5` 都能取。"""
    if isinstance(value, str):
        match = _RATE_PATTERN.match(value.strip())
        return float(match.group(1)) if match else None
    return _num(value)


def _num(value: Any) -> float | None:
    """宽松取数（上游积分可能是 int / float / 字符串）。bool 不算。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None
