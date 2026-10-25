"""CodeBuddy 上游客户端：聊天流、额度探测、凭证规范化。

范围：bearer-only 手动凭证 + 聊天 + 个人版额度；OAuth 轮询、企业额度、
多账号切换未接入。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import time
import weakref
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .activity import ActivityResult
    from .checkin import CheckinResult

import httpx

from ...engine.sse import iter_frames
from ...provider import base
from ...provider.base import ErrKind, Event, GrowthResult, Model, Quota
from ...tasks.pacer import paced
from ..lazy_clients import LazyClients
from . import events as cb_events
from .credential import CodeBuddyCredential, parse_credential
from .events import UpstreamProtocolViolation
from .headers import (
    CN_ENDPOINT,
    EP_CHAT,
    EP_ENTERPRISE_USAGE,
    EP_USER_RESOURCE,
    QUOTA_RANGE_END,
    generate_headers,
    host_of,
)

# 写超时覆盖的是上传请求体阶段，不是首字等待：大上下文请求体可达数 MB，
# 经代理链路 10s 会稳定超时；首字等待由 read=None +
# stream_first_event_timeout_seconds 负责，两者不要混为一谈。


DEFAULT_MODELS: tuple[str, ...] = ("glm-5.2", "deepseek-v4-pro")
EP_CONFIG = "/v3/config"
MODEL_CACHE_TTL_SECONDS = 600
# 模型缓存条目上限（LOW）：key 含 token 前缀，凭证轮换后旧条目永不命中。
MODEL_CACHE_MAX_ENTRIES = 256

# ---------------------------------------------------------------- 模型可用性
# 国际站 `/v3/config` 返回**全部**模型，但账号套餐决定能否真正调用：
# 免费号请求 gpt-5.5 / gpt-5.6-luna 等模型时上游回 400 / 11158
# `model [...] is not allowed for user type [personal_free_user]`。
# 与 Zen 的免费层一样，列表只保留**实际探活通过**的模型；差异是这里没有
# 「免费/付费」后缀可收窄，必须逐个真发一次请求。
#
# 判活只认两个业务码：11158（套餐不允许）与 11102（后端无此模型）。429 /
# 5xx / 超时 / 认证失败一律视为 **inconclusive** 并保留模型——把限流误判成
# 「模型不存在」会让列表随上游抖动闪没，代价比多显示一个模型大得多。
MODEL_ACCESS_CACHE_TTL_SECONDS = 6 * 3600
MODEL_ACCESS_CACHE_MAX_ENTRIES = 256
MODEL_ACCESS_PROBE_TIMEOUT = 5.0
MODEL_ACCESS_PROBE_CONCURRENCY = 4
MODEL_ACCESS_DENIED_CODES = frozenset({11102, 11158})
_MODEL_ACCESS_CACHE: dict[tuple[str, str], tuple[float, dict[str, bool | None]]] = {}
_PROBE_PROMPT = "hi"

logger = logging.getLogger(__name__)

# 上游内容风控（11128）已确认的指纹串：出现在 system/assistant 消息正文
# （含行中）即整单拒绝。清单来自 2026-09 对真实上游的确定性复现实验。
CHANNEL_MARKERS: tuple[str, ...] = (
    "You are Claude Code, Anthropic's official CLI for Claude.",
    "x-anthropic-billing-header",
    "Main branch (you will usually use this for PRs):",
)
_CHANNEL_MARKER_STANDIN = "[external-client-identity]"

# 缺省首条 system 的占位正文。国际站网关**硬性要求** messages[0] 是
# system prompt，否则整单 400 / 11128 `first message is not system prompt`
# （2026-10-12 用真实国际站凭证实测：无 system → 400，同请求前置下面这句
# → 200）。国内站（copilot.tencent.com）无此要求，存量凭证不带 system 也能
# 正常出站，所以只在 `require_leading_system=True` 时注入。
# 正文刻意取中性最小句：不带任何厂商指纹（否则撞 11128 内容风控），
# 不覆盖客户端自带的 system（已有则不注入，见 `_ensure_leading_system`）。
DEFAULT_LEADING_SYSTEM = "You are a helpful assistant."


def ensure_leading_system(body: dict[str, Any]) -> bool:
    """messages[0] 非 system 时补一条占位 system，返回是否真的补了。

    只在 messages 是非空列表、且首条确实是 dict 时补；首条已是 system
    （客户端自带）则原样返回 False——不覆盖、不合并、不重排，原有 system
    正文与位置都是客户端的意图。
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    first = messages[0]
    if not isinstance(first, dict) or first.get("role") == "system":
        return False
    messages.insert(0, {"role": "system", "content": DEFAULT_LEADING_SYSTEM})
    return True



