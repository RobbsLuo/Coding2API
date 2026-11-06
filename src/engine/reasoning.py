"""reasoning 档位映射与思考内容过滤（纯函数，无状态）。

三件事，都是**机制**而非策略——默认全部不生效，配置由使用者显式给出：

1. `parse_effort_map` / `map_effort`：出站把客户端显式给的 `reasoning_effort`
   改写成渠道期望的档位（各渠道对档位的接受度不同，且**未逐一核实**，
   故不内置默认矩阵）。
2. `parse_thinking_efforts` / `thinking_effort`：Anthropic 入站的
   `thinking.budget_tokens` → `reasoning_effort`。Anthropic 只规定
   `budget_tokens ≥ 1024`、未给「预算 ↔ 档位」对应表，任何固定阈值都是本项目
   自己的策略，因此阈值表必须由使用者显式给出，留空即完全不映射。
3. 思考内容剥离（`force_disable_thinking`）不在这里：它是「过滤事件流」，
   落在 executor 消费上游事件的循环里，见 `engine/executor.py`。

解析一律**宽容**（与 `parse_fallback_groups` 同风格）：忽略空段 / 缺 `:` /
缺 `=` 的坏段，一行坏配置不能让整个网关起不来，也不能因一个笔误丢掉别的
合法条目。
"""

from __future__ import annotations

# 目标档位写成 `none` 表示「删除该键、走渠道默认」，而不是把字符串 "none"
# 发给上游——上游未必认这个档位，而「不发送」是所有渠道都接受的语义。
DELETE_EFFORT = "none"

# 出站映射里的兜底渠道键：命中不到具体渠道时用它。
WILDCARD_CHANNEL = "*"


def parse_effort_map(raw: str) -> dict[str, dict[str, str]]:
    """档位映射文本 → `{渠道: {源档位: 目标档位}}`。

    格式：`渠道=from:to,from:to;*=from:to`。分隔：`;` 分渠道段、`=` 分
    「渠道 / 映射表」、`,` 分条目、`:` 分「源 / 目标」。渠道取
    `KNOWN_PROVIDERS` 的取值或 `*`（兜底）；源 / 目标都去首尾空白。
    重复渠道后者覆盖前者；同渠道内重复源档位后者覆盖前者。留空 = 无映射。
    """
    mapping: dict[str, dict[str, str]] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        channel, _, entries_raw = chunk.partition("=")
        channel = channel.strip()
        if not channel:
            continue
        entries: dict[str, str] = {}
        for entry in entries_raw.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            source, _, target = entry.partition(":")
            source, target = source.strip(), target.strip()
            if not source or not target:
                continue
            entries[source] = target
        if entries:
            mapping[channel] = entries
    return mapping


def map_effort(mapping: dict[str, dict[str, str]], provider_id: str | None,
               effort: str) -> str | None:
    """按渠道改写档位：返回新档位；返回 `None` 表示「删除该键」。

    渠道自身优先、`*` 兜底。同一渠道命中多条时以**源档位精确匹配**为准；
    未命中任何条目时原样返回 `effort`（调用方据此判断「无需改写」）。
    """
    entries = mapping.get(provider_id or "") or mapping.get(WILDCARD_CHANNEL)
    if not entries:
        return effort
    target = entries.get(effort)
    if target is None:
        return effort
    return None if target == DELETE_EFFORT else target


