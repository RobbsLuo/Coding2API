# Coding2API 技术方案

> 结论：单个 FastAPI 进程内三平面（请求 / 管理 / 后台）共享一份 SQLite，十个协议私有渠道统一为中立事件层，由调度器按健康度、到期额度、冷却与在途选号。

PROPOSAL.md 定方向，本文定实现，只写**当前代码现状**。使用与部署见 [README.md](README.md)。

## 1. 技术栈

Python 3.12（`requires-python >=3.12`；Dockerfile 3.15-slim）+ FastAPI / Uvicorn + pydantic-settings（`Settings` frozen）+ 标准库 sqlite3（WAL，手写 SQL）+ cryptography Fernet；HTTP 用 httpx 双客户端（流式 / 短请求，`httpx[socks]` 提供按渠道 SOCKS5，共享 `trust_env=False`）；包管理 uv；测试 pytest + pytest-cov + respx；前端 React 19 + Tailwind 4 + shadcn/ui + Vite。小版本随依赖漂移，以 `pyproject.toml` / `uv.lock` / `web/package.json` 为准。

## 2. 模块结构

| 路径 | 职责 |
|---|---|
| `src/main.py` `config.py` `runtime_settings.py` `version.py` | 组装 / lifespan / 路由；全部 env + `live()`；运行时配置覆盖层（DB > env + 白名单 + snapshot）；版本号（`pyproject.toml` 真源） |
| `src/webapp/` | 请求体上限 ASGI（登录 8KB / 备份恢复 512MB / 其余 16MB）、Host 白名单 + CSP、请求 ID、logfmt 访问日志、日志环 + `redact_or_drop`、异常 → 稳定错误码、SPA catch-all（排除 `/api`、`/v1`） |
| `src/db/` `src/auth/` | DDL / 连接 / 迁移 / SQL / 限额 / 用量聚合 / Fernet / 备份；用户与引导 / 会话 HMAC / Key 摘要 / CSRF / 登录限流 / 三角色 / 白名单 |
| `src/provider/` `src/provider/<渠道>/` | Provider 协议与中立类型、代理与出口粘性、模型名归一、OAuth pending、懒建连接池；各上游客户端 + SSE → Event + 凭证解析 + 签到 / 额度 / OAuth / 签名 |
| `src/engine/` | 选号 + 冷却状态机 + pin、执行 / 收窄 / 轮换 / fallback、限额、在途租约、半开探测、会话粘性、续写、上下文压缩、粘性裁剪、tool 输出压缩、档位映射、前缀诊断、SSE 解析 |
| `src/compat/` `src/api/` | Chat 校验 / 聚合 / DSML；Responses / Anthropic 出口；Services 容器、鉴权依赖、四出口、模型目录 / 合并 / 别名、凭证 / Key / 用户 / 配置 / 审计 / 告警 / 备份 / 账单 / 日志 / 统计 / 指标 |
| `src/tasks/` `src/stats/` | 节流器、额度探测、签到、成长、活跃上报、预刷新、清理、备份、告警、上游状态、模型探活；回填、明细 + 小时双写、实时广播、查询、CSV |
| `src/security/private_files.py` `web/` | 私有目录 / 文件权限收口；React 管理台（`.npmrc` 的 `auto-install-peers=false` 与 lock 强耦合，`Dockerfile` 必须 `COPY web/.npmrc`；`shadcn` CLI 不是依赖） |

## 3. 核心类型

### 3.1 中立事件层（`provider/base.py`）

```python
class EventKind(StrEnum):
    CONTENT = "content"; REASONING = "reasoning"; TOOL_CALLS = "tool_calls"
    USAGE = "usage"; FINISH = "finish"; ERROR = "error"

@dataclass(slots=True)
class Event:
    kind: EventKind
    content: str | None = None; tool_calls: list[dict] | None = None
    usage: Usage | None = None; finish_reason: str | None = None
    error_code: int | None = None; error_message: str | None = None
    error_kind: ErrKind | None = None
    retry_after_seconds: float | None = None  # 仅排队信号带
    model: str | None = None                  # 仅用于静默替换观测

@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None; output_tokens: int | None = None
    reasoning_tokens: int | None = None; cached_tokens: int | None = None
    credit: float | None = None; credit_estimated: bool = False  # True 时展示 ≈
```

- 无语义事件（TRAE `metadata` / `timing_cost` / `extra_info` / `progress_notice`）不在 `KNOWN_EVENT_NAMES` → 跳过；已知事件 JSON 非对象 / 类型错 → `UpstreamProtocolViolation`，不退回默认值。**tool_call 续块须保留**：TRAE 首片带 `function_call.name`，续片只有 `arguments`；只丢「无 name **且** arguments 空」噪声（`is_blank_tool_call` 同判据）。

### 3.2 错误分类（定冷却时长与作用域）

| ErrKind | 触发 | 处置 |
|---|---|---|
| `PLAN` | 权益耗尽 `1005` | 冷却 12h |
| `CREDIT` | 余额不足 402 / `14018` | 冷却到次日 04:00（等签到） |
| `SOFT` | 限流 / 404 | 60s，不累计错误数 |
| `DEAD` | session 失效 | 硬禁用 `disabled=1`；额度证据成立时先降级 `CREDIT` |
| `RESTRICTED` | 风控 `11140` | 账号级 2h 起指数冷却，封顶 7 天；当前请求不再换号 |
| `OTHER` | 其他 4xx/5xx | 累计，连续 3 次 → 10m |
| `INVALID` | 请求无效 / 上游无此模型 | 不冷却，跳过该上游；全拒 → 400 |
| `MODEL` | `429+6004` | 只冷却 `(凭证, 模型)`，10m 起翻倍封顶 2h |
| `CONCURRENCY` | 并发 / 频率打满 | 只冷却 `(凭证, 模型)`：首枪 60s，同链翻倍，封顶 5m / 30m |
| `BLOCKED` | `400/404+11102` | `(凭证, 模型)` 负缓存，6h 起翻倍封顶 24h |
| `REQUEST` | `11101/11128/11135` | 零动作：不冷却、不累计，仅换号 |
| `CONTEXT` | `11115 prompt is too long` | 同 REQUEST，统计独立成类 |
| `CAPABILITY` | CodeArts `001001020` | 同 INVALID，统计独立成类 |
| `NETWORK` | 本机网络 / DNS 失败 | 零动作，不惩罚凭证 |
| `QUEUE` | `isQueued` / `RESOURCE_LIMIT` | 等 `retryAfterSeconds` 后原号重开 |

`MODEL_SCOPED_KINDS = {MODEL, BLOCKED, CONCURRENCY}` 只写 `credential_model_cooldowns`；`NO_PENALTY_KINDS = {REQUEST, CONTEXT, NETWORK}`。账号级冷却出现 → 清该凭证模型级条目。

| 上游信号 | CB | TRAE | Kilo | ErrKind |
|---|---|---|---|---|
| 权益耗尽 | `code=1005` | `code:1005` | — | PLAN |
| 余额不足 | 402 / `14018` | 402 / `14018` | — | CREDIT |
| 模型级限流 | 429 + `6004` | 429 + `6004` | 429 / 502/503/504（点名模型） | MODEL |
| 供应商过载 | 429/5xx + `11134` | — | — | QUEUE |
| 并发打满 | — | — | — | CONCURRENCY（CodeArts `TM.00001041`） |
| 无此模型 | 400/404 + `11102` | 400/404 + `11102` | — | BLOCKED |
| 请求级错误 | 400 + 11101/11128/11135 | — | 403 | REQUEST |
| 工具 schema 拒收 | 400/403 + `11129` | — | — | INVALID |
| 上下文超限 | 400 + `11115` | — | — | CONTEXT |
| 限流 / 不存在 | 429 / 404 | 429 / 404 | — | SOFT |
| 会话失效 | 401/403 | 401 | — | DEAD |
| 服务端错误 | 5xx | 5xx | 500 | OTHER |
| 请求自身无效 | 400 | 400（含 4001） | 400/404/422 | INVALID |

- 回退链未出帧就用尽 → `invalid_request` 改判 400 JSON，含 11128 / 11115 / 11135 / 11129 / 4001 语义。
- 业务码只从 `"code": N` 键值形态提取；`429+6004` 先于 429，`402/14018` 先于状态码兜底。`11158`（套餐不允许）与 `11102` 共用 `MODEL_BLOCKED_CODES`。
- **RESTRICTED 拦在 `401/403 → DEAD` 之前**：否则写不可逆 `disabled=1`。`Retry-After` 夹在 `[now + 本次时长, now + 24h]`；窗口内第二个不同凭证也命中 → 渠道级暂停 24h（`cool_provider`，不写 disabled）。
- **QUEUE 不换号**：`pinned` 记住被点名凭证；`QUEUE_MAX_ROUNDS = 3`、单轮封顶 `QUEUE_MAX_WAIT_SECONDS = 60s`；`queueCount == 0` 不等，`null` 是未上报；已出帧不重开。
- **短冷却原地等**：无立即可用候选、最短剩余冷却 ≤ `SHORT_COOLDOWN_WAIT_SECONDS`（默认 60）时等它到期重取一次；本轮 `tried` 中的号也参与，等到的号先 `tried.discard`。
- **TLS 中断 → SOFT**：`executor._classify` 与 `httpx.TransportError` 并列兜住 `ssl.SSLError`，不泛化到 `OSError`。上游 `Retry-After` / `Try again in …` 经 `parse_retry_after` 覆盖常量，钳 `[now + 常量, now + 24h]`。
- **kilo 429 与 502/503/504 → MODEL**；**zen / kilo 401 → INVALID**（无凭证，只表示该模型要付费 key）；**CodeArts `APIG.0602` → SOFT**（判 DEAD 会断自愈路径）。
- **额度耗尽写成凭据失效 → 分类层仍 DEAD，引擎按新鲜额度证据收口为 CREDIT**：`executor._credit_if_exhausted` 在 `health == -1` 或 `quota_remaining <= 0` 时改判；证据陈旧先 `_probe_quota_now` 复探（`DEAD_QUOTA_PROBE_TTL_SECONDS` 内同号共用）。
- **内容风控 11128**：`sanitize_channel_markers` 出站替换 `system` / `assistant` 正文与 `tools[].function.description`（`CODEBUDDY_SANITIZE_CHANNEL_MARKERS=false` 关闭）；`tool_calls` / reasoning 不触发。

