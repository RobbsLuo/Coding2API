"""模型可用性视图与主动探活：`GET /api/models/availability` + 探活/接管端点。
**回答的问题**：「最近 N 天哪些 (渠道, 模型) 在真实请求里被判成『上游不认这个
模型』？」——此前只有首次请求才学到的 BLOCKED 负缓存（
`credential_model_cooldowns.reason='blocked'`），没有汇总视图，也就没有
「谁下线了」的看板与恢复入口。

口径：

* 数据源是 `usage_events` 的**明细**（小时汇总没有 error_type），窗口由
  `since` 绝对时刻限定，走 `idx_usage_ts`；`days` 默认 7、上限 30，`limit`
  上限 200——聚合必须限窗口与行数，否则一张跑了一年的库会被这一个只读端点
  拖垮。
* 只认 `error_type in ('upstream_blocked', 'model_not_found', 'invalid_request')`：
  `model_not_found` 是 executor 对「上游明确说没有这个模型」（`ErrKind.BLOCKED`，
  2026-10-26 从 `invalid_request` 里独立出来）的归类，`upstream_blocked` 是协议
  预留值（provider 直接上报时自动纳入），`invalid_request` 是请求无效
  （`ErrKind.INVALID` / `ErrKind.REQUEST`，上游 400 拒绝也落这里）。
* `blocked` 是**现算**的：拿 `MODEL_BLOCKLIST`（热更）与记录里的模型名
  做 glob 匹配，与 `/v1/models` 的过滤同口径（归一键 / 展示名任一命中即算）。
  不在服务端缓存，改完黑名单下一次请求立即反映。

主动探活（`model_probes` 表 + `ModelProbeTask`）：

* 真实请求的失败只能「证明我这次没打通」，不能区分「上游真没有这个模型」与
  「额度耗尽 / 限流 / 网络抖动」。探活用一条最小对话把这两类分开，因此三态
  判定里 `inconclusive` **绝不**触发禁用。
* 探活与生产完全隔离：直连 provider，不写 `usage_events`、不动冷却表、不改
  健康度，结论只进独立的 `model_probes` 表。唯一的出口影响是**列表展示**：
  判死（`failed`）且负缓存仍生效的 (渠道, 模型) 从 `/v1/models` 里摘掉，
  到期口径是那条负缓存的 `cooling_until`（见 `api.models.merged_entries`）。
* 探活**成功**才是恢复入口：它清掉该 (渠道, 模型) 的 blocked 负缓存，模型
  立即回到池子，不必等 6–24h 的 TTL 走完。
* 人工接管（`manual != ''`）优先于自动探活：管理员点过「恢复」之后不希望
  后台又把同一结论纠回去。

目录页（`admin_model_catalog`）复用同一份聚合结果给每行补 `suspected`
字段：两边都用 `model_match.match_keys` 的候选键求交集，OpenRouter 的
`z-ai/glm-5.3` 与网关侧的 `glm-5.3` 才能对上。
"""

from __future__ import annotations

import time
from fnmatch import fnmatch
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..audit.actions import ACTION_MODEL_PROBE, ACTION_MODEL_PROBE_MANUAL
from ..auth.rbac import require_operator
from ..compat.openai.request import InvalidRequest
from ..model_match import match_keys
from ..provider.naming import normalize_model_key
from ..tasks.model_probe import ModelProbeTask
from .deps import Services, csrf_protected, principal_from_request

# 窗口：默认 7 天、上限 30 天（`usage_events` 只保留 90 天，30 天足够判断
# 「最近是不是坏了」，再宽只是把早已恢复的历史失败翻出来）。
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 30

# 返回行数上限：按失败次数降序，超出即截断并标记 `truncated`（前端提示
# 「只看前 N 条」，而不是静默给半份表）。
DEFAULT_LIMIT = 100
MAX_LIMIT = 200

# 计入「模型级失败」的 error_type（见模块 docstring）。
FAILURE_ERROR_TYPES = ("upstream_blocked", "model_not_found", "invalid_request")


