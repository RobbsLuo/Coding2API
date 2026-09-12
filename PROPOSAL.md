# Coding2API 实施方案

把 CodeBuddy 与 TRAE SOLO 两个 coding agent 上游通道，统一封装为 OpenAI 兼容 API，并提供公共凭证池、统一调度与按人用量统计。

---

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

---

## 2. 目标与非目标

### 目标

- 单一 OpenAI 兼容端点，后面挂 CodeBuddy 与 TRAE 两个上游
- 凭证由 admin 集中维护，全员共享，调度器自动挑健康的号
- 按人统计用量（请求数、成功率、token、延迟）
- 上游死亡自动冷却，不反复踩死号
- 支持 `model@provider` 精确指定上游

### 非目标（明确不做）

- **不做配额/限流**：上游是订阅制通道，成本不随 token 线性增长；10 人规模靠统计页可见性约束滥用
- **不做通用 provider 网关**：只支持两个上游，硬编码在 registry，不做插件系统
- **v1 不做 Anthropic 协议**
- **不做旧项目数据迁移**
- **不做自更新脚本**
- **不做货币/积分换算**：两个上游的积分单位不互通，分开记录

---

## 3. 关键事实（已核实）

### 3.1 上游协议

**CodeBuddy**（腾讯）
- 端点：`https://copilot.tencent.com`（国际站 `https://www.codebuddy.ai`）
- 聊天：`POST /v2/chat/completions`，**只支持流式**，非流式需本地聚合
- 认证：`POST /v2/plugin/auth/state?platform=CLI` → 拿 `authUrl`/`state` → 轮询 `POST /v2/plugin/auth/token?state=...`（设备码模式）
- 账号切换：`/v2/plugin/login/account`、`/v2/plugin/accounts`
- 额度：个人版 `POST /v2/billing/meter/get-user-resource`（`CycleCapacity*Precise`），企业版 `POST /v2/billing/meter/get-enterprise-user-usage`（`credit` 已用、`limitNum` 总额）
- 签到：`POST /billing/meter/daily-checkin`
- 请求头需 `X-Domain`、`X-User-Id`、`X-Enterprise-Id`、`X-Department-Info`（部门名须 UTF-8 百分号编码）

**TRAE SOLO**（字节）
- Agent Host `https://trae-api-cn.mchost.guru`、UG Host `https://api.trae.cn`、OAuth Host `https://api.trae.com.cn`
- 聊天：`POST /api/agent/v3/llm_utils_chat`；模型 `POST /api/ide/v1/get_detail_param`
- 认证：浏览器登录 → 302 回调 `http://127.0.0.1:<port>/authorize` → `ExchangeToken` → `GetUserInfo`
- Token 刷新：`POST /cloudide/api/v3/trae/oauth/ExchangeToken`（refreshToken 轮换）
- 签到：`/trae/api/v2/ug/checkin_credits/{status,claim}`；额度：`/trae/api/v2/pay/ide_user_ent_usage`
- SSE 事件序列：`metadata` → `timing_cost` → `output`×N → `extra_info` → `token_usage` → `done`
- SSE **只有 `token_usage`，无 per-request credit**
- 错误码 `1005` = 权益不足；上游仅流式，非流式需聚合

### 3.2 冲突与陷阱

| 问题 | 事实 | 对策 |
|---|---|---|
| 模型 ID 撞车 | 两边都有 `glm-5.2`、`DeepSeek-V4-Pro` | 扁平名 + 健康度路由 + `@provider` 后缀 |
| 积分语义不同 | CB 有周期会重置；TRAE 是单调余额 | 健康分统一为百分比，展示层标注周期语义 |
| credit 可得性 | CB 有 per-request；TRAE 只有账户总额 | 统计表 credit 字段 nullable |
| 登录机制 | CB 轮询（后端出网）；TRAE 回调（浏览器可达） | 双轨，回调统一走主端口 |
| 媒体/工具 | 两边 SSE 都含工具调用 | v1 透传，不做语义转换 |

---

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

Provider 承担上游协议私有部分：发请求、解析事件、分类错误，以及凭证生命周期与健康度探测。调度、冷却、重试、统计全在共享引擎。

