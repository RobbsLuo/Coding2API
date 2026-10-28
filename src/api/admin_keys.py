"""API Key 管理：列出 / 创建 / 改配额 / 删除（明文只在创建时返回一次）。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from ..audit.actions import (
    ACTION_API_KEY_CREATE,
    ACTION_API_KEY_DELETE,
    ACTION_API_KEY_QUOTA,
)
from ..auth.access import normalize_allowed_ips, normalize_allowed_models
from ..compat.openai.request import InvalidRequest
from ..engine.limits import day_start
from ..engine.model_resolver import KNOWN_PROVIDERS
from ..key_quota import KeyQuota, parse_key_quota, quota_text
from .deps import Services, csrf_protected, principal_from_request, request_ip


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


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    api_keys = services.api_keys

    @router.get("/api/api-keys")
    async def list_keys(sort: str | None = None, order: str | None = None,
                        limit: int = 50, offset: int = 0,
                        principal=Depends(principal_from_request)):
        # 分页：total 供前端算页数
        rows = api_keys.list_for(principal.username, sort=sort, order=order,
                                 limit=limit, offset=offset)
        # 列表「当日已用」：一次批量聚合（不是逐行查库）。只算当前页的 Key，
        # 口径与配额判定同一自然日（北京时间 0 点起）。
        usage_map = services.key_usage.map_for(
            [row["id"] for row in rows], since=day_start(int(time.time())))
        for row in rows:
            used = usage_map.get(row["id"])
            row["usage_today"] = {
                "requests": used.requests if used else 0,
                "tokens": used.tokens if used else 0,
                "cost_usd": round(used.cost_usd, 6) if used else 0.0,
            }
        return {"api_keys": rows,
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
