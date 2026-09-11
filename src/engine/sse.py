"""SSE 帧解析（跨 provider 共用的规范层）。

只负责把字节流切成 (event, data) 帧；事件语义由各 provider 的 events.py 映射。
"""

from __future__ import annotations

import codecs
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SSEFrame:
    event: str
    data: str


class FrameAssembler:
    """逐行喂入，遇空行成帧；末尾无空行时用 flush() 收尾。

    同步与流式解析共用同一状态机，避免两份逻辑漂移。
    """

    __slots__ = ("_data_parts", "_event")

    def __init__(self) -> None:
        self._event = ""
        self._data_parts: list[str] = []

    def feed_line(self, raw_line: str) -> SSEFrame | None:
        line = raw_line.rstrip("\r")
        if line == "":
            frame = self.flush()
            self._event = ""
            return frame
        if line.startswith(":"):
            return None                       # 注释/心跳行
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            self._event = value
        elif field == "data":
            self._data_parts.append(value)
        return None

    def flush(self) -> SSEFrame | None:
        if not self._data_parts:
            return None
        frame = SSEFrame(event=self._event, data="\n".join(self._data_parts))
        self._data_parts = []
        return frame


def parse_frames(text: str) -> list[SSEFrame]:
    """把一段 SSE 文本解析为帧列表（测试与聚合路径用）。"""
    assembler = FrameAssembler()
    frames: list[SSEFrame] = []
    for line in text.split("\n"):
        frame = assembler.feed_line(line)
        if frame is not None:
            frames.append(frame)
    tail = assembler.flush()
    if tail is not None:
        frames.append(tail)
    return frames


async def iter_frames(chunks: AsyncIterator[bytes]) -> AsyncIterator[SSEFrame]:
    """流式解析：增量 UTF-8 解码，按行缓冲，遇空行成帧。

    用增量解码器，避免多字节字符被块边界切断后变成替换字符。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    assembler = FrameAssembler()
    buffer = ""
    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            frame = assembler.feed_line(line)
            if frame is not None:
                yield frame
    if buffer:                                # 末行没有换行符（不可能是空行）
        assembler.feed_line(buffer)
    tail = assembler.flush()                  # 末帧没有空行
    if tail is not None:
        yield tail


def format_openai_frame(payload: str) -> bytes:
    """OpenAI 兼容的 SSE 帧。"""
    return f"data: {payload}\n\n".encode()


SSE_DONE = b"data: [DONE]\n\n"


def format_frames(frames: Iterable[SSEFrame]) -> bytes:  # pragma: no cover - 测试辅助
    return b"".join(
        f"event: {f.event}\ndata: {f.data}\n\n".encode() if f.event
        else f"data: {f.data}\n\n".encode()
        for f in frames
    )
