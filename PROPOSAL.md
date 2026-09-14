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
| Q11 | 前端范围 | 中等 6 页（无设置页，配置走 env） |
| Q12 | 调度策略 | 统一健康度 + 冷却状态机，保留手动 pin |
| Q13 | Anthropic | v1.1，架构预留中立事件层 |
| Q14 | 测试 | 核心路径 100%，其余 70%，契约测试优先 |
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
- 签到：`POST /billing/meter/daily-checkin`
- 请求头需 `X-Domain`、`X-User-Id`、`X-Enterprise-Id`、`X-Department-Info`（部门名须 UTF-8 百分号编码）

### 3.2 TRAE SOLO（字节）

- Agent Host `https://trae-api-cn.mchost.guru`、UG Host `https://api.trae.cn`、OAuth Host `https://api.trae.com.cn`
- 聊天：`POST /api/agent/v3/llm_utils_chat`；模型：`POST /api/ide/v1/get_detail_param`
- 认证：浏览器登录 → 302 回调 `/authorize` → `ExchangeToken` → `GetUserInfo`
- Token 刷新：`POST /cloudide/api/v3/trae/oauth/ExchangeToken`（refreshToken 轮换）
- 签到：`/trae/api/v2/ug/checkin_credits/{status,claim}`；额度：`/trae/api/v2/pay/ide_user_ent_usage`
- SSE 事件序列：`metadata` → `timing_cost` → `output`×N → `extra_info` → `token_usage` → `done`
- `token_usage` 含缓存字段 `cache_read_input_tokens` / `cache_creation_input_tokens`（未命中为 0，非缺失），映射为统计的 `cached_tokens`；**无 per-request credit**
- 错误码 `1005` = 权益不足；仅流式，非流式需聚合

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

### 4.3 调度器（Q12=B + Q26）

统一实现，两个 provider 共用：手动 pin 优先 → 过滤 healthy → 按健康度三态排序（`known 降序 > unknown > exhausted`）→ 无可用返回 None。冷却与错误累计规则见 [TECHNICAL.md §6](TECHNICAL.md)。

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

**回调统一走主端口** `/authorize`，废弃 TRAE 的 18080 独立端口。远程部署只需暴露一个端口。回调地址要写进登录 URL，因此必须可配：`PUBLIC_BASE_URL`（默认 `http://127.0.0.1:8000`），远程部署设为浏览器可达的公网地址。前端一个 `LoginSession` 组件，两种 flow 共用状态机：`pending → success / failed / expired`。

### 4.6 中立事件层（Q13=B 预留）

v1 只接 OpenAI 出口，但上游 SSE 解析到「中立事件」这一步独立成层（`Event` 定义见 [TECHNICAL.md §3.1](TECHNICAL.md)）。v1.1 加 Anthropic 出口时，只新增一个 `Event → Anthropic SSE` 适配器，不动上游逻辑。

## 5. 数据模型

- **用户不建表**：`users.txt`（PBKDF2）是唯一源，角色走 `ADMIN_USERNAMES` env；`api_keys.username` 由应用层校验存在性，不加外键
- **API Key 存摘要**：SHA-256，明文仅创建时返回一次
- **凭证加密列**：`data_enc` 走 Fernet，调度状态（`health` / `cooling_until` / `err_count` / `pinned`）落库，进程重启不丢冷却状态
- **用量脱敏**：`usage_events`（明细 90 天）+ `usage_hourly`（小时汇总永久），`credit` 可空仅辅助展示
- 签到去重与模型列表缓存均进程内实现，不进库

DDL 以 [src/db/schema.sql](../src/db/schema.sql) 为准，补充实现细节见 [TECHNICAL.md §7](TECHNICAL.md)。

**脱敏纪律**（继承 CB）：不存提示词、回答、请求头、Token、工具参数、原始错误体、会话 ID。

## 6. 目录结构

以 [TECHNICAL.md §2](TECHNICAL.md) 为准（随代码同步维护）。

## 7. API 契约

外部（API Key 鉴权）：`POST /v1/chat/completions`（流式 + 非流式）、`GET /v1/models`（扁平模型名 + `providers` 字段）、`GET /health`。

管理台（会话 Cookie）：凭证管理、API Key 管理、用量统计、Playground 等，admin 管凭证与全量统计，普通用户仅见自己的数据。回调（无鉴权，TRAE 浏览器 302 不带 key）：`GET /authorize`。

实现以代码为准，使用说明见 [README.md](README.md)。

## 8. 安全边界

沿用 codebuddy2api 的既有约定：

- 上游 endpoint 白名单：**只接受明确配置的地址**，真实 Token 绝不转发到未授权站点（`CODEBUDDY_API_ENDPOINT` 启动时强制校验，不在白名单直接失败）
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

- trae2api-web - https://github.com/connectedGraph/trae2api-web
  Copyright (c) 2026 connectedGraph - MIT License
  （提供 TRAE SOLO 上游协议、账号池冷却状态机的设计参考）

  其上游：
  - xueyue33/codebuddy2api - https://github.com/xueyue33/codebuddy2api
  - Sliverkiss/traework2api - https://github.com/Sliverkiss/traework2api

本项目的代码为独立实现，不复制上述项目的源代码。
上游服务的协议细节来自对客户端行为的观察，不属于上述项目的版权范围。
```
