# Coding2API 技术方案

PROPOSAL.md 定方向，本文档定实现。每个模块标注来源决策（Q 编号）。

---

## 1. 技术栈定稿

| 层 | 选型 | 版本 | 决策 |
|---|---|---|---|
| 运行时 | Python | 3.12（本机 3.12.14；`requires-python >=3.12`，CI/Dockerfile 均 3.12） | Q2 |
| 包管理 | uv（venv + pyproject.toml + uv.lock） | 本机 0.12.x | T-Q1 |
| Web | FastAPI + Uvicorn | 0.141 / 0.52 | Q2=A |
| 配置 | pydantic-settings | 2.15 | T-Q3 |
| HTTP | httpx 双客户端（流式/短请求分离） | 0.28.1 | T-Q4 |
| 数据库 | 标准库 sqlite3（WAL）+ 手写 SQL | 内置 | T-Q2 |
| 加密 | cryptography Fernet（凭证列） | 50.0 | T-Q2 |
| 测试 | pytest + pytest-cov + respx + 文件化 fixture | pytest 9.1 / respx 0.23.1 | T-Q5 |
| 前端 | React 19 + Tailwind 4 + shadcn/ui + @lobehub/icons + Vite 7 | 已核实（实例版本见 `web/package.json`） | Q5 |

> 上表是**选型定稿**；具体小版本随依赖更新漂移，实际以 `pyproject.toml` /
> `uv.lock` / `web/package.json` 为准。仅当**主版本或兼容边界**（如
> `requires-python`、Node 主版本）变化时才需要回改本表。

---

## 2. 目录结构与模块规格

```
coding2api/
├── pyproject.toml               # uv 项目；[tool.pytest.ini_options] 设 coverage 目标
├── src/
│   ├── main.py                  # FastAPI 组装、lifespan、路由挂载（只做接线）
│   ├── config.py                # pydantic-settings：README「配置」全部 env；live() 归一化标量/取值器
│   ├── runtime_settings.py      # 运行时配置覆盖层（B3.2）：DB 覆盖 > env，白名单 + 校验 + snapshot
│   ├── version.py               # 版本号（pyproject.toml 为唯一真源，读不到回落常量）
│   ├── webapp/                  # HTTP 边缘层（横切关注点，与业务装配分开）
│   │   ├── limits.py            # 请求体上限 ASGI 中间件（登录 8KB / 其余 16MB）
│   │   ├── security.py          # Host 白名单 + 安全响应头（CSP/nosniff）
│   │   ├── handlers.py          # 异常 → HTTP 响应（稳定错误码，§6.3）
│   │   ├── logging.py           # root logger 配置（审计/上游日志落 stderr）
│   │   └── static.py            # 前端产物定位 + SPA catch-all
│   ├── db/
│   │   ├── schema.sql           # DDL 定稿
│   │   ├── conn.py              # 线程本地连接 + WAL + busy_timeout
│   │   ├── migrate.py           # 启动时执行 schema.sql（CREATE IF NOT EXISTS）
│   │   ├── repo.py              # 凭证 / API Key 持久化（手写 SQL）
│   │   └── crypto.py            # Fernet：APP_SECRET → key derive → encrypt/decrypt
│   ├── auth/
│   │   ├── users.py             # users.txt 解析（username:PBKDF2）；hash_password.py CLI
│   │   ├── session.py           # 会话 Cookie 签发/校验（手写 HMAC，不落库）
│   │   ├── api_key.py           # sk- 生成、SHA-256 摘要存储、常量时间校验
│   │   ├── csrf.py              # 写操作 CSRF 校验（自定义头 / 同源 Origin）
│   │   ├── throttle.py          # 登录限流（三级窗口 + PBKDF2 并发上限）
│   │   ├── rbac.py              # ADMIN_USERNAMES 判定；require_admin 依赖
│   │   └── access.py            # API Key 来源 IP 白名单（B3.5，纯函数）
│   ├── provider/
│   │   ├── base.py              # Provider 协议、Event、ErrKind、Quota、HealthScore
│   │   ├── codebuddy/
│   │   │   ├── client.py        # 上游 HTTP + SSE 流 + 额度探测
│   │   │   ├── events.py        # OpenAI 风格 SSE → Event
│   │   │   ├── credential.py    # 凭证类型与解析
│   │   │   ├── headers.py       # 上游技术常量与请求头构造
│   │   │   ├── oauth.py         # 设备码轮询
│   │   │   ├── checkin.py       # 签到 + 连续天数
│   │   │   ├── activity.py      # 活跃上报协议层（B1.7）
│   │   │   ├── growth.py        # 成长中心协议层：15 个端点
│   │   │   ├── growth_runner.py # 成长中心编排：7 类领取的顺序与失败判定
│   │   │   └── refresh.py       # token 刷新 + 多账号切换
│   │   ├── trae/
│   │   │   ├── client.py        # SOLO 上游 + 额度探测
│   │   │   ├── events.py        # 自定义 SSE → Event
│   │   │   ├── credential.py    # 凭证解析（嵌套/扁平）+ 原子写回 + 签到设备号
│   │   │   └── callback.py      # 登录 URL 构造 + 回调解析
│   │   ├── token_expiry.py      # 到期提取：显式 expires_at → JWT exp 回落（B3.3）
│   │   └── fixtures/            # 真实样本
│   │       ├── codebuddy/*.sse
│   │       └── trae/*.sse
│   ├── engine/
│   │   ├── scheduler.py         # 选号 + 冷却状态机 + pin
│   │   ├── executor.py          # 请求执行 + 轮换重试 + 统计埋点
│   │   ├── affinity.py          # 会话粘性（CONVERSATION_STICKY_SECONDS，B1.5）
│   │   ├── continuation.py      # 截断续写（AUTO_CONTINUE_MAX，B1.4）
│   │   ├── model_resolver.py    # "glm-5.2" | "glm-5.2@trae" | auto → 候选集
│   │   └── sse.py               # SSE 帧解析（跨 provider 共用）
│   ├── compat/
│   │   ├── openai/
│   │   │   ├── request.py       # ChatRequest 校验 + 上游 payload 构造
│   │   │   ├── response.py      # 流式 chunk 生成 + 非流式聚合
│   │   │   └── errors.py        # OpenAI error shape
│   │   └── responses/           # Responses 出口（B2.1，仅 Codex CLI 子集）
│   │       ├── request.py       # Responses → ChatRequest 入站映射
│   │       └── response.py      # Event → Responses SSE
│   ├── tasks/
│   │   ├── pacer.py             # 全局节流器（PACER_MIN/MAX 随机区间）
│   │   ├── quota_probe.py       # 启动立即一轮 + 每 QUOTA_PROBE_MINUTES
│   │   ├── checkin.py           # 全天每 10 分钟；成功即当日封账
│   │   ├── growth.py            # 成长中心（仅 CB），落 growth_events
│   │   ├── activity.py          # 活跃上报（仅 CB，默认关闭）
│   │   ├── refresh.py           # 每 60 分钟；REFRESH_SKEW_HOURS 窗口内预刷新
│   │   ├── retention.py         # 每 5 分钟：小时汇总重算 + 90 天前明细清理
│   │   ├── status.py            # 任务清单 + 进程内运行态（B4）
│   │   └── runner.py            # 后台任务调度，接入应用生命周期
│   ├── stats/
│   │   ├── collector.py         # usage_events 写入（脱敏）+ 小时汇总双写/重算
│   │   └── query.py             # overview / by-provider / timeline / model-timeline / events
│   └── api/
│       ├── deps.py              # Services 容器 + require_api_key / session / csrf 依赖
│       ├── chat.py              # POST /v1/chat/completions
│       ├── responses.py         # POST /v1/responses
│       ├── models.py            # GET /v1/models（动态拉取 + 黑名单 + 元数据）
│       ├── balance.py           # GET /v1/user/balance（读探测缓存聚合）
│       ├── authorize.py         # GET /authorize（TRAE 回调落点）
│       ├── admin_credentials.py # 凭证 CRUD / toggle / pin / probe / checkin / 成长 / 账号切换
│       ├── admin_keys.py        # API Key CRUD（渠道绑定 / IP 白名单，B3.5）
│       ├── admin_settings.py    # GET/PUT /api/settings（B3.2）+ GET /api/tasks（B4）
│       ├── admin_stats.py       # 统计查询
│       ├── admin_auth.py        # 登录 / 登出 / 会话；上游登录 start/poll/cancel
│       ├── playground.py        # 会话调试端点（无需 API Key）
│       └── streaming.py         # SSE 流包装（长空隙插心跳帧）
├── web/                         # React 前端
├── deploy/                      # newsyslog / logrotate / systemd 模板
├── diagrams/                    # 架构图（HTML + 源 JSON）
├── scripts/                     # hash_password / install-newsyslog
├── tests/
├── Dockerfile / docker-compose.yml  # 仓库根（compose build context 依赖根目录）
├── NOTICE / LICENSE / README.md（中文）/ README.en.md
```

---

## 3. 核心类型定义

### 3.1 中立事件层（provider/base.py，Q13=B 预留）