```python
class Provider(Protocol):
    id: str  # "codebuddy" | "trae"

    # --- 凭证生命周期 ---
    def start_auth(self) -> AuthSession: ...
    def poll_auth(self, state: str) -> AuthResult | None: ...
    def import_credential(self, raw: dict) -> Credential: ...
    def refresh(self, cred: Credential) -> None: ...

    # --- 可选能力：CB 独有的多账号切换；TRAE 返回 NotImplemented ---
    def list_accounts(self, cred: Credential) -> list[Account]: ...
    def switch_account(self, cred: Credential, account_id: str) -> None: ...

    # --- 健康度（调度器唯一依赖） ---
    def health(self, cred: Credential) -> HealthScore:
        """三态：known(0-100) / unknown / exhausted。用于跨 provider 排序。"""

    # --- 执行 ---
    def stream(self, cred: Credential, req: ChatRequest) -> AsyncIterator[bytes]: ...
    def parse_event(self, raw: bytes) -> Event | None: ...
    def classify(self, status: int, body: bytes) -> ErrKind: ...

    # --- 运维 ---
    def probe_quota(self, cred: Credential) -> Quota: ...
    def checkin(self, cred: Credential) -> CheckinResult: ...
    def list_models(self, cred: Credential) -> list[Model]: ...
```

### 4.3 调度器（Q12=B + Q26）

统一实现，两个 provider 共用：

```python
class Scheduler:
    def select(self, model: str, tried: set[str]) -> Credential | None:
        # 1. 按模型名解析候选 provider（name → 全部；name@trae → 仅 trae）
        # 2. 手动 pin 优先（Q12=B 保留）：范围内有 pinned 且 healthy → 直接用
        # 3. 过滤 healthy（非 disabled、非冷却中）
        # 4. 按健康度三态排序：known(按分降序) > unknown > exhausted
        # 5. 无可用 → None

    def pin(self, credential_id: str | None) -> None:
        """手动指定当前凭证；None = 恢复自动路由。"""

    def note_success(self, cred): ...
    def note_error(self, cred, kind: ErrKind):
        # ErrPlan   → 冷却 12h
        # ErrSoft   → 冷却 60s（429/404，不累计错误数）
        # ErrDead   → 硬禁用，需重新登录
        # ErrOther  → 累计，连续 3 次 → 冷却 10m
```

**健康度归一化**（这是 Q26 的核心）。两者都是积分制，但周期语义不同：

| | CodeBuddy | TRAE |
|---|---|---|
| 剩余 | `CycleCapacityRemainPrecise` | `credits_limit - credits_amount` |
| 总量 | `CycleCapacitySizePrecise` | `credits_limit` |
| 周期 | `CycleStartTime`/`CycleEndTime` | 无（单调余额） |

```python
def health(cred) -> HealthScore:  # known(0-100) | unknown | exhausted
    q = cred.quota                  # 后台探测缓存
    if q is None or q.probe_failed:
        return "unknown"            # 探测失败 / bearer-only 无额度信息 ≠ 没额度
    if q.total <= 0:
        return "exhausted"
    return clamp(round(q.remaining / q.total * 100), 0, 100)
```

**为什么必须三态**：CB 允许 bearer-only 手动凭证（无额度信息），探测失败也会发生。若把未知当成 0 分，这类凭证在有健康号时永远轮不到——探测失败被误判为「没额度」。unknown 排在 known 之后但仍参与调度；exhausted 才是真正的垫底。

**展示层必须标注周期语义**：CB 是「本周期剩余（到期回满）」，TRAE 是「账户剩余（单调递减）」；「unknown」显示为「未探测到额度」。

**credit 不可作为统计核心指标**：已核实两边上游的 SSE 都不保证返回 per-request credit（CB 的 `usage.credit` 是上游可选字段，样本中基本不出现；TRAE 只有 `token_usage`）。健康度的唯一可靠来源是额度探测接口的 remaining；统计页的 credit 维度大面积空缺，只做辅助展示，主指标是 token。

### 4.4 模型名解析（Q21=C + Q27=A）

```
"glm-5.2"        → 健康度路由，自动选 provider
"glm-5.2@trae"   → 强制 TRAE
"glm-5.2@codebuddy" → 强制 CodeBuddy
```

`/v1/models` 返回扁平名（去重），附 `providers: ["codebuddy", "trae"]` 字段。

