"""上游官方状态页聚合：抓取各模型 / 网关提供方的公开状态接口并归一化。

为什么要有这一页：本服务自己的 429/5xx 冷却只能回答「这个号现在不行」，回答
不了「是上游整体挂了，还是我这边凭证 / 额度的问题」。官方状态页恰好补上另一半，
两者合起来才定位得了。**只做解释，不做决策**——状态页滞后于我们自己的实时
冷却（见 `src/provider/kilo/client.py` 的模型级冷却），把它并入选号 / 熔断只会
让调度更慢更糊涂。

为什么只存内存、不落盘：状态数据分钟级易变，重启后拿一份几小时前的「一切正常」
比显示「尚未抓取」危险得多。

为什么按「提供方」而不是按本站渠道组织：有状态页的是模型方与网关方，而本站的
渠道是编码 IDE（codebuddy / trae / qoder / codearts）。只有 zen / kilo 与提供方
一一对应，其余靠 `related_providers` 标注关联，**不做模型名模糊匹配**——那是一个
必然出错的启发式。

四种协议（各自一个纯函数解析器；加源只需在 `SOURCE_SPECS` 加一行）：
- `statuspage`：Atlassian Statuspage v2 的 `/api/v2/summary.json`（多家共用）。
- `opencode`：OpenCode Zen 的 `/api/status`（自定义 JSON，**逐模型 TTFT/tps**）。
- `betterstack`：Better Stack 的 `/index.json`（JSON:API，逐资源可用率）。
- `atom`：DeepSeek 的 `/history.atom`（Flashduty 产出：只有事故史、无当前组件态）。

本轮不做的源（仅 HTML，无稳定机器可读端点）：Groq（incident.io 前端渲染）、
腾讯云 `status.tencentcloud.com`（Next.js SPA）、阿里云 `status.aliyun.com`
（SPA）、华为云（域名解析失败）、SiliconFlow（本地代理下 TLS 失败）。做之前先
确认端点能稳定返回 JSON / XML，否则等于每天修一次 HTML 解析。
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

#: defusedxml 复用 stdlib 的 Element 类型（只是把解析入口换成安全版）。
from xml.etree.ElementTree import Element as XmlElement

import httpx

# Atom 事故史来自外部站点：用 defusedxml 而非 stdlib ElementTree——后者对
# 「十亿笑声 / 实体展开」这类 XXE / DoS 载荷不设防（bandit B314）。
from defusedxml.ElementTree import ParseError as XmlParseError
from defusedxml.ElementTree import fromstring as xml_fromstring

# ------------------------------------------------------------------ 健康档位

LEVEL_OPERATIONAL = "operational"
LEVEL_DEGRADED = "degraded"
LEVEL_PARTIAL = "partial"
LEVEL_MAJOR = "major"
LEVEL_MAINTENANCE = "maintenance"
LEVEL_UNKNOWN = "unknown"

# 严重度排序（`aggregate_level` 取最差）：数值越大越严重。
# `unknown` 给 -1——抓取失败不该压过真实故障：一个源连不上，其余源报的
# 「部分中断」才是这一页该突出的信息。全源 unknown 时 max 仍落到 unknown。
_LEVEL_RANK: dict[str, int] = {
    LEVEL_MAJOR: 4,
    LEVEL_PARTIAL: 3,
    LEVEL_DEGRADED: 2,
    LEVEL_MAINTENANCE: 1,
    LEVEL_OPERATIONAL: 0,
    LEVEL_UNKNOWN: -1,
}

# Statuspage v2 顶层 `status.indicator` → 档位。
_STATUSPAGE_INDICATOR: dict[str, str] = {
    "none": LEVEL_OPERATIONAL,
    "minor": LEVEL_DEGRADED,
    "major": LEVEL_MAJOR,
    # critical 比 major 更重，但对外只暴露六档，多一档只会让前端配色更碎。
    "critical": LEVEL_MAJOR,
    "maintenance": LEVEL_MAINTENANCE,
}

# Statuspage v2 组件 `status` → 档位。
_STATUSPAGE_COMPONENT: dict[str, str] = {
    "operational": LEVEL_OPERATIONAL,
    "degraded_performance": LEVEL_DEGRADED,
    "partial_outage": LEVEL_PARTIAL,
    "major_outage": LEVEL_MAJOR,
    "under_maintenance": LEVEL_MAINTENANCE,
}

# Statuspage v2 事故 `impact` → 档位（`none` 是信息性通报，按 degraded 处理）。
_STATUSPAGE_IMPACT: dict[str, str] = {
    "none": LEVEL_DEGRADED,
    "minor": LEVEL_DEGRADED,
    "major": LEVEL_MAJOR,
    "critical": LEVEL_MAJOR,
}

# OpenCode Zen / Better Stack 共用的「当前态」词表 → 档位。
# `down` 只出现在 Zen；`downtime` 只出现在 Better Stack。
_LIVE_STATUS: dict[str, str] = {
    "operational": LEVEL_OPERATIONAL,
    "degraded": LEVEL_DEGRADED,
    "downtime": LEVEL_MAJOR,
    "down": LEVEL_MAJOR,
    "maintenance": LEVEL_MAINTENANCE,
}

# OpenCode 的 `reason` → 中文短语（拼进 detail / 事故标题）。
_REASON_LABEL: dict[str, str] = {
    "latency": "延迟",
    "upstream": "上游",
    "error": "报错",
}

# DeepSeek 事故标题里的关键词 → 档位。命中不到时按 degraded（有事故总不是好事）。
_DEEPSEEK_TITLE_LEVEL: tuple[tuple[str, str], ...] = (
    ("大规模", LEVEL_MAJOR),
    ("严重", LEVEL_MAJOR),
    ("不可用", LEVEL_MAJOR),
    ("部分中断", LEVEL_PARTIAL),
    ("中断", LEVEL_PARTIAL),
    ("性能下降", LEVEL_DEGRADED),
    ("degraded", LEVEL_DEGRADED),
    ("maintenance", LEVEL_MAINTENANCE),
    ("维护", LEVEL_MAINTENANCE),
)

# 单源最多保留的事故条数：状态页只返回未恢复 + 近期已恢复，但 OpenAI 这类
# 条目很多的源会把整张卡片撑到几百行。
MAX_INCIDENTS = 5

# 抓取用的 UA：Statuspage 背后的 Cloudflare 会拒空 UA / 默认 UA。
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Accept": "application/json, text/xml, text/plain, */*",
}

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
# 剥标签会留下「monitoring .」这类空格（`</b>.` → ` .`）：把标点前的空白收掉。
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?%)\]])")


# ------------------------------------------------------------------ 数据模型


@dataclass(frozen=True)
class SourceSpec:
    """一个状态源的静态描述（声明式：加源 = 在 SOURCE_SPECS 加一行）。"""

    key: str
    label: str
    #: 机器可读端点（JSON / XML）
    api_url: str
    #: 给人看的状态页地址（卡片上的外链）
    page_url: str
    #: 解析器选择
    kind: Literal["statuspage", "opencode", "betterstack", "atom"]
    #: `channel` = 与本站某个渠道一一对应；`model` = 模型 / 上游方，经渠道间接使用
    scope: Literal["channel", "model"]
    #: 本站渠道 key（仅 channel 级有意义；见 src/config.py 的 KNOWN_PROVIDERS）
    related_providers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Component:
    """一个组件 / 模型的当前状态（详情文本由各解析器拼好，前端不再二次加工）。"""

    name: str
    level: str
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "level": self.level, "detail": self.detail}


@dataclass(frozen=True)
class Incident:
    """一条事故（进行中或已恢复）。

    `ongoing` 是归一化后的生命周期：源侧写法各异（Statuspage 的
    investigating/identified/monitoring、OpenCode 的 resolvedAt 是否为空、
    DeepSeek 正文里的 `Status: resolved`），前端只认这一个布尔。
    """

    title: str
    level: str
    ongoing: bool
    updated_at: int | None = None
    url: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "level": self.level,
            "ongoing": self.ongoing,
            "updated_at": self.updated_at,
            "url": self.url,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SourceStatus:
    """一个源的一次抓取结果（成功或失败都在这里，失败带 `error`）。"""

    spec: SourceSpec
    level: str
    components: tuple[Component, ...] = ()
    incidents: tuple[Incident, ...] = ()
    fetched_at: int = 0
    error: str | None = None
    #: 源侧自己声明的「数据已过期」（目前只有 OpenCode Zen 提供）
    source_stale: bool = False

    @property
    def abnormal(self) -> bool:
        """该源是否处于非正常态（含抓取失败）。"""
        return self.level != LEVEL_OPERATIONAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.spec.key,
            "label": self.spec.label,
            "scope": self.spec.scope,
            "page_url": self.spec.page_url,
            "related_providers": list(self.spec.related_providers),
            "level": self.level,
            "fetched_at": self.fetched_at,
            "error": self.error,
            "source_stale": self.source_stale,
            "components": [item.as_dict() for item in self.components],
            "incidents": [item.as_dict() for item in self.incidents],
        }


# ------------------------------------------------------------------ 源清单

SOURCE_SPECS: tuple[SourceSpec, ...] = (
    # 渠道级：与本站渠道一一对应，出问题就是「我们的入口」出问题。
    SourceSpec("opencode", "OpenCode Zen", "https://status.opencode.ai/api/status",
               "https://status.opencode.ai", "opencode", "channel", ("zen",)),
    SourceSpec("kilo", "Kilo", "https://status.kilo.ai/api/v2/summary.json",
               "https://status.kilo.ai", "statuspage", "channel", ("kilo",)),
    # 模型 / 上游级：经 zen / kilo / trae 等渠道间接使用，用于判断「模型报错
    # 是不是上游全局问题」。顺序按与本服务的相关度排。
    SourceSpec("deepseek", "DeepSeek", "https://status.deepseek.com/history.atom",
               "https://status.deepseek.com", "atom", "model"),
    SourceSpec("together", "Together AI", "https://status.together.ai/index.json",
               "https://status.together.ai", "betterstack", "model"),
    SourceSpec("moonshot", "Moonshot / Kimi",
               "https://status.moonshot.cn/api/v2/summary.json",
               "https://status.moonshot.cn", "statuspage", "model"),
    SourceSpec("minimax", "MiniMax", "https://status.minimaxi.com/api/v2/summary.json",
               "https://status.minimaxi.com", "statuspage", "model"),
    SourceSpec("openai", "OpenAI", "https://status.openai.com/api/v2/summary.json",
               "https://status.openai.com", "statuspage", "model"),
    SourceSpec("anthropic", "Anthropic", "https://status.claude.com/api/v2/summary.json",
               "https://status.claude.com", "statuspage", "model"),
)


def spec_by_key(key: str) -> SourceSpec | None:
    """按 key 找源（`SOURCE_SPECS` 的键唯一，由测试守着）。"""
    return next((spec for spec in SOURCE_SPECS if spec.key == key), None)


# ------------------------------------------------------------------ 工具


def aggregate_level(levels: Iterable[str]) -> str:
    """取最差档位（见 `_LEVEL_RANK`）；空集合视作 `unknown`。"""
    ranked = [(_LEVEL_RANK.get(level, -1), level) for level in levels]
    if not ranked:
        return LEVEL_UNKNOWN
    return max(ranked)[1]


def _as_int(value: Any) -> int | None:
    """尽力取整数：上游混用字符串 / 浮点 / 毫秒时间戳。取不到给 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _as_ms_epoch(value: Any) -> int | None:
    """毫秒时间戳 → 秒；小于 1e11 的值当作已经是秒（避免把秒乘成万年）。"""
    number = _as_int(value)
    if number is None or number <= 0:
        return None
    return number // 1000 if number >= 100_000_000_000 else number


