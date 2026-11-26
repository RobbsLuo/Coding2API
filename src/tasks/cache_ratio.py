"""缓存命中率校准：用积分余额差反推 TRAE 上游的实际缓存命中率。

**为什么需要**：TRAE 自 2026-10 起不再上报缓存命中（`cache_read_input_tokens`
恒 0、`prompt_cache_pool_record` 恒 null），但计费仍按缓存折扣——直接把未上报
当成 0 会让积分 / 成本估算系统性偏高。上游不告诉我们命中多少，但它的钱袋会
说：同一时段内「按刊例价应付的积分」减去「实际扣掉的积分」，差额只可能来自
缓存折扣。

**推算**（对整窗合并的样本）：

    实际扣费 = 全价应付 − 命中比例 × 缓存省下的钱
    Σ in_m·P_in_m + out_m·P_out_m − r · Σ in_m·(P_in_m − P_cache_m) = spend

解出 `r`。单价用 `provider.trae.pricing.effective_prices`（含实测折扣与有效价
覆盖），单位「积分 / 百万 token」，两边同量级，`r` 无量纲。

**样本口径**：
- TRAE 的 `usage_events` 按 `(凭证, 模型)` 聚合 input/output token，覆盖整个
  窗口；**不按天切**——余额差是「两次探测之间」的净变化，与 UTC 天边界无关，
  按天对齐会把消费与 token 分到不同天（实测按天 0.934，整窗 0.979）。
- `credit_events` 只累加**负向** delta（`delta < 0` 取反）；正向 delta 是签到 /
  奖励 / 补发，不属于请求消费，必须忽略。
- 多凭证合并反解：公式对 token 线性，合并解等价于逐凭证解后按 token 加权平均。
- 只保留 `spend > 0`、分母 > 0、输入 token ≥ `min_input_tokens`、
  `0 ≤ r ≤ 1` 的窗口（浮点轻微越界钳回边界，明显越界的丢弃）。

**写回**：与当前生效值差 ≥ 0.01 才 `runtime.set`（避免每轮抖动）；写入失败
（例如越界）让异常向上抛，由 `TaskRunner._guarded` 记成任务失败并留下日志，
而不是静默吞掉——比例被写坏会直接污染成本估算。

**只读**：本任务只 SELECT，不写任何业务表；唯一副作用是回写运行时配置。
非 TRAE 部署（无 TRAE 凭证 / 无样本）返回空报告、不写配置。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..provider.trae.pricing import effective_prices

if TYPE_CHECKING:
    from ..db.conn import Database
    from ..runtime_settings import RuntimeSettings

logger = logging.getLogger(__name__)

# 回写阈值：与当前值的绝对差达到该值才写，避免反解值在 0.95 附近来回抖动。
WRITE_THRESHOLD = 0.01

# 浮点越界容差：反解值理论上在 [0,1]，浮点误差可能略越界；此范围内钳回边界，
# 超出则视为样本异常（价格表过期 / 余额窗口错位）丢弃。
_RATIO_EPSILON = 1e-9

# `(凭证, 模型)` 的 token 聚合，覆盖**整个窗口**。
#
# 为什么按整窗合并而不是按天对齐：`credit_events` 的余额差是「两次探测之间」
# 的净变化，探测时刻与 UTC 天边界毫无关系；按天切会把一部分消费划到前一天、
# 对应的 token 留在后一天，样本必然错位（实测按天反解 0.934，整窗 0.979，
# 而账单侧独立估算 ≈0.98）。整窗合并让分子分母覆盖同一段时间，多凭证直接相加
# 等价于按 token 加权平均（公式对 token 线性），不再需要取中位数。
#
# 只算成功请求：失败请求的 token 通常为 NULL，但 `ok=0` 行即使带了 token 也
# 不产生消费口径，计入会把分母做大、命中率算低。
_USAGE_SQL = """
SELECT credential_id,
       model,
       SUM(COALESCE(input_tokens, 0)) AS input_tokens,
       SUM(COALESCE(output_tokens, 0)) AS output_tokens
FROM usage_events
WHERE provider = 'trae'
  AND ok = 1
  AND credential_id IS NOT NULL
  AND ts >= ?
  AND ts <= ?
GROUP BY credential_id, model
"""

# 窗口内消费：只看负向 delta（正数是奖励 / 补发，不是请求消费）。
# 只取 provider='trae' 的凭证——credit_events 没有 provider 列，必须借
# credentials 关联；凭证被删后其历史流水自然不参与（无对应 usage 也凑不出样本）。
_SPEND_SQL = """
SELECT c.credential_id AS credential_id,
       SUM(CASE WHEN c.delta < 0 THEN -c.delta ELSE 0 END) AS spend
