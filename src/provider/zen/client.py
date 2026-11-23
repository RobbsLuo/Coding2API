"""OpenCode Zen 上游客户端：免费层门禁伪装 + 标准 OpenAI SSE。

免费层门禁（实测，2026-09-29 复现，上游会演进故集中在此）：

* `User-Agent` 形如 `opencode/<version>`，且 version ≥ 1.18.0
  （低于阈值 → 426；无版本号 → 403）；
* `x-opencode-session` 匹配 `^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$`，**且前 12 位
  十六进制必须解码回近期时间戳**（官方 `Identifier.create` 的编码，见
  `new_session_id`；随机 hex 在 2026-10-09 上游收紧后一律 403）；
* body `stream` 必须为 `true`（本项目只用流式，非流式由引擎聚合）；
* body `tools` 必须**同时**含 name 为 `bash` 与 `read` 的工具
  （只校验 name，参数留空壳即可）。

2026-11 官方客户端进入 2.x：第三方抓包要求再加 4 个 session 副本头、
`x-opencode-client/project`、`b3`/`traceparent`，并在 system 消息里带
`<env>Working directory: …</env>`。故增加 `ZEN_CLIENT_PROFILE=v2` 档位；
默认保持 1.x（`legacy`），因为 2026-11-19 匿名额度耗尽（429 + Retry-After
≈19h），两档都无法端到端复验。

门禁要求 tools 含 bash/read，但这两个可能是**我们伪造的**、客户端从没声明过
（如 OpenCode 的执行工具叫 `shell`）。模型偏好调 `bash` 这个名字（实测
mimo-v2.6 高频），若直接吞掉调用，客户端收到「空正文 + stop」会提前结束
回合（表现为「自动结束」）。故对注入名两级处理（见 `_InjectedToolFilter`）：

1. 客户端声明了同义工具（`_TOOL_SYNONYMS`，如 `bash`→`shell`）→ 把 tool_call
   的函数名**改写**成同义工具再透传，客户端可正常执行；
2. 没有同义工具 → 才整块丢弃并记 WARNING，finish 收敛为 stop。

过滤/改写只针对「本次由我们注入的名字」——用户自己就带了 bash/read 时
视为真实工具，不注入也不处理。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
import string
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...engine.sse import iter_frames
from ...provider import base, naming
from ...provider.base import (
    ErrKind,
    Event,
    EventKind,
    Model,
    Quota,
)
from ...tasks.pacer import paced
from ..lazy_clients import LazyClients
from . import events as zen_events

logger = logging.getLogger(__name__)

# 上游端点（公开事实，非用户可配置输入；凭证/主机不随凭证 JSON 变化）
ZEN_HOST = "https://opencode.ai"
EP_CHAT = "/zen/v1/chat/completions"
EP_MODELS = "/zen/v1/models"
# 门禁要求的 UA 版本下限；实测 1.18.0 及以上通过，1.17.9 返回 426
MIN_OPENCODE_VERSION = "1.18.0"
# 门禁要求 tools 里必须出现的工具名（大小写敏感）。列表可经
# `ZEN_GATE_TOOLS` 覆盖：第三方实测上游在 10-14 收紧后要求
# `bash/glob/grep/read` 四件套，本仓实测仍以两件套通过 —— 属移动靶，
# 故做成配置而非硬编码（改完重启即换，无需改代码）。
REQUIRED_TOOLS: tuple[str, ...] = ("bash", "read")
# 导入时可接受的 Key 字段名（与 GenericProvider 同一套别名，方便批量脚本复用）。
API_KEY_FIELDS: tuple[str, ...] = ("api_key", "apiKey", "token", "access_token")
# 上游 API 根。`/zen/v1` 与 `/inference/v1` 当前都返回 200（2026-10-28 实测
# `GET /models`），第三方实测称前者已死、后者可用 —— 结论冲突且免费额度耗尽
# 无法端到端复验，故默认保持现状，另经 `ZEN_API_ROOT` 可切，随时回退。
DEFAULT_API_ROOT = "/zen/v1"
# 付费档（Console 充值 + API Key）：官方基址是 `https://opencode.ai/inference`，
# OpenAI Chat Completions 的完整路径为 `/openai/v1/chat/completions`。与免费层
# 的 `/zen/v1` 是两套端点，换认证方式必须连根一起换（见 docs/console/harnesses
# 「Base URLs」表）。模型清单仍在 `/inference/v1`。
PAID_API_ROOT = "/inference/openai/v1"
PAID_MODELS_ROOT = "/inference/v1"
# 客户端指纹档位：
# - `legacy`：1.x 形态（UA `opencode/<version>` + 时间编码 session）；
# - `v2`：2.x 形态（额外 4 个 session 头、client/project 头、b3/traceparent，
#   并在 system 消息注入 `<env>` 块）。
# 2026-11-19 上游免费额度耗尽（429 + Retry-After ~19h），两档都无法端到端
# 复验，故默认保持 `legacy`，通过 `ZEN_CLIENT_PROFILE=v2` 灰度切换。
CLIENT_PROFILES: tuple[str, ...] = ("legacy", "v2")
DEFAULT_CLIENT_PROFILE = "legacy"
# v2 的 UA 默认形态（官方 2.2.2 抓包）；仍可被 ZEN_USER_AGENT 覆盖。
V2_DEFAULT_UA = "opencode/latest/{version}/cli"
# 上游 prompt 检查读的字面 `<env>` 块：一行、含 `<env>` / `</env>` 与
# `Working directory:`。值本身不校验（第三方实测 `/`、`/tmp` 均可）。
ENV_BLOCK = "<env>Working directory: /</env>"
# 注入名 → 客户端常见同义工具名（按优先级）。模型调了我们注入的伪工具时，
# 改写成客户端真实声明的同义工具再透传，避免「调用被吞 → 空 stop → 客户端
# 提前结束回合」。键是门禁名，值只收录各 agent 客户端的真实工具名。
TOOL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "bash": ("shell", "execute"),
    "read": ("read_file",),
}

# 免费模型判定：上游 `/zen/v1/models` 返回**全部**模型（含付费）且不带任何
# 免费/付费标记（`owned_by` 恒为 `opencode`——它是厂商名不是模型名，故展示名
# 另由 id 派生，见 `pretty_model_name`；亦无 cost 字段），换鉴权头 / query
# 也仍是全量；公开模型目录（OpenRouter 等）又与上游实际可用集不同步。
# 唯一权威信号是**匿名可用性**：付费模型恒 401 AuthError（`Missing API key.`），
# 免费模型永不 401。
#
# 故「只显示 free 模型」分两步：
#   1. 后缀收窄候选——上游用 `-free` 命名免费模型（约定，非契约）；
#   2. 逐个探活——真发一次最小请求，只保留 2xx（真能用的）；已下线（400）、
#      区域限制（403）、上游故障（5xx）一并剔除。
# 清单每次现拉现探，无静态白名单：上游增删免费模型自动跟随。
FREE_MODEL_SUFFIX = "-free"
# 探活并发上限：后缀候选实测约 11 个，取 ≥ 候选数让它们单批跑完，避免
# 多批叠加超时；仍显式限定，免得未来后缀泛滥打爆上游。
PROBE_CONCURRENCY = 12
# 单候选探活上限（秒）：探活发生在 /v1/models 的冷路径与启动预热里，而启动
# 预热在应用开始服务 /health 之前，不能无限等首字（实测正常 1–12s，偶发 >100s）。
# 超时按不可用处理：宁可少显示一个慢模型，也不拖垮启动与健康检查。
PROBE_TIMEOUT = 20.0
# 探活判定「该模型由此主机确定不可用」的状态码：付费/需鉴权（401）、已下线
# （400/404）、区域限制（403）。命中即从免费候选里剔除。
# 其余非 2xx（429 匿名免费额度耗尽、5xx、连接超时）按「暂不可判定」保留——
# 上游限流或抖动不该把整条免费渠道清空（实测 2026-11-03 匿名探测全线
# `429 FreeUsageLimitError`，旧口径要求 2xx 会让 Zen 显示「无模型」）。
PROBE_DEAD_STATUSES = frozenset({400, 401, 403, 404})
# 探活用最小请求（真发一次流式对话；只看响应头，不读 body）。
PROBE_PROMPT = "hi"
# 探活结果缓存时长（秒）：探活是整条模型列表链路里最贵的一步——逐个真发一次
# 推理，首字最慢的模型可占十几秒，而总耗时等于最慢那个。免费模型的增删却很慢，
# 所以缓存**判活结论**，比服务层的 MODEL_LIST_TTL_SECONDS（300s）长一档：服务层
# 每 5 分钟到期重拉清单时直接复用上次判活集，只有超过本 TTL 才真的重探。
# 代价：免费模型下线后，最多多留在列表里 30 分钟（选中会 400/401，由引擎按
# 无效请求处理，不会误冷却凭证）。
#
# **只给判活结论用，不给模型清单用**（P2-18）：清单本身必须跟着服务层 TTL 走，
# 否则服务层「到期重拉」拿到的还是 30 分钟前的旧清单，两层周期倒挂。
PROBE_CACHE_TTL_SECONDS = 1800.0
# 清单请求去重窗口（秒）：纯粹挡「同一时刻的并发/紧邻多次调用」，恒小于服务层
# 的 300s，故不会把列表刷新周期拖长。清单 GET 本身很便宜，不值得长缓存。
MODEL_LIST_DEDUPE_SECONDS = 60.0

# 流式无总超时防长流截断；短请求 30s 防悬挂（与 TRAE 同策略）
# 写超时覆盖的是上传请求体阶段，不是首字等待：大上下文请求体可达数 MB，
# 经代理链路 10s 会稳定超时；首字等待由 read=None +
# stream_first_event_timeout_seconds 负责，两者不要混为一谈。


# 403 门禁抖动重试（实测 2026-10-09/12 共 7 次）：上游**偶发**对同一请求回
# FreeTierError 403，数秒后的同请求即 200（门禁条件每次都满足，抖动来自上游）。
# 403 归 ErrKind.REQUEST → 不冷却也不跳过渠道，但 zen 只有一条虚拟凭证，
# 轮换候选随即耗尽 → 对外 503「无可用凭证」（表现为「每次第一次请求都报错」）。
# 故抖动先在客户端内部消化：退避 GATE_403_RETRY_DELAY 后换新 session、关旧连接
# 重发一次；重试仍 403 才上抛，走引擎既有轮换语义（若上游改为持续拒绝，
# 只多一次往返，不会变成重试风暴）。只对 403 重试：其余状态码语义不变。
GATE_403_RETRIES = 1
GATE_403_RETRY_DELAY = 1.0

# session id 的 base62 字母表（12 位 hex + 14 位 base62，共 26 位）
_SESSION_ALPHABET = string.ascii_letters + string.digits

# 会话 id 前 12 位 hex 的**时间编码**（2026-10-09 上游收紧，实测）：
# 门禁不只校验形态（`ses_` + 12 hex + 14 base62），还要求该 12 位 hex 能解码回
# **近期时间**。官方 `Identifier.create` 的做法是把毫秒时间戳左移 12 位（`*0x1000`）
# 再 `+1`，然后**按位取反**，取低 48 位写成 12 位小写十六进制。随机 hex 一律
# 403 `FreeTierError`，与形态是否合规无关（详见 TECHNICAL.md §Zen）。
_SESSION_TIME_SHIFT = 0x1000
_SESSION_TIME_MASK = (1 << 48) - 1


def _normalize_api_root(root: str) -> str:
    """校验 API 根：必须 `/` 开头、不含 query/fragment/空白，去掉尾部斜杠。

    配置错误在启动时就报错，而不是把畸形 URL 发给上游（那只会得到 404，
    看起来像「上游挂了」）。
    """
    value = root.strip().rstrip("/")
    if not value:
        return DEFAULT_API_ROOT
    if (not value.startswith("/") or "?" in value or "#" in value
            or any(ch.isspace() for ch in value)):
        raise ValueError(f"ZEN_API_ROOT 必须是 / 开头的路径，收到 {root!r}")
    return value


def _session_time_prefix(now_ms: int) -> str:
    """毫秒时间戳 → 会话 id 的 12 位十六进制前缀（官方编码的复刻）。"""
    value = (~(now_ms * _SESSION_TIME_SHIFT + 1)) & _SESSION_TIME_MASK
    return format(value, "012x")


def new_session_id(*, now_ms: int | None = None) -> str:
    """生成符合门禁的会话 ID：`ses_` + 12 位时间 hex + 14 位 base62。

    前 12 位是**近期时间戳**的编码（见 `_session_time_prefix`），后 14 位内容
    自由。`now_ms` 仅供测试注入固定时间；生产走当前时钟，故每次调用都新鲜。
    """
    millis = int(time.time() * 1000) if now_ms is None else int(now_ms)
    suffix = "".join(secrets.choice(_SESSION_ALPHABET) for _ in range(14))
    return f"ses_{_session_time_prefix(millis)}{suffix}"


def _trace_headers(trace_id: str) -> dict[str, str]:
    """v2 的 b3 / traceparent：同一条 trace，每个请求新 span。

    `b3` = `{trace}-{span}-1-{parent}`（sampled），`traceparent` 是同一条
    trace/span 的 W3C 拼写。parent 独立随机（上游只校验形态）。
    """
    span = secrets.token_hex(8)
    parent = secrets.token_hex(8)
    return {
        "b3": f"{trace_id}-{span}-1-{parent}",
        "traceparent": f"00-{trace_id}-{span}-01",
    }


def gate_headers(*, version: str = MIN_OPENCODE_VERSION,
                 session_id: str | None = None,
                 user_agent: str = "",
                 profile: str = DEFAULT_CLIENT_PROFILE,
                 trace_id: str | None = None) -> dict[str, str]:
    """免费层门禁头。每次请求新生成 session（门禁只校验格式，不校验归属）。

    `user_agent` 为空 → 按档位取默认形态（`legacy`: `opencode/<version>`；
    `v2`: `opencode/latest/<version>/cli`）。非空时按模板渲染，模板里出现
    `{version}` 就代入版本号（如 `opencode/latest/{version}/cli`），没有占位符
    则原样使用。UA 形态同样是移动靶，交给 `ZEN_USER_AGENT` 配置而不是硬编码。

    `profile="v2"` 额外复刻官方 2.x 客户端的头集合（4 个 session 副本、
    `x-opencode-client/project`、b3/traceparent）。未知名档位直接报错，避免
    配置笔误静默退回 legacy。
    """
    if profile not in CLIENT_PROFILES:
        raise ValueError(f"未知 ZEN 客户端档位 {profile!r}，可选 {CLIENT_PROFILES}")
    agent = user_agent or ("opencode/{version}" if profile == "legacy"
                           else V2_DEFAULT_UA)
    if "{version}" in agent:
        agent = agent.replace("{version}", version)
    session = session_id or new_session_id()
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": agent,
        "x-opencode-session": session,
        # 匿名也通；带公开占位符比完全省略更稳（避免网关把无 Authorization
        # 的请求路由到别的鉴权分支）
        "Authorization": "Bearer public",
    }
    if profile == "v2":
        headers.update({
            "x-opencode-client": "cli",
            "x-opencode-project": "global",
            # 官方 2.x 把同一个 session 重复在四个头里；`x-opencode-session-id`
            # 是实测不可省的那一份（10-14 第三方复验），其余按原样带上。
            "x-opencode-session-id": session,
            "x-session-affinity": session,
            "x-session-id": session,
            **_trace_headers(trace_id or secrets.token_hex(16)),
        })
    return headers


# 展示名从上游 id 派生（上游 `/zen/v1/models` 只有 `id`/`owned_by`，后者恒为
# `opencode`、不是模型名，直接透传会让 Zen 所有模型在列表里都叫「opencode」）。
# 归一与清洗规则与其余五条渠道共用 `provider.naming`，zen 只保留一个薄封装：
# 上游 free 档用 `-free` 命名，免费后缀只是可用性命名约定（同一模型的付费档
# 没有该后缀），不是模型名的一部分，故展示时去掉。id 去掉后缀后为空时返回
# 空串，由前端回退展示 id。
def pretty_model_name(model_id: str,
                      free_suffix: str = FREE_MODEL_SUFFIX) -> str:
    """上游 id → 人类可读名（`longcat-2.5-preview-free` → `LongCat 2.5 Preview`）。

    `free_suffix` 可换（如 `_free`）以对齐自定义后缀：此时把后缀先剥掉再走
    通用清洗，否则通用规则只认 `-free` / `_free` 之外还会残留分隔符。
    """
    stem = model_id
    suffix = free_suffix.lower()
    if suffix and stem.lower().endswith(suffix):
        stem = stem[: -len(suffix)]
    return naming.display_model_name(stem)


def _empty_tool(name: str,
                proto: dict[str, Any] | None = None) -> dict[str, Any]:
    """门禁工具：只要求 name 存在；有同义原型时复制其描述与参数 schema。

    复制 schema 的意义：模型看到的伪 `bash` 与客户端真实的 `shell` 完全一致，
    生成的 arguments 直接匹配改写目标的参数名（否则空壳会让模型自由发挥，
    改写后参数对不上客户端工具）。
    """
    function: dict[str, Any] = {"name": name}
    if isinstance(proto, dict):
        description = proto.get("description")
        if isinstance(description, str):
            function["description"] = description
        parameters = proto.get("parameters")
        if isinstance(parameters, dict):
            function["parameters"] = parameters
    function.setdefault("parameters", {"type": "object"})
    return {"type": "function", "function": function}


def _tool_names(tools: list[Any]) -> set[str]:
    """收集已声明工具的名字（只认 `function.name` 字符串）。"""
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


def _find_synonym(tools: list[Any], names: tuple[str, ...]) -> dict[str, Any] | None:
    """按优先级找第一个命中的同义工具的 `function` 定义（无则 None）。"""
    wanted = set(names)
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name") in wanted:
            return function
    return None


def ensure_gate_tools(body: dict[str, Any],
                      required: tuple[str, ...] = REQUIRED_TOOLS) -> frozenset[str]:
    """确保 body.tools 同时含 `bash`/`read`，返回**本次注入**的名字集合。

    只补缺、不覆盖：用户自带同名工具时视为真实工具，不注入也不过滤。
    注入时若客户端声明了同义工具（`TOOL_SYNONYMS`），伪工具复制同义工具的
    description/parameters（见 `_empty_tool`），供 `gate_aliases` 改写透传。
    """
    existing = body.get("tools")
    tools = list(existing) if isinstance(existing, list) else []
    present = _tool_names(tools)
    injected: set[str] = set()
    for name in required:
        if name not in present:
            proto = _find_synonym(tools, TOOL_SYNONYMS.get(name, ()))
            tools.append(_empty_tool(name, proto))
            injected.add(name)
    body["tools"] = tools
    return frozenset(injected)


def gate_aliases(tools: list[Any] | None,
                 injected: frozenset[str]) -> dict[str, str]:
    """注入名 → 客户端真实同义工具名（无同义则不进映射，按丢弃处理）。

    只对**本次注入**的名字建映射：用户自带的 bash/read 是真工具，不改写。
    """
    present = _tool_names(list(tools) if isinstance(tools, list) else [])
    aliases: dict[str, str] = {}
    for name in sorted(injected):
        for synonym in TOOL_SYNONYMS.get(name, ()):
            if synonym in present:
                aliases[name] = synonym
                break
    return aliases


def has_env_block(content: str) -> bool:
    """`content` 是否已含门禁读取的 `<env>` 块。

    只认第一对 `<env>...</env>`，且对内必须出现 `Working directory:`；
    空对（`<env></env>`）在前、有效对在后是上游明确拒绝的形态。
    """
    start = content.find("<env>")
    if start < 0:
        return False
    rest = content[start + len("<env>"):]
    end = rest.find("</env>")
    if end < 0:
        return False
    return "Working directory:" in rest[:end]


def _system_text(message: dict[str, Any]) -> str | None:
    """system 消息的字符串正文；数组/其它形态返回 None（与官方 2.x 判定一致）。"""
    content = message.get("content")
    return content if isinstance(content, str) else None


def ensure_env_block(body: dict[str, Any]) -> bool:
    """确保第一条 system 消息带 `<env>` 块；返回本次是否发生注入。

    复刻官方 2.x 客户端的形态（也是上游 prompt 检查读取的位置）：
    * 已有 system 消息含有效块 → 不动；
    * 没有 system 消息 → 最前面插入一条；
    * 有 system 消息 → 追加到正文末尾；正文已出现 `<env`（但没有有效块，
      如空对）→ 把块放到**最前面**，避免空对成为第一匹配；
    * 数组正文 → 作为额外 text part 追加/前置，不替换客户端内容。
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    system_indexes = [
        index for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "system"
    ]
    for index in system_indexes:
        text = _system_text(messages[index])
        if text is not None and has_env_block(text):
            return False
    if not system_indexes:
        messages.insert(0, {"role": "system", "content": ENV_BLOCK})
        return True
    message = messages[system_indexes[0]]
    content = message.get("content")
    if isinstance(content, str):
        if "<env" in content:
            message["content"] = f"{ENV_BLOCK}\n\n{content}"
        elif not content:
            message["content"] = ENV_BLOCK
        else:
            message["content"] = f"{content}\n\n{ENV_BLOCK}"
    elif isinstance(content, list):
        holds_tag = any(
            isinstance(part, dict) and isinstance(part.get("text"), str)
            and "<env>" in part["text"]
            for part in content
        )
        part = {"type": "text", "text": ENV_BLOCK}
        if holds_tag:
            content.insert(0, part)
        else:
            content.append(part)
    else:
        message["content"] = ENV_BLOCK
    return True