### 3.3 三态健康度

```python
@dataclass(slots=True)
class Quota:
    remaining: float | None = None; total: float | None = None
    cycle_end: int | None = None
    expiry_ladder: list[tuple[int, float]] | None = None
    packages: list[dict] | None = None
    probed_at: int | None = None; probe_failed: bool = False

UNKNOWN = None; EXHAUSTED = -1   # 0-100 已知；None 未知；-1 已耗尽
```

- 排序 `known DESC > unknown > exhausted(-1)`；`probe_failed` 与「探测到 0」分开——Zen/Kilo 虚拟凭证恒 `probe_failed=True`。必须三态：探测失败算 0 分会永远轮不到。
- CB 个人版 `remaining` = 逐包 `CycleCapacityRemainPrecise` 之和，**不用 `TotalDosage`**（上周期口径）；企业版 `limitNum - credit`。TRAE 取 `credits_limit - credits_amount`。

### 3.4 截断续写

`finish_reason == "length"` → 同凭证续写，最多 `AUTO_CONTINUE_MAX`（默认 10，0 关闭）。`ContinuationStream` 追加「已产出正文 + 续写指令」重发，输出上限两键归零，跨轮累计 usage，末端补累计 usage + 最后一轮真实 `finish_reason`；事件流包装器而非 executor 重跑。只做 `length`：`仅 reasoning 无正文` / `空正文` / `代码块未闭合` 无证据，不做。

- CB 忽略 `max_completion_tokens`、只认 `max_tokens`；TRAE 两键都不生效。`reasoning_effort` 缺失补 `medium`（不发则 CB 把思考写进 `delta.content`），`setdefault` 不覆盖显式值。

### 3.5 会话粘性键

优先级：① body `conversation_id` > `conversationId` > `prompt_cache_key` > `session_id`（同键名先 `metadata` 再顶层）→ ② 头 `x-session-id` / `x-opencode-session` / `x-session-affinity` / `thread-id` / `session-id` / `conversation-id` / `conversation_id`（须匹配 `[A-Za-z0-9._:-]{1,128}`）→ ③ 消息增量前缀指纹。

- 头经 contextvar 传入，键带 `header:` 前缀避免撞键；带 `user_id` 不派生前缀兜底键；键掺用户名（头值同样掺），防猜头值占用他人账号。TTL `CONVERSATION_STICKY_SECONDS`；条目上限 512，超量先淘汰过期再按最旧。
- **请求级错误不搬粘性**：`REQUEST` / `CONTEXT` 换号成功后不重绑，`NETWORK` 同（可能是该号出口挂了）；其余照旧重绑。

### 3.6 模型元数据与黑名单

- 元数据：`name` / `context_length` / 上下文窗口 / `supported_parameters` / `architecture.input_modalities` / 价格倍率；CB、TRAE 各自映射。
- 黑名单 `MODEL_BLOCKLIST`（fnmatch glob，只影响展示）：内部 / 代理模型、被取代的老模型、已下架免费池与小模型、测试号与日期快照代号；在售主力刻意不加。**渠道内部的可见性 / 身份门槛不走黑名单**（黑名单是全局且可热更的，会误伤别的渠道的同名模型）：TRAE 按上游 `is_invisible_to_user` 与 `display_contact_config.access.data.identity_list` 在 `fetch_models` 里过滤，本账号身份由 `TRAE_ACCOUNT_IDENTITIES` 配置（默认 `0` 免费档，留空 = 不过滤）。
- 静态兜底要**申报来源**：TRAE / CodeBuddy 动态拉取失败时回退内置静态表并照常返回，服务层据此记失败 + `coding2api_model_list_degraded{provider}` + `/api/models/probes` 的 `degraded`，否则「渠道一直没拿到上游真值」只能靠翻日志发现。
- 自定义别名 `MODEL_ALIASES`（热更）：`对外名=模型@渠道;对外名2=模型`；解析宽容，注入期判撞名 / 目标不存在 → 该段 ERROR 跳过；无别名链。

### 3.7 Responses 出口（Codex CLI）

`src/api/responses.py` + `compat/responses/`；与 chat 共用 executor，唯一注入点 `Executor.stream(..., translator=...)`。

| Responses | chat |
|---|---|
| `instructions` / `input` 字符串 / `input[].type=message` | 首条 `system` / `user` / `messages[]`（`developer` → `system`） |
| `input[].content[].type=input_image` | `image_url` part（`detail` 丢弃）；纯文本折字符串 |
| `input[].type=function_call` / `function_call_output` / `reasoning` | assistant `tool_calls` / `tool` 消息 / `reasoning_content`（仅明文） |
| `tools[].type=function` / `namespace` / `web_search/tool_search/custom` | 嵌套 function；默认组直译、非默认命名空间丢弃；托管 / freeform 工具跳过 |
| `max_output_tokens` / `reasoning.effort` / `text.format` | `max_tokens` / `reasoning_effort` / `response_format` |
| `temperature` / `top_p` / `parallel_tool_calls` / `prompt_cache_key` / `text.verbosity` | 同名透传 |

- 流式序列 `response.created` → `output_item.added` → `content_part.added` → `output_text.delta × N` → `output_text.done` → `content_part.done` → `output_item.done` → `response.completed | incomplete`；无 `[DONE]`，终止事件即结束。`length` → `response.incomplete`；流内错误 → `response.failed`（`no_healthy_credential` → `server_is_overloaded`）；首帧即请求级错误 → 400 JSON。
- `include=["reasoning.encrypted_content"]` 接受并忽略，其他 `include` 400；`previous_response_id` / `store=true` 400；回传密文 reasoning 丢弃；私有 item 显式 400。**未经真实 Codex CLI 端到端验证**。

### 3.8 Anthropic Messages 出口（Claude Code）

`POST /v1/messages` + `/v1/messages/count_tokens`，同模式复用 executor。

- `system` → 首条 system；`tool_result` → `tool` 消息（`tool_use_id` → `tool_call_id`）；`image` → `image_url`（`data:{media_type};base64,{data}`）；`document` / `source.type=file` 显式 400。user 连续块按类型拆多条；assistant 块合并一条（`thinking` → `reasoning_content`、`tool_use` → `tool_calls`、`redacted_thinking` 丢弃）；纯文本仍折字符串。
- `tools[].input_schema` → `function.parameters`；`any` → `required`；`stop_sequences` → `stop`；`top_k` 丢弃。**`cache_control` 原样透传**（默认开）：只校验对象 + 非空 `type`，无标记时逐字节不变。
- 出口序列 `message_start` → `content_block_*` → `message_delta` → `message_stop`；thinking 块补占位 `signature_delta`；错误走 `event: error`。**心跳**：Anthropic 用 `event: ping`，chat / Responses 用 `: keepalive`（Responses 协议无 ping）。
- **非流式保活**（`NONSTREAM_KEEPALIVE_SECONDS`，默认 0）：宽限内完成 → 普通 `JSONResponse`；超时才提交 200 + chunked 并先吐 `"\n"`。**首帧延迟发头**（`STREAM_START_GRACE_SECONDS = 5`）：先拉上游第一帧再决定状态码；宽限内无帧照旧 200，不取消在途取帧任务。
- **usage**：命中时 `input_tokens = max(0, prompt_tokens − cached_tokens)`，另记 `cache_read_input_tokens`。**鉴权**先 `x-api-key`，空则 `Authorization: Bearer`；`count_tokens` 本地估算不转发。
- **CC Switch 深链**：`ccswitch://v1/import?resource=provider&…`，`app` = `claude` / `codex` / `opencode`；`endpoint` 随 `apiFormat`（`anthropic` 填根不带 `/v1`，`openai_chat` / `openai_responses` 填 `/v1`）。含 Key 明文 → 只在用户主动操作时生成（仅存内存），需 CC Switch v4.0.10+。

### 3.8b Gemini 出口

`/v1beta/models/{model}:generateContent|:streamGenerateContent|:countTokens` + `GET /v1beta/models`。

