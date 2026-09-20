# Coding2API 技术方案

PROPOSAL.md 定方向，本文档定实现。每个模块标注来源决策（Q 编号）。

---

## 1. 技术栈定稿

| 层 | 选型 | 版本 | 决策 |
|---|---|---|---|
| 运行时 | Python | 3.14（≥3.12） | Q2 |
| 包管理 | uv（venv + pyproject.toml + uv.lock） | 本机 0.12.12 | T-Q1 |
| Web | FastAPI + Uvicorn | 最新稳定 | Q2=A |
| 配置 | pydantic-settings | 最新稳定 | T-Q3 |
| HTTP | httpx 双客户端（流式/短请求分离） | 0.28.1 | T-Q4 |
| 数据库 | 标准库 sqlite3（WAL）+ 手写 SQL | 内置 | T-Q2 |
| 加密 | cryptography Fernet（凭证列） | 50.x | T-Q2 |
| 测试 | pytest + pytest-cov + respx + 文件化 fixture | respx 0.23.1 | T-Q5 |
| 前端 | React 19.3 + Tailwind 4.3 + shadcn/ui 4.21 + @lobehub/icons + Vite | 已核实 | Q5 |

---

## 2. 目录结构与模块规格

```
coding2api/
├── pyproject.toml               # uv 项目；[tool.pytest.ini_options] 设 coverage 目标
├── src/
│   ├── main.py                  # FastAPI 组装、lifespan、路由挂载（只做接线）
│   ├── config.py                # pydantic-settings：README「配置」全部 env
│   ├── webapp/                  # HTTP 边缘层（横切关注点，与业务装配分开）
│   │   ├── limits.py            # 请求体上限 ASGI 中间件（登录 8KB / 其余 16MB）
│   │   ├── security.py          # Host 白名单 + 安全响应头（CSP/nosniff）
│   │   ├── handlers.py          # 异常 → HTTP 响应（稳定错误码，TECHNICAL §6.5）
│   │   ├── logging.py           # root logger 配置（审计/上游日志落 stderr）
│   │   └── static.py            # 前端产物定位 + SPA catch-all
│   ├── db/
│   │   ├── schema.sql           # DDL 定稿
│   │   ├── conn.py              # 连接管理（线程本地 + WAL + busy_timeout）
│   │   ├── migrate.py           # 启动时执行 schema.sql（CREATE IF NOT EXISTS）
│   │   ├── repo.py              # 凭证/API Key 持久化（手写 SQL）
│   │   └── crypto.py            # Fernet：APP_SECRET → key derive → encrypt/decrypt
│   ├── auth/
│   │   ├── users.py             # users.txt 解析（username:PBKDF2），hash_password.py CLI
│   │   ├── session.py           # 会话 Cookie 签发/校验（itsdangerous 或手写 HMAC）
│   │   ├── api_key.py           # sk- 生成（secrets）、SHA-256 摘要存储、常量时间校验
│   │   ├── csrf.py             # 写操作 CSRF 校验（自定义头 / 同源 Origin）
│   │   └── throttle.py         # 登录限流（三级窗口 + PBKDF2 并发上限）
│   │   └── rbac.py              # ADMIN_USERNAMES 判定；require_admin 依赖
│   ├── provider/
│   │   ├── base.py              # Provider 协议、Event、ErrKind、Quota、HealthScore
│   │   ├── codebuddy/
│   │   │   ├── client.py        # 上游 HTTP + SSE 流 + 额度探测
│   │   │   ├── events.py        # OpenAI 风格 SSE → Event
│   │   │   ├── credential.py    # 凭证类型与解析
│   │   │   ├── headers.py       # 上游技术常量与请求头构造
│   │   │   ├── oauth.py         # 设备码轮询
│   │   │   ├── checkin.py       # 签到 + 签到状态（连续天数）
│   │   │   ├── growth.py        # 成长中心协议层：15 个端点的请求与解析
│   │   │   ├── growth_runner.py # 成长中心编排：7 类领取的顺序与失败判定
│   │   │   └── refresh.py       # token 刷新 + 多账号切换
│   │   ├── trae/
│   │   │   ├── client.py        # SOLO 上游 + 双 httpx 客户端 + 额度探测
│   │   │   ├── events.py        # 自定义 SSE → Event
│   │   │   ├── credential.py    # 凭证解析（嵌套/扁平）+ 原子写回 + 签到设备号生成
│   │   │   └── callback.py      # 登录 URL 构造 + 回调解析
│   │   └── fixtures/            # fixture 清单
│   │       ├── codebuddy/*.sse
│   │       └── trae/*.sse
│   ├── engine/
│   │   ├── scheduler.py         # 选号 + 冷却状态机 + pin
│   │   ├── executor.py          # 请求执行 + 轮换重试 + 统计埋点
│   │   ├── affinity.py          # 会话粘性：对话前缀指纹 → 固定凭证（CONVERSATION_STICKY_SECONDS）
│   │   ├── model_resolver.py    # "glm-5.2" | "glm-5.2@trae" | auto → 候选集
│   │   └── sse.py               # SSE 帧解析（跨 provider 共用）
│   ├── compat/
│   │   └── openai/
│   │       ├── request.py       # ChatRequest 校验 + 上游 payload 构造
│   │       ├── response.py      # 流式 chunk 生成 + 非流式聚合
│   │       └── errors.py        # OpenAI error shape
│   ├── tasks/
│   │   ├── pacer.py             # 全局节流器（PACER_MIN/MAX 随机区间）
│   │   ├── quota_probe.py       # 启动立即跑一轮 + 每 QUOTA_PROBE_MINUTES 分钟探测
│   │   ├── checkin.py           # 全天每 10 分钟签到；成功即当日封账该凭证
│   │   ├── growth.py            # 成长中心（仅 CB）：GROWTH_INTERVAL_MINUTES 一轮，落 growth_events
│   │   ├── refresh.py           # 每 60 分钟；REFRESH_SKEW_HOURS 窗口内预刷新
│   │   ├── retention.py         # 每 5 分钟：小时汇总重算（幂等）+ 90 天前明细清理
│   │   └── runner.py            # 后台任务调度，接入应用生命周期
│   ├── stats/
│   │   ├── collector.py         # usage_events 写入（脱敏）+ 小时汇总双写/重算
│   │   └── query.py             # overview / by-provider / timeline / events 查询（前者读小时汇总，events JOIN credentials 带凭证昵称）
│   └── api/
│       ├── deps.py              # Services 容器 + require_api_key / session / csrf 依赖
│       ├── chat.py              # POST /v1/chat/completions
│       ├── models.py            # GET /v1/models（动态拉取 + 黑名单 + 缓存兑底 + 元数据）
│       ├── balance.py           # GET /v1/user/balance（DeepSeek 兼容余额，读探测缓存聚合）
│       ├── authorize.py         # GET /authorize（TRAE 回调落点）
│       ├── admin_credentials.py # 凭证 CRUD / toggle / pin / probe / checkin / 成长中心 / 账号切换
│       ├── admin_keys.py        # API Key CRUD
│       ├── admin_stats.py       # 统计查询（overview / by-provider / timeline / model-timeline）
│       ├── admin_auth.py        # 登录 / 登出 / 会话；上游登录 start/poll/cancel
│       ├── playground.py        # 会话调试端点（无需 API Key）
│       └── streaming.py         # SSE 流包装（长空隙插心跳帧）
├── web/                         # React 前端（M2）
├── tests/
├── Dockerfile / docker-compose.yml  # 仓库根（compose build context 依赖根目录）
├── NOTICE / LICENSE / README.md（中文）/ README.en.md
```

