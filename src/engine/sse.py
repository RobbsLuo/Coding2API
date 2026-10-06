"""SSE 帧解析（跨 provider 共用的规范层）。

只负责把字节流切成 (event, data) 帧；事件语义由各 provider 的 events.py 映射。
"""

from __future__ import annotations

import codecs
from collections.abc import AsyncIterator
from dataclasses import dataclass


class SSEFrameTooLarge(Exception):
    """单行/单帧超过上限：中止该流（恶意或协议损坏的上游）。

    上游持续发送不含换行的大块、或 endless `data:` 行且不给空行时，`buffer`
    与 `_data_parts` 会无界增长 → 内存耗尽。达到上限即抛错终止流，绝不静默
    截断（截断会把损坏数据当正常内容交给客户端）。
    """


# 单行缓冲上限与单帧 data 累积上限。正常 SSE 帧远小于此（几 KB）；8 MiB
# 足以容纳任何真实模型输出，且确保单个连接不会撑爆进程内存。
MAX_SSE_LINE_BYTES = 8 * 1024 * 1024
MAX_SSE_FRAME_CHARS = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SSEFrame:
    event: str
    data: str


class FrameAssembler:
    """逐行喂入，遇空行成帧；末尾无空行时用 flush() 收尾。

    同步与流式解析共用同一状态机，避免两份逻辑漂移。
    """

    __slots__ = ("_data_parts", "_event", "_length")

    def __init__(self) -> None:
        self._event = ""
        self._data_parts: list[str] = []
        self._length = 0                      # _data_parts 的累计字符数（含分隔）

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
            self._length += len(value)
            if self._length > MAX_SSE_FRAME_CHARS:
                raise SSEFrameTooLarge(
                    f"SSE frame exceeds {MAX_SSE_FRAME_CHARS} characters")
        return None

    def flush(self) -> SSEFrame | None:
        if not self._data_parts:
            return None
        frame = SSEFrame(event=self._event, data="\n".join(self._data_parts))
        self._data_parts = []
        self._length = 0
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
        # 单行缓冲上限：上游一直不吐换行时 buffer 无界增长（见 SSEFrameTooLarge）。
        # 按字符计数（O(1)）；UTF-8 单字符最多 4 字节，以字符数当字节上限只会
        # 更宽松一点，仍有界。
        if len(buffer) > MAX_SSE_LINE_BYTES:
            raise SSEFrameTooLarge(f"SSE line exceeds {MAX_SSE_LINE_BYTES} characters")
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
# SSE 注释帧：被客户端忽略，只为长空隙保活（上游思考/排队时可能长时间无字节）
SSE_COMMENT = b": keepalive\n\n"
