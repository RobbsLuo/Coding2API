"""进程内日志环形缓冲（管理台只读日志页的数据源，E3）。

**为什么是进程内**：日志本来就只在**本进程**产生（`access_log` 写 stdout、
应用 logger 写 stderr），单进程 SQLite 部署下从 `logging` 根处理器旁路一份
最快、也没有「读哪个文件 / 怎么 tail / 轮转后 inode 变了」这一整套平台差异。
不读文件、不 tail，重启即清空——这是运维看「刚刚发生了什么」的视图，不是
审计流水（后者在 `audit_events` / 平台日志里）。

**脱敏是硬前提**：每行格式化后必须过 `redact_or_drop`，命中敏感形态整行
丢弃。宁可漏一条日志，也不能把凭证推进浏览器——日志页的读取者虽然是
admin，但浏览器缓存、历史记录、截图、共享屏幕都会把内容带出受控边界。
白名单按「凭证形状」而不是「字段名」判：

- 敏感词（token / secret / authorization / cookie / password / api_key）
  作为独立单词出现 → 丢（`完成 token 预刷新` 也会丢，这是可接受的漏）；
- 敏感词作为标识符的一部分且后面跟 `=` / `:`（`access_token=…`、
  `X-Api-Key: …`、`set-cookie: …`）→ 丢；
- `sk-…` 形状的密钥、`Bearer <值>` → 丢。

**容量热更**：容量由取值器现读（`log_buffer_lines`），0 = 关闭。每写一行
与每次读取快照都按当前上限裁剪，热更调小后旧行不会继续留在内存里。

**线程安全**：`logging` 可能来自任意线程（后台任务、OAuth 轮询），写入与
裁剪在锁内完成；订阅推送走 `loop.call_soon_threadsafe`——`asyncio.Event.set`
本身不是线程安全的，跨线程直接 set 不会唤醒等待中的 SSE 生成器。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..request_context import get_request_id
from ..stats.live import LiveEventHub

# 单行上限：异常 traceback / 上游响应片段可能很长，缓冲里 500 行 × 无上限
# 等于把内存交给日志内容。截断只影响页面可读性，不影响 stdout 的完整输出。
MAX_LINE_CHARS = 4000

# 订阅者上限（与统计实时流同一考虑）：每个订阅者占一条长连接 + 一个 deque，
# 日志页是低频入口，4 个足够，超出直接 429 而不是静默拖垮服务。
MAX_LOG_SUBSCRIBERS = 4

# 敏感词：独立单词形态。`\b` 在 `access_token` 内部不成立（下划线是词字符），
# 标识符形态由 `_SENSITIVE_IDENTIFIER` 兜住。
_SENSITIVE_WORD = re.compile(
    r"\b(?:tokens?|secrets?|authorization|cookies?|passwords?|passwd|api[_-]?key)\b",
    re.IGNORECASE,
)
# 标识符形态：`access_token=` / `X-Api-Key:` / `set-cookie:` / `client_secret =`
# / JSON 里的 `"access_token": "…"`（键名带引号也必须命中——上游原始 JSON 落进
# 日志时才不会把凭证整段带出去）。
_SENSITIVE_IDENTIFIER = re.compile(
    r"[A-Za-z0-9_.\-]*(?:token|secret|passwd|password|cookie|authorization|api[_-]?key)"
    r"[A-Za-z0-9_.\-]*[\"']?\s*[:=]",
    re.IGNORECASE,
)
# 凭证形状：OpenAI 风格 `sk-…`（要求前面不是词字符，避免误伤 `task-run`）。
_SECRET_SK = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{2,}")
# `Authorization: Bearer xxx` 的裸值形态（前缀词可能已被上面的规则命中，
# 这里保留是为了覆盖 `Bearer` 单独出现时的情况）。
_BEARER = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)


def redact_or_drop(line: str) -> str | None:
    """格式化后的日志行 → 可下发的文本；命中敏感形态返回 `None`（整行丢弃）。

    纯函数：不读全局状态、不做 IO，便于把全部分支钉在测试里。
    """
    if not line or not line.strip():
        return None
    if (_SENSITIVE_WORD.search(line) is not None
            or _SENSITIVE_IDENTIFIER.search(line) is not None
            or _SECRET_SK.search(line) is not None
            or _BEARER.search(line) is not None):
        return None
    return line


@dataclass(slots=True)
class LogLine:
    """缓冲里的一行。`seq` 单调递增，用于回放与实时推送去重。"""

    seq: int
    ts: float
    level: str
    logger: str
    message: str
    request_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "seq": self.seq,
            "ts": self.ts,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
        }
        # 没有请求上下文（后台任务）时不带该键：前端据此显示「—」。
        if self.request_id is not None:
            payload["request_id"] = self.request_id
        return payload


class LogBuffer:
    """按当前容量保存最近 N 行的环形缓冲，并向订阅者广播新行。"""

    def __init__(self, *, capacity: Callable[[], int],
                 hub: LiveEventHub | None = None,
                 clock: Callable[[], float] = time.time,
                 persist: Any | None = None) -> None:
        self._capacity = capacity
        self._clock = clock
        # 可选落库（`db/log_repo.LogRepository`）：None = 只保留内存环。
        # 写内存与写库都在 append 里，页面实时性与重启后回看各取所需；
        # 落库异常在 LogRepository 内部吞掉，不影响这里的推送。
        self._persist = persist
        self._lines: deque[LogLine] = deque()
        self._lock = threading.Lock()
        self._seq = 0
        # 订阅中心：与统计实时流同范式（有界缓冲 + 每订阅者上限 + 超限 429）。
        self.hub = hub or LiveEventHub(max_subscribers=MAX_LOG_SUBSCRIBERS)
        # 事件循环在 SSE 端点里绑定（build_app 时应用还没跑起来）。
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------- 容量/开关

    def capacity(self) -> int:
        """当前生效容量（热更现读）；负值按 0 处理。"""
        return max(int(self._capacity()), 0)

    @property
    def enabled(self) -> bool:
        """是否开启日志页（`log_buffer_lines > 0`）。"""
        return self.capacity() > 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定事件循环（SSE 端点每次建连时调用，幂等）。"""
        self._loop = loop

    # ---------------------------------------------------------------- 写入

    def append(self, *, level: str, logger_name: str, message: str,
               request_id: str | None = None, ts: float | None = None
               ) -> dict[str, Any] | None:
        """写入一行；关闭状态下清空缓冲并返回 `None`（不推送）。"""
        capacity = self.capacity()
        if capacity <= 0:
            with self._lock:
                self._lines.clear()
            return None
        with self._lock:
            self._seq += 1
            line = LogLine(seq=self._seq,
                           ts=self._clock() if ts is None else ts,
                           level=level, logger=logger_name, message=message,
                           request_id=request_id)
            self._lines.append(line)
            while len(self._lines) > capacity:
                self._lines.popleft()
        payload = line.as_dict()
        # 落库只**入队**（`LogRepository.append` 内部是 `put_nowait`，满即丢弃）：
        # 这里常在事件循环线程上，同步写 SQLite 会被后台任务的写锁卡到
        # `busy_timeout`（5s），把整个服务拖停。真正的批量落库在消费者线程上。
        if self._persist is not None:
            self._persist.append(ts=line.ts, level=line.level,
                                 logger_name=line.logger, message=line.message,
                                 request_id=line.request_id)
        self._publish(payload)
        return payload

    def snapshot(self, limit: int | None = None) -> list[dict[str, Any]]:
        """当前缓冲的副本（旧→新）；`limit` 只取最近若干行。

        读取时也按当前容量裁剪：热更调小后，无需等下一行写入才收缩。
        """
        with self._lock:
            capacity = self.capacity()
            if capacity <= 0:
                self._lines.clear()
                return []
            while len(self._lines) > capacity:
                self._lines.popleft()
            lines = list(self._lines)
        if limit is not None:
            lines = lines[-limit:] if limit > 0 else []
        return [line.as_dict() for line in lines]

    # ---------------------------------------------------------------- 推送

    def _publish(self, payload: dict[str, Any]) -> None:
        """把新行推给订阅者（跨线程安全）。

        没有订阅者时不排队：首个订阅者的快照已包含此刻之前的全部行，
        逐行 `call_soon_threadsafe` 只是白占事件循环。
        """
        loop = self._loop
        if loop is None or loop.is_closed() or self.hub.subscriber_count == 0:
            return
        loop.call_soon_threadsafe(self.hub.publish, payload)


