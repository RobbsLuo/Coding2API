"""Provider 协议与中立类型（细接口，中立事件层预留）。

Provider 承担上游协议私有部分：发请求、解析事件、分类错误、凭证生命周期与额度探测。
调度、冷却、重试、统计在共享引擎（engine/）。
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable


class EventKind(StrEnum):
    CONTENT = "content"
    REASONING = "reasoning"
    TOOL_CALLS = "tool_calls"
    USAGE = "usage"
    FINISH = "finish"
    ERROR = "error"


class ErrKind(StrEnum):
    """错误分类 → 冷却时长。

    分类的粒度决定冷却的作用域：账号级（PLAN/SOFT/DEAD/OTHER/RESTRICTED）与
    模型级（MODEL/BLOCKED）必须分开，否则单个模型打满会连累同账号的
    其他模型（实测 6004 只冷却触发的那个模型）。
    """

    PLAN = "plan"      # 权益耗尽（1005）→ 12h 长冷却
    # 余额不足（402 / 14018）：等签到恢复，冷却到次日 04:00 而非固定时长
    CREDIT = "credit"
    SOFT = "soft"      # 限流/404 → 60s，不累计错误数
    DEAD = "dead"      # session 失效 → 硬禁用
    # 账号被上游风控限制（CodeBuddy 11140「request illegal」）：账号级长冷却、
    # **当前请求不再换号**。与 DEAD 的区别：DEAD 是凭据本身失效（重登可恢复，
    # 因此永久禁用 + 人工 revive），RESTRICTED 是上游按账号/IP 拉黑（本地无解，
    # 到期自动半开，逐个号去撞只会扩大标记面）。
    RESTRICTED = "restricted"
    OTHER = "other"    # 其他 4xx/5xx → 累计，连续 3 次 → 10m
    INVALID = "invalid"  # 请求无效（如模型不存在）→ 不冷却凭证，直接 400 回客户端
    # 模型级限流（6004）：只冷却触发的那个模型，切其他模型立即可用
    MODEL = "model"
    # 并发会话打满（CodeArts TM.00001041）：在途排空即恢复，用固定短冷却，
    # 不套 MODEL 的翻倍退避（一次打满锁 10min 起、翻倍到 2h 太久，见 scheduler）。
    CONCURRENCY = "concurrency"
    # 该后端无此模型（11102）：(账号, 模型) 负缓存，重试无意义
    BLOCKED = "blocked"
    # 请求级错误（请求体坏 11101 / 上下文超限 11115 / 渠道风控 11128 /
    # 图片无效 11135 / CodeArts 非多模态模型收到图片 001001020）：不是账号
    # 的问题，不冷却、不累计；仍会换号重试
    REQUEST = "request"
    # 排队中（上游明确回 `isQueued` / `RESOURCE_LIMIT` + `retryAfterSeconds`）：
    # 既不是账号故障也不是请求错误，是「这个号现在满了，等 N 秒再打它」。
    # 不冷却、不累计、不占轮换预算，等在原号上重开（见 executor 的排队重开）。
    # 与 SOFT 的关键差别：SOFT 冷却 60s 且换号，排队按上游点名的秒数等在原号。
    QUEUE = "queue"


# 冷却作用域是「(凭证, 模型)」而非整个凭证的错误类别。
# 账号级冷却必须显式清空模型级条目（见 scheduler.note_error），
# 否则上一次模型级限流的豁免会泄漏到本次账号级冷却上（换模型错误绕过）。
MODEL_SCOPED_KINDS = frozenset({ErrKind.MODEL, ErrKind.BLOCKED, ErrKind.CONCURRENCY})


def body_hint(body: bytes, limit: int = 2048) -> str:
    """上游错误响应体的单行摘要（进日志便于定位拒绝原因）。

    上限默认 2048：错误信封（含嵌套 `data`）可能远超 160，截到 160 会把真正
    的业务码/文案切掉；但也不能无界——`body` 大小本身由 BOUNDED_BODY_LIMIT
    兜底（见 read_body_bounded）。超限时明确标注截断，避免被误读为完整响应。
    """
    if not body:
        return ""
    text = body.decode("utf-8", errors="replace")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


# 错误响应体的读取上限：只用于生成可读诊断，超出部分丢弃。防止恶意/异常
# 上游返回超大 body 时把代理进程读爆内存（错误路径本不需要完整正文）。
BOUNDED_BODY_LIMIT = 256 * 1024


_RETRY_AFTER_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-z]+)")
_RETRY_AFTER_PREFIX = re.compile(r"^(?:try\s+again\s+)?(?:in\s+)?", re.IGNORECASE)
# 文本式 `Retry-After` 的单位（`Try again in 17h 59m`）：h/m/s 及其常见长写法
_RETRY_AFTER_UNITS = {
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
}


def _header_value(headers: Any, name: str) -> str:
    """按名取响应头（大小写不敏感，兼容 httpx.Headers 与普通 dict）。"""
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = getter(name)
        if value is not None:
            return str(value)
    items = getattr(headers, "items", None)
    if items is None:
        return ""
    lowered = name.lower()
    for key, value in items():
        if str(key).lower() == lowered:
            return str(value)
    return ""


def _retry_after_delta(raw: str, now: int) -> float | None:
    """`Retry-After` 原始值 → 距现在多少秒；无法识别返回 None。

    支持三种真实形态：整数秒（RFC 9110 标准）、HTTP-date（标准）、以及
    部分上游回的「Try again in 17h 59m」文本（实测 cline-proxy 一类网关）。
    负数 / 过去时刻 / 无法整除的脏值一律 None：宁可回落本地常量，也不
    让一个坏头把所有凭证瞬间打爆（见 scheduler.note_error 的双端钳制）。
    """
    text = raw.strip()
    if not text:
        return None
    if text.isdigit():
        return float(int(text))
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        moment = None
    if moment is not None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.timestamp() - now
    body = _RETRY_AFTER_PREFIX.sub("", text)
    total = 0.0
    cursor = 0
    matched = False
    for match in _RETRY_AFTER_TEXT.finditer(body):
        # token 之间只允许空白 / 逗号 / and（`17h 59m`、`1 hour, 30 min`）
        if not re.fullmatch(r"[\s,]*(?:and\s+)?", body[cursor:match.start()]):
            return None
        unit = _RETRY_AFTER_UNITS.get(match.group(2).lower())
        if unit is None:
            return None
        total += float(match.group(1)) * unit
        matched = True
        cursor = match.end()
    if not matched or body[cursor:].strip():
        return None
    if not math.isfinite(total):
        return None
    return total


def parse_retry_after(headers: Any, now: int) -> int | None:
    """上游 `Retry-After` → 「何时恢复」的 epoch 秒；缺头/非法返回 None。

    这是唯一解析入口：八渠道在构造 `UpstreamHTTPError` 时统一调用
    `retry_hints`（本函数的薄包装），再交给调度器按错误分类的作用域钳制。
    """
    delta = _retry_after_delta(_header_value(headers, "retry-after"), now)
    if delta is None or delta < 0:
        return None
    return int(now + delta)


def parse_retry_after_seconds(headers: Any, now: int) -> float | None:
    """上游 `Retry-After` → 距现在多少秒（同上，保留小数供诊断）。"""
    delta = _retry_after_delta(_header_value(headers, "retry-after"), now)
    if delta is None or delta < 0:
        return None
    return delta


# 响应体里点名的重置时刻：CodeBuddy 国际站 `429 + 6004` 不回 `Retry-After`
# 头，恢复时刻只写在正文里——`{"code":6004,"msg":"usage exceeds frequency
# limit, but don't worry, your usage will reset at 2026-10-25 22:34:25 UTC+8,
# alternatively, you can switch to the other models..."}`。
# 只解析这一种带显式 UTC 偏移的形态：没有偏移的本地时刻无从判断时区，
# 猜错会把冷却写到过去（立刻解冻）或几天后（长期移出池子），一律放弃。
_RESET_AT_TEXT = re.compile(
    r"reset\s+at\s+(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
    r"\s*UTC\s*([+-])(\d{1,2})(?::?(\d{2}))?",
    re.IGNORECASE)


def parse_reset_at(body: bytes | str, now: int) -> int | None:
    """响应体里的「usage will reset at … UTC+8」→ 恢复时刻 epoch；无则 None。

    与 `parse_retry_after` 同一语义（「何时恢复」的绝对时刻），供
    `retry_hints` 在**没有 `Retry-After` 头**时兜底。过去时刻 / 无法解析
    返回 None：宁可回落本地常量冷却，也不要让一个坏正文把冷却写到过去。
    """
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    if not text or "reset" not in text.lower():
        return None
    match = _RESET_AT_TEXT.search(text)
    if match is None:
        return None
    year, month, day, hour, minute, second, sign, hours, minutes = match.groups()
    try:
        offset = timedelta(hours=int(hours), minutes=int(minutes or 0))
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute),
                          int(second),
                          tzinfo=timezone(-offset if sign == "-" else offset))
    except ValueError:      # 2 月 30 日 / 偏移越界这类脏值
        return None
    return int(moment.timestamp()) if int(moment.timestamp()) > now else None


def retry_hints(headers: Any, now: int | None = None,
                body: bytes | str | None = None) -> dict[str, Any]:
    """`UpstreamHTTPError` 的 retry 关键字参数（无头时两值均为 None）。

    让各渠道的 raise 点保持一行：`**retry_hints(response.headers)`。

    `body` 是**头缺失时的兜底来源**（见 `parse_reset_at`）：部分上游（CodeBuddy
    国际站 429+6004）只把恢复时刻写在正文里，不给 `Retry-After` 头，不解析正文
    就只能回落到固定 600s 基数、到期重试再撞一次 429。头优先于正文——头是协议
    标准位，正文只是文案。
    """
    moment = int(time.time()) if now is None else now
    retry_at = parse_retry_after(headers, moment)
    retry_after_seconds = parse_retry_after_seconds(headers, moment)
    if retry_at is None and body is not None:
        reset_at = parse_reset_at(body, moment)
        if reset_at is not None:
            retry_at = reset_at
            retry_after_seconds = float(reset_at - moment)
    return {"retry_at": retry_at, "retry_after_seconds": retry_after_seconds}


async def read_body_bounded(response: Any, limit: int = BOUNDED_BODY_LIMIT) -> bytes:
    """有界读取响应体：最多读 `limit` 字节即停止。

    非 2xx 时的完整 `aread()` 无大小上限；上游（或中间盒）回一个超大 body
    就会让每个失败请求都撑一次内存。这里只取够诊断的前缀。
    """
    collected = bytearray()
    async for chunk in response.aiter_bytes():
        collected.extend(chunk)
        if len(collected) >= limit:
            break
    return bytes(collected[:limit])


# 上游「排队中」信号：`isQueued` 为真，或子码点名 RESOURCE_LIMIT。
# 这是跨渠道的**协议形状**而非某家私有码，故放中立层。
QUEUE_FLAG_KEYS = ("isQueued", "is_queued", "queued")
QUEUE_WAIT_KEYS = ("retryAfterSeconds", "retry_after_seconds", "retryAfter", "retry_after")
QUEUE_SUB_CODES = ("RESOURCE_LIMIT", "RESOURCE_LIMIT_EXCEEDED")
# 上游只说「排队」不给秒数时的兜底等待（秒）。取 5s：比 SOFT 的 60s 短得多，
# 又足以让排队轮次真正错开，不至于变成忙等。
QUEUE_DEFAULT_WAIT_SECONDS = 5.0
# 单轮最长等待（秒）：上游可能点名「2 小时后再来」，照等会把客户端连接吊死。
# 超过上限就按上限等一轮，下一轮仍排队则轮次用尽、退回普通错误处理。
QUEUE_MAX_WAIT_SECONDS = 60.0
# 单次请求最多在原号上重开几轮。上限存在的意义：上游若持续排队，无限重开
# 会把客户端连接吊死；3 轮 + 兜底 5s 最坏多等 ~15s，比直接 503 更友好。
QUEUE_MAX_ROUNDS = 3


def parse_queue_hint(body: bytes | str) -> float | None:
    """上游 body 是否表示「排队中」→ 建议等待秒数；非排队返回 None。

    形状来自上游信封：`{"isQueued": true, "retryAfterSeconds": 30,
    "subCode": "RESOURCE_LIMIT"}`。三者**任一**成立即认定排队——只认
    `isQueued` 会漏掉只给子码的上游，只认子码又会把「配额真的耗尽」误判成
    排队（故子码只认 `RESOURCE_LIMIT` 这一族，不含 `QUOTA_EXCEEDED`）。

    宁可漏判不可误判：识别不出就返回 None，走原有的错误分类路径（最坏是
    一次 SOFT 冷却换号），绝不能把普通失败当成排队而无限等下去。
    """
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    if not text.strip():
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _queue_wait_in(payload, depth=0)


def _queue_wait_in(node: Any, depth: int) -> float | None:
    """在（可能嵌套的）信封里找排队标记；命中返回等待秒数。

    只下钻 3 层：上游信封常见 `{"data": {"error": {...}}}`，再深就不是
    协议结构而是业务正文了，继续递归只会在长正文上白烧 CPU。

    **字符串也要下钻**：上游常把内层报文**再序列化成字符串**（Qoder 的
    排队报文就是 `{"code":"403","message":"{\\"code\\":\\"10605\\",…}"}`，
    排队标记在第二层字符串里）。只认 dict/list 会漏判排队 → 403 被当成
    硬失败 → **好凭证被硬禁用**（实测 2026-10-26：kmodel_latest 排队时
    回 403 + `isQueued`，凭证被误判死）。

    标记节点**没有**秒数时不立即回落兜底值，而是继续往深处找带秒数的
    那一层（Qoder 的 `retryAfterSeconds:30` 在最内层），实在没有才用兜底。
    """
    if depth > 3:
        return None
    if isinstance(node, dict):
        if _is_queue_flag(node):
            wait = _queue_wait_seconds(node)
            if wait is not None:
                return wait
            for value in node.values():
                found = _queue_wait_in(value, depth + 1)
                if found is not None:
                    return found
            return QUEUE_DEFAULT_WAIT_SECONDS
        for value in node.values():
            found = _queue_wait_in(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _queue_wait_in(item, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, str):
        found = _queue_wait_in_text(node, depth)
        if found is not None:
            return found
    return None


def _queue_wait_in_text(text: str, depth: int) -> float | None:
    """字符串里的 JSON 载荷 → 继续找排队标记（上游把内层报文再序列化）。

    **不额外消耗深度**：串里套串（上游把排队报文序列化两次）是同一个协议
    层级，计深度会让最内层的 `retryAfterSeconds` 落在预算外（实测 Qoder
    排队报文串了两层，秒数在最内层）。真正的层级由 dict/list 分支计。
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return _queue_wait_in(parsed, depth)