def _blocked(model: str, patterns: tuple[str, ...]) -> bool:
    """模型名是否命中当前黑名单（与 `/v1/models` 的过滤同口径）。

    记录里的 `model` 是请求用的对外名（归一键 / 展示名 / 别名），故同时拿
    原样与归一键去比；两边都忽略大小写（用户照展示名写 `Kimi K3` 与照
    归一键写 `kimi-k3` 应等价命中）。
    """
    names = {model, normalize_model_key(model)} - {""}
    return any(fnmatch(name.lower(), pattern.lower())
               for name in names for pattern in patterns)


def failure_rows(services: Services, *, days: int, limit: int = DEFAULT_LIMIT,
                 now: float | None = None) -> dict[str, Any]:
    """聚合窗口内的模型级失败；`days` / `limit` 已由调用方钳制。

    返回 `{rows, since, days, truncated}`。每行带 `keys`（`model_match` 候选键），
    供目录页求交集；对外端点会把 `keys` 去掉（内部匹配细节不外泄，也避免前端
    误把它当稳定契约）。
    """
    since = int((time.time() if now is None else now) - days * 86400)
    patterns = tuple(getattr(services.settings, "blocklist_patterns", ()) or ())
    raw = services.stats_query.model_failures(since=since, limit=limit + 1)
    truncated = len(raw) > limit
    rows: list[dict[str, Any]] = []
    for item in raw[:limit]:
        rows.append({
            "provider": item["provider"],
            "model": item["model"],
            "failures": item["failures"],
            "last_failed_at": item["last_failed_at"],
            "blocked": _blocked(item["model"], patterns),
            "keys": sorted(match_keys(item["model"])),
        })
    return {"rows": rows, "since": since, "days": days, "truncated": truncated}


