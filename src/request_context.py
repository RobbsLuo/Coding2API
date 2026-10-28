"""请求 ID 的生成、校验与跨层传递（PROPOSAL §6）。

为什么用 contextvar 而不是往每个函数签名里加参数：请求 ID 要在**统计写入**
（`StatsCollector.record`）时落库，而调用链是
`chat 路由 → executor → stats_collector`——中间隔着引擎层，不该为了一个
可观测性字段把参数一路透传（会污染所有中间层签名，且新增调用点容易漏传）。
contextvar 由中间件在请求入口设置，同一 asyncio 任务及其子任务可见。

纪律：
- 入站 `X-Request-ID` 是**客户端可写**的输入，必须按白名单字符集与长度校验
  后才采信（否则日志/DB 会被人塞进换行、超长串）。不合法就丢弃、改用自生成。
- 自生成用 `uuid4().hex`（32 位十六进制）：与 `usage_events.id` 同风格，
  无连字符便于日志里整段 grep。
- 出站**不**透传到上游：各渠道的出站头语义不同（qoder 的 `X-Request-ID` 是
  uid 派生的防重放值、trae/codearts 有指纹敏感头），统一改写会破坏既有契约。
  本字段的用途是本服务侧「响应头 + 统计」的关联，不冒充上游追踪。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# 采信的入站 X-Request-ID 字符集：可见 ASCII 里去掉空格与引号类，长度 1-128。
# 128 足够容纳常见实现（UUID、base62 trace id），又不至于让一列被滥用成日志。
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# 响应头名（与入站同名，回显或生成值）。
REQUEST_ID_HEADER = "X-Request-ID"

_current: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """生成一个新的请求 ID（32 位十六进制，无连字符）。"""
    return uuid.uuid4().hex


def sanitize_request_id(raw: str | None) -> str | None:
    """校验入站请求 ID；不合法（含换行/超长/空）返回 None，由调用方自生成。"""
    if not raw:
        return None
    return raw if REQUEST_ID_PATTERN.match(raw) else None


def set_request_id(value: str | None) -> None:
    """在当前上下文设置请求 ID（中间件在调用下游之前调用）。

    允许 None 是为了测试/后台线程能把上下文复原（默认值即 None）。
    """
    _current.set(value)


def get_request_id() -> str | None:
    """读当前上下文的请求 ID；不在请求内（后台任务、脚本）返回 None。"""
    return _current.get()


def resolve_request_id(header: str | None) -> str:
    """入站头 → 最终请求 ID：合法则沿用（便于与客户端日志对齐），否则自生成。"""
    return sanitize_request_id(header) or new_request_id()


# 发起请求的 API Key id（api_keys.id）。同上：走 contextvar 是因为调用链
# `鉴权依赖 → api 出口 → executor → stats_collector` 跨引擎层，不该为一个计量
# 字段污染所有中间层签名。**只由鉴权层设置**（`src/api/deps.py` 校验通过后），
# 绝不从请求头读——入站头是客户端可写的，信它等于把归属权交给调用方。
_api_key_id: ContextVar[str | None] = ContextVar("api_key_id", default=None)


def set_api_key_id(value: str | None) -> None:
    """在当前上下文设置 API Key id（鉴权依赖在放行之前调用；None=复原）。"""
    _api_key_id.set(value)


def get_api_key_id() -> str | None:
    """读当前上下文的 API Key id；无请求上下文（后台任务/脚本）返回 None。"""
    return _api_key_id.get()


# 当前请求使用的凭证 id（credentials.id）。executor 在每次调用上游前设置：
# 出站代理按凭证分配（「一号一出口」，见 `provider/proxy.py`），provider 层
# 读它选连接池。**只由 engine 层设置**（凭证来自调度器，不是客户端输入）。
# 后台任务（探活/签到）也会设置——同一个账号的探活与推理走同一出口才一致。
_credential_id: ContextVar[str | None] = ContextVar("credential_id", default=None)


def set_credential_id(value: str | None) -> None:
    """在当前上下文设置凭证 id；None = 复原（回落到渠道级代理）。"""
    _credential_id.set(value)


def get_credential_id() -> str | None:
    """读当前上下文的凭证 id；无请求上下文（或虚拟凭证）返回 None。"""
    return _credential_id.get()


@contextmanager
def credential_scope(credential_id: str | None) -> Iterator[None]:
    """临时把当前凭证设为 `credential_id`，退出时**还原**（不写成 None）。

    用于后台任务与管理台单条动作：循环里逐条设置，退出后不能把外层（如批量
    端点自己在用的默认出口）误清成空。`token` 还原是 contextvar 的标准做法。
    """
    token = _credential_id.set(credential_id)
    try:
        yield
    finally:
        _credential_id.reset(token)


# 入站请求头带来的会话标识（E1）：客户端（Codex / Claude Code / opencode 等）
# 用 `x-session-id` 这类头声明「这是同一段对话」，比消息前缀兜底可靠得多。
# 与 request_id 同一套纪律：**入站头是客户端可写输入**，按白名单字符集与长度
# 校验后才采信，不合法一律丢弃；且只作会话粘性的**兜底键**（优先级低于请求体
# 里的显式键），不会影响模型、路由或鉴权。
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# 接受的头名，按优先级排列。前三个是 opencode / Codex 生态实际在用的写法，
# 后两个是 conversation_id 的头形式（部分客户端把它放头里）。
SESSION_ID_HEADERS: tuple[bytes, ...] = (
    b"x-session-id",
    b"x-opencode-session",
    b"x-session-affinity",
    b"conversation-id",
    b"conversation_id",
)

_session_id: ContextVar[str | None] = ContextVar("session_id", default=None)


def sanitize_session_id(raw: str | None) -> str | None:
    """校验会话标识；不合法（含换行/超长/空）返回 None，由调用方丢弃。"""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    return text if SESSION_ID_PATTERN.match(text) else None


def session_id_from_headers(headers) -> str | None:
    """从入站头取会话标识（`headers` 为 ASGI 的 [(bytes, bytes), ...]）。

    按 `SESSION_ID_HEADERS` 的顺序取第一个**合法**的非空值；全部不合法返回
    None。头名大小写不敏感（ASGI 里规范为小写，但仍统一 lower 一次防调用方
    传大小写混合的列表）。
    """
    for name in SESSION_ID_HEADERS:
        for key, value in headers:
            if key.lower() == name:
                candidate = sanitize_session_id(value.decode("latin-1"))
                if candidate is not None:
                    return candidate
    return None


def set_session_id(value: str | None) -> None:
    """在当前上下文设置会话标识（请求入口调用；None = 复原）。"""
    _session_id.set(value)


def get_session_id() -> str | None:
    """读当前上下文的会话标识；无请求上下文返回 None。"""
    return _session_id.get()

