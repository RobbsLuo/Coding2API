"""CodeBuddy SSE → 中立事件映射。

上游是 OpenAI 风格 SSE（data: {...choices[].delta...}），以 data: [DONE] 结束。
宽松解析：不在事件层做完整协议校验（AGENTS.md 约束），
但结构非法（JSON 坏、非对象、choices 非数组）必须显式失败而非静默。
"""

from __future__ import annotations

from typing import Any

from ...engine.sse import SSEFrame
from ...provider.base import (
    ErrKind,
    Event,
    UpstreamProtocolViolation,  # noqa: F401 - credential/client 经本模块导入
    Usage,
    body_hint,
    business_codes,
)
from ..openai_chunk import openai_event, openai_events, payload_of, usage_from_openai


def parse_frame(frame: SSEFrame) -> Event | None:
    """单帧 → 中立事件；[DONE] 与无 delta 的帧返回 None。"""
    payload = payload_of(frame, "CodeBuddy")
    return None if payload is None else openai_event(payload, usage=_usage)


def parse_all_events(frame: SSEFrame) -> list[Event]:
    """一帧拆成多个事件：带 usage 的收尾帧同时给出 finish_reason 与 usage。"""
    return openai_events(frame, "CodeBuddy", usage=_usage)


def _usage(raw: dict[str, Any]) -> Usage:
    return usage_from_openai(raw, credit=True)


_MODEL_RATE_LIMIT_CODE = 6004       # 该模型用量超限 → 模型级软冷却
# 该模型当前饱和 → 模型级软冷却（与 6004 同类，同样只罚 (凭证, 模型)）。
# 国际版实测原文：displayMsg「Model busy. Please switch models or retry later」
# （zh「模型繁忙，请换模型或稍后重试」）、displayTips「This model is currently
# saturated」。上游明确要求「switch models」，说明同凭证其他模型可用，
# 落账号级 SOFT 会让整张凭证 60s 内全部模型一起不可用。
_MODEL_SATURATED_CODE = 14003
# 「这个号用不了这个模型」→ (账号, 模型) 负缓存。两个业务码同义：
#   11102 该后端无此模型（上游根本没部署）
#   11158 套餐不允许：`model [gpt-6-luna] is not allowed for user type
#         [personal_free_user]`（国际站免费号实测 2026-10-29 10:36 / 10:37）
# 两者对**当前凭证**的结论完全一样——重试多少次都失败，换号才有意义，且与
# 账号健康无关（不能落账号级冷却）。此前只认 11102，11158 掉进 `400 其他`
# 被归成 `invalid_request`，归因面板显示「请求无效」，读起来像客户端参数
# 写错了（与 2026-10-26 officeAce 81009 同一类误判）。
# 列表探活（client.filter_available_models）共用本集合，口径不会再漂。
MODEL_BLOCKED_CODES = frozenset({11102, 11158})
_CREDITS_EXHAUSTED_CODE = 14018     # 积分耗尽 → 冷却到次日签到
_PLAN_EXHAUSTED_CODE = 1005         # 权益/套餐耗尽
# 请求级错误（不是账号的问题）：请求体坏 / 上下文超限 / 图片无效 / 渠道风控。
# 11128「Illegal API invocation from an unapproved channel」实测是窗口型瞬时
# 拦截：同凭证同时刻换模型即可成功、同 (凭证, 模型) 秒级交替成功/失败、窗口
# 内自愈，绝不是凭证或模型的问题。归请求级 → 零动作、仅换号重试。
# （若归 INVALID，executor 会跳过该上游全部凭证、零重试直接 400）
_REQUEST_LEVEL_CODES = frozenset({11101, 11115, 11128, 11135})
_BAD_PARAMS_PHRASE = "unmarshal chat params failed"
# 账号级风控限制（403 + `"code":11140`「request illegal」）：displayMsg 是
# `Your account has been restricted due to unusual activity` /
# 「账号存在异常行为，已被限制使用」，displayTips 引导 SUBMIT_FEEDBACK。
# 与 session 过期不同：重新登录无效，只能申诉或换号。
#
# 业务码与文案分开存：`"11140"` **不做整段正文子串匹配**——request id、时间戳、
# 用户回显内容里出现这五个数字就会把普通错误判成 RESTRICTED（2h 冷却、两次即
# 升级整渠道 24h 暂停）。正文只认三语文案（上游点名人类可读文案时给的兜底）。
_ACCOUNT_RESTRICTED_CODE = 11140
_ACCOUNT_RESTRICTED_PHRASES: tuple[str, ...] = (
    "account has been restricted",
    "账号存在异常行为",
    "帳號存在異常行為",
)


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """HTTP 状态码 + body 业务码分类。

    判定顺序即优先级，改动前先确认没把更具体的一类遮住（6004 与 429
    的先后就是一处：6004 只冷却触发模型，落进 SOFT 会误伤整个账号）：

    * 402 / 14018 → 余额不足（冷却到次日 04:00，等签到恢复）
    * 1005 → 权益耗尽（12h 长冷却）
    * 11102/11158「该号用不了这个模型」→ (账号, 模型) 负缓存（400/404 形态）
    * 11140「账号被风控限制」→ 账号级长冷却，**当前请求不再换号**
    * 401/403 → session 失效（硬禁用）
    * 404 → 软冷却，不累计错误数
    * 429 + 6004/14003 → 模型级限流（用量超限 / 模型饱和）；429 其他 → 账号级软限流
    * 400 + 11101/11115/11128/11135 → 请求级错误：不罚号，但仍换号
    * 400 其他 → 请求无效（模型不存在等），不冷却凭证
    """
    codes = business_codes(body.decode("utf-8", errors="replace"))
    if status == 402 or _CREDITS_EXHAUSTED_CODE in codes:
        return ErrKind.CREDIT
    if _PLAN_EXHAUSTED_CODE in codes:
        return ErrKind.PLAN
    if status in (400, 404) and codes & MODEL_BLOCKED_CODES:
        return ErrKind.BLOCKED
    if status >= 400 and _is_account_restricted(body):
        # 必须拦在 401/403 → DEAD 之前：11140 是上游按账号/IP 拉黑，不是凭据
        # 失效——判 DEAD 会永久硬禁用并让 executor 逐个号换着撞（实测一次
        # 请求连打 3 个号全 403，新号 3 分钟后也被封）。
        return ErrKind.RESTRICTED
    if status in (401, 403):
        return ErrKind.DEAD
    if status == 404:
        return ErrKind.SOFT
    if status == 429:
        return (ErrKind.MODEL
                if _MODEL_RATE_LIMIT_CODE in codes or _MODEL_SATURATED_CODE in codes
                else ErrKind.SOFT)
    if status == 400:
        if (codes & _REQUEST_LEVEL_CODES
                or _BAD_PARAMS_PHRASE in body_hint(body).lower()):
            # 发给上游的 body 有问题：换号也大概率一样，但不罚号（仍轮转试一次）
            return ErrKind.REQUEST
        # 请求无效（如模型不存在）是客户端错误，冷却凭证只会误伤健康凭证
        return ErrKind.INVALID
    return ErrKind.OTHER


