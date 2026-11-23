"""API Key 管理：列出 / 创建 / 编辑策略 / 改配额 / 删除（明文只在创建时返回一次）。"""

from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import APIRouter, Depends, Request

from ..audit.actions import (
    ACTION_API_KEY_CREATE,
    ACTION_API_KEY_DELETE,
    ACTION_API_KEY_QUOTA,
    ACTION_API_KEY_UPDATE,
)
from ..auth.access import normalize_allowed_ips, normalize_allowed_models
from ..compat.openai.request import InvalidRequest
from ..db.key_update import EDITABLE_COLUMNS, update_api_key_fields
from ..engine.limits import day_start
from ..engine.model_resolver import KNOWN_PROVIDERS
from ..key_quota import KeyQuota, parse_key_quota, quota_text
from .deps import Services, csrf_protected, principal_from_request, request_ip

# 列表「窗口用量」：值是窗口覆盖的**自然日**数（含今天）。日界与配额判定同一
# 口径（北京时间 0 点，`day_start`）——`today` 只是 N=1 的退化情形，不引入
# 第二套边界（滚动 N×86400 会在「今天 00:30 查 7 天」时把 7 天前的半天算进来）。
USAGE_WINDOWS: dict[str, int] = {"today": 1, "7d": 7, "30d": 30}


def _parse_binding(raw: object) -> str:
    """校验渠道绑定：空 = 自动；否则必须是已注册渠道。"""
    binding = str(raw or "").strip().lower()
    if binding and binding not in KNOWN_PROVIDERS:
        raise InvalidRequest(
            f"provider_binding must be empty or one of {', '.join(KNOWN_PROVIDERS)}")
    return binding


def _parse_allowed_ips(raw: object) -> str:
    """校验并规范化来源 IP 白名单（写入时校验，读取路径只做匹配）。"""
    try:
        return normalize_allowed_ips(str(raw or ""))
    except ValueError as error:
        raise InvalidRequest(str(error)) from error


def _parse_allowed_models(raw: object) -> str:
    """校验并规范化模型白名单（fnmatch glob，逗号分隔，空 = 不限制）。"""
    try:
        return normalize_allowed_models(str(raw or ""))
    except ValueError as error:
        raise InvalidRequest(str(error)) from error