- 入站：`contents[].parts`（`text` / `thought:true` → `reasoning_content` / `inlineData` → data URL / `fileData` 原样 URI / `functionCall` → `tool_calls`（id 用 `call_{name}`）/ `functionResponse` → `role:"tool"`）、`systemInstruction`、`generationConfig`（含 `responseMimeType` / `responseSchema`）、`toolConfig` AUTO/ANY/NONE。
- 显式 400：服务端工具（`googleSearch` / `codeExecution` / `retrieval`）、`candidateCount != 1`、未知 `responseMimeType`、畸形 parts；`thinkingConfig` / `topK` 丢弃。
- 出站：`candidates[].content.parts` + `finishReason`（stop→STOP、length→MAX_TOKENS、content_filter→SAFETY）+ `usageMetadata`（`promptTokenCount` / `candidatesTokenCount` / `cachedContentTokenCount` / `thoughtsTokenCount`）+ `modelVersion` / `responseId`；流式每帧一个完整响应，终止帧带 `finishReason`（无 `[DONE]`）；`countTokens` 本地估算。

### 3.9 上下文压缩

动机：上游输入硬限（CB `11115 prompt is too long`），换号无意义。只做确定性「估算 → 裁剪」；`engine/compress.py` + `api/context.py`。

- 估算：中文 0.55 tok/字、数字 0.33、其他 0.25；图片 part 按 URL/base64 长度计入。预算 = `模型上限 × safety_ratio − reserve_for_output`。
- 裁剪：`system` 永久保留；`assistant.tool_calls` 与其后连续 `tool` 结果同生共死；至少留 `min_keep_messages` 条；其外从新到老回填，**只保留连续后缀**；仍超限 → 截断最长非 system 消息（留头尾 + 标记）。未知上限不压缩；`_CHANNEL_WINDOW_FLOOR` 按渠道抬到实测下限（TRAE `1_048_576`），只抬不压；压缩在选号之后执行，每轮从原文重压。
- 热更：`CONTEXT_COMPRESS_ENABLED`（true）/ `CONTEXT_COMPRESS_RESERVE_TOKENS`（4096）/ `CONTEXT_COMPRESS_MIN_KEEP_MESSAGES`（4）/ `CONTEXT_COMPRESS_SAFETY_RATIO`（0.95）/ `CONTEXT_COMPRESS_STICKY_ENABLED`（true）。
- **tool 输出压缩**（默认关）：只压超 `TOOL_OUTPUT_COMPRESS_MAX_CHARS`（8000）的 `role=tool` 纯文本——去 ANSI、折叠重复行、保留头尾 40 行 + 错误行；确定性，不破坏前缀缓存。
- **粘性裁剪**（默认开）：只在超预算时裁，一次裁到 `budget × 0.6`，把保留窗口起点与摘要记进进程内 LRU（渠道 + 模型 + 会话身份）；起点未变就沿用。日志 `reason=no_memory|digest_changed|not_group_start|over_budget`。**前缀诊断**（`PREFIX_DEBUG_ENABLED`，默认关）：只记消息 `sha256` 前 16 位、tools 摘要、`lcp`、`reusable`、`split`；`reusable = min(lcp, 1)`。

### 3.10 API Key 策略

| 列 | 取值 | 空值语义 |
|---|---|---|
| `provider_binding` | `KNOWN_PROVIDERS` | 空 = 跨渠道 |
| `allowed_ips` | 逗号分隔 IP/CIDR | 空 = 不限 |
| `allowed_models` | fnmatch glob | 空 = 不限 |
| `quota` | 六项 `daily_requests` / `daily_tokens` / `daily_cost_usd` / `rpm` / `tpm` / `max_concurrency` | 空 / 0 = 关闭 |
| `expires_at` | epoch 秒 | NULL = 永不过期 |

- 渠道绑定只在绑定渠道选号；模型属他渠道 / `模型@渠道` 冲突 → 400；目录未就绪保守放行。来源 IP 默认不采信 `X-Forwarded-For`，仅 `TRUST_PROXY=true` 取最后一个；白名单解析不出 → 拒绝。
- 模型白名单三个 /v1 出口都校验，空名 / `auto` 按 `default_model` 判。Key 级配额六项独立：当日类按北京自然日聚合，速率 / 并发为进程内滑动窗口；超限 429 + `quota_exceeded`；**判定异常一律放行 + warning**。
- chat 入口 role 白名单 `system` / `user` / `assistant` / `tool` / `developer`，非法 → 400；出站 `normalize_developer_role` 归一。

### 3.11 reasoning 控制

只给机制不给策略，三项默认关 / 留空。`FORCE_DISABLE_THINKING`（false）在 executor 消费事件循环剥离响应侧，不改请求体；`REASONING_EFFORT_MAP`（空）格式 `渠道=from:to,…;*=from:to`，目标 `none` 即删键，只动显式值；`ANTHROPIC_THINKING_EFFORTS`（空）格式 `预算:档位,…,disabled:档位`，留空 = 完全不映射。

- 档位自适应降级：上游拒收 `reasoning_effort` 时在原号降一档重试一次（`xhigh→high→medium→low→minimal`），不换号 / 不冷却 / 不计轮换；已出帧不重试。

### 3.12 通用上游渠道（`generic`，可选）

`{base}/chat/completions` + `{base}/models`，标准 OpenAI 兼容、非逆向，凭证是部署者自己的 key。

- `GENERIC_API_ENDPOINT` 非空才启用；装配期解析一次，同时决定注册与 base_url。分类与 Zen / Kilo 相反：401/403 = key 失效 → `DEAD`；402 → `CREDIT`，400/404/422 → `INVALID`，429 → `SOFT`，5xx → `OTHER`。
- `GENERIC_ALLOWED_ENDPOINTS` 非空只放行名单；留空放行任意 `http(s)`，拒绝非 HTTP scheme。请求体深拷 + 强制 `stream=true`；流式复用 `openai_chunk`；`/models` 失败回落 `GENERIC_MODELS`。无签到 / 成长 / 活跃 / 刷新 / 额度探测；一个部署只配一条。

### 3.13 内置检索工具与 503 拆解

- 检索 `/agenttool/v1/search` 与 `/agenttool/v1/webfetch`（官方 CLI 2.185.0 解包）。搜索 `{query, type:"text2text", max_results:5, allowed_domains?, freshness?}`；抓取 `{url, format:"markdown", prompt, timeout, max_length}`；**不落 `usage_events`**。
- 私有信封 `code == 0` 取 `data`；`11101/11128/11135` → 400、`14018/1005` → 429、`11140` → 403、其余 → 502。选号 `pick_aux` 不做模型级冷却，但账号级冷却 / 禁用 / 限额 / 在途生效。
- 全池耗尽按 `disabled → paused → cooling → model_cooled → limits → inflight → tried → selectable` 归类计数，拼进 503 文案与 WARN 日志；只列非零项。

## 4. Provider 接口

```python
class Provider(Protocol):
    id: str
    def start_auth(self, callback_url: str) -> AuthSession: ...   # flow=poll|callback|paste
    def poll_auth(self, state: str) -> AuthResult | None: ...
    async def complete_callback(self, raw_url: str, state: str) -> dict: ...
    def import_credential(self, raw: dict) -> dict: ...
    def refresh(self, credential_data: dict) -> dict: ...
    async def probe_quota(self, credential_data: dict) -> Quota: ...
    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]: ...
    def classify(self, status: int, body: bytes) -> ErrKind: ...
    def list_models(self, credential_data: dict) -> list[Model]: ...
    async def aclose(self) -> None: ...
```

- 与 `src/provider/base.py` 严格一致；凭证以 dict 传递（解密后的 `data_enc`），provider 不接触 sqlite。
- **可选能力不实现即不定义**：调用方 `getattr` 探测，缺失 → 400「该凭证不支持此操作」。当前可选集：`start_auth`/`poll_auth`、`complete_callback`（仅 TRAE）、`list_accounts`/`switch_account`（仅 CB）、`credential_from`/`checkin_scope`、`checkin`/`checkin_status`、`growth`（仅 CB）、`host`、`model_list_source`（自带静态兜底表的渠道申报「本次是否降级 + 原因」）。
- CodeBuddy / TRAE / Zen / Kilo / Qoder / OfficeAce 共用 `engine/sse.py`；CodeArts 自实现 `iter_data_lines`，只复用 `MAX_SSE_LINE_BYTES` / `SSEFrameTooLarge`。

## 5. 各渠道协议实现

### 5.1 OpenCode Zen（`zen`，虚拟凭证）

标准 OpenAI 流式（`chat.completion.chunk` + `[DONE]`）；末段正文与 `finish_reason` 同帧 → 同帧产出 CONTENT 与 FINISH；`[DONE]` 后 `{"choices": [], "cost": "0"}` 跳过。门禁在鉴权之前：

| 门禁 | 要求 | 违反 |
|---|---|---|
| `User-Agent` | `opencode/<version>`，version ≥ 1.18.0 | 低于 → 426；无版本 → 403 |
| `x-opencode-session` | `^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$`，前 12 位 hex 须解码回近期时间戳 | 403 |
| body `stream` | 必须 `true` | 403 |
| body `tools` | 必须同时含 `bash` 与 `read` | 403 |
| `Authorization` | `Bearer public` 可带可不带 | 带伪造 key → 401 |