def _is_queue_flag(node: dict[str, Any]) -> bool:
    for key in QUEUE_FLAG_KEYS:
        if node.get(key) is True:
            return True
    for key in ("subCode", "sub_code", "errorCode", "code"):
        value = node.get(key)
        if isinstance(value, str) and value.upper() in QUEUE_SUB_CODES:
            return True
    return False


def _queue_wait_seconds(node: dict[str, Any]) -> float | None:
    """节点自带的等待秒数；没有则 None（由调用方决定回落策略）。"""
    for key in QUEUE_WAIT_KEYS:
        value = node.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            return float(value)
    return None


_BUSINESS_CODE_RE = re.compile(r'"code"\s*:\s*(-?\d+)')


def business_codes(text: str) -> frozenset[int]:
    """提取 body 里的全部 `"code": N` 业务码（两个上游共用这个信封形状）。

    返回集合而非「第一个」：响应可能嵌套多层 envelope（`{"code":0,"data":
    {"code":11102}}`），按出现顺序取首个会漏掉真正的业务码，按集合成员判断
    才与状态码分支的优先级组合出确定结果。

    不搜裸数字：时间戳、流水号里也会出现 11102 这类片段，直接 substring
    匹配会把无关响应判成模型错误（例如 `"code":111020` 含有 `11102`）。
    只认 `"code":` 键值形态，且要求整个数字 token 相等。
    """
    return frozenset(int(match) for match in _BUSINESS_CODE_RE.findall(text))