def _record_request_id(record: logging.LogRecord) -> str | None:
    """优先读记录自带的 `request_id`，否则回落 contextvar（后台任务为 None）。"""
    value = getattr(record, "request_id", None)
    if value:
        return str(value)
    return get_request_id()


class LogBufferHandler(logging.Handler):
    """把格式化后的记录写进 `LogBuffer`（脱敏在写入前完成）。

    **绝不抛出**：日志处理器抛异常会沿 `logger.info(...)` 冒泡到业务代码，
    一条畸形日志就能把请求打成 500。异常交给 `handleError`（与其他内置
    handler 一致：`logging.raiseExceptions` 为真时打印到 stderr）。
    """

    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self.buffer = buffer
        # 只保留消息正文：缓冲行已带 ts/level/logger 字段，再拼一遍时间戳与
        # 级别只会在页面上重复。
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact_or_drop(self.format(record))
            if message is None:
                return
            if len(message) > MAX_LINE_CHARS:
                message = message[:MAX_LINE_CHARS] + "…（已截断）"
            self.buffer.append(level=record.levelname, logger_name=record.name,
                               message=message, request_id=_record_request_id(record),
                               ts=record.created)
        except Exception:  # noqa: BLE001 - 日志缓冲故障不得影响业务
            self.handleError(record)


def sync_root_handler(buffer: LogBuffer,
                      root: logging.Logger | None = None) -> LogBufferHandler | None:
    """让 root logger 上恰好挂一个指向 `buffer` 的处理器（幂等）。

    - `log_buffer_lines == 0`：摘掉已有处理器（关闭即不产生开销）；
    - 已挂着别的实例（测试反复 `build_app`、开发 reload）：替换，避免旧缓冲
      继续吃日志或叠加多份；
    - 热更从 0 打开：端点每次访问也会调本函数，页面一打开就自动挂上。
    """
    logger = root or logging.getLogger()
    existing = next((h for h in logger.handlers if isinstance(h, LogBufferHandler)), None)
    if not buffer.enabled:
        if existing is not None:
            logger.removeHandler(existing)
        return None
    if existing is not None and existing.buffer is buffer:
        return existing
    if existing is not None:
        logger.removeHandler(existing)
    handler = LogBufferHandler(buffer)
    logger.addHandler(handler)
    return handler


def detach_root_handler(buffer: LogBuffer, root: logging.Logger | None = None) -> bool:
    """摘掉指向 `buffer` 的处理器（生命周期结束时调用，避免留住整个 app）。

    返回是否真的摘掉了：测试里可能已被 `sync_root_handler` 换成新实例。
    """
    logger = root or logging.getLogger()
    existing = next((h for h in logger.handlers
                     if isinstance(h, LogBufferHandler) and h.buffer is buffer), None)
    if existing is None:
        return False
    logger.removeHandler(existing)
    return True
