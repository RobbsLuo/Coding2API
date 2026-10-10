"""模型能力排行（Artificial Analysis 指数，经 OpenRouter 公开接口分发）。

**为什么走 OpenRouter**：Artificial Analysis 官方 Data API 要 API key
（`artificialanalysis.ai/api/v2/...` 无 key 返回 `{"error":"API key required"}`），
而它的三项指数同样出现在 OpenRouter 的公开模型列表里
（`benchmarks.artificial_analysis.{intelligence_index, coding_index, agentic_index}`），
**无需任何凭证**即可拉取（2026-10-10 实测：`GET /api/v1/models` 匿名 200、
458 个模型、其中 298 个带指数）。三条纪律：

1. **只做等值匹配**（`model_match`，候选键唯一命中才采用）：错配会把 A 的
   分数标到 B 上，比没有分数更糟。渠道内部占位模型（`custom_model_*`、
   `*_subagent`）本来就不该有分，匹配不到就没有。
2. **失败安静降级**：拉不到 / 表为空只在管理台与选择器里表现为「没有徽章」，
   绝不影响模型列表本身——与价表（`pricing`）同一条纪律。
3. **指数是第三方数据，不是本服务实测**：前端与 API 都标 `source`，用户能
   看出分数出处，不会误以为是本服务跑出来的成绩。

快照口径与价表一致（7 天上限、tmp + `os.replace` 原子写、版本不符整体退空），
启动时同步读回（零上游请求），后台任务周期刷新。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from .model_match import build_table

logger = logging.getLogger(__name__)

# OpenRouter 公开模型列表：无需 API key（2026-10-10 实测匿名 200）。它不是
# 官方承诺稳定的接口，故拉取失败一律降级为空表（见模块 docstring 第 2 条）。
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
BENCHMARKS_FILENAME = "model_benchmarks.json"
BENCHMARKS_VERSION = 1
# 快照最长可信时长（秒）：与价表同一口径——停机很久又一直拉不通时，宁可没有
# 分数（显示 —）也不展示一周前的旧排名。
BENCHMARKS_MAX_AGE_SECONDS = 7 * 24 * 3600
DEFAULT_TIMEOUT_SECONDS = 30.0

# 指数来源标识：透给前端与 API，让「数据是谁的」始终可见。
SOURCE = "openrouter"
SOURCE_LABEL = "Artificial Analysis 指数（经 OpenRouter 公开接口）"

# 一条能力记录：三个指数 + 来源溯源。字段固定、缺项不带（前端判断存在性）。
BenchmarkRecord = dict[str, Any]
# 候选键 → 能力记录（键口径见 model_match.match_keys）
BenchmarkTable = dict[str, BenchmarkRecord]

_INDEX_FIELDS = ("intelligence_index", "coding_index", "agentic_index")


def _as_score(value: Any) -> float | None:
    """指数 → 有限非负 float；缺失 / 非数值 / 负数 / NaN / inf 一律 None。

    inf 与 NaN 一样不是有效指数：上游字段类型意外时它们会一路带进 JSON 响应，
    让前端渲染出 `Infinity`，且 SQLite/图表都无法处理。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number != number or number == float("inf"):
        return None
    return number


def build_benchmark_table(raw: Any) -> BenchmarkTable:
    """OpenRouter `/api/v1/models` 原始 JSON → `{候选键: 能力记录}`。

    只收 `benchmarks.artificial_analysis` 下的三项指数（`null` 视为没有该指数，
    不带字段）。一个模型可能带 `design_arena` 等其它 block，与本项目无关，不碰。

    结构异常（顶层非 dict、`data` 非 list、单条非 dict）逐条跳过，不让一个坏
    条目拖垮整张表。完全没有指数时**不入表**：空记录只会让前端多渲染一个
    「无分」徽章，不如没有条目。
    """
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data")
    if not isinstance(data, list):
        return {}

    entries: list[tuple[list[str], BenchmarkRecord]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
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
                  if (number := _as_score(indices.get(field))) is not None}
        if not scores:
            continue
        source_model = item.get("id")
        # 键来源 id 与展示名两路：上游 id 带厂商前缀（`moonshotai/kimi-k3`），
        # 展示名是另一种写法（`MoonshotAI: Kimi K3`），本项目 id 也可能是第三
        # 种（`kimi-k3`）——都入表，按唯一命中选择（见 lookup）。
        names = [name for name in (item.get("id"), item.get("name"))
                 if isinstance(name, str) and name]
        if not names:
            continue
        entries.append((names, {"source": SOURCE,
                                "source_model": source_model if isinstance(
                                    source_model, str) else "",
                                **scores}))
    return build_table(entries)


async def fetch_openrouter_models(url: str = OPENROUTER_MODELS_URL, *,
                                  transport: Any | None = None,
                                  timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    """拉取 OpenRouter 模型列表原始 JSON；网络 / HTTP 异常向上抛。

    `transport` 只为测试注入（httpx.MockTransport），生产走真实网络。签名与
    `pricing.fetch_models_dev` 一致，调用方（`main` 的刷新协程）照着写即可。
    """
    import httpx

    kwargs: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        kwargs["transport"] = transport
    async with httpx.AsyncClient(**kwargs) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


def benchmarks_path(data_dir: str) -> str:
    return os.path.join(data_dir, BENCHMARKS_FILENAME)


def save_benchmarks(data_dir: str, table: Mapping[str, BenchmarkRecord]) -> None:
    """能力表原子写盘（tmp + replace）；失败只记日志，绝不影响模型列表。"""
    payload = {
        "version": BENCHMARKS_VERSION,
        "saved_at": time.time(),
        "models": dict(table),
    }
    path = benchmarks_path(data_dir)
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            import json

            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as error:
        logger.warning("能力排行落盘失败 %s: %s", path, error)


def load_benchmarks(data_dir: str, *, now: float | None = None) -> BenchmarkTable:
    """读回落盘能力表；缺失 / 损坏 / 版本不符 / 过旧一律退化成空表。"""
    return load_benchmarks_snapshot(data_dir, now=now)[0]


def load_benchmarks_snapshot(
    data_dir: str, *, now: float | None = None,
) -> tuple[BenchmarkTable, float | None]:
    """读回 `(能力表, 快照保存时刻)`；异常与表本身一样安静降级为 `({}, None)`。

    与 `pricing.load_prices_snapshot` 同一口径：这里多返回保存时刻，供管理台
    「模型列表」页展示「这份分数是什么时候拉取的」。
    """
    path = benchmarks_path(data_dir)
    moment = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as handle:
            import json

            raw = json.load(handle)
        if raw.get("version") != BENCHMARKS_VERSION:
            logger.warning("能力排行版本不匹配 %s，忽略", path)
            return {}, None
        saved_at = raw["saved_at"]
        if moment - saved_at > BENCHMARKS_MAX_AGE_SECONDS:
            return {}, None
        models = raw["models"]
        if not isinstance(models, dict):
            return {}, None
        table: BenchmarkTable = {}
        for key, record in models.items():
            # 只认本进程写过的形状：dict + 至少一个指数字段。坏条目跳过而不拖垮
            # 整张表（前端对缺失也有兜底）。
            if not isinstance(record, dict) or not any(
                    field in record for field in _INDEX_FIELDS):
                continue
            table[key] = record
        return table, float(saved_at)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        logger.warning("能力排行读取失败 %s: %s", path, error)
        return {}, None