```python
class EventKind(StrEnum):
    CONTENT = "content"        # 正文增量
    REASONING = "reasoning"    # 思考链增量
    TOOL_CALLS = "tool_calls"  # 工具调用增量（上游原样，index 由 engine 补齐）
    USAGE = "usage"            # prompt/completion/reasoning tokens；credit 可空
    FINISH = "finish"          # finish_reason
    ERROR = "error"            # 流内业务错误

@dataclass(slots=True)
class Event:
    kind: EventKind
    content: str | None = None
    tool_calls: list[dict] | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error_code: int | None = None
    error_message: str | None = None

@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None  # 输入中命中缓存的 token（TRAE cache_read 映射；缺省 None）
    credit: float | None = None    # 上游可选字段，两边都经常为 None
```

事件名过滤：TRAE 上游会发 `metadata` / `timing_cost` / `extra_info` / `progress_notice`
等无下游语义的事件，`events.py` 的 `KNOWN_EVENT_NAMES` 之外的名字直接跳过。
「解析失败不静默」（PROPOSAL §11 风险清单）针对的是**已知事件的畸形数据**：归一集内的事件
JSON 非对象/字段类型不对仍然抛 `UpstreamProtocolViolation`，不退回默认值。

### 3.2 错误分类（决定冷却时长，Q12=B）

```python
class ErrKind(StrEnum):
    PLAN = "plan"        # 权益耗尽（1005）→ 冷却 12h
    CREDIT = "credit"    # 余额不足（402/14018）→ 冷却到次日 04:00（等签到恢复）
    SOFT = "soft"        # 限流/404 → 冷却 60s，不累计错误数
    DEAD = "dead"        # session 失效 → 硬禁用 disabled=1
    OTHER = "other"      # 其他 4xx/5xx → 累计，连续 3 次 → 冷却 10m
    INVALID = "invalid"  # 请求无效（模型不存在等）→ 不冷却凭证，跳过该上游；全拒 → 400
    MODEL = "model"      # 该模型用量超限（429+6004）→ 只冷却 (凭证, 模型)，10m 起翻倍封顶 2h
    BLOCKED = "blocked"  # 该账号无此模型（400/404+11102）→ (凭证, 模型) 负缓存，6h 起翻倍封顶 24h
    REQUEST = "request"  # 请求级错误（11101/11115/11128/11135）→ 零动作：不冷却、不累计，仅换号
```

`MODEL_SCOPED_KINDS = {MODEL, BLOCKED}`：这两类只写 `credential_model_cooldowns`
（见 §6.1），不碰账号级 `cooling_until`，因此同账号的其他模型仍可选。反之账号级冷却
出现时会清空该凭证的模型级条目——否则「切模型」能绕过账号级限流。

| 上游信号 | CB | TRAE | ErrKind |
|---|---|---|---|
| 权益耗尽 | `code=1005`/plan 相关 | `"code":1005` | PLAN |
| 余额不足 | 402 / `code=14018` | 402 / `code=14018` | CREDIT |
| 模型级限流 | 429 + `code=6004` | 429 + `code=6004` | MODEL |
| 该账号无此模型 | 400/404 + `code=11102` | 400/404 + `code=11102` | BLOCKED |
| 请求级错误 | 400 + 11101/`Unmarshal chat params failed`/11115/11135 | — | REQUEST |
| 限流 | 429（无 6004） | 429（无 6004） | SOFT |
| 不存在 | 404 | 404 | SOFT |
| 会话失效 | 401/403 | 401 | DEAD |
| 服务端错误 | 5xx | 5xx | OTHER |
| 请求自身无效 | 400 | 400（含 4001） | INVALID |
| 流内错误事件 | SSE error 事件 | `event:error` 业务码 | 同上映射（`executor._event_kind`） |

> 400 + `11128`（`Illegal API invocation from an unapproved channel`）也归 REQUEST：实测主因是**内容风控**——`system`/`assistant` 消息正文出现「伪装其他厂商官方客户端」的指纹串时整单拒绝（已确认 3 条：Claude Code 系统提示的身份声明行、其 billing 头字段名、其 git 上下文行；完整清单见 `client.py` 的 `CHANNEL_MARKERS`，此处刻意不写原文以免污染阅读本文件的 agent 会话）。特征：与凭证无关（换号无效）、确定性复现、仅 `user`/`tool` 之外的这两个角色的 `content` 命中（`tool_calls` 参数与 reasoning 均不触发）；会话一旦把指纹写进历史，后续每轮（含压缩请求本身）都持续 11128。处理：出站前 `sanitize_channel_markers` 替换为占位符（`CODEBUDDY_SANITIZE_CHANNEL_MARKERS=false` 关闭），客户端历史不受影响；`content` 为文本块列表时逐块处理（2026-09-21 直证块形态同样触发）。曾误落 INVALID → 跳过上游全部凭证、零重试直接 400（`invalid_request`），是 `deepseek-v4.1-flash` / `glm-5.3-flash` 报「not available on any configured upstream」的根因。

业务码一律从 `"code": N` 键值形态提取（`provider.base.business_codes`），不搜裸数字：`"code":111020` 含 `11102` 子串，裸匹配会把无关响应判成模型错误。判定顺序即优先级，`429+6004` 必须先于 `429`、`402/14018` 必须先于状态码兜底（见 `classify_status` docstring）。

### 3.3 三态健康度（Q26/A）

```python
@dataclass(slots=True)
class Quota:
    remaining: float | None = None
    total: float | None = None
    cycle_end: int | None = None      # 最早到期 epoch（CB 多包各自独立）；TRAE None
    expiry_ladder: list[tuple[int, float]] | None = None  # [(到期 epoch, 该包剩余积分)]
    packages: list[dict] | None = None  # [{"name","total","used","end"}]，仅展示
    probed_at: int | None = None
    probe_failed: bool = False

type HealthScore = int | None        # 0-100 known；None unknown；-1 exhausted

def health(q: Quota | None) -> HealthScore:
    if q is None or q.probe_failed or q.total is None or q.total <= 0:
        return None if (q is None or q.probe_failed) else -1
    return max(0, min(100, round(q.remaining / q.total * 100)))
```

调度排序：`known DESC > unknown（中性参与）> exhausted(-1)`。

### 3.4 截断续写（B1.4，按实测收窄）

上游以 `finish_reason == "length"` 结束本轮流时，同凭证自动续写，最多 `AUTO_CONTINUE_MAX`（默认 10，0 关闭）。实现在 `src/engine/continuation.py` 的 `ContinuationStream`：包装上游事件流，截断则追加「已产出正文（含 reasoning）+ 续写指令」重发，并把输出上限两键归零（否则在同一处再次截断），跨轮累计 usage，末端补发一条累计 usage + 最后一轮真实 `finish_reason`。做成事件流包装器而非 executor 内重跑：凭证固定，轮换 / 记账 / 统计零改动。

**为什么不实现已批准计划里的其余三类判据**（2026-09-21 直连上游实测，36+ 请求，交错中性对照排除频率窗口假因）：

| 判据 | 实测结论 |
|---|---|
| 仅 reasoning 无正文 | 真实存在，但**只在客户端下发 `max_tokens` 时**出现：glm-5.3-flash `max_tokens` 8/24/64 → content=0 / reasoning=33/96/307，`finish_reason=length`。生产路径（PI 等）只发 `reasoning_effort`、不发任何 max 键，本网关也不做 max 键映射 → 该形态在生产不可达。凭空加启发式会把「模型就是想空回」误判成截断 |
| 空正文无工具调用 | 本机 5744 条统计 **0 例**，无证据 |
| 代码块未闭合 | 纯启发式，**无任何实测支撑**，不做 |

另两条实测事实（影响 `length` 可观测性）：`max_completion_tokens` 被 CB 上游**完全忽略**（`=1` 仍出 59 tokens）；`max_tokens` 才生效（精确截断 + `length`）；两键同发时后者胜出；TRAE 对两个键**都不生效**（80/80/80 字符）。CB 的 `usage.reasoning_tokens` 恒为 `None`（口径差异，见 §3.1）；`enable_thinking: false` 被上游**忽略**（仍产 reasoning 且计入 `max_tokens`）。

参考实现（IceeAn/codebuddy2api）只**统计** `finish_reason`、**不实现**续写，故本项无照搬蓝本，全部依据上述直连实测。

### 3.5 会话粘性键（B1.5）与模型元数据/黑名单（B1.6）

**会话粘性键**（`src/engine/affinity.py`，详见 [PROPOSAL.md 会话粘性](PROPOSAL.md)）：键优先级 `conversation_id` > `conversationId` > `prompt_cache_key`（同键名先 `metadata` 对象再请求体顶层）→ 无显式标识时回落「用户名 + 消息增量前缀指纹」；请求体带 `user_id`（顶层或 `metadata` 内）时不派生兜底键。键名核实：`prompt_cache_key` 是 OpenAI 官方顶层参数、`metadata.user_id` 是 Anthropic Messages API 官方字段；`conversation_id`/`conversationId`/顶层 `user_id` 非两家标准键，本机 71 份真实 dump（PI 客户端）**未观测到**，作为兼容探测接受（命中即用、未命中无害）。

**模型元数据**（B1.6，2026-09-21 两边模型配置接口实测）：两边上游都直接给出推理元数据，网关透传为 `/v1/models` 的 OpenAI 额外字段：

