# Coding2API 立项决策

把 CodeBuddy 与 TRAE SOLO 两个 coding agent 上游通道，统一封装为 OpenAI 兼容 API，并提供公共凭证池、统一调度与按人用量统计。

> 本文档记录立项决策与可行性核实。实现细节见 [TECHNICAL.md](TECHNICAL.md)，使用与部署见 [README.md](README.md)。

## 1. 决策摘要

| 编号 | 决策 | 选择 |
|---|---|---|
| Q1 | 目标场景 | 2–10 人小团队共享 |
| Q2 | 技术栈 | Python / FastAPI，从零新架构 |
| Q3 | 仓库 | 全新仓库，新建架构 |
| Q4/Q20 | 项目名 | `coding2api` |
| Q5 | 前端 | React + Tailwind，从零新写 |
| Q6 | 迁移方式 | 从零重写，旧项目仅作参考 |
| Q7 | 凭证归属 | 公共池 + 按人统计 |
| Q8 | 协议出口 | v1 仅 OpenAI；v1.1 加 Anthropic |
| Q9 | 存储 | SQLite，schema 重新设计，凭证入加密列 |
| Q10 | 配额 | 不做配额，只做按人统计 |
| Q11 | 前端范围 | 中等 6 页：Dashboard / 凭证 / API Key / 统计 / Playground / 登录（无设置页，配置走 env） |
| Q12 | 调度策略 | 统一健康度 + 到期积分指标 + 冷却状态机，保留手动 pin |
| Q13 | Anthropic | v1.1，架构预留中立事件层 |
| Q14 | 测试 | 全量 100%（行 + 分支），契约测试优先 |
| Q15/Q26 | 健康度归一化 | 剩余积分百分比，跨 provider 可比 |
| Q16 | 抽象边界 | 细接口：Provider 只管发请求 + 解析 + 分类错误 |
| Q17 | 登录 | 双轨（CB 轮询 / TRAE 回调），前端统一状态机 |
| Q18 | 权限 | 单 admin + 普通用户，admin 管凭证 |
| Q19 | 交付顺序 | 串行：骨架 → TRAE → CB 基础 → CB 完整化 |
| Q21 | 模型名 | 扁平，自动路由 |
| Q22 | 数据迁移 | 不迁移，全新开始 |
| Q23 | 发布 | 源码 + Dockerfile + compose，CI 跑测试 |
| Q24 | 统计 | 统一 token，credit 可空 |
| Q25 | 抽象风险 | M0 mock 冻结接口；M1b 结束做双 provider 对比验证 |
| Q27 | 指定上游 | `model@provider` 后缀覆盖 |
| Q28 | 容器 | Dockerfile + compose 双份，CI 验证 compose |
| Q29 | 文档 | 中文为主 + 英文 README |
| Q30 | License | MIT + NOTICE 三方溯源，不做自更新 |
| Q31 | 到期积分排序 | 窗口内到期积分总量做第一排序键（默认 36h，env 可配）；落库到期阶梯而非单一日期 |

## 2. 目标与非目标

### 目标

- 单一 OpenAI 兼容端点，后面挂 CodeBuddy 与 TRAE 两个上游
- 凭证由 admin 集中维护，全员共享，调度器自动挑健康的号
- 按人统计用量（请求数、成功率、token、耗时与首字延迟）
- 上游死亡自动冷却，不反复踩死号
- 支持 `model@provider` 精确指定上游

### 非目标（明确不做）

- **不做配额/限流**：上游是订阅制通道，成本不随 token 线性增长；10 人规模靠统计页可见性约束滥用
- **不做通用 provider 网关**：只支持两个上游，硬编码，不做插件系统
- **v1 不做 Anthropic 协议**
- **不做旧项目数据迁移**
- **不做自更新脚本**
- **不做货币/积分换算**：两个上游的积分单位不互通，分开记录

## 3. 关键事实（已核实）

### 3.1 CodeBuddy（腾讯）