def parse_thinking_efforts(raw: str) -> tuple[str | None, tuple[tuple[int, str], ...]]:
    """`thinking.budget_tokens` 阈值表 → `(disabled 档位, 阈值表)`。

    格式：`4096:low,32768:medium,disabled:high`。条目 `disabled:档位` 匹配
    `thinking.type == "disabled"`；其余条目 `预算:档位` 是阈值（预算 ≥ 该值
    即候选）。阈值表按预算**降序**返回，取第一个满足的即最大的一档；同一预算
    重复时后者覆盖前者。无法解析成整数的段被忽略。留空 = `(None, ())`，
    调用方据此完全不映射。
    """
    disabled: str | None = None
    thresholds: dict[int, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        key, _, value = entry.partition(":")
        key, value = key.strip(), value.strip()
        if not key or not value:
            continue
        if key == "disabled":
            disabled = value
            continue
        try:
            budget = int(key)
        except ValueError:
            continue
        thresholds[budget] = value
    return disabled, tuple(sorted(thresholds.items(), reverse=True))


def output_config_effort(output_config: object) -> str | None:
    """Anthropic `output_config` → `reasoning_effort`；`None` = 不映射。

    Claude Code 2.1.318 起「`/effort` 选了哪档」落在 `output_config.effort`
    （取值 `low|medium|high|xhigh|max`），并与 `thinking:{type:"adaptive"}`
    一起发送。此前本网关整块丢弃 `output_config`，用户改档位**静默无效**且
    没有任何报错——比 400 更难排查，故这里原样透传取值。

    只认 `effort` 一个键：`output_config` 的其他成员（格式等）chat 无对应
    表达，仍按原样忽略，避免把未核实的语义猜成上游字段。
    """
    if not isinstance(output_config, dict):
        return None
    effort = output_config.get("effort")
    if not isinstance(effort, str) or not effort.strip():
        return None
    return effort.strip().lower()


def thinking_effort(raw: str, thinking: object) -> str | None:
    """Anthropic `thinking` 对象 → `reasoning_effort`；`None` = 不映射。

    只认两种形态：`type == "disabled"` 取 disabled 档位；`type == "enabled"`
    按 `budget_tokens` 取「阈值 ≤ 预算」中最大的那一档，低于最小阈值则不映射。
    其余形态（缺 `type` / `adaptive` 等未经核实的扩展）一律不映射——猜错会
    把客户端「不要思考」的意图翻成「高预算思考」。
    """
    if not isinstance(thinking, dict):
        return None
    disabled, thresholds = parse_thinking_efforts(raw)
    if not thresholds and disabled is None:
        return None
    kind = thinking.get("type")
    if kind == "disabled":
        if disabled is None:
            return None
        return None if disabled == DELETE_EFFORT else disabled
    if kind != "enabled":
        return None
    budget = thinking.get("budget_tokens")
    if not isinstance(budget, int) or isinstance(budget, bool):
        return None
    for threshold, effort in thresholds:
        if budget >= threshold:
            return None if effort == DELETE_EFFORT else effort
    return None


# 档位从高到低（只覆盖已确认存在的档位）。降级链只在这条链上走一步：
# 未知档位（渠道私有写法）不猜，直接放弃重试——猜错会把「高」降成别的语义。
# `max` 是 Codex 0.16x 起的新顶档（`codex-rs/protocol/src/openai_models.rs`
# 的 `ReasoningEffort::Max`），必须排在 `xhigh` 之上，否则「max 被拒」时
# 降级链直接放弃重试，用户只看到 400。
EFFORT_ORDER: tuple[str, ...] = ("max", "xhigh", "high", "medium", "low", "minimal")

# 客户端新写法 → 线上档位。只收**语义明确**的别名：
# `ultra`（Codex 的 Ultra）是「最高档」的另一种叫法，落到 `max`；
# `persistent` 在 Codex 里序列化成线上值 `"disabled"`（见
# `client_tests.rs::reasoning_effort_for_requests_preserves_non_ultra_and_persistent_behavior`），
# 它不是「更高」的档位，无法参与降级链，故不映射（保留 None = 放弃重试）。
_EFFORT_ALIASES: dict[str, str] = {"ultra": "max"}

# 「上游拒收该推理档位」的错误文案特征。保守到只认点名 effort / budget 的写法：
# 泛化到「invalid」会把普通的参数错误也当成档位问题，白白多打一次上游。
_EFFORT_REJECTION_MARKERS: tuple[str, ...] = (
    "reasoning_effort", "reasoning effort", "unsupported effort",
    "invalid effort", "effort level", "budget_tokens", "thinking budget",
    "思考强度", "推理强度", "思考档位", "推理档位",
)


def downgrade_effort(effort: str) -> str | None:
    """降一档；已是最低档 / 未知档位返回 None（调用方据此放弃重试）。"""
    normalized = effort.strip().lower()
    normalized = _EFFORT_ALIASES.get(normalized, normalized)
    try:
        index = EFFORT_ORDER.index(normalized)
    except ValueError:
        return None
    return EFFORT_ORDER[index + 1] if index + 1 < len(EFFORT_ORDER) else None


def is_effort_rejection(message: str | None) -> bool:
    """错误文案是否「不认这个推理档位」（大小写不敏感，None 视为否）。"""
    if not isinstance(message, str) or not message:
        return False
    text = message.lower()
    return any(marker in text for marker in _EFFORT_REJECTION_MARKERS)
