# Coding2API 设计文档

> 结论：九个上游厂商、十个渠道（CodeBuddy 国内 / 国际、TRAE SOLO、OpenCode Zen、Kilo、Qoder、CodeArts、OfficeAce、小浣熊、灵犀）统一为一条 OpenAI 兼容出口，公共凭证池 + 统一模型名 + 自动调度 + 按人用量统计；实现见 [TECHNICAL.md](TECHNICAL.md)，使用与部署见 [README.md](README.md)。

本文只记仍成立的目标、上游事实、架构与约束。

## 1. 目标与非目标

**目标**：单一 OpenAI 兼容出口挂十个渠道；`模型@渠道` 精确指定；凭证 admin 集中维护、全员共享；调度器自动挑健康号，上游死亡自动冷却；按人统计用量与估算成本。

**非目标（硬边界）**：

- 不做按 token 的计量计费——上游订阅制，成本不随 token 线性增长；Key / 用户级配额只是可选旁路治理，默认全关、判定异常放行，**不是安全边界**。
- 不做通用 provider 网关——硬编码十渠道 + 一个可选的通用标准端点渠道 `generic`（部署者填 base_url）。
- 不做**上游积分 → 货币**换算——各渠道积分互不通用，分开记流水；成本只按刊例价估算，USD 落库并按可配汇率另记 CNY 展示值。
- 不做数据迁移、自更新、mTLS；不做管理台与端口的 IP 限制（交给反向代理）；不做全量请求审计。

## 2. 上游事实与协议要点

| 渠道 | 协议 / 鉴权 | 关键约束 |
|---|---|---|
| `codebuddy` | `POST /v2/chat/completions`，**仅流式**；设备码认证 | `11140` 账号级风控（换号/重登无效）；`6004` / `14003` 模型级；`11102` / `11158` 负缓存；`11129` schema 拒收；`11115` 上下文超限；`11134` 供应商过载 → QUEUE；`14018` / `1005` 额度耗尽 |
| `codebuddy_intl` | 同协议、独立账号体系 | 首条必须 system（否则 `400/11128`）；指纹对齐官方 CLI 2.185.0 |
| `trae` | `POST /api/agent/v3/llm_utils_chat`，仅流式 | 签到须回查积分到账；`1005` / `4008` → CREDIT；`4001` 参数 / 模型不可用；两键 `max_tokens` 都不生效 |
| `qoder` | 私有 COSY 签名 + 信封 SSE | 保活帧 `body:"null"` 必须跳过；节点故障包成 400 → MODEL；401 一律 DEAD、403 多数 SOFT |
| `codearts` | `SDK-HMAC-SHA256` + DPoP | refresh_token 一次性且与 `client_id` + 私钥绑定；并发 3 会话（`TM.00001041`）；`001001020` 非多模态 → CAPABILITY；额度＝积分 |
| `officeace` | 聊天 Basic 不过期 / 控制面 V11+SDK 双签名约 2h | 限流走伪 200；`81004` / `81009` 模型级；`max_tokens` 需改写 |
| `raccoon` | 标准 OpenAI + 网页会话 JWT | 未知模型静默回落默认 → 双层校验；`429 user_model_rate_limited` → CONCURRENCY；缺 `x-client-device-id` 回 `100002 params_invalid_error`、会话不可信回 `400 200811 device_bind_required` |
| `lingxi` | WPS `wps_sid` cookie，一次对话三条请求 | 按 run 计费 → 缓存 1h；`usage.total_tokens` 是上下文大小需自算 |
| `zen` / `kilo` | 标准 OpenAI，虚拟凭证 | zen 需门禁伪装（UA / session / tools）；kilo 有 `isFree` 无需探活 |
| `generic` | 标准 OpenAI，部署者自己的 key | 401/403 → `DEAD`（与 zen / kilo 相反） |

**跨渠道事实**：

- **reasoning 客户端给什么透传什么**；`reasoning_effort` 缺失补 `medium`（不发则 CB 把思考写进 `delta.content`）。三项可选机制（`REASONING_EFFORT_MAP` / `ANTHROPIC_THINKING_EFFORTS` / `FORCE_DISABLE_THINKING`）默认全关，全关行为逐字节不变。
- **输出上限键名不对称**：CB 只认 `max_tokens`、TRAE 两键都不生效——不做键映射。
- **图片能力标志不可信**：入站统一归一 `image_url`，**不做模型能力门禁**，能否识图交上游裁决。
- **登录三轨**：`poll`（CB / Qoder / OfficeAce）、`callback`（TRAE 302 回 `/authorize`）、`paste`（CodeArts 本机回环监听不到）。同一账号重登按稳定身份**就地合并**，不新增重复行（重复行会绕过节流与统计口径）。
- **积分语义不同**：CB 有周期重置，TRAE / CodeArts / 灵犀各自权益包独立到期 → 健康分统一百分比，到期阶梯按渠道落库。