---

## 3. 核心类型定义

### 3.1 中立事件层（engine/provider/base.py，Q13=B 预留）

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
    credit: float | None = None    # 上游可选字段，两边都经常为 None
```

事件名过滤：TRAE 上游会发 `metadata` / `timing_cost` / `extra_info` / `progress_notice`
等无下游语义的事件，`events.py` 的 `KNOWN_EVENT_NAMES` 之外的名字直接跳过。
「解析失败不静默」（PROPOSAL §11）针对的是**已知事件的畸形数据**：归一集内的事件
JSON 非对象/字段类型不对仍然抛 `UpstreamProtocolViolation`，不退回默认值。

### 3.2 错误分类（决定冷却时长，Q12=B）

```python
class ErrKind(StrEnum):
    PLAN = "plan"        # 权益耗尽 → 冷却 12h
    SOFT = "soft"        # 限流/404 → 冷却 60s，不累计错误数
    DEAD = "dead"        # session 失效 → 硬禁用 disabled=1
    OTHER = "other"      # 其他 4xx/5xx → 累计，连续 3 次 → 冷却 10m
    INVALID = "invalid"  # 请求无效（模型不存在等）→ 不冷却凭证，跳过该上游；全拒 → 400
```

| 上游信号 | CB | TRAE | ErrKind |
|---|---|---|---|
| 权益耗尽 | `code=1005`/plan 相关 | `"code":1005` | PLAN |
| 限流 | 429 | 429 | SOFT |
| 不存在 | 404 | 404 | SOFT |
| 会话失效 | 401/403 | 401 + login 标记 | DEAD |
| 服务端错误 | 5xx | 5xx | OTHER |
| 请求自身无效 | 400 | 400 | INVALID |
| 流内错误事件 | SSE error 事件 | `event:error` code=1005→PLAN | 同上映射 |

### 3.3 三态健康度（Q26/A）

```python
@dataclass(slots=True)
class Quota:
    remaining: float | None = None
    total: float | None = None
    cycle_end: int | None = None      # 最早到期 epoch（CB 多包各自独立）；TRAE None
    expiry_ladder: list[tuple[int, float]] | None = None  # [(到期 epoch, 该包剩余积分)]
    probed_at: int | None = None
    probe_failed: bool = False