def prepare_body(payload: dict[str, Any], model: str,
                 required: tuple[str, ...] = REQUIRED_TOOLS,
                 profile: str = DEFAULT_CLIENT_PROFILE,
                 *,
                 gate: bool = True,
                 ) -> tuple[dict[str, Any], frozenset[str]]:
    """上游请求体：深拷 messages、强制 stream、注入门禁工具。

    返回 `(body, injected_names)`。deepcopy 的必要性与 TRAE 相同：下游可能
    原地改写 messages，不能穿透回引擎持有的 `request.raw`（会话粘性指纹
    与续写都读它）。`stream` 无条件置 true——上游非流式会被门禁 403。
    `profile="v2"` 时再注入官方 2.x 的 `<env>` 块。

    `gate=False` 用于**付费档**（Console API Key）：门禁工具与 `<env>` 块是
    免费层才校验的伪装物，带 Key 的请求不需要；`stream` 仍强制（引擎按流式
    解析）。此时 `injected` 为空集，调用方的 `_InjectedToolFilter` 不会误伤
    用户真实声明的 bash/read。
    """
    body = dict(payload)
    body["messages"] = copy.deepcopy(payload.get("messages"))
    base.normalize_developer_role(body["messages"])
    body["model"] = model
    body["stream"] = True
    if not gate:
        return body, frozenset()
    if profile == "v2":
        ensure_env_block(body)
    injected = ensure_gate_tools(body, required)
    return body, injected