## 3. 架构决策

### 3.1 三平面单进程

一个 FastAPI 进程内三平面，共享同一份 SQLite（WAL + `busy_timeout=5000`）与 Provider 客户端：

- **请求面**（API Key 鉴权）：`/v1/chat/completions` · `/v1/responses` · `/v1/messages` · `/v1/models` · `/v1/user/balance` → 协议层 → 执行引擎 → 中立事件流 → 十渠道 Provider。
- **管理面**（会话 Cookie + RBAC）：React 管理台 → `/api/*`。
- **后台面**（无 HTTP 入口，`TaskRunner` 起 12 类循环）：额度探测、token 预刷新、签到、成长、活跃上报、明细清理、备份、模型列表兜底刷新、模型探活、OpenRouter 目录刷新、运维告警、上游状态聚合。

**为什么这样切**：请求面无状态、可并发，不知「人」是谁（只认 Key 归属用户）；两鉴权面互不替代（Key 进不了管理台，Cookie 进不了 `/v1`）；后台面与请求面共用 Provider 与节流器——风控按最小间隔生效，不能各发各的；三面共享一份 SQLite，升级只重启一个进程。

### 3.2 Provider 边界与会话粘性

Provider 只管上游私有部分（发请求、解析事件、分类错误、凭证生命周期、额度探测）；调度、冷却、重试、统计全在共享引擎。接口三类：凭证生命周期、`probe_quota`、执行与分类。

**会话粘性三级**：body 显式键（`conversation_id` / `prompt_cache_key`）> 请求头（`x-session-id` 族，须匹配 `[A-Za-z0-9._:-]{1,128}`）> 消息增量前缀指纹。头夹中间：body 最精确，头比指纹可靠；头值掺 `username` 再哈希，否则可猜头值占用别人账号；带 `user_id` 时不派生前缀兜底键（同用户并行对话前缀可能相同）。**请求级错误（`REQUEST` / `CONTEXT` / `NETWORK`）换号后不重绑**——上游前缀缓存按账号隔离，误重绑会让命中率从 97–99% 掉到 0–12%。

### 3.3 调度与冷却

选号顺序：**手动 pin**（绝对过滤，不是同档提权）→ **免费优先**（`credit_rate == 0`，`None` 不算免费；x0 会被实测扣费校准）→ **在途最少**（并发拆分，静态指标会全压同一个号）→ **到期额度两级**（36h 主窗口，7 天次窗口）→ **健康度三态**（`known > unknown > exhausted`，探测失败算 0 分会让其永远轮不到）→ `credential_id` 稳定。

**冷却必须分两层**：`429 + 6004` 是「此模型在当前账号用超」，`400/404 + 11102` 是「当前账号没此模型」；一律记账号级 → 一次模型级限流踢掉整号。故账号级写 `cooling_until`（带来源 `cooling_kind`，`revive` 默认保留上游点名的限流 / 额度类），模型级另建 `credential_model_cooldowns`；账号级冷却出现时清该凭证模型级条目，防「切模型」绕过。

时长：`PLAN` 12h、`SOFT` 60s、`OTHER` 连续 3 次→10m、`MODEL` 10m 起翻倍封顶 2h、`BLOCKED` 6h 起封顶 24h、`CONCURRENCY` 首枪 60s 且同一命中链才翻倍（封顶默认 5min / raccoon 30min）、`CREDIT` 到次日 04:00、`RESTRICTED` 2h 起按命中次数翻倍（封顶 7 天；同渠道窗口内第二个不同凭证再中招 → 该渠道整体 24h）。**上游点名的恢复时刻优先于常量**：`Retry-After` 或正文 `reset at … UTC+8` 解析后钳在 `[now + 常量, now + 24h]`；CB `429+6004` 头缺失时只能读正文。

**传输层失败归 SOFT**：`httpx.TransportError`、`ssl.SSLError`、`UpstreamTransportError` 统一在 `executor._classify` 兜住；不泛化到 `OSError`，`ssl.SSLError` 必须单列（否则逃成 `internal_error`：不换号、不冷却、统计零痕迹）。

**「窗口内积分总量」而非单一到期日**：CB 额度是几十个各自到期的小包，只存一个日期没有区分度；落库到期阶梯 `[(到期 epoch, 包剩余积分)]`，过滤 `end > now` 且排除已用完，两级窗口才够区分。展示用 `quota_packages` 与调度用 `quota_expiry_ladder` 分开存。

### 3.4 模型名与出口