| 字段 | CB 来源 | TRAE 来源 | 实测取值 |
|---|---|---|---|
| `supports_reasoning` | `supportsReasoning` | `display_config.model_capability`（`reasoning_model`→true、`chat_model`→false），缺失回落 `reasoning_effort_config.support_thinking` | true / false / null |
| `default_effort` | `reasoning.effort` | 无对应字段（`reasoning_effort_config` 只有 `support_thinking` 布尔） | CB: `high` / `medium`；TRAE 恒 null |

`supported_efforts`（可接受档位集合）**不实现**：上游只给「默认档位」，不给档位清单，凭空枚举 ChatGPT 三档属编造（同 §3.4 原则）。CB 的 `onlyReasoning`/`canDisableThinking` 同样**不透传**——语义未经核实。逐字段补缺（双上游同名模型先到先填、后到只补 None）。

**黑名单默认值**（`MODEL_BLOCKLIST`，fnmatch glob，只影响列表展示、直连不受影响）：在原有 `custom_model_*` / `*sub*agent*` / `summary` / `browser_use_*` 之外，按实测补入三类**确认不可用**的噪音模型：

| 模式 | 命中实例 | 实测结论 |
|---|---|---|
| `default` | CB `default` | HTTP 200 但**零内容**（不可用于 chat） |
| `hunyuan-image-*` | CB `hunyuan-image-alpha`、`-edit` | HTTP 400 `11103`「Backend [hunyuan-stream] is not supported」 |
| `file_search_agent` | TRAE `file_search_agent` | HTTP 200 但错误 `3003`「model service is unavailable」+ 零内容（`*sub*agent*` 不含 "sub" 故漏网） |

**刻意不加**（推翻计划原拟噪音名，全部直连实测）：`*-volc`（`deepseek-v3-2-volc` 实测正常 chat）、`codewise-*` / `completion-*` / `*-lkeap`（两边清单零命中，无从核实）、`aquila` / `sagitta` / `seed-code-pro-0430`（TRAE 实测正常 chat，名字可疑但可用）。

**黑名单热更与列表缓存（B4 修正）**：`MODEL_BLOCKLIST` 是热更项，但 `list_models` 的 `model_list_cache` 原先存**过滤后**结果，导致改完最长要等 `MODEL_LIST_TTL_SECONDS`（300s）才反映到 Playground，且被滤模型在 TTL 内会从「上游失败兜底缓存」复活。现在缓存只存**未过滤原始表**，过滤在每个出口现做（`_visible()`，命中缓存 / 成功拉取 / 失败兜底三条路径都过一遍）；前端保存后显式 `invalidateQueries(["playground-models"])`，因此黑名单与列表两条缓存都立即生效，不依赖 TTL 过期。

### 3.6 活跃上报（B1.7，默认关闭）

CodeBuddy 成长中心的「连登天数 / 活跃地图」按日统计客户端对话事件；账号只被网关
自动调用时会断连登。`ACTIVITY_REPORT_ENABLED=true` 时 `src/tasks/activity.py` 每天在
`ACTIVITY_REPORT_HOUR`（默认 10 点，北京时间）整点窗口内，为每个上游账号补发**一条**
`chat_request_send`；按「endpoint + userId」隔离，当日成功即封账（内存态），失败下轮
重试。协议在 `src/provider/codebuddy/activity.py`。

实测（2026-09-21，直连 CN 上游，端到端点亮连登 1→2）：

| 项 | 实测结论 |
|---|---|
| 端点 | `POST {endpoint}/v2/report`（与聊天同基址 `copilot.tencent.com`），HTTP 200 `{"code":0,"msg":"OK"}` |
| 请求体 | **事件数组** `[chatRequestEvent]`（非单对象），`eventCode=chat_request_send`，全字段形状（36 键） |
| `userId` | **必填**。缺失时上游 HTTP 200 `code:0` 但**静默丢弃**（连登不变，实测复现） |
| userId 来源 | OAuth 凭证的 `user_id`/`account_uid` 实测**为空**（上游账号接口未回填），回落 bearer JWT 的 `sub`（36 位 UUID，实测有效）；取不到则跳过，绝不编造 |
| `X-User-Id` | 与 body `userId` 同时带上（body 是必要项；头补一份更稳） |
| 频率 | 每号每天 1 条即可，不做多时点高频上报 |

**刻意不做**：计划原拟的「三套客户端指纹」（CLI / 桌面 / Web+小程序）。实测现有
CLI 指纹（`build_headers`）即可被接受，无需切换身份；多套指纹只增加失败面。

**风险声明**：官方活动条款禁止模拟器/脚本篡改活动数据（处罚为取消资格并追回礼品）。
默认关闭；事件名与形状依赖上游实现，改版即失效，**不作为可靠性功能**。

### 3.7 Responses 出口（B2.1，Codex CLI）

`POST /v1/responses`：`src/api/responses.py`（端点）+ `src/compat/responses/request.py`
（入站映射）+ `src/compat/responses/response.py`（出站翻译）。与 `/v1/chat/completions`
**共用同一个 executor**，选号 / 冷却 / 轮换 / 记账 / 会话粘性全部零改动；引擎侧唯一
改动是 `Executor.stream(..., translator=...)` 注入缝——出口形状由 translator 决定，
引擎不再硬编码 chat 形状的 SSE 帧（`_stream_error_frame` 缺失时回落 chat 帧）。

字段形状**全部取自官方 `openai` Python SDK（3.x）由 OpenAI OpenAPI 生成的类型定义**，
并用该 SDK 作客户端做端到端契约验证（该 SDK 是 OpenAI 自己的解析实现，比手写断言权威）；
不凭记忆写。

**入站映射**（Responses → 内部 chat 载荷）：

| Responses | chat |
|---|---|
| `instructions` | 首条 `system` 消息 |
| `input` 字符串 | `user` 消息 |
| `input[].type=message`（`input_text`/`output_text` part） | `messages[]`；`developer` 角色改写为 `system` |
| `input[].type=function_call` | assistant 消息的 `tool_calls[]` |
| `input[].type=function_call_output` | `tool` 消息 |
| `input[].type=reasoning` | assistant 消息的 `reasoning_content`（仅明文部分） |
| `tools[].type=function`（**扁平** `{name, description, parameters}`） | 嵌套 `{type:function, function:{...}}` |
| `tool_choice` 字符串 / `{type:function,name}` | 同名 / 嵌套形式 |
| `max_output_tokens` | `max_tokens`（CB 上游只认这个键，§3.4） |
| `reasoning.effort` | `reasoning_effort` |
| `temperature` / `top_p` / `parallel_tool_calls` / `prompt_cache_key` / `text.verbosity` | 同名透传 |

**出站事件序列**（流式）：

```
response.created
response.output_item.added            (message / reasoning / function_call)
response.content_part.added           (output_text)
response.output_text.delta × N
response.output_text.done
response.content_part.done
response.output_item.done
response.completed | response.incomplete
```

思考内容用独立 `reasoning` item + `response.reasoning_summary_text.delta/.done`
（不混进正文）；工具调用用 `function_call` item + `response.function_call_arguments.delta/.done`。
**Responses 协议没有 `[DONE]` 哨兵**，终止事件本身就是流结束（`done_sent` 与之对齐，
供 executor 区分「正常收尾」与「中途断开」）。流式已完成后再来的 FINISH/ERROR 一律忽略，
不重复发终止事件。`finish_reason=length` → `response.incomplete`
（`incomplete_details.reason=max_output_tokens`），`content_filter` 同理；其余为 `completed`。
流内错误（executor 拦不到的那类，如全凭证拒绝模型）以 `response.failed` 表达，
并把错误码映射成 Codex CLI 认识的取值（`no_healthy_credential` → `server_is_overloaded`，
见 `codex-rs/codex-api/src/sse/responses.rs` 的分类表）。

**按实测收窄已批准计划**（2026-09-21 核 `openai/codex` 源码 + 官方 SDK 端到端）：

| 计划原文 | 实测 | 落地 |
|---|---|---|
| `include` → 400 | Codex CLI **每轮必带** `include=["reasoning.encrypted_content"]`（`codex-rs/core/src/client.rs`） | 400 会把主客户端直接打死：改为**接受并忽略**该值（本网关不产加密推理内容），其他 `include` 值才 400 |
| `previous_response_id` → 400 | Codex HTTP 路径**不发**该字段（`ResponsesApiRequest` 无此字段） | 保持 400（本网关无状态） |
| `store=true` → 400 | Codex 恒发 `store=false` | 保持 400（`false` 放行） |

**客户端取证结论**（`openai/codex`，非本机实测）：Codex CLI 按 SSE 的 `event:` 行（而非 `data.type`）分派事件，所以两个字段都必须发；`response.completed.response` 在它那边是**强类型解析**（`id` 必填、`usage` 含 `input_tokens`/`output_tokens`/`total_tokens`），解析失败即整轮报错——故 `response` 对象按官方必填字段完整发出。Codex 回传的历史里 `reasoning`/`compaction` 只有密文、没有 chat 等价物，**有意无损丢弃**（不是静默降级：不影响回答质量，仅不再回传），其余 Responses 私有 item 类型（`local_shell_call`/`custom_tool_call` 等）显式 400。

