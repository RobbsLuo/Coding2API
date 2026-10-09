"""模型定价表（models.dev 刊例价）+ 单请求成本估算。

**为什么单独一个模块**：成本不等于上游真实扣费，而是「按 token × 公开刊例价」
的估算，供管理台横向对比渠道成本。价表来自 `https://models.dev/api.json`，
每条 cost 是**美元 / 百万 token**（`input` / `output` / `cache_read`）。

**匹配口径**：按 `model.id.lower()`（models.dev 的模型 id）对齐本地记录里的
模型名。同一 id 往往挂在几十个 provider 下、价差极大（含 0 价套餐/折扣）：

- 有**原厂 provider** 时优先它——判定依据是 `canonical_model_id` 的前缀等于
  provider id（如 `zhipuai/glm-5.3-flash` 的原厂是 `zhipuai`）；
- 否则回退「input 价最高」的非空条目，避开 0 价套餐。

价差是真实存在的信息，本模块只做确定性选择，不猜「哪个价才对」。

**落盘快照**（`DATA_DIR/model_prices.json`）：与模型目录同理，进程重启即丢，
启动时同步读回，后台任务周期刷新。损坏 / 版本不符 / 过旧一律安静降级为空表
——价表缺失只让成本显示 `—`，绝不影响聊天。

管理台「模型列表」页展示的是同一次抓取的**明细目录**（`DATA_DIR/models_dev_catalog.json`）：
同一选条口径，但在价格之外保留 models.dev 的名称 / 上下文 / 模态 / 能力 / 知识
截止等元数据（见 `build_model_catalog`）。

**成本写入时定值**：`estimate_cost_usd` + `to_cny` 在写明细那一刻算好落库，
历史行不随价表或汇率变化而重算（与 credit 推算同一心智模型）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

PRICES_FILENAME = "model_prices.json"
PRICES_VERSION = 1
# 模型目录（管理台「模型列表」页）：与价表同一次抓取、同口径选条，但保留
# models.dev 的更多元数据（名称 / 上下文 / 模态 / 能力 / 知识截止 / 价格）。
# 单独落一个文件，不动价表快照的既有契约（只加不改）。
CATALOG_FILENAME = "models_dev_catalog.json"
CATALOG_VERSION = 1
# 快照最长可信时长（秒）：7 天。启动后后台刷新会覆盖它，这个上限只兜住
# 「停机很久 + 一直拉不通 models.dev」的组合——那时宁可没有价表（显示 —）。
PRICES_MAX_AGE_SECONDS = 7 * 24 * 3600
MODELS_DEV_URL = "https://models.dev/api.json"
DEFAULT_TIMEOUT_SECONDS = 30.0

# model.id.lower() → (input, output, cache_read)  USD / 百万 token
PriceTable = dict[str, tuple[float, float, float]]
# model.id.lower() → 该模型的一条明细（JSON 友好的扁平 dict，含价格）
ModelCatalog = dict[str, dict[str, Any]]


def _as_number(value: Any) -> float | None:
    """models.dev 的价格字段 → 非负 float；缺失 / 非数值 / 负数一律 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0:
        return None
    return number


def _as_str(value: Any) -> str | None:
    """非空字符串才保留；其余（缺失 / 类型不符 / 空串）为 None。"""
    if isinstance(value, str) and value:
        return value
    return None


def _as_int(value: Any) -> int | None:
    """非负整数才保留（布尔被排除，models.dev 的 limit 是纯数值）。"""
    number = _as_number(value)
    if number is None:
        return None
    return int(number)


def _as_bool(value: Any) -> bool:
    """只认显式 True——models.dev 用布尔字段表达能力，缺失即不具备。"""
    return value is True


