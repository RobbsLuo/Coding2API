"""OpenAI 兼容响应：中立 Event → chunk（流式）或聚合（非流式）。

约定（继承 codebuddy2api 的语义）：
- 首块补 role:assistant
- 上游缺失 index 的 tool_calls 补稳定 index，不重生成已有 id；`id` 缺失时
  按 index 补一个稳定值（OpenAI 客户端按 id 聚合分片，缺 id 会丢工具调用）
- reasonng 走 delta.reasoning_content
- 流以 data: [DONE] 结束
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ...engine.sse import SSE_COMMENT, SSE_DONE, format_openai_frame
from ...provider.base import Event, EventKind, Usage
from . import dsml
from .errors import InvalidToolArguments, UpstreamStreamError, stream_error_frame

ROLE_CHUNK = {"role": "assistant", "content": ""}


def _chunk(model: str, delta: dict[str, Any], *,
           finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": _completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def usage_payload(usage: Usage | None) -> dict[str, Any]:
    """OpenAI `usage` 对象形状：三个主计数 + 两个 details 子对象。

    流式收尾帧与非流式 `aggregate()` 共用一处，避免两份形状各自漂移。
    「上游未上报」与「上报 0」必须可区分，故缺省一律为 None 而非 0。
    """
    return {
        "prompt_tokens": (usage.input_tokens if usage else None),
        "completion_tokens": (usage.output_tokens if usage else None),
        "total_tokens": (
            (usage.input_tokens or 0) + (usage.output_tokens or 0) if usage else None
        ),
        "prompt_tokens_details": {
            "cached_tokens": usage.cached_tokens if usage else None},
        "completion_tokens_details": {
            "reasoning_tokens": usage.reasoning_tokens if usage else None},
    }


def _completion_id() -> str:
    return "chatcmpl-" + f"{time.time_ns():x}"


@dataclass
class ToolIndexState:
    """优先沿用上游 index，缺失时按出现顺序补稳定位置；`id` 缺失时补稳定 id。

    上游（尤其聚合型中转）偶尔只发 `function.arguments` 分片而不发 `id`。
    OpenAI 客户端按 `id` 聚合流式工具调用分片，缺 id 的分片会被丢弃，表现为
    「模型调了工具但客户端收不到」。故按 index 生成并记忆 id，后续同 index 的
    分片沿用同一个值（每次现生成会让同一调用被拆成多个）。
    """

    id_to_index: dict[str, int] = field(default_factory=dict)
    index_to_id: dict[int, str] = field(default_factory=dict)
    used: set[int] = field(default_factory=set)
    _seq: int = 0

    def resolve(self, tool_call: dict[str, Any]) -> tuple[dict[str, Any], int]:
        item = dict(tool_call)
        index = item.get("index")
        tool_id = item.get("id")
        if isinstance(index, int) and not isinstance(index, bool):
            self.used.add(index)
            self._bind(index, tool_id, item)
            return item, index
        if isinstance(tool_id, str) and tool_id and tool_id in self.id_to_index:
            resolved = self.id_to_index[tool_id]
            item["index"] = resolved
            return item, resolved
        candidate = 0
        while candidate in self.used:
            candidate += 1
        self.used.add(candidate)
        self._bind(candidate, tool_id, item)
        item["index"] = candidate
        return item, candidate

    def _bind(self, index: int, tool_id: Any, item: dict[str, Any]) -> None:
        """登记 id↔index 双向映射；`id` 缺失时按 index 补一个稳定值。

        每片都补同一个 id（而不是只补首片）：按 id 聚合的客户端若在续片上
        拿到空 id，会把一次调用拆成两条；重复同一个 id 则各种聚合方式都安全。
        """
        if isinstance(tool_id, str) and tool_id:
            self.id_to_index[tool_id] = index
            self.index_to_id.setdefault(index, tool_id)
            return
        existing = self.index_to_id.get(index)
        if existing is None:
            self._seq += 1
            existing = f"call_{index}_{self._seq}"
            self.index_to_id[index] = existing
        item["id"] = existing


class StreamTranslator:
    """把 Event 序列翻译为 OpenAI SSE 帧序列。"""

    def __init__(self, model: str) -> None:
        self.model = model
        self._sent_role = False
        self._tool_index = ToolIndexState()
        self._finished = False
        # 上游 usage 不在收到时就成帧（OpenAI 惯例是收尾帧之后、[DONE] 之前
        # 补一帧 choices 为空的 usage），这里先缓冲 latest 值供统计与收尾帧共用
        self.usage: Usage | None = None
        # [DONE] 已产出：客户端此后断开属正常收尾（拿到回调即关连接）
        self.done_sent = False
        # DSML 泄漏回读：普通正文零缓冲，只有出现标记才进入缓冲态
        self._dsml = dsml.DsmlStreamFilter()

    def translate(self, event: Event) -> Iterator[bytes]:
        if event.kind is EventKind.ERROR:
            yield format_openai_frame(json.dumps(
                {"error": {"message": event.error_message or "upstream error",
                           "type": "upstream_error", "code": event.error_code}},
                ensure_ascii=False))
            return
        if event.kind is EventKind.USAGE:
            # 此处不成帧：留到 _close 随 [DONE] 之前补发（OpenAI 标准形态）。
            # 多次 USAGE 取最后一次上游汇报的值。
            self.usage = event.usage
            return
        if event.kind is EventKind.FINISH:
            self._finished = True
            yield from self._close(event.finish_reason or "stop")
            return

        text = event.content
        recovered: list[dict[str, Any]] = []
        if event.kind is EventKind.CONTENT and text:
            # DSML 泄漏回读：可见正文照常流式放行，回读出的调用另起一帧
            text, recovered = self._dsml.feed(text)
            if not text and not recovered:
                return
        delta: dict[str, Any] = {}
        if not self._sent_role and (text or event.tool_calls or recovered):
            delta.update(ROLE_CHUNK)
            self._sent_role = True
        if event.kind is EventKind.CONTENT and text:
            delta["content"] = text
        elif event.kind is EventKind.REASONING and event.content:
            delta["reasoning_content"] = event.content
        elif event.kind is EventKind.TOOL_CALLS and event.tool_calls:
            delta["tool_calls"] = [self._tool_index.resolve(tc)[0] for tc in event.tool_calls]
        if delta:
            yield format_openai_frame(
                json.dumps(_chunk(self.model, delta), ensure_ascii=False))
        if recovered:
            calls = [self._tool_index.resolve(tc)[0] for tc in recovered]
            yield format_openai_frame(json.dumps(
                _chunk(self.model, {"tool_calls": calls}), ensure_ascii=False))

    def finish(self) -> Iterator[bytes]:
        """上游未发 done 就断流：补一个结束帧，保证客户端不会挂住。"""
        if self._finished:
            return  # 上游已发 done，DONE 已随 _close 发出，不能重复
        # [DONE] 由 _close 统一发出（与 FINISH 路径同源），此处不再补
        yield from self._close("stop")

    def keepalive(self) -> bytes:
        """SSE 注释帧心跳。

        上游推理可能长时间不吐字节（长思考、排队），中间没有字节时部分
        客户端/反向代理会因空闲超时断开。注释帧被 SSE 客户端忽略，
        只为保活。
        """
        return SSE_COMMENT

    def error_frame(self, message: str, code: str) -> bytes:
        """出口错误帧（形状随协议不同，由各出口的 translator 决定）。"""
        return stream_error_frame(message, code)

    def _close(self, finish_reason: str) -> Iterator[bytes]:
        # 缓冲里剩下的正文（不完整的 DSML 标记）原样交出，绝不吞用户内容
        pending = self._dsml.flush()
        if pending:
            delta: dict[str, Any] = {}
            if not self._sent_role:
                delta.update(ROLE_CHUNK)
                self._sent_role = True
            delta["content"] = pending
            yield format_openai_frame(
                json.dumps(_chunk(self.model, delta), ensure_ascii=False))
        if self._dsml.recovered and finish_reason == "stop":
            # 回读出了工具调用：把 stop 改成 tool_calls，否则客户端不执行
            finish_reason = "tool_calls"
        yield format_openai_frame(json.dumps(
            _chunk(self.model, {}, finish_reason=finish_reason), ensure_ascii=False))
        # OpenAI 标准形态：include_usage 时收尾帧之后、[DONE] 之前补一帧
        # choices 为空的 usage。上游没报就不补（不发 0 占位冒充已上报）。
        if self.usage is not None:
            yield format_openai_frame(json.dumps(
                {"id": _completion_id(),
                 "object": "chat.completion.chunk",
                 "created": int(time.time()),
                 "model": self.model,
                 "choices": [],
                 "usage": usage_payload(self.usage)},
                ensure_ascii=False))
        # 在产出 [DONE] 前置位：executor 据此区分「完整响应已送出后的断开」
        # （客户端拿到回调即关闭连接，属正常收尾）与真正的中途断开
        self.done_sent = True
        yield SSE_DONE


def aggregate(events: Iterable[Event], model: str) -> dict[str, Any]:
    """非流式：把 Event 聚合成 chat.completion（上游只有流式，本地聚合）。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_index = ToolIndexState()
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: Usage | None = None
    finish_reason = "stop"

    for event in events:
        if event.kind is EventKind.ERROR:
            raise UpstreamStreamError(event)
        if event.kind is EventKind.USAGE:
            usage = event.usage
        elif event.kind is EventKind.FINISH:
            finish_reason = event.finish_reason or "stop"
        elif event.kind is EventKind.CONTENT and event.content:
            content_parts.append(event.content)
        elif event.kind is EventKind.REASONING and event.content:
            reasoning_parts.append(event.content)
        elif event.kind is EventKind.TOOL_CALLS and event.tool_calls:
            for raw in event.tool_calls:
                item, index = tool_index.resolve(raw)
                current = tool_calls.get(index)
                if current is None:
                    tool_calls[index] = item
                else:
                    current.setdefault("function", {})
                    fragment = (item.get("function") or {}).get("arguments")
                    if isinstance(fragment, str):
                        current["function"]["arguments"] = (
                            (current["function"].get("arguments") or "") + fragment)

    # DSML 泄漏回读：模型把工具调用写成文本时还原成结构化调用，并从正文剥掉
    content_text = "".join(content_parts)
    recovered, content_text = dsml.extract(content_text)
    if recovered:
        next_index = max(tool_calls) + 1 if tool_calls else 0
        for offset, call in enumerate(recovered):
            call = dict(call)
            call["index"] = next_index + offset
            tool_calls[call["index"]] = call
        if finish_reason == "stop":
            finish_reason = "tool_calls"
    ordered = [tool_calls[i] for i in sorted(tool_calls)]
    for call in ordered:
        function = call.get("function") or {}
        if not dsml.is_valid_arguments(function.get("arguments")):
            # fail-closed：坏参数不递交给客户端执行，按协议级失败处理
            raise InvalidToolArguments(function.get("name") or "-",
                                       function.get("arguments"))
    message: dict[str, Any] = {"role": "assistant",
                              "content": content_text or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if ordered:
        message["tool_calls"] = ordered
    result: dict[str, Any] = {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    result["usage"] = usage_payload(usage)
    return result