- 会话前缀照 `Identifier.create`：`ms * 0x1000 + 1`，按位取反，低 48 位写 12 位小写 hex。配置：`ZEN_OPENCODE_VERSION`、`ZEN_API_ENDPOINT`（受 `ZEN_ALLOWED_ENDPOINTS` 校验）、`ZEN_CLIENT_PROFILE`（`legacy` 默认 / `v2`）、`ZEN_MODELS_ROOT` / `ZEN_API_ROOT`（空 = 跟随对话根）、`ZEN_USER_AGENT`、`ZEN_GATE_TOOLS`（默认 `bash,read`）；`v2` 额外发 client/project/session 副本与 b3/traceparent，并注入 `<env>` system 块。
- **403 FreeTierError 客户端内消化**：退避 `GATE_403_RETRY_DELAY`（1s）换新 session id 重发，`GATE_403_RETRIES=1`，仍 403 才上抛。**注入空壳 + 改写 / 过滤**：有同义工具（`bash`→`shell`/`execute`、`read`→`read_file`）→ 注入名改写透传；无同义工具 → 整块丢弃 + WARNING；按 `index` 跟踪分片。
- **免费清单全动态**：唯一权威信号是匿名可用性；`-free` 收窄 → 并发探活，只剔除 `PROBE_DEAD_STATUSES`（400/401/403/404），429 / 5xx / 超时保留；判活缓存 30 分钟。虚拟凭证 `credential_data={}`、`added_by="system"`，`probe_quota` 恒 `probe_failed=True`；删除后重启复活，永久停用用「暂停」。

### 5.2 Kilo（`kilo`，虚拟凭证）

`https://api.kilo.ai/api/gateway`，标准 OpenAI，无私有信封、无门禁；思考在 `delta.reasoning`（`reasoning_details` 忽略）。免费模型有 `isFree` → 不探活；过滤为空抛协议错，列表用缓存兜底，免费模型标 x0。透传 `name` / `context_length` / `top_provider.max_completion_tokens` / `supported_parameters` / `architecture.input_modalities`。分类：401 / 400 / 404 / 422 → INVALID；403 → REQUEST；429 与 502/503/504 → MODEL。`KILO_CHAT_MIN_INTERVAL`（默认 0）独立 pacer。

### 5.3 Qoder（`qoder`）

推理 `POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation`，**只支持流式**，信封式 SSE。

- 请求体为自定义 Base64 变体（三段轮转 + 自定义字母表 + `=`→`$`）；认证 `Authorization: Bearer COSY.<payload_b64>.<md5sig>` + 整套 `cosy-*` 头；签名 `md5(payload_b64 + "\n" + cosy_key + "\n" + date + "\n" + body + "\n" + path)`；`cosy-machineid` / `cosy-machinetoken` 按 `uid` 稳定派生。
- 信封 `data:{"headers":…,"body":"<内层 OpenAI chunk>","statusCodeValue":200}`；`body=="[DONE]"` 结束，`statusCodeValue != 200` 判上游错误（HTTP 仍可能 200）；**保活帧 `body:"null"` 必须跳过**（结构判据 `is_keepalive_body`）。
- 模型 `GET {gateway}/algo/api/v2/model/list?Encode=1`，须带整套 COSY 头，方法固定 GET；额度 `GET {openapi}/api/v2/quota/usage`，网页 Cookie 时走 `me/usages/big_model_credits` 逐包到期。
- 签到：`GET /sash/api/v1/me/campaigns`（**须带 `Cosy-ClientType: 10`**）→ `CLAIM_BENEFIT` + `CLAIMABLE` → `POST …/{campaignId}/claim`；`replayed:true` 与 `BLOCKED+SAME_PERSON_ALREADY_CLAIMED` 归一已签。每日 **10:00（UTC+8）** 换窗口，`seal_until` 取最晚 `endAt`；国际版 404。
- 分类：节点故障包成 400（`[FAIL]node:…` / `Execution failed`）→ MODEL，否则 INVALID；403 排队 → QUEUE，`Billing daily count exceeded`（`code 110`）→ CREDIT，`TOKEN_EXPIRE` / `12153` / `Offline user session not found` → DEAD，其余 SOFT；401 一律 DEAD。`developer` 归一 system；assistant 带 `tool_calls` 时 `content:null` → `""`；入站孤立 `tool` 消息直接丢弃。

### 5.4 CodeArts（`codearts`）

- `SDK-HMAC-SHA256`：AK/SK + `X-Security-Token`，`Authorization: SDK-HMAC-SHA256 Access=<AK>, SignedHeaders=…, Signature=…`。signedHeaders = 全部头（小写、字典序）；CanonicalURI 每段 percent-encode 且末尾补 `/`；payload hash 取 `X-Sdk-Content-Sha256`。
- DPoP 刷新刚性：`POST {sts}/v1/oauth2/tokens` + ES256/P-256 JWS；refresh_token 与 `client_id=codearts-agent` + 私钥三者绑定、一次性，刷后回写；ES256 需低 S 归一化（`s > n/2` 取 `n - s`）。`refresh_skew_cap_seconds = 2700`，窗口须宽于 `REFRESH_INTERVAL_MINUTES`（默认 30）。
- SSE 双形状：v2 标准 OpenAI chunk（`tool_stream:true` 分片在 `delta.tool_calls`）；旧形状 `{"text":"<累计全文>"}` 替换语义 → `TextSnapshot` 做差；错误两路（HTTP 非 2xx / 流内 `error_code`，HTTP 仍 200）。
- 签到三端点挂 snap 引擎根、不带 `snap-manager`：`GET {snap}/v1/ops/delivery?channel=IDE` → `POST /v1/ops/claim` → `POST /v1/ops/confirm`；按 `type=="USER_LOGIN"` 挑活动，`campaignId` 取当轮（上游改过 1→34）；`ELIGIBLE` claim+confirm、`CLAIMED` 只补 confirm、`CONFIRMED`/`CONSUMED` 已签；claim 时积分即到账。
- 额度＝积分：`usageTotalPackageCredit` 直接进 `quota_remaining`/`quota_total`，分项走 `quota_packages`；`end_date` 只作展示、**不进到期阶梯**；逐笔到期走网页 Cookie `ops/credit/overview` + `campaign/{id}`，`expireAt <= now` 判死，Cookie 失效回退 7 天窗口。
- 并发：每账号每约 60s 最多 3 会话，超出 `400 TM.00001041`；`CODEARTS_MAX_CONCURRENCY`（默认 3）+ `CODEARTS_REQUEST_WINDOW_SECONDS`（默认 60，0 关闭）；名额归还需显式 `aclose`。`CODEARTS_ALLOWED_ENDPOINTS` 含 snap、STS、门户；`CODEARTS_CHAT_MIN_INTERVAL`（默认 5s）独立 pacer。

### 5.5 CodeBuddy 国际（`codebuddy_intl`）

独立渠道而非切 `CODEBUDDY_API_ENDPOINT`（进程级单值，会让存量国内凭证打到国际站）；同协议 → `CodeBuddyIntlProvider` 是数据类子类，只覆渠道 id 与三项能力。

- **唯一行为差异：首条 system 强制注入**：要求 `messages[0]` 是 system，否则 400 + `11128`；`ensure_leading_system` 不覆盖 / 不重排，补 `DEFAULT_LEADING_SYSTEM`。
- **指纹**：`X-Conversation-ID` 由 `conversation_seed` 派生 UUID v1；版本常量对齐官方现行 CLI（2026-11-18 为 2.185.0）；补 `X-Session-ID` / `X-Model-ID` / `X-Trace-ID` / `X-Product-Version`，值不明的头不发；`X-User-Id` 回退 JWT `sub`；请求体 gzip（仅 intl）；探活结果落盘 `{data_dir}/codebuddy-model-access.json`（TTL 6h）。
- **11140 无客户端解**（只能申诉）；能力边界：签到渠道级缺席（`checkin*` 置 None）、成长中心未验证（`growth=None`）、活跃上报 `/v2/report` 可用。
- `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` 默认只有国际站，填国内站启动即 `ValueError`；`CODEBUDDY_INTL_CHAT_MIN_INTERVAL`（默认 5s）独立 pacer。

### 5.6 OfficeAce（`officeace`）

| 凭据 | 寿命 | 用途 | 签名 |
|---|---|---|---|
| `Basic model_app_key:model_app_secret` | 不过期 | 聊天 | HTTP Basic |
| 临时 AK/SK + `security_token` + `project_id` | 约 2h | 积分 / 签到 / 费率 | `V11-HMAC-SHA256` |
| 同上，打控制面 | 同上 | 开通校验 | `SDK-HMAC-SHA256` |

- V11 三处必须原样：派生外层 key=AK、msg=SK；`HMAC(HMAC(AK,SK), scope+0x01)[:32]` 转 hex 文本当下一轮密钥；不加 `X-Sdk-Content-Sha256`。`CanonicalURI` 每段 percent-encode 且末尾补 `/`，query 参与签名。网关地址归一：`client-permission-validate` 的 `api_base` 带 `/v2` → 去尾部再拼端点；每账号一个网关主机，不进白名单。
- 分类：限流走伪 200（`data:{"error":{...}}`，也有首帧正常、第二帧才错）；`81004` / `81009` → BLOCKED；`81111` / `81112` / `81114` → 限流；`14018` → CREDIT；`APIG.1009` 走 DEAD 收口复探 `GET /v1/subscription`（只读）。
- `max_tokens` 改写：`>= 16` 追加 1024 headroom 再夹到模型上限；`< 16` 原样；未给不补。`max_completion_tokens` 归一为 `max_tokens`。上限 `glm-5*` 131072、`qwen3*` 65536、其余 393216。积分即主账；签到 `POST {gateway}/v1/subscription/bonus/claim` body 必须 `{}` + `Content-Type: application/json`，当日重复幂等；收获按 `bonus_skus` 剩余量增量算。
- 登录 poll 轨道：`POST /v1/claw/auth/state` → 云端回调 → `GET /v1/claw/auth/code?state=`（202 未完成）→ `POST /v1/oauth2/tokens` → `client-permission-validate`；`AuthStateStore` 按 username 分键。
- `OFFICEACE_API_ENDPOINT` / `OFFICEACE_ALLOWED_ENDPOINTS` 只管控制面；`OFFICEACE_CHAT_MIN_INTERVAL`（默认 0）。

