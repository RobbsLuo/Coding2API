"""TRAE 单请求积分推算。

上游 `token_usage` 帧不含积分字段（实测确认，只有 token 数），所以单请求积分
只能按官方计费公式**推算**，推算值在展示层标 `≈`。

官方公式（docs.trae.cn/enterprise_billing-items）：

    积分 = (输入 token − 缓存命中 token) × 输入单价
         + 输出 token × 输出单价
         + 缓存命中 token × 缓存单价

单价单位「积分 / 百万 token」。换算关系（实测反推）：

- 官方刊例价单位是「元 / 百万 token」，乘 **40** 得积分 / 百万：
  qwen-3.7-plus 2 元/M → 80 积分/M、kimi-k2.7-code 6.5 → 260、
  minimax-m3 2.1 → 84、step-5-preview 输出 20 → 800，四个模型一致。
- 部分模型有账号身份 / 限时活动 / 闲时折扣（官方「内置模型限时折扣」页），
  实测有效价低于刊例价，见 `MEASURED_DISCOUNT`。

未收录的模型返回 None（不推算，展示层仍显示 `—`），避免用错误单价误导。
单价与折扣会随官方调价 / 活动变化，改动集中在本模块。

换算常数与折扣的实测方法见 `scripts/probe_trae_credit_rate.py`：对同一凭证
串行「探额度 → 发一次最小对话 → 再探额度」，用两次额度差除以 token 数即可
反推出该模型的积分 / 百万，多模型多轮拟合 R²≈1 即得本表。
"""

from __future__ import annotations

# 官方刊例价（元 / 百万 token）：(输入, 输出, 缓存读取/命中)
# 来源 docs.trae.cn/enterprise_billing-items 的「模型价格参考表」。
# key 为 TRAE 模型 id 的小写形式。
LIST_PRICES_CNY: dict[str, tuple[float, float, float]] = {
    "doubao-seed-evolving": (6.0, 30.0, 1.2),
    "doubao-seed-2.1-pro": (6.0, 30.0, 1.2),
    "doubao-seed-2.1-turbo": (3.0, 15.0, 0.6),
    "doubao-seed-2.0-code": (3.2, 16.0, 0.64),
    "step-5-preview": (7.0, 20.0, 0.35),
    "glm-5.3-flash": (0.8, 2.8, 0.23),
    "glm-5.3": (8.0, 28.0, 2.0),
    "glm-5.2": (8.0, 28.0, 2.0),
    "glm-5": (4.0, 18.0, 1.0),
    "minimax-m3": (2.1, 8.4, 0.42),
    "qwen3.8-max": (12.0, 36.0, 2.4),
    "qwen-3.7-plus": (2.0, 8.0, 0.4),
    "kimi-k3": (20.0, 100.0, 2.0),
    "kimi-k2.8-preview": (6.5, 27.0, 1.7),
    "kimi-k2.7-code": (6.5, 27.0, 1.3),
    # kimi-k2.6 官方未单列，实测系数与 k2.7-code 一致，按后者近似
    "kimi-k2.6": (6.5, 27.0, 1.3),
    "deepseek-v4.1-flash": (2.0, 8.0, 0.04),
    "deepseek-v4-pro": (4.8, 9.6, 0.4),            # 活动价
    "deepseek-v4-pro-official": (9.0, 27.0, 0.3),  # 正式版
    "deepseek-v4-flash": (3.0, 9.0, 0.1),          # 正式版
    "deepseek-v4-flash-official": (3.0, 9.0, 0.1),
}

# 积分 / 元：官方元价 × 40 = 积分 / 百万（见模块 docstring 实测反推）。
CREDITS_PER_YUAN = 40.0

# 实测有效折扣（官方价 × 40 × 折扣 = 实测积分 / 百万）。折扣随账号身份、
# 限时活动、闲时时段变化，非固定值；未列出的模型按 1.0（无折扣）。
#   - glm-5.2 / glm-5.3：会员专属补贴，实测 0.675
#   - deepseek-v4.1-flash：闲时 5 折（探测时处于闲时时段），实测 0.35
MEASURED_DISCOUNT: dict[str, float] = {
    "glm-5.2": 0.675,
    "glm-5.3": 0.675,
    "deepseek-v4.1-flash": 0.35,
}

# 积分保留到 4 位小数：计费量子为 0.0004，4 位足够，避免浮点长尾。
_QUANTUM = 4


def effective_prices(model: str) -> tuple[float, float, float] | None:
    """模型 → 实际积分单价 (输入, 输出, 缓存命中) / 百万 token；未收录返回 None。"""
    key = model.strip().lower()
    entry = LIST_PRICES_CNY.get(key)
    if entry is None:
        return None
    factor = CREDITS_PER_YUAN * MEASURED_DISCOUNT.get(key, 1.0)
    return (entry[0] * factor, entry[1] * factor, entry[2] * factor)


def estimate_credit(model: str, *, input_tokens: int | None,
                    output_tokens: int | None,
                    cached_tokens: int | None = None) -> float | None:
    """按官方公式推算单请求积分；模型未收录或缺输入 token 时返回 None。"""
    prices = effective_prices(model)
    if prices is None or not _is_int(input_tokens):
        return None
    input_price, output_price, cache_price = prices
    cached = cached_tokens if _is_int(cached_tokens) else 0
    cached = max(0, min(cached, input_tokens))
    output = output_tokens if _is_int(output_tokens) else 0
    total = ((input_tokens - cached) * input_price
             + output * output_price
             + cached * cache_price)
    return round(total / 1_000_000, _QUANTUM)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)