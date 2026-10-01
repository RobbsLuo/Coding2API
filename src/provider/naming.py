"""模型命名归一：六条渠道统一的「三字段」口径。

每个模型对外只有三个字段，来源是同一条链：

1. **raw key**（`raw_id`）：渠道请求时真正发的 key，**永不改动**。
   各渠道内部代号互不相同（Qoder `kmodel_latest` = TRAE `kimi-k3-1`），
   常带厂商命名空间前缀（Kilo 的 `kilo-auto/free`、
   `nvidia/nemotron-3-ultra-550b-a55b:free`），对外一律保留，由别名表在
   转发时换回去。
2. **normalized key**（`normalize_model_key`）：同模型验证 / 合并键。
   剥掉上游的可用性噪声（免费标记）与厂商命名空间前缀，slug 化成
   `kilo-auto`、`longcat-2.5-preview`、`nemotron-3-ultra`。
3. **display name**（`display_model_name`）：展示名。首字母大写、分隔符
   转空格、`LongCat 2.5 Preview`；品牌与缩写按表纠正大小写
   （机械 title-case 会把 `DeepSeek` 写成 `Deepseek`）。

**为什么归一与清洗要分两步**：合并要的是稳定可比的键（`longcat-2.5-preview`），
展示要的是可读文本（`LongCat 2.5 Preview`）。同一函数只做一件事，键再由
展示名 slug 化得到，两者不会各自漂移。

清洗对上游已给的可读名同样适用（Qoder 的 `Qwen3.8-Max` → `Qwen3.8 Max`），
且**幂等**：对清洗结果再跑一次输出不变，故各渠道 client 自行派生过 name 的
情况下也不会出现二次清洗的副作用。
"""

from __future__ import annotations

import re

# 免费档标记：上游用它标记免费模型，是**可用性命名约定**、不是模型名的一部分。
# 实测四种形态（2026-10-01，kilo 一家就占三种）：
#   zen：`longcat-2.5-preview-free`（连字符尾缀）
#   kilo：`kilo-auto/free`（路径段）、`poolside/laguna-s-2.1:free`（冒号尾缀）、
#         展示名 `NVIDIA: Nemotron 3 Ultra (free)`（括号词）
# 注：只删「整段 / 整词 free」，`freeplay` / `freeball` 这类内嵌形式不动。
FREE_MARK = "free"

# 命名空间前缀：上游两种写法都带厂商名——原代号的 `厂商/模型`（kilo 的
# `nvidia/nemotron-3-ultra:free`、`stealth/space-bunny-alpha`）与展示名的
# `厂商: 模型`（kilo 的 `NVIDIA: Nemotron 3 Ultra`）。厂商名不是模型身份的
# 一部分，归一与展示都要去掉，否则同一模型在不同渠道因厂商写法不同而对不上
# （kilo 的 `nvidia/...-ultra` vs zen 的 `nemotron-3-ultra`）。
_PATH_SEP = "/"
# 展示名的厂商前缀 `Vendor: `：冒号后**必须有空白**才算（`a:b` 不动，避免
# 误伤 `qwen3.8:max` 这类无空格写法）。
_NAMESPACE_PREFIX = re.compile(r"^[^:\s][^:]*:\s+")
# 尾部的 free 标记：`-free`（zen）/ `_free` / `:free`（kilo）/ ` Free` /
# `(free)`（kilo 展示名）。**必须有分隔符或括号**，故 `freeplay` 这类内嵌
# 形式不会被误伤。比较忽略大小写。
_TRAILING_FREE = re.compile(r"(?:[\s\-_:]+free|\s*[([]\s*free\s*[)\]])$",
                            re.IGNORECASE)
# 展示分段里独立的 free 词（`(free)` 摘括号后成 `free`）：整段丢弃。
_FREE_WORD = re.compile(r"^free$", re.IGNORECASE)

# 展示名与归一键之间的分隔符集合：连字符 / 下划线 / 空白。点号**不是**分隔
# ——`glm-5.2` / `qwen3.8` 的版本号靠它区分，压成 `glm-5-2` 就换了模型。
_SEGMENT_SEP = re.compile(r"[-_\s]+")
# 归一键里剔除的杂字符：括号（kilo 的 `Kilo (offline)`）等包裹符号。点号与
# 加号保留（版本号 / `qwen3.8-max+`）；非 ASCII 字母数字保留（上游名可能有
# 中文后缀，如「Kimi K3 长尾」——压掉会与「Kimi K3」撞成同一个键）。归一键要
# 直接当 API 的 model 值，带括号会逼用户转义，故连同相邻分隔符一起去掉。
_JUNK_CHARS = re.compile(r"[^\w.+-]+", re.UNICODE)
# 展示分段外层的括号：`_pretty_segment` 只认字母打头，故先摘掉外层括号
# （`Kilo (offline)` → `Kilo Offline`），内部括号仍原样保留。
_WRAPPERS = "()[]{}"

# 品牌与缩写单独维护：id 是小写连字符，机械 title-case 会把 `DeepSeek` /
# `MiMo` / `GLM` / `GPT` 写成 `Deepseek` / `Mimo` / `Glm` / `Gpt`。
_DISPLAY_BRANDS: dict[str, str] = {
    "deepseek": "DeepSeek", "qwen": "Qwen", "kimi": "Kimi", "mimo": "MiMo",
    "longcat": "LongCat", "nemotron": "Nemotron", "glm": "GLM", "gpt": "GPT",
    "doubao": "Doubao", "minimax": "MiniMax", "seed": "Seed",
}
_DISPLAY_ACRONYMS: frozenset[str] = frozenset(
    {"ai", "vl", "vlm", "llm", "sft", "tts", "ocr", "moe", "oss"})