type HealthScore = int | None        # 0-100 known；None unknown；-1 exhausted

def health(q: Quota | None) -> HealthScore:
    if q is None or q.probe_failed or q.total is None or q.total <= 0:
        return None if (q is None or q.probe_failed) else -1
    return max(0, min(100, round(q.remaining / q.total * 100)))
```

调度排序：`known DESC > unknown（中性参与）> exhausted(-1)`。

---

## 4. Provider 协议（Q16=A 细接口）

```python
class Provider(Protocol):
    id: ClassVar[str]

    # 凭证生命周期（上游协议私有，必然在 provider 内）
    def start_auth(self) -> AuthSession: ...            # flow=poll|callback
    def poll_auth(self, state: str) -> AuthResult | None: ...
    def complete_callback(self, url: str) -> AuthResult: ...  # 仅 trae
    def import_credential(self, raw: dict) -> Credential: ...
    def refresh(self, cred: DecryptedCred) -> None: ...

    # 可选能力（CB 独有：多账号切换）
    def list_accounts(self, cred: DecryptedCred) -> list[Account]: ...
    def switch_account(self, cred: DecryptedCred, account_id: str) -> None: ...

    # 额度探测（调度器依赖：健康度 + 到期阶梯）
    async def probe_quota(self, cred: DecryptedCred) -> Quota: ...

    # 执行
    def stream_chat(self, cred: DecryptedCred, req: ChatRequest) -> AsyncIterator[Event]: ...
    def classify(self, status: int, body: bytes) -> ErrKind: ...

    # 运维
    def checkin(self, cred: DecryptedCred) -> CheckinResult: ...
    def list_models(self, cred: DecryptedCred) -> list[Model]: ...