- 端点：`https://copilot.tencent.com`（国际站 `https://www.codebuddy.ai`）
- 聊天：`POST /v2/chat/completions`，**只支持流式**，非流式需本地聚合
- 认证：`POST /v2/plugin/auth/state?platform=CLI` → 拿 `authUrl`/`state` → 轮询 `POST /v2/plugin/auth/token?state=...`（设备码模式）
- 账号切换：`/v2/plugin/login/account`、`/v2/plugin/accounts`
- 额度：个人版 `POST /v2/billing/meter/get-user-resource`（`CycleCapacity*Precise`），企业版 `POST /v2/billing/meter/get-enterprise-user-usage`（`credit` 已用、`limitNum` 总额）
- 签到：`POST /billing/meter/daily-checkin`；状态：`POST /billing/meter/checkin-activity-status`（连续天数 / 今日是否已签）
- **成长中心**（逆向自 WorkBuddy 桌面端，前缀 `/v2/activity/growth`，仅 CodeBuddy 有）：
  只读 `buddy/travel/status`、`buddy/travel/config`、`tasks`、`streak`、`redeem/summary`、`lottery/chances`、`buddy/quota`、`energy`；
  写入 `buddy/travel/claim`、`buddy/travel/depart`、`tasks/accept`、`makeup-cards/use`、`redeem`、`lottery/draw`、`buddy/open`
- 鉴权头与聊天一致（`Authorization` + `X-User-Id` + `X-Domain`）；成长中心实测可用项目内的 OAuth bearer 凭证直连，无需桌面端凭据文件
- 成长中心任务契约（2026-09）：`accept_status` 五态；接单 `POST /tasks/accept` 收 `{"task_codes": [...]}`（单数一律 400）；领奖 `POST /tasks/{code}/claim`；`/redeem` 的 `tier` 是档位标识 `"7d"/"14d"/"28d"`，实发字段是 `*_granted`
- **活跃度的驱动来源（实测，勿凭单次实验下结论）**：活动类操作（领取成长中心奖励等）**计入**——两天无桌面端使用、仅差别在有无领取动作，`score` 就从 0 变 5；而纯 `/v2/chat/completions` 对话**不计入**（3 次完整对话后 `today.score` 与 `updated_at` 均不动）。连登天数含 1 天容忍窗口（H5 规则原文：按「连续登录且使用的天数」计，每月清零）。热力墙分档 = score 0 / 1-10 / 11-30 / 31-60 / >60，每日 02:00 批算。**与积分无关，不参与调度决策**
- **活跃上报（B1.7，默认关闭）**：`POST /v2/report`（与聊天同基址），body 为 `[chatRequestSendEvent]`（`eventCode=chat_request_send`，全字段），`userId` 必填——缺失时上游 HTTP 200 `code:0` 但静默丢弃（连登不变）。OAuth 凭证 `account_uid`/`user_id` 实测为空，回落 bearer JWT 的 `sub`。实测一条即点亮连登（1→2）。**默认关闭**：H5 条款明禁模拟器/脚本篡改数据，处罚为取消资格并追回已发礼品；事件形状依赖上游实现、改版即失效，不作为可靠性功能
- **凭证身份可能为空**：OAuth 登录路径下上游账号接口未回填 `account_uid`/`user_id`（实测发生），签到/成长中心的同账号隔离必须回落到 `credential_id`,否则第二个账号会被静默跳过
- 请求头需 `X-Domain`、`X-User-Id`、`X-Enterprise-Id`、`X-Department-Info`（部门名须 UTF-8 百分号编码）
- **reasoning 字段当前直接透传，不做注入也不剥离**（实测 71 份真实请求 dump）：客户端自己会带 `reasoning_effort`（69/71，只有 `low`/`medium` 两档，非推理模型如 `hy3` 不带），也会在历史 assistant 消息里回传 `reasoning_content`（51/71，含带 `tool_calls` 的消息），上游原样接受（`deepseek-v4.1-flash` 4260 次请求 99.6% 成功）。因此既不需要「effort 档位映射」，也不存在「客户端丢弃 reasoning_content」这一前提；`enable_thinking` 只在客户端未给时补 `true`
- **CB 的 usage 不回 `reasoning_tokens`**（实测恒为 0：`deepseek-v4.1-flash` 289 万 output tokens / reasoning_tokens 全 0），TRAE 侧正常回（`qwen-3.7-plus` 单请求 6~114）。统计页 CB 的思考 token 恒显示 0 属上游口径差异，不是采集丢失
- **输出上限键名不对称**（2026-09-21 直连实测）：CB 上游**完全忽略 `max_completion_tokens`**（`=1` 仍出 59 tokens，无该键亦同），只认 `max_tokens`（精确截断 + `finish_reason=length`），两键同发时后者胜出；TRAE 对两个键**都不生效**。本网关不做键映射，客户端限额原样透传——因此若客户端只发 `max_completion_tokens`，输出**不会被截断**。`enable_thinking: false` 亦被上游忽略（仍产 reasoning 并计入 `max_tokens`）