async def aclose_stream(stream: Any) -> None:
    """关闭上游事件流，让 provider 的 `finally`（节流名额归还）**同步**执行。

    `async for ... break` 不会关闭 async generator：CPython 只在生成器耗尽、
    显式 `aclose()`、或被 GC 的 asyncgen finalizer 回收时才跑它的 `finally`。
    依赖 GC 意味着名额归还时机不确定——这在 `max_concurrency > 0` 时不是
    「节流变松」，而是**永久丢失许可**：桶停在满载，新请求在 `wait_turn`
    无限阻塞，表现为「用了三次就限制」。

    所以所有提前结束消费的地方（流内错误 break、换号重试、续写中途关闭、
    客户端断开）都必须显式关闭。对没有 `aclose` 的流（纯 AsyncIterator
    实现的测试替身）直接跳过。
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        await aclose()


class UpstreamHTTPError(Exception):
    """上游非 2xx；两个 provider 共用同一形状（status + 原始 body）。

    `kind()` 由子类绑定各自的 classify_status，因为 1005 等业务码的
    判定规则是上游协议私有的。

    `retry_at` / `retry_after_seconds` 来自上游 `Retry-After` 头（见
    `parse_retry_after`）：调度器在**该错误分类的作用域**内据此拉长冷却，
    实现「上游说什么时候恢复就什么时候恢复」。缺头时为 None，行为与
    历史版本逐字节一致。
    """

    classify_status: ClassVar[Callable[[int, bytes], ErrKind]]

    def __init__(self, status: int, body: bytes, *,
                 retry_at: int | None = None,
                 retry_after_seconds: float | None = None) -> None:
        self.status = status
        self.body = body
        self.retry_at = retry_at
        self.retry_after_seconds = retry_after_seconds
        # 异常文本里的 body 摘要只进服务端日志（executor 对这些异常打 warning）；
        # 对外响应经 `_error_summary` 只取 status/受控标识，绝不回显上游正文。
        super().__init__(f"upstream http {status}: {body_hint(body)}")

    def kind(self) -> ErrKind:
        return self.classify_status(self.status, self.body)


class UpstreamReloginRequired(RuntimeError):
    """凭证的续期凭据已被上游作废，**程序侧无法自愈**，只能重新登录。

    一次性 refresh_token 被消费（CodeArts `STS5.1806 the refresh token has been
    used`）、绑定项不符（client_id / DPoP 私钥）、以及「刷新成功但响应里没有
    轮转后的 refresh_token」都属于此类：refresh_token 与 client_id / DPoP 私钥
    三者绑定，程序换不出新票，重试每次用的都是同一张废票。

    与「可重试失败」的区别只在处置方式上：RefreshTask 捕获它之后把凭证标成需
    重新登录并停止重试（`tasks/refresh.py`），否则表现是每轮任务刷一条同样的
    警告，而凭证其实早就死了。
    """


class UpstreamProtocolViolation(ValueError):
    """上游响应违反可映射的结构约束（非 JSON / 字段缺失 / 形状不符）。

    响应结构解析失败**不静默吞掉**。各渠道 `events.py` 现统一 `from
    provider.base import UpstreamProtocolViolation` 复用本类——此前每家有
    一份同名类，导致「按类型翻译探测失败原因」只能逐个渠道硬编码。

    `public` 是**可安全回显给客户端**的文案：异常文本会带上游原文或
    内部端点 URL，只能进日志；HTTP 处理器改用 `public_message`，缺省等于
    `str(self)`（多数文案本就不含上游数据），仅少数内嵌 URL/body 的调用点
    显式传 `public=` 收紧。
    """

    def __init__(self, message: str, *, public: str | None = None) -> None:
        super().__init__(message)
        self.public_message = message if public is None else public


class UpstreamTransportError(ConnectionError):
    """上游请求在传输层失败（连接超时 / DNS / 连接被拒 / 读超时）。

    只承载**原始传输异常**（`httpx.TransportError` 等），由各渠道客户端在
    请求处捕获后 `raise ... from error` 抛出，好让上层按类型翻译成可操作
    的原因（network_unreachable），而不是让 `httpx` 私有异常泄漏到翻译层。

    分类固定为 SOFT：传输层失败（连不上 / 写请求体超时 / 读超时）是**链路
    瞬态**，与具体账号无关，按 SOFT 短冷却、可换号重试，且不累计错误数——
    累计到阈值会把健康凭证踢出池子，反而放大了网络抖动的影响。
    """

    def kind(self) -> ErrKind:
        return ErrKind.SOFT


@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None   # 输入中命中缓存的 token（上游可选，缺省 None）
    credit: float | None = None   # 上游可选字段，两边都经常为 None
    # credit 是否为本服务推算（TRAE 按官方单价折算）。
    # True 时展示层标 ≈；上游真给 credit 的渠道恒为 False。
    credit_estimated: bool = False


@dataclass(slots=True)
class Event:
    kind: EventKind
    content: str | None = None
    tool_calls: list[dict] | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error_code: int | None = None
    error_message: str | None = None
    # 流内 error 事件的业务分类：provider 解析时按自己的错误码语义定好，
    # executor 不再维护第二份 code→kind 映射（两份必然漂移）
    error_kind: ErrKind | None = None
    # 上游点名「过多久再来」（秒）：目前只有排队信号带它。executor 据此决定
    # 在原号上等多久再重开；None 表示上游没给，按 `QUEUE_DEFAULT_WAIT_SECONDS`。
    retry_after_seconds: float | None = None
    # 上游响应的模型名（协议里有就填，缺省 None）：executor 只用来做
    # 「静默替换模型」观测，不一致时只告警、不改响应、不冷却凭证。
    model: str | None = None


def event_error_kind(event: Event) -> ErrKind:
    """中立 error 事件的业务分类，未标时按 OTHER。

    provider 解析时已按自己的错误码语义写好 `error_kind`；合成事件 / 测试桩
    未标时才落到 OTHER（「其它上游失败」），绝不猜成更重的死禁。executor 的
    流内错误处理与 `UpstreamStreamError.kind()` 共用此口径，避免两处各写一份
    兜底而漂移。
    """
    return event.error_kind if event.error_kind is not None else ErrKind.OTHER


@dataclass(slots=True)
class Quota:
    """额度探测结果。probe_failed 与「探测到 0」必须区分（三态）。"""

    remaining: float | None = None
    total: float | None = None
    cycle_end: int | None = None      # 最早到期（epoch）；无到期信息的渠道为 None
    # 到期阶梯 [(到期 epoch, 该套餐剩余积分)]：选号按"窗口内即将到期积分"排序。
    # 按包独立到期的渠道（CodeBuddy、TRAE 的权益包）有值；企业版等无此信息为 None。
    expiry_ladder: list[tuple[int, float]] | None = None
    # 额度明细包：[{"name", "total", "used", "end"}]，仅用于展示。
    # 与 expiry_ladder 分开存：后者的结构装不下名称，且两者口径不同
    # （明细额外含「已过期但仍有余额」与「已用完但仍有效」的包）。
    packages: list[dict[str, Any]] | None = None
    probed_at: int | None = None
    probe_failed: bool = False


UNKNOWN: int | None = None
EXHAUSTED = -1
"""HealthScore：0-100 已知；None 未知；-1 已耗尽。"""


def _ratio_health(remaining: float | None, total: float | None) -> int:
    """剩余/总量 → 0-100 百分比（夹紧）；总量缺失或 ≤0 判为「已耗尽」。"""
    if total is None or total <= 0:
        return EXHAUSTED
    return max(0, min(100, round((remaining or 0.0) / total * 100)))


def health_score(quota: Quota | None) -> int | None:
    """三态健康度：known(0-100) / unknown(None) / exhausted(-1)。"""
    if quota is None or quota.probe_failed:
        return UNKNOWN
    return _ratio_health(quota.remaining, quota.total)


@dataclass(slots=True)
class CheckinResult:
    """签到结果。already_checked_in 表示当日已签（不算错误）。"""

    ok: bool
    credit: float | None = None
    code: int | None = None
    message: str = ""
    already_checked_in: bool = False
    # 渠道可选回填的活动状态（CB 有；TRAE 为 None 或仅回填部分字段）。
    # 用 dict 而非具体类型：这是 provider 私有形状，中立层不该认识它。
    status: Any | None = None
    # 封账截止时刻（epoch 秒），None = 封到本地当日结束（`CheckinTask` 默认）。
    # 只有**渠道的签到周期不等于本地自然日**时才需要回填：Qoder 每日 10:00
    # （UTC+8）才换 campaign，凌晨 00:00 那轮看到的仍是上一轮窗口且已 CLAIMED，
    # 按自然日封账会把整天封掉 → 新窗口 10:00 出现后再也不签（只能人工点）。
    # 回填成「当前窗口 endAt」（= 下一轮开放的时刻）后，任务在新窗口出现时
    # 会自动再跑一轮，签到窗口每天只打一次上游。
    seal_until: int | None = None


@dataclass(slots=True)
class CheckinStatus:
    """签到活动状态。全字段可缺失：上游改版时不该因此报错。"""

    active: bool = False
    today_checked_in: bool = False
    streak_days: int | None = None
    today_credit: float | None = None
    total_credits: float | None = None
    activity_name: str = ""
    is_streak_day: bool = False


class StepStatus(StrEnum):
    """成长中心单步结果。区分 skipped（无事可做）与 failed（需要人看）。"""

    DONE = "done"
    IDLE = "idle"        # 无事可做（Buddy 还在路上 / 名额用完）——不是错误
    SKIPPED = "skipped"  # 渠道不支持 / 配置关闭
    FAILED = "failed"


@dataclass(slots=True)
class GrowthStep:
    """成长中心一个子步骤的结果；detail 是给人看的一句中文。

    reportable 控制该步是否进「一行汇报」：汇报是用户看的摘要，必须只含
    用户**能据此行动**的信息（领到什么、哪里出错了、需要他去做什么）。
    默认只收 DONE 与 FAILED；IDLE 里若有需要用户动手的事项（如「N 个任务
    需先在客户端完成前置任务」），显式置 reportable=True 带进汇报——
    否则用户只会看到「接单完成 共 1 个」，看不到还有 17 个被门住。
    """

    name: str
    status: StepStatus
    detail: str = ""
    credit: float | None = None
    reportable: bool | None = None      # None=按 status 判定（DONE/FAILED 进）


@dataclass(slots=True)
class GrowthResult:
    """成长中心一轮的结果。

    session_dead 必须与 failed 区分：前者要重新登录（调度器硬禁用），
    后者只是这一轮没领到。二者混同会让接口坏了却当成登录过期。
    """

    ok: bool = True
    steps: list[GrowthStep] = field(default_factory=list)
    credit: float | None = None        # 本轮累计获得积分
    energy: int | None = None
    streak_days: int | None = None
    report: str = ""                   # 一行中文汇报（存 events 表 / 直接展示）
    session_dead: bool = False

    @property
    def gained(self) -> bool:
        """本轮是否有实质收获（决定要不要落库）。"""
        return any(step.status == StepStatus.DONE for step in self.steps)

    @property
    def failed(self) -> list[GrowthStep]:
        return [step for step in self.steps if step.status == StepStatus.FAILED]


@dataclass(slots=True)
class Model:
    """模型条目：id/name 之外的可选元数据来自模型列表接口（有则透传）。"""

    id: str
    name: str = ""
    # 消耗倍数：CB 为 "x0.29 credits" 解析出的数值；TRAE 为 consumption_rate.rate
    credit_rate: float | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_images: bool | None = None
    supports_tool_call: bool | None = None
    # 推理元数据：两边上游的模型配置接口都直接给出，有则透传
    supports_reasoning: bool | None = None
    # 上游为该模型配置的默认档位（CB: reasoning.effort ∈ {high, medium}；
    # TRAE 的 reasoning_effort_config 只有 support_thinking 布尔，无档位，为 None）
    default_effort: str | None = None


class AuthFlow(StrEnum):
    """登录轨道类型。

    - `poll`：前端轮询 `poll_auth`，服务端监听本机回环端口
    - `callback`：浏览器 302 回本服务 `/authorize`（TRAE）
    - `paste`：用户把回调链接粘回（服务端无法监听本机回环端口，如 CodeArts）

    用枚举而非裸字符串：`"paste"` 曾被文档漏写，裸串让这类第三值悄悄漂移。
    """

    POLL = "poll"
    CALLBACK = "callback"
    PASTE = "paste"


@dataclass(slots=True)
class AuthSession:
    """登录轨道：poll 出 auth_url/interval；callback 出 callback_url；
    paste 出 auth_url 但需用户把回调链接粘回（服务端无法监听本机回环端口）。"""

    flow: AuthFlow
    state: str
    auth_url: str | None = None
    interval: int | None = None
    callback_url: str | None = None


@dataclass(slots=True)
class AuthResult:
    credential_data: dict
    nickname: str = ""


@runtime_checkable
class Provider(Protocol):
    """全部 provider 共同实现的核心接口（TECHNICAL §4）。

    `id` 是渠道标识；其余方法分三类：凭证生命周期、额度探测、执行与分类。
    签名与 `TECHNICAL.md §4` 严格一致——两处定义漂移过一次（`start_auth`
    参数、`complete_callback` 参数、缺失 `stream_chat`/`aclose`），此注释是
    防止再次漂移的锚点：改这里必须同步改 §4。
    """

    id: str

    # 凭证生命周期（上游协议私有，必然在 provider 内）
    def start_auth(self, callback_url: str) -> AuthSession: ...
    def poll_auth(self, state: str) -> AuthResult | None: ...
    async def complete_callback(self, raw_url: str, state: str) -> dict: ...
    def import_credential(self, raw: dict) -> dict: ...
    def refresh(self, credential_data: dict) -> dict: ...

    # 额度探测（调度器依赖：健康度 + 到期阶梯）
    async def probe_quota(self, credential_data: dict) -> Quota: ...

    # 执行与分类
    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]: ...
    def classify(self, status: int, body: bytes) -> ErrKind: ...
    def list_models(self, credential_data: dict) -> list[Model]: ...
    async def aclose(self) -> None: ...