def _clean_history_tool_calls(body: dict[str, Any]) -> None:
    """清理历史消息中的脏 tool_call（如 PI 会话里残留的空名调用）：
    剔除无 function/name 的条目；清空后 content 为空的 assistant 占位
    整条丢弃；悬空的 role=tool 结果消息成对清理。"""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    rewritten = []
    orphan_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            rewritten.append(message)
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            kept = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    orphan_ids.add(str(tc.get("id")))
                    continue
                if not str(fn.get("name") or "").strip():
                    orphan_ids.add(str(tc.get("id")))
                    continue
                kept.append(tc)
            if kept:
                message["tool_calls"] = kept
            else:
                message.pop("tool_calls", None)
                if message.get("content") is None:
                    continue
        rewritten.append(message)
    body["messages"] = [
        m for m in rewritten
        if not (isinstance(m, dict) and m.get("role") == "tool"
                and str(m.get("tool_call_id")) in orphan_ids)
    ]


def _neutralize_text(text: str) -> tuple[str, int]:
    """单段正文的中和：返回（替换后文本, 命中处数）。"""
    hits = 0
    for marker in CHANNEL_MARKERS:
        if marker in text:
            hits += text.count(marker)
            text = text.replace(marker, _CHANNEL_MARKER_STANDIN)
    return text, hits