def _as_str_list(value: Any) -> list[str]:
    """字符串数组 → 全字符串列表；非数组回空，数组里的非串元素丢弃。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _is_vendor(provider_id: str, entry: Mapping[str, Any]) -> bool:
    """该 provider 是否为该模型的「原厂」。

    判定依据是 `canonical_model_id` 的前缀（`zhipuai/glm-5.3-flash` → `zhipuai`）。
    缺失 canonical（上游没填）时返回 False，交给「input 最高」回退——刻意不猜。
    """
    canonical = entry.get("canonical_model_id")
    if not isinstance(canonical, str) or "/" not in canonical:
        return False
    return canonical.split("/", 1)[0].strip().lower() == provider_id.strip().lower()


def _select_entries(raw: Any) -> dict[str, tuple[str, Mapping[str, Any]]]:
    """models.dev 原始 JSON → `{小写模型 id: (命中的 provider id, 该条目的原始 dict)}`。

    这是价表与模型目录**共用的选条口径**：同一 id 在多 provider 下各有一条，
    这里选出唯一代表。有原厂 provider（canonical 前缀命中）时优先它；否则取
    input 价最高者避开 0 价套餐，打平按 provider id 升序——结果确定，测试与
    线上一致。结构异常（非 dict / cost 非 dict / input 非数值）的条目直接跳过。
    """
    if not isinstance(raw, dict):
        return {}
    # provider_id → [ (是否原厂, input 价, 小写 provider id, 原始 provider id, 条目) ]
    candidates: dict[str, list[tuple[bool, float, str, str, Mapping[str, Any]]]] = {}
    for provider_id, provider in raw.items():
        if not isinstance(provider, dict) or not isinstance(provider_id, str):
            continue
        models = provider.get("models")
        if not isinstance(models, dict):
            continue
        for key, entry in models.items():
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id") or key
            if not isinstance(model_id, str) or not model_id:
                continue
            cost = entry.get("cost")
            if not isinstance(cost, dict):
                continue
            price_in = _as_number(cost.get("input"))
            if price_in is None:            # 无输入价（含 null）→ 该条不可用
                continue
            candidates.setdefault(model_id.lower(), []).append(
                (_is_vendor(provider_id, entry), price_in, provider_id.lower(),
                 provider_id, entry))
    selected: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for model_id, entries in candidates.items():
        vendors = [item for item in entries if item[0]]
        pool = vendors or entries
        # input 降序、provider id 升序：结果确定，测试与线上一致
        chosen = sorted(pool, key=lambda item: (-item[1], item[2]))[0]
        selected[model_id] = (chosen[3], chosen[4])
    return selected


def build_price_table(raw: Any) -> PriceTable:
    """models.dev 原始 JSON → `{小写模型 id: (input, output, cache_read)}`。

    选条口径见 `_select_entries`。cache_read 缺失表示上游未声明缓存价
    （不是 0）：按 input 原价计，宁可不打折也不凭空把缓存 token 记成免费。
    """
    table: PriceTable = {}
    for model_id, (_provider_id, entry) in _select_entries(raw).items():
        cost = entry.get("cost")
        price_in = _as_number(cost.get("input"))
        price_out = _as_number(cost.get("output"))
        cache_read = _as_number(cost.get("cache_read"))
        # _select_entries 已保证 input 是有效非负数值
        cached_price = price_in if cache_read is None else cache_read
        table[model_id] = (price_in or 0.0, price_out or 0.0, cached_price or 0.0)
    return table


def build_model_catalog(raw: Any) -> ModelCatalog:
    """models.dev 原始 JSON → 管理台「模型列表」明细（与价表同口径选条）。

    在价格之外补上 models.dev 的元数据：展示名 / 所属家族 / 上下文与输出上限 /
    输入·输出模态 / 能力（附件·推理·工具调用·结构化输出）/ 是否开放权重 /
    知识截止 / 发布日期 / provider。所有字段都收敛成 JSON 友好的标量或列表，
    缺失即 None / 空列表 / False，不把上游的任意结构直接透传。
    """
    catalog: ModelCatalog = {}
    for model_id, (provider_id, entry) in _select_entries(raw).items():
        cost = entry.get("cost")
        limit = entry.get("limit")
        limit = limit if isinstance(limit, dict) else {}
        modalities = entry.get("modalities")
        modalities = modalities if isinstance(modalities, dict) else {}
        price_in = _as_number(cost.get("input")) or 0.0
        cache_read = _as_number(cost.get("cache_read"))
        catalog[model_id] = {
            "id": model_id,
            "name": _as_str(entry.get("name")),
            "provider": provider_id,
            "family": _as_str(entry.get("family")),
            "knowledge": _as_str(entry.get("knowledge")),
            "release_date": _as_str(entry.get("release_date")),
            "context": _as_int(limit.get("context")),
            "max_output": _as_int(limit.get("output")),
            "input_modalities": _as_str_list(modalities.get("input")),
            "output_modalities": _as_str_list(modalities.get("output")),
            "attachment": _as_bool(entry.get("attachment")),
            "reasoning": _as_bool(entry.get("reasoning")),
            "tool_call": _as_bool(entry.get("tool_call")),
            "structured_output": _as_bool(entry.get("structured_output")),
            "open_weights": _as_bool(entry.get("open_weights")),
            "input": price_in,
            "output": _as_number(cost.get("output")) or 0.0,
            # 与价表同口径：上游未声明缓存价时按 input 原价（成本就是这么算的）
            "cache_read": price_in if cache_read is None else cache_read,
            "cache_write": _as_number(cost.get("cache_write")),
        }
    return catalog


def estimate_cost_usd(
    table: Mapping[str, tuple[float, float, float]] | None,
    model: Any,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
) -> float | None:
    """按刊例价估算一次请求的美元成本；无法定价时返回 None。

    返回 None 的两种情形：价表里没有该模型，或没有输入 token 数（拿不到用量
    就无法估算——不能拿 0 冒充「免费」）。缓存 token 会被夹到 `[0, input]`，
    多报的缓存不会把未命中部分算成负数。
    """
    if not isinstance(model, str) or not model:
        return None
    price = (table or {}).get(model.strip().lower())
    if price is None:
        return None
    if input_tokens is None:
        return None
    price_in, price_out, price_cached = price
    input_count = max(0, int(input_tokens))
    cached_count = 0 if cached_tokens is None else max(0, int(cached_tokens))
    cached_count = min(cached_count, input_count)
    uncached = input_count - cached_count
    output_count = 0 if output_tokens is None else max(0, int(output_tokens))
    return (uncached * price_in + cached_count * price_cached
            + output_count * price_out) / 1_000_000


def to_cny(cost_usd: float, rate: float) -> float:
    """美元成本按汇率折人民币；rate 非法（NaN / 非正）时按 1:1 兜底。

    汇率校验放这里是为了让落库值永远有限：坏汇率若产生 NaN 会污染整段聚合，
    且一进 SQLite 再也回不来（与 credit 拒绝 NaN 同理）。
    """
    numeric = float(cost_usd)
    try:
        exchange = float(rate)
    except (TypeError, ValueError):
        exchange = 0.0
    if not (exchange > 0) or exchange != exchange:  # 非正 / NaN
        exchange = 1.0
    return numeric * exchange


def prices_path(data_dir: str) -> str:
    return os.path.join(data_dir, PRICES_FILENAME)


def save_prices(data_dir: str, table: Mapping[str, tuple[float, float, float]]) -> None:
    """价表原子写盘（tmp + replace）；失败只记日志，绝不影响聊天。"""
    payload = {
        "version": PRICES_VERSION,
        "currency": "USD",
        "saved_at": time.time(),
        "prices": {model: list(price) for model, price in table.items()},
    }
    path = prices_path(data_dir)
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as error:
        logger.warning("价表落盘失败 %s: %s", path, error)


def load_prices(data_dir: str, *, now: float | None = None) -> PriceTable:
    """读回落盘价表；文件缺失 / 损坏 / 版本不符 / 过旧一律退化成空表。"""
    return load_prices_snapshot(data_dir, now=now)[0]


def load_prices_snapshot(
    data_dir: str, *, now: float | None = None,
) -> tuple[PriceTable, float | None]:
    """读回 `(价表, 快照保存时刻)`；校验口径与 `load_prices` 完全一致。

    保存时刻供管理台「模型列表」页展示「这份表是什么时候拉取的」；任何异常都与
    表本身一样安静降级为 `({}, None)`——价表缺失只让成本显示 `—`，绝不影响聊天。
    """
    path = prices_path(data_dir)
    moment = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if raw.get("version") != PRICES_VERSION:
            logger.warning("价表版本不匹配 %s，忽略", path)
            return {}, None
        saved_at = raw["saved_at"]
        if moment - saved_at > PRICES_MAX_AGE_SECONDS:
            return {}, None
        prices = raw["prices"]
        if not isinstance(prices, dict):
            return {}, None
        table: PriceTable = {}
        for model, price in prices.items():
            # JSON 对象的 key 恒为字符串；这里只校验值形状。
            if not isinstance(price, (list, tuple)) or len(price) != 3:
                continue
            numbers = tuple(_as_number(value) for value in price)
            if any(value is None for value in numbers):
                continue
            table[model.lower()] = numbers  # type: ignore[assignment]
        # 上面 `moment - saved_at` 已排除非数值，这里可安全转 float。
        return table, float(saved_at)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        logger.warning("价表读取失败 %s: %s", path, error)
        return {}, None


def catalog_path(data_dir: str) -> str:
    return os.path.join(data_dir, CATALOG_FILENAME)


def save_model_catalog(data_dir: str, catalog: ModelCatalog) -> None:
    """模型目录原子写盘（tmp + replace）；失败只记日志，绝不影响聊天与成本。

    值本身已是 JSON 友好的扁平 dict（见 `build_model_catalog`），直接落盘。
    """
    payload = {
        "version": CATALOG_VERSION,
        "saved_at": time.time(),
        "models": catalog,
    }
    path = catalog_path(data_dir)
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as error:
        logger.warning("模型目录落盘失败 %s: %s", path, error)


def load_model_catalog(data_dir: str, *, now: float | None = None) -> ModelCatalog:
    """读回落盘模型目录；缺失 / 损坏 / 版本不符 / 过旧一律退化成空目录。

    条目只做「是 dict」这一层校验：内容由本进程写、版本已对齐，页面对缺字段
    有兜底（显示 —）。坏条目跳过而不拖垮整份目录。
    """
    path = catalog_path(data_dir)
    moment = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if raw.get("version") != CATALOG_VERSION:
            logger.warning("模型目录版本不匹配 %s，忽略", path)
            return {}
        saved_at = raw["saved_at"]
        if moment - saved_at > PRICES_MAX_AGE_SECONDS:
            return {}
        models = raw["models"]
        if not isinstance(models, dict):
            return {}
        return {model: record for model, record in models.items()
                if isinstance(record, dict)}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        logger.warning("模型目录读取失败 %s: %s", path, error)
        return {}


async def fetch_models_dev(url: str = MODELS_DEV_URL, *,
                           transport: Any | None = None,
                           timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    """拉取 models.dev 原始 JSON；网络 / HTTP 异常向上抛，由调用方决定降级。

    价表与模型目录来自同一次抓取（数 MB 大表，没必要打两遍），故这里只返回
    原始结构，构建交给 `build_price_table` / `build_model_catalog`。
    `transport` 只为测试注入（httpx.MockTransport），生产走真实网络。
    """
    import httpx

    kwargs: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        kwargs["transport"] = transport
    async with httpx.AsyncClient(**kwargs) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
