"""管理台凭证运维：CRUD / toggle / pin / probe / checkin / 成长中心 / 账号切换 / 批量。

探测失败原因翻译（describe_probe_failure）在此，因为只有这里返回探测结果。

批量端点（`POST /api/credentials/batch`）与单条端点共用同一组动作实现
（`probe_credential_once` / `checkin_credential_once`），避免「批量版」与
「单条版」两套逻辑各自漂移——比如探测失败的 reason 翻译只在单条版修好。
"""

from __future__ import annotations

import asyncio
import logging
import math

import httpx
from fastapi import APIRouter, Depends

from ..audit.actions import (
    ACTION_CREDENTIAL_DELETE,
    ACTION_CREDENTIAL_IMPORT,
    ACTION_CREDENTIAL_LIMITS,
    ACTION_CREDENTIAL_PIN,
    ACTION_CREDENTIAL_REVIVE,
    ACTION_CREDENTIAL_SWITCH_ACCOUNT,
    ACTION_CREDENTIAL_TAGS,
    ACTION_CREDENTIAL_TOGGLE,
)
from ..auth.rbac import require_operator
from ..compat.openai.request import InvalidRequest
from ..provider.base import GrowthResult, GrowthStep, StepStatus
from ..sorting import sort_rows
from ..tags import parse_tags
from .deps import Services, csrf_protected, principal_from_request

logger = logging.getLogger(__name__)

# 账号切换列表排序：API 键 → 取值函数（昵称缺失排最后）。
ACCOUNT_SORT_KEYS = {
    "nickname": lambda row: row["nickname"] or None,
    "account_id": lambda row: row["account_id"],
    "type": lambda row: row["type"],
}

# 批量端点支持的动作。刻意只收「已有单条端点」的动作：
# - `refresh`（token 预刷新）没有单条入口，且一次性 refresh_token 由后台任务
#   按 skew 窗口驱动，批量强刷会白耗票据；
# - `delete` 破坏性太强，需要单独的二次确认流，不走通用批量通道。
BATCH_ACTIONS = frozenset({"probe", "toggle", "checkin", "revive"})

# 单次批量的上限：一次点「全部探测」最多打多少个上游。上限存在的原因是
# 探测/签到都会真的出网，没有上限时前端一个「全选」就能把整池凭证的请求
# 同时排进事件循环。
BATCH_MAX_IDS = 200

# 批量的在途并发上限。后台任务用 Pacer 严格串行化上游请求，但那是跨任务
# 节流；批量端点由操作员手动触发，若完全串行则 50 个号要等几分钟才返回。
# 折中：限制同时在途的凭证数，既不打爆上游，也不至于让请求长时间挂住。
BATCH_CONCURRENCY = 4

# 批量动作 → 审计动作。探测 / 签到是只读语义（与单条端点一致，不写审计），
# 只有真正改动凭证状态的 toggle / revive 落一条汇总流水。
# 写入本身有兜底：审计落库失败只记 warning，不让整批操作返回 500
# （逐条结果比留痕更重要，且变更此时已提交生效）。
BATCH_AUDIT_ACTIONS = {
    "toggle": ACTION_CREDENTIAL_TOGGLE,
    "revive": ACTION_CREDENTIAL_REVIVE,
}


def _optional_limit(payload: dict, key: str, *, integer: bool = False):
    """限额字段解析：缺省 / null / ≤0 一律当「关闭」，非数值报 400。"""
    if payload.get(key) is None:
        return None
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequest(f"{key} must be a number")
    if not math.isfinite(value):
        raise InvalidRequest(f"{key} must be finite")
    number = int(value) if integer else float(value)
    return number if number > 0 else None


def _per_model_limits(payload: dict) -> dict[str, int]:
    """按模型 token 上限：{模型名: 上限}；空值 / ≤0 的项直接丢弃。"""
    raw = payload.get("per_model_tokens")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidRequest("per_model_tokens must be an object")
    out: dict[str, int] = {}
    for model, cap in raw.items():
        name = str(model).strip()
        if not name:
            continue
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) \
                or not math.isfinite(cap):
            raise InvalidRequest(f"per_model_tokens[{name}] must be a number")
        cap_int = int(cap)
        if cap_int > 0:
            out[name] = cap_int
    return out