def suspected_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """聚合行 → `{候选键: 疑似下线信息}`（同一键多条时取失败次数最多的一条）。

    目录页用候选键求交集，因此这里把每行的 `keys` 摊平成索引。同一键对应多条
    (渠道, 模型) 时只保留失败最多的一条：页面只需要「这条 OpenRouter 模型有
    问题、最坏是哪个渠道」，逐渠道明细留给 `/api/models/availability`。
    """
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        info = {"provider": row["provider"], "model": row["model"],
                "failures": row["failures"], "last_failed_at": row["last_failed_at"],
                "blocked": row["blocked"]}
        for key in row["keys"]:
            existing = index.get(key)
            if existing is None or info["failures"] > existing["failures"]:
                index[key] = info
    return index


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/models/availability")
    async def get_model_availability(days: int = DEFAULT_WINDOW_DAYS,
                                     limit: int = DEFAULT_LIMIT,
                                     principal=Depends(principal_from_request)):
        """最近 N 天模型级失败聚合（只读，operator 门槛）。

        `days` 钳到 `[1, 30]`、`limit` 钳到 `[1, 200]`：越界值不报错而是收窄
        ——这是看板端点，前端缓存里的旧参数不该把它打成 400。
        """
        require_operator(principal)
        window = max(1, min(MAX_WINDOW_DAYS, int(days)))
        capped = max(1, min(MAX_LIMIT, int(limit)))
        result = failure_rows(services, days=window, limit=capped)
        return {
            "window_days": result["days"],
            "since": result["since"],
            "rows": [{key: value for key, value in row.items() if key != "keys"}
                     for row in result["rows"]],
            "truncated": result["truncated"],
            "error_types": list(FAILURE_ERROR_TYPES),
        }

    @router.get("/api/models/probes")
    async def list_model_probes(principal=Depends(principal_from_request)):
        """探活结论 + 当前仍生效的 blocked 负缓存（operator 门槛）。

        一次返回两份数据是为了让页面不必自己拼：`probes` 回答「上次探活什么
        时候、结论如何」，`blocked` 回答「现在还被避让的是哪些」——只看前者
        会漏掉「从未探过但正被负缓存锁着」的模型（那才是该点探活的）。
        `blocked` 按 (渠道, 模型) 聚合，`credentials` 是该结论涉及的号数
        （负缓存是逐号的，结论是模型级的）。

        **命中黑名单的行整条不返回**（口径同 `/v1/models` 的过滤）：黑名单是
        管理员「这个模型我不要了」的显式决定，它已经把自己排除在模型列表之外，
        再在探活面板里冒出来只会诱导管理员反复去救一个已主动弃用的模型。
        黑名单现读热更值，故点掉黑名单后下一次请求立即重新露出。
        """
        require_operator(principal)
        patterns = tuple(getattr(services.settings, "blocklist_patterns", ()) or ())
        rows = services.credentials.blocked_cooldowns()
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["provider"], row["model"])
            if _blocked(row["model"], patterns):
                continue
            item = grouped.setdefault(key, {
                "provider": row["provider"], "model": row["model"],
                "credentials": 0, "until": 0, "hits": 0,
            })
            item["credentials"] += 1
            item["until"] = max(item["until"], row["cooling_until"])
            item["hits"] = max(item["hits"], row["hits"])
        probes = services.model_probes.all()
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for key, item in grouped.items():
            merged[key] = {**item, **(probes.get(key) or {"state": "", "failures": 0,
                                                          "last_probe_at": 0,
                                                          "last_ok_at": 0,
                                                          "last_error": "", "manual": ""})}
        # 有人工接管标记但负缓存已被清掉的行也要露出（否则「恢复」之后那一行
        # 就从页面上消失了，用户无法把它交还给自动探活）。
        for key, probe in probes.items():
            if key in merged or _blocked(key[1], patterns):
                continue
            merged[key] = {"provider": key[0], "model": key[1], "credentials": 0,
                           "until": 0, "hits": 0, **probe}
        items = sorted(merged.values(), key=lambda item: (
            item["state"] != "failed", -int(item.get("until") or 0),
            item["provider"], item["model"]))
        return {"rows": items}

    @router.post("/api/models/probes/probe")
    async def probe_model(payload: dict, request: Request,
                          _csrf: None = Depends(csrf_protected),
                          principal=Depends(principal_from_request)):
        """立即探活一个 (渠道, 模型)（operator + CSRF + 审计）。

        真发一次最小对话，成功即清该渠道的 blocked 负缓存。这是「人工恢复」
        的显式入口：模型已恢复但 TTL 还没到（最长 24h）时，用户不必干等。
        """
        require_operator(principal)
        provider, model = _probe_target(payload)
        task = _probe_task(request)
        state, error = await task.probe_now(provider, model)
        services.audit.record(
            actor=principal.username, action=ACTION_MODEL_PROBE,
            target=f"{provider}/{model}", detail=f"state={state} {error}".strip())
        return {"provider": provider, "model": model, "state": state, "error": error}

    @router.post("/api/models/probes/manual")
    async def take_over(payload: dict, request: Request,
                        _csrf: None = Depends(csrf_protected),
                        principal=Depends(principal_from_request)):
        """人工接管一个 (渠道, 模型)（operator + CSRF + 审计）。

        `manual` 取值：`on`（接管，自动探活永久跳过）或 `off`（交还自动探活）。
        接管**不**改负缓存：用户可能只是不想让后台反复探这个模型（比如它是
        确认下线的），而不是要立刻用它。要立刻恢复请走 `/probe`。
        """
        require_operator(principal)
        provider, model = _probe_target(payload)
        manual = str(payload.get("manual") or "").strip()
        if manual not in ("on", "off"):
            raise InvalidRequest("manual must be 'on' or 'off'")
        _probe_task(request)  # 装配校验：探活任务不存在时早失败
        services.model_probes.set_manual(provider=provider, model=model,
                                         manual="manual" if manual == "on" else "")
        services.audit.record(actor=principal.username,
                              action=ACTION_MODEL_PROBE_MANUAL,
                              target=f"{provider}/{model}", detail=f"manual={manual}")
        return {"provider": provider, "model": model, "manual": manual == "on"}

    return router


def _probe_target(payload: dict) -> tuple[str, str]:
    """从请求体取 (渠道, 模型)；两者都是必填字符串。"""
    provider = str(payload.get("provider") or "").strip()
    model = str(payload.get("model") or "").strip()
    if not provider or not model:
        raise InvalidRequest("provider and model are required")
    return provider, model


def _probe_task(request: Request) -> ModelProbeTask:
    """取装配好的探活任务（与后台循环同一个实例，共用 provider 与仓储）。"""
    task = getattr(request.app.state, "model_probe_task", None)
    if task is None:  # pragma: no cover - 生产路径恒装配；测试注入空 registry 时才有
        raise InvalidRequest("model probe task not configured")
    return task
