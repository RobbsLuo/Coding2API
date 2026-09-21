"""Provider 协议与中立类型（Q16=A 细接口，Q13=B 中立事件层预留）。

Provider 承担上游协议私有部分：发请求、解析事件、分类错误、凭证生命周期与额度探测。
调度、冷却、重试、统计在共享引擎（engine/）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
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
    """错误分类 → 冷却时长（Q12=B）。

    分类的粒度决定冷却的作用域：账号级（PLAN/SOFT/DEAD/OTHER）与
    模型级（MODEL/BLOCKED）必须分开，否则单个模型打满会连累同账号的
    其他模型（实测 6004 只冷却触发的那个模型）。
    """

    PLAN = "plan"      # 权益耗尽（1005）→ 12h 长冷却
    # 余额不足（402 / 14018）：等签到恢复，冷却到次日 04:00 而非固定时长
    CREDIT = "credit"
    SOFT = "soft"      # 限流/404 → 60s，不累计错误数
    DEAD = "dead"      # session 失效 → 硬禁用
    OTHER = "other"    # 其他 4xx/5xx → 累计，连续 3 次 → 10m
    INVALID = "invalid"  # 请求无效（如模型不存在）→ 不冷却凭证，直接 400 回客户端
    # 模型级限流（6004）：只冷却触发的那个模型，切其他模型立即可用
    MODEL = "model"
    # 该后端无此模型（11102）：(账号, 模型) 负缓存，重试无意义
    BLOCKED = "blocked"
    # 请求级错误（请求体坏 11101 / 上下文超限 11115 / 渠道风控 11128 /
    # 图片无效 11135）：不是账号的问题，不冷却、不累计；仍会换号重试
    REQUEST = "request"


# 冷却作用域是「(凭证, 模型)」而非整个凭证的错误类别。
# 账号级冷却必须显式清空模型级条目（见 scheduler.note_error），
# 否则上一次模型级限流的豁免会泄漏到本次账号级冷却上（换模型错误绕过）。
MODEL_SCOPED_KINDS = frozenset({ErrKind.MODEL, ErrKind.BLOCKED})


def body_hint(body: bytes, limit: int = 160) -> str:
    """上游错误响应体的单行摘要（进日志与错误文案，便于定位拒绝原因）。"""
    if not body:
        return ""
    text = body.decode("utf-8", errors="replace")
    text = " ".join(text.split())
    return text[:limit]


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


class UpstreamHTTPError(Exception):
    """上游非 2xx；两个 provider 共用同一形状（status + 原始 body）。

    `kind()` 由子类绑定各自的 classify_status，因为 1005 等业务码的
    判定规则是上游协议私有的。
    """

    classify_status: ClassVar[Callable[[int, bytes], ErrKind]]

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        super().__init__(f"upstream http {status}: {body_hint(body)}")

    def kind(self) -> ErrKind:
        return self.classify_status(self.status, self.body)


@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None   # 输入中命中缓存的 token（上游可选，缺省 None）
    credit: float | None = None   # 上游可选字段，两边都经常为 None


@dataclass(slots=True)
class Event:
    kind: EventKind
    content: str | None = None
    tool_calls: list[dict] | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error_code: int | None = None
    error_message: str | None = None


@dataclass(slots=True)
class Quota:
    """额度探测结果。probe_failed 与「探测到 0」必须区分（Q26 三态）。"""

    remaining: float | None = None
    total: float | None = None
    cycle_end: int | None = None      # 最早到期（epoch）；TRAE 为 None
    # 到期阶梯 [(到期 epoch, 该套餐剩余积分)]：选号按"窗口内即将到期积分"排序。
    # 只有按包独立到期的渠道（CodeBuddy）有值；TRAE 无周期概念为 None。
    expiry_ladder: list[tuple[int, float]] | None = None
    # 额度明细包：[{"name", "total", "used", "end"}]，仅用于展示。
    # 与 expiry_ladder 分开存：后者是调度排序指标（TRAE 刻意为 None），
    # 塞入纯展示数据会改变选号行为，且其结构装不下名称。
    packages: list[dict[str, Any]] | None = None
    probed_at: int | None = None
    probe_failed: bool = False


UNKNOWN: int | None = None
EXHAUSTED = -1
"""HealthScore：0-100 已知；None 未知；-1 已耗尽。"""


def health_score(quota: Quota | None) -> int | None:
    """三态健康度：known(0-100) / unknown(None) / exhausted(-1)。"""
    if quota is None or quota.probe_failed:
        return UNKNOWN
    if quota.total is None or quota.total <= 0:
        return EXHAUSTED
    remaining = quota.remaining or 0.0
    return max(0, min(100, round(remaining / quota.total * 100)))


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


@dataclass(slots=True)
class AuthSession:
    """flow=poll 用 auth_url/interval；flow=callback 用 callback_url。Q17=C 双轨。"""

    flow: str                       # "poll" | "callback"
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
    id: str

    def start_auth(self) -> AuthSession: ...
    def poll_auth(self, state: str) -> AuthResult | None: ...
    def complete_callback(self, url: str) -> AuthResult: ...
    def import_credential(self, raw: dict) -> dict: ...
    def refresh(self, credential_data: dict) -> dict: ...
    async def probe_quota(self, credential_data: dict) -> Quota: ...
    def classify(self, status: int, body: bytes) -> ErrKind: ...
    def list_models(self, credential_data: dict) -> list[Model]: ...