边界行为：

- model 为空或 `"auto"` → 路由到 `DEFAULT_MODEL`（env，默认 `glm-5.2`）
- 未知模型名 → 400 `invalid_request`，不回退到列表首项
- `@` 后缀的 provider 不存在 → 400
- TRAE 动态模型拉取失败 → 回退内置 32 个静态模型表（继承原项目），失败负缓存 5 分钟

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

**回调统一走主端口** `/authorize`，废弃 TRAE 的 18080 独立端口。远程部署只需暴露一个端口。

回调地址要写进登录 URL，因此必须可配：`PUBLIC_BASE_URL`（默认 `http://127.0.0.1:8000`）。远程部署设为公网地址，且浏览器必须可达。

前端一个 `LoginSession` 组件，两种 flow 共用状态机：`pending → success / failed / expired`。

### 4.6 中立事件层（Q13=B 预留）

v1 只接 OpenAI 出口，但上游 SSE 解析到「中立事件」这一步必须独立成层：

```python
@dataclass
class Event:
    kind: Literal["content", "reasoning", "tool_calls", "usage", "done", "error"]
    content: str | None = None
    tool_calls: list | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
```

v1.1 加 Anthropic 出口时，只新增一个 `Event → Anthropic SSE` 适配器，不动上游逻辑。

---

## 5. 数据模型

```sql
-- 用户不建表：users.txt（PBKDF2）是唯一源（沿用 CB 运维方式），角色走 ADMIN_USERNAMES env。
-- api_keys.username 由应用层校验存在性，不加外键。

-- API Key（存摘要，明文仅创建时返回一次）
CREATE TABLE api_keys (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    key_digest  TEXT NOT NULL UNIQUE,
    preview     TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    last_used_at INTEGER
);

-- 凭证（公共池；凭证内容加密存）
CREATE TABLE credentials (
    id            TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,          -- codebuddy | trae
    nickname      TEXT NOT NULL DEFAULT '',
    data_enc      BLOB NOT NULL,          -- 加密的凭证 JSON
    enabled       INTEGER NOT NULL DEFAULT 1,  -- 用户软开关
    disabled      INTEGER NOT NULL DEFAULT 0,  -- session 死亡硬禁用
    disabled_reason TEXT,
    -- 调度状态
    health        INTEGER,              -- NULL=unknown；0-100=known；-1=exhausted
    cooling_until INTEGER,
    err_count     INTEGER NOT NULL DEFAULT 0,
    -- 额度快照（后台探测）
    quota_remaining REAL,
    quota_total     REAL,
    quota_cycle_end INTEGER,              -- CB 有周期；TRAE 为 NULL
    quota_probed_at INTEGER,
    created_at    INTEGER NOT NULL,
    added_by      TEXT                  -- 应用层校验存在于 users.txt，不加外键
);

-- 用量事件（脱敏，90 天）
CREATE TABLE usage_events (
    id           TEXT PRIMARY KEY,
    ts           INTEGER NOT NULL,
    username     TEXT NOT NULL,
    provider     TEXT NOT NULL,
    credential_id TEXT,
    model        TEXT NOT NULL,           -- 归一化后的真实模型
    ok           INTEGER NOT NULL,
    error_type   TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    credit       REAL,                    -- 上游可选字段，两边都经常为 NULL，仅辅助展示
    latency_ms   INTEGER,
    ttfb_ms      INTEGER
);
CREATE INDEX idx_usage_ts ON usage_events(ts);
CREATE INDEX idx_usage_user ON usage_events(username, ts);

-- 小时汇总（永久）
CREATE TABLE usage_hourly (
    hour_utc     INTEGER NOT NULL,
    username     TEXT NOT NULL,
    provider     TEXT NOT NULL,
    model        TEXT NOT NULL,
    requests     INTEGER NOT NULL DEFAULT 0,
    ok_count     INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    credit_sum   REAL,
    credit_known INTEGER NOT NULL DEFAULT 0,
    latency_sum  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_utc, username, provider, model)
);

-- 签到记录（按上游账号去重，避免同号多凭证重复签到）
CREATE TABLE checkins (
    provider     TEXT NOT NULL,
    account_key  TEXT NOT NULL,
    date_local   TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    credit       REAL,
    checked_at   INTEGER NOT NULL,
    PRIMARY KEY (provider, account_key, date_local)
);

-- 模型缓存
CREATE TABLE model_cache (
    provider     TEXT NOT NULL,
    model_id     TEXT NOT NULL,
    fetched_at   INTEGER NOT NULL,
    PRIMARY KEY (provider, model_id)
);
```

