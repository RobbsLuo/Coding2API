"""OpenAI 兼容响应：中立 Event → chunk（流式）或聚合（非流式）。

约定（继承 codebuddy2api 的语义）：
- 首块补 role:assistant
- 上游缺失 index 的 tool_calls 补稳定 index，不重生成 id
- reasonng 走 delta.reasoning_content
- 流以 data: [DONE] 结束
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ...engine.sse import SSE_DONE, format_openai_frame
from ...provider.base import Event, EventKind, Usage

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


def _completion_id() -> str:
    return "chatcmpl-" + f"{time.time_ns():x}"


@dataclass
class ToolIndexState:
    """优先沿用上游 index，缺失时按出现顺序补稳定位置。"""

    id_to_index: dict[str, int] = field(default_factory=dict)
    used: set[int] = field(default_factory=set)

    def resolve(self, tool_call: dict[str, Any]) -> tuple[dict[str, Any], int]:
        item = dict(tool_call)
        index = item.get("index")
        tool_id = item.get("id")
        if isinstance(index, int) and not isinstance(index, bool):
            self.used.add(index)
            if isinstance(tool_id, str) and tool_id:
                self.id_to_index[tool_id] = index
            return item, index
        if isinstance(tool_id, str) and tool_id and tool_id in self.id_to_index:
            resolved = self.id_to_index[tool_id]
            item["index"] = resolved
            return item, resolved
        candidate = 0
        while candidate in self.used:
            candidate += 1
        self.used.add(candidate)
        if isinstance(tool_id, str) and tool_id:
            self.id_to_index[tool_id] = candidate
        item["index"] = candidate
        return item, candidate


class StreamTranslator:
    """把 Event 序列翻译为 OpenAI SSE 帧序列。"""

    def __init__(self, model: str) -> None:
        self.model = model
        self._sent_role = False
        self._tool_index = ToolIndexState()
        self._finished = False
        # 上游 usage 不单独成帧，但要留给统计采集
        self.usage: Usage | None = None

    @staticmethod
    def _is_empty_payload(event: Event) -> bool:
        """空内容不消耗首帧 role 标记，避免发出无意义 chunk。"""
        return not (event.content or event.tool_calls)

    def translate(self, event: Event) -> Iterator[bytes]:
        if event.kind is EventKind.ERROR:
            yield format_openai_frame(json.dumps(
                {"error": {"message": event.error_message or "upstream error",
                           "type": "upstream_error", "code": event.error_code}},
                ensure_ascii=False))
            return
        if event.kind is EventKind.USAGE:
            # usage 不单独成帧，由聚合路径处理；流式沿用上游语义不额外发 usage 块
            self.usage = event.usage
            return
        if event.kind is EventKind.FINISH:
            self._finished = True
            yield from self._close(event.finish_reason or "stop")
            return

        delta: dict[str, Any] = {}
        if not self._sent_role and not self._is_empty_payload(event):
            delta.update(ROLE_CHUNK)
            self._sent_role = True
        if event.kind is EventKind.CONTENT and event.content:
            delta["content"] = event.content
        elif event.kind is EventKind.REASONING and event.content:
            delta["reasoning_content"] = event.content
        elif event.kind is EventKind.TOOL_CALLS and event.tool_calls:
            delta["tool_calls"] = [self._tool_index.resolve(tc)[0] for tc in event.tool_calls]
        if not delta:
            return
        yield format_openai_frame(json.dumps(_chunk(self.model, delta), ensure_ascii=False))

    def finish(self) -> Iterator[bytes]:
        """上游未发 done 就断流：补一个结束帧，保证客户端不会挂住。"""
        if self._finished:
            return  # 上游已发 done，DONE 已随 _close 发出，不能重复
        yield from self._close("stop")
        yield SSE_DONE

    def _close(self, finish_reason: str) -> Iterator[bytes]:
        yield format_openai_frame(json.dumps(
            _chunk(self.model, {}, finish_reason=finish_reason), ensure_ascii=False))
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

    message: dict[str, Any] = {"role": "assistant",
                              "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    result: dict[str, Any] = {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    result["usage"] = {
        "prompt_tokens": (usage.input_tokens if usage else None),
        "completion_tokens": (usage.output_tokens if usage else None),
        "total_tokens": (
            (usage.input_tokens or 0) + (usage.output_tokens or 0) if usage else None
        ),
        "completion_tokens_details": {
            "reasoning_tokens": usage.reasoning_tokens if usage else None},
    }
    return result


class UpstreamStreamError(Exception):
    """流内业务错误（聚合路径抛出，由 executor 转成冷却 + 轮换）。"""

    def __init__(self, event: Event) -> None:
        super().__init__(event.error_message or "upstream stream error")
        self.event = event


def error_payload(message: str, code: str, status: int) -> dict[str, Any]:
    return {"error": {"message": message, "type": "api_error", "code": code,
                      "status": status}}