**验证状态**：本机无 Codex CLI、无 Responses 参考实现，故以官方 `openai` SDK（`responses.create(stream=True/False)`，含工具调用）作权威客户端跑通全部契约，并对**真实 CB 上游**冒烟（流式 / 非流式 / 工具调用三条，模型 `deepseek-v4-pro`），另用按 `codex-rs` 源码构造的真实请求体核对入站映射。**未经真实 Codex CLI 端到端验证**，剩余风险：客户端行为细节（如 reasoning item 无 `encrypted_content` 时的降级路径）。

---

### 3.8 运行时配置热更（B3.2，推翻 Q11「无设置页」）

**问题**：十余项运行期语义的配置（默认模型、到期窗口、节流区间、后台周期、活跃上报开关）此前只在 `build_app` 启动时读一次并烘焙进对象（`Scheduler(...)`、`Pacer(...)`、`growth_interval_minutes=...`），改一项要重启整个进程，管理台也没有任何入口。

**分层**（这是设计核心，不是实现细节）：

| 类别 | 例子 | 位置 | 变更方式 |
|---|---|---|---|
| 启动期不可变项 | `APP_SECRET` / `HOST` / `PORT` / `DATA_DIR` / `USERS_FILE` / `CODEBUDDY_ALLOWED_ENDPOINTS` | `config.Settings`（frozen） | 改 env + 重启；**不进白名单**，管理台改不了 |
| 运行时可覆盖项 | 见 `runtime_settings.HOT_SETTINGS`（13 项） | `RuntimeSettings` 覆盖层 | 管理台改，立即生效 |

启动期项拒绝热更的原因：它们决定进程如何启动（监听地址、加密密钥、上游白名单），运行期变更只会让「当前进程」与「磁盘配置」静默分叉，而分叉后的行为无法从任一处推断。

**生效优先级**：`runtime_settings` 表（DB 覆盖）> `.env`（默认值来源）。覆盖行只有被管理台改过的 key；「恢复默认」= 删除该行，不是写入 env 当前值。UI 与日志都必须明示「DB 覆盖 .env」，否则用户改 `.env` 不生效会当成 bug。

**读取机制**：`RuntimeSettings` 对热更 key 返回覆盖值，其余属性 `__getattr__` 透明委托给 `Settings`——调用方仍写 `settings.default_model`，不必感知覆盖层。覆盖值在内存缓存，写入后 `reload()` 刷新；读取路径不查库（每次选号/建请求都查库的开销远超热更省的收益）。

**消费端**：`config.live(value)` 把「标量或零参 callable」统一成取值器。生产装配传 `lambda: runtime.xxx`（每次读当前值），测试与一次性任务仍传标量。已接线的热更点：

- `Scheduler.expiry_window` / `secondary_expiry_window`：读取时跑 `expiry_windows()` 归一化（主窗口 ≤0 → 次窗口一并归零），避免只热更主窗口导致次窗口单独排序
- `ConversationAffinity.ttl_seconds`：存量条目按写入时的到期时刻失效，调小 TTL 不会让旧条目突然作废
- `Pacer.min/max_seconds`：校验从构造期挪到读取期（覆盖层把下限改到上限之上只能在用时拒绝），但构造时仍校验一次尽早失败
- `Executor`：`default_model` / `max_auto_continues` 每次请求现读
- `GrowthTask.allow_irreversible`：每轮现读
- `TaskRunner` 周期（探测/成长/刷新/清理）与活跃上报开关、时点：每轮 `sleep` 前现读
- 活跃上报改为**恒建对象**（构造成本为零），由 `TaskRunner._activity_enabled` callable 每轮 gate：只有对象先存在，管理台才能把默认关闭的它热开到不需要重启

**校验**：白名单外 key 直接拒绝；类型（`int`/`float`/`bool`/`str`）与范围（min/max）在写入前校验；跨字段组合（`pacer_min ≤ pacer_max`）用「这一批写完之后」的对端值比较，校验失败则整批不落库（半新半旧的组合比拒绝更糟）。表里的坏行（白名单外/类型非法）在读取时跳过并记警告——一行坏数据不能让服务起不来。

**接口**：`GET /api/settings`（admin）返回 `snapshot()`（`key`/`env_name`/`label`/`description`/`kind`/`value`/`default`/`overridden`/`task`）+ 覆盖计数；`PUT /api/settings`（admin + CSRF）body `{"values": {key: 标量 | null}}`，`null` 表示恢复默认。写操作记审计日志（谁改了哪些 key）。

`snapshot()` 里的 `task` 是**配置项 → 后台任务 key** 的归属（`HotSetting.task`，`null` = 网关/调度项）。它只用于管理台把配置归到任务卡片下（§3.12），不参与运行时语义——前端不硬编码 key 列表，后端加任务或改归属不需要改前端。

---

### 3.9 token 到期展示与预警（B3.3）

**问题**：凭证池里 access token 何时过期，管理台此前看不到；只有 token 真失效、上游回 401、凭证被硬禁用（`disabled=1`）才暴露成红色状态，而那时已无法接对话流量。`revive` 只清禁用标记、不补刷新，凭证无法自愈。

**到期时间的来源**（实测结论，决定实现，不能只读凭证字段）：

| 渠道 | 凭证 JSON 的 `expires_at` | 实际可靠来源 |
|---|---|---|
| TRAE | 有值（= JWT `exp`） | 凭证字段即可 |
| CodeBuddy | **恒为 0**：OAuth 登录与刷新响应都不带 `expires_at`/`created_at`/`expires_in` | 只能从 bearer token 的 JWT `exp` 解析 |

三个 CodeBuddy 凭证实测 `expires_at` 全为 0，而其 bearer token 都是 JWT、带权威 `exp`。严格只读凭证字段，预警对**全部** CodeBuddy 凭证名存实亡；更糟的是 `CodeBuddyCredential.needs_refresh` 首行要求 `expires_at > 0`，于是 CodeBuddy 的 token **从来不预刷新**——这正是本批顺带修掉的真实故障。

**实现**：`provider/token_expiry.py`（渠道中立，刻意不 import provider 子模块，否则 `provider.codebuddy.events → engine.sse` 会被拖进 `db` 层）：

- `credential_token_times(data)`：返回 `(签发, 到期)` 两个 epoch。到期优先显式 `expires_at`/`expiresAt`，缺失 / 非法时遍历可能的 token 键（`bearer_token`/`accessToken`/…）解析 JWT `exp`；签发时间只来自 JWT `iat`（上游不会单独回传）。各自拿不到时返回 **0 = 未知**。**不猜本地 TTL**——捏造的到期时间会让管理台显示假预警，比不显示更糟；拿回填时刻冒充 `iat` 同样不行（那是「我们何时写的」，不是「上游何时签发的」）。
- `credential_expiry(data)`：上面的到期分量（预刷新判定与兼容入口）。
- `jwt_times(token)` / `jwt_expiry(token)`：只 base64url 解码不验签（签名由上游校验，这里仅用于展示与预刷新判定）；非 JWT / 结构异常 / claim 非法一律 0。
- `normalize_epoch()`：毫秒时间戳归一（TRAE 原先的私有 `_normalize_epoch` 收敛到这里，两渠道共用）。

**落库**：`credentials` 新增两列（`SCHEMA_VERSION` 10 → 11，走 `_MIGRATION_COLUMNS`）：

- `token_expires_at`：`add()` 与 `save_credential_data()` 都写回，值来自 `credential_token_times()`。**JWT 派生值不写回凭证 JSON**——否则刷新换到新 token 后旧派生值会残留成「权威」到期时间。
- `token_issued_at`：access token 签发 epoch（JWT `iat`，即「最后续期」），仅落库供诊断、不在列表展示。拿不到时写 0。

**老库升级不批量回填**：全池解密会拖慢启动，改在列表读到时按需从 `data_enc` 派生（`_token_times_from_blob()`，解密 / 解析失败返回 `(0, 0)` 而不让整个列表崩掉）；一旦写回就只读列值（`NULL` = 老库未回填 → 派生；`0` = 已确认未知 → 不重复解密）。

**读路径**：`list_all()` 只下发绝对 epoch，**不代前端算剩余秒数**——服务端算好的「剩余」不会随页面 tick 更新，还会与冷却时长各用一套口径。剩余时间与预警判定由前端 `tokenExpiryView()` 用同一个时钟现算，阈值由 `TOKEN_EXPIRY_WARNING_SECONDS` 经列表接口下发。

**进度条已移除**（原设计满量程取 `exp - iat`）：两渠道 token 寿命相差近四倍（CodeBuddy 55 天、TRAE 14 天），同一根条没有可比性；「还剩多久」已回答调度关心的唯一问题。`tokenExpiryView()` 仍返回 `percent`（接口未变、测试仍覆盖），只是展示层不再使用。

**展示纪律**：只给「剩余时间」一个数。曾同时展示「最后续期」（JWT `iat`），但那要求读者拿两个数做二次推理（刚续期 vs 没人管），属解释性信息；`iat` 仍落库供诊断。到期未知（0）时单元格显示 `—`，**绝不当成已过期**。