class _InjectedToolFilter:
    """处理模型对「我们伪造的 bash/read」的调用：有同义工具则**改写透传**，
    没有才丢弃。

    OpenAI 流式 tool_call 分片：首个分片带 `id`/`function.name`，后续分片
    只有 `index` 与 `function.arguments`。因此按 `index` 记录已丢弃的调用，
    后续同名 index 的分片一并丢弃，避免残留半截 arguments；改写的调用不标
    丢弃，后续 arguments 分片自然保留（name 只在首片）。
    """

    def __init__(self, injected: frozenset[str],
                 aliases: dict[str, str] | None = None) -> None:
        self._injected = injected
        # 注入名 → 客户端真实同义工具名（`gate_aliases` 产出；无则全丢）
        self._aliases = dict(aliases) if aliases else {}
        self._dropped_indexes: set[int] = set()

    def keep(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for call in tool_calls:
            index = call.get("index")
            index = index if isinstance(index, int) and not isinstance(index, bool) else None
            if index is not None and index in self._dropped_indexes:
                continue
            function = call.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and name in self._injected:
                target = self._aliases.get(name)
                if target is None:
                    # 无同义工具可映射：只能丢弃，否则客户端收到自己没定义的
                    # 函数调用。WARNING 让「空 stop 提前结束」可诊断。
                    logger.warning("门禁伪工具 %r 无客户端同义工具，调用已丢弃",
                                   name)
                    if index is not None:
                        self._dropped_indexes.add(index)
                    continue
                # 改写成客户端真实工具名，id/arguments/index 原样保留
                call = dict(call)
                call["function"] = {**function, "name": target}
            kept.append(call)
        return kept


class UpstreamHTTPError(base.UpstreamHTTPError):
    """Zen 上游非 2xx；kind() 走 Zen 的状态码规则。"""

    classify_status = staticmethod(zen_events.classify_status)


class ZenClient(LazyClients):
    """上游 HTTP 客户端。host / version 可覆盖，便于测试与灰度。"""

    def __init__(
        self,
        *,
        host: str = ZEN_HOST,
        version: str = MIN_OPENCODE_VERSION,
        api_root: str = DEFAULT_API_ROOT,
        models_root: str = "",
        profile: str = DEFAULT_CLIENT_PROFILE,
        gate_tools: tuple[str, ...] = REQUIRED_TOOLS,
        user_agent: str = "",
        free_suffix: str = FREE_MODEL_SUFFIX,
        models_cache_ttl: float = MODEL_LIST_DEDUPE_SECONDS,
        probe_cache_ttl: float = PROBE_CACHE_TTL_SECONDS,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
        proxy: str | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.version = version
        if profile not in CLIENT_PROFILES:
            raise ValueError(
                f"未知 ZEN 客户端档位 {profile!r}，可选 {CLIENT_PROFILES}")
        self.profile = profile
        self.api_root = _normalize_api_root(api_root)
        # 模型清单根与对话根解耦：`/inference/openai/v1` 只服务对话，
        # 清单在 `/inference/v1`（官方 2.x 端点拆分）。空 = 跟随 api_root。
        self.models_root = (_normalize_api_root(models_root)
                            if models_root.strip() else self.api_root)
        self.gate_tools = tuple(gate_tools) or REQUIRED_TOOLS
        self.user_agent = user_agent
        self.free_suffix = free_suffix
        # **两层缓存，两个周期**（P2-18）：
        # - 清单：短 TTL，只挡并发 / 紧邻调用，服务层 300s 到期即拿到新清单。
        # - 判活：长 TTL（服务层 TTL 长一档），最贵的一步不必跟着列表重跑。
        self.models_cache_ttl = models_cache_ttl
        self.probe_cache_ttl = probe_cache_ttl
        # v2 的 trace 每进程一条、span 每请求一条（官方抓包行为）
        self._trace_id = secrets.token_hex(16)
        # 上次**判活**的 (monotonic 时间, 探过的 id 集合, 其中判活的 id 集合)：
        # TTL 内只探新增模型。与清单分开——清单必须跟着服务层刷新，判活结论可以
        # 更长寿（探活是整条链路最贵的一步）。
        self._probe_cache: tuple[float, frozenset[str], frozenset[str]] | None = None
        # 上次拉到的免费档清单（单调时钟, 模型表）：只做短窗口去重。
        self._models_cache: tuple[float, list[Model]] | None = None
        # 付费档清单单独缓存：带 Key 时上游返回全部付费模型、且不需要探活，
        # 与免费档的缓存语义不同，混用会让「匿名 8 条 / 付费 70 条」互相覆盖。
        self._paid_models_cache: tuple[float, list[Model]] | None = None
        self._stream_client = stream_client
        self._short_client = short_client
        self._proxy = proxy

    def _chat_url(self, api_key: str = "") -> str:
        """对话端点：带 Key 走付费根，否则走配置的免费根。"""
        root = PAID_API_ROOT if api_key else self.api_root
        return f"{self.host}{root}/chat/completions"

    def _models_url(self, api_key: str = "") -> str:
        root = PAID_MODELS_ROOT if api_key else self.models_root
        return f"{self.host}{root}/models"

    def _gate_headers(self, api_key: str = "") -> dict[str, str]:
        """请求头：免费层门禁头 + 认证。

        带 Key 时把 `Authorization` 换成 `Bearer <key>`（官方付费档的鉴权），
        其余门禁头保留——它们对上游是多余的，但改掉会让「同一条渠道两种档位」
        出现两套行为，排障时难以对照。
        """
        headers = gate_headers(version=self.version, user_agent=self.user_agent,
                               profile=self.profile, trace_id=self._trace_id)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def stream_chat(self, payload: dict[str, Any], model: str,
                          api_key: str = "") -> AsyncIterator[Event]:
        """POST chat/completions 并逐事件产出中立 Event。非 2xx 抛 UpstreamHTTPError。

        `api_key` 非空 = 付费档：不带门禁工具/`<env>` 伪装，走 `/inference/openai/v1`
        并以 `Bearer <key>` 鉴权；空 = 免费匿名档，行为与历史版本一致。
        """
        paid = bool(api_key)
        body, injected = prepare_body(payload, model, self.gate_tools,
                                      profile=self.profile, gate=not paid)
        tool_filter = _InjectedToolFilter(
            injected, gate_aliases(body.get("tools"), injected))
        # 有实际（保留或改写）tool_call 时才允许 finish_reason=tool_calls；
        # 全是无法映射的伪工具调用时收敛为 stop，避免客户端收到空的收尾。
        kept_tool_calls = False
        attempt = 0
        while True:
            async with self._stream.stream(
                "POST", self._chat_url(api_key), json=body,
                headers=self._gate_headers(api_key),
            ) as response:
                if (response.status_code == 403 and not paid
                        and attempt < GATE_403_RETRIES):
                    # 门禁抖动重试：正文**不读**直接退避——httpx 退出 context 时
                    # 关掉该连接，重发走新连接 + 新 session id（见 GATE_403_RETRIES）。
                    # 付费档的 403 是鉴权/区域判定，等待重试只会重复同一个拒绝。
                    attempt += 1
                    logger.warning(
                        "zen 上游 403（FreeTierError 抖动），%.1fs 后重试（第 %d 次）",
                        GATE_403_RETRY_DELAY, attempt)
                    await asyncio.sleep(GATE_403_RETRY_DELAY)
                    continue
                if response.status_code >= 400:
                    raw = await base.read_body_bounded(response)
                    raise UpstreamHTTPError(response.status_code, raw,
                                            **base.retry_hints(response.headers))
                async for frame in iter_frames(response.aiter_bytes()):
                    for event in zen_events.parse_all_events(frame):
                        if event.kind is EventKind.TOOL_CALLS and event.tool_calls:
                            kept = tool_filter.keep(event.tool_calls)
                            if not kept:
                                continue
                            kept_tool_calls = True
                            event = Event(kind=EventKind.TOOL_CALLS, tool_calls=kept)
                        elif (event.kind is EventKind.FINISH
                              and event.finish_reason == "tool_calls" and not kept_tool_calls):
                            event = Event(kind=EventKind.FINISH, finish_reason="stop")
                        yield event
                return

    async def fetch_models(self, api_key: str = "") -> list[Model]:
        """GET 模型清单，只返回**免费候选**（探活确认不可用的才剔除）。

        `api_key` 非空 = 付费档，改走 `_fetch_paid_models`（全部模型、不探活）。
        空 = 免费匿名档，口径如下：

        上游清单含全部（含付费）模型且无任何免费标记，直接透传会把 70 多个
        选不得的付费模型塞进 Playground（选中即 401）。此处按后缀收窄候选，
        再逐个探活，只有**确定不可用**（付费 401 / 下线 400 / 区域限制 403）的
        才从结果里剔除；`429`（匿名免费额度耗尽）/ 5xx / 连接超时一律保留——
        上游限流或抖动不该把整条免费渠道清空（实测 2026-11-03 匿名探测全线
        `429 FreeUsageLimitError`，旧口径要求 2xx 会让 Zen 显示「无模型」）。
        与 CodeBuddy 国际「429 / 5xx / 超时保留」同一取向。

        `fetch_models` 的结果同时也是引擎登记模型归属的来源，过滤后扁平名
        请求不会再被路由到 zen 的付费模型上（否则 401 会误伤虚拟凭证）。

        判活集按 `probe_cache_ttl`（默认 1800s，比服务层 TTL 长一档）缓存：
        服务层每 300s 到期重拉**清单**时直接复用上次判活结论，避免每 5 分钟
        就把十几个免费候选重探一遍——探活是这条链路里最贵的一步（P2-18）。
        **清单本身不跟着判活缓存**：清单另有 `models_cache_ttl`（60s）的短窗口
        去重，恒短于服务层 TTL，故服务层到期重拉能拿到新清单。
        """
        if api_key:
            return await self._fetch_paid_models(api_key)
        if self._models_cache is not None:
            cached_at, cached_models = self._models_cache
            if time.monotonic() - cached_at < self.models_cache_ttl:
                return list(cached_models)
        response = await self._short.get(
            self._models_url(), headers=self._gate_headers())
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            data = response.json()
        except ValueError as error:
            raise zen_events.UpstreamProtocolViolation("non-JSON models response") from error
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise zen_events.UpstreamProtocolViolation("models response missing data list")
        candidates: list[Model] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if (isinstance(model_id, str) and model_id
                    and model_id.lower().endswith(self.free_suffix)):
                # 上游 `owned_by` 恒为 `opencode`（不是模型名），展示名一律从
                # id 派生，否则 Zen 所有模型在列表里都叫「opencode」。
                # 免费层不消耗额度：显式给 0（而非 None），让列表 UI 显示 x0，
                # 排序时也天然排在最省的一档。
                candidates.append(Model(id=model_id,
                                        name=pretty_model_name(model_id,
                                                                self.free_suffix),
                                        credit_rate=0.0))
        if not candidates:
            raise zen_events.UpstreamProtocolViolation("models api returned no free candidates")
        available = await self._probe_cached([model.id for model in candidates])
        models = [model for model in candidates if model.id in available]
        if not models:
            # 候选全被**确定**判死（付费 401 / 下线 400 / 区域 403）：多半是上游
            # 真实下架或本机网络受限。抛出让上层用上次成功的缓存兜底。
            raise zen_events.UpstreamProtocolViolation("no free model passed liveness probe")
        self._models_cache = (time.monotonic(), list(models))
        return models

    async def _probe_cached(self, model_ids: list[str]) -> set[str]:
        """判活结论按 `probe_cache_ttl` 缓存；只探**没探过**的 id。

        缓存记两套 id：「这一轮探过谁」（`probed`）与「其中谁活着」（`alive`）。
        TTL 内再来一轮时：

        * 候选全在 `probed` 里 → 直接返回 `alive ∩ 候选`，一个请求都不发
          （稳态：免费模型增删很慢，服务层每 300s 重拉清单也不该重探）。
        * 出现没探过的 id（上游新上架的免费模型）→ **只探新增的那几个**，
          并合并进缓存。既不会把新模型当成死号藏 30 分钟，也不会因为多一条
          就让十几个老候选全部重探一遍（那是旧实现每 300s 白花的代价）。
        """
        now = time.monotonic()
        cached = self._probe_cache
        if cached is not None:
            probed_at, probed, alive = cached
            if now - probed_at < self.probe_cache_ttl:
                fresh = [model_id for model_id in model_ids if model_id not in probed]
                if not fresh:
                    return set(model_ids) & set(alive)
                newly_alive = await self._probe_available(fresh)
                self._probe_cache = (
                    probed_at, probed | frozenset(fresh), alive | frozenset(newly_alive))
                return set(model_ids) & (set(alive) | set(newly_alive))
        alive = await self._probe_available(model_ids)
        self._probe_cache = (now, frozenset(model_ids), frozenset(alive))
        return alive

    async def _fetch_paid_models(self, api_key: str) -> list[Model]:
        """带 Key 的模型清单：全部模型、不按 `-free` 收窄、不逐个探活。

        免费档按后缀收窄，是为了把「选不得的付费模型」挡在池外；带 Key 时
        这些付费模型正是用户买到的，收窄会让清单只剩免费几条。带 Key 也不
        探活：上游按 Key 的账户返回可用集合，清单即权限，逐个真发一次对话
        只是白花十几个请求。缓存与免费档分开（`_paid_models_cache`），TTL 同源。
        """
        if self._paid_models_cache is not None:
            cached_at, cached = self._paid_models_cache
            if time.monotonic() - cached_at < self.models_cache_ttl:
                return list(cached)
        response = await self._short.get(
            self._models_url(api_key), headers=self._gate_headers(api_key))
        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code,
                                    await base.read_body_bounded(response))
        try:
            data = response.json()
        except ValueError as error:
            raise zen_events.UpstreamProtocolViolation("non-JSON models response") from error
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise zen_events.UpstreamProtocolViolation("models response missing data list")
        models: list[Model] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not (isinstance(model_id, str) and model_id):
                continue
            # 免费档模型带 Key 也用不了：上游对 `-free` 与零价模型仍回 429
            # `FreeUsageLimitError`（官方口径：免费层只对 OpenCode 客户端开放）。
            # 而这条 429 是按**账号**记的，留在清单里被选中一次就会把刚买的付费
            # 凭证一起冷却 16h（实测 2026-11-21）。付费档宁可清单短，也不留必撞
            # 的模型；账号一个付费模型都没开通时返回空表，让 zen 彻底不参与路由。
            if model_id.lower().endswith(self.free_suffix):
                continue
            if input_price(item) == 0:
                continue
            # 上游付费清单带 `name`（免费清单也带），有就用；没有则从 id 派生，
            # 保持与免费档同一套展示规则。
            name = item.get("name")
            models.append(Model(
                id=model_id,
                name=name.strip() if isinstance(name, str) and name.strip()
                else pretty_model_name(model_id)))
        if not models:
            logger.info("zen 付费档无可用模型（账号未开通付费模型访问或余额为 0）")
        self._paid_models_cache = (time.monotonic(), list(models))
        return models

    async def _probe_available(self, model_ids: list[str]) -> set[str]:
        """并发探活：剔除**确定不可用**的模型，保留可用与暂不可判定的。

        `_probe_one` 判 `False`（付费 401 / 下线 400 / 区域 403）才剔除；
        `True`（2xx）与 `None`（429 / 5xx / 连接超时——暂不可判定）都保留。
        用于模型列表展示，不触达业务统计。
        """
        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def one(model_id: str) -> str | None:
            async with semaphore:
                if await self._probe_one(model_id) is False:
                    return None
                return model_id

        results = await asyncio.gather(*(one(model_id) for model_id in model_ids))
        return {model_id for model_id in results if model_id is not None}

    async def _probe_one(self, model_id: str) -> bool | None:
        """单模型探活：`True` 可用 / `False` 确定不可用 / `None` 暂不可判定。

        只看状态行、**不读 body**：进 context 即拿到响应头，随即关闭连接，
        不会等整段生成跑完（付费 401 / 下线 400 / 区域 403 都在响应头就定性）。
        整体套 `asyncio.timeout`：首字过慢按 `None`（暂不可判定，保留模型），
        不拖垮启动与 /health。
        """
        body, _ = prepare_body(
            {"messages": [{"role": "user", "content": PROBE_PROMPT}]}, model_id,
            self.gate_tools, profile=self.profile)
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                async with self._short.stream(
                    "POST", self._chat_url(), json=body,
                    headers=self._gate_headers(),
                ) as response:
                    status = response.status_code
                    if status < 300:
                        return True
                    if status in PROBE_DEAD_STATUSES:
                        return False
                    # 429 = 匿名免费额度耗尽。它是**已知的稳态**（同一批免费模型
                    # 会持续回 429，每轮 8 条），打 WARNING 只会把真问题挤出视野
                    # ——实测 2026-11-08 一天 224 条，占全部 WARNING 的 73%。
                    # 降级为 DEBUG：还保留「哪个模型被限流」的排障线索。
                    if status == 429:
                        logger.debug("zen 探活 %s 暂不可判定：HTTP 429", model_id)
                        return None
                    logger.warning("zen 探活 %s 暂不可判定：HTTP %s", model_id, status)
                    return None
        except (httpx.HTTPError, TimeoutError):
            logger.warning("zen 探活 %s 暂不可判定：连接失败/超时", model_id)
            return None


def input_price(item: dict[str, Any]) -> float | None:
    """清单条目的输入单价；字段缺失/畸形返回 None（无法判定，按可用保留）。

    只用来识别**零价模型**——它们与 `-free` 一样属于官方客户端专属免费层，
    带 Key 请求会被 429。单位无所谓（只判 0）。
    """
    cost = item.get("cost")
    text = cost.get("text") if isinstance(cost, dict) else None
    entry = text.get("input") if isinstance(text, dict) else None
    price = entry.get("price") if isinstance(entry, dict) else entry
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    return float(price)


def _api_key(credential_data: dict) -> str:
    """凭证里的 Key：非字符串 / 空白一律当匿名档，避免把坏数据发成 `Bearer None`。"""
    value = credential_data.get("api_key")
    return value.strip() if isinstance(value, str) else ""


@dataclass(slots=True)
class ZenProvider:
    """Provider 协议实现（细接口）。

    两种档位由凭证内容决定：
    * 空凭证 `{}` = 免费匿名层（门禁伪装、`/zen/v1`）；
    * `{"api_key": "sk-…"}` = Console 付费档（`/inference/openai/v1` +
      `Bearer <key>`，模型清单含付费模型）。

    两个档位都没有额度接口、签到/成长：只实现对话与模型列表三条能力，
    其余任务靠「方法缺失」自然跳过（CheckinTask / GrowthTask / RefreshTask
    都用 getattr 探测，不需要为此改任务代码）。
    """

    client: ZenClient = field(default_factory=ZenClient)
    pacer: Any | None = None

    id: str = "zen"

    def import_credential(self, raw: dict) -> dict:
        """导入：`{"api_key": "sk-…"}` / `{"apiKey": …}` / 裸字符串 → 落 Key。

        其余入参一律落空对象（免费层虚拟凭证只是调度占位）。只认非空字符串：
        `{"api_key": ""}` 不应把匿名档悄悄变成「有 Key 但空」的付费档。
        """
        if isinstance(raw, str):
            raw = {"api_key": raw}
        if not isinstance(raw, dict):
            return {}
        for key in API_KEY_FIELDS:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return {"api_key": value.strip()}
        return {}

    def classify(self, status: int, body: bytes) -> ErrKind:
        return zen_events.classify_status(status, body)

    async def probe_quota(self, credential_data: dict) -> Quota:
        """无额度接口 → 未知（probe_failed），绝不返回 total=None 被判「已耗尽」。

        `health_score` 对 `probe_failed=True` 返回 None（unknown）：调度器按
        中性处理，不会把这条免费渠道错误地降级到「已耗尽」。
        """
        return Quota(probe_failed=True, probed_at=int(time.time()))

    async def list_models(self, credential_data: dict) -> list[Model]:
        return await self.client.fetch_models(_api_key(credential_data))

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        api_key = _api_key(credential_data)
        async with paced(self.pacer, "zen"):
            async for event in self.client.stream_chat(payload, model, api_key):
                yield event

    async def aclose(self) -> None:
        await self.client.aclose()
