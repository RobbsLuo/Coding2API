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
│   ├── main.py                  # FastAPI 组装、lifespan、路由挂载
│   ├── config.py                # pydantic-settings：README「配置」全部 env
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
│   │   │   ├── checkin.py       # 签到
│   │   │   └── refresh.py       # token 刷新 + 多账号切换
│   │   ├── trae/
│   │   │   ├── client.py        # SOLO 上游 + 双 httpx 客户端 + 额度探测
│   │   │   ├── events.py        # 自定义 SSE → Event
│   │   │   ├── credential.py    # 凭证解析（嵌套/扁平）+ 原子写回
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
│   │   ├── refresh.py           # 每 60 分钟；REFRESH_SKEW_HOURS 窗口内预刷新
│   │   ├── retention.py         # 每 5 分钟：小时汇总重算（幂等）+ 90 天前明细清理
│   │   └── runner.py            # 后台任务调度，接入应用生命周期
│   ├── stats/
│   │   ├── collector.py         # usage_events 写入（脱敏）
│   │   └── query.py             # overview / by-provider / events 查询（events JOIN credentials 带凭证昵称）
│   └── api/
│       ├── deps.py              # Services 容器 + require_api_key / session / csrf 依赖
│       ├── chat.py              # POST /v1/chat/completions
│       ├── models.py            # GET /v1/models（动态拉取 + 黑名单 + 缓存兑底 + 元数据）
│       ├── balance.py           # GET /v1/user/balance（DeepSeek 兼容余额，读探测缓存聚合）
│       ├── authorize.py         # GET /authorize（TRAE 回调落点）
│       ├── admin_credentials.py # 凭证 CRUD / toggle / pin / probe / checkin / 账号切换
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

    # 可选能力（CB 独有；trae 抛 NotImplementedError）
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

状态全部落 `credentials` 表（`cooling_until` / `err_count` / `health` / `disabled` / `quota_expiry_ladder`），进程重启不丢冷却状态。写路径无应用层锁：每个写方法直接走当前线程的连接，并发写靠 SQLite WAL + `busy_timeout=5000` 串行化。

到期积分只算一处：`expiring_credits()`。选号走 `Candidate.expiry_credits()`，管理台列表走 `GET /api/credentials` 的 `quota_expiring_credits`（窗口值随响应返回 `expiry_window_seconds`），两处共用同一实现，界面数字与选号顺序不会漂移；渠道无到期信息时返回 `null`（不显示），窗口关闭或确实无积分临近过期时返回 `0`（同样不显示）。

---

## 6.1 后台任务（tasks/）

`TaskRunner`（runner.py）每类任务一个独立 asyncio 循环，失败只记日志不拖垮服务；间隔有安全下限，避免打爆上游。所有对外 HTTP 请求经 `pacer.py` 全局节流。

| 任务 | 周期 | 行为 |
|---|---|---|
| 额度探测（quota_probe.py） | 启动立即跑一轮（不节流） + 每 `QUOTA_PROBE_MINUTES`（默认 60）分钟 | 探测上游剩余额度 → `credentials.quota_*` / `quota_expiry_ladder` / `health` 写回 |
| token 预刷新（refresh.py） | 每 60 分钟 | 到期前 `REFRESH_SKEW_HOURS`（默认 24h）窗口内轮换 refresh token |
| 每日签到（checkin.py） | 每 10 分钟（全天） | 成功即封账该凭证当日（`日期:scope`，进程内内存态，重启重建）；失败凭证持续重试，同账号多凭证共享一次 |
| 明细清理（retention.py） | 每 5 分钟 | `usage_events` 全量重算小时汇总（幂等 upsert，最新小时滞后 ≤5 分钟）+ 90 天前明细清理 |

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

- 连接：`threading.local()` 每线程一个 `sqlite3.Connection(row_factory=sqlite3.Row)`；引擎与 FastAPI 线程池各自持有自己的连接。没有应用层写锁，写入并发由 SQLite 自身串行化（WAL + `busy_timeout=5000` 下短写足够；确需多语句原子性时用显式事务）
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