### 5.7 小浣熊（`raccoon`）

标准 OpenAI + 网页会话 JWT。凭据接受裸 JWT / `{"access_token":…}` / `{"token":…}` / `{"xhx_token":…}` / 整段 cookie；`uid`、`expires_at` 从 claims 解出，不验签；无登录轨道。

- **未知模型防护**：发请求前按最近目录校验，流首帧再比对上游回显 `model`，不一致即失败（上游会静默回落默认模型并回 200）。额度 `available_points` 即积分，`Quota.total = remaining`；`billing_effective_multiplier` 当 `credit_rate`；按请求计费（`points=-1`）。
- 分类：401 / `user identity could not be verified` → DEAD；积分耗尽 → CREDIT；`429 user_model_rate_limited` → CONCURRENCY（首枪 60s、同链翻倍、封顶 30min）；其余 429 → SOFT；400/404/422 → INVALID。并发 `RACCOON_MAX_CONCURRENCY`（默认 2）+ 独立 pacer。
- 签到＝`user_info` → `fetch_quota` → `fetch_bills` → `checkin_result()`；只认流水 `daily_grant`，今天没有 = 失败；会话需重登信号无自愈通道。按需登录（`raccoon_auto_login`，默认关）：密码经 `PUT /api/credentials/{id}/login-secret` 录入；登录成功后 `devices_current_bind` 绑设备；`derive_device_id` 必须 ≤ 64 字符。
- 活跃上报：`GET /api/web/auth/v1/user_info` → `POST /api/web/auth/v1/devices_current_heartbeat`，头族 `x-client-device-id` 等；心跳不补积分。

### 5.8 灵犀（`lingxi`）

WPS 账号 cookie + 一次对话三条请求。凭据接受整段 Cookie / 裸 `wps_sid`；`uid` 从 Cookie 段取，重登合并首选 `uid`、兜底 `sid-<sha256 前 16 位>`。无 refresh，过期只能重贴，判据只看 401。

- `POST /api/aioffice/v1/cowork/sessions` → `POST /api/public/v1/credits/runs` → `POST /api/public/v1/agent-runtime/chat/completions`；按 run 计费，且**一个 run 只能跑一次对话**（同一 `run_id` 的第二次 chat 一律 409 `ResourceConflict`，实测 2026-11-21；并发两条只有先到的成立）→ 每个请求现领一个 run，不做跨请求缓存；重复建会话上游照收（实测连发两次均 200）。409 在出帧前换新 run 重试一次。伪装桌面端：`X-Client-Type: lingxi_claw` + `x-kso-app-name/app-version/device-id`；端点默认 `https://lingxi.wps.cn`，账号面回落 `lingxi.kdocs.cn`（白名单两者都要含）。
- SSE：正文在 `choices[].text`（非 `delta.content`）、不发 `[DONE]`；`usage.total_tokens` 是当前上下文大小 → 自算 `prompt+completion`；每帧单行 `data:{…}`，不能空行分帧。工具 schema 压平（`anyOf` / `oneOf` / `$ref` / 数组型 `type` 会让整轮失败）；`max_tokens` 注入 `base_llm_arguments`。
- 分类：401 → DEAD；402 / 额度文案 → CREDIT；403 → SOFT；404/422 → INVALID；429 → MODEL；`AuditFailed` → INVALID。额度＝智点；`bonus_credits[].expire_time` 进到期阶梯（15 天窗口）；签到 `daily_check_in` 按领前 / 领后余额差算，绝不看 HTTP 200。

### 5.9 CodeBuddy 国内（`codebuddy`）

- 端点 `https://copilot.tencent.com`；`POST /v2/chat/completions` **只支持流式**（非流式本地聚合）。头 `Authorization` + `X-User-Id` + `X-Domain` + `X-Enterprise-Id` + `X-Department-Info`（部门名 UTF-8 百分号编码）。设备码认证：`POST /v2/plugin/auth/state?platform=CLI` → 轮询 `POST /v2/plugin/auth/token?state=`；账号切换 `/v2/plugin/login/account`、`/v2/plugin/accounts`。
- 额度：个人版 `POST /v2/billing/meter/get-user-resource`（`CycleCapacity*Precise`）；企业版 `POST /v2/billing/meter/get-enterprise-user-usage`（`credit` 已用、`limitNum` 总额）。签到 `POST /billing/meter/daily-checkin`。成长中心 `/v2/activity/growth`：只读 `buddy/travel/status|config`、`tasks`、`streak`、`redeem/summary`、`lottery/chances`、`buddy/quota`、`energy`；写入 `claim|depart`、`tasks/accept`、`/tasks/{code}/claim`、`makeup-cards/use`、`redeem`、`lottery/draw`、`buddy/open`。五态 `accept_status`，接单 body 复数数组，`*_granted` 实发。
- OAuth 路径可能不回填 `account_uid`/`user_id` → 同账号隔离回落 `credential_id`；活跃上报 `POST /v2/report`（默认关），`userId` 缺失回 200 `code:0` 但静默丢弃。
- 错误码：`6004` / `14003` 模型级；`11102` / `11158` 无此模型 / 套餐不允许（负缓存）；`11129` 工具 schema 拒收；`14018` 积分耗尽；`1005` 权益耗尽；`11140` 账号风控；`11101/11115/11128/11135` 请求级。`6004` 不给 `Retry-After`，恢复时刻只在 `msg` 正文。

### 5.10 TRAE SOLO（`trae`）

- Host：Agent `https://trae-api-cn.mchost.guru`、UG `https://api.trae.cn`、OAuth `https://api.trae.com.cn`；聊天 `POST /api/agent/v3/llm_utils_chat`、模型 `POST /api/ide/v1/get_detail_param`，均仅流式。`apiHost` 用户可控 → 导入按白名单校验。
- 模型清单：`get_detail_param` 的响应体是**正常业务数据**（实测 54 条 / 535,796 字节、无 `content-length`），成功路径上限独立为 `MODELS_BODY_LIMIT`（8MB），不复用错误体的 `BOUNDED_BODY_LIMIT`（256KB，否则动态拉取恒失败并静默回退静态表）。拉取时按上游权威字段过滤：`is_invisible_to_user is True`（内部模型）+ `identity_list` 不含本账号身份（会员模型，选中必回 1005）。静态兜底表只收客户端可见模型，且沿用上次的过滤结论。
- 认证：浏览器登录 → 302 回调 → `ExchangeToken` → `GetUserInfo`；刷新 `POST /cloudide/api/v3/trae/oauth/ExchangeToken`（refreshToken 轮换）。签到 `/trae/api/v2/ug/checkin_credits/{status,claim}`；额度 `/trae/api/v2/pay/ide_user_ent_usage`。签到须确认到账：`claim` 对当天已签也回 `code:0` → `checked_in` 为真且回查 `credits` 有增加；`9074` 每次换 16 位数字串，一轮最多重试 2 次。
- SSE 序列 `metadata` → `timing_cost` → `output`×N → `extra_info` → `token_usage` → `done`；`cache_read_input_tokens` / `cache_creation_input_tokens` → `cached_tokens`，无 per-request credit。流内 `1005` 权益不足、`4008` 配额耗尽（CREDIT，冷却到次日签到）、`4001` 参数 / 模型不可用；`developer` 归一 system。

## 6. 调度器

### 6.1 选号规格

```python
MAX_ROTATE = 3
EXPIRY_WINDOW = 36h          # QUOTA_EXPIRY_WINDOW_SECONDS 覆盖；≤0 关闭整套
SECONDARY_EXPIRY_WINDOW = 7d # QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS 覆盖
COOLDOWN = {ErrKind.PLAN: 12h, ErrKind.SOFT: 60s, ErrKind.OTHER: 10m}
ERR_THRESHOLD = 3
```

排序键（字典序）：

1. **免费优先档**：候选含 `credit_rate == 0` → 免费渠道整体排前（`None` 不算免费）。`x0` 会被实测扣费校准：最近 24h `usage_events` 有该 (渠道, 归一键) 的 `credit > 0 AND credit_estimated = 0` → 注入实测均值（`db/paid_models.py`，TTL 600s）。
2. **到期额度两级**：主窗口（默认 36h）内到期额度多者先用；打平比次窗口（默认 7 天）；无到期信息计 0；主窗口 ≤0 → 次窗口失效。
3. **健康度三态**：`known(0-100) DESC > unknown > exhausted(-1)`。
4. **账户剩余积分**：只作健康度打平键。
5. 仍同分按 `credential_id` 稳定。