def _is_account_restricted(body: bytes) -> bool:
    """响应体是不是 CodeBuddy 的「账号被风控限制」（11140 / 三语文案）。

    业务码走 `business_codes`（只认 `"code": N` 键值形态）而不是整段正文子串：
    正文里任何位置出现 `11140` 都算的话，一次普通 400/5xx（body 回显用户内容
    或带 request id）就会误判 RESTRICTED → 该凭证 2h 冷却、两次即整渠道 24h
    暂停。文案匹配是上游只给人类可读文案时的兜底，同样不认裸数字。
    """
    text = body.decode("utf-8", errors="replace")
    if _ACCOUNT_RESTRICTED_CODE in business_codes(text):
        return True
    lowered = text.lower()
    return any(phrase in lowered for phrase in _ACCOUNT_RESTRICTED_PHRASES)


def disabled_reason(body: bytes = b"") -> str:
    """401/403 响应体 → 硬禁用时展示的可操作原因；识别不到返回空串。

    为什么需要它：`disabled_reason` 原先一律写 "session dead"，而 11140 的
    真实含义是「账号被上游风控限制」——管理台按 "session dead" 去重新登录
    只会白折腾，实际动作是提交申诉或换号。这里只回**受控文案**，不拼接上游
    正文（与 `relogin_required` 同口径）。
    """
    if _is_account_restricted(body):
        return "账号被上游限制（账号异常行为），需提交申诉；重新登录无效"
    return ""