# 段内拆成「字母部分 + 其余」（`qwen3.8` → `qwen` + `3.8`、`v2.6` → `v` +
# `2.6`），数字/版本段原样保留，只对字母部分做大小写处理。
_SEGMENT_WORD = re.compile(r"([A-Za-z]+)([^A-Za-z]*)")


def _pretty_segment(segment: str) -> str:
    """单个分段 → 展示用片段（`nemotron` → `Nemotron`、`2.6` → `2.6`）。"""
    # 外层括号不是词的一部分：`(offline)` → `offline`。只摘最外层，
    # `K2.7(code)` 这类内嵌写法内部括号原样保留。
    inner = segment.strip(_WRAPPERS).strip()
    match = _SEGMENT_WORD.match(inner)
    if match is None:
        return inner                        # 纯数字/符号段，去括号后原样
    word, rest = match.groups()
    lowered = word.lower()
    if lowered in _DISPLAY_BRANDS:
        head = _DISPLAY_BRANDS[lowered]
    elif lowered in _DISPLAY_ACRONYMS:
        head = lowered.upper()
    else:
        head = word[:1].upper() + word[1:].lower()
    return head + rest


def _is_free_segment(segment: str) -> bool:
    """该分段是否只是 free 标记本身。

    连外层括号一起看（`(free)` / `[free]` 都算），故 kilo 展示名里的
    「(free)」在摘括号后能被整段丢掉；而 `freeplay` 不是整词，不算。
    """
    return _FREE_WORD.match(segment.strip(_WRAPPERS).strip()) is not None


def _strip_noise(value: str) -> str:
    """去掉命名空间前缀与免费标记，留下模型名本体。

    上游的噪声共两类、四种形态（实测 2026-10-01，kilo 一家占三种）：

    * **命名空间前缀**，不是模型身份的一部分，留着会让同一模型在不同渠道
      因厂商写法不同而对不上：
        - 路径式 `厂商/模型`（kilo 原代号 `nvidia/nemotron-3-ultra:free`、
          `stealth/space-bunny-alpha`）→ 取**最后一段**；
        - 冒号式 `厂商: 模型`（kilo 展示名 `NVIDIA: Nemotron 3 Ultra`）→
          削掉前缀（要求冒号后有空白，`a:b` 不动）。
    * **免费标记**，只是可用性命名约定、不是模型名的一部分：
        - 路径段 `.../free`（kilo `kilo-auto/free`）→ 整段丢掉；
        - 尾缀 `-free` / `_free`（zen `longcat-2.5-preview-free`）/ `:free`
          （kilo `laguna-s-2.1:free`）/ ` Free` / `(free)`（kilo 展示名）
          → 尾部削掉。

    比较一律忽略大小写（上游有 `GAMMA-FREE` 这类写法）。剥完只剩 free 噪声
    （`-free` / `free`）时返回空串，由调用方决定回退原 id —— 宁可显示原始
    id，也不要给模型安一个叫「Free」的名字。
    """
    parts = [part for part in value.split(_PATH_SEP)
             if part.strip().lower() != FREE_MARK]
    # 路径被 free 段占满（`free` / `free/free`）时没有模型名可取
    stem = parts[-1] if parts else ""
    stem = _NAMESPACE_PREFIX.sub("", stem)
    return _TRAILING_FREE.sub("", stem)


def normalize_model_key(raw: str) -> str:
    """渠道原始 key → 归一键（去 free / 去命名空间前缀 / slug 化）。

    `kilo-auto/free` → `kilo-auto`，`stealth/space-bunny-alpha` →
    `space-bunny-alpha`，`longcat-2.5-preview-free` → `longcat-2.5-preview`，
    `glm-5.2` → `glm-5.2`。多渠道合并后的对外 id 也由展示名经本函数得到，
    故输出一定是 slug（小写、无空格），可直接当 API 的 model 值。

    清洗后为空（`-free`、`   ` 这类退化 key）时回退原串小写，调用方据此判断
    「拿不到干净名字」并回退展示原始 id，而不是产出空 id。
    """
    cleaned = _strip_noise(raw.strip())
    slug = _SEGMENT_SEP.sub("-", _JUNK_CHARS.sub("-", cleaned)).strip("-").lower()
    return slug or raw.strip().lower()


def display_model_name(source: str) -> str:
    """原始 key 或上游可读名 → 展示名。

    `longcat-2.5-preview-free` → `LongCat 2.5 Preview`，`Qwen3.8-Max` →
    `Qwen3.8 Max`，`Hy4 preview` → `Hy4 Preview`。上游没给名字时直接传 id，
    同一条路径产出可用名。

    清洗后为空（整个名字就是 `free` 之类）时返回 `""`，由调用方回退原始 id。
    """
    # 括号里的 free 在摘括号后变成独立的一段（kilo 的 `Nemotron 3 Ultra
    # (free)`），此处按段剔掉。`(free)` / `[free]` / ` Free` 都命中，而
    # `freeplay` 这类内嵌形式不动。
    text = _strip_noise(source.strip())
    return " ".join(
        _pretty_segment(segment)
        for segment in _SEGMENT_SEP.split(text)
        if segment and not _is_free_segment(segment))