def _parse_expires_at(raw: object) -> int | None:
    """校验到期时间：null/空 = 永不过期；否则必须是正的 epoch 秒。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise InvalidRequest("expires_at must be a unix timestamp in seconds or null")
    value = int(raw)
    if value <= 0:
        raise InvalidRequest("expires_at must be a positive unix timestamp")
    return value


def _parse_quota(raw: object) -> KeyQuota:
    """校验 Key 配额（dict 或 JSON 文本；空 = 不限制），错误归一为 400。"""
    try:
        return parse_key_quota(raw)
    except ValueError as error:
        raise InvalidRequest(str(error)) from error


def _quota_detail(quota: KeyQuota) -> str:
    """审计明细：一行可读短句（全关 = 清除配额）。"""
    labels = (("daily_requests", "每日请求"), ("daily_tokens", "每日 token"),
              ("daily_cost_usd", "每日成本 USD"), ("rpm", "RPM"),
              ("tpm", "TPM"), ("max_concurrency", "并发"))
    parts = [f"{label} {getattr(quota, name):g}"
             for name, label in labels if getattr(quota, name)]
    return "；".join(parts) or "清除配额"


def _policy_detail(name: str, binding: str, allowed_ips: str,
                   allowed_models: str, expires_at: int | None,
                   quota: KeyQuota) -> str:
    """创建审计明细：一行可读摘要，回答「这把 Key 是谁、能干什么」。

    Key 明文只在创建响应里返回一次，事后追责只能靠审计行——不含策略摘要的
    `api_key.create` 记录回答不了「这把能刷无限制模型的 Key 是谁建的」。
    """
    parts = [f"名称 {name or '(未命名)'}"]
    if binding:
        parts.append(f"绑定渠道 {binding}")
    if allowed_ips:
        parts.append(f"来源 IP {allowed_ips}")
    if allowed_models:
        parts.append(f"模型白名单 {allowed_models}")
    parts.append(f"到期 {expires_at}" if expires_at else "永不过期")
    parts.append(_quota_detail(quota))
    return "；".join(parts)


# 编辑接口的字段解析器：与创建共用同一套校验/规范化（渠道绑定小写化、IP
# 网段收敛、模型名去重、到期时间正数化），两处口径不会漂移。
_KEY_FIELD_PARSERS: dict[str, Callable[[object], object]] = {
    "name": lambda raw: str(raw or ""),
    "provider_binding": _parse_binding,
    "allowed_ips": _parse_allowed_ips,
    "allowed_models": _parse_allowed_models,
    "expires_at": _parse_expires_at,
}

# 审计 detail 里的字段名与「空值」文案：`''` 直接打印读不出含义
# （`来源 IP → ` 到底是不限制还是没改？）。
_FIELD_LABELS: dict[str, str] = {
    "name": "名称",
    "provider_binding": "绑定渠道",
    "allowed_ips": "来源 IP",
    "allowed_models": "模型白名单",
    "expires_at": "到期",
}
_FIELD_EMPTY_TEXT: dict[str, str] = {
    "name": "(未命名)",
    "provider_binding": "自动",
    "allowed_ips": "不限制",
    "allowed_models": "不限制",
    "expires_at": "永不过期",
}


def _field_text(field: str, value: object) -> str:
    """审计 detail 的单字段展示形态（空值给语义化文案）。"""
    if field == "expires_at":
        return f"到期 {value}" if value else _FIELD_EMPTY_TEXT[field]
    return str(value or "") or _FIELD_EMPTY_TEXT[field]


def _change_detail(before: dict, changes: dict) -> list[tuple[str, str]]:
    """实际变化的字段 → `[(字段名, 一行可读 diff)]`。

    值没变的字段不算变化：「名称 laptop → laptop」这种记录只会把真实改动淹没。
    """
    detail: list[tuple[str, str]] = []
    for field in EDITABLE_COLUMNS:
        if field not in changes or changes[field] == before.get(field):
            continue
        detail.append((field, f"{_FIELD_LABELS[field]} "
                              f"{_field_text(field, before.get(field))} → "
                              f"{_field_text(field, changes[field])}"))
    return detail


def _usage_since(window: str, now: int) -> int:
    """窗口起点：含今天在内的最近 N 个自然日（北京时间 0 点起，`today` 即 N=1）。

    `day_start` 是 UTC+8 固定偏移（无夏令时），因此「前 N-1 天」直接减
    `(N-1) * 86400` 秒，不会跨时区算错。
    """
    days = USAGE_WINDOWS.get(window)
    if days is None:
        raise InvalidRequest(f"window must be one of {', '.join(USAGE_WINDOWS)}")
    return day_start(now) - (days - 1) * 86400



def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    api_keys = services.api_keys

    @router.get("/api/api-keys")
    async def list_keys(sort: str | None = None, order: str | None = None,
                        limit: int = 50, offset: int = 0,
                        window: str = "today",
                        principal=Depends(principal_from_request)):
        # 分页：total 供前端算页数
        rows = api_keys.list_for(principal.username, sort=sort, order=order,
                                 limit=limit, offset=offset)
        # 列表「窗口用量」：一次批量聚合（不是逐行查库）。只算当前页的 Key，
        # 口径与配额判定同一自然日边界（北京时间 0 点起）；窗口只是把起点
        # 往前挪 N-1 天，明细保留 90 天，30d 完全在留存范围内。
        since = _usage_since(window, int(time.time()))
        usage_map = services.key_usage.map_for(
            [row["id"] for row in rows], since=since)
        for row in rows:
            used = usage_map.get(row["id"])
            row["usage"] = {
                "requests": used.requests if used else 0,
                "tokens": used.tokens if used else 0,
                "cost_usd": round(used.cost_usd, 6) if used else 0.0,
            }
        return {"api_keys": rows, "window": window,
                "total": api_keys.count_for(principal.username)}

    @router.post("/api/api-keys")
    async def create_key(payload: dict, request: Request,
                         _csrf: None = Depends(csrf_protected),
                         principal=Depends(principal_from_request)):
        # 先解析再创建：写库前校验全部通过，避免半成品行（create 是一次 INSERT，
        # 但配额/白名单校验失败会让请求 400 而 Key 已经发出去）。
        binding = _parse_binding(payload.get("provider_binding"))
        allowed_ips = _parse_allowed_ips(payload.get("allowed_ips"))
        allowed_models = _parse_allowed_models(payload.get("allowed_models"))
        expires_at = _parse_expires_at(payload.get("expires_at"))
        quota = _parse_quota(payload.get("quota"))
        created = api_keys.create(
            principal.username, str(payload.get("name") or ""),
            provider_binding=binding, allowed_ips=allowed_ips,
            allowed_models=allowed_models, expires_at=expires_at,
            quota=quota_text(quota))
        # 发 Key 是「造了一把进网关的凭据」，必须留痕：明文只返回这一次，
        # 事后只能靠审计行回答「这把能刷全模型的 Key 是谁建的、什么时候」。
        services.audit.record(
            actor=principal.username, action=ACTION_API_KEY_CREATE,
            target=created["id"],
            detail=_policy_detail(str(payload.get("name") or ""), binding,
                                  allowed_ips, allowed_models, expires_at, quota),
            ip=request_ip(request, services.settings))
        return created          # 明文只在此返回一次

    @router.put("/api/api-keys/{key_id}")
    async def update_key(key_id: str, payload: dict, request: Request,
                         _csrf: None = Depends(csrf_protected),
                         principal=Depends(principal_from_request)):
        """按字段增量更新一把 Key 的策略（名称 / 渠道绑定 / 两个白名单 / 到期时间）。

        **「未提供」与「显式清空」**：字段不在请求体里 = 保持原值；在请求体里
        给空串（`expires_at` 给 `null`）= 清空该项（名称清空、绑定回到自动、
        白名单恢复不限制、永不过期）。刻意不用 `None` 当「未提供」的哨兵——
        `null` 本身就是 `expires_at` 的合法清空值，两者在 JSON 上无法区分，
        只能逐字段 `in payload` 判断。

        改的是鉴权路径**每次现读**的那几列，因此改完下一个请求即生效：鉴权
        (`ApiKeyRepository.authenticate`) 不缓存策略。若将来给鉴权加缓存，
        必须在这里同步失效，否则撤销白名单要等缓存过期。

        明文 Key 与 id 不可改：明文只在创建响应里返回一次，id 是审计归属；
        换策略不需要「删键重建」（重建会签发新明文并让旧 Key 立即失效）。
        """
        # 先取原值：既做存在性/归属校验，也是审计 diff 的 before 侧。
        row = next((item for item in api_keys.list_for(principal.username)
                    if item["id"] == key_id), None)
        if row is None:
            raise InvalidRequest("api key not found")
        unknown = sorted(set(payload) - set(EDITABLE_COLUMNS))
        if unknown:
            # 静默忽略会让「用 PUT 改配额」看起来成功了：明确指回专用端点。
            raise InvalidRequest(
                f"不可编辑的字段: {', '.join(unknown)}"
                f"（可编辑: {', '.join(EDITABLE_COLUMNS)}；"
                "配额请用 PUT /api/api-keys/{key_id}/quota）")
        changes = {field: _KEY_FIELD_PARSERS[field](payload[field])
                   for field in EDITABLE_COLUMNS if field in payload}
        diffs = _change_detail(row, changes)
        if not diffs:
            # 没提供字段 / 提供了但值没变：不写库、不留审计（空改动记录只会
            # 把真实变更淹没在审计流水里）。
            return {"ok": True, "changed": []}
        # 并发删除的竞态兜底：上面的 SELECT 到 UPDATE 之间 Key 可能已被删。
        if not update_api_key_fields(api_keys, key_id, principal.username, changes):
            raise InvalidRequest("api key not found")
        services.audit.record(
            actor=principal.username, action=ACTION_API_KEY_UPDATE, target=key_id,
            detail="；".join(text for _, text in diffs),
            ip=request_ip(request, services.settings))
        return {"ok": True, "changed": [field for field, _ in diffs]}

    @router.put("/api/api-keys/{key_id}/quota")
    async def set_key_quota(key_id: str, payload: dict,
                            _csrf: None = Depends(csrf_protected),
                            principal=Depends(principal_from_request)):
        """整体替换一把 Key 的配额（空配置 / 全 0 = 清除配额）。

        只改 `api_keys.quota` 一列；鉴权读路径每次请求现读该列，因此改完
        **下一请求**即生效（速率类窗口计数不重置——已占用的名额仍按原样
        滑出窗口，这与「调高限额」的预期一致）。
        """
        quota = _parse_quota(payload.get("quota"))
        if not api_keys.set_quota(key_id, principal.username, quota_text(quota)):
            raise InvalidRequest("api key not found")
        services.audit.record(actor=principal.username, action=ACTION_API_KEY_QUOTA,
                              target=key_id, detail=_quota_detail(quota))
        return {"ok": True, "quota": quota.as_dict()}

    @router.delete("/api/api-keys/{key_id}")
    async def delete_key(key_id: str, request: Request,
                         _csrf: None = Depends(csrf_protected),
                         principal=Depends(principal_from_request)):
        # 先捞名字（删完就查不到了）：审计行只留 id，事后对不上「被删的是哪把」。
        deleted_name = ""
        for row in api_keys.list_for(principal.username):
            if row["id"] == key_id:
                deleted_name = row["name"]
                break
        if not api_keys.delete(key_id, principal.username):
            raise InvalidRequest("api key not found")
        services.audit.record(
            actor=principal.username, action=ACTION_API_KEY_DELETE, target=key_id,
            detail=f"名称 {deleted_name or '(未命名)'}",
            ip=request_ip(request, services.settings))
        return {"ok": True}

    return router
