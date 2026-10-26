"""模型可用性主动探活：探「已被负缓存」与「已判死」的 (渠道, 模型)。

**为什么只探这两类**：遍历全量模型表 × 全量凭证要真发几百次对话，耗的是
用户额度。而负缓存（`credential_model_cooldowns.reason='blocked'`，11102「该
后端无此模型」）才是唯一会**静默锁死一个模型 6–24 小时**的状态——探它，就是
回答「这个模型到底回来了没有」，同时也是唯一能让它提前恢复的入口。**判死行**
（`model_probes.state='failed'`）同理必须探：判死会把该 (渠道, 模型) 从
`/v1/models` 与 Playground 里摘掉（见 `api.models.merged_entries`），而隐藏
本身没有到期时间——不探就永远是隐藏的。

**与生产完全隔离**：直连 provider 的 `stream_chat`，不经过 executor，因此
不写 `usage_events`、不动冷却表、不改健康度。探活结论写进独立的
`model_probes` 表；唯一的出口影响是列表展示——`failed` 期间该 (渠道, 模型)
从 `/v1/models` 里摘掉，`ok` 即恢复（见 `api.models.merged_entries`），不影响
选号与凭证健康度。

**三态判定**（对应 `state`）：

* `ok`——拿到内容或正常收尾。清掉该 (渠道, 模型) 的 blocked 负缓存，模型
  立即回到池子里（这就是「恢复入口」）。
* `failed`——上游明确说「没这个模型」（`ErrKind.BLOCKED` / `INVALID`）。保留
  负缓存，累加失败次数，并把该 (渠道, 模型) 从模型列表里暂时摘掉。
* `inconclusive`——额度耗尽、限流、并发打满、网络/超时等。**绝不据此禁用**：
  这些与「上游有没有这个模型」无关，把它们算成失败会让一次余额不足把所有
  模型都判死。

**人工接管**：`manual != ''` 的行整条跳过。管理员点过「恢复」之后不希望后台
又把同一结论纠回去——否则会变成两个系统互相打架。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..db.crypto import CredentialDecryptError
from ..provider.base import ErrKind, EventKind, UpstreamHTTPError, event_error_kind
from . import TaskReport

logger = logging.getLogger(__name__)

# 探活请求：只要上游肯回一个 token 就够证明「这个模型认得」，故正文取最小。
# max_tokens 给 16 而不是 1：部分渠道对 1 有校验下限，而 16 的额度代价可忽略。
PROBE_MAX_TOKENS = 16
PROBE_PROMPT = "ping"
# 单次探活的整体超时（秒）：上游不吐字节时不能把整个任务挂住。
PROBE_TIMEOUT_SECONDS = 60.0

# 判定为「模型本身不可用」的错误分类：其余一律 inconclusive。
_FAILURE_KINDS = frozenset({ErrKind.BLOCKED, ErrKind.INVALID})


def probe_payload() -> dict[str, Any]:
    """最小对话载荷。与 executor 出口同形状（OpenAI chat），由各 provider 改写。"""
    return {
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": True,
    }


class ModelProbeTask:
    """探一轮仍被负缓存或已判死的 (渠道, 模型)，成功即清负缓存并恢复展示。"""

    def __init__(self, credentials, providers: dict, probes, *,
                 pacer=None, timeout: float = PROBE_TIMEOUT_SECONDS,
                 upstream_model=None) -> None:
        self._credentials = credentials
        self._providers = providers
        self._probes = probes
        self._pacer = pacer
        self._timeout = timeout
        # (渠道, 归一名) → 上游原始名。负缓存里存的通常已是上游名，映射交给
        # 调用方（None = 原样传），避免 tasks 层硬编码命名规则。
        self._upstream_model = upstream_model

    async def run_once(self) -> TaskReport:
        report = TaskReport()
        manual = self._probes.manual_marked()
        seen: set[tuple[str, str]] = set()
        for row in self._targets():
            key = (row["provider"], row["model"])
            if key in manual or key in seen:
                # 同一 (渠道, 模型) 可能挂着多个号的负缓存，只需探一次：结论
                # 是模型级的，清的时候也按渠道整片清。
                continue
            seen.add(key)
            provider = self._providers.get(row["provider"])
            data = self._probe_data(row["provider"])
            if provider is None or data is None:
                report.skipped += 1
                continue
            report.attempted += 1
            state, error = await self._probe_one(
                provider, data, self._model_name(row["provider"], row["model"]))
            if state == "ok":
                report.succeeded += 1
                self._credentials.clear_blocked(provider=row["provider"],
                                                model=row["model"])
            elif state == "failed":
                report.failed += 1
            else:
                report.skipped += 1
            self._probes.record(provider=row["provider"], model=row["model"],
                                state=state, error=error)
        return report

    def _targets(self) -> list[dict[str, Any]]:
        """本轮要探的 (渠道, 模型)：负缓存行 + 已判死行（去重）。

        判死行必须一起探：判死即从列表摘掉且**没有到期时间**，唯一的恢复入口
        就是复探成功。顺序上先负缓存行、再判死行——判死行多半也挂着负缓存，
        去重后不会重复探；两类都去重后同一对只探一次。
        """
        targets: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in self._credentials.blocked_cooldowns():
            key = (row["provider"], row["model"])
            if key not in seen:
                seen.add(key)
                targets.append({"provider": row["provider"], "model": row["model"]})
        for row in self._probes.failed_rows():
            key = (row["provider"], row["model"])
            if key not in seen:
                seen.add(key)
                targets.append(row)
        return targets

    async def probe_now(self, provider_id: str, model: str) -> tuple[str, str]:
        """立即探一个 (渠道, 模型)（管理台「立即探活」/ 人工恢复前复核）。

        与周期探活同一套判定，唯一差别是不看人工接管标记——按钮就是人来点的，
        再拦一次只会让按钮点了没反应。成功同样清负缓存。
        """
        provider = self._providers.get(provider_id)
        data = self._probe_data(provider_id)
        if provider is None or data is None:
            return ("inconclusive", "渠道未注册或无可用凭证")
        state, error = await self._probe_one(
            provider, data, self._model_name(provider_id, model))
        if state == "ok":
            self._credentials.clear_blocked(provider=provider_id, model=model)
        self._probes.record(provider=provider_id, model=model, state=state, error=error)
        return state, error

    def _model_name(self, provider: str, model: str) -> str:
        if self._upstream_model is None:
            return model
        return self._upstream_model(provider, model)

    def _probe_data(self, provider: str) -> dict[str, Any] | None:
        """取一个凭证的明文：优先 selectable，全部不可选时退回任意一条。

        退回是必要的：负缓存常在凭证冷却期间产生，此时所有号都不可选，而探活
        恰恰应该趁「服务空窗」先确认模型回来了没有。凭证本身失效只会得到
        inconclusive，不会误判模型。
        """
        for candidates in (self._credentials.candidates([provider], selectable_only=True),
                           self._credentials.candidates([provider])):
            for candidate in candidates:
                # 解不开（APP_SECRET 换过 / 密文损坏）→ 跳过这条凭证：
                # 一条坏凭证不该让整个探活任务失败。
                try:
                    data = self._credentials.credential_data(candidate.credential_id)
                except CredentialDecryptError:
                    logger.error("探活跳过 %s：凭证解密失败（APP_SECRET 是否被更换过？）",
                                 candidate.credential_id)
                    continue
                if data is not None:
                    return data
        return None

    async def _probe_one(self, provider, data: dict[str, Any],
                         model: str) -> tuple[str, str]:
        """发一次最小对话 → (`ok`|`failed`|`inconclusive`, 错误摘要)。"""
        if self._pacer is not None:
            await self._pacer.wait_turn()
        try:
            async with asyncio.timeout(self._timeout):
                async for event in provider.stream_chat(data, probe_payload(), model):
                    if event.kind is EventKind.ERROR:
                        kind = event_error_kind(event)
                        detail = event.error_message or f"error {event.error_code}"
                        return ("failed" if kind in _FAILURE_KINDS else "inconclusive",
                                _trim(detail))
                    if event.kind in (EventKind.CONTENT, EventKind.FINISH):
                        return ("ok", "")
        except UpstreamHTTPError as error:
            state = "failed" if error.kind() in _FAILURE_KINDS else "inconclusive"
            return (state, _trim(f"HTTP {error.status}: {error}"))
        except TimeoutError:
            return ("inconclusive", f"探活超时（{self._timeout:.0f}s）")
        except Exception as error:  # noqa: BLE001 - 任何异常都不能打挂任务循环
            logger.warning("模型探活异常（%s / %s）：%s", provider.id, model, error)
            return ("inconclusive", _trim(f"{type(error).__name__}: {error}"))
        # 流正常结束但一个有效事件都没出：上游静默失败（TRAE 的 3003 就是这种），
        # 既不能算成功，也不能断言模型不存在。
        return ("inconclusive", "上游未返回任何内容")


def _trim(text: str, limit: int = 200) -> str:
    """错误摘要单行化并截断：进 DB 的文本不该带换行、也不该无界。"""
    return " ".join(text.split())[:limit]