- **管理员权重**（`credentials.routing_weight`，`-1000..1000`，NULL/0 默认）在排序后做同健康档内稳定重排，不能把 exhausted/unknown 越过 known 档。**手动 pin 优先**：有 `pinned` → 只在 pinned 间排序。到期积分只算一处 `expiring_credits()`。
- **可选中过滤**（`Executor._selectable`，不进 `Candidate.is_selectable`）：① 凭证限额；② 在途租约；之后注入 `Candidate.inflight` 供排序。**TRAE 也落 `quota_expiry_ladder`**（权益包独立到期），`quota_cycle_end` 仍 NULL。

### 6.2 候选收窄

`Executor._narrow_providers`：**候选只信模型目录缓存**。目录能证明归属 → 只保留它们；全不可用直接 503，不逐渠道试。目录未就绪或模型不在目录 → 全部候选；`模型@渠道` 与 Key 绑定不受影响。缓存已收敛到服务层唯一一层（TTL 以服务层 `MODEL_LIST_TTL_SECONDS` 为准）+ 后台兜底刷新。

### 6.3 模型级冷却

`credential_model_cooldowns(credential_id, model, cooling_until, hits, reason)` 按 **(凭证, 模型)** 建表；`model_probes` 只是观测，冷却表才是调度权威。

- 登记名 = 该凭证所属上游的原始模型名（`_upstream_model`）。**冷却到期先半开**（`engine/half_open.py`）：只放行一个探测请求；成功记「已探通」120s 内直放；失败写新冷却并释放探测位；TTL 600s 兜 GC。
- 退避：MODEL 10m→2h；BLOCKED 6h→24h；CONCURRENCY 首枪 60s，同链翻倍，封顶默认 5min / `CONCURRENCY_LONG_WINDOW_PROVIDERS`（raccoon）30min。同 (provider, model) 90s 内第二个不同凭证再命中 → 升渠道封顶档。
- 上游点名恢复时刻优先：`Retry-After` → `parse_retry_after`；头缺失时 CB 走 `parse_reset_at` 读正文；均钳 `[now + 常量, now + 24h]`。
- 清除：`save_success(..., model=)` 只删 `reason='blocked'`；删除凭证 / 账号级冷却出现 / revive 都清模型条目；留存任务清理过期行。**503 带 `Retry-After`**：取所有候选里最早的自动恢复点；禁用 / 限额 / 在途不参与；无自愈候选不发。全部候选只因该模型被排除 → 503 `model 'x' temporarily unavailable on upstream`，统计 `error_type=model_unavailable`。

### 6.4 跨渠道 fallback 兼容组

`MODEL_FALLBACK_GROUPS`（默认 `""` 关闭）：`组名=成员1,成员2;…`。组名是入口别名、不进链；`@渠道` 与 Key 绑定不参与；目录可用时剔除未登记成员；流式只在本项未出帧前切换；非流式逐链项走完整选号 / 冷却 / 轮换。

### 6.5 凭证限额护栏

`credential_limits(credential_id PK, keep_credits, daily_tokens, daily_credits, per_model_tokens JSON, …)`，**缺行 = 不受限**。

- `quota_remaining ≤ keep_credits` 停整号；`daily_*` 当日累计 `≥ 上限` 停整号；`per_model_tokens` 只停该模型；`None` / `≤0` 关闭。当日口径 = 北京自然日 0 点；聚合 `usage_events`（不用 `usage_hourly`，主键无 `credential_id`），失败请求也计入。`LimitGuard` 快照 TTL 15s，写入即失效；全部因限额跳过 → `no_healthy_credential`。

### 6.6 凭证在途租约与隐私脱敏

`MAX_INFLIGHT_PER_CREDENTIAL`（默认 0 不限）：达上限后该凭证不参与选号。在途计数兼作负载键，按「最少连接」排（免费档之后、到期额度之前）。`InflightTracker` = `credential_id → 活跃租约开始时刻`，同步实现，选号 → 登记间无 await；`LEASE_TTL_SECONDS = 600` 兜底。与 pacer `max_concurrency` 正交：后者按渠道分，这里是跨渠道凭证级上限。

`PRIVACY_RULES`（默认空 = 关）：压缩 / 档位映射之后、发上游之前对请求体副本打码；规则 `url_credentials` / `email` / `cn_mobile` / `private_ip`。只处理 `messages[].content` 文本与 `tools[].function.description`；`request.raw`、`tool_calls.arguments`、JSON Schema 不动。占位符单向、不还原；规则名错误一律忽略。

## 7. 请求时序（chat completions 主链路）