**接口**：`GET /api/credentials` 响应新增 `token_expiry_warning_seconds`；每条凭证新增 `token_expires_at` 与 `token_issued_at`（均为 0 表示未知）。

### 3.10 积分变动流水（B3.4）

**要解决的问题**：签到 / 成长中心 / 对话都会改余额，但上游这些接口**不打日志**，拿不到「这次动作加了多少分」。管理台此前只有「当前剩余」一张快照。

**做法：在额度探测写回时比对余额，只增记一条**（`credit_events`）。写入刻意放在 `save_quota` 的**同一个事务**里：分两次读改写会与并发探测交错，记出「before 是别人写过的值」的错行。

**归因纪律（本批最重要的取舍）**：diff 只能看到 `[上次探测, 本次探测]` 区间的**净变化**，其间签到、成长领取与对话消耗可能同时发生，无法区分谁贡献了多少。所以 `source` 列**只表达归因已知度**，不写来源枚举：`observed` = 两次探测之间的净变化（delta 可正可负或 NULL）；`sync` = 首次建立基线、没有对照（delta 为 NULL，不是「+0」）。前端文案一律说「净变化」，不写「签到 +5」——那等于把猜测当事实。

**只记有信息量的行**（否则表被噪声淹没）：

| 情形 | 是否记 | 理由 |
|---|---|---|
| 首次探测（无对照） | 记（`sync`），`delta` 为 NULL | 「从无到有」不是一次真实的积分变动 |
| 余额未变 | **不记** | 否则每轮探测落一行 0 |
| 余额变化 | 记（`observed`），`delta = after - before` | 正负都如实记 |
| 任一端未知 | 记，`delta` 为 NULL | 「余额变未知」本身是该追的异常，绝不量化成 0 |
| 两端都未知 | **不记** | 什么也没学到 |
| 凭证被并发删除 | **不记** | 避免悬挂行 |

`window_start` 记的是**上次探测时刻**（不是「上次有变动」）：中间那次没变化但同样观测过，区间必须从最近一次观测算起，否则会把一段无人观测的时间也算进去。

**接口**：`GET /api/credentials/{id}/credit-events?limit=20`（管理员；未知凭证 400 而不是空列表，避免拼错 id 时看起来「没有记录」）。前端入口是凭证**额度**列数字后的下箭头，展开为该行内独立的「积分记录」抽屉。

**保留**：`RetentionTask` 按 `usage_events` 同一保留期（90 天）回收，报告里体现为 `purged_credit_events`。

---

### 3.11 池健康与多 Key 出口（B3.5）

**`GET /healthz`**：返回 `{status, service, version, credentials:{total, ready, cooling, paused, disabled}}`，无鉴权；`GET /health` 保留为纯存活探针。分工明确——存活探针回答「进程还在吗」，`/healthz` 额外回答「凭证池还能用吗」，而 `ready=0` 是「活着但用不了」的状态，存活探针看不出来，需在监控侧单独告警。

计数**复用调度器的 `Candidate.is_selectable`**（`CredentialRepository.pool_counts`），不另写 SQL——两套口径一旦分叉，健康检查会报出与实际选号不符的「可用数」。五类互斥、合计 = `total`，判定优先级 `disabled → paused → cooling → ready`（系统禁用 / 用户暂停优先于冷却）。计划原文只列 4 类，补 `paused` 是因为项目已明确区分「系统禁用」与「用户暂停」（Q33/B3.1），少一类计数就对不上。

**多 Key 出口**：`api_keys` 增两列（`SCHEMA_VERSION` 12→13）：

| 列 | 取值 | 空值语义 |
|---|---|---|
| `provider_binding` | `codebuddy` / `trae` | 空 = 自动（跨渠道选健康凭证，原行为） |
| `allowed_ips` | 逗号分隔 IP/CIDR | 空 = 不限制来源 IP |

来源 IP 判定在 `deps.api_key_user`（鉴权**当场**判掉，不往上传递）：

- **默认不采信 `X-Forwarded-For`**：该头由客户端可写，信它等于白名单形同虚设。只有 `TRUST_PROXY=true` 才采信，且取**最后一个**条目——`$proxy_add_x_forwarded_for` 语义下那是紧邻的受信代理实际看到的地址，第一个条目是客户端自己写的。故该开关只适用于「本服务前恰好一层受信反代」，多层或直连必须保持关闭。
- 策略是纯函数（`auth/access.py`，不依赖框架）：写入时用 `normalize_allowed_ips` 校验并规范化（`10.0.0.1` → `10.0.0.1/32`），非法值 400；读取路径宽松解析，脏条目丢弃，整份白名单一条都解析不出则**拒绝**（fail closed，不因脏数据敞开）。
- 白名单命中失败返回 **403**（`forbidden`）——Key 本身有效，是来源不被允许；与 401「凭证无效」区分，便于调用方排查。
- `api_key_user` 由「返回用户名」升级为返回 `ApiKeyPrincipal(username, key_id, provider_binding)`（4 处出口调用同步调整）。`ApiKeyRepository.verify` 保留为只回用户名的薄封装。

**渠道绑定的执行**在 `Executor._apply_binding`（`resolve_target` 内），与 `@provider` 强制指定共用收窄路径：

- 绑定把候选固定为该渠道并置 `forced`（跳过模型目录收窄，归属已在此判定）。
- 模型目录能证明模型属于别家渠道 → 400 并给出实际归属；这比落到「无可用凭证」503 更准确——前者是「你请求错了」，后者读起来像服务坏了。
- 请求里 `@provider` 与绑定冲突 → 400，绝不静默改道。
- 目录未就绪（拉取失败）时不做归属判断，保守放行给下游选号，避免用缓存外信息误拒。
- 流式同样生效：`preflight` 带绑定，冲突在 200 响应头发出前就 400。

**不做**每 Key 配额 / 多租户（与 Q10「不做配额」冲突）。

---

### 3.12 后台任务可视化（B4，「任务与配置」页）

**问题**：`TaskRunner` 跑着 6 类循环（额度探测 / token 预刷新 / 每日签到 / 成长中心 / 活跃上报 / 明细清理），但除失败时一行 `logger.warning`，没有任何地方能看到「上次何时跑的、结果如何」，也没有端点暴露。运维只能翻 launchd 日志。

**上一轮调研结论**：项目内**不存在**后台任务页（无 `TasksPage`、无 `/api/tasks`，git 全历史与文档均无），所以这不是「找回旧页面」而是新增；同类项目（ithtelab/workbuddy-manager）的做法是「任务记录页 + 30s 自动刷新 + 单次 200 条上限」，关键教训是**容器重建即丢、必须采集落库**。

**本项目的选择：只做进程内运行态，不落库**（PROPOSAL Q38）：任务状态回答的是「**这次进程活着的时候**谁跑过、结果如何」，重启本身意味着任务刚被重新调度，显示「本次启动以来未运行」比捞出一条重启前旧记录更诚实；落库要新增表 + 保留期清理 + 老库迁移，而跨重启的历史价值有限（业务留痕已有 `growth_events` / `credit_events` / `usage_events`）。代价明确写进 UI：卡片只显示本进程内的运行，不承诺「历史记录」。

**记录口径（核心语义，容易退化）**：`_guarded` 在任务返回 `None` 时**不入账**。`sleep` 到点但 `due()=False`（签到当天已签、活跃上报未到窗口 / 未开启）是 no-op，若当成一次成功执行，页面会显示「签到 3 分钟前刚跑过」——而当天其实**一次都没签**。因此 `TaskRun` 只在真实执行（返回非 `None`）或抛异常时写入；异常也入账（`last_error`），否则「一直在失败」会被显示成「尚未执行」。计数 `runs` 同样只涨真实执行。

**任务清单与归属**：`tasks/status.py` 的 `TASK_SPECS` 是静态描述（key / 名称 / 一句话说明），与 `TaskRunner.start()` 建立的循环一一对应；周期与开关**运行时现算**（`TaskRunner.task_status()` 读热更值），不是装配快照——改完配置刷新页面就该看到新周期。未装配的任务（测试或降级时不传 growth/activity）不出现在清单里，避免展示「永远不跑」的卡片。

配置项一侧用 `HotSetting.task`（§3.8）表达归属，前端据此把配置塞进对应任务卡片；无归属（`default_model` / 黑名单 / 到期窗口 / 粘性 / 两个 pacer / CB 聊天间隔）归入「网关与调度」区。两处通过 `TASK_BY_KEY` 交叉校验（测试保证 `task` 指向真实任务 key）。

**接口**：`GET /api/tasks`（admin）返回 `{tasks: [...], server_time}`。每条含 `key`/`name`/`description`/`interval_seconds`/`enabled`/`runs`/`last_started_at`/`last_finished_at`/`last_ok`/`last_report`/`last_error`。带 `server_time` 是为了让前端用**服务端时钟**算「距今多久」——浏览器时钟偏移会把刚跑完的任务显示成几小时前。`app.state.task_runner` 不存在时（未进 lifespan）返回空列表而不是 500。