**脱敏纪律**（继承 CB）：不存提示词、回答、请求头、Token、工具参数、原始错误体、会话 ID。

---

## 6. 目录结构

```
coding2api/
├── src/
│   ├── main.py                  # FastAPI app 组装
│   ├── config.py                # env + 默认值
│   ├── db/
│   │   ├── schema.sql
│   │   ├── migrate.py
│   │   └── crypto.py            # 凭证字段加解密（密钥走 env）
│   ├── auth/
│   │   ├── users.py             # users.txt + PBKDF2
│   │   ├── session.py
│   │   ├── api_key.py
│   │   └── rbac.py              # admin 判定（ADMIN_USERNAMES env）
│   ├── provider/
│   │   ├── base.py              # Provider 协议、Event、ErrKind
│   │   ├── registry.py
│   │   ├── codebuddy/
│   │   │   ├── client.py
│   │   │   ├── oauth.py         # 设备码轮询
│   │   │   ├── quota.py         # 个人/企业额度
│   │   │   ├── checkin.py
│   │   │   ├── refresh.py       # 多账号切换
│   │   │   └── events.py        # SSE → Event
│   │   └── trae/
│   │       ├── client.py
│   │       ├── credential.py    # 解析 + 原子写回
│   │       ├── callback.py      # 登录闭环
│   │       └── events.py
│   ├── engine/
│   │   ├── scheduler.py         # 健康度选号 + 冷却状态机
│   │   ├── executor.py          # 请求执行 + 轮换重试
│   │   ├── model_resolver.py    # name | name@provider
│   │   └── sse.py
│   ├── compat/
│   │   └── openai/              # v1
│   │       ├── request.py
│   │       └── response.py
│   ├── scheduler/               # 后台任务
│   │   ├── quota_probe.py       # 周期额度探测
│   │   ├── checkin.py
│   │   ├── refresh.py
│   │   └── pacer.py             # 全局节流器
│   ├── stats/
│   │   ├── collector.py
│   │   └── query.py
│   └── api/
│       ├── openai.py
│       ├── admin.py
│       ├── auth.py
│       └── authorize.py         # TRAE 回调落点
├── web/                         # React + Tailwind + shadcn/ui
│   └── src/
│       ├── pages/               # 6 页
│       └── components/
├── tests/
├── deploy/
│   ├── Dockerfile
│   └── docker-compose.yml
├── NOTICE
├── LICENSE
├── README.md                    # 英文
└── README.zh.md                 # 中文
```

---

## 7. API 契约

### 外部（API Key 鉴权）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | 流式 + 非流式 |
| GET | `/v1/models` | 扁平模型名 + `providers` 字段 |
| GET | `/health` | 健康检查 |

### 管理台（会话 Cookie）

| 方法 | 路径 | 权限 |
|---|---|---|
| POST | `/api/auth/login` / `logout` | 全部 |
| GET | `/api/auth/session` | 全部 |
| GET | `/api/credentials` | 全部（脱敏） |
| POST | `/api/credentials` | admin |
| POST | `/api/credentials/{id}/toggle` | admin |
| POST | `/api/credentials/pin` | admin（body: credential_id，null 恢复自动） |
| DELETE | `/api/credentials/{id}` | admin |
| POST | `/api/credentials/{id}/probe` | admin |
| POST | `/api/credentials/{id}/checkin` | admin |
| POST | `/api/auth/upstream/start` | admin |
| GET | `/api/auth/upstream/result` | admin |
| GET | `/api/api-keys` | 全部（自己的） |
| POST | `/api/api-keys` | 全部 |
| DELETE | `/api/api-keys/{id}` | 全部 |
| GET | `/api/stats/overview` | 全部（自己的）；admin 可加 `?username=` 看任何人 |
| GET | `/api/stats/by-provider` | 全部；admin 可跨用户聚合 |
| GET | `/api/stats/timeline` | 全部；按小时的请求量时间序列（实装新增） |
| GET | `/api/stats/model-timeline` | 全部；按模型请求量趋势 Top N（实装新增） |
| GET | `/api/stats/events` | 全部（自己的）；逐请求明细（rowid 游标分页，明细保留 90 天；实装新增） |
| GET/POST | `/api/credentials/{id}/accounts[/select]` | admin；多账号切换（实装新增） |
| POST | `/api/auth/upstream/cancel` | admin；取消进行中的登录（实装新增） |
| GET/POST | `/api/playground/models`、`/api/playground/chat/completions` | 全部（会话鉴权，无需 API Key；实装新增） |