> 可视化：[`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html)。`POST /v1/responses` 同链路，先映射成 `ChatRequest`。

```
客户端 → POST /v1/chat/completions (Bearer sk-)
  1. deps.require_api_key：摘要查 api_keys → username（+ IP/到期/模型白名单）
  2. request.py：校验 body → ChatRequest；model_resolver 解析候选集
     - "glm-5.2" → 各 provider；"glm-5.2@trae" → 仅 trae；auto/空 → DEFAULT_MODEL
  3. 选号（executor._select → scheduler.select）：
     a. 收窄到登记该模型的渠道；收窄后无可用 → 503
     b. 模型级冷却过滤（按上游原始模型名）
     c. pin 优先，其次粘性命中且可选
     d. 过滤 healthy（enabled=1, disabled=0, 非冷却中）
     e. 免费优先 → 到期额度两级 → 健康度三态 → 剩余积分 → credential_id
  4. executor：解密 → 按实际渠道压缩 → provider.stream_chat()（≤ AUTO_CONTINUE_MAX）
     - 上游 HTTP ≥400 → classify → note_error → tried → 回到 3（最多 3 次）
     - 流内 Event.ERROR → 同上；注入出口错误帧 + 冷却 + 轮换
     - INVALID 不冷却凭证，跳过该上游全部凭证；全拒 → 400
  5. 出口：首块补 role:assistant；tool_calls 补稳定 index / id
     收尾帧 finish chunk → usage 帧（choices: []）→ [DONE]；未报 usage 不补
  6. stats.collector：写 usage_events（username/provider/model/tokens/latency/ttfb/ok）
  7. scheduler.note_success：清 err_count，重绑粘性
```

- 客户端断连 → `error_type=client_disconnect` + 关闭上游流；`[DONE]` 后收尾断开按成功记账。流截断（HTTP 200 但从未产出 FINISH）→ `ok=False` + `error_type=truncated_stream`，凭证照常 `save_success`。
- 上游模型观测：`Event.model` 与请求模型先无损归一（去 `models/`、`sn-`、`ep-…`、`.`/`_`→`-`、版本尾缀），双向前缀匹配；真替换才 WARNING，不进失败归因。TRAE 模型只在元数据帧回显；Qoder 实测只有 `model:"auto"`。

## 8. 数据库

DDL 以 [src/db/schema.sql](src/db/schema.sql) 为准。`SCHEMA_VERSION = 39`（v33 `web_cookie_state`、v34 `routing_weight`、v35 `login_secret`、v36 `log_events`、v37 `credential_events`、v38 `access_freezes`、v39 `credentials.updated_at` + `credentials.allowed_models`），17 张表：

`api_keys` / `credentials` / `credential_limits` / `users` / `audit_events` / `usage_events` / `usage_hourly` / `growth_events` / `checkin_events` / `credit_events` / `credential_model_cooldowns` / `credential_events` / `access_freezes` / `model_probes` / `runtime_settings` / `alert_events` / `log_events`。

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;   -- 库里没有外键，关系全在应用层校验
```

- 连接：`threading.local()` 每线程一个 `sqlite3.Connection`；写入统一走 `Database.transaction()`，并发由 SQLite 串行化。
- 加密：`Fernet(base64.urlsafe_b64encode(sha256(APP_SECRET).digest()))`；APP_SECRET 丢失 = 凭证全部不可解。启动按熵拒弱密钥（长度 ≥16、不同字符数 ≥8）。
- **解密失败不连累整批**：批处理路径（选号 / 模型列表 / 后台任务 / 探测）捕获后跳过该凭证；单条探测端点如实报 500 `credential_decrypt_failed`。
- `runtime_settings(key, value, updated_at)`：只存管理台改过的 key，未出现回落 env；值由 `HOT_SETTINGS` 校验，非法行读取时忽略并记日志。
- 模型目录不占表：进程内 TTL + `DATA_DIR/model_catalog.json` / `openrouter_catalog.json`（默认 `DATA_DIR=./data`）。
- **备份**：`VACUUM INTO` + `PRAGMA integrity_check`，产物 0600 / 目录 0700；每 `BACKUP_INTERVAL_MINUTES`（默认 1440）备份，保留 `BACKUP_KEEP`（默认 7）。恢复 `scripts/restore_db.py`：校验 → `.pre-restore-<ts>` → `os.replace`；默认预览，`--apply` 才写。
- **凭证标签**：逗号分隔，`MAX_TAGS=16` / `MAX_TAG_LENGTH=24`；写入校验、读取容错；不参与调度与鉴权。
- **凭证级模型白名单**：`credentials.allowed_models`（fnmatch glob，逗号分隔，`''` = 不限制，与 `api_keys` 同语法）。在 `candidates(model=...)` 生成候选时收窄，**不硬禁用、不写冷却**——换个模型立刻可用。不传 `model` 的后台路径（签到 / 刷新 / 探活 / 额度 / 模型列表）看到全量。
- **编辑版本号**：`credentials.updated_at` 只有**人工编辑**推进（`MAX(now, 旧值+1)`），后台自动写（冷却 / 额度 / 探活 / 心跳）不推进——否则 30s 轮询会让打开中的编辑页永远 409。编辑端点带 `expected_updated_at` 做乐观并发，不匹配回 409 + 当前行快照；不传 = 兼容旧调用方（最后写入者赢）。
- **统计写入**：`record()` 先入队、由抢到写锁的写者一次写空（每批 20 行），保持 read-after-write；停机前 `flush()` 排空。失败行回填队首并计入 `stats_write_failures_total`（不再只留一行 WARNING）。
- **救援判定索引**：`idx_usage_request_rescue (request_id, username, ok)`。刻意不改 SQL（先聚合成功集、把全表成功行物化出来反而更慢——实测 5 个场景 4 个更慢），改用覆盖索引消掉回表。
- **迁移纪律**：`schema.sql` 只加不改；新增列走 `_MIGRATION_COLUMNS`，删表走 `_MIGRATION_DROPS`，删列走 `_MIGRATION_COLUMN_DROPS`，均 `SCHEMA_VERSION + 1`；版本记 `PRAGMA user_version`，库版本高于程序 → 启动报错；升级只在启动时发生。

## 9. 模型目录与模型名

### 9.1 三字段与归一键

| 字段 | 函数 | 例 |
|---|---|---|
| 原代号 `raw_id`（渠道请求 key，永不改动） | — | `kmodel_latest`、`kilo-auto/free` |
| 归一键（合并键 / 对外 id） | `normalize_model_key` | `kilo-auto`、`longcat-2.5-preview` |
| 展示名 `name` | `display_model_name` | `LongCat 2.5 Preview`、`Qwen3.8 Max` |

- 归一键剥免费标记（`-free` / `_free` / `/free` / `:free` / `(free)`）、`new`、命名空间前缀（路径取末段、冒号前缀、`sn-`）后 slug 化；点号不是分隔符。
- 合并键 = 归一键，十渠道统一；同模型异名走 `MODEL_SYNONYMS`，只登记人工核实等价；哨兵 `auto` / `default` 不跨渠道合并。
- 对外 id 一律归一键，原代号经 `services.model_aliases` 映射回；每渠道登记对外 id / 原代号 / 展示名三键；撞车按 `_fallback_ids` 消歧。展示顺序 `_PROVIDER_RANK` 只影响展示，调度选号顺序（`KNOWN_PROVIDERS`）不变；自定义别名见 §3.6。

### 9.2 加载、SWR 与探活

- 没凭证的渠道不读缓存、不拉上游、不展示。失败也进 TTL（负缓存）：连续失败按 `2^(n-1) × TTL` 退避，封顶 `MODEL_LIST_BACKOFF_MAX_SECONDS`（2h），成功清零。
- `/v1/models` 与 `/api/playground/models` 走 SWR：有缓存立即回旧列表、过期渠道丢后台；某渠道一条缓存都没有时才同步等；后台刷新去重并在 `lifespan` 关闭时取消；逐渠道增量 `publish_aliases()`。
- `model_probes.failed()` 命中的 (渠道, 模型) 只从展示摘掉，**别名登记保留**（否则收窄失效 → 全渠道扇出 → 负缓存污染）。`ModelProbeTask` 复探 `state='failed'` 行，成功即恢复；`cleanup_stale_blocked.py` 清理噪声负缓存。

### 9.3 用量统计与成本

- `overview` / `by_provider` / `by_model` / `by_user` / `timeline` / `model-timeline` 读 `usage_hourly`（永久），`events` 读明细（90 天）；凭证维度读明细表。
- 双写：`record()` 写明细 + 增量累加当前小时；`rollup_hourly` 每 5 分钟只重算最近 `ROLLUP_OVERLAP_SECONDS`（2h，整点对齐）而非全量，幂等。
- 成本 = 刊例价 × token（输入 − 命中 / 命中 / 输出三段），写入时按 `USD_CNY_RATE` 折 CNY，历史不重算；匹配不到不计（`cost_known=0`）。
- 失败归因：`failures` 是尝试口径，`rescued` 为同用户同 `request_id` 另有成功行，`failures − rescued` 为未救回次数。`BLOCKED` → `model_not_found`；`CONTEXT` → `context_overflow`；`CAPABILITY` → `model_capability`；`NETWORK` → `network_error`；`SOFT` → `rate_limit`。
- 时序指标在聚合后相除，无样本给 `null`；时间轴补小时（上限 `MAX_DENSE_HOURS` 92 天）。实时流 `GET /api/stats/events/stream`：进程内广播，订阅者有界 200、上限 8，超出 429 + `Retry-After: 10`。列表排序 `sort` 白名单 + `order`，未知键回落默认；分页 `limit` + `offset` + `total`。

## 10. 后台任务

`TaskRunner` 每类任务一个 asyncio 循环，失败只记日志。`Pacer` 后台严格模式（单锁串行）/ 聊天并发模式（按「渠道 + 凭证身份」分桶）；抖动 `JITTER_RATIO = 0.25`，定值间隔只向上抖。

| 任务 | 周期 | 行为 |
|---|---|---|
| 额度探测 | 启动立即 + 每 `QUOTA_PROBE_MINUTES`（60） | 写 `quota_*` / `health`；硬禁用与 `enabled=0` 跳过 |
| token 预刷新 | 每 `REFRESH_INTERVAL_MINUTES`（30，下限 5） | `REFRESH_SKEW_HOURS`（24h）窗口内轮换；短寿命渠道自封顶（CodeArts 2700s） |
| 每日签到 | `CHECKIN_TIMES` 空 → 每 10 分钟 | 成功封账；失败指数退避 10m→1h；每次落 `checkin_events` |
| 成长中心 | 每 `GROWTH_INTERVAL_MINUTES`（60，下限 5） | 仅 CB：7 类领取；落 `growth_events` |
| 活跃上报 | 每 10 分钟醒，仅 `ACTIVITY_REPORT_HOUR`（10 点）窗口 | CB + 国际版；按「endpoint + userId」隔离、当日封账 |
| 明细清理 | 每 5 分钟 | 重算小时汇总 + 90 天清理 + 过期冷却；`asyncio.to_thread` |
| 数据库备份 | 每 `BACKUP_INTERVAL_MINUTES`（1440） | `VACUUM INTO` + `integrity_check`，保留 `BACKUP_KEEP`（7） |
| 渠道模型列表刷新 | 每 `MODEL_CATALOG_MINUTES`（30） | 兜底重拉（TTL + 逐渠道 publish + 落盘） |
| 模型目录刷新 | 每 `OPENROUTER_CATALOG_MINUTES`（1440） | 拉 OpenRouter → 三张表 → 落盘 |
| 运维告警 | 每 `ALERT_INTERVAL_MINUTES`（5） | 九类规则；命中落 `alert_events` + webhook；静默窗去重 |
| 上游状态聚合 | 每 `UPSTREAM_STATUS_MINUTES`（5） | 并发抓状态 → 归一 → 原子替换内存快照；整轮永不抛 |
| 模型主动探活 | 每 `MODEL_PROBE_MINUTES`（0 关闭） | 只探仍被 blocked 的 (渠道, 模型)，`max_tokens=16` |

- 签到 / 成长隔离键 `checkin_scope(data) or f"credential|{credential_id}"`；`_guarded` 在任务返回 `None` 时不入账；运行态只在本进程，重启归零。
- 手动 `POST /api/tasks/{key}/run`（admin + CSRF）返回 `{…, ran, busy}`；运行中撞上回 `busy=true`。
- 告警九类：`pool_empty` / `task_failed` / `token_expiring` / `error_rate` / `key_burst` / `off_hours` / `budget` / `channel_dead` / `checkin_failing`；单 Key / 非工作时段 / 预算默认关，后两类默认开。迟滞 `ALERT_FOR_TICKS`（1）/ `ALERT_CLEAR_TICKS`（2），恢复写 `resolved_at`。
- webhook 恒带 `X-Coding2API-Signature: sha256=<HMAC-SHA256(APP_SECRET, body)>`，签名算在实际发送字节上；飞书 / Slack 只带标题行。
- 上游状态四解析器（statuspage / opencode / betterstack / atom）归一六档；`aggregate_level` 取最差，`unknown` 权重 -1；只存内存不落盘。

## 11. 出口辅助端点与错误语义

### 11.0 端点总览

**外部（API Key）**：`POST /v1/chat/completions`（`n` 只接受 1）、`POST /v1/responses`、`POST /v1/messages` + `POST /v1/messages/count_tokens`、`POST /v1beta/models/{model}:{generateContent|streamGenerateContent|countTokens}` + `GET /v1beta/models`、`GET /v1/models` + `GET /v1/models/{id}`（对外 id / 展示名 / 任一渠道原 id 三种写法，不存在或不在 Key 白名单 → 404 `model_not_found`）、`GET /v1/usage?days=1|7|30`（只查本 Key 明细）、`GET /v1/user/balance`、`GET /health`、`GET /healthz`、`GET /metrics`。

**管理台（Cookie + RBAC）**：`/api/credentials`（`checkin` / `toggle` / `revive` / `probe` / `activity` / `pin` / `accounts/select` / `export` / `batch` / `batch-import` / `limits` / `tags` / `weight` / `login-secret` / `web-cookie`）、`/api/api-keys`、`/api/users`（`policy` / `disable` / `enable` / `reset-password`，**无 DELETE**）、`/api/model-catalog`、`/api/models/availability`、`/api/settings`、`/api/tasks` + `POST /api/tasks/{key}/run`、`/api/alerts` + `POST /api/alerts/{id}/resolve`、`/api/upstream-status`、`/api/stats/*`、`/api/billing`、`/api/logs` + `/api/logs/stream`、`/api/backups` + `/api/backups/restore`、`/api/nav-badges`、`/api/auth/*`、`/api/audit`、`GET /authorize`。列表端点统一 `sort` + `order`，未知键回落默认。

### 11.1 余额、健康检查与指标

- `GET /v1/user/balance`：DeepSeek 形状，Bearer `sk-...`，余额来自探测缓存；未探测成功 → `balance_known=false`。
- `GET /health`：`{"status":"ok"}`；`GET /healthz` 额外给池计数，`ready=0` 时 503；两者无鉴权、接受 HEAD。四类计数互斥，优先级 `disabled → paused → cooling → ready`；`/healthz` 的 `ready` 不看 `health`，侧栏角标是另一套口径。
- `GET /metrics`：零依赖手写 Prometheus text，默认开、默认不鉴权；`METRICS_ENABLED=false`（热更）→ 404；`METRICS_TOKEN` 非空要求 Bearer（恒时比较）。指标 `coding2api_requests_total` / `coding2api_tokens_total` / `coding2api_credentials` / `coding2api_upstream_errors_total` / `coding2api_task_failures_total`；禁高基数标签（不打 credential_id / api_key_id / username / request_id）。
- `GET /api/nav-badges`：按角色裁剪 payload，缺失键 = 无权看；计数只数「待办」，与页面数据不同口径。

### 11.2 日志

- 只写 stdout / stderr，不写文件、不自行轮转；`build_app` 配 root logger（`LOG_LEVEL` 默认 INFO；审计是 INFO）。`RequestLogMiddleware` 每业务请求一行 logfmt：`event=request request_id=… method=… route=… status=… duration_ms=…`；只取 `scope["path"]`，不读 query string / body / 头 / 用户名。
- 日志页 `GET /api/logs` + `GET /api/logs/stream`（admin-only）；`redact_or_drop` 命中即整行丢弃；`LOG_BUFFER_LINES=0` → 404。`log_events` 容量 `LOG_PERSIST_LINES`（默认 2000，0 = 不落库），按最近 N 行裁剪。

### 11.3 错误语义与出口辅助

- 管理端点失败给稳定枚举 `reason`：`credential_rejected` / `rate_limited` / `upstream_unavailable` / `upstream_rejected` / `upstream_response_invalid` / `upstream_timeout` / `network_unreachable` / `unknown_error`；`detail` 仅供排查。`ClientDisconnect` → 499 + `client_disconnected`。对外 400 / 503 只带受控标识。`CREDENTIAL_PROXY_POOL`（启动期）非空时同一凭证固定出口：`sha256(credential_id) → int % len(pool)`；优先级 凭证池 → `PROVIDER_PROXIES[渠道]` → 直连；OAuth 登录阶段不覆盖。
- 工具调用：`id` 缺失按 index 生成 `call_{index}_{seq}` 并记忆；**DSML 泄漏回读**（单 / 双竖线方言 + V4 自闭合 `invoke`，流式增量过滤，回读出调用时 `finish_reason` 改写 `tool_calls`）；**坏参数 fail-closed**（非流式校验 `arguments`，非法 → `InvalidToolArguments`，换上游 / 全败 400 / 不冷却）；**用量对账**（≥200 字符却无 token、或 >12 字符/token → WARNING，只告警）。

## 12. 测试策略

- `tests/` 按 `src/` 子包镜像；跨模块回归在 `tests/regression/`，私有替身放非收集模块 `_support.py`。**100%（行 + 分支）硬门槛** `--cov-fail-under=100`；禁 pragma 达标。例外（注释说明）：`__main__`、协议演进防御、前置 return 死分支；测试替身宽于用例接口的方法。
- **平台差异分支禁 pragma，须 monkeypatch 测全**；测试不得依赖本地 `.env`（`Settings(_env_file=None, APP_SECRET=...)`）。fixture 存 `src/provider/fixtures/`，断言两方向：解析正确 + 畸形帧抛 `UpstreamProtocolViolation`；codebuddy / trae 有落盘样本，其余逐帧单测。真连上游默认关闭，需真探活标 `@pytest.mark.real_upstream`。

## 13. 部署、配置与安全

### 13.1 运行时配置覆盖层

| 类别 | 例子 | 变更方式 |
|---|---|---|
| 启动期不可变 | `APP_SECRET` / `HOST` / `PORT` / `DATA_DIR` / `USERS_FILE` / 端点白名单 / `PROVIDER_PROXIES` | 改 env + 重启 |
| 运行时可覆盖 | `runtime_settings.HOT_SETTINGS` 白名单 | 管理台改，立即生效 |

- 优先级 `runtime_settings` 表 > `.env`；覆盖行只存改过的 key，「恢复默认」= 删行；读取不查库。`config.live(value)` 把标量或零参 callable 统一成取值器。`GET /api/settings` 返回 snapshot + 覆盖计数 + groups；`PUT` body `{"values": {key: 标量 | null}}`，写入前校验类型 / 范围 / 跨字段，写操作记审计。

### 13.2 出站代理与重启

- `PROVIDER_PROXIES`（启动期，默认 `""` 直连）：`渠道=代理URL;…`，协议 `http/https/socks5/socks5h`，作用于该渠道全部出站请求；未知渠道 / 非法协议 → `ValueError`。`trust_env=False` 是全项目纪律。
- 前端 `FileResponse` 每次现读 `web/dist`，重建后刷新即生效；后端 `src/` 只在启动时装载 → 改 `src/` 必须重启，否则「新前端 + 旧后端」新端点 404。

```bash
docker compose up -d --force-recreate   # Docker / compose
sudo systemctl restart coding2api       # systemd
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version;"   # 期望 36
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users  # 期望 401
```

### 13.3 账号、审计与安全

- 三层引导：导入 `users.txt` → `ADMIN_USERNAMES` 引导期提权 → 无活跃 admin 启动失败。三角色 `admin` / `operator` / `viewer`。鉴权链：签名 Cookie（HMAC，含用户名与 `session_epoch`）→ 用户存在且启用 → epoch 一致 → 角色现读 DB；角色不进 Cookie，降级立即生效。
- 一次性激活令牌：库里只存 SHA-256 摘要 + 过期时间（24h）；`must_change_password=1` 时只放行看会话 / 改密 / 登出。审计覆盖登录、账号、凭证、Key 发 / 删、用户策略、运行时配置、明细导出；绝不记明文。
- 用户级策略（`allowed_models` / `quota`）：Playground 与 `/v1` 都判，与 Key 白名单同时生效；配额按用户名聚合。kill switch（`gateway_paused`）拦在 API Key 鉴权依赖，返 503 `service_paused`。上游端点白名单：各渠道独立 `*_ALLOWED_ENDPOINTS`；CB 国际白名单不含国内站；TRAE `apiHost` 按 `ALLOWED_OAUTH_HOSTS` 拒。
- 请求体上限（登录 8KB / 备份恢复 512MB / 其余 16MB）；CSP `frame-ancestors 'none'`；登录两级限流（IP / 用户名；全局窗口只观测，避免未认证攻击者打满后锁死全站登录）+ PBKDF2 并发上限；API Key 仅存摘要，明文只回显一次。入站解析拒绝越界数值与 `NaN` / `Infinity`（含 `1e999`）；SSE 单行与单帧缓冲有上限；非 2xx 错误体有界读取（256KB）。

## 14. 已知取舍

- **同步 sqlite3**：本地微秒级操作，asyncio 封装开销大于收益；ORM 收益为负。**双 httpx 客户端**：聊天流 `read=None` 防长流截断，短请求总超时 30s；非流式由引擎聚合同一流式上游。
- **polling OAuth 不转回调**：上游协议决定；TRAE 回调走主端口 + `PUBLIC_BASE_URL`，`/authorize` 无鉴权靠会话 + `loginTraceID` 双绑定。**effort 映射默认不做**：只 `setdefault` 补 `medium`，三项机制默认关。
- **Zen / Kilo 用虚拟凭证行而非特判**：调度 / 冷却 / 统计 / 粘性全按 `credentials` 行工作。**Responses / Anthropic 只做各自客户端子集**：不做 `store=true` / `previous_response_id`，不发 `[DONE]`；未跑真实 Codex CLI 端到端。
- **统计 / 成本 / 日志三条口径**：统计以 `usage_hourly` 为准（最近 ≤5 分钟未汇总不计）；成本是下限；应用日志只写 stderr。**批量运维只收已有单条端点的动作**（`probe` / `toggle` / `checkin` / `revive`），`BATCH_CONCURRENCY=4`；审计只记一条汇总。
- **窄屏表格卡片化改样式不改 DOM**：`<768px` 一行一卡，`TableCell label` → `data-label`；断点对齐 Tailwind `md`。**一个渠道一个组**：`groupByProvider()` 按真实 `provider` 聚合；CodeArts 账本为 `credits`，其余 `pool`；后端不拆行（refresh_token 一次性）。