def _as_iso_epoch(value: Any) -> int | None:
    """ISO8601 → epoch 秒（DeepSeek 的 Atom 用 `+08:00` 带偏移写法）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(datetime.fromisoformat(value.strip()).timestamp())
    except ValueError:
        return None


def _text(raw: Any, limit: int = 300) -> str | None:
    """HTML / 空白归一成一行纯文本（Atom 的 summary 是转义过的 HTML 片段）。"""
    if not isinstance(raw, str):
        return None
    plain = _WHITESPACE.sub(" ", html.unescape(_HTML_TAG.sub(" ", raw))).strip()
    plain = _SPACE_BEFORE_PUNCT.sub(r"\1", plain).strip()
    if not plain:
        return None
    return plain if len(plain) <= limit else plain[:limit].rstrip() + "…"


def _level_of(mapping: dict[str, str], raw: Any) -> str:
    """查表归一档位；未知取值一律回落 `unknown`，绝不透传原始字符串。"""
    if not isinstance(raw, str):
        return LEVEL_UNKNOWN
    return mapping.get(raw.strip().lower(), LEVEL_UNKNOWN)


def _speed_detail(speed: Any, prefix: str = "") -> str | None:
    """OpenCode 的 `speed` → `TTFT 2.3s · 195 tok/s`（缺项就少一段）。"""
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    if isinstance(speed, dict):
        ttft = _as_int(speed.get("ttft"))
        tps = _as_int(speed.get("tps"))
        if ttft is not None and ttft > 0:
            parts.append(f"TTFT {ttft / 1000:.1f}s")
        if tps is not None and tps > 0:
            parts.append(f"{tps} tok/s")
    return " · ".join(parts) if parts else None


def _reason_text(reason: Any) -> str | None:
    if not isinstance(reason, str) or reason in ("", "none"):
        return None
    return _REASON_LABEL.get(reason, reason)


def _short_error(error: Exception) -> str:
    """异常 → 短消息（进 API 响应与页面）。这些端点都是公开地址，无需脱敏。"""
    message = _WHITESPACE.sub(" ", str(error)).strip()
    if not message:
        return type(error).__name__
    return f"{type(error).__name__}: {message[:200]}"


# ------------------------------------------------------------------ 解析器


def parse_statuspage(spec: SourceSpec, payload: Any, *, now: int) -> SourceStatus:
    """Atlassian Statuspage v2 `/api/v2/summary.json`。

    顶层 `status.indicator` 是整页结论，`components[]` 是逐组件状态。
    `group: true` 的组件只是分组标题（无自身状态），跳过。
    """
    if not isinstance(payload, dict):
        raise ValueError("statuspage payload is not an object")

    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    level = _level_of(_STATUSPAGE_INDICATOR, status.get("indicator"))

    components: list[Component] = []
    for item in _dicts(payload.get("components")):
        if item.get("group"):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        components.append(Component(
            name=name.strip(),
            level=_level_of(_STATUSPAGE_COMPONENT, item.get("status")),
        ))

    incidents: list[Incident] = []
    for item in _dicts(payload.get("incidents")):
        title = item.get("name")
        if not isinstance(title, str) or not title.strip():
            continue
        incidents.append(Incident(
            title=title.strip(),
            level=_level_of(_STATUSPAGE_IMPACT, item.get("impact")),
            ongoing=_is_ongoing(item.get("status")),
            updated_at=_as_iso_epoch(item.get("updated_at") or item.get("created_at")),
            url=_str_or_none(item.get("shortlink")),
            detail=_latest_update_body(item.get("incident_updates")),
        ))
        if len(incidents) >= MAX_INCIDENTS:
            break

    return SourceStatus(spec=spec, level=level, components=tuple(components),
                        incidents=tuple(incidents), fetched_at=now)


def parse_opencode(spec: SourceSpec, payload: Any, *, now: int) -> SourceStatus:
    """OpenCode Zen `/api/status`：逐服务 + **逐模型** TTFT/tps。

    这是唯一给出实时性能数字的源，且直接对应本站 zen 渠道。组件行优先展开
    `breakdown`（逐模型），没有 breakdown 的服务退回服务级一行。
    """
    if not isinstance(payload, dict):
        raise ValueError("opencode payload is not an object")

    level = _level_of(_LIVE_STATUS, payload.get("status"))

    components: list[Component] = []
    for item in _dicts(payload.get("components")):
        service = item.get("service")
        service_name = service.strip() if isinstance(service, str) else "?"
        breakdown = _dicts(item.get("breakdown"))
        if breakdown:
            for entry in breakdown:
                model = entry.get("id")
                if not isinstance(model, str) or not model.strip():
                    continue
                components.append(Component(
                    name=model.strip(),
                    level=_level_of(_LIVE_STATUS, entry.get("status")),
                    detail=_speed_detail(entry.get("speed"), service_name),
                ))
            continue
        components.append(Component(
            name=service_name,
            level=_level_of(_LIVE_STATUS, item.get("status")),
            detail=_reason_text(item.get("reason")),
        ))

    incidents: list[Incident] = []
    for item in _dicts(payload.get("incidents")):
        service = item.get("service")
        service_name = service.strip() if isinstance(service, str) else "?"
        models = [str(entry.get("id")) for entry in _dicts(item.get("models"))
                  if isinstance(entry.get("id"), str)]
        reason = _reason_text(item.get("reason"))
        title = f"{service_name} · {', '.join(models)}" if models else service_name
        if reason:
            title = f"{title}（{reason}）"
        incidents.append(Incident(
            title=title,
            level=_level_of(_LIVE_STATUS, item.get("status")),
            ongoing=_as_int(item.get("resolvedAt")) is None,
            updated_at=_as_ms_epoch(item.get("resolvedAt") or item.get("startedAt")),
            url=None,
        ))
        if len(incidents) >= MAX_INCIDENTS:
            break

    return SourceStatus(spec=spec, level=level, components=tuple(components),
                        incidents=tuple(incidents), fetched_at=now,
                        source_stale=bool(payload.get("stale")))


def parse_betterstack(spec: SourceSpec, payload: Any, *, now: int) -> SourceStatus:
    """Better Stack `/index.json`（JSON:API）：逐资源当前态 + 近 90 天可用率。

    事故不在 `index.json` 里（只有 `/feed.json` 的 RSS 有），故 `incidents` 恒空；
    可用率已经把「最近挂过多久」量化了，够用。
    """
    if not isinstance(payload, dict):
        raise ValueError("betterstack payload is not an object")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    attributes = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    level = _level_of(_LIVE_STATUS, attributes.get("aggregate_state"))

    included = _dicts(payload.get("included"))
    sections = {item.get("id"): item.get("attributes", {}).get("name")
                for item in included
                if item.get("type") == "status_page_section"
                and isinstance(item.get("attributes"), dict)}

    components: list[Component] = []
    for item in included:
        if item.get("type") != "status_page_resource":
            continue
        item_attributes = item.get("attributes")
        if not isinstance(item_attributes, dict):
            continue
        name = item_attributes.get("public_name")
        if not isinstance(name, str) or not name.strip():
            continue
        section = sections.get(item_attributes.get("status_page_section_id"))
        detail = _availability_detail(item_attributes.get("availability"), section)
        components.append(Component(
            name=name.strip(),
            level=_level_of(_LIVE_STATUS, item_attributes.get("status")),
            detail=detail,
        ))

    return SourceStatus(spec=spec, level=level, components=tuple(components),
                        fetched_at=now)


def parse_atom(spec: SourceSpec, text: Any, *, now: int) -> SourceStatus:
    """DeepSeek `/history.atom`：**只有事故史，没有当前组件态**。

    源级别 = 最严重的那条未恢复事故；没有未恢复事故就是 `operational`。正文里
    `Status: resolved` 是恢复信号，`identified` / `investigating` / `monitoring`
    都算进行中。XML 结构变化时抛 `ValueError`，由 `fetch_source` 兜成 unknown。
    """
    if not isinstance(text, str):
        raise ValueError("atom payload is not text")
    try:
        root = xml_fromstring(text)
    except XmlParseError as error:
        raise ValueError(f"atom payload is not valid XML: {error}") from error

    incidents: list[Incident] = []
    for entry in _atom_children(root, "entry")[:MAX_INCIDENTS]:
        title = _atom_text(entry, "title")
        if not title:
            continue
        summary = _atom_text(entry, "summary")
        resolved = bool(summary) and "status: resolved" in summary.lower()
        incidents.append(Incident(
            title=title,
            level=_deepseek_title_level(title),
            ongoing=not resolved,
            updated_at=_as_iso_epoch(_atom_text(entry, "updated")),
            url=_atom_link(entry),
            detail=summary,
        ))

    ongoing = [item.level for item in incidents if item.ongoing]
    return SourceStatus(spec=spec, level=aggregate_level(ongoing) if ongoing
                        else LEVEL_OPERATIONAL,
                        incidents=tuple(incidents), fetched_at=now)


def _dicts(raw: Any) -> list[dict[str, Any]]:
    """只保留 dict 元素（上游偶尔混入 null / 字符串）。"""
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _str_or_none(raw: Any) -> str | None:
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _is_ongoing(raw: Any) -> bool:
    """Statuspage 事故 `status`：resolved / postmortem 之外都算进行中。"""
    if not isinstance(raw, str):
        return False
    return raw.strip().lower() not in ("resolved", "postmortem")


def _latest_update_body(raw: Any) -> str | None:
    """Statuspage 事故的最新一条 update 正文（通常比标题信息量大）。"""
    updates = _dicts(raw)
    if not updates:
        return None
    return _text(updates[0].get("body"))


def _availability_detail(raw: Any, section: Any) -> str | None:
    """Better Stack 的可用率 → `可用率 99.85% · Inference - Chat`。"""
    parts: list[str] = []
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        parts.append(f"可用率 {float(raw):.2%}")
    if isinstance(section, str) and section.strip():
        parts.append(section.strip())
    return " · ".join(parts) if parts else None


def _deepseek_title_level(title: str) -> str:
    lowered = title.lower()
    for keyword, level in _DEEPSEEK_TITLE_LEVEL:
        if keyword in lowered:
            return level
    return LEVEL_DEGRADED


_ATOM_NS = "http://www.w3.org/2005/Atom"


def _atom_children(root: XmlElement, name: str) -> list[XmlElement]:
    """按 Atom 命名空间找子元素；无命名空间的裸 `<entry>` 也认（上游换库不崩）。"""
    found = root.findall(f"{{{_ATOM_NS}}}{name}")
    if found:
        return found
    return [child for child in root if child.tag.split("}")[-1] == name]


def _atom_text(entry: XmlElement, name: str) -> str | None:
    children = _atom_children(entry, name)
    if not children:
        return None
    return _text("".join(children[0].itertext())) or None


def _atom_link(entry: XmlElement) -> str | None:
    for child in _atom_children(entry, "link"):
        href = child.get("href")
        if href:
            return href.strip()
    return None


# ------------------------------------------------------------------ 抓取

_PARSERS = {
    "statuspage": parse_statuspage,
    "opencode": parse_opencode,
    "betterstack": parse_betterstack,
    "atom": parse_atom,
}


async def fetch_source(spec: SourceSpec, *, client: httpx.AsyncClient,
                       timeout: float = 10.0) -> SourceStatus:
    """抓一个源并解析；**任何异常都兜成 `unknown` + `error`，绝不上抛**。

    单源失败不能影响其余源（`fetch_all` 并发跑，一个坏端点不该让整页空白），
    也不能让后台任务被判失败——任务失败会触发「任务连续失败」告警，而一个
    上游状态页临时抽风不值得半夜叫人。

    **成功路径也必须有界**（`_read_bounded` + `client.stream`）：原先 `.text` /
    `.json()` 会把整个 body 读进内存，被控或畸形的状态页能直接顶爆进程内存。
    """
    now = int(time.time())
    try:
        async with client.stream("GET", spec.api_url, timeout=timeout,
                                 follow_redirects=True, headers=_HEADERS) as response:
            response.raise_for_status()
            raw = await _read_bounded(response)
        if spec.kind == "atom":
            # 与原先 `response.text` 同口径：优先用 content-type 声明的 charset。
            # 不能读 `response.encoding`——流式读取后 body 未缓存，该属性会抛
            # `ResponseNotRead`。
            charset = response.charset_encoding or "utf-8"
            return parse_atom(spec, raw.decode(charset, "replace"), now=now)
        return _PARSERS[spec.kind](spec, json.loads(raw), now=now)
    except Exception as error:  # noqa: BLE001 - 抓取失败即降级，不能拖垮任务
        return SourceStatus(spec=spec, level=LEVEL_UNKNOWN, fetched_at=now,
                            error=_short_error(error))


#: 状态页响应体上限：这些端点都是「几 KB 的摘要 / 事故史」，1MB 已宽松到不可能
#: 误杀正常响应，同时挡住「回一个 GB 级 body 撑爆内存」的放大攻击。
MAX_SOURCE_BODY = 1024 * 1024


class OversizedBody(ValueError):
    """状态页响应体超过 `MAX_SOURCE_BODY`（继承 ValueError，与解析失败同口径）。

    不设成独立异常族：`fetch_source` 对**所有**异常都降级成 unknown，多一层
    类型只会让「哪些失败算协议违规」多一个没人读的分类。
    """


async def _read_bounded(response: httpx.Response,
                        limit: int = MAX_SOURCE_BODY) -> bytes:
    """流式读响应体，最多 `limit` 字节；超限抛 `OversizedBody`。

    必须配合 `client.stream` + `aiter_bytes`：先 `client.get` 再判长度没有
    意义——那时 body 已经整个在内存里了。
    """
    collected = bytearray()
    async for chunk in response.aiter_bytes():
        collected.extend(chunk)
        if len(collected) > limit:
            raise OversizedBody(f"response body exceeds {limit} byte limit")
    return bytes(collected)


async def fetch_all(specs: Iterable[SourceSpec] = SOURCE_SPECS, *,
                    timeout: float = 10.0,
                    client: httpx.AsyncClient | None = None) -> tuple[SourceStatus, ...]:
    """并发抓取全部源；`client` 为空时自建一个（测试注入 MockTransport 用）。

    返回顺序与 `specs` 一致（`gather` 保序），前端卡片顺序因此稳定。
    """
    ordered = tuple(specs)
    if client is not None:
        return tuple(await asyncio.gather(
            *(fetch_source(spec, client=client, timeout=timeout) for spec in ordered)))
    # `trust_env=False` 与 provider/proxy.build_client 同口径：不吃宿主环境
    # / macOS 系统代理。这台机开了全局代理（scutil --proxy → 127.0.0.1:7897），
    # httpx 默认 `trust_env=True` 会把它当出站代理，代理对状态页域名握手失败时
    # 八个源全部 ConnectError（实测全站显示「抓取失败」）。
    async with httpx.AsyncClient(trust_env=False) as owned:
        return tuple(await asyncio.gather(
            *(fetch_source(spec, client=owned, timeout=timeout) for spec in ordered)))


# ------------------------------------------------------------------ 存储


class UpstreamStatusStore:
    """进程内快照：整批原子替换。

    不落盘：状态数据分钟级易变，重启后拿一份几小时前的「一切正常」比显示
    「尚未抓取」危险得多（与 `TaskStatusStore` 同一理由）。
    """

    def __init__(self) -> None:
        self._snapshot: tuple[SourceStatus, ...] = ()
        self._fetched_at: float | None = None

    def update(self, statuses: Iterable[SourceStatus], *,
               fetched_at: float | None = None) -> None:
        self._snapshot = tuple(statuses)
        self._fetched_at = time.time() if fetched_at is None else fetched_at

    def snapshot(self) -> tuple[SourceStatus, ...]:
        return self._snapshot

    @property
    def fetched_at(self) -> float | None:
        """上次抓取时刻（epoch 秒）；从未抓取为 None。"""
        return self._fetched_at

    def payload(self) -> dict[str, Any]:
        """API 响应里的 `sources` + `summary` 两部分（路由只再拼运行态字段）。"""
        counts: dict[str, int] = {level: 0 for level in (
            LEVEL_OPERATIONAL, LEVEL_DEGRADED, LEVEL_PARTIAL, LEVEL_MAJOR,
            LEVEL_MAINTENANCE, LEVEL_UNKNOWN)}
        for status in self._snapshot:
            counts[status.level] = counts.get(status.level, 0) + 1
        return {
            "summary": {
                "level": aggregate_level(status.level for status in self._snapshot),
                "operational": counts[LEVEL_OPERATIONAL],
                # 「异常」= 除 operational / unknown 之外的一切（含维护）
                "abnormal": len(self._snapshot) - counts[LEVEL_OPERATIONAL]
                            - counts[LEVEL_UNKNOWN],
                "unknown": counts[LEVEL_UNKNOWN],
                "total": len(self._snapshot),
            },
            "fetched_at": self._fetched_at,
            "sources": [status.as_dict() for status in self._snapshot],
        }