### 回调（无鉴权，TRAE 浏览器 302 不带 key）

| 方法 | 路径 |
|---|---|
| GET | `/authorize` |

---

## 8. 安全边界

沿用 codebuddy2api 的既有约定：

- 上游 endpoint 白名单：**只接受明确配置的地址**，真实 Token 绝不转发到未授权站点
- TLS 校验默认开启，公网部署必须保持
- Host / Origin 白名单，CSP `frame-ancestors`
- 登录三级限流（全局 / IP / 用户名）+ PBKDF2 并发上限
- 请求体上限 16MB，登录接口 8KB
- API Key 仅存摘要，明文只在创建时返回一次
- 凭证内容加密入库，密钥走 `APP_SECRET` env；**密钥丢失 = 已存凭证全部不可解，只能重录**，不做密钥轮换
- 管理台会话 Cookie `SameSite=Lax` + 写操作自定义头校验（CSRF）
- 日志脱敏：不打印 Token、完整请求体

不做的：mTLS、审计日志、IP 白名单（交给反向代理）。

### 配置参考（env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | 必填 | 凭证列加密密钥 |
| `ADMIN_USERNAMES` | 空 | 逗号分隔；空 = 无 admin（凭证只读） |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | TRAE 回调地址前缀，写入登录 URL |
| `CODEBUDDY_API_ENDPOINT` | 中国站 | 上游；只接受白名单内地址 |
| `CODEBUDDY_ALLOWED_ENDPOINTS` | 中国站、国际站 | 真实 Token 可发往的上游白名单 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址与端口 |
| `DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `DEFAULT_MODEL` | `glm-5.2` | model 为空/auto 时的路由目标 |
| `CHECKIN_HOUR` | `9` | 每日签到时刻（服务器本地时区） |
| `QUOTA_PROBE_MINUTES` | `60` | 额度探测周期 |
| `PACER_MIN_SECONDS` / `PACER_MAX_SECONDS` | `5` / `20` | 后台任务随机节流区间 |
| `REFRESH_SKEW_HOURS` | `24` | token 预刷新窗口 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `CODEBUDDY_CHAT_MIN_INTERVAL` | `5` | CB 聊天最小间隔（秒），避频控风控；0 关闭 |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*` | 模型列表黑名单（fnmatch glob）；只影响列表展示 |
| `ALLOWED_HOSTS` | 空 | Host 白名单（防 DNS rebinding）；空 = 本地回环 + PUBLIC_BASE_URL 主机 |
| `DUMP_REQUEST_BODIES` | `false` | 诊断开关：/v1 原始请求体落到 data/dumps/ |

---

## 9. 里程碑

### M0 — 骨架（1 周）

- 项目初始化、配置（env 清单见 §8）、SQLite schema + migration
- 用户体系（`users.txt` + PBKDF2）、会话、API Key、RBAC（`ADMIN_USERNAMES`）
- **Mock Provider + 调度器**（Q25=A 要求先用 mock 验证接口）
- 测试 100% 覆盖（Q14=B 范围）：调度器（冷却状态机、三态健康度排序、轮换、pin）与鉴权

产出：`Scheduler` 可独立验证，接口契约冻结。

### M1a — TRAE 全功能（2 周）

- TRAE provider：客户端、回调登录、token 刷新、额度、SSE 解析
- OpenAI 协议出口
- 执行引擎：请求规范化、轮换重试、错误分类、统计采集（含客户端断连观测）
- 后台任务：额度探测、签到、token 刷新、节流器、usage_events 90 天清理
- 协议适配 SSE 解析 100% 覆盖（fixture 契约测试），其余 70%

产出：curl 走 TRAE 能对话，管理 API 可用。

