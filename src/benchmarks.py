"""OpenRouter 公开模型目录：刊例价 + 明细元数据 + 能力排行，一处抓取三处用。

**为什么是 OpenRouter 一个源**：这个项目原先两份公开目录——`models.dev`
（价格 + 元数据）与 OpenRouter（能力排行），各自一套匹配口径、各自一个后台
任务。2026-10-10 决定合并为一个源：

* OpenRouter 匿名可读（无需 key），且同时带价格（`pricing`）、元数据
  （`architecture` / `top_provider` / `supported_parameters`）与 Artificial
  Analysis 三项指数（`benchmarks.artificial_analysis`）——三份数据一次抓完；
* 两套匹配口径漂移过（doubao / -official / 版本点连字符各修一遍），合成一个
  源后只剩 `model_match` 一套规则；
* 后台任务从两个（`price_catalog` + `benchmark_catalog`）合成一个
  `openrouter_catalog`，落盘文件从三个（`model_prices.json` /
  `models_dev_catalog.json` / `model_benchmarks.json`）合成一个
  `openrouter_catalog.json`。

**代价（已知取舍）**：OpenRouter 只收录 458 个模型，models.dev 有 3536 个，
故「模型列表」页从 3536 条缩到 458 条；`space-bunny` / `doubao-seed-2.1-turbo`
/ `qwen3.8-max` 三个本项目在用模型上游没有刊例价（成本显示 —）；开放权重 /
所属家族 / **原厂发布时间**三列上游没有（见 `build_model_catalog`）。这是
用户在 2026-10-10 明确选择的「完全替换」方案。

三条纪律（与原先的价表 / 能力表一致）：

1. **只做等值匹配**（`model_match`，候选键唯一命中才采用）：错配会把 A 的
   价格/分数标到 B 上，比没有更糟。
2. **失败安静降级**：拉不到 / 快照坏只让成本与分数显示 `—`，绝不影响模型
   列表本身与聊天。
3. **能力指数是第三方数据，不是本服务实测**：透给前端 `source` 字段。

快照口径（7 天上限、tmp + `os.replace` 原子写、版本不符整体退空，公共实现见
`persistence/json_snapshot.py`），启动时同步读回（零上游请求），后台任务周期刷新。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from typing import Any

from .model_match import build_table
from .persistence.json_snapshot import load_snapshot, save_snapshot
from .pricing import PriceRecord, PriceTable

# OpenRouter 公开模型列表：无需 API key（2026-10-10 实测匿名 200）。它不是
# 官方承诺稳定的接口，故拉取失败一律降级为空表（见模块 docstring 第 2 条）。
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# 落盘文件名刻意避开 `api/model_catalog.py` 的 `model_catalog.json`（渠道模型
# 别名表，另一个概念）——两者都在 DATA_DIR 下，同名会互相覆盖。
CATALOG_FILENAME = "openrouter_catalog.json"
CATALOG_VERSION = 2
# 快照最长可信时长（秒）：停机很久又一直拉不通时，宁可没有价表与分数
# （显示 —）也不展示一周前的旧数据。
MAX_AGE_SECONDS = 7 * 24 * 3600
DEFAULT_TIMEOUT_SECONDS = 30.0
# 知识截止只认 `YYYY-MM-DD` 这一个形态（不解析时区、不做各种本地化格式的
# 兼容猜测：解析不出来就显示 —，别给一个错的日期）。
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")

# 能力指数来源标识：透给前端与 API，让「数据是谁的」始终可见。
SOURCE = "openrouter"
SOURCE_LABEL = "Artificial Analysis 指数（经 OpenRouter 公开接口）"

# 一条能力记录：三个指数 + 来源溯源。字段固定、缺项不带（前端判断存在性）。
BenchmarkRecord = dict[str, Any]
# 候选键 → 能力记录（键口径见 model_match.match_keys）
BenchmarkTable = dict[str, BenchmarkRecord]

# 模型明细：管理台「模型列表」页一行（与价表同一次抓取、同口径选条）。
ModelCatalog = dict[str, dict[str, Any]]

_INDEX_FIELDS = ("intelligence_index", "coding_index", "agentic_index")

# 快照日期戳识别（2026-10-19 起）：OpenRouter 对同一个模型会同时挂多条记录——
# **浮动别名**（`deepseek/deepseek-v4.1-flash`）与**带日期快照**
# （`deepseek/deepseek-v4.1-flash-1017`）。上游把浮动别名钉在旧快照上、还换了
# 评测变体（实测 2026-10-19：浮动别名标 `Non-reasoning` 24.7，`-1017` 标
# `Max` 39.5），于是能力分看着「掉」了 15 分，其实只是取了另一个变体。
#
# 本项目转发时推理默认开着（渠道 `default_effort` 多为 high），与 Max 变体
# 同口径，故**浮动别名取同族最新快照的分**；显式带日期戳的 id
# （`deepseek-v4-pro-0813`）仍用自己的分——那才是用户点名要的那个快照。
_SNAPSHOT_DATE = re.compile(r"-\d{8}$")                 # canonical 尾部 YYYYMMDD
_ID_DATE_STAMP = re.compile(r"[-_](?:\d{8}|\d{4})$")    # id 尾部快照日期戳


def _as_number(value: Any) -> float | None:
    """数值字段 → 有限非负 float；缺失 / 非数值 / 负数 / NaN / inf 一律 None。

    OpenRouter 的价格是**字符串**（`"0.000000039"`），指数是数值，故这里同时
    吃两种。布尔显式排除（`True` 不是 1）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number != number or number == float("inf"):
        return None
    return number


