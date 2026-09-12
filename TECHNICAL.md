# coding2api 技术方案

PROPOSAL.md 定方向，本文档定实现。每个模块标注来源决策（Q 编号）。

---

## 1. 技术栈定稿

| 层 | 选型 | 版本 | 决策 |
|---|---|---|---|
| 运行时 | Python | 3.14（≥3.12） | PROPOSAL §10 |
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
│   ├── config.py                # pydantic-settings：PROPOSAL §8 全部 14 项 env
│   ├── db/
│   │   ├── schema.sql           # §5 DDL 定稿
│   │   ├── conn.py              # 连接管理（线程本地 + WAL + busy_timeout）
│   │   ├── migrate.py           # 启动时执行 schema.sql（CREATE IF NOT EXISTS）
│   │   └── crypto.py            # Fernet：APP_SECRET → key derive → encrypt/decrypt
│   ├── auth/
│   │   ├── users.py             # users.txt 解析（username:PBKDF2），hash_password.py CLI
│   │   ├── session.py           # 会话 Cookie 签发/校验（itsdangerous 或手写 HMAC）
│   │   ├── api_key.py           # sk- 生成（secrets）、SHA-256 摘要存储、常量时间校验
│   │   ├── csrf.py             # 写操作 CSRF 校验（自定义头 / 同源 Origin）
│   │   └── throttle.py         # 登录限流（三级窗口 + PBKDF2 并发上限）
│   │   └── rbac.py              # ADMIN_USERNAMES 判定；require_admin 依赖
│   ├── provider/
│   │   ├── base.py              # Provider 协议、Event、ErrKind、HealthScore、Quota
│   │   ├── registry.py          # {"codebuddy": ..., "trae": ...}；未注册 → 400
│   │   ├── codebuddy/
│   │   │   ├── client.py        # 上游 HTTP + SSE 流
│   │   │   ├── events.py        # OpenAI 风格 SSE → Event
│   │   │   ├── oauth.py         # 设备码轮询（801 行参考）
│   │   │   ├── quota.py         # 个人/企业额度
│   │   │   ├── checkin.py       # 签到
│   │   │   └── refresh.py       # token 刷新 + 多账号切换
│   │   ├── trae/
│   │   │   ├── client.py        # SOLO 上游 + 双 httpx 客户端
│   │   │   ├── events.py        # 自定义 SSE → Event
│   │   │   ├── credential.py    # 凭证解析（嵌套/扁平）+ 原子写回
│   │   │   ├── callback.py      # 登录 URL 构造 + 回调解析（205 行参考）
│   │   │   └── quota.py         # ide_user_ent_usage
│   │   └── fixtures/            # §8 fixture 清单
│   │       ├── codebuddy/*.sse
│   │       └── trae/*.sse
│   ├── engine/
│   │   ├── scheduler.py         # 选号 + 冷却状态机 + pin
│   │   ├── executor.py          # 请求执行 + 轮换重试 + 统计埋点
│   │   ├── model_resolver.py    # "glm-5.2" | "glm-5.2@trae" | auto → 候选集
│   │   └── sse.py               # SSE 帧解析（跨 provider 共用）
│   ├── compat/
│   │   └── openai/
│   │       ├── request.py       # ChatRequest 校验 + 上游 payload 构造
│   │       ├── response.py      # 流式 chunk 生成 + 非流式聚合
│   │       └── errors.py        # OpenAI error shape
│   ├── tasks/
│   │   ├── pacer.py             # 全局节流器（PACER_MIN/MAX 随机区间）
│   │   ├── quota_probe.py       # 周期探测 → credentials.quota_* 写回
│   │   ├── checkin.py           # CHECKIN_HOUR 每日签到 + 启动补偿
│   │   ├── refresh.py           # REFRESH_SKEW_HOURS 窗口预刷新
│   │   └── retention.py         # usage_events 90 天清理（小时汇总永久）
│   ├── stats/
│   │   ├── collector.py         # usage_events 写入（脱敏）
│   │   └── query.py             # overview / by-provider 聚合查询
│   └── api/
│       ├── deps.py              # Services 容器 + require_api_key / session / csrf 依赖
│       ├── chat.py              # POST /v1/chat/completions
│       ├── models.py            # GET /v1/models（动态拉取 + 黑名单 + 缓存兑底 + 元数据）
│       ├── authorize.py         # GET /authorize（TRAE 回调落点）
│       ├── admin_credentials.py # 凭证 CRUD / toggle / pin / probe / checkin / 账号切换
│       ├── admin_keys.py        # API Key CRUD
│       ├── admin_stats.py       # 统计查询（overview / by-provider / timeline / model-timeline）
│       ├── admin_auth.py        # 登录 / 登出 / 会话；上游登录 start/poll/cancel
│       └── playground.py        # 会话调试端点（无需 API Key）
├── web/                         # React 前端（M2）
├── tests/                       # §9
├── Dockerfile / docker-compose.yml  # 仓库根（M3；compose build context 依赖根目录）
├── NOTICE / LICENSE / README.md / README.zh.md
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

### 3.2 错误分类（决定冷却时长，Q12=B）

```python
class ErrKind(StrEnum):
    PLAN = "plan"        # 权益耗尽 → 冷却 12h
    SOFT = "soft"        # 限流/404 → 冷却 60s，不累计错误数
    DEAD = "dead"        # session 失效 → 硬禁用 disabled=1
    OTHER = "other"      # 其他 4xx/5xx → 累计，连续 3 次 → 冷却 10m
```

| 上游信号 | CB | TRAE | ErrKind |
|---|---|---|---|
| 权益耗尽 | `code=1005`/plan 相关 | `"code":1005` | PLAN |
| 限流 | 429 | 429 | SOFT |
| 不存在 | 404 | 404 | SOFT |
| 会话失效 | 401/403 | 401 + login 标记 | DEAD |
| 服务端错误 | 5xx | 5xx | OTHER |
| 流内错误事件 | SSE error 事件 | `event:error` code=1005→PLAN | 同上映射 |

### 3.3 三态健康度（Q26/A）

```python
@dataclass(slots=True)
class Quota:
    remaining: float | None = None
    total: float | None = None
    cycle_end: int | None = None      # CB 有周期；TRAE None
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

    # 健康度（调度器唯一依赖）
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
  3. scheduler.select(model, tried)：
     a. 候选 = 注册表中支持该模型的 provider
     b. pin 优先：pinned 凭证属于候选 provider 且 healthy → 直接用
     c. 过滤 healthy（enabled=1, disabled=0, 非冷却中）
     d. health 三态排序，取最高分；同分按 provider 顺序
  4. executor：解密凭证 → provider.stream_chat()
     - 上游 HTTP ≥400 → classify → scheduler.note_error → tried 加入 → 回到 3（最多 3 次轮换）
     - 流内 Event.ERROR → 同上映射 → 注入 OpenAI SSE 错误帧 + 冷却 + 轮换
  5. response.py：Event → OpenAI chunk（流式）或聚合（非流式）
     - 首块补 role:assistant；上游无 index 的 tool_calls 补稳定 index
  6. stats.collector：写 usage_events（username/provider/model/tokens/latency/ttfb/ok）
  7. scheduler.note_success：清 err_count
```

客户端断连：`request.is_disconnected()` 轮询，断连时统计 `error_type=client_disconnect`，关闭上游流。

---

## 6. 调度器规格（Q12=B + Q26）

```python
class Scheduler:
    MAX_ROTATE = 3
    COOLDOWN = {ErrKind.PLAN: 12h, ErrKind.SOFT: 60s, ErrKind.OTHER: 10m}
    ERR_THRESHOLD = 3          # 连续 OTHER 错误 → 冷却

    def select(self, model: str, tried: set[str]) -> DecryptedCredRef | None: ...
    def note_success(self, cred_id: str) -> None: ...
    def note_error(self, cred_id: str, kind: ErrKind) -> None: ...
    def pin(self, credential_id: str | None) -> None: ...
```

状态全部落 `credentials` 表（`cooling_until` / `err_count` / `health` / `disabled`），进程重启不丢冷却状态。写路径用单连接串行（SQLite WAL 下单写者）。

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

DDL 以 PROPOSAL §5 为准（users.txt 为用户唯一源、无 users 表、凭证加密列、usage_events.credit 可空），补充实现细节：

```sql
-- conn.py 打开时执行
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;      -- api_keys 之外无外键（users.txt 无表）
```

- 连接：`threading.local()` 每线程一个 `sqlite3.Connection(row_factory=sqlite3.Row)`；写操作集中在引擎线程，读操作 FastAPI 线程池
- 加密：`Fernet(base64.urlsafe_b64encode(sha256(APP_SECRET).digest()))`；APP_SECRET 丢失 = 凭证全部不可解，只能重录（Q13 已明示）
- migration：启动时读 `schema.sql` 逐条 `CREATE TABLE IF NOT EXISTS`；未来加列用 `ALTER TABLE ... ADD COLUMN` 幂等脚本

---

## 8. 测试策略（Q14=B + T-Q5）

| 目标 | 覆盖 | 方式 |
|---|---|---|
| 调度器（冷却/三态排序/轮换/pin） | 100% | 纯单元，注入假 provider |
| 鉴权（users/apikey/session/rbac） | 100% | 单元 + FastAPI TestClient |
| SSE 帧解析 | 100% | fixture 契约测试 |
| provider 事件映射 | 100% | fixture：真实 SSE 样本 → Event 断言 |
| OpenAI 协议适配 | 100% | 流式/非流式/工具调用 fixture |
| OAuth/回调解析 | 100% | fixture：真实回调 URL/state 响应 |
| HTTP 客户端 | ~ | respx mock 状态码 + body → classify 断言 |
| 统计/查询 | 70% | sqlite 内存库集成 |
| 其余 | ≥70% | — |

fixture 提取来源（开工时执行，不手写）：

| fixture | 来源文件 | 提取内容 |
|---|---|---|
| `codebuddy/chat-basic.sse` | `codebuddy2api/tests/test_stream_service.py`（L268/L1285 附近） | `data: {"choices":[...],"usage":{...}}` 帧 |
| `codebuddy/tool-calls.sse` | 同上（`_process_tool_calls` 用例） | delta.tool_calls 分片 |
| `codebuddy/reasoning.sse` | 同上 | reasoning_content delta |
| `codebuddy/error-state.json` | `test_codebuddy_oauth.py` | auth/state 响应体 |
| `trae/chat-basic.sse` | `trae2api-web/internal/upstream/sse_test.go` | metadata→output→token_usage→done 全序列 |
| `trae/tool-calls.sse` | 同上 | output.tool_calls |
| `trae/error-1005.sse` | 同上 | `event:error` code=1005 |
| `trae/callback-url.txt` | `internal/server/callback_test.go` | 真实回调 URL 样本 |

fixture 断言两个方向：**解析正确**（样本 → 期望 Event）与**不静默**（畸形样本 → UpstreamProtocolViolation）。

---

## 9. 里程碑 → 模块映射

| 里程碑 | 交付模块 | 周 |
|---|---|---|
| M0 骨架 | config / db / auth / engine(scheduler+mock provider) / 测试基线 | 1 |
| M1a TRAE | provider/trae 全部 + engine(executor/sse) + compat/openai + api/chat,models,authorize + tasks | 2 |
| M1b CB 基础 | provider/codebuddy 的 client/events/quota + import_credential（bearer-only） | 2 |
| M1.5 CB 完整 | oauth / refresh(多账号) / checkin + tasks/checkin | 2 |
| M2 前端 | web/ 6 页 | 2 |
| M3 收尾 | deploy/ + 文档 + NOTICE + CI | 1 |

M1b 结束执行双 provider 对比验证（Q25=A）：同一请求走两个 provider，断言调度行为、统计字段、错误分类一致。

---

## 10. 已知取舍备忘

- **同步 sqlite3 而非 aiosqlite**（T-Q2）：本地微秒级操作，asyncio 封装开销大于收益
- **双 httpx 客户端**（T-Q4）：聊天流 read=None 防长流截断；短请求总超时 30s 防悬挂；共享 `trust_env=False`
- **手写 SQL 而非 ORM**：8 张表规模下 ORM 收益为负
- **polling OAuth 不转回调**（Q17=C）：上游协议决定；TRAE 回调走主端口 + PUBLIC_BASE_URL
- **v1 无 Anthropic**（Q8=A）：Event 层已预留，v1.1 只加 `compat/anthropic/` 适配器