三字段派生：**原代号**（`raw_id`，转发时经别名表换回，永不改动）、**归一键**（合并键 / 对外 id）、**展示名**。`/v1/models` 的 `id` 一律归一键，原代号进 `by_provider.{渠道}.raw_id`；同模型异名由 `MODEL_SYNONYMS` 收敛，只登记人工核实；哨兵名（`auto` / `default`）不跨渠道合并。

中性事件层（`EventKind`：`content` / `reasoning` / `tool_calls` / `usage` / `finish` / `error`）之上挂三个出口适配器：OpenAI chat、Responses（Codex CLI）、Anthropic Messages（Claude Code）。**入口语义收口**：`response_format` 只放行 `text` / `json_object` / `json_schema`；Anthropic `cache_control` 默认原样透传，只校验结构，无标记时映射逐字节不变。

### 3.5 数据落库边界

- **落库**：调度状态、到期阶梯、签到审计（`checkin_events`）、竞态敏感的凭证状态。重启不丢。
- **进程内**：签到去重、模型列表缓存、任务运行态、迟滞计数——重启归零，**不编造记录**。去重是运行态，与审计日志是两回事。
- **文件**：模型目录 `DATA_DIR/model_catalog.json` 与 `openrouter_catalog.json`（默认 `DATA_DIR=./data`）。
- **脱敏纪律**：不存提示词、回答、请求头、Token、工具参数、原始错误体、会话 ID。唯一例外 `DUMP_REQUEST_BODIES=true`（默认关，有界保留 200 份，不得长期开启）。
- **schema 变更纪律**：`schema.sql` 只加不改；新增列走幂等补列，删表须同时补删表迁移并升版本（`CREATE TABLE IF NOT EXISTS` 对老库无效，不补删则遗留死表）。

### 3.6 观测与告警口径

- **失败归因按尝试计**：硬失败每次尝试一行，限流 / 额度 / 超时 / 上下文等每次请求一行且类型取最后一次错误的真实归因；`failures − rescued` 是未被救回的失败尝试数。
- **九类告警**：池耗尽、任务连续失败、token 到期、上游错误率、单 Key 用量暴涨、非工作时段请求、预算分层、渠道零成功、签到连续失败。后两类补的是既有规则的两个盲区：`channel_dead` 绕开错误率被 fallback 稀释，`checkin_failing` 绕开「任务跑成功但上游回失败」不计任务失败。
- **上游状态聚合只做解释不做决策**，不进选号与熔断；只存内存不落盘（重启后拿几小时前的「一切正常」比「尚未抓取」更危险）。


## 4. 关键取舍与否决理由

| 决策 | 否决的替代 | 理由 |
|---|---|---|
| 同步 sqlite3 + 手写 SQL | aiosqlite / ORM | 本地微秒级操作，asyncio 封装与 ORM 开销大于收益（十几张表规模） |
| 单进程三平面共享一份 SQLite | 管理面独立服务 | 两面共用执行引擎与仓储；升级只重启一个进程 |
| 双 httpx 客户端 | 单客户端通吃 | 聊天流需 `read=None` 防长流截断，短请求需总超时防悬挂 |
| 账号级 + 模型级两层冷却 | 只记账号级 | 一次模型级限流会踢掉整号 |
| `codebuddy_intl` 独立渠道 | 切 `CODEBUDDY_API_ENDPOINT` | endpoint 是进程级单值，切过去会让存量国内凭证打到国际站（两站账号与额度不通用） |
| Zen / Kilo 用虚拟凭证行 | 每环节加「无凭证 provider」分支 | 调度 / 冷却 / 统计 / 粘性全按 `credentials` 行工作，种一条空凭证代价更小 |
| Zen 注入 `bash` / `read` | 直接报错要求客户端带工具 | 「对上游撒谎、对客户端诚实」：只补缺，有同义工具就改写透传，没有才过滤并收敛 `finish_reason` |
| 三轨 OAuth，`/authorize` 靠会话 + `loginTraceID` 双绑定 | 只靠 Origin / Referer 判同源 | 回调本就跨源 302，`no-referrer` 可绕过 |
| effort 映射默认不做，只 `setdefault` 补 `medium` | 内置渠道默认矩阵 | 未实测即编造；改为默认关闭的三项机制 |
| 批量运维只收已存在的单条动作 | 批量 `refresh` / `delete` | `refresh` 强刷白耗一次性票据；`delete` 需二次确认。批量 `toggle` 传显式 `enabled` 而非取反（混选会让一半号反向） |
| 统计以 `usage_hourly` 为准 | 全部读明细 | 小时汇总永久、明细 90 天；代价是最近 ≤5 分钟未汇总不计入 |
| 成本估算为下限 | 拿 0 冒充免费 / 猜价 | 匹配不到价表或未上报输入 token 就不计，`null` 显示 `—` |
| 批量审计只记一条汇总 | 逐条审计 | 50 个号是一次操作、一个意图 |
| 窄屏表格卡片化改样式不改 DOM | 按视口渲染两套 JSX | 页面测试按 testid 断言，DOM 一变换语义就没了 |
| 凭证页一个渠道一个组，后端不拆行 | 每条 token 一行 | `refresh_token` 一次性且绑定 `client_id` + DPoP 私钥，拆行会二次刷新并烧票 |
| 内置检索工具独立出口 | 只在 Responses 里映射 `web_search` | 先有可直连的出口，再谈把托管工具映射过来（未做） |