**拆分原因**：CB 侧仅协议相关代码在原项目就有 3.5K 行（OAuth 801 / 额度 710 / 签到 600 / 刷新 559 / 流处理 896），是 TRAE 全部参考实现（2.9K 行）的 2.4 倍，原计划「3 周并行交付两个 provider 全量功能」不成立，改为串行。

### M1b — CodeBuddy 基础（2 周）

- CB provider：客户端、bearer-only 手动凭证导入、聊天、个人版额度探测、SSE 解析

产出：curl 走两个上游都能对话（CB 暂用手动粘贴的 token）。

**验证点**：同一请求分别走两个 provider，对比调度行为、统计字段、错误分类是否一致。若抽象需要修改，此时改成本最低。

### M1.5 — CodeBuddy 完整化（2 周）

- CB OAuth 设备码轮询登录（801 行参考实现）
- 多账号切换、企业版额度、签到

### M2 — 前端（2 周）

React + Tailwind + shadcn/ui，6 页（对齐 Q11=B：无设置页，运行配置走 env）：

1. 登录
2. 凭证管理（列表、启停、导入、登录入口、pin 入口）
3. 池仪表盘（健康度三态、冷却中的账号、周期语义标注）
4. API Key 管理
5. 用量统计（按人、按 provider）
6. Playground（调试用）

### M3 — 收尾（1 周）

- 中文文档 + 英文 README
- Dockerfile + compose，CI 跑测试 + lint + compose 启动验证
- NOTICE 三方溯源
- 集成测试：端到端冒烟

**总计约 10 周**（单人全职）。M1.5 与 M2 有重叠窗口（前端联调期间可推进 CB OAuth），乐观 8 周。

---

## 10. 技术选型（已核实版本）

| 层 | 选型 | 版本 |
|---|---|---|
| 运行时 | Python | 3.14（要求 ≥3.12） |
| Web | FastAPI + Uvicorn | 最新稳定 |
| HTTP 客户端 | httpx（异步） | `trust_env=False` 避免 SOCKS 代理破坏启动 |
| 存储 | SQLite（WAL 模式） | 标准库 sqlite3 |
| 前端 | React | 19.3 |
| 样式 | Tailwind CSS | 4.3 |
| 组件 | shadcn/ui（Radix 底座） | shadcn 4.21 / Radix 1.1 |
| 构建 | Vite | 最新 |
| 测试 | pytest + pytest-cov + respx（HTTP mock）+ 契约 fixture | |

---

## 11. 风险清单

| 风险 | 等级 | 对策 |
|---|---|---|
| 从零重写丢失旧项目踩坑经验 | 高 | 把两个项目的 AGENTS.md / RESEARCH.md 关键约束抄进 CLAUDE.md；M1 结束做双 provider 对比验证 |
| CB 协议复杂度是 TRAE 的 2.4 倍（OAuth 801 + 额度 710 + 签到 600 + 刷新 559 行） | 高 | M1 拆成 M1a/M1b/M1.5 串行消化；CB 先 bearer-only 跑通再补 OAuth |
| 上游 credit 不保证可得 | 低 | credit 仅辅助展示；健康度只依赖额度探测接口 |
| 上游 SSE 格式变更 | 中 | 每个 provider 的 SSE 样本存 fixture 做契约测试；解析失败不静默 |
| 抽象设计错误（Q25） | 中 | M0 用 mock provider 先冻结调度接口，100% 覆盖 |
| 模型 ID 撞车导致路由错误 | 中 | 扁平名 + `@provider` 后门；统计行强制带 provider 字段 |
| 积分语义混淆（周期 vs 余额） | 低 | 健康分仅用于调度；展示层标注周期语义 |
| 容器环境特殊（Apple container，无 compose） | 低 | Dockerfile 本地 `container build` 验证；compose 靠 CI 验证 |
| License 溯源不全 | 中 | NOTICE 列明四个项目的署名与协议 |

---

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

---

## 13. 待办（开工前）

- [ ] 确认 GitHub 仓库名 `coding2api` 未占用（已核实：空闲）
- [ ] `container system start` + `container system kernel set --recommended`（M3 需要）
- [ ] 准备两个上游的有效凭证用于联调
- [ ] 决定 `ADMIN_USERNAMES` 初始值