### 3.2 TRAE SOLO（字节）

- Agent Host `https://trae-api-cn.mchost.guru`、UG Host `https://api.trae.cn`、OAuth Host `https://api.trae.com.cn`
- 聊天：`POST /api/agent/v3/llm_utils_chat`；模型：`POST /api/ide/v1/get_detail_param`
- 认证：浏览器登录 → 302 回调 `/authorize` → `ExchangeToken` → `GetUserInfo`
- Token 刷新：`POST /cloudide/api/v3/trae/oauth/ExchangeToken`（refreshToken 轮换）
- 签到：`/trae/api/v2/ug/checkin_credits/{status,claim}`；额度：`/trae/api/v2/pay/ide_user_ent_usage`
- **签到成功必须"确认到账"，不能只看领取接口的返回码**：TRAE 的 `claim` 对当天已签过的账号
  也返回 `code:0 success`（幂等），实测此时 `status.credits` 前后都是 150、`checked_in` 已是 true。
  用「claim 返回 0」判断成功会把「什么都没发生」报成成功（本项目曾据此得出错误结论并返工）。
  正确判定：`checked_in` 为真且回查 `credits` 确有增加。CB 侧同规矩（`code=0` 且 `credit` 是有限数值）
- **签到 9074 按设备标识处理（不猜设备号格式）**：观测到的是「数字串是必要条件、非充分条件」——同一账号 hex32 与确定性派生值失败、随机新数字串成功；`X-Device-Id` 空串返回 9004（参数错误）。取值需为数字串且不宜复用，本项目每次 claim 生成新的 16 位数字串，一轮内最多换号重试 2 次（`CHECKIN_ATTEMPTS`），其余交给上一层的 10 分钟周期。**注意：某账号当天签到成功后，任何 device_id 的 claim 都会返回 `code:0`（幂等）**，所以判断成功必须看 `status.checked_in`，否则极易得出错误结论（本项目为此返工过一次）
- SSE 事件序列：`metadata` → `timing_cost` → `output`×N → `extra_info` → `token_usage` → `done`
- `token_usage` 含缓存字段 `cache_read_input_tokens` / `cache_creation_input_tokens`（未命中为 0，非缺失），映射为统计的 `cached_tokens`；**无 per-request credit**
- 错误码 `1005` = 权益不足；仅流式，非流式需聚合
- **接受客户端传来的 `reasoning_effort`**（实测透传 `low`/`medium` 均 200 且正常出流，`reasoning_tokens` 有值）：不认 `thinking` 对象，也无需服务端补注入；`developer` 角色上游不认（静默空流），已归一为 `system`

### 3.3 冲突与陷阱

| 问题 | 事实 | 对策 |
|---|---|---|
| 模型 ID 撞车 | 两边都有 `glm-5.2`、`DeepSeek-V4-Pro` | 扁平名 + 健康度路由 + `@provider` 后缀 |
| 积分语义不同 | CB 有周期会重置；TRAE 是单调余额 | 健康分统一为百分比，展示层标注周期语义 |
| credit 可得性 | CB 有 per-request；TRAE 只有账户总额 | 统计表 credit 字段 nullable |
| 登录机制 | CB 轮询（后端出网）；TRAE 回调（浏览器可达） | 双轨，回调统一走主端口 |
| 媒体/工具 | 两边 SSE 都含工具调用 | v1 透传，不做语义转换 |

## 4. 架构

### 4.1 分层

```
┌──────────────────────────────────────────────┐
│  客户端  POST /v1/chat/completions            │
│          GET  /v1/models                      │
└────────────────────┬─────────────────────────┘
                     │
┌────────────────────▼─────────────────────────┐
│  协议层  OpenAI 请求规范化 / 响应适配          │
│  模型名解析：name | name@provider             │
└────────────────────┬─────────────────────────┘
                     │
┌────────────────────▼─────────────────────────┐
│  执行引擎                                     │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐      │
│  │ 调度器   │ │ 冷却状态 │ │ 统计采集 │      │
│  │ Select() │ │ 机       │ │          │      │
│  └──────────┘ └──────────┘ └──────────┘      │
└────────────────────┬─────────────────────────┘
                     │ 中立事件流
        ┌────────────┴────────────┐
┌───────▼────────┐       ┌────────▼───────┐
│ CodeBuddy      │       │ TRAE SOLO      │
│ Provider       │       │ Provider       │
└────────────────┘       └────────────────┘
```