**前端**：导航与页头从「运行时配置」改为「任务与配置」，`SettingsPage` 重组为任务卡片区（运行态 + 该任务的配置项，复用 `SettingRow`）+ 网关区；`useTasks` 以 `refetchInterval: 30_000` 自动刷新（运行态是随时间变化的观测量，手动刷新会让人以为任务停了），`/api/settings` 不自动刷新（配置改动由用户触发）。

---

## 4. Provider 协议（Q16=A 细接口）

```python
class Provider(Protocol):
    id: ClassVar[str]

    # 凭证生命周期（上游协议私有，必然在 provider 内）
    def start_auth(self, callback_url: str) -> AuthSession: ...   # flow=poll|callback
    def poll_auth(self, state: str) -> AuthResult | None: ...     # 仅 poll 轨道
    def complete_callback(self, raw_url: str, state: str) -> AuthResult: ...  # 仅 TRAE
    def import_credential(self, raw: dict) -> dict: ...
    def refresh(self, credential_data: dict) -> dict: ...

    # 额度探测（调度器依赖：健康度 + 到期阶梯）
    async def probe_quota(self, credential_data: dict) -> Quota: ...

    # 执行与分类
    async def stream_chat(self, credential_data: dict, payload: dict, model: str) -> AsyncIterator[Event]: ...
    def classify(self, status: int, body: bytes) -> ErrKind: ...
    def list_models(self, credential_data: dict) -> list[Model]: ...
```

约定：
- 凭证在引擎与 provider 之间以 dict 传递（解密后的 `data_enc`）；`credential_from`
  等能力把 dict 还原为 provider 私有 dataclass，provider 不接触 sqlite
- `stream_chat` 只产出 `Event`，产出前先做 HTTP 状态码检查；`classify` 由 executor 调用
- 可选能力**不实现即不定义**（不是抛 `NotImplementedError`）：调用方用
  `getattr`/`hasattr` 探测，缺失时返回 400「该凭证不支持此操作」，而不是 500。
  当前可选集：`start_auth`/`poll_auth`（仅支持 poll 的 provider 才有）、
  `complete_callback`（仅 TRAE）、`list_accounts`/`switch_account`（仅 CodeBuddy）、
  `credential_from`/`checkin_scope`（刷新与签到任务的能力探测）、
  `checkin`/`checkin_status`（签到）、`growth`（成长中心，仅 CodeBuddy）、`host`（展示用）
- 两个 provider 共用 `engine/sse.py` 的帧解析器（SSE 规范层），事件语义各自映射

---

## 5. 请求时序（chat completions 主链路）

`POST /v1/responses` 与本节同链路：`responses/request.py` 先把 Responses 请求体映射成等价的 `ChatRequest`（`raw` 为 chat 形状），其后选号 / 轮换 / 记账 / 粘性完全一致；出口侧由注入的 translator 决定 SSE 形状（§3.7）。

```
客户端 → POST /v1/chat/completions (Bearer sk-)
  1. deps.require_api_key：摘要查 api_keys 表 → username
  2. request.py：校验 body → ChatRequest；model_resolver 解析候选集
     - "glm-5.2" → 两 provider 都可能；"glm-5.2@trae" → 仅 trae；auto/空 → DEFAULT_MODEL
  3. 选号（executor._select → scheduler.select）：
     a. 候选 = 注册表中支持该模型的 provider（目录能证明归属时先收窄，_narrow_providers）
     b. 模型级冷却过滤：逐凭证按**自己所属上游的原始模型名**查 (凭证, 模型) 冷却表，
        被限流/负缓存的本次跳过（同账号其他模型不受影响，见 §6.1）
     c. 会话粘性：存在可选的 pinned 凭证时跳过（pin 优先），否则粘性命中且仍可选的
        凭证直接复用、不参与排序。粘性键优先取显式会话标识
        （conversation_id/conversationId/prompt_cache_key，metadata 或顶层），
        无则回落消息前缀指纹；带 user_id 时不派生前缀兜底键（B1.5）
     d. 过滤 healthy（enabled=1, disabled=0, 非冷却中）。enabled=0（管理台「暂停」）
        只作用于本条对话路径：后台任务只检查 disabled，暂停期间照常运行
     e. 到期积分排序（两级字典序）：quota_expiry_ladder 中「距到期 ≤ 主窗口」
        （QUOTA_EXPIRY_WINDOW_SECONDS，默认 36h）的积分加总，多的先用；打平再比
        次窗口（默认 7 天）；无周期信息（TRAE/企业版）计 0；主窗口 ≤0 时次窗口
        一并失效（expiry_windows() 统一折算）
     f. 两级到期积分都相同时按 health 三态取最高分；同分按 credential_id 稳定
  4. executor：解密凭证 → provider.stream_chat()
     - 上游 HTTP ≥400 → classify → scheduler.note_error → tried 加入 → 回到 3（最多 3 次）
     - 流内 Event.ERROR → 同上映射 → 注入 OpenAI SSE 错误帧 + 冷却 + 轮换
     - 上游 400（INVALID，如模型不存在）不冷却凭证，跳过该上游全部凭证；
       全拒时 400 invalid_request（未知模型名 ≡ 无上游提供，不走 503）
     - REQUEST 类（11101/11115/11128/11135）换号但不落库：既不冷却也不累计
       （累计到阈值同样会熔断），否则会顺手把已有的 cooling_until 写成 NULL
  5. response.py：Event → OpenAI chunk（流式）或聚合（非流式）
     - 首块补 role:assistant；上游无 index 的 tool_calls 补稳定 index
  6. stats.collector：写 usage_events（username/provider/model/tokens/latency/ttfb/ok）
  7. scheduler.note_success：清 err_count，并把本对话重新粘到实际服务的凭证
```

客户端断连：生成器被关闭/取消时统计 `error_type=client_disconnect`，关闭上游流；例外：`[DONE]` 已产出后的收尾断开（客户端拿到回调即关连接，框架在结束帧 `more_body=False` 前有一拍竞态会把它判为断开）按成功记账，避免误标。

---

## 6. 调度器规格（Q12=B + Q26 + Q31）

```python
class Scheduler:
    MAX_ROTATE = 3
    EXPIRY_WINDOW = 36h          # 主到期排序窗口，QUOTA_EXPIRY_WINDOW_SECONDS 覆盖；≤0 关闭整套
    SECONDARY_EXPIRY_WINDOW = 7d # 次到期排序窗口（主窗口打平时才比较），QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS 覆盖
    COOLDOWN = {ErrKind.PLAN: 12h, ErrKind.SOFT: 60s, ErrKind.OTHER: 10m}
    ERR_THRESHOLD = 3          # 连续 OTHER 错误 → 冷却

    def select(self, candidates, tried: set[str], now: int) -> str | None: ...
    # candidates: Iterable[Candidate]（引擎层已按模型收窄）；返回 credential_id
    def note_success(self, cred_id: str) -> None: ...
    def note_error(self, cred_id: str, kind: ErrKind) -> None: ...
    def pin(self, credential_id: str | None) -> None: ...
```

状态全部落 `credentials` 表（`cooling_until` / `err_count` / `health` / `disabled` / `quota_expiry_ladder`），进程重启不丢冷却状态。写路径无应用层锁：并发写靠 SQLite WAL + `busy_timeout=5000` 串行化；每个写方法走 `Database.transaction()` 上下文（正常提交、异常回滚），不再散落 `connect()/commit()` 样板。

到期积分只算一处：`expiring_credits()`。选号走 `Candidate.expiry_credits()`（主/次两级窗口各调一次），管理台列表走 `GET /api/credentials` 的 `quota_expiring_credits` 与 `quota_expiring_credits_secondary`（两个窗口值随响应返回 `expiry_window_seconds` / `expiry_secondary_window_seconds`），两处共用同一实现与同一个 `expiry_windows()` 折算（主窗口 ≤0 时两级一起失效），界面数字与选号顺序不会漂移；渠道无到期信息时返回 `null`（不显示），窗口关闭或确实无积分临近过期时返回 `0`（同样不显示）。管理台只在主窗口无数字时才渲染次窗口那一行（次窗口是主窗口的超集，主窗口有值时重复展示没有信息量）。

### 6.1 模型级冷却（B1.1）

`credential_model_cooldowns(credential_id, model, cooling_until, hits, reason)` 按 **(凭证, 模型)** 独立建表——账号级 `cooling_until` 放不下「同账号其他模型仍可用」这层语义。

- 登记的名字是**该凭证所属上游的原始模型名**：每个 provider 各自把归一模型名映射成自己注册的原始 id（`_upstream_model(provider_id, model)`，未知则原样），因为 CB 与 TRAE 的注册名大小写变体不同，用归一名会漏判
- 选号路径：`executor._select` 逐凭证过滤 `c.is_selectable(now, 该凭证的模型名)`，再交给 `Scheduler.select`；这里同时完成模型收窄、粘性命中与排序兜底（`_sticky(...) or scheduler.select(...)`）
- 退避：`MODEL` 基数 10m 起翻倍、封顶 2h；`BLOCKED` 6h 起翻倍、封顶 24h。换 reason 重新计数（两者基数与封顶不同，沿用对方 hits 会得到第三种时长）
- 清除：`save_success(..., model=)` 只删 `reason='blocked'`（模型限流按上游重置，成功一次不代表限制解除）；`revive` / 删除凭证 / 账号级冷却出现都清模型条目
- 回流：留存任务每轮 `purge_expired_model_cooldowns()` 回收过期行；管理台列表只下发未过期条目（`model_cooldowns`）
- 无模型名可归因时（流内事件未带 model）退化为账号级 SOFT，不写孤儿记录