def _limits_detail(limits: dict) -> str:
    """限额审计明细：一行可读短句（空配置=清除）。"""
    parts: list[str] = []
    if limits["keep_credits"]:
        parts.append(f"保留积分 {limits['keep_credits']:g}")
    if limits["daily_tokens"]:
        parts.append(f"每日 token {limits['daily_tokens']}")
    if limits["daily_credits"]:
        parts.append(f"每日积分 {limits['daily_credits']:g}")
    if limits["per_model_tokens"]:
        models = ", ".join(f"{m}:{c}" for m, c in sorted(limits["per_model_tokens"].items()))
        parts.append(f"按模型 {models}")
    return "；".join(parts) or "清除限额"


def describe_probe_failure(error: Exception) -> str:
    """把探测异常翻译成用户能据以行动的原因。

    不能直接暴露 Python 类名（如 UpstreamProtocolViolation）——那是实现细节，
    用户看到它既判断不出问题，也不知道下一步该做什么。

    按**基类**分派：所有渠道的 `UpstreamHTTPError` 都继承
    `provider.base.UpstreamHTTPError`，`UpstreamProtocolViolation` 也已收归
    `provider.base`，故这里无需逐渠道 import——新增渠道自动被覆盖，不会像
    早先那样只因漏 import 某家就退化成 `unknown_error`。
    """
    from ..provider.base import UpstreamHTTPError, UpstreamProtocolViolation

    if isinstance(error, UpstreamHTTPError):
        status = getattr(error, "status", 0)
        if status in (401, 403):
            return "credential_rejected"      # 凭证失效，需要重新登录
        if status == 429:
            return "rate_limited"             # 上游限流，稍后再试
        if status >= 500:
            return "upstream_unavailable"     # 上游故障，与凭证无关
        return "upstream_rejected"            # 上游拒绝该请求
    if isinstance(error, UpstreamProtocolViolation):
        return "upstream_response_invalid"    # 响应结构不符，可能是上游改版
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        # 连接阶段失败：DNS 解析失败 / 连不上域名 / 连接被拒 / 建连超时。
        # 注意 `ConnectTimeout` 是 `TimeoutException` 而非 `ConnectError`
        # 子类，故两类都要在这里列出。这是「网络不可达」，与「连上了但
        # 读超时」的用户动作不同
        return "network_unreachable"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "upstream_timeout"             # 已连上但响应超时
    if isinstance(error, (httpx.TransportError, OSError)):
        return "network_unreachable"          # 其余传输层失败（读中断 / TLS 中止）
    return "unknown_error"


async def probe_credential_once(credentials, provider, data, credential_id: str) -> dict:
    """探测一条凭证并把结果写回额度 / 健康度，返回对外响应体。

    单条与批量端点共用这一份实现：探测失败是**正常结果**而不是异常——
    降级为 `{"probed": False, "reason": ...}`，调用方（单条端点）照旧回 200。
    两处各写一份时，`describe_probe_failure` 的修正只落在一处，另一处继续
    把 Python 类名当 reason 吐给用户。
    """
    try:
        quota = await provider.probe_quota(data)
    except Exception as error:  # noqa: BLE001 - 探测失败 → unknown，不当作 0
        credentials.mark_probe_failed(credential_id)
        reason = describe_probe_failure(error)
        logger.info("额度探测失败 %s: %s", credential_id, reason)
        # detail 只在**服务端日志**里带上游原文（可能是 2048 字的响应体摘要）；
        # 对外只回受控 reason，避免上游回显请求头/正文时泄露凭证片段。
        logger.info("额度探测失败详情 %s: %s", credential_id, str(error)[:200])
        return {"probed": False, "reason": reason}
    credentials.save_quota(credential_id, quota)
    return {"probed": True, "remaining": quota.remaining, "total": quota.total,
            "cycle_end": quota.cycle_end}