def _as_int(value: Any) -> int | None:
    """非负整数才保留（上下文 / 输出上限）。"""
    number = _as_number(value)
    return None if number is None else int(number)


def _as_str(value: Any) -> str | None:
    """非空字符串才保留；其余（缺失 / 类型不符 / 空串）为 None。"""
    return value if isinstance(value, str) and value else None


def _as_date(value: Any) -> str | None:
    """知识截止 → `YYYY-MM-DD`；不合法回 None。

    OpenRouter 的 `knowledge_cutoff` 给的是**日期字符串**（`"2026-02-16"`），
    不是 epoch 秒——早期按数值解析会整列解析失败、451 条全空。仍留数值分支：
    上游若哪天改回 epoch 秒，页面不至于整列塌成 —，只是格式约定要跟着改。
    """
    if isinstance(value, str):
        text = value.strip()
        if _DATE_PATTERN.match(text):
            return text
        return None
    number = _as_number(value)
    if number is None:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(number))
    except (OverflowError, OSError, ValueError):
        return None


def _as_str_list(value: Any) -> list[str]:
    """字符串数组 → 全字符串列表；非数组回空，数组里的非串元素丢弃。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _match_names(item: Mapping[str, Any]) -> list[str]:
    """一条上游记录的候选写法：id 与展示名两路。

    上游 id 带厂商前缀（`moonshotai/kimi-k3`），展示名是另一种写法
    （`MoonshotAI: Kimi K3`），本项目 id 可能是第三种（`kimi-k3`）——都入表，
    按唯一命中选择（见 `model_match.lookup`）。
    """
    return [name for name in (item.get("id"), item.get("name"))
            if isinstance(name, str) and name]


def _select_entries(raw: Any) -> list[Mapping[str, Any]]:
    """OpenRouter 原始 JSON → 可用条目列表（结构异常逐条跳过）。

    与 models.dev 时代的 `_select_entries` 不同：这里**不需要跨 provider 选条**
    ——OpenRouter 一个模型只有一条记录（价格由它自己的路由决定，不带其它
    provider 的价）。故只做「是 dict + 有 id + 有可解析的价格」三层校验。
    """
    if not isinstance(raw, dict):
        return []
    data = raw.get("data")
    if not isinstance(data, list):
        return []
    entries: list[Mapping[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if not any(isinstance(item.get(field), str) and item.get(field)
                   for field in ("id", "name")):
            continue
        entries.append(item)
    return entries


def _price_of(item: Mapping[str, Any]) -> PriceRecord | None:
    """`pricing` block → `(input, output, cache_read)`；无输入价时 None。

    上游按 **USD / token** 给字符串小数（`"0.000000039"`），这里乘 1e6 转成
    与 models.dev 一致的「USD / 百万 token」。cache_read 缺失按 input 原价
    （与成本估算公式同口径）。
    """
    pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        return None
    price_in = _as_number(pricing.get("prompt"))
    if price_in is None:            # 无输入价（含 null）→ 该条不可定价
        return None
    price_out = _as_number(pricing.get("completion")) or 0.0
    cache_read = _as_number(pricing.get("input_cache_read"))
    cached_price = price_in if cache_read is None else cache_read
    return (price_in * 1e6, price_out * 1e6, cached_price * 1e6)


def build_price_table(raw: Any) -> PriceTable:
    """OpenRouter 原始 JSON → `{候选键: (input, output, cache_read)}`。

    与能力表同一批候选键：价格与分数挂在同一模型的不同字段上，键口径必须一致
    才能「同一次匹配同时拿到价格与分数」。
    """
    entries: list[tuple[list[str], PriceRecord]] = []
    for item in _select_entries(raw):
        price = _price_of(item)
        if price is None:
            continue
        # `_select_entries` 只放行 id / name 至少一个有值的条目，故 names 非空。
        entries.append((_match_names(item), price))
    return build_table(entries)


def _snapshot_info(item: Mapping[str, Any]) -> tuple[str | None, str, bool]:
    """一条上游记录 → `(家族基键, 快照日期, 是否浮动别名)`。

    `canonical_slug` 是上游自己的「这条属于哪个发布」声明：带 `-YYYYMMDD`
    尾缀的才是**同一发布的快照序列**（`deepseek/deepseek-v4.1-flash-20260910`
    与 `...-20261017`）。家族基键只从这种带日期的 canonical 派生——像
    `mistralai/mistral-large`（Feb '24）与 `mistralai/mistral-large-2512`
    （Mistral Large 3）削完日期虽然同基，却是两个模型，上游没给前者日期戳
    就是在说「它不是谁的快照」，故**不入家族**。

    「浮动别名」只按**上游 id** 判定：`deepseek/deepseek-v4.1-flash`（浮动，
    跟随上游漂移）vs `deepseek/deepseek-v4.1-flash-1017`（点名要的快照）。
    不看展示名——`DeepSeek: DeepSeek V4 Pro 0423` 这类名字带日期、id 却不带，
    恰恰就是「浮动别名被钉在旧快照上」的典型形态。
    """
    canonical = _as_str(item.get("canonical_slug")) or ""
    match = _SNAPSHOT_DATE.search(canonical)
    family = canonical[:match.start()] if match else None
    date = match.group(0)[1:] if match else ""
    raw_id = _as_str(item.get("id")) or ""
    return family, date, not _ID_DATE_STAMP.search(raw_id)


def build_benchmark_table(raw: Any) -> BenchmarkTable:
    """OpenRouter 原始 JSON → `{候选键: 能力记录}`。

    只收 `benchmarks.artificial_analysis` 下的三项指数（`null` 视为没有该指数，
    不带字段）。一个模型可能带 `design_arena` 等其它 block，与本项目无关，不碰。

    结构异常（顶层非 dict、`data` 非 list、单条非 dict）逐条跳过，不让一个坏
    条目拖垮整张表。完全没有指数时**不入表**：空记录只会让前端多渲染一个
    「无分」徽章，不如没有条目。

    **浮动别名取同族最新快照的分**（2026-10-19，见 `_SNAPSHOT_DATE` 注释）：
    OpenRouter 会把浮动别名钉在旧快照上、并且对同一快照只发布一个评测变体
    （实测 `deepseek/deepseek-v4.1-flash` 标 `Non-reasoning` 24.7，而
    `-1017` 标 `Max` 39.5）。本项目转发时推理默认开着，与 Max 同口径，故按
    `canonical_slug` 的日期取同族最新一条；显式带日期戳的 id 仍用自己的分。
    `source_model` 一律指向真正提供分数的上游 id，溯源不失真。
    """
    scored: list[tuple[Mapping[str, Any], BenchmarkRecord,
                       str | None, str, bool]] = []
    for item in _select_entries(raw):
        indices = item.get("benchmarks")
        # `benchmarks` 缺失 / 非 dict（上游结构变了）时整条跳过。注意不能写
        # `(item.get("benchmarks") or {})`：那样会把 `"bad"` 这类非 dict 值漏到
        # 下一行才炸，异常从 `build_benchmark_table` 逃出去。
        if not isinstance(indices, dict):
            continue
        indices = indices.get("artificial_analysis")
        if not isinstance(indices, dict):
            continue
        scores = {field: number
                  for field in _INDEX_FIELDS
                  if (number := _as_number(indices.get(field))) is not None}
        if not scores:
            continue
        record: BenchmarkRecord = {"source": SOURCE,
                                   "source_model": item.get("id", ""),
                                   **scores}
        scored.append((item, record, *_snapshot_info(item)))

    # 每个家族的最新快照（按 canonical 日期；同日期时先到先得，保持确定性）。
    newest: dict[str, tuple[str, BenchmarkRecord]] = {}
    for _item, record, family, date, _floating in scored:
        if not family:
            continue
        current = newest.get(family)
        if current is None or date > current[0]:
            newest[family] = (date, record)

    entries: list[tuple[list[str], BenchmarkRecord]] = []
    for item, record, family, date, floating in scored:
        if floating and family:
            latest_date, latest_record = newest[family]
            if latest_date > date:
                record = latest_record
        entries.append((_match_names(item), record))
    return build_table(entries)


def build_model_catalog(raw: Any) -> ModelCatalog:
    """OpenRouter 原始 JSON → 管理台「模型列表」明细。

    字段口径与 models.dev 时代对齐（前端契约不变），来源换成 OpenRouter：

    | 列 | 来源 |
    |---|---|
    | 上下文 / 输出上限 | `context_length` / `top_provider.max_completion_tokens` |
    | 输入·输出模态 | `architecture.{input,output}_modalities` |
    | 推理 | `supported_parameters` 含 `reasoning` |
    | 工具调用 | `supported_parameters` 含 `tools` |
    | 结构化输出 | `supported_parameters` 含 `structured_outputs` |
    | 附件 | 输入模态含 `image` / `file` |
    | 知识截止 | `knowledge_cutoff`（上游给的是 `"YYYY-MM-DD"` 字符串） |

    **没有的列**（models.dev 有、OpenRouter 没有）：所属家族 `family`、开放
    权重 `open_weights`、发布日期 `release_date`。前端对它们按 null / false
    兜底（显示 — / 不列标签），故这里直接给 None / False，不改前端契约。

    关于发布日期：OpenRouter **不提供原厂发布时间**（458 条实测无此字段）。
    它有个 `created`（epoch 秒，458 条全有），但那是「该模型被 OpenRouter 收录
    的时间」，与原厂发布日无关（2026-10 实测大量模型 `created` 就在近期）。
    拿它当发布日期会是错标，故不给——宁可不显示也不显示错的。
    """
    catalog: ModelCatalog = {}
    for item in _select_entries(raw):
        price = _price_of(item)
        if price is None:
            continue                 # 没有刊例价 → 不进目录（页面上没有意义）
        names = _match_names(item)
        top_provider = item.get("top_provider")
        top_provider = top_provider if isinstance(top_provider, dict) else {}
        architecture = item.get("architecture")
        architecture = architecture if isinstance(architecture, dict) else {}
        input_modalities = _as_str_list(architecture.get("input_modalities"))
        parameters = set(_as_str_list(item.get("supported_parameters")))
        cutoff = _as_date(item.get("knowledge_cutoff"))
        price_in, price_out, cache_read = price
        raw_cache_write = (
            _as_number(item.get("pricing").get("input_cache_write"))
            if isinstance(item.get("pricing"), dict) else None)
        catalog[names[0]] = {
            "id": names[0],
            "name": _as_str(item.get("name")),
            # provider 列上游没有对应物：用 id 的厂商前缀（`z-ai/glm-5.3` →
            # `z-ai`），与 models.dev 时代的「命中的 provider」同义。
            "provider": (names[0].split("/", 1)[0] if "/" in names[0] else ""),
            "family": None,
            "knowledge": cutoff,
            "release_date": None,
            "context": _as_int(item.get("context_length")),
            "max_output": _as_int(top_provider.get("max_completion_tokens")),
            "input_modalities": input_modalities,
            "output_modalities": _as_str_list(architecture.get("output_modalities")),
            "attachment": bool({"image", "file"} & set(input_modalities)),
            "reasoning": "reasoning" in parameters,
            "tool_call": "tools" in parameters,
            "structured_output": ("structured_outputs" in parameters
                                  or "response_format" in parameters),
            "open_weights": False,
            "input": price_in,
            "output": price_out,
            # 与价表同口径：上游未声明缓存价时按 input 原价（成本就是这么算的）
            "cache_read": cache_read,
            # 同为 USD/百万 token（上游按 token 给字符串小数）
            "cache_write": (None if raw_cache_write is None
                            else raw_cache_write * 1e6),
        }
    return catalog


async def fetch_openrouter_models(url: str = OPENROUTER_MODELS_URL, *,
                                  transport: Any | None = None,
                                  timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    """拉取 OpenRouter 模型列表原始 JSON；网络 / HTTP 异常向上抛。

    `transport` 只为测试注入（httpx.MockTransport），生产走真实网络。

    `trust_env=False` 与 provider/proxy.build_client 同口径：不吃宿主环境
    / macOS 系统代理。本机开着全局代理时 httpx 默认 `trust_env=True` 会把
    它当出站代理，代理握手失败即整份目录抓不到（表现为成本与能力分全空）。
    """
    import httpx

    kwargs: dict[str, Any] = {"timeout": timeout, "trust_env": False}
    if transport is not None:
        kwargs["transport"] = transport
    async with httpx.AsyncClient(**kwargs) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


def catalog_path(data_dir: str) -> str:
    return os.path.join(data_dir, CATALOG_FILENAME)


def save_catalog(data_dir: str, prices: PriceTable, benchmarks: BenchmarkTable,
                 catalog: ModelCatalog) -> None:
    """三张表一次原子写盘（tmp + replace）；失败只记日志，绝不影响模型列表。

    三个来源同一次抓取，故同一个快照文件、同一个 `saved_at`——「价格是 3 号的、
    分数是 5 号的」这种半新半旧的状态不该出现。
    """
    payload = {
        "version": CATALOG_VERSION,
        "saved_at": time.time(),
        "prices": {model: list(price) for model, price in prices.items()},
        "benchmarks": dict(benchmarks),
        "models": catalog,
    }
    save_snapshot(catalog_path(data_dir), payload)


def _valid_prices(raw: Any) -> PriceTable:
    """快照里的价表 → 合法条目；坏条目跳过而不拖垮整张表。"""
    if not isinstance(raw, dict):
        return {}
    table: PriceTable = {}
    for model, price in raw.items():
        if not isinstance(model, str) or not model:
            continue
        if not isinstance(price, (list, tuple)) or len(price) != 3:
            continue
        numbers = tuple(_as_number(value) for value in price)
        if any(value is None for value in numbers):
            continue
        table[model.lower()] = numbers  # type: ignore[assignment]
    return table


def _valid_benchmarks(raw: Any) -> BenchmarkTable:
    """快照里的能力表 → 合法条目；坏条目跳过而不拖垮整张表。"""
    if not isinstance(raw, dict):
        return {}
    table: BenchmarkTable = {}
    for key, record in raw.items():
        if not isinstance(key, str) or not key:
            continue
        # 只认本进程写过的形状：dict + 至少一个指数字段。
        if not isinstance(record, dict) or not any(
                field in record for field in _INDEX_FIELDS):
            continue
        table[key] = record
    return table


def _valid_catalog(raw: Any) -> ModelCatalog:
    """快照里的模型明细 → 合法条目；坏条目跳过而不拖垮整张表。"""
    if not isinstance(raw, dict):
        return {}
    return {model: record for model, record in raw.items()
            if isinstance(model, str) and model and isinstance(record, dict)}


def load_catalog_snapshot(
    data_dir: str, *, now: float | None = None,
) -> tuple[PriceTable, BenchmarkTable, ModelCatalog, float | None]:
    """读回 `(价表, 能力表, 模型明细, 快照保存时刻)`。

    文件缺失 / 损坏 / 版本不符 / 过旧一律退化成空表 + `None` 时刻。保存时刻
    供管理台展示「这份数据是什么时候拉取的」。
    """
    return load_snapshot(
        catalog_path(data_dir),
        version=CATALOG_VERSION,
        max_age=MAX_AGE_SECONDS,
        validator=_restore_payload,
        fallback=({}, {}, {}, None),
        now=now,
    )


def _restore_payload(
    raw: Any, now: float, max_age: float,
) -> tuple[PriceTable, BenchmarkTable, ModelCatalog, float | None]:
    """整份 payload → 三张表；三表共用同一个 `saved_at`，故整份判新鲜度。"""
    saved_at = raw["saved_at"]
    if now - saved_at > max_age:
        return {}, {}, {}, None
    return (_valid_prices(raw.get("prices")),
            _valid_benchmarks(raw.get("benchmarks")),
            _valid_catalog(raw.get("models")),
            float(saved_at))