```

约定：
- `DecryptedCred` = db 取出 `data_enc` → Fernet 解密后的 dataclass；provider 不接触 sqlite
- `stream_chat` 只产出 `Event`，产出前先做 HTTP 状态码检查；`classify` 由 executor 调用
- 可选能力**不实现即不定义**（不是抛 `NotImplementedError`）：调用方用
  `getattr`/`hasattr` 探测，缺失时返回 400「该凭证不支持此操作」，而不是 500。
  当前可选集：`start_auth`/`poll_auth`（仅支持 poll 的 provider 才有）、
  `complete_callback`（仅 TRAE）、`list_accounts`/`switch_account`（仅 CodeBuddy）、
  `credential_from`/`checkin_scope`（刷新与签到任务的能力探测）
- 两个 provider 共用 `engine/sse.py` 的帧解析器（SSE 规范层），事件语义各自映射

---

## 5. 请求时序（chat completions 主链路）

```
客户端 → POST /v1/chat/completions (Bearer sk-)
  1. deps.require_api_key：摘要查 api_keys 表 → username
  2. request.py：校验 body → ChatRequest；model_resolver 解析候选集
     - "glm-5.2" → 两 provider 都可能；"glm-5.2@trae" → 仅 trae；auto/空 → DEFAULT_MODEL
  3. 选号（executor._pick → scheduler.select）：
     a. 候选 = 注册表中支持该模型的 provider（模型目录能证明归属时先收窄，见 _narrow_providers）
     b. 会话粘性：存在可选的 pinned 凭证时跳过（pin 优先），否则指纹命中的
        凭证仍可选时直接复用，不参与排序（CONVERSATION_STICKY_SECONDS，≤0 关闭）
     c. pin 优先：pinned 凭证属于候选 provider 且 healthy → 直接用
     d. 过滤 healthy（enabled=1, disabled=0, 非冷却中）
     e. 到期积分排序：把 quota_expiry_ladder 中「距到期 ≤ QUOTA_EXPIRY_WINDOW_SECONDS」
        （默认 36h）的积分加总，多的先用（避免积分过期浪费）；无周期信息
        （TRAE/企业版）计 0 分；窗口 ≤0 时全员 0 分，等于关闭该指标
     f. 到期积分相同时按 health 三态排序取最高分；同分按 credential_id 稳定
  4. executor：解密凭证 → provider.stream_chat()
     - 上游 HTTP ≥400 → classify → scheduler.note_error → tried 加入 → 回到 3（最多 3 次轮换）
     - 流内 Event.ERROR → 同上映射 → 注入 OpenAI SSE 错误帧 + 冷却 + 轮换
     - 上游 400（INVALID，如模型不存在）不冷却凭证，跳过该上游全部凭证；
       全部拒绝时 400 invalid_request（未知模型名 ≡ 无上游提供，不走 503）
  5. response.py：Event → OpenAI chunk（流式）或聚合（非流式）
     - 首块补 role:assistant；上游无 index 的 tool_calls 补稳定 index
  6. stats.collector：写 usage_events（username/provider/model/tokens/latency/ttfb/ok；latency=端到端耗时，ttfb=首字延迟）
  7. scheduler.note_success：清 err_count，并把本对话重新粘到实际服务的凭证
```

客户端断连：生成器被关闭/取消时统计 `error_type=client_disconnect`，关闭上游流；
例外：`[DONE]` 已产出后的收尾断开（客户端拿到回调即关连接，框架在结束帧
`more_body=False` 前有一拍竞态会把它判为断开）按成功记账，避免误标。

---

## 6. 调度器规格（Q12=B + Q26 + Q31）

```python
class Scheduler:
    MAX_ROTATE = 3
    EXPIRY_WINDOW = 36h          # 到期积分排序窗口，QUOTA_EXPIRY_WINDOW_SECONDS 覆盖；≤0 关闭
    COOLDOWN = {ErrKind.PLAN: 12h, ErrKind.SOFT: 60s, ErrKind.OTHER: 10m}
    ERR_THRESHOLD = 3          # 连续 OTHER 错误 → 冷却

    def select(self, model: str, tried: set[str]) -> DecryptedCredRef | None: ...
    def note_success(self, cred_id: str) -> None: ...
    def note_error(self, cred_id: str, kind: ErrKind) -> None: ...
    def pin(self, credential_id: str | None) -> None: ...