async def checkin_credential_once(credentials, provider, data, credential_id: str,
                                  schedule_probe) -> dict:
    """签到一条凭证，返回对外响应体（单条与批量共用）。"""
    result = await provider.checkin(data)
    if result.ok and not result.already_checked_in:
        schedule_probe(credential_id)      # 只有真签到了才会发积分
    # already_checked_in 必须透传：前端靠它区分「刚签到」与「今天已签过」
    return {"ok": result.ok, "credit": result.credit, "code": result.code,
            "message": result.message,
            "already_checked_in": result.already_checked_in,
            # 渠道可选的活动状态（连续天数/今日积分）；无此能力的渠道为 None
            "status": result.status}


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    credentials = services.credentials
    registry = services.registry
    schedule_probe = services.schedule_probe

    def provider_for(credential_id: str, capability: str | None = None,
                     error: str | None = None):
        """取凭证对应的上游 provider 与其凭证数据；缺失/不支持即 400。

        集中了 7 个 handler 共用的三行前导（provider_of → registry.get →
        credential_data）。以前逐份复写，漏掉任一判空就会把 None 传进上游
        调用，报错从「凭证不存在」退化成难以理解的 AttributeError。

        `capability` 为 None 时文案是 "credential not found"（探测端点），
        否则是 "credential does not support {capability}"；`error` 可覆盖后者，
        用于两处对外文案与能力名不一致的端点（"checkin status" / "growth center"
        / "activity report" / "account switching"）。这些串是既有对外契约。
        """
        provider = registry.get(credentials.provider_of(credential_id) or "")
        data = credentials.credential_data(credential_id)
        # 能力检查用 getattr(...) is None 而非 hasattr：CodeBuddy 国际站以
        # 类属性 `growth = None` 显式声明「不支持」，hasattr 会放它过去。
        if provider is None or data is None or (
                capability is not None and getattr(provider, capability, None) is None):
            raise InvalidRequest(
                error or ("credential not found" if capability is None
                          else f"credential does not support {capability}"))
        return provider, data

    @router.get("/api/credentials")
    async def list_credentials(sort: str | None = None, order: str | None = None,
                               principal=Depends(principal_from_request)):
        window = services.settings.quota_expiry_window_seconds
        secondary = services.settings.quota_expiry_secondary_window_seconds
        rows = credentials.list_all(
                    expiring_window=window, expiring_secondary_window=secondary,
                    sort=sort, order=order,
                    # 昵称来自共享池（常含邮箱/手机）：仅 admin/operator 可见
                    include_nickname=principal.is_operator)
        # 限额配置按凭证附带（缺行 = null = 不受限）：管理台据此显示护栏与
        # 编辑入口；吊销/未配置的渠道不额外占字段。
        limits_map = services.limits.map()
        for row in rows:
            record = limits_map.get(row["id"])
            row["limits"] = record.as_dict() if record is not None else None
        return {"credentials": rows,
                "expiry_window_seconds": window,
                "expiry_secondary_window_seconds": secondary,
                # 到期预警阈值：前端进度条与 <1h 红字同源，避免两处各写一个数
                "token_expiry_warning_seconds":
                    services.settings.token_expiry_warning_seconds,
                "viewer": principal.username,
                "is_admin": principal.is_admin,
                # 凭证写门槛（admin 或 operator）：前端据此决定是否显示
                # 导入/启停/删除等写操作，与后端 require_operator 同一口径。
                "is_operator": principal.is_operator}

    @router.post("/api/credentials")
    async def import_credential(payload: dict,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_operator(principal)
        provider_id = str(payload.get("provider") or "")
        if provider_id not in registry:
            raise InvalidRequest(f"unknown provider {provider_id!r}")
        credential_data = registry[provider_id].import_credential(
            payload.get("credential") or {})
        try:
            # 导入时可选带标签（列表页「添加」对话框不填即为空）
            tags = parse_tags(payload.get("tags"))
        except ValueError as error:
            raise InvalidRequest(str(error)) from error
        credential_id = credentials.add(provider=provider_id,
                                        credential_data=credential_data,
                                        nickname=str(payload.get("nickname") or ""),
                                        added_by=principal.username, tags=tags)
        logger.info("管理员 %s 新增凭证 %s（上游 %s）", principal.username, credential_id,
                    provider_id)
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_IMPORT,
                              target=credential_id, detail=f"上游 {provider_id}")
        schedule_probe(credential_id)
        return {"id": credential_id}

    @router.post("/api/credentials/{credential_id}/toggle")
    async def toggle_credential(credential_id: str, payload: dict,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_operator(principal)
        if not credentials.set_enabled(credential_id, bool(payload.get("enabled", True))):
            raise InvalidRequest("credential not found")
        logger.info("管理员 %s 开关凭证 %s -> %s", principal.username, credential_id,
                    payload.get("enabled", True))
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_TOGGLE,
                              target=credential_id,
                              detail=f"enabled={bool(payload.get('enabled', True))}")
        return {"ok": True}

    @router.post("/api/credentials/{credential_id}/revive")
    async def revive_credential(credential_id: str,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        """解除硬禁用/冷却，让重新登录后的凭证回到池子。"""
        require_operator(principal)
        if not credentials.revive(credential_id):
            raise InvalidRequest("credential not found")
        logger.info("管理员 %s 恢复了凭证 %s", principal.username, credential_id)
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_REVIVE,
                              target=credential_id)
        return {"ok": True}

    @router.put("/api/credentials/{credential_id}/limits")
    async def set_credential_limits(credential_id: str, payload: dict,
                                    _csrf: None = Depends(csrf_protected),
                                    principal=Depends(principal_from_request)):
        """设置/清除一条凭证的限额护栏（整体替换：缺省字段=关闭该护栏）。

        四个护栏全关时（提交空配置 / 全 0）删除配置行，凭证回到「不受限」。
        写入后 `limit_guard.invalidate()` 让新值立即参与选号，不等快照 TTL。
        """
        require_operator(principal)
        if credentials.provider_of(credential_id) is None:
            raise InvalidRequest("credential not found")
        limits = {
            "keep_credits": _optional_limit(payload, "keep_credits"),
            "daily_tokens": _optional_limit(payload, "daily_tokens", integer=True),
            "daily_credits": _optional_limit(payload, "daily_credits"),
            "per_model_tokens": _per_model_limits(payload),
        }
        services.limits.set(credential_id, updated_by=principal.username, **limits)
        services.limit_guard.invalidate()
        logger.info("管理员 %s 设置凭证 %s 限额: %s", principal.username, credential_id,
                    _limits_detail(limits))
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_LIMITS,
                              target=credential_id, detail=_limits_detail(limits))
        record = services.limits.get(credential_id)
        return {"limits": record.as_dict() if record is not None else None}

    @router.put("/api/credentials/{credential_id}/tags")
    async def set_credential_tags(credential_id: str, payload: dict,
                                  _csrf: None = Depends(csrf_protected),
                                  principal=Depends(principal_from_request)):
        """整体替换一条凭证的标签（空列表 = 清空）。

        标签只影响列表展示与筛选，不参与调度/鉴权，因此不触发任何缓存失效
        （与限额护栏不同：那个要 `limit_guard.invalidate()`）。
        """
        require_operator(principal)
        try:
            tags = parse_tags(payload.get("tags"))
        except ValueError as error:
            raise InvalidRequest(str(error)) from error
        if not credentials.set_tags(credential_id, tags):
            raise InvalidRequest("credential not found")
        logger.info("管理员 %s 设置凭证 %s 标签: %s", principal.username, credential_id,
                    ",".join(tags) or "（清空）")
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_TAGS,
                              target=credential_id, detail=",".join(tags) or "（清空）")
        return {"tags": tags}

    @router.post("/api/credentials/pin")
    async def pin_credential(payload: dict,
                             _csrf: None = Depends(csrf_protected),
                             principal=Depends(principal_from_request)):
        require_operator(principal)
        pin = payload.get("credential_id")
        # 类型收口：非字符串直接 400。放任 dict/list 进 set_pinned 会走到
        # sqlite3 的参数绑定 → ProgrammingError → 500（客户端输入不该是 5xx）。
        # None = 取消指定（合法），其余必须是字符串。
        if pin is not None and not isinstance(pin, str):
            raise InvalidRequest("credential_id must be a string or null")
        # 存在性校验：不存在的 id 会被 set_pinned 的「先清全部 pin」顺带解除
        # 其它凭证的指定，而端点仍回 200——管理员以为已指定，实则把指定清空了。
        if not credentials.set_pinned(pin):
            raise InvalidRequest("credential not found")
        logger.info("管理员 %s 固定凭证 %s", principal.username, pin)
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_PIN,
                              target=pin,
                              detail=f"pinned={pin!r}")
        return {"ok": True}

    @router.delete("/api/credentials/{credential_id}")
    async def delete_credential(credential_id: str,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_operator(principal)
        if not credentials.delete(credential_id):
            raise InvalidRequest("credential not found")
        logger.info("管理员 %s 删除凭证 %s", principal.username, credential_id)
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_DELETE,
                              target=credential_id)
        return {"ok": True}

    @router.post("/api/credentials/{credential_id}/probe")
    async def probe_credential(credential_id: str,
                               _csrf: None = Depends(csrf_protected),
                               principal=Depends(principal_from_request)):
        require_operator(principal)
        provider, data = provider_for(credential_id)
        return await probe_credential_once(credentials, provider, data, credential_id)

    @router.post("/api/credentials/{credential_id}/checkin")
    async def checkin_credential(credential_id: str,
                                 _csrf: None = Depends(csrf_protected),
                                 principal=Depends(principal_from_request)):
        require_operator(principal)
        provider, data = provider_for(credential_id, "checkin")
        return await checkin_credential_once(credentials, provider, data,
                                             credential_id, schedule_probe)

    @router.post("/api/credentials/batch")
    async def batch_credentials(payload: dict,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        """批量运维：对多条凭证执行同一动作，返回逐条结果。

        与「前端循环调单条端点」的区别不只是省往返：单条端点任一条失败就
        中断整轮，批量端点**逐条隔离**——一条凭证失效（不存在 / 渠道不支持 /
        上游报错）不影响其余条，前端能一次性看到「成功 N / 失败 M」与失败原因。

        `toggle` 的 `enabled` 由请求体显式给出，不做「各自取反」：混选「已启用
        + 已暂停」时取反会让一半号走向相反方向，而操作员的意图只有一个。
        """
        require_operator(principal)
        action = str(payload.get("action") or "")
        if action not in BATCH_ACTIONS:
            raise InvalidRequest(f"unknown batch action {action!r}")
        raw_ids = payload.get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise InvalidRequest("ids must be a non-empty list")
        # 去重保序：前端「先单选再全选」会带重复项，重复探测同一凭证纯属浪费
        ids: list[str] = []
        seen: set[str] = set()
        for item in raw_ids:
            credential_id = str(item).strip()
            if credential_id and credential_id not in seen:
                seen.add(credential_id)
                ids.append(credential_id)
        if not ids:
            raise InvalidRequest("ids must be a non-empty list")
        if len(ids) > BATCH_MAX_IDS:
            raise InvalidRequest(f"too many credentials (max {BATCH_MAX_IDS})")
        enabled = bool(payload.get("enabled", True))
        semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def run_one(credential_id: str) -> dict:
            try:
                if action == "toggle":
                    if not credentials.set_enabled(credential_id, enabled):
                        return {"id": credential_id, "ok": False, "error": "credential not found"}
                    return {"id": credential_id, "ok": True}
                if action == "revive":
                    if not credentials.revive(credential_id):
                        return {"id": credential_id, "ok": False, "error": "credential not found"}
                    return {"id": credential_id, "ok": True}
                # probe / checkin 会真的出网：在途上限只约束这两类动作。
                # toggle / revive 是本地 DB 写，无上游压力，不受信号量限制。
                async with semaphore:
                    if action == "probe":
                        provider, data = provider_for(credential_id)
                        detail = await probe_credential_once(
                            credentials, provider, data, credential_id)
                        return {"id": credential_id, "ok": bool(detail["probed"]),
                                "detail": detail}
                    provider, data = provider_for(credential_id, "checkin")
                    detail = await checkin_credential_once(
                        credentials, provider, data, credential_id, schedule_probe)
                    return {"id": credential_id, "ok": bool(detail["ok"]), "detail": detail}
            except InvalidRequest as error:
                # 凭证不存在 / 渠道不支持该能力：可预期的逐条失败，原因直接可读
                return {"id": credential_id, "ok": False, "error": str(error)}
            except Exception as error:  # noqa: BLE001 - 单条失败不拖垮整批
                # 上游异常文本可能含凭证片段，只记服务端日志，对外回受控原因
                logger.info("批量 %s 失败 %s: %s", action, credential_id, str(error)[:200])
                return {"id": credential_id, "ok": False, "error": "action_failed"}

        results = await asyncio.gather(*(run_one(credential_id) for credential_id in ids))
        succeeded = sum(1 for result in results if result["ok"])
        # 审计只记一条汇总，不逐条落库：批量暂停 50 个号是一次操作、一个意图，
        # 50 行审计会把流水刷成噪声，反而更难定位「谁在什么时候批量做了什么」。
        # toggle 带上方向：暂停与取消暂停是同一动作名，不写方向就看不出意图。
        direction = f"（{'启用' if enabled else '暂停'}）" if action == "toggle" else ""
        audit_action = BATCH_AUDIT_ACTIONS.get(action)
        if audit_action is not None:
            # 审计落库失败**不得**吞掉逐条结果：`results` 是操作员唯一的反馈
            # （成功 N / 失败 M 与失败原因），而写审计只是留痕。二者是两件事，
            # 且此时逐条变更已经提交生效，返回 500 会让操作员以为整批没跑。
            try:
                services.audit.record(
                    actor=principal.username, action=audit_action, target=None,
                    detail=f"批量 {action}{direction}：{succeeded}/{len(results)} 成功")
            except Exception as error:  # noqa: BLE001 - 留痕失败不该毁掉操作结果
                logger.warning("批量 %s 审计写入失败: %s", action, error)
        logger.info("管理员 %s 批量 %s %d 个凭证（成功 %d）", principal.username, action,
                    len(results), succeeded)
        return {"action": action, "total": len(results), "succeeded": succeeded,
                "failed": len(results) - succeeded, "results": results}

    @router.get("/api/credentials/{credential_id}/checkin")
    async def checkin_status(credential_id: str,
                             principal=Depends(principal_from_request)):
        """只读查询签到状态（连续天数 / 今日是否已签）；不产生任何写入。"""
        require_operator(principal)
        provider, data = provider_for(
            credential_id, "checkin_status",
            error="credential does not support checkin status")
        return {"status": await provider.checkin_status(data)}

    @router.get("/api/credentials/{credential_id}/growth")
    async def growth_history(credential_id: str, limit: int = 20,
                             principal=Depends(principal_from_request)):
        """成长中心历史：最近几轮的一行汇报（仅 CodeBuddy 有该活动）。"""
        require_operator(principal)
        if services.credentials.provider_of(credential_id) is None:
            raise InvalidRequest("credential not found")
        return {"events": services.growth_events.recent(credential_id, limit=limit)}

    @router.get("/api/credentials/{credential_id}/credit-events")
    async def credit_events(credential_id: str, limit: int = 20, sort: str | None = None,
                            order: str | None = None, ledger: str = "pool",
                            principal=Depends(principal_from_request)):
        """变动流水：两次额度探测之间的净变化（倒序）。

        `ledger` 选账本：`pool`（默认）＝套餐/每日池余额，`credits`＝CodeArts 积分。
        两本账量级与语义都不同，必须分开读，混在一起会被当成同一本账的连续
        读数。未知取值由仓储回落 `pool`（与老库默认列值一致）。

        刻意不返回「来源」枚举：上游不打日志，diff 看到的只是区间净变化，
        写「签到 +5」就是把猜测当事实。source 只表达归因已知度
        （observed=常规区间 / sync=首次建立基线）。
        """
        require_operator(principal)
        if services.credentials.provider_of(credential_id) is None:
            raise InvalidRequest("credential not found")
        return {"events": services.credit_events.recent(
            credential_id, limit=limit, sort=sort, order=order, ledger=ledger)}

    @router.post("/api/credentials/{credential_id}/growth")
    async def run_growth(credential_id: str,
                         _csrf: None = Depends(csrf_protected),
                         principal=Depends(principal_from_request)):
        """手动跑一轮成长中心：与定时任务同一条路径，结果同样落库。

        不可逆动作（抽奖/兑换/开盲盒/补登卡）遵循与定时任务相同的配置开关：
        手动入口不另设开关，否则「保守部署」只挡得住定时任务。
        """
        require_operator(principal)
        provider, data = provider_for(
            credential_id, "growth",
            error="credential does not support growth center")
        growth = provider.growth
        result = await growth(
            data, allow_irreversible=services.settings.growth_irreversible_actions)
        credentials.save_growth_result(credential_id, result.report)
        services.growth_events.record(credential_id=credential_id, result=result,
                                      trigger="manual")
        logger.info("管理员 %s 手动执行成长中心 %s（ok=%s）", principal.username,
                    credential_id, result.ok)
        return {"ok": result.ok, "report": result.report, "credit": result.credit,
                "energy": result.energy, "streak_days": result.streak_days,
                "session_dead": result.session_dead,
                "steps": [{"name": step.name, "status": step.status, "detail": step.detail,
                           "credit": step.credit} for step in result.steps]}

    @router.post("/api/credentials/{credential_id}/activity")
    async def run_activity(credential_id: str,
                           _csrf: None = Depends(csrf_protected),
                           principal=Depends(principal_from_request)):
        """手动补发一条活跃上报：与定时任务同一条路径。

        独立于 activity_report_enabled 开关：定时任务默认关闭不影响管理员在
        管理台手动补报一次（用途就是部署后验证闭环）。结果记一行 growth_events。
        """
        require_operator(principal)
        provider, data = provider_for(
            credential_id, "activity",
            error="credential does not support activity report")
        activity = provider.activity
        result = await activity(data)
        if result.ok:
            detail = result.message or "已上报一条对话事件"
            services.growth_events.record(
                credential_id=credential_id, trigger="manual",
                result=GrowthResult(
                    report=f"活跃上报：{detail}",
                    steps=[GrowthStep("活跃上报", StepStatus.DONE, detail)]))
        logger.info("管理员 %s 手动活跃上报 %s（ok=%s）", principal.username,
                    credential_id, result.ok)
        return {"ok": result.ok, "message": result.message}

    @router.get("/api/credentials/{credential_id}/accounts")
    async def list_credential_accounts(credential_id: str, sort: str | None = None,
                                       order: str | None = None,
                                       principal=Depends(principal_from_request)):
        require_operator(principal)
        provider, data = provider_for(
            credential_id, "list_accounts",
            error="credential does not support account switching")
        accounts = await provider.list_accounts(data)
        rows = [{"account_id": a.account_id, "nickname": a.nickname,
                 "type": a.account_type} for a in accounts]
        # 默认按昵称升序（上游返回序无意义）；缺失昵称排最后（见 src/sorting.py）
        rows = sort_rows(rows, sort, order, ACCOUNT_SORT_KEYS,
                         default_key="nickname", default_desc=False)
        return {"accounts": rows}

    @router.post("/api/credentials/{credential_id}/accounts/select")
    async def select_credential_account(credential_id: str, payload: dict,
                                        _csrf: None = Depends(csrf_protected),
                                        principal=Depends(principal_from_request)):
        require_operator(principal)
        provider, _ = provider_for(
            credential_id, "switch_account",
            error="credential does not support account switching")
        # 编辑锁：与 RefreshTask 的「读 → await → 整份覆写」互斥，否则
        # 刷新会把刚切好的账号覆盖回旧值（反之亦然）。
        async with credentials.edit_lock(credential_id):
            data = credentials.credential_data(credential_id)
            if data is None:
                raise InvalidRequest("credential does not support account switching")
            switched = await provider.switch_account(
                data, str(payload.get("account_id") or ""))
            credentials.save_credential_data(credential_id, switched)
        logger.info("管理员 %s 切换凭证 %s 账号 -> %s", principal.username, credential_id,
                    payload.get("account_id"))
        services.audit.record(actor=principal.username,
                              action=ACTION_CREDENTIAL_SWITCH_ACCOUNT,
                              target=credential_id,
                              detail=f"账号 -> {payload.get('account_id')}")
        # 账号切换后额度对应的是新账号，必须重探测而不是沿用旧值
        schedule_probe(credential_id)
        return {"switched": True}

    return router
