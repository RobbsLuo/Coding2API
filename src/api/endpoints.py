"""三个 /v1 兼容出口（chat / responses / messages）共用的请求骨架。

三者只差「入站映射」与「出口翻译」两处，其余逐行相同：读体 → 解析 →
模型白名单 → 流式（preflight + translator）或非流式（转换形状）。此前每份
都复写这十几行，改一处（如白名单报错文案、preflight 时机）要改三遍，且容易
漏改导致某个出口的 400 语义与其他不一致。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.access import model_allowed
from ..compat.openai.request import ChatRequest, InvalidRequest
from ..config import Settings
from ..engine.compress import estimate_messages
from ..engine.executor import Executor, StreamSink
from ..engine.sse import SSE_COMMENT
from ..key_quota import Lease, QuotaExceeded
from ..runtime_settings import RuntimeSettings
from .deps import ApiKeyPrincipal, get_services, read_json_body
from .streaming import respond_with_keepalive, with_keepalive


def ensure_model_allowed(chat_request: ChatRequest, principal: ApiKeyPrincipal,
                         settings: Settings | RuntimeSettings) -> None:
    """模型白名单校验：不通过即 400（三个出口与 count_tokens 同一口径）。"""
    ensure_model_allowed_for(chat_request, principal.allowed_models, settings,
                             subject="this api key")


def ensure_model_allowed_for(chat_request: ChatRequest, allowed_models: str,
                             settings: Settings | RuntimeSettings, *,
                             subject: str) -> None:
    """白名单校验的共用实现：错误文案里的主体（Key / 用户）由调用方给。

    两种主体共用同一判据：`api_keys.allowed_models`（外部 Key）与
    `users.allowed_models`（管理台 Playground 的会话直连）。分开写两份实现必然
    漂移——其中一份漏了「省略模型名即默认模型」的绕过口子，白名单就等于没有。
    """
    if not model_allowed(chat_request.model, allowed_models,
                         default_model=settings.default_model):
        raise InvalidRequest(
            f"model {chat_request.model!r} is not allowed for {subject}")


def estimated_tokens(chat_request: ChatRequest) -> int:
    """转发前的 token 预估：输入启发式估算 + 输出上限（TPM 判定用）。

    真实用量要等响应结束才知道，而 TPM 必须在转发前裁决；这里取「输入估算 +
    客户端给的最大输出」，偏保守（宁可少放一个请求，也不让同一分钟超发）。
    """
    output = 0
    for key in ("max_tokens", "max_completion_tokens"):
        value = chat_request.raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            output = max(output, value)
    return int(estimate_messages(chat_request.messages)) + output


def quota_exceeded_response(error: QuotaExceeded) -> JSONResponse:
    """429：`type` 固定为 `quota_exceeded`（客户端据此区分限流与额度耗尽）。

    刻意不走 `error_payload`：那个把 `type` 固定成 `api_error`，而这里需要
    让 OpenAI 兼容客户端一眼看出「是 Key 的配额，不是服务故障」。带
    `Retry-After`：当日配额给到下个北京时间 0 点的秒数，速率类给到窗口滑出。
    """
    return JSONResponse(
        status_code=429,
        content={"error": {"type": "quota_exceeded", "message": str(error)}},
        headers={"Retry-After": str(error.retry_after)},
    )


async def dispatch_compat(
        executor: Executor, settings: Settings | RuntimeSettings,
        request: Request, principal: ApiKeyPrincipal, *,
        parse: Callable[[Any], ChatRequest],
        translator_for: Callable[[str], StreamSink | None] | None = None,
        convert: Callable[[dict[str, Any]], Any] | None = None,
        on_body: Callable[[Any], None] | None = None,
) -> JSONResponse | StreamingResponse:
    """读体 → 解析 → 白名单 → 按 stream 分派到流式或非流式出口。

    `translator_for(model)` 造出口协议的中立事件翻译器（chat 出口为 None，
    即用默认 chat 形状）；`convert(completion)` 把非流式 completion 转成该
    协议的响应体（chat 出口为 None，直接回 completion）；`on_body` 是解析前
    的原始体钩子（chat 出口用它做诊断落盘）。
    """
    body = await read_json_body(request)
    if on_body is not None:
        on_body(body)
    chat_request = parse(body)
    ensure_model_allowed(chat_request, principal, settings)
    binding = principal.provider_binding
    # 配额准入：读体 + 白名单之后、executor 之前。放在这里而不是鉴权依赖里，
    # 是因为 TPM 需要预估 token（要解析后的请求体）；放在 executor 里则会让
    # 三个出口各写一遍。超限返回 429，**不消耗**任何上游调用。
    try:
        lease = _admit_quota(request, principal, chat_request)
    except QuotaExceeded as error:
        return quota_exceeded_response(error)
    if chat_request.stream:
        # 前置校验：在 200 响应头发出前拒绝不可能成功的请求（否则只能断流）
        try:
            target = executor.preflight(chat_request, binding)
        except Exception:
            lease.release()          # 400 不该占着并发名额
            raise
        translator = translator_for(target.model) if translator_for else None
        # 心跳帧随出口协议走：Anthropic 要 `event: ping` 具名事件（只解析
        # `event:` 行的客户端会忽略注释帧），chat / Responses 保持注释帧
        # （Responses 无 ping 事件，未知事件类型会让严格解析器报错）。
        keepalive_frame = (translator.keepalive() if translator is not None
                           else SSE_COMMENT)
        frames = with_keepalive(executor.stream_guarded(chat_request,
                                                        username=principal.username,
                                                        translator=translator,
                                                        provider_binding=binding),
                                keepalive_frame=keepalive_frame)
        return StreamingResponse(
            _release_when_done(frames, lease), media_type="text/event-stream")
    # 非流式保活（`nonstream_keepalive_seconds`，默认 0 = 关闭）：宽限期内
    # 完成仍是逐字节相同的 JSONResponse，超时才转 chunked 吐空白心跳。
    keepalive = float(getattr(settings, "nonstream_keepalive_seconds", 0) or 0)
    if keepalive > 0:
        return await respond_with_keepalive(
            lambda: executor.complete(chat_request, username=principal.username,
                                      provider_binding=binding),
            grace=keepalive, convert=convert, release=lease.release)
    try:
        completion = await executor.complete(chat_request, username=principal.username,
                                             provider_binding=binding)
    finally:
        lease.release()
    return JSONResponse(convert(completion) if convert else completion)


def _admit_quota(request: Request, principal: ApiKeyPrincipal,
                 chat_request: ChatRequest) -> Lease:
    """按 Key 配额准入；超限抛 `QuotaExceeded`，放行返回 Lease（不受限时不计数）。

    不受限的 Key（配额全关）也返回 Lease——`counted=False`，`release()` 是
    空操作，调用方不必到处判空。
    """
    return get_services(request).key_quota.admit(
        principal.key_id, principal.quota, tokens=estimated_tokens(chat_request))


async def _release_when_done(frames, lease: Lease):
    """流式收尾归还并发名额：客户端断开 / 正常结束 / 异常都要释放。"""
    try:
        async for frame in frames:
            yield frame
    finally:
        lease.release()