```

状态全部落 `credentials` 表（`cooling_until` / `err_count` / `health` / `disabled` / `quota_expiry_ladder`），进程重启不丢冷却状态。写路径无应用层锁：并发写靠 SQLite WAL + `busy_timeout=5000` 串行化；每个写方法走 `Database.transaction()` 上下文（正常提交、异常回滚），不再散落 `connect()/commit()` 样板。

到期积分只算一处：`expiring_credits()`。选号走 `Candidate.expiry_credits()`，管理台列表走 `GET /api/credentials` 的 `quota_expiring_credits`（窗口值随响应返回 `expiry_window_seconds`），两处共用同一实现，界面数字与选号顺序不会漂移；渠道无到期信息时返回 `null`（不显示），窗口关闭或确实无积分临近过期时返回 `0`（同样不显示）。

---

## 6.1 后台任务（tasks/）

`TaskRunner`（runner.py）每类任务一个独立 asyncio 循环，失败只记日志不拖垮服务；间隔有安全下限，避免打爆上游。所有对外 HTTP 请求经 `pacer.py` 全局节流。

| 任务 | 周期 | 行为 |
|---|---|---|
| 额度探测（quota_probe.py） | 启动立即跑一轮（不节流） + 每 `QUOTA_PROBE_MINUTES`（默认 60）分钟 | 探测上游剩余额度 → `credentials.quota_*` / `quota_expiry_ladder` / `health` 写回 |
| token 预刷新（refresh.py） | 每 60 分钟 | 到期前 `REFRESH_SKEW_HOURS`（默认 24h）窗口内轮换 refresh token |
| 每日签到（checkin.py） | 每 10 分钟（全天） | 成功即封账该凭证当日（`日期:scope`，进程内内存态，重启重建）；失败凭证持续重试，同账号多凭证共享一次 |
| 成长中心（growth.py） | 每 `GROWTH_INTERVAL_MINUTES`（默认 60，下限 5 分钟） | 仅 CodeBuddy：领旅行礼物 / 派 Buddy / 领取新任务 / 领任务奖 / 断登补登 / 连登兑换 / 开盲盒 / Buddy 盲盒；结果落 `growth_events` + 回写 `credentials.growth_last_result` |

**签到 / 成长中心的「同账号」隔离键**：`checkin_scope(data) or f"credential|{credential_id}"`。
provider 在身份未知时返回空串（CB 的 `checkin_scope_key` 在 `account_uid` 与
`user_id` 都为空时返回 `""`），任务层必须回落到 `credential_id`。这不是保守取值：
共享空 scope 会让第二个账号被 `seen` 集合永久跳过，表现为「只有第一个凭证被自动签到」，
且没有任何报错。回落到凭证 ID 最坏只是多签一次（上游签到幂等，返回 ALREADY）。

**成长中心的失败判定**（三层分开，勿合并）：
- 协议层（`growth.py`）：非 2xx 抛 `GrowthRejected`；业务码非 0 / 缺 data / 非 JSON 抛
  `UpstreamProtocolViolation`（HTTP 200 也可能是失败，只看状态码会把失败当成功）
- 编排层（`growth_runner.py`）：4xx 业务规则（名额用完、未解锁、抽奖没次数、
  能量不足）记为 `IDLE` 而非 `FAILED`；5xx 与协议违规记 `FAILED`；401/403 立即置
  `session_dead` 并停止后续请求（再打只会一路 401）
- 结论：`failed 且 gained=False` 才算整体失败。部分成功仍是成功——上游某个接口抖动
  不该让「今天领到 300 积分」变成一张红牌，否则定时任务天天报红，真故障被淹没

**接单失败要分三类，不能一律记 FAILED**（实测一个新账号的报告里刷出 17 条
`prerequisite not met: first_buddy`，把「其实只需做一件事」淹没了）：

| 上游返回 | 处置 |
|---|---|
| `prerequisite not met: <task_code>` | 前置任务未完成。**按原因归并成一条**并标 `reportable=True`（用户需要知道去做什么），记 IDLE 不算失败 |
| `task does not require acceptance` | 正常应答（该任务不需要接单），**不产生任何步骤** |
| 其余 | 逐条记 FAILED（真需要人看的失败） |

`GrowthStep.reportable` 控制该步是否进「一行汇报」：默认只收 DONE/FAILED，
但 IDLE 里若有**用户需要动手**的事项（前置任务受阻、尚未领取 Buddy）必须显式
置 True 带进汇报——否则用户只看到「接单完成 共 1 个」，看不到还有 17 个被门住。

同理，`no active buddy`（400）是账号状态（新账号还没 Buddy）而非故障，记 IDLE。

**任务契约是五态，不是三态**（2026-09 桌面端成长中心 H5 `growthSpace` chunk 读出，
被上游改版坑过一次，勿按直觉回退）：

| accept_status | 该做什么 |
|---|---|
| `not_accepted` | **接单**（进度从这一刻才开始计） |
| `accepted` / `in_progress` | 什么都不做（等用户完成） |
| `completed` | **领奖** |
| `claimed` | 跳过 |

- 接单：`POST /tasks/accept`，body 是**复数数组** `{"task_codes": [...]}`；旧的单数
  `{"task_code": x}` 在新服务端一律 400。逐条结果在 `data.results`，失败（如
  `prerequisite not met: first_buddy`）必须上报
- 领奖：`POST /tasks/{task_code}/claim`（body 空），**不再走 accept**；回包
  `already_claimed` 为真时不重复计分
- 把 `not_accepted` 当成「已接单」跳过，会让所有新任务永远既不接单也不领奖
  （实测一个账号积压 5 个任务共 650 积分未领）

**连登兑换的 `tier` 是档位标识**（`"7d"` / `"14d"` / `"28d"`），权威来源是
`GET /streak` 的 `redemption_status.tiers[].tier`。传天数或档位名分别得到
`invalid request` / `unknown tier`。实发字段是 `*_granted`
（`credit_granted` / `energy_granted` …），读裸 `credit` 恒为空、会漏计全部兑换所得。

**403 不总是登录失效**：未解锁档位返回 403 + 「连续登录天数不足」，必须先于
session 判定处理，否则整轮成长中心会被误报成「登录态已失效」并中止。
只有 401/403 且不是「天数不足」才置 `session_dead`。

不可逆动作（抽奖 / 连登兑换 / 开 Buddy 盲盒 / 消耗补登卡）由
`GROWTH_IRREVERSIBLE_ACTIONS` 总开关控制，**手动入口与定时任务读同一个开关**——
否则「保守部署」只挡得住定时任务。开关只挡「消耗」，不挡「查询」：
`/streak` 是只读接口，关掉开关时仍要打（否则连签展示会静默变成 `null`）。

**两个 `streak_days` 不是同一个数**（实测同一天分别是 5 与 1），不要合并展示：

| 来源 | 字段 | 含义 |
|---|---|---|
| 签到接口 `checkin-activity-status` | `streak_days` | 每日签到连签（有 `checkin_dates` 逐日佐证，语义确定） |
| 成长中心 `/v2/activity/growth/streak` | `streak.days` | 连登天数：**只计官方客户端（桌面端 / CLI）的实际使用**，且**含 1 天容忍窗口**（`days = 使用天数 + 1`），每月清零 |

官方规则原文（桌面端成长中心 H5 `GrowthSpace` chunk）：

> 连登天数每月清零，计算方式为当前日期前连续登录且使用的天数，含 1 天容忍窗口；
> 断登前的连续登录天数不作为可用连登天数，如有需要可通过补登卡来补救
> 兑换需手动发起，每档每月限兑 1 次，兑换不消耗连登天数

**推论（已实测佐证）**：代理层的 API 转发不计入连登——我们的请求再多也刷不动它
（实测同期转发 42 次，`days` 不变）。账户只能靠用户在官方客户端里使用来涨连登，
自动化能做的只有「别让它断」（补登卡）。

成长中心汇报文案用官方术语「连登 N 天」（与签到接口的「连签」区分）。注意这个值
不能直接读成客户端使用天数：它含 1 天容忍窗口（`连登 = 使用天数 + 1`）。

**活跃地图（热力墙）分档**（H5 `GrowthSpace` chunk 的 `Ae()` 与 `zt[]`）：

| score | 档位 | 文案 |
|---|---|---|
| `<= 0` | 无活跃 | 尚未登场 |
| `1–10` | 轻度 | 轻轻路过 |
| `11–30` | 中度 | 持续输出 |
| `31–60` | 高效 | 效率拉满 |
| `> 60` | 极高 | 卷王模式 |

数据**每日凌晨 02:00 更新**（缓存里看到的 23:15/23:18 是写入时间，不是批算时刻；
`today` 字段是实时的，操作完立刻变）。score 同样只计官方客户端行为：实测桌面端发
一句话 → `today.score` 由 0 变 2，同期我们的 42 次代理转发一分未计。
**与积分无关，不参与调度决策。**
| 明细清理（retention.py） | 每 5 分钟 | `usage_events` 全量重算小时汇总（幂等 upsert，与 record 的增量双写对账）+ 90 天前明细清理 |

**清理切点必顶对齐到小时边界**（`purge_expired`）。这不是保守取值而是正确性要求：
`rollup_hourly` 对整行是 REPLACE 语义，只有保证「仍有明细的小时保有全部明细」
重算才精确；若把边界小时只删一半，下一轮 rollup 会把汇总行覆盖成剩下那一半，
被删部分永久丢失（明细已不在）。代价：明细最多多留 1 小时。

---

## 6.5 面向用户的错误语义

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

DDL 以 src/db/schema.sql 为准（users.txt 为用户唯一源、无 users 表、凭证加密列、usage_events.credit 可空），补充实现细节：

```sql
-- conn.py 打开时执行
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;      -- api_keys 之外无外键（users.txt 无表）
```

- 连接：`threading.local()` 每线程一个 `sqlite3.Connection(row_factory=sqlite3.Row)`；引擎与 FastAPI 线程池各自持有自己的连接。写入统一走 `Database.transaction()`（`conn.commit()` / 异常 `rollback()`），没有应用层写锁，并发由 SQLite 自身串行化（WAL + `busy_timeout=5000` 下短写足够）
- 加密：`Fernet(base64.urlsafe_b64encode(sha256(APP_SECRET).digest()))`；APP_SECRET 丢失 = 凭证全部不可解，只能重录（Q13 已明示）
- migration：启动时读 `schema.sql` 逐条 `CREATE TABLE IF NOT EXISTS`（只加不改，列注释可改）；新增列写进 `migrate._MIGRATION_COLUMNS` 走 `ALTER TABLE ... ADD COLUMN`（重复列名忽略，老库幂等补列），删表写进 `migrate._MIGRATION_DROPS` 走 `DROP TABLE IF EXISTS`（`CREATE TABLE IF NOT EXISTS` 对老库无效，不给删会遗留死表），同时 `SCHEMA_VERSION + 1`，版本记在 `PRAGMA user_version`

---

## 8. 测试策略（Q14=B + T-Q5）

| 目标 | 覆盖 | 方式 |
|---|---|---|
| 调度器（到期指标/冷却/三态排序/轮换/pin） | 100% | 纯单元，注入假 provider |
| 鉴权（users/apikey/session/rbac） | 100% | 单元 + FastAPI TestClient |
| SSE 帧解析 | 100% | fixture 契约测试 |
| provider 事件映射 | 100% | fixture：真实 SSE 样本 → Event 断言 |
| OpenAI 协议适配 | 100% | 流式/非流式/工具调用 fixture |
| OAuth/回调解析 | 100% | fixture：真实回调 URL/state 响应 |
| HTTP 客户端 | ~ | respx mock 状态码 + body → classify 断言 |
| 统计/查询 | 70% | sqlite 内存库集成 |
| 其余 | ≥70% | — |

fixture 存于 `src/provider/fixtures/`（真实 SSE/JSON 样本，覆盖正文、思考、工具调用、错误码与额度）。

fixture 断言两个方向：**解析正确**（样本 → 期望 Event）与**不静默**（畸形样本 → UpstreamProtocolViolation）。

---

## 9. 已知取舍备忘

- **同步 sqlite3 而非 aiosqlite**（T-Q2）：本地微秒级操作，asyncio 封装开销大于收益
- **双 httpx 客户端**（T-Q4）：聊天流 read=None 防长流截断；短请求总超时 30s 防悬挂；共享 `trust_env=False`
- **手写 SQL 而非 ORM**：8 张表规模下 ORM 收益为负
- **polling OAuth 不转回调**（Q17=C）：上游协议决定；TRAE 回调走主端口 + PUBLIC_BASE_URL
- **v1 无 Anthropic**（Q8=A）：Event 层已预留，v1.1 只加 `compat/anthropic/` 适配器
- **统计一律以 `usage_hourly` 为准**：`overview` / `by_provider` / `timeline` /
  `model-timeline` 均读小时汇总，只有 `events`（逐请求明细）读 `usage_events`。
  统一口径是为了让选「全部」时总览与图表同值（明细只留 90 天，汇总永久）
- **小时汇总双写**：`record()` 写入明细的同时增量累加当前小时行，所以新请求
  立即可见于统计页（不依赖 5 分钟一轮的 rollup）；`rollup_hourly` 仍每 5 分钟
  全量重算作对账，两者结果一致（幂等）
- **延迟均值只算成功请求**：分子 `SUM(latency_ms WHERE ok=1)` 与分母 `ok_count`
  配对；失败请求的耗时不能拉偏「典型耗时」（与图表口径一致）
- **应用日志只写 stderr，轮转交给平台**：不在应用内开文件、不用
  `RotatingFileHandler`。理由：三种部署形态（launchd / systemd / docker）的
  采集方式不同但都靠 stdout/stderr 对接；应用自己写文件会与平台轮转争抢同一个
  文件，容器里还会写进镜像层（重启即丢且 `docker logs` 看不到）。
  各自配置见 `deploy/`（newsyslog / logrotate / systemd）与 compose 的 `logging` 段
- **必须在 build_app 里配 root logger**：uvicorn 默认 `LOGGING_CONFIG` 只配
  `uvicorn` / `uvicorn.access`（`propagate=false`），**从不配 root**；root 默认
  `WARNING` 且无 handler，导致 `logging.getLogger(__name__)` 的 INFO 静默丢失。
  生产路径 `uvicorn src.main:build_app --factory` 不经过 `run()`，
  所以配置必须挂在 `build_app`（幂等，见 `src/webapp/logging.py`）
- **两套数据源共存（已知不一致）**：`overview` / `by_provider` 读 `usage_events`（即时，
  仅覆盖 90 天明细），`timeline` / `model-timeline` 读 `usage_hourly`（≤5 分钟滞后，永久）。
  时间范围 ≤90 天时两者一致（汇总由同一批明细算出）；选「全部」时总览会小于图表，
  因为超过 90 天的明细已被清理、只剩小时汇总。修法已列入待办（把总览也切到小时表），
  但会引入 ≤5 分钟延迟，待定。