### 4.2 Provider 接口（Q16=A 细接口）

Provider 承担上游协议私有部分：发请求、解析事件、分类错误，以及凭证生命周期与健康度探测。调度、冷却、重试、统计全在共享引擎。协议定义见 [TECHNICAL.md §4](TECHNICAL.md)。

### 4.3 调度器（Q12=B + Q26 + Q31）

统一实现，两个 provider 共用：手动 pin 优先（粘性让位，见下） → 会话粘性命中且可选时直接复用（不排序） → 过滤 healthy（含**模型级**避让：逐凭证按自己所属上游的原始模型名查 (凭证, 模型) 冷却表，见 [TECHNICAL.md §6.2](TECHNICAL.md#62-模型级冷却b11)） → 到期积分多者优先（把 `quota_expiry_ladder` 中距到期 ≤ `QUOTA_EXPIRY_WINDOW_SECONDS`、默认 36h 的积分加总）→ 按健康度三态排序（`known 降序 > unknown > exhausted`；同分按 `credential_id` 稳定）→ 无可用返回 None。到期指标让快过期的积分先用掉，避免白丢；冷却与错误累计规则见 [TECHNICAL.md §6](TECHNICAL.md)，会话粘性见下文。

**为什么冷却要分「账号级」与「模型级」两层**。上游的拒绝语义并不都是账号级问题：`429 + 6004` 是「这个模型在当前账号上用超了」，`400/404 + 11102` 是「当前账号没有这个模型」。把它们一律记成账号级冷却，会让一次模型级限流把整个账号踢出池（同账号的其他模型明明可用），而把 `11102` 丢掉不管又会让坏组合被反复选中。因此账号级继续写 `credentials.cooling_until`，模型级另建 `credential_model_cooldowns`；账号级冷却出现时清空该凭证的模型级条目，防「切模型」绕过账号级限制。业务码识别只认 `"code": N` 键值形态，不搜裸数字（`"code":111020` 含 `11102` 子串）。

**为什么是「窗口内积分总量」而不是「是否即将过期」（Q31）**。实测 CodeBuddy 的额度不是一个整块周期，而是几十个各自独立到期的小包（每日 100 积分 × N，`get-user-resource` 一次返回 30~36 个套餐），这决定了三个取舍：

- **只存一个日期没有区分度**：各账号的「最早到期」经常落在同一天同一时刻，布尔分组退化成健康度排序。改成统计窗口内的到期积分总量，账号之间才有可比的高低。
- **落库到期阶梯而非预计算数字**：窗口是运行时参数，存 `[(到期 epoch, 该包剩余积分)]` 后，改 `QUOTA_EXPIRY_WINDOW_SECONDS` 立刻生效，不必等下一轮探测。
- **过滤条件必须是 `end > now`**：上游会把已过期套餐一起返回（`PackageEndTimeRangeBegin` 过滤的是套餐有效期，不是积分周期），不过滤的话「最早到期」永远是过去的时间，指标恒为 0；已用完的包（剩余 0）同样排除，它不携带积分。

窗口 `≤0` 等于全员 0 分，退回纯健康度排序；TRAE 无周期概念，恒为 0 分。

**展示与调度指标分开存**（`quota_packages` vs `quota_expiry_ladder`）。管理台需要在凭证行上展开"这个账号有哪些额度包、各自什么时候到期、用了多少"，而 `quota_expiry_ladder` 是为选号设计的指标：结构只有 `[到期, 剩余]` 装不下包名，且 TRAE 必须保持 `None`（填了就会改变选号行为）。因此另存一列 `quota_packages`（`[{"name","total","used","end"}]`，JSON）供管理台悬浮展示：只有展示需要的数据，调度一行不动。两边口径差异也保留：阶梯只收「未过期 + 有余额」的包，而展示明细额外包含「已过期但还有余额」（提醒浪费）与「已用完但仍有效」的包。

**会话粘性**（调度前置一步）。OpenAI 协议本身无会话概念，客户端「对话」的识别按可靠性分两级（B1.5）：

1. **显式会话标识**：`conversation_id` / `conversationId` / `prompt_cache_key`（`metadata` 对象内或请求体顶层），客户端直接给出会话身份，消息被裁剪也能粘住。
2. **回落：消息增量前缀指纹**：以上一轮的完整 messages 为前缀再追加，据此定位上一轮实际服务的凭证。

TTL（`CONVERSATION_STICKY_SECONDS`，默认 1h，≤0 关闭）内固定复用，不再按到期积分/健康度重排——对话中途换号会触发上游风控并丢掉上游侧的提示词缓存。请求体带 `user_id`（顶层或 `metadata` 内）时**不派生**第 2 级兜底键：同一用户的并行对话消息前缀可能相同，派生兜底键会把它们误钉到同一凭证。

> 键名核实状态：`prompt_cache_key`（OpenAI 官方顶层参数）与 `metadata.user_id`（Anthropic Messages API 官方字段）已核实；`conversation_id`/`conversationId`/顶层 `user_id` 非两家标准键，属客户端惯用约定，本机 71 份真实 dump（PI 客户端）中**未观测到**，作为兼容探测接受（命中即用、未命中无害）。

优先级：**手动 pin 优先于粘性**。存在可选（enabled、未禁用、未冷却）的 pinned 凭证时粘性让位，否则管理员显式「指定」会在对话中途无形失效。粘住的凭证报错仍走正常轮换，成功后重新粘到实际服务的凭证。指纹链掺入用户名，防不同用户的相同消息数组串到同一凭证；条目纯内存，重启后丢粘性只影响一轮选号。

**健康度归一化**（Q26 核心）。两者都是积分制，但周期语义不同：

| | CodeBuddy | TRAE |
|---|---|---|
| 剩余 | `CycleCapacityRemainPrecise` | `credits_limit - credits_amount` |
| 总量 | `CycleCapacitySizePrecise` | `credits_limit` |
| 周期 | `CycleStartTime`/`CycleEndTime` | 无（单调余额） |

```python
def health(q) -> HealthScore:   # known(0-100) | unknown | exhausted
    if q is None or q.probe_failed: return "unknown"
    if q.total <= 0:                 return "exhausted"
    return clamp(round(q.remaining / q.total * 100), 0, 100)
```

**为什么必须三态**：CB 允许 bearer-only 手动凭证（无额度信息），探测失败也会发生。若把未知当成 0 分，这类凭证在有健康号时永远轮不到——探测失败被误判为「没额度」。`unknown` 排在 known 之后但仍参与调度；`exhausted` 才是真正的垫底。

**展示层必须标注周期语义**：CB 是「本周期剩余（到期回满）」，TRAE 是「账户剩余（单调递减）」；`unknown` 显示为「未探测到额度」。

**credit 不可作为统计核心指标**：两边上游的 SSE 都不保证返回 per-request credit（CB 的 `usage.credit` 是可选字段，样本中基本不出现；TRAE 只有 `token_usage`）。健康度的唯一可靠来源是额度探测接口的 `remaining`；统计页 credit 只做辅助展示，主指标是 token。

### 4.4 模型名解析（Q21=C + Q27=A）

```
"glm-5.2"          → 健康度路由，自动选 provider
"glm-5.2@trae"     → 强制 TRAE
"glm-5.2@codebuddy" → 强制 CodeBuddy
```

边界行为：

- model 为空或 `"auto"` → 路由到 `DEFAULT_MODEL`（env，默认 `glm-5.2`）
- 未知模型名 → 400 `invalid_request`，不回退到列表首项
- `@` 后缀的 provider 不存在 → 400
- TRAE 动态模型拉取失败 → 回退内置静态模型表，失败负缓存 5 分钟

### 4.5 登录双轨（Q17=C）

```python
class AuthSession(BaseModel):
    flow: Literal["poll", "callback"]
    # poll: 后端轮询上游
    auth_url: str | None
    interval: int | None
    # callback: 浏览器 302 回本服务
    callback_url: str | None
    state: str
```

**回调统一走主端口** `/authorize`，废弃 TRAE 的 18080 独立端口。远程部署只需暴露一个端口。回调地址要写进登录 URL，因此必须可配：`PUBLIC_BASE_URL`（默认 `http://127.0.0.1:8000`），远程部署设为浏览器可达的公网地址。

前端在「凭证管理」页内实现两种 flow（`CredentialsPage` 的 `startLogin`/`cancelLogin`，无独立组件）：
- **CB poll**：拿到 `authUrl` 开新窗 → 后端轮询 `/api/auth/upstream/poll`，得到成功/失败/超时
- **TRAE callback**：开授权窗，浏览器 302 回 `/authorize` 直接落库；前端轮询凭证列表出现新条目即视为完成（上游不回传 state，无法直接轮询登录状态）
- **取消**：重新 `start` 拿 state 后调 `/api/auth/upstream/cancel`

两轨的失败/超时以通知文案呈现（无统一 `failed`/`expired` 状态机）。

### 4.6 中立事件层（Q13=B 预留）

v1 只接 OpenAI 出口，但上游 SSE 解析到「中立事件」这一步独立成层（`Event` 定义见 [TECHNICAL.md §3.1](TECHNICAL.md)）。v1.1 加 Anthropic 出口时，只新增一个 `Event → Anthropic SSE` 适配器，不动上游逻辑。

## 5. 数据模型

- **用户不建表**：`users.txt`（PBKDF2）是唯一源，路径走 `USERS_FILE`（`config.py` 的 `users_file`，默认 `secrets/users.txt`），角色走 `ADMIN_USERNAMES` env；`api_keys.username` 由应用层校验存在性，不加外键
- **API Key 存摘要**：SHA-256，明文仅创建时返回一次
- **凭证加密列**：`data_enc` 走 Fernet，调度状态（`health` / `cooling_until` / `err_count` / `pinned` / `quota_expiry_ladder`）落库，进程重启不丢冷却状态与到期阶梯
- **用量脱敏**：`usage_events`（明细 90 天）+ `usage_hourly`（小时汇总永久），`credit`/`cached_tokens` 可空仅辅助展示
- **成长中心**：`growth_events` 只存汇总行（一轮一行人话汇报 + 积分/能量/连签 + trigger），不存活动内部数据结构；`credentials.growth_last_run_at/growth_last_result` 供列表直接显示；活跃上报（B1.7）复用该表记一行，不新增表
- 签到去重与模型列表缓存均进程内实现，不进库

DDL 以 [src/db/schema.sql](../src/db/schema.sql) 为准，补充实现细节见 [TECHNICAL.md §7](TECHNICAL.md)。

**脱敏纪律**（继承 CB）：不存提示词、回答、请求头、Token、工具参数、原始错误体、会话 ID。

唯一例外：诊断开关 `DUMP_REQUEST_BODIES=true`（默认关）会把 `/v1` 原始请求体落盘到 `data/dumps/`（有界保留 200 份）。这是排查客户端差异的临时手段，**含完整对话内容**，不得长期开启、不得随库交付。

## 6. 目录结构

以 [TECHNICAL.md §2](TECHNICAL.md) 为准（随代码同步维护）。

## 7. API 契约

外部（API Key 鉴权）：`POST /v1/chat/completions`（流式 + 非流式）、`GET /v1/models`（扁平模型名 + `providers` 字段）、`GET /v1/user/balance`（DeepSeek 兼容余额，读探测缓存聚合，不实时打上游）、`GET /health`。

管理台（会话 Cookie）：凭证管理、API Key 管理、用量统计、Playground 等，admin 管凭证与全量统计，普通用户仅见自己的数据。凭证运维端点含 `POST /api/credentials/{id}/checkin`（签到）、`GET|POST /api/credentials/{id}/growth`（成长中心状态与手动执行，仅 CodeBuddy）。回调（无鉴权，TRAE 浏览器 302 不带 key）：`GET /authorize`。

实现以代码为准，使用说明见 [README.md](README.md)。

## 8. 安全边界

沿用 codebuddy2api 的既有约定：

- 上游 endpoint 白名单：**只接受明确配置的地址**，真实 Token 绝不转发到未授权站点
  - CodeBuddy：`CODEBUDDY_API_ENDPOINT` 启动时强制校验，不在白名单直接失败
  - TRAE：凭证 JSON 里的 `apiHost` 是用户可控输入，导入时按官方地址白名单校验，
    不在白名单直接拒绝；旧库里已存的越界 `apiHost` 在刷新/取用户信息前退回官方地址
    （校验在 `TraeClient` 内部，不只 HTTP 边界）
- TLS 校验默认开启，公网部署必须保持
- Host / Origin 白名单，CSP `frame-ancestors`
- 登录三级限流（全局 / IP / 用户名）+ PBKDF2 并发上限
- 请求体上限 16MB，登录接口 8KB（ASGI 层按实际字节计数，`chunked` 不能绕过）
- API Key 仅存摘要，明文只在创建时返回一次
- 凭证内容加密入库，密钥走 `APP_SECRET` env；**密钥丢失 = 已存凭证全部不可解，只能重录**，不做密钥轮换
  - `APP_SECRET` 最短 16 字符，弱密钥拒绝启动
  - 解密失败返回可行动错误码 `credential_decrypt_failed`，不暴露裸 500
- 管理台会话 Cookie `SameSite=Lax` + 写操作自定义头校验（CSRF，含 logout）
- 会话与 API Key 除签名/摘要外**校验用户仍存在于 users.txt**：删用户即失效
- 未匹配的 `/api`、`/v1` 路径返回 JSON 404（不落到 SPA 的 200 + HTML）
- 日志脱敏：不打印 Token、完整请求体
- 审计：凭证增删改、pin、账号切换写 INFO 日志（含操作人）

不做的：mTLS、IP 白名单（交给反向代理）。审计日志只覆盖凭证管理写操作，不做全量请求审计（统计表已是脱敏的请求级记录）。

env 完整清单见 [README.md「配置」](README.md)（以 `src/config.py` 为准）。

## 9. 里程碑

M0 骨架 → M1a TRAE → M1b CB 基础 → M1.5 CB 完整化 → M2 前端 → M3 收尾，**已全部完成**。状态见 [README.md「状态」](README.md)。

## 10. 技术选型

见 [TECHNICAL.md §1](TECHNICAL.md)。

## 11. 风险清单

| 风险 | 等级 | 对策 |
|---|---|---|
| 从零重写丢失旧项目踩坑经验 | 高 | 关键约束抄进 AGENTS.md；M1 结束做双 provider 对比验证 |
| CB 协议复杂度是 TRAE 的 2.4 倍 | 高 | M1 拆成 M1a/M1b/M1.5 串行消化；CB 先 bearer-only 跑通再补 OAuth |
| 上游 credit 不保证可得 | 低 | credit 仅辅助展示；健康度只依赖额度探测接口 |
| 上游 SSE 格式变更 | 中 | 每个 provider 的 SSE 样本存 fixture 做契约测试；解析失败不静默 |
| 抽象设计错误（Q25） | 中 | M0 用 mock provider 先冻结调度接口，100% 覆盖 |
| 模型 ID 撞车导致路由错误 | 中 | 扁平名 + `@provider` 后门；统计行强制带 provider 字段 |
| 积分语义混淆（周期 vs 余额） | 低 | 健康分仅用于调度；展示层标注周期语义 |
| 容器环境特殊（Apple container，无 compose） | 低 | Dockerfile 本地构建验证；compose 靠 CI 验证 |
| License 溯源不全 | 中 | NOTICE 列明四个项目的署名与协议 |

## 12. NOTICE 三方溯源

```
Coding2API
Copyright (c) 2026

本项目从零实现，但在设计与实现上参考了以下项目：

- codebuddy2api - https://github.com/IceeAn/codebuddy2api
  Copyright (c) 2026 An! - MIT License
  （提供 CodeBuddy 上游协议、凭证管理、脱敏统计的设计参考）

- workbuddy-auto-signin - https://github.com/88lin/workbuddy-auto-signin
  Copyright (c) 2026 88lin - MIT License
  （提供 CodeBuddy 成长中心与签到接口的逆向结论与运行经验：端点路径、
   client_token 规则、/redeem 传天数、4xx 业务规则不算失败、
   时间预算与退避重试的教训）

- trae2api-web - https://github.com/connectedGraph/trae2api-web
  Copyright (c) 2026 connectedGraph - MIT License
  （提供 TRAE SOLO 上游协议、账号池冷却状态机的设计参考）

  其上游：
  - xueyue33/codebuddy2api - https://github.com/xueyue33/codebuddy2api
  - Sliverkiss/traework2api - https://github.com/Sliverkiss/traework2api

本项目的代码为独立实现，不复制上述项目的源代码。
上游服务的协议细节来自对客户端行为的观察，不属于上述项目的版权范围。
```
