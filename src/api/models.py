"""模型列表服务层：拉取 / 缓存 / TTL / SWR，供 `/v1/models` 与 playground 共用。

归一 / 合并 / 命名规则在 `api.model_merge`（纯函数，无 Services 依赖）；本模块
只管这些规则的**生命周期**：何时打上游、缓存与落盘、别名表的 publish 时机。

**按凭证加载**：只处理「当前有可用凭证（selectable，即未暂停且未硬禁用）」
的渠道——从未接入、全部暂停或会话失效的渠道**既不拉取也不展示**，避免把
根本打不通的上游模型混进列表。冷启动时只有 zen / kilo（各自带虚拟凭证）在列，等
用户在管理台接入 CodeBuddy / TRAE 后，下一次列表请求（该渠道无缓存）才会拉取。

带服务层缓存：某上游拉取成功后把归一结果写入 services.model_list_cache；
下次该上游拉取失败时用缓存兜底，保证 /v1/models 稳定返回完整列表
（冷启动无缓存时才退化为跳过该上游）。缓存另有一份**落盘快照**
（`model_catalog.json`，见 `api/model_catalog.py`），启动时同步读回并立即
publish 别名表——否则启动预热跑完之前别名表是空的，扁平名请求无法收窄候选，
会真实打一轮不认该模型的上游。TTL 记的是**上次尝试**时间（成功或
失败都记）：失败不记时间戳的话，上游一次抖动就会让其后的每次 /v1/models 都
重跑一遍拉取（zen 探活最慢可占十几秒），把列表请求打成一串超时。**连续失败
再按 2^(n-1) 拉长重试间隔**（封顶 `MODEL_LIST_BACKOFF_MAX_SECONDS`）：上游被
长期封禁（zen 免费层按 IP 拦）时每 5 分钟重探一次纯属刷屏，且日志只在首次
失败打 WARNING、其后降 INFO（见 `_refresh_provider`）。

**缓存存的是未过滤列表，过滤在每个出口现做**：MODEL_BLOCKLIST 是可热更项
（「改完立即生效」），若把过滤结果存进缓存，改完黑名单要等 TTL（300s）
才反映到 Playground，且被滤掉的模型在 TTL 内会从「兜底缓存」里复活。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..benchmarks import BenchmarkTable
from ..compat.openai.errors import error_payload
from ..engine.reasoning import parse_effort_map
from ..model_alias import parse_model_aliases
from ..pricing import PriceTable
from .deps import ApiKeyPrincipal, Services, api_key_user
from .model_catalog import load_catalog, save_catalog
from .model_merge import (
    _build_aliases,
    _entry_response,
    _merge_provider,
    _sort_key,
    _visible,
)

logger = logging.getLogger(__name__)

# 模型列表 TTL：TTL 内直接复用上次结果。客户端（IDE/Playground）打开面板
# 就会调 /v1/models，无 TTL 时每次都会向上游真实发起请求。
# 时间戳记「上次尝试」（含失败）：失败也进 TTL，否则一次抖动之后每次请求
# 都会重试，zen 探活的十几秒会叠加成一串慢请求。
MODEL_LIST_TTL_SECONDS = 300

# 连续失败退避上限：失败的重试间隔按 2^(连续失败次数-1) × TTL 拉长，到此封顶
# （2h）。上游被长期封禁（如 zen 免费层按出口 IP 拦截）时，每 5 分钟重探一遍
# （zen 逐个真发一次推理，十几秒）既费时又刷日志，退避后恢复探测的代价只是
# 最多晚 2h 知道上游恢复了（期间一直有缓存兜底）。
MODEL_LIST_BACKOFF_MAX_SECONDS = 7200

# 拉取串行锁：模块级单锁（一次只有一个进程内的列表刷新在跑）。调用方有 HTTP
# 出口与后台兜底刷新两处，不串行就会同时打同一条渠道的上游。
_refresh_lock = asyncio.Lock()


def credential_providers(services: Services) -> set[str]:
    """当前有可用凭证（selectable）的渠道集合。

    `list_models` 用它决定「哪些渠道该出现在模型列表里」：没有可用凭证的
    渠道既不值得拉模型（只会白打上游/回退静态表），也不该对外展示——
    用户没接入或自己暂停了，就不该在 Playground / `/v1/models` 里看到它。

    口径与调度器一致（`selectable_only=True`：未暂停、未硬禁用）；冷却中的
    凭证仍算「有凭证」——渠道接了只是暂时限流，模型列表不该跟着闪没。
    """
    return {row.provider
            for row in services.credentials.candidates(selectable_only=True)}


def merged_entries(services: Services, connected: set[str]) -> list[dict[str, Any]]:
    """按当前缓存表合并出全部条目（归一键 → 定稿后按 `_sort_key` 排序）。

    只看 `services.model_list_cache`，不看「本次拉到了什么」：这样缓存兜底、
    TTL 复用、落盘恢复三条路径共用同一段合并逻辑，行为一致。渠道过滤用
    `connected`（有可用凭证），与 `list_models` 的拉取范围保持同口径。
    """
    patterns = services.settings.blocklist_patterns
    grouped: dict[str, dict[str, Any]] = {}   # 归一键 → {providers, meta, raw_ids, ...}
    for provider_id, table in services.model_list_cache.items():
        if provider_id in connected and table:
            _merge_provider(grouped, provider_id, _visible(table, patterns))
    # 先按渠道优先级 + 归一键排序（对外 id 尚未算出），再定稿：`_disambiguate`
    # 要靠这个顺序决定对外 id 撞车时谁保留。定稿后顺序不再变化，输出即此序。
    return sorted(grouped.values(), key=_sort_key)


def _publish(services: Services, entries: list[dict[str, Any]]) -> None:
    """定稿好的条目 → 就地更新别名表（executor 的闭包引用同一个 dict 对象）。

    先算好再 clear+update：`_build_aliases` 若抛错，不能让别名表停在「已清空、
    未写入」的中间态（那会让所有扁平名请求失去候选收窄）。对象身份必须保留，
    故不能整体替换 dict。

    自定义别名（`MODEL_ALIASES`）**每次现读**运行态覆盖层再解析：这是热更
    项，改完下一次列表 / 单查就生效，不需要重启或等 TTL。解析结果同时进
    「条目表」（列表侧）与「别名表」（执行侧），两者天然是同一份。
    """
    custom = parse_model_aliases(str(getattr(services.settings, "model_aliases", "")))
    aliases = _build_aliases(entries, custom)
    services.model_aliases.clear()
    services.model_aliases.update(aliases)


def publish_aliases(services: Services, connected: set[str]) -> None:
    """按当前缓存表重建别名表并就地 publish。

    **逐渠道增量调用**（见 `list_models`）：某渠道拉完就 publish，不必等最慢的
    zen 探活（十几秒）——那段时间里已拉好的渠道本该已经能收窄候选，否则扁平名
    请求会按「全部渠道」扇出，真实打一轮不认这个模型的上游。
    """
    _publish(services, merged_entries(services, connected))


async def _refresh_provider(services: Services, provider_id: str, provider: Any) -> None:
    """拉一条渠道的模型表：成功更新缓存并落盘，失败保留旧缓存（兜底）。

    缓存里存**未过滤**的原始表：过滤在每个出口现做，否则黑名单热更后缓存里
    仍是被滤前的旧结果（最长 TTL 才生效）。
    """
    # 用该上游的一个可用凭证拉取（凭证有归属，模型列表是账号级的）
    # selectable_only：硬禁用/用户关闭的凭证取不到数据，只会白失败
    candidates = services.credentials.candidates([provider_id], selectable_only=True)
    credential_data = (
        services.credentials.credential_data(candidates[0].credential_id)
        if candidates else {}
    )
    try:
        models = await provider.list_models(credential_data)
    except Exception as error:  # noqa: BLE001 - 单上游失败不影响其他
        # 失败也记时间戳（负缓存）：TTL 内不再重试，有缓存用缓存、无缓存跳过。
        # 连续失败按 `_needs_refresh` 的退避拉长下一次尝试；日志只在**首次**
        # 失败打 WARNING，其后降 INFO——长期封禁下同样内容的 WARNING 刷屏
        # 只会淹没真问题，连续次数仍留在消息里供排查。
        failures = services.model_list_failures.get(provider_id, 0) + 1
        services.model_list_failures[provider_id] = failures
        services.model_list_fetched_at[provider_id] = time.monotonic()
        log = logger.warning if failures == 1 else logger.info
        if services.model_list_cache.get(provider_id):
            log("模型列表获取失败 %s（连续 %d 次），使用上次缓存: %s",
                provider_id, failures, error)
        else:
            log("模型列表获取失败 %s（连续 %d 次）: %s", provider_id, failures, error)
        return
    services.model_list_failures.pop(provider_id, None)
    services.model_list_cache[provider_id] = {model.id.lower(): model for model in models}
    services.model_list_fetched_at[provider_id] = time.monotonic()
    save_catalog(services.settings.data_dir, services.model_list_cache)


def _needs_refresh(services: Services, provider_id: str, *, now: float) -> bool:
    """该渠道是否需要（重新）打上游。

    TTL 按「上次尝试」计（失败也刷新，restore 也播种）：TTL 内一律不打上游
    ——有缓存就用缓存、没缓存就跳过，避免上游抖动时每次 `/v1/models` 都重跑
    一遍拉取（zen 探活的十几秒会叠加成一串慢请求）。

    **连续失败再退避**：拉取连续失败时按 2^(n-1) × TTL 拉长重试间隔，封顶
    `MODEL_LIST_BACKOFF_MAX_SECONDS`。只放大间隔、不阻断恢复——成功一次即
    清零失败计数（见 `_refresh_provider`），回到正常 TTL。
    """
    fetched_at = services.model_list_fetched_at.get(provider_id)
    if fetched_at is None:
        return True
    failures = services.model_list_failures.get(provider_id, 0)
    interval = MODEL_LIST_TTL_SECONDS
    if failures:
        interval = min(interval * 2 ** min(failures - 1, 10),
                       MODEL_LIST_BACKOFF_MAX_SECONDS)
    return now - fetched_at >= interval


def restore_model_catalog(services: Services) -> int:
    """把落盘目录灌回进程内缓存并**立即 publish 别名表**（同步、零上游请求）。

    启动时调用（`main.lifespan`，在后台预热之前）。解决的是「别名表为空的那段
    启动窗口」：这段时间里扁平名请求无法收窄候选，会真实打一轮不认这个模型的
    上游（CodeBuddy 11102 / TRAE 4001）。

    同时把快照年龄折进 `model_list_fetched_at`：快照不只是数据，还带新鲜度。
    不播种的话 TTL 时间戳是空的，启动预热（`_needs_refresh` 见无时间戳即刷新）
    会把刚恢复的表全量重拉一遍——kilo 实测 10–22s、zen 探活 12–15s，白花。

    返回恢复的渠道数（供日志/测试）。
    """
    connected = credential_providers(services)
    restored = 0
    now = time.time()
    for provider_id, (saved_at, table) in load_catalog(
            services.settings.data_dir, now=now).items():
        # 未注册的渠道（换过的装配）与没有可用凭证的渠道都不恢复：目录里的
        # 快照不该让「用户已暂停的渠道」重新出现在模型列表里。
        if provider_id not in services.registry or provider_id not in connected:
            continue
        services.model_list_cache[provider_id] = table
        services.model_list_fetched_at[provider_id] = (
            time.monotonic() - max(0.0, now - saved_at))
        restored += 1
    if restored:
        publish_aliases(services, connected)
        logger.info("已从落盘目录恢复模型目录: %s", ",".join(sorted(services.model_list_cache)))
    return restored


async def _refresh_providers(services: Services, connected: set[str]) -> None:
    """同步拉取所有「需要刷新」的渠道，每拉完一条就 publish 一次别名表。

    没凭证的渠道直接跳过：不拉取、不展示。已接入的渠道即使本次拉取失败，也会
    走缓存兜底，不会因为一次抖动就从列表里消失。
    **每拉完一条渠道就 publish**（`publish_aliases`），不等最慢的那条
    （zen 探活十几秒）——否则这段时间里别名表空着，扁平名请求会扇出到不认该
    模型的上游。整段串行化（`_refresh_lock`）：HTTP 出口与后台兜底刷新两处都
    可能进来，无锁时会同时对同一条渠道打上游（zen 那次是十几秒真推理），后到
    的拿到的还是同一份数据，纯属白打。
    """
    async with _refresh_lock:
        now = time.monotonic()
        for provider_id, provider in services.registry.items():
            if provider_id not in connected:
                continue
            if not _needs_refresh(services, provider_id, now=now):
                continue
            await _refresh_provider(services, provider_id, provider)
            publish_aliases(services, connected)


def _schedule_refresh(services: Services, connected: set[str],
                      stale: set[str]) -> None:
    """把「过期渠道」的刷新丢到后台（去重 + 完成后自动摘除）。

    HTTP 出口专用：**有缓存就直接返回旧列表**，把 zen 探活的十几秒从请求路径
    挪走——此前 TTL 一过就同步重拉全部渠道，Playground 每次卡十几到三十秒。
    代价是列表最多滞后一个 TTL：TTL 到期后的第一次请求仍回旧数据，后台刷新
    完成、下一次请求才转新鲜。

    去重：已有刷新在途就不再排新的（`model_refreshing`）；`_refresh_providers`
    里还有 `_refresh_lock`，但那是「多请求串行化」，这里再挡一层避免同一渠道被
    反复排队。任务句柄进 `model_refresh_tasks`，lifespan 关闭时统一取消。
    """
    if services.model_refreshing:
        return
    services.model_refreshing.update(stale)

    async def run() -> None:
        try:
            await _refresh_providers(services, connected)
        except Exception as error:  # noqa: BLE001 - 后台刷新失败只记日志
            logger.warning("后台模型列表刷新失败: %s", error)
        finally:
            services.model_refreshing.difference_update(stale)

    task = asyncio.create_task(run())
    services.pending_model_refreshes.append(task)
    services.model_refresh_tasks.add(task)

    def _done(_task: asyncio.Task) -> None:
        services.model_refresh_tasks.discard(_task)
        with contextlib.suppress(ValueError):
            services.pending_model_refreshes.remove(_task)

    task.add_done_callback(_done)


def _served_entries(services: Services, connected: set[str],
                    allowed_models: str = "") -> list[dict[str, Any]]:
    """合并 + 定稿 + 就地 publish 别名表，并按 Key 白名单过滤。

    `allowed_models` 非空时按该 Key 的模型白名单过滤展示：白名单是
    Key 级策略，执行时的权威判定在出口（`model_allowed`），这里只是让
    `/v1/models` 与 Key 实际能用的模型一致。

    列表与单模型查询（`serve_model`）共用这一段，保证两条出口看到的是
    同一份「定稿后」条目（`_publish` 会就地 finalize，id 撞车消歧只发生一次）。
    """
    entries = merged_entries(services, connected)
    _publish(services, entries)
    if allowed_models:
        from ..auth.access import model_allowed
        entries = [entry for entry in entries
                   if model_allowed(entry["id"], allowed_models)]
    return entries


def _build_response(services: Services, connected: set[str],
                    allowed_models: str = "",
                    benchmarks: BenchmarkTable | None = None,
                    prices: PriceTable | None = None,
                    efforts: dict[str, dict[str, str]] | None = None) -> dict:
    """按当前缓存合并出响应（不碰上游），并就地 publish 别名表。

    `benchmarks` 为能力分表（`app.state.model_benchmarks`）；None/空表时条目
    不带 `benchmarks` 字段，与加该字段前的行为一致。`prices`（刊例价）与
    `efforts`（生效中的档位映射）同理：缺省时条目不带 `cost` /
    `reasoning_efforts`。
    """
    entries = _served_entries(services, connected, allowed_models)
    return {"object": "list",
            "data": [_entry_response(entry, benchmarks, prices, efforts)
                     for entry in entries]}


def _match_entry(entries: list[dict[str, Any]],
                 model_id: str) -> dict[str, Any] | None:
    """按对外 id / 展示名 / 任一渠道原 id 查单条（大小写不敏感）。

    三种写法都要认，否则「列表里复制 id」能用、「手写上游原 id」却 404：
    别名表（`_register_aliases`）在执行侧本来就认这三种，单模型查询保持同口径。
    返回 `None` = 不存在或不在该 Key 的白名单内（调用方统一 404，不泄露
    「存在但你没权限」）。
    """
    target = model_id.strip().lower()
    if not target:
        return None
    for entry in entries:
        if entry["id"].lower() == target:
            return entry
        name = entry["meta"].get("name")
        if isinstance(name, str) and name.lower() == target:
            return entry
        if any(str(raw).lower() == target for raw in entry["raw_ids"].values()):
            return entry
    return None


async def _revalidated_providers(services: Services) -> set[str]:
    """按 SWR 策略刷新过期渠道，返回「当前有可用凭证」的渠道集合。

    列表（`serve_models`）与单模型查询（`serve_model`）共用：两者的刷新时机
    必须一致，否则会出现「列表里有、单查 404」的错觉。
    """
    connected = credential_providers(services)
    stale = {provider_id for provider_id in connected
             if _needs_refresh(services, provider_id, now=time.monotonic())}
    if any(not services.model_list_cache.get(provider_id) for provider_id in stale):
        # 无可回退模型（冷启动无快照 / 新接入渠道）：必须同步拉一次
        await _refresh_providers(services, connected)
    elif stale:
        _schedule_refresh(services, connected, stale)
    return connected


def _benchmarks_of(request: Request | None) -> BenchmarkTable | None:
    return getattr(request.app.state, "model_benchmarks", None) if request else None


def _prices_of(request: Request | None) -> PriceTable | None:
    """刊例价表（`app.state.price_table`）：与能力分同一次抓取、同一形态。"""
    return getattr(request.app.state, "price_table", None) if request else None


def _efforts_of(services: Services) -> dict[str, dict[str, str]] | None:
    """生效中的档位映射（热更）：每请求现读并解析，改完下一次请求即生效。"""
    return parse_effort_map(str(getattr(services.settings, "reasoning_effort_map", "")))


async def serve_models(services: Services, allowed_models: str = "",
                       request: Request | None = None) -> dict:
    """HTTP 出口（`/v1/models`、Playground）：先回旧列表，过期渠道后台刷。

    stale-while-revalidate：TTL 到期不再把 zen 探活的十几秒压在请求上。只有某
    渠道**一条缓存都没有**时（冷启动无落盘快照 / 新接入渠道）才同步等它一次
    ——否则列表会缺一条渠道的模型。落盘快照恢复后几乎所有渠道都有缓存，故稳态
    请求不再阻塞；代价是列表最多滞后一个 TTL（到期后第一次仍回旧数据，后台刷
    新完成后下一次才转新鲜）。

    后台刷新任务收敛在 `services.model_refresh_tasks` / `pending_model_refreshes`，
    `model_refreshing` 去重，lifespan 关闭时统一取消。

    `request` 非空时从 `request.app.state.model_benchmarks` 现读能力分表并随
    条目透出（与 `models_dev_catalog` 同一形态：运行数据挂 app.state，不是
    Services 依赖）。不传（测试 / 内部调用）时条目不带 `benchmarks` 字段。
    刊例价（`cost`）走同一条 `app.state.price_table` 取法。
    """
    connected = await _revalidated_providers(services)
    return _build_response(services, connected, allowed_models,
                           _benchmarks_of(request), _prices_of(request),
                           _efforts_of(services))


async def serve_model(services: Services, model_id: str, allowed_models: str = "",
                      request: Request | None = None) -> dict[str, Any] | None:
    """单模型查询（`GET /v1/models/{id}`）：命中返回条目，否则 None。

    刷新与合并完全复用列表那一段（`_revalidated_providers` + `_served_entries`）：
    单查也要能看到「刚接入的渠道」，否则列表有、单查 404 会让人以为是权限问题。

    **不在 Key 白名单内的模型与不存在同样返回 None**（调用方统一 404）：
    `_served_entries` 已按白名单过滤，查不到即 404——不能让客户端用单查探测
    「池子里还有哪些模型」。
    """
    connected = await _revalidated_providers(services)
    entry = _match_entry(_served_entries(services, connected, allowed_models), model_id)
    if entry is None:
        return None
    return _entry_response(entry, _benchmarks_of(request), _prices_of(request),
                           _efforts_of(services))


async def list_models(services: Services,
                      benchmarks: BenchmarkTable | None = None) -> dict:
    """同步拉取并合并模型列表（供后台预热 / 兜底刷新循环）。

    同一模型在各渠道的内部代号互不相同（Qoder `kmodel_latest` = TRAE
    `kimi-k3-1` = CodeBuddy `kimi-k3`），大小写、连字符、free 后缀、命名空间
    前缀的差异又各不相同；归一规则见 `provider.naming`：按清洗后的展示名归
    一键合并，providers 取并集，同时记录各上游的原始 id 供执行时映射回去。
    某上游拉取失败时用上次成功的缓存兜底，而不是让该上游从列表里消失。

    只遍历「当前有可用凭证」的渠道（见 `credential_providers`）：没接入 /
    全部暂停 / 会话失效的渠道不拉取也不展示，所以冷启动只有 zen / kilo（自带
    虚拟凭证），接入 CodeBuddy / TRAE 后下一次请求才把它们拉进来。

    TTL 内直接复用缓存（`model_list_fetched_at`，落盘恢复时按快照年龄播种）；
    **每拉完一条渠道就 publish 一次别名表**（`publish_aliases`），不等最慢的
    那条（zen 探活十几秒）——否则这段时间里别名表空着，扁平名请求会扇出到
    不认该模型的上游。黑名单在每个出口现算（缓存不做过滤），因此改完黑名单
    下一次调用立即生效。输出顺序按 `_sort_key`：CB / TR 渠道的模型优先。

    整段串行化（`_refresh_lock`，见 `_refresh_providers`）。后台任务要的是
    「真的刷新」，所以这条保持同步；HTTP 出口走 `serve_models`（SWR）。
    """
    connected = credential_providers(services)
    await _refresh_providers(services, connected)
    return _build_response(services, connected, "", benchmarks)


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_v1_models(request: Request,
                             principal: ApiKeyPrincipal = Depends(api_key_user)):
        return await serve_models(services, principal.allowed_models, request)

    @router.get("/v1/models/{model_id:path}")
    async def get_v1_model(model_id: str, request: Request,
                           principal: ApiKeyPrincipal = Depends(api_key_user)):
        """单模型查询（OpenAI `GET /v1/models/{model}` 同形）。

        `:path` 而非普通路径参数：上游原 id 允许带命名空间（kilo 的
        `dots-studio/dots-3-note-preview:free`），普通参数会在斜杠处 404。
        三种写法都认（对外 id / 展示名 / 任一渠道原 id），与执行侧别名表同口径。

        找不到（或不在该 Key 的白名单内）一律 404 + OpenAI 错误形状：白名单外
        的模型不能靠单查探测出「池子里有它」。
        """
        item = await serve_model(services, model_id, principal.allowed_models, request)
        if item is None:
            return JSONResponse(
                status_code=404,
                content=error_payload(f"model not found: {model_id}",
                                      "model_not_found", 404))
        return item

    return router
