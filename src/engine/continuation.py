"""截断续写（B1.4，按实测收窄）。

触发条件只有一个：上游以 `finish_reason == "length"` 结束本轮流。此时
上游是被输出长度上限截断的，末尾内容不完整，续写能显著改善体验；且该
信号来自上游而非猜测，误判代价为零（不截断就不会触发）。
同凭证续写、累计用量、上限 `max_continues`。

**为什么不按已批准计划做其余三类判据**（2026-09-21 直连上游实测，
36+ 请求，见 TECHNICAL.md §3.4）：
- 「仅 reasoning 无正文」：真实存在，但只在客户端下发 `max_tokens` 时
  出现（实测 glm-5.3-flash 小 max_tokens → content=0 / reasoning>0）。
  生产路径（PI 等）只发 `reasoning_effort`，不发任何 max 键；本网关也
  不做 max 键映射 → 该形态在生产不可达。凭空加启发式会把「模型就是想
  空回」误判成截断。
- 「代码块未闭合」：纯启发式，无任何实测支撑，不做。
- 「空正文无工具调用」：本机 5744 条统计 0 例，无证据。

另：`max_completion_tokens` 被 CB 上游完全忽略，`max_tokens` 才生效
（TRAE 两个键都不生效），这正是 `length` 在生产路径罕见的原因。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..provider.base import Event, EventKind, Usage

# 续写指令：要求直接接着写，不要复述或重来
_CONTINUE_PROMPT = "Output limit reached. Continue exactly where you left off."


def continues(finish_reason: str | None) -> bool:
    """本轮是否需要续写：仅 `length`（上游截断信号）。"""
    return finish_reason == "length"


def extend_payload(payload: dict[str, Any], content: str, reasoning: str
                   ) -> dict[str, Any]:
    """生成本轮请求体：把已产出正文作为 assistant 消息追加到末尾。

    - `content` 与 `reasoning` 都拼接：reasoning 模型（glm 等）正文常在
      reasoning 里，只带 content 会丢失上下文
    - `max_completion_tokens` / `max_tokens` 归零并移除：续写就是要把剩下
      的写完，沿用原上限会再次在同一处截断（死循环）
    - 其余字段（tools / 温度 / reasoning_effort 等）原样保留
    """
    body = dict(payload)
    messages = body.get("messages")
    if isinstance(messages, list):
        body["messages"] = list(messages) + [{
            "role": "assistant",
            "content": content or "",
            **({"reasoning_content": reasoning} if reasoning else {}),
        }, {
            "role": "user",
            "content": _CONTINUE_PROMPT,
        }]
    # 移除输出上限：让本轮把话写完
    body.pop("max_completion_tokens", None)
    body.pop("max_tokens", None)
    return body


class ContinuationStream:
    """把「一轮上游流」包装成「自动续写到完整」的事件流。

    实现为一个事件流包装器（而非在 executor 里重跑请求），这样凭证固定、
    轮换/记账/统计这些既有逻辑完全不用改：executor 只会看到一条更长的流。
    """

    def __init__(self, provider: Any, credential_data: dict[str, Any],
                 payload: dict[str, Any], model: str, *, max_continues: int) -> None:
        self._provider = provider
        self._credential_data = credential_data
        self._payload = payload
        self._model = model
        self._max_continues = max_continues
        self.continues_done = 0

    async def __aiter__(self) -> AsyncIterator[Event]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage = Usage()
        finish_reason: str | None = None

        source = self._provider.stream_chat(
            self._credential_data, self._payload, self._model)
        while True:
            async for event in source:
                if event.kind is EventKind.FINISH:
                    finish_reason = event.finish_reason
                    continue
                if event.kind is EventKind.USAGE:
                    usage = _add_usage(usage, event.usage)
                    continue
                if event.kind is EventKind.CONTENT and event.content:
                    content_parts.append(event.content)
                elif event.kind is EventKind.REASONING and event.content:
                    reasoning_parts.append(event.content)
                yield event

            # 未截断 / 已达上限：按最后一轮的真实 finish_reason 收尾
            if (not continues(finish_reason)
                    or self.continues_done >= self._max_continues):
                yield Event(kind=EventKind.USAGE, usage=usage)
                yield Event(kind=EventKind.FINISH, finish_reason=finish_reason)
                return

            self.continues_done += 1
            self._payload = extend_payload(
                self._payload, "".join(content_parts), "".join(reasoning_parts))
            content_parts, reasoning_parts = [], []
            source = self._provider.stream_chat(
                self._credential_data, self._payload, self._model)


def _add_usage(total: Usage, part: Usage | None) -> Usage:
    """跨续写轮次累计 usage；字段缺失按 0 累加，全缺则保留 None。"""
    if part is None:
        return total
    return Usage(
        input_tokens=_sum(total.input_tokens, part.input_tokens),
        output_tokens=_sum(total.output_tokens, part.output_tokens),
        reasoning_tokens=_sum(total.reasoning_tokens, part.reasoning_tokens),
        cached_tokens=_sum(total.cached_tokens, part.cached_tokens),
        credit=_sum(total.credit, part.credit),
    )


def _sum(left: Any, right: Any) -> Any:
    if left is None:
        return right
    if right is None:
        return left
    return left + right