---

### 6.2 后台任务（tasks/）

`TaskRunner`（runner.py）每类任务一个独立 asyncio 循环，失败只记日志不拖垮服务；间隔有安全下限，避免打爆上游。所有对外 HTTP 请求经 `pacer.py` 全局节流。

每个任务最近一次**真实执行**（时间 / 结果 / 错误 / 轮数）记在进程内（`status.py` 的 `TaskStatusStore`），由 `GET /api/tasks` 下发给管理台「任务与配置」页；no-op 轮次不入账，重启归零——见 §3.12。

| 任务 | 周期 | 行为 |
|---|---|---|
| 额度探测（quota_probe.py） | 启动立即一轮（不节流）+ 每 `QUOTA_PROBE_MINUTES`（默认 60）分钟 | 探测剩余额度 → 写 `credentials.quota_*` / `quota_expiry_ladder` / `quota_packages` / `health` |
| token 到期（token_expiry.py） | —（读路径，非任务） | 从显式 `expires_at` 或 JWT `exp` 派生到期时间，写 `credentials.token_expires_at`（§3.9） |
| token 预刷新（refresh.py） | 每 60 分钟 | 到期前 `REFRESH_SKEW_HOURS`（默认 24h）窗口内轮换 refresh token；到期时间同上（CB 实测无显式字段） |
| 每日签到（checkin.py） | 每 10 分钟（全天） | 成功即封账该凭证当日（`日期:scope`，进程内内存态）；失败持续重试 |
| 成长中心（growth.py） | 每 `GROWTH_INTERVAL_MINUTES`（默认 60，下限 5） | 仅 CodeBuddy：8 类领取；结果落 `growth_events` + 回写 `credentials.growth_last_result` |
| 活跃上报（activity.py，默认关闭） | 每 10 分钟醒一次，仅 `ACTIVITY_REPORT_HOUR`（默认 10 点，北京时间）窗口内执行 | 仅 CodeBuddy：补发一条 `chat_request_send` 续连登；按「endpoint + userId」隔离、当日封账；成功落一行 `growth_events` |
| 明细清理（retention.py） | 每 5 分钟 | `usage_events` 全量重算小时汇总（幂等 upsert，与 record 的增量双写对账）+ 90 天前明细清理；同期限回收 `credit_events`（§3.10） |

**签到 / 成长中心的「同账号」隔离键**：`checkin_scope(data) or f"credential|{credential_id}"`。provider 在身份未知时返回空串（CB 的 `checkin_scope_key` 在 `account_uid` 与 `user_id` 都为空时返回 `""`），任务层必须回落到 `credential_id`。这不是保守取值：共享空 scope 会让第二个账号被 `seen` 集合永久跳过，表现为「只有第一个凭证被自动签到」且没有任何报错；回落到凭证 ID 最坏只是多签一次（上游签到幂等，返回 ALREADY）。

**成长中心的失败判定**（三层分开，勿合并）：

- 协议层（`growth.py`）：非 2xx 抛 `GrowthRejected`；业务码非 0 / 缺 data / 非 JSON 抛 `UpstreamProtocolViolation`（HTTP 200 也可能是失败，只看状态码会把失败当成功）
- 编排层（`growth_runner.py`）：4xx 业务规则（名额用完、未解锁、抽奖没次数、能量不足）记 `IDLE` 而非 `FAILED`；5xx 与协议违规记 `FAILED`；401/403 立即置 `session_dead` 并停止后续请求（再打只会一路 401）
- 结论：`failed 且 gained=False` 才算整体失败。部分成功仍是成功——某个接口抖动不该让「今天领到 300 积分」变成红牌，否则定时任务天天报红，真故障被淹没

**接单失败要分三类，不能一律记 FAILED**（实测一个新账号报告刷出 17 条 `prerequisite not met: first_buddy`，把「其实只需做一件事」淹没了）：

| 上游返回 | 处置 |
|---|---|
| `prerequisite not met: <task_code>` | 前置任务未完成。**按原因归并成一条**并标 `reportable=True`（用户需知道去做什么），记 IDLE 不算失败 |
| `task does not require acceptance` | 正常应答（该任务不需接单），**不产生任何步骤** |
| 其余 | 逐条记 FAILED（真需要人看的失败） |

`GrowthStep.reportable` 控制该步是否进「一行汇报」：默认只收 DONE/FAILED，但 IDLE 里若有**用户需动手**的事项（前置任务受阻、尚无 Buddy）必须显式置 True——否则用户只看到「接单完成 共 1 个」，看不到还有 17 个被门住。同理 `no active buddy`（400）是账号状态而非故障，记 IDLE。

**任务契约是五态，不是三态**（2026-09 桌面端 `growthSpace` chunk 读出，被上游改版坑过一次，勿按直觉回退）：

| accept_status | 该做什么 |
|---|---|
| `not_accepted` | **接单**（进度从这一刻才开始计） |
| `accepted` / `in_progress` | 什么都不做（等用户完成） |
| `completed` | **领奖** |
| `claimed` | 跳过 |

- 接单：`POST /tasks/accept`，body 是**复数数组** `{"task_codes": [...]}`；旧的单数 `{"task_code": x}` 在新服务端一律 400。逐条结果在 `data.results`，失败（如 `prerequisite not met: first_buddy`）必须上报
- 领奖：`POST /tasks/{task_code}/claim`（body 空），**不再走 accept**；回包 `already_claimed` 为真时不重复计分
- 把 `not_accepted` 当成「已接单」跳过，会让所有新任务永远既不接单也不领奖（实测一个账号积压 5 个任务共 650 积分未领）

**连登兑换的 `tier` 是档位标识**（`"7d"` / `"14d"` / `"28d"`），权威来源是 `GET /streak` 的 `redemption_status.tiers[].tier`。传天数或档位名分别得 `invalid request` / `unknown tier`。实发字段是 `*_granted`（`credit_granted` / `energy_granted` …），读裸 `credit` 恒为空、会漏计全部兑换所得。

**403 不总是登录失效**：未解锁档位返回 403 + 「连续登录天数不足」，必须先于 session 判定处理，否则整轮成长中心会被误报成「登录态已失效」并中止。只有 401/403 且不是「天数不足」才置 `session_dead`。

不可逆动作（抽奖 / 连登兑换 / 开 Buddy 盲盒 / 消耗补登卡）由 `GROWTH_IRREVERSIBLE_ACTIONS` 总开关控制，**手动入口与定时任务读同一个开关**——否则「保守部署」只挡得住定时任务。开关只挡「消耗」，不挡「查询」：`/streak` 是只读接口，关掉开关时仍要打（否则连签展示会静默变成 `null`）。

**两个 `streak_days` 不是同一个数**（实测同一天分别是 5 与 1），不要合并展示：

| 来源 | 字段 | 含义 |
|---|---|---|
| 签到接口 `checkin-activity-status` | `streak_days` | 每日签到连签（有 `checkin_dates` 逐日佐证，语义确定） |
| 成长中心 `/v2/activity/growth/streak` | `streak.days` | 连登天数：`客户端使用天数 + 1`（含 1 天容忍窗口），每月清零 |

官方规则（桌面端 `GrowthSpace` chunk）：连登每月清零，含 1 天容忍窗口；兑换需手动发起，每档每月限兑 1 次且不消耗连登天数；断登可用补登卡补救。汇报文案用官方术语「连登 N 天」以区别签到接口的「连签」。

**什么算「使用」——有实测数据，别再凭单次实验下结论**：

| 日期 | 代理聊天转发 | 成长中心领取 | `score` |
|---|---|---|---|
| 09-18 | 360 + 253 次 | 未部署 | **0** |
| 09-19 | 391 + 202 次 | 有（21:32 起） | **5** |
| 09-20 | 110 + 227 次 | 有 | 0 / 2（另有桌面端对话） |

两天都没有桌面端使用，唯一差别是「有没有成长中心领取动作」——**活动类接口操作计入活跃**；而单纯走 `/v2/chat/completions` 发对话不计入（实测 3 次完整对话跑到 `[DONE]`，`today.score` 与 `updated_at` 都不动）。本项目一度写死「只计官方客户端、代理一律不计」，那是**拿单次实验推广出的错误结论**，已更正。

**边界**：活动类操作（领取奖励、兑换、开盲盒）可由本项目管理；依赖**产品内真实功能使用**的任务（如 `first_buddy` = 「在客户端新建任务并发起对话」）必须在官方客户端完成——这不是接口限制，是任务定义本身要求的行为。

**活跃地图（热力墙）分档**（`GrowthSpace` 的 `Ae()` 与 `zt[]`）：

| score | 档位 | 文案 |
|---|---|---|
| `<= 0` | 无活跃 | 尚未登场 |
| `1–10` | 轻度 | 轻轻路过 |
| `11–30` | 中度 | 持续输出 |
| `31–60` | 高效 | 效率拉满 |
| `> 60` | 极高 | 卷王模式 |