## 5. 安全边界（审查结论）

| 面 | 结论 |
|---|---|
| 上游端点 | 各渠道独立 `*_ALLOWED_ENDPOINTS`；CB 国际白名单**刻意不含国内站**（只校验「在名单里」不够，Token 会照样跨站发）；TRAE `apiHost` 用户可控 → 导入按白名单拒 |
| 凭证加密 | Fernet + `APP_SECRET`（≥16 字符、拒低熵）；**丢失 = 凭证全部不可解**，不做轮换；解密失败给可行动错误码，批处理路径跳过该凭证 |
| API Key | 仅存摘要，明文只回显一次；可限渠道 / IP（默认不信 `X-Forwarded-For`，仅 `TRUST_PROXY=true` 取最后一个）/ 模型（无法判定时 fail closed）/ 到期；发删与导出都进审计 |
| 用户级策略 | 会话 Cookie 只回答「你是谁」，Playground 是免 Key 直连 → 模型白名单与配额必须落在人身上，否则 viewer 用 Cookie 就能绕过 Key 的限制 |
| kill switch | 拦在鉴权依赖而非各端点（新端点不会漏拦）；管理台与登录保持可用（否则开了就关不上） |
| 会话 | `SameSite=Lax` + 写操作 CSRF；`Secure` 按实际协议；无 Cookie 的写端点也做跨站防护；吊销用 `session_epoch` 自增而非会话表 |
| 错误与日志 | 错误响应不回流上游正文；未匹配 `/api`、`/v1` 返回 JSON 404（任何方法）；`ClientDisconnect` → 499；日志页 `redact_or_drop` 整行过滤，宁可漏一条也不把凭证推进浏览器 |
| 输入 | 请求体上限（登录 8KB / 备份恢复 512MB / 其余 16MB）；拒绝越界数值与 `NaN` / `Infinity`（含 `1e999`）；SSE 单行 / 单帧缓冲有界；非 2xx 错误体有界读取 256KB |
| 热更 | 写入即校验（float 拒 NaN/±inf、长度上限）；webhook 只接受 `http(s)` 且做 SSRF 校验（写入期 + 投递前按 DNS 复核） |

**明确不做**：mTLS；管理台与端口的 IP 限制（交给反向代理）；全量请求审计。

## 6. 风险清单

| 风险 | 等级 | 对策 |
|---|---|---|
| CB 协议复杂度高 | 高 | 先 bearer-only 跑通再补 OAuth；不可逆动作用配置开关总控 |
| 上游 SSE / 私有协议变更 | 中 | 样本存 fixture 做契约测试；解析失败不静默（`UpstreamProtocolViolation`） |
| 模型 ID 撞车导致路由错误 | 中 | 扁平名 + 归一键合并 + 健康度路由 + `@渠道`；统计行强制带 provider |
| 未知模型静默回落 | 中 | 发请求前按目录校验 + 流首帧比对回显 `model`（raccoon 实测会回 200 + 默认模型） |
| 上游 credit 不保证可得 | 低 | credit 仅辅助展示；健康度只依赖额度探测接口 |

## 7. 路线图未完成项

- **`generic` 多渠道**：一个部署只配一条（渠道 id 固定 `generic`）；多接几家需把 `KNOWN_PROVIDERS` 动态化，未做。
- **Zen 客户端档位**：`ZEN_CLIENT_PROFILE=v2`（复刻官方 2.x 指纹）已实现但默认不切——2026-11-19 上游免费额度耗尽，两档均无法端到端复验，待额度恢复后 A/B。
- **Responses / Anthropic 出口验证**：仅按官方 SDK 做契约验证，**未经真实 Codex CLI 端到端**。
- **`cache_control` 透传实测覆盖**：6 家已验证；zen 因 `FreeTierError` 不能验证、OfficeAce 未跑探针，小浣熊与灵犀未验证。
- **内置检索工具**：国际站是否同样挂载 `/agenttool/*`、搜索 / 抓取是否消耗积分，均未核实；Responses 的 `web_search` 托管工具仍未映射过来。
- **告警「多 IP 同 Key」**：不存 IP，故该规则未实现。
- **CodeArts 签到之外的活动操作**：未接入。
- **上游状态聚合**：只做解释不做决策，不进选号 / 熔断（不是缺陷，是边界）。