FROM credit_events AS c
JOIN credentials AS cred ON cred.id = c.credential_id
WHERE cred.provider = 'trae'
  AND c.ts >= ?
  AND c.ts <= ?
GROUP BY c.credential_id
"""


class CacheRatioTask:
    """一轮 = 反解并（在漂移足够时）回写 TRAE 假定缓存命中率。

    `now` 可注入（测试固定时间窗）；`window_days` 是回看天数；样本的输入 token
    不足 `min_input_tokens` 时丢弃——几条小请求的余额差会被探测噪声（签到、
    其他窗口的消费）淹没，反解出的比例不可信。
    """

    def __init__(
        self,
        db: Database,
        runtime: RuntimeSettings,
        *,
        now: Callable[[], float] | None = None,
        window_days: int = 7,
        min_input_tokens: int = 1_000_000,
    ) -> None:
        self._db = db
        self._runtime = runtime
        self._now = now or time.time
        self._window_days = window_days
        self._min_input_tokens = min_input_tokens

    def run_once(self) -> dict[str, Any]:
        """同步跑一轮（SQLite 只读查询，调用方负责放到线程池）。"""
        now = int(self._now())
        since = now - self._window_days * 86_400
        rows, spend, credentials = self._window(since, now)
        ratio = _solve(rows, spend, self._min_input_tokens)
        if ratio is None:
            logger.info("缓存命中率校准：样本不足（%d 个凭证，输入 token 或消费"
                        "不足），本轮不写", credentials)
            return {"applied": False, "ratio": None, "samples": credentials,
                    "window_days": self._window_days, "reason": "样本不足"}

        ratio = round(ratio, 3)
        current = self._runtime.trae_assumed_cache_ratio
        applied = False
        if abs(ratio - current) >= WRITE_THRESHOLD:
            # 写坏比例会直接污染成本估算；失败就让上层记任务失败，不吞异常。
            self._runtime.set("trae_assumed_cache_ratio", ratio)
            applied = True
            logger.info(
                "缓存命中率校准：%d 个凭证合并反解 %.3f，回写假定命中率 %.3f → %.3f",
                credentials, ratio, current, ratio)
        else:
            logger.info(
                "缓存命中率校准：%d 个凭证合并反解 %.3f，与当前 %.3f 差 < %.2f，不写",
                credentials, ratio, current, WRITE_THRESHOLD)
        return {"applied": applied, "ratio": ratio, "samples": credentials,
                "window_days": self._window_days}

    def _window(self, since: int, until: int) -> tuple[list[tuple[str, int, int]], float, int]:
        """窗口内全部 TRAE 用量、总消费与参与计算的凭证数。

        合并整个窗口反解：余额差与 token 覆盖同一段时间，不会像按天切那样在
        UTC 边界错位（实测按天 0.934 vs 整窗 0.979）。多凭证的消费与用量
        直接相加即可——公式对 token 线性，合并解等价于逐凭证解后再按 token
        加权平均，不需要再取中位数。
        """
        conn = self._db.connect()
        rows: list[tuple[str, int, int]] = []
        credentials: set[str] = set()
        for row in conn.execute(_USAGE_SQL, (since, until)):
            rows.append((row["model"], int(row["input_tokens"]),
                         int(row["output_tokens"])))
            credentials.add(row["credential_id"])
        spend = sum(float(row["spend"])
                    for row in conn.execute(_SPEND_SQL, (since, until)))
        return rows, spend, len(credentials)


def _solve(rows: list[tuple[str, int, int]], spend: float,
           min_input_tokens: int) -> float | None:
    """一组 `(模型, 输入, 输出)` + 当日消费 → 命中率；无效样本返回 None。"""
    if spend <= 0:
        return None
    full = 0.0
    denom = 0.0
    priced_input = 0
    for model, input_tokens, output_tokens in rows:
        prices = effective_prices(model)
        if prices is None:
            # 未收录模型不参与推算：缺单价既算不出全价，也算不出缓存省下的钱；
            # 把它计入分母会让比例系统性偏移。
            continue
        input_price, output_price, cache_price = prices
        full += (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        denom += input_tokens * (input_price - cache_price) / 1_000_000
        priced_input += input_tokens
    if denom <= 0 or priced_input < min_input_tokens:
        return None
    ratio = (full - spend) / denom
    if -_RATIO_EPSILON <= ratio < 0:
        return 0.0
    if 1 < ratio <= 1 + _RATIO_EPSILON:
        return 1.0
    if 0 <= ratio <= 1:
        return ratio
    return None