数据**每日凌晨 02:00 更新**（缓存里看到的 23:15/23:18 是写入时间，不是批算时刻；`today` 字段实时）。`score` 由活动类操作与客户端使用共同驱动，口径未公开；**与积分无关，不参与调度决策**——不要为刷它伪造客户端行为（H5 条款明禁，处罚为取消资格并追回已发礼品）。

**清理切点必顶对齐到小时边界**（`purge_expired`）。这不是保守取值而是正确性要求：`rollup_hourly` 对整行是 REPLACE 语义，只有保证「仍有明细的小时保有全部明细」重算才精确；若边界小时只删一半，下一轮 rollup 会把汇总行覆盖成剩下那一半，被删部分永久丢失（明细已不在）。代价：明细最多多留 1 小时。

---

### 6.3 面向用户的错误语义

管理端点返回的失败原因必须是**稳定的机器可读枚举**，不能是 Python 异常类名。
类名是实现细节：用户既判断不出问题，也不知道下一步做什么，而且重构时会漂移。

`POST /api/credentials/{id}/probe` 的失败响应：

```json
{ "probed": false, "reason": "credential_rejected", "detail": "upstream http 401" }
```

| `reason` | 含义 | 用户该做什么 |
|---|---|---|
| `credential_rejected` | 上游 401/403 拒绝凭证 | 重新登录该账号 |
| `rate_limited` | 上游 429 | 稍后重试 |
| `upstream_unavailable` | 上游 5xx | 等待上游恢复，与本账号无关 |
| `upstream_rejected` | 其他 4xx | 检查账号状态 |
| `upstream_response_invalid` | 响应结构不符 | 可能是官方接口变更 |
| `upstream_timeout` | 请求超时 | 重试 |
| `unknown_error` | 未归类 | 查 `detail` |

`detail` 保留原始错误摘要**仅供排查**，界面不得把它当作主提示展示。
前端 `probeFailureLabel()` 负责把 `reason` 翻成中文，并对未知值兜底。

未识别的 `reason` 必须回退到 `unknown_error`，不得透传原始字符串。

## 7. 数据库（T-Q2 定稿）

DDL 以 `src/db/schema.sql` 为准（users.txt 为用户唯一源、无 users 表、凭证加密列、`usage_events.credit`/`cached_tokens` 可空）。当前 `SCHEMA_VERSION = 13`，共 8 张表：`api_keys` / `credentials` / `usage_events` / `usage_hourly` / `growth_events` / `credit_events` / `credential_model_cooldowns` / `runtime_settings`。补充实现细节：

```sql
-- conn.py 打开时执行
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;      -- api_keys 之外无外键（users.txt 无表）
```

- 连接：`threading.local()` 每线程一个 `sqlite3.Connection(row_factory=sqlite3.Row)`，引擎与 FastAPI 线程池各持自己的连接。写入统一走 `Database.transaction()`（`commit()` / 异常 `rollback()`），无应用层写锁，并发由 SQLite 串行化（WAL + `busy_timeout=5000` 下短写足够）
- 加密：`Fernet(base64.urlsafe_b64encode(sha256(APP_SECRET).digest()))`；APP_SECRET 丢失 = 凭证全部不可解，只能重录
- migration：启动时读 `schema.sql` 逐条 `CREATE TABLE IF NOT EXISTS`（只加不改）；新增列写进 `migrate._MIGRATION_COLUMNS` 走 `ALTER TABLE ... ADD COLUMN`（重复列名忽略，老库幂等补列），删表写进 `migrate._MIGRATION_DROPS` 走 `DROP TABLE IF EXISTS`（`CREATE TABLE IF NOT EXISTS` 对老库无效，不删会遗留死表），同时 `SCHEMA_VERSION + 1`，版本记在 `PRAGMA user_version`
  - 新增**表**不需要 `_MIGRATION_COLUMNS`：`CREATE TABLE IF NOT EXISTS` 对老库同样执行，建表即完成迁移，只需 `SCHEMA_VERSION + 1` 并补一条老库升级测试
  - 删除凭证时显式清理其模型级冷却行（无外键级联），否则重建同 id 凭证会继承旧的模型冷却

---

## 8. 测试策略（Q14=B + T-Q5）

| 目标 | 覆盖 | 方式 |
|---|---|---|
| 调度器（到期指标/冷却/三态排序/轮换/pin） | 100% | 纯单元，注入假 provider |
| 鉴权（users/apikey/session/rbac） | 100% | 单元 + FastAPI TestClient |
| SSE 帧解析、provider 事件映射、OpenAI 协议适配、OAuth/回调解析 | 100% | fixture 契约测试（真实样本 → Event / 响应断言） |
| HTTP 客户端 | 100% | respx mock 状态码 + body → classify 断言 |
| 统计/查询 | 100% | sqlite 内存库集成 |
| 其余 | 100% | — |

**全量 100%（行 + 分支）是硬门槛**：CI 里 `pytest --cov-fail-under=100`，新增/修改的代码必须带测试，缺口一律补测试解决，不用 pragma / 排除达标。

fixture 存于 `src/provider/fixtures/`（真实 SSE/JSON 样本，覆盖正文、思考、工具调用、错误码与额度），断言两个方向：**解析正确**（样本 → 期望 Event）与**不静默**（畸形样本 → `UpstreamProtocolViolation`）。

---

## 9. 已知取舍备忘

- **同步 sqlite3 而非 aiosqlite**（T-Q2）：本地微秒级操作，asyncio 封装开销大于收益
- **双 httpx 客户端**（T-Q4）：聊天流 `read=None` 防长流截断；短请求总超时 30s 防悬挂；共享 `trust_env=False`
- **手写 SQL 而非 ORM**：8 张表规模下 ORM 收益为负
- **polling OAuth 不转回调**（Q17=C）：上游协议决定；TRAE 回调走主端口 + `PUBLIC_BASE_URL`
- **v1 无 Anthropic**（Q8=A）：Event 层已预留，v1.1 只加 `compat/anthropic/` 适配器
- **Responses 出口只做 Codex CLI 用到的子集**（Q32，详见 §3.7）：不做 `store=true` / `previous_response_id`（服务端无状态，不假装支持）；`include=["reasoning.encrypted_content"]` 按实测接受并忽略——Codex CLI 每轮必带，400 会直接打死主客户端；流式终止用 `response.completed` / `response.incomplete` / `response.failed`，**不发 `[DONE]`**（Responses 协议无该哨兵）。形状取自官方 `openai` SDK 类型并用其作客户端验证，对真实 CB 上游冒烟过；**未经真实 Codex CLI 端到端验证**（本机无 CLI）
- **不做 reasoning 注入 / effort 档位映射**（原 B1.2，实测后取消）：原计划对「强制推理模型族」注入 `thinking` + `reasoning_effort` 并回填历史 `reasoning_content`，实测前提不成立——（1）客户端已自带 `reasoning_effort`（仅 `low`/`medium`）且上游直接接受；（2）客户端已回传历史 `reasoning_content` 且上游接受；（3）原计划的默认模型清单与实际在用命名无关，且 `glm-5.1` 在 `MODEL_BLOCKLIST` 里，硬编码白名单会空转；（4）真要做「客户端丢弃时回填」必须服务端存对话内容，与脱敏纪律冲突。参考实现 IceeAn/codebuddy2api 走相反取向（对白名单模型强制 `reasoning_effort=max` 覆盖客户端），属单来源且会改写客户端意图，不采纳
- **统计一律以 `usage_hourly` 为准**：`overview` / `by_provider` / `timeline` / `model-timeline` 均读小时汇总，只有 `events`（逐请求明细）读 `usage_events`。统一口径是为了让选「全部」时总览与图表同值（明细只留 90 天，汇总永久）。代价：最近 ≤5 分钟未进汇总的请求不计入，刷新一次即可
- **小时汇总双写**：`record()` 写明细的同时增量累加当前小时行，新请求立即可见于统计页（不依赖 5 分钟一轮的 rollup）；`rollup_hourly` 仍每 5 分钟全量重算作对账，两者结果一致（幂等）
- **延迟均值只算成功请求**：分子 `SUM(latency_ms WHERE ok=1)` 与分母 `ok_count` 配对；失败请求的耗时不能拉偏「典型耗时」（与图表口径一致）
- **应用日志只写 stderr，轮转交给平台**：不在应用内开文件、不用 `RotatingFileHandler`。三种部署形态（launchd / systemd / docker）采集方式不同但都靠 stdout/stderr 对接；应用自己写文件会与平台轮转争抢同一文件，容器里还会写进镜像层（重启即丢且 `docker logs` 看不到）。各自配置见 `deploy/` 与 compose 的 `logging` 段
- **必须在 `build_app` 里配 root logger**：uvicorn 默认 `LOGGING_CONFIG` 只配 `uvicorn` / `uvicorn.access`（`propagate=false`），**从不配 root**；root 默认 `WARNING` 且无 handler，导致 `logging.getLogger(__name__)` 的 INFO 静默丢失。生产路径 `uvicorn src.main:build_app --factory` 不经过 `run()`，所以配置必须挂在 `build_app`（幂等，见 `src/webapp/logging.py`）