def sanitize_channel_markers(body: dict[str, Any]) -> int:
    """中和出站 system/assistant 正文里的「伪装其他厂商官方客户端」指纹。

    上游内容风控（11128 Illegal API invocation）对该类指纹整单拒绝：
    与凭证无关（换号无效）、确定性复现、user/tool 角色 / tool_calls 参数 /
    reasoning 均不触发，仅 system/assistant 的 content 命中（2026-09 实测，
    见 TECHNICAL.md §3.2）。content 为文本块列表（{"type": "text", ...}）
    时逐块处理——块形态指纹同样触发（2026-09-21 直证）。只改写出站副本，
    客户端会话历史保持原样。返回替换处数（用于日志与测试）。
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    replaced_total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") not in ("system", "assistant"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            replaced, hits = _neutralize_text(content)
            if hits:
                replaced_total += hits
                message["content"] = replaced
        elif isinstance(content, list):
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "text"
                        and isinstance(block.get("text"), str)):
                    continue
                replaced, hits = _neutralize_text(block["text"])
                if hits:
                    replaced_total += hits
                    block["text"] = replaced
    return replaced_total


def build_headers(credential: CodeBuddyCredential, endpoint: str, *,
                  quota_only: bool = False) -> dict[str, str]:
    """头构造统一入口：X-Domain 与 Host 由同一 endpoint 派生。"""
    return generate_headers(
        endpoint=endpoint, bearer_token=credential.bearer_token,
        user_id=credential.user_id or None, account_uid=credential.account_uid or None,
        domain=credential.domain or None, enterprise_id=credential.enterprise_id or None,
        department_full_name=credential.department_full_name or None, quota_only=quota_only,
    )


class CodeBuddyClient(LazyClients):
    def __init__(
        self,
        *,
        endpoint: str = CN_ENDPOINT,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
        sanitize_markers: bool = True,
        require_leading_system: bool = False,
        proxy: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._stream_client = stream_client
        self._short_client = short_client
        self.sanitize_markers = sanitize_markers
        self.require_leading_system = require_leading_system
        self._proxy = proxy

    # ------------------------------------------------------------- 聊天流

    async def stream_chat(self, credential: CodeBuddyCredential, payload: dict[str, Any],
                          model: str) -> AsyncIterator[Event]:
        """上游只支持流式；非流式由调用方聚合。"""
        body = dict(payload)
        # 出站改写只作用于副本：浅拷贝与请求原文共享 messages 内层 dict，
        # 下面的原地改写（developer→system / 指纹中和 / 脏 tool_call 清理）
        # 会穿透回 request.raw；而会话粘性在改写前后都用请求原文算指纹
        # （pin_for / remember），穿透会让指纹链失配、粘性静默失效
        # （目标场景恰是带 developer 角色的对话）
        body["messages"] = copy.deepcopy(body.get("messages"))
        body["model"] = model
        body["stream"] = True
        _clean_history_tool_calls(body)
        # PI 等客户端对 reasoning 模型会把 system 转成 OpenAI 的 developer
        # 角色；腾讯后端不认 developer，实测直接判 11128 渠道风控 → 归一为 system
        messages = body.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "developer":
                    message["role"] = "system"
        # 必须在 developer→system 归一**之后**判首条：客户端带 developer 时
        # 归一已把它变成 system，再注入就成了重复首条。国际站网关硬性要求
        # 首条为 system（见 DEFAULT_LEADING_SYSTEM 注释），国内站不注入。
        if self.require_leading_system:
            ensure_leading_system(body)
        # 会话正文里的伪装客户端指纹同样触发 11128 内容风控（换号无效），
        # 出站前中和；只指出现在 system/assistant 的 content
        if self.sanitize_markers:
            sanitized = sanitize_channel_markers(body)
            if sanitized:
                logging.getLogger(__name__).warning(
                    "出站消息命中伪装客户端指纹，已中和 %s 处（上游 11128 内容风控）",
                    sanitized)
        # 官方 CLI 请求的标准特征字段；缺失会被渠道风控判定非官方调用（11128）
        body.setdefault("enable_thinking", True)
        # 缺 reasoning_effort 时上游退化成「可见推演」：思考整段写进
        # delta.content、reasoning_content 恒空、usage 的 reasoning_tokens 恒 0
        # （非官方 CLI 客户端不发该字段，官方 CLI 69/71 份真实 dump 都带）。
        # 补缺省即恢复独立思考通道；客户端显式值不覆盖——与参考实现
        # codebuddy2api「白名单模型强制 max」不同，那是改写客户端意图。
        body.setdefault("reasoning_effort", "medium")
        stream_options = body.get("stream_options")
        body["stream_options"] = {
            **(stream_options if isinstance(stream_options, dict) else {}),
            "include_usage": True,
        }
        url = f"{self.endpoint}{EP_CHAT}"
        async with self._stream.stream(
            "POST", url, json=body, headers=build_headers(credential, self.endpoint),
        ) as response:
            if response.status_code >= 400:
                raw = await base.read_body_bounded(response)
                # body 兜底：国际站 429+6004 只把重置时刻写在正文（`reset at
                # … UTC+8`），不给 Retry-After 头；不传 body 就只能回落 600s
                # 基数，到期重试再撞一次 429（实测同一号一天被爆 42 次）。
                raise UpstreamHTTPError(response.status_code, raw,
                                        **base.retry_hints(response.headers,
                                                           body=raw))
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
        # 不带 ProductCode：上游对部分账号已拒绝 "codebuddy" 产品码
        # （InvalidParameterValue: productCode:param format error），而缺省时
        # 会返回该账号全部套餐（实测与官方后台口径一致）。
        now = time.localtime()
        payload = {
            "PageNumber": 1,
            "PageSize": 200,
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": time.strftime("%Y-%m-%d %H:%M:%S", now),
            "PackageEndTimeRangeEnd": QUOTA_RANGE_END,
        }
        body = await self._post_json(f"{self.endpoint}{EP_USER_RESOURCE}", payload,
                                     credential, quota_only=True)
        accounts = _extract_accounts(body)
        now_epoch = time.time()
        total = 0.0
        remaining = 0.0
        cycle_end: int | None = None
        ladder: list[tuple[int, float]] = []
        packages: list[dict[str, Any]] = []
        for account in accounts:
            if not isinstance(account, dict) or account.get("Status") != 0:
                continue
            package_total = _cycle_capacity(account, "CycleCapacitySize")
            if package_total <= 0:
                continue
            package_remaining = _cycle_capacity(account, "CycleCapacityRemain")
            total += package_total
            remaining += package_remaining
            end = _cycle_end_epoch(account.get("CycleEndTime"))
            # 展示明细：比调度阶梯（下面的 ladder）宽——已用完但仍有效的包
            # 也要显示。但「已过期且已用完」的包既不携带积分也不可能再被
            # 消耗，只是上游的历史残留，不堆进弹层。
            if package_remaining > 0 or (end is not None and end > now_epoch):
                packages.append({
                    "name": str(account.get("PackageName") or ""),
                    "total": package_total,
                    "used": max(0.0, package_total - package_remaining),
                    "end": end,
                })
            # 一个账号常有几十个套餐各自独立到期（每日 100 积分 × N）。
            # 上游会把已过期套餐一起返回，所以 end > now 才有效；已用完的包
            # （剩余 0）不携带积分。两类都不进入到期排序。
            if end is None or end <= now_epoch or package_remaining <= 0:
                continue
            cycle_end = end if cycle_end is None else min(cycle_end, end)
            ladder.append((end, package_remaining))
        # remaining 一律取逐包 `CycleCapacityRemain`（Precise 优先）之和，**不**
        # 用响应顶层的 `TotalDosage`。实测（2026-09-28，三个真实账号）：
        #   TotalDosage = Σ(size − CapacityUsed)，而 CapacityUsed 是「上周期」
        #   口径——本周期消耗记在 CycleCapacityUsed/CycleCapacityRemain，故
        #   TotalDosage 在一整个周期内不变，无法反映实时余额（曾出现
        #   remaining == total、健康度恒 100、credit_events 不再记录）。
        #   它与本函数已算出的逐包 remaining 基准不同，会与 expiry_ladder /
        #   quota_packages / 管理台明细自相矛盾。PROPOSAL §3.3 的口径也是
        #   CycleCapacityRemainPrecise。
        return Quota(remaining=remaining, total=total, cycle_end=cycle_end,
                     expiry_ladder=ladder, packages=packages or None,
                     probed_at=int(time.time()))

    async def _fetch_enterprise_quota(self, credential: CodeBuddyCredential) -> Quota:
        data = await self._post_json(f"{self.endpoint}{EP_ENTERPRISE_USAGE}", {}, credential)
        used = _number(data.get("credit"))
        total = _number(data.get("limitNum"))
        if total is None:
            raise UpstreamProtocolViolation("enterprise quota missing limitNum")
        return Quota(remaining=max(0.0, total - (used or 0.0)), total=total,
                     probed_at=int(time.time()))

    async def fetch_models(self, credential: CodeBuddyCredential) -> list[Model]:
        """动态拉取模型列表（/v3/config），带 10 分钟缓存。失败抛错由调用方兜底。"""
        cache_key = (credential.bearer_token[:64], self.endpoint)
        cached = _MODEL_CACHE.get(cache_key)
        now = time.time()
        if cached and now - cached[0] < MODEL_CACHE_TTL_SECONDS:
            return cached[1]

        headers = build_headers(credential, self.endpoint)
        host = host_of(self.endpoint)
        headers.update({
            "Host": host,
            "X-Domain": host,
            "Accept": "application/json",
            "X-IDE-Type": "CodeBuddyIDE",
            "X-IDE-Name": "CodeBuddyIDE",
            "X-IDE-Version": CODEBUDDY_IDE_VERSION,
            "X-Product-Version": CODEBUDDY_IDE_VERSION,
        })
        response = await self._short.get(f"{self.endpoint}{EP_CONFIG}", headers=headers)
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            body = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation("config response is not JSON") from error
        if not isinstance(body, dict):
            raise UpstreamProtocolViolation("config response is not an object")
        if body.get("code") != 0:
            raise UpstreamProtocolViolation(
                f"config rejected with code {body.get('code')!r}")

        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise UpstreamProtocolViolation("config response missing models list")
        models: list[Model] = []
        seen: set[str] = set()
        for item in data["models"]:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id", "")).strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            models.append(Model(
                id=model_id,
                name=str(item.get("name") or ""),
                credit_rate=_parse_credit_rate(item.get("credits")),
                max_input_tokens=_int_field(item.get("maxInputTokens")),
                max_output_tokens=_int_field(item.get("maxOutputTokens")),
                supports_images=_bool_field(item.get("supportsImages")),
                supports_tool_call=_bool_field(item.get("supportsToolCall")),
                supports_reasoning=_bool_field(item.get("supportsReasoning")),
                default_effort=_effort_field(item.get("reasoning")),
            ))
        if not models:
            raise UpstreamProtocolViolation("config returned no valid model ids")
        # 容量上限（LOW）：key 含 token 前缀，凭证轮换/删除后旧条目永不命中，
        # 无界增长会长期占内存。超过上限时先清掉过期条目，仍超再整体清空
        # （模型表是可重建的缓存，宁可多拉一次也不留无界字典）。
        if len(_MODEL_CACHE) >= MODEL_CACHE_MAX_ENTRIES:
            cutoff = now - MODEL_CACHE_TTL_SECONDS
            for key in [k for k, v in _MODEL_CACHE.items() if v[0] < cutoff]:
                del _MODEL_CACHE[key]
            if len(_MODEL_CACHE) >= MODEL_CACHE_MAX_ENTRIES:
                _MODEL_CACHE.clear()
        _MODEL_CACHE[cache_key] = (now, models)
        return models

    async def filter_available_models(
            self, credential: CodeBuddyCredential,
            models: list[Model]) -> list[Model]:
        """只保留当前账号**实际可用**的模型（国际站套餐门禁）。

        上游 `/v3/config` 返回全量模型，套餐限制只在真正调用时以
        400 / 11158 暴露。这里像 Zen 免费层一样逐个探活，但判据更保守：
        只有 11158 / 11102 才判不可用，限流 / 超时 / 5xx 都保留模型。
        探活结果按账号缓存 6 小时——套餐变化很慢，而每轮 20 个模型全探
        一次，不值得按 5 分钟的列表 TTL 重跑。
        """
        if not models or not credential.bearer_token:
            return models
        cache_key = self._model_access_cache_key(credential)
        now = time.time()
        cached = _MODEL_ACCESS_CACHE.get(cache_key)
        if cached and now - cached[0] < MODEL_ACCESS_CACHE_TTL_SECONDS:
            access = cached[1]
        else:
            access = await self._probe_model_accesses(
                credential, [model.id for model in models])
            self._remember_model_access(cache_key, now, access)
        kept = [model for model in models if access.get(model.id) is not False]
        if not kept:
            # 全被拒绝多半是探活整体异常或账号刚降级：保留全量，避免渠道
            # 从列表里凭空消失（调用时仍会由上游给出准确错误）。
            logger.warning("CodeBuddy 模型可用性探活全部失败，回退全量列表")
            return models
        return kept

    def _model_access_cache_key(
            self, credential: CodeBuddyCredential) -> tuple[str, str]:
        """可用性缓存按**账号**隔离，不能按 token 前缀。

        同一 realm 签发的 access token 前 64 字符完全相同（JWT 头 + payload
        开头），按前缀取 key 会让付费号与免费号共享同一份探活结果——正是
        最需要区分套餐的场景。OAuth 凭证有稳定 nickname，直接用它；手动
        凭证无昵称时回退到整 token 的 SHA-256，既不碰撞也不把明文 token
        当 key 长期挂在模块字典里。
        """
        identity = credential.nickname or hashlib.sha256(
            credential.bearer_token.encode("utf-8")).hexdigest()
        return identity, self.endpoint

    async def _probe_model_accesses(
            self, credential: CodeBuddyCredential,
            model_ids: list[str]) -> dict[str, bool | None]:
        """并发探活一批模型；返回值 None 表示不确定（保留该模型）。"""
        semaphore = asyncio.Semaphore(MODEL_ACCESS_PROBE_CONCURRENCY)

        async def one(model_id: str) -> tuple[str, bool | None]:
            async with semaphore:
                return model_id, await self._probe_model_access(credential, model_id)

        return dict(await asyncio.gather(*(one(model_id) for model_id in model_ids)))

    async def _probe_model_access(
            self, credential: CodeBuddyCredential, model_id: str) -> bool | None:
        """发一次最小流式请求，只看响应状态行就断开。

        200 说明套餐允许；11158 / 11102 说明模型不可用。其余状态码与网络
        异常一律返回 None——不能把限流或上游故障当成「模型不存在」。
        """
        body = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": DEFAULT_LEADING_SYSTEM},
                {"role": "user", "content": _PROBE_PROMPT},
            ],
            "max_tokens": 1,
            "stream": True,
            "enable_thinking": True,
            "reasoning_effort": "medium",
            "stream_options": {"include_usage": True},
        }
        try:
            async with asyncio.timeout(MODEL_ACCESS_PROBE_TIMEOUT):
                async with self._short.stream(
                        "POST", f"{self.endpoint}{EP_CHAT}", json=body,
                        headers=build_headers(credential, self.endpoint)) as response:
                    if response.status_code < 400:
                        return True
                    raw = await base.read_body_bounded(response)
        except (httpx.HTTPError, TimeoutError):
            return None
        codes = base.business_codes(raw.decode("utf-8", errors="replace"))
        return False if codes & MODEL_ACCESS_DENIED_CODES else None

    @staticmethod
    def _remember_model_access(cache_key: tuple[str, str], now: float,
                               access: dict[str, bool | None]) -> None:
        """写入探活缓存，并按模型缓存同样的策略限制条目数。"""
        if len(_MODEL_ACCESS_CACHE) >= MODEL_ACCESS_CACHE_MAX_ENTRIES:
            cutoff = now - MODEL_ACCESS_CACHE_TTL_SECONDS
            for key in [k for k, value in _MODEL_ACCESS_CACHE.items()
                        if value[0] < cutoff]:
                del _MODEL_ACCESS_CACHE[key]
            if len(_MODEL_ACCESS_CACHE) >= MODEL_ACCESS_CACHE_MAX_ENTRIES:
                _MODEL_ACCESS_CACHE.clear()
        _MODEL_ACCESS_CACHE[cache_key] = (now, access)

    async def _post_json(self, url: str, payload: dict[str, Any],
                         credential: CodeBuddyCredential, *,
                         quota_only: bool = False) -> dict[str, Any]:
        headers = build_headers(credential, self.endpoint, quota_only=quota_only)
        response = await self._short.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            data = response.json()
        except ValueError as error:
            raise UpstreamProtocolViolation(
                f"non-JSON response from {url}",
                public="upstream returned a non-JSON response") from error
        if not isinstance(data, dict):
            raise UpstreamProtocolViolation(
                f"unexpected response shape from {url}",
                public="upstream returned an unexpected response shape")
        return data


def _parse_credit_rate(value: object) -> float | None:
    """"credits": "x0.29 credits" → 0.29；格式不符返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("x"):
        return None
    try:
        return float(text[1:].split()[0])
    except (ValueError, IndexError):
        return None


def _int_field(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _bool_field(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _effort_field(value: object) -> str | None:
    """CB 模型条目的 `reasoning` 对象 → 默认档位（实测只有 effort 字段）。

    上游给 `{"effort": "high"|"medium", "summary": "auto"}`；缺失或类型不符
    返回 None（透传 None 比编造档位诚实）。
    """
    if not isinstance(value, dict):
        return None
    effort = value.get("effort")
    return effort if isinstance(effort, str) and effort else None


def _extract_accounts(body: dict[str, Any]) -> list[Any]:
    """从上游响应里取出账户列表。

    真实结构是三层嵌套：``data.Response.Data.Accounts``。
    只读 ``data.Accounts`` 会把有效凭证误判成「未探测到额度」。
    上游历史上出现过不同层级，因此按已知路径依次尝试，最后才失败。
    """
    data = body.get("data")
    if not isinstance(data, dict):
        raise UpstreamProtocolViolation("quota response missing data")
    # 业务层错误（如参数被上游拒绝）与「没有额度」必须区分：前者抛探测
    # 失败，否则会被当成 total=0，把还有积分的凭证误标成「已耗尽」。
    response = data.get("Response")
    error = response.get("Error") if isinstance(response, dict) else None
    if isinstance(error, dict) and error.get("Code"):
        raise UpstreamProtocolViolation(
            f"quota response error {error.get('Code')}: "
            f"{str(error.get('Message') or '').strip()}")
    for path in (
        ("Response", "Data", "Accounts"),   # 实测结构
        ("Response", "Accounts"),
        ("Data", "Accounts"),
        ("Accounts",),                       # 兼容更早的扁平结构
    ):
        node: Any = data
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, list):
            return node
        if node is None and path == ("Response", "Data", "Accounts"):
            # 该路径存在但值为 null → 探测成功但无个人版额度
            holder: Any = data
            for key in path[:-1]:
                holder = holder.get(key) if isinstance(holder, dict) else None
            if isinstance(holder, dict) and path[-1] in holder:
                return []
    raise UpstreamProtocolViolation("quota response missing Accounts")


def _cycle_capacity(account: dict[str, Any], field: str) -> float:
    """优先 *Precise 字段（AGENTS.md：额度以 Precise 为准）。

    上游把 Precise 值返回成**字符串**（"500"），因此必须能解析数字字符串，
    否则整批额度都会被算成 0。
    """
    precise = account.get(f"{field}Precise")
    value = precise if precise is not None else account.get(field)
    return _number(value) or 0.0


def _number(value: Any) -> float | None:
    """接受 int/float 以及上游返回的数字字符串（Precise 字段是字符串）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _cycle_end_epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(value, fmt)))
        except ValueError:
            continue
    return None


# 任务客户端缓存：弱引用挂 client——用 id(client) 作键时，client 被 GC 后
# id 复用会让新实例命中旧缓存对象（旧连接池/endpoint），且未走 aclose 的
# client 会在缓存里永远驻留
_CHECKIN_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_REFRESH_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_GROWTH_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_ACTIVITY_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _cached_checkin(client: CodeBuddyClient):
    from .checkin import CodeBuddyCheckin

    cached = _CHECKIN_CACHE.get(client)
    if cached is None:
        cached = CodeBuddyCheckin(client.endpoint, client=client._short,
                                     proxy=client._proxy)
        _CHECKIN_CACHE[client] = cached
    return cached


def _cached_growth(client: CodeBuddyClient):
    from .growth import CodeBuddyGrowth

    cached = _GROWTH_CACHE.get(client)
    if cached is None:
        cached = CodeBuddyGrowth(client.endpoint, client=client._short,
                                     proxy=client._proxy)
        _GROWTH_CACHE[client] = cached
    return cached


def _cached_activity(client: CodeBuddyClient):
    from .activity import CodeBuddyActivity

    cached = _ACTIVITY_CACHE.get(client)
    if cached is None:
        cached = CodeBuddyActivity(client.endpoint, client=client._short,
                                     proxy=client._proxy)
        _ACTIVITY_CACHE[client] = cached
    return cached


def _cached_refresh(client: CodeBuddyClient):
    from .refresh import CodeBuddyRefresh

    cached = _REFRESH_CACHE.get(client)
    if cached is None:
        cached = CodeBuddyRefresh(client.endpoint, client=client._short,
                                     proxy=client._proxy)
        _REFRESH_CACHE[client] = cached
    return cached


_MODEL_CACHE: dict[tuple[str, str], tuple[float, list[Model]]] = {}
CODEBUDDY_IDE_VERSION = "1.42.0"


def _pacer_key(provider_id: str, credential: CodeBuddyCredential) -> str:
    """聊天节流桶键：账号身份优先，取不到时退到 token（同账号多凭证共桶）。

    provider_id 参与键构造：国内站与国际站是两条独立渠道，各自持有独立
    节流器，但仍按渠道 id 分桶，避免将来共用 Pacer 时两站账号互相占位。
    """
    from ...tasks.pacer import stable_key

    return stable_key(provider_id, credential.account_uid or credential.user_id
                      or credential.bearer_token)


class UpstreamHTTPError(base.UpstreamHTTPError):
    """CodeBuddy 上游非 2xx；kind() 走 CB 的 1005/400 规则。"""

    classify_status = staticmethod(cb_events.classify_status)


@dataclass(slots=True)
class CodeBuddyProvider:
    """Provider 协议实现（bearer-only + 聊天 + 个人额度）。

    pacer：聊天请求节流器（腾讯频率风控对策）；None 表示不限速。
    """

    client: CodeBuddyClient = field(default_factory=CodeBuddyClient)
    pacer: Any | None = None

    id: str = "codebuddy"

    def import_credential(self, raw: dict) -> dict:
        return parse_credential(raw, auth_source="manual").to_dict()

    def classify(self, status: int, body: bytes) -> ErrKind:
        return cb_events.classify_status(status, body)

    async def probe_quota(self, credential_data: dict) -> Quota:
        return await self.client.fetch_quota(CodeBuddyCredential.from_dict(credential_data))

    async def list_models(self, credential_data: dict) -> list[Model]:
        """动态拉取上游模型；失败回退静态表（保证 Play grounds/客户端始终有列表）。"""
        try:
            return await self.client.fetch_models(
                CodeBuddyCredential.from_dict(credential_data))
        except Exception as error:  # noqa: BLE001 - 兜底不是静默：错误会带上抛路径
            import logging

            logging.getLogger(__name__).warning("动态模型拉取失败，回退静态表: %s", error)
            return [Model(id=mid) for mid in DEFAULT_MODELS]

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        credential = CodeBuddyCredential.from_dict(credential_data)
        key = _pacer_key(self.id, credential)
        async with paced(self.pacer, key):
            async for event in self.client.stream_chat(credential, payload, model):
                yield event

    def host(self) -> str:
        return host_of(self.client.endpoint)

    # --------------------------------------------------------------

    async def aclose(self) -> None:
        """释放内部 HTTP 连接池与 OAuth 客户端。"""
        await self.client.aclose()
        for cached in (_CHECKIN_CACHE.pop(self.client, None),
                       _REFRESH_CACHE.pop(self.client, None),
                       _GROWTH_CACHE.pop(self.client, None),
                       _ACTIVITY_CACHE.pop(self.client, None)):
            closer = getattr(cached, "aclose", None)
            if callable(closer):
                await closer()

    def credential_from(self, credential_data: dict) -> CodeBuddyCredential:
        return CodeBuddyCredential.from_dict(credential_data)

    async def checkin(self, credential_data: dict) -> CheckinResult:  # noqa: F821

        client = _cached_checkin(self.client)
        credential = CodeBuddyCredential.from_dict(credential_data)
        result = await client.claim(credential)
        # status 是 provider 私有 dict（CheckinStatus.to_dict）；中立层只透传
        if result.status is not None:
            result.status = result.status.to_dict()
        return result

    async def checkin_status(self, credential_data: dict) -> dict:
        """仅查签到状态（管理台展示连续天数，不产生任何写入）。"""
        client = _cached_checkin(self.client)
        credential = CodeBuddyCredential.from_dict(credential_data)
        status = await client.fetch_status(credential)
        return status.to_dict()

    async def growth(self, credential_data: dict, *,
                     allow_irreversible: bool = True) -> GrowthResult:
        """成长中心一轮：领礼物 / 派 Buddy / 任务 / 补登 / 连登兑换 / 抽奖 / Buddy 盲盒。

        allow_irreversible=False 时跳过不可逆动作（抽奖/兑换/开盲盒/补登卡消耗），
        只做可逆的查询与领取（礼物、任务奖）——给「只想保守跑」的部署留开关。
        """
        from .growth_runner import GrowthRunner

        credential = CodeBuddyCredential.from_dict(credential_data)
        runner = GrowthRunner(_cached_growth(self.client),
                              allow_irreversible=allow_irreversible)
        return await runner.run(credential)

    async def activity(self, credential_data: dict) -> ActivityResult:
        """活跃上报一条 chat_request_send（可选，默认关闭）。

        只发一条：与官方「单日一次对话」等价。userId 必填，缺失直接判失败。
        """
        from .activity import resolve_user_id

        credential = CodeBuddyCredential.from_dict(credential_data)
        if not resolve_user_id(credential):
            from .activity import ActivityResult

            return ActivityResult(ok=False,
                                  message="无法确定账号 userId（凭证与 token 均无）")
        client = _cached_activity(self.client)
        return await client.report_chat(credential)

    def activity_scope(self, credential_data: dict) -> str:
        """活跃上报隔离键：endpoint + userId（回落 JWT sub）。

        与 checkin_scope 不同：签到用 account_uid/user_id（本网关实测为空，
        会回落到 credential_id），而上报的 userId 可从 JWT sub 取到，故同账号
        多凭证能正确共享「每天一次」。身份都取不到时返回空串，由调用方回落到
        credential_id（最坏多报一次，上游按天去重）。
        """
        from .activity import resolve_user_id

        credential = CodeBuddyCredential.from_dict(credential_data)
        user_id = resolve_user_id(credential)
        if not user_id:
            return ""
        return f"{self.client.endpoint.rstrip('/')}|{user_id}"

    def checkin_scope(self, credential_data: dict) -> str:
        """签到隔离键：endpoint + X-User-Id；身份未知时返回空串（调用方回落到凭证 ID）。

        OAuth 路径拿不到 account_uid / user_id 时（上游账号接口未回填，实测发生），
        返回空串比返回 "endpoint|" 安全：后者会让所有 CB 凭证算出同一个 scope，
        签到任务的 seen 集合只跑第一个账号。
        """
        from .checkin import checkin_scope_key

        credential = CodeBuddyCredential.from_dict(credential_data)
        return checkin_scope_key(self.client.endpoint,
                                 credential.account_uid or credential.user_id)

    async def refresh(self, credential_data: dict) -> dict:
        """OAuth 凭证刷新；bearer-only 手动凭证直接返回原值（AGENTS.md）。"""

        credential = CodeBuddyCredential.from_dict(credential_data)
        if not credential.needs_refresh(0, now=2**31 - 1) and not credential.is_oauth:
            return credential_data
        client = _cached_refresh(self.client)
        outcome = await client.refresh(credential)
        return outcome.credential.to_dict()

    async def list_accounts(self, credential_data: dict) -> list:

        client = _cached_refresh(self.client)
        return await client.list_accounts(CodeBuddyCredential.from_dict(credential_data))

    async def switch_account(self, credential_data: dict, account_id: str) -> dict:

        client = _cached_refresh(self.client)
        switched = await client.switch_account(
            CodeBuddyCredential.from_dict(credential_data), account_id)
        return switched.to_dict()
