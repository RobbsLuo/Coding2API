# Coding2API 技术方案

PROPOSAL.md 定方向，本文定实现。只写**当前代码现状**。

## 1. 技术栈
| 层 | 选型 | 版本 | 说明 |
|---|---|---|---|
| 运行时 | Python | 3.12（`requires-python >=3.12`；CI 3.12，Dockerfile 3.14-slim） | |
| 包管理 | uv（venv + pyproject.toml + uv.lock） | 见 `uv.lock` | |
| Web | FastAPI + Uvicorn | 0.143.3 / 0.54.0 | |
| 配置 | pydantic-settings | 2.16.0 | `Settings` 为 frozen |
| HTTP | httpx 双客户端（流式/短请求分离）；`httpx[socks]` 提供按渠道 SOCKS5 | 0.28.1 / socksio | 共享 `trust_env=False` |
| 数据库 | 标准库 sqlite3（WAL）+ 手写 SQL | 内置 | |
| 加密 | cryptography Fernet | 50.0.2 | |
| 测试 | pytest + pytest-cov + respx + 文件化 fixture | 9.1.1 / 7.1.0 / 0.23.1 | |
| 前端 | React 19 + Tailwind 4 + shadcn/ui + @lobehub/icons + Vite 8 + TypeScript 7 | 见 `web/package.json` | |

> 小版本随依赖漂移，以 `pyproject.toml` / `uv.lock` / `web/package.json` 为准；仅主版本或兼容边界变化时回改本表。

## 2. 目录结构与模块
行内列出该目录全部文件；职责按「／」与文件顺序对应。

| 路径 | 文件 | 职责 |
|---|---|---|
| `pyproject.toml` | — | uv 项目；`[tool.pytest.ini_options]` 设 coverage 目标 |
| 仓库根 | `Dockerfile`、`docker-compose.yml` | compose build context 依赖根目录 |
| `src/` | `main.py` `config.py` `runtime_settings.py` `version.py` | 组装·lifespan·路由挂载／pydantic-settings 全部 env + `live()` 归一化／运行时配置覆盖层（DB > env + 白名单 + snapshot）／版本号（`pyproject.toml` 真源） |
| `src/webapp/` | `limits.py` `security.py` `request_id.py` `access_log.py` `log_buffer.py` `handlers.py` `logging.py` `static.py` | 请求体上限 ASGI（登录 8KB / 其余 16MB）／Host 白名单 + CSP·nosniff／请求 ID（最外层，含 413/400）+ 会话 contextvar／请求级 logfmt／日志环形缓冲 + `redact_or_drop`／异常 → 稳定错误码／root logger／SPA catch-all（排除 `/api`、`/v1`） |
| `src/db/` | `schema.sql` `conn.py` `migrate.py` `repo.py` `limits.py` `crypto.py` `backup.py` | DDL（只加不改）／线程本地连接 + WAL + busy_timeout + `transaction()`／迁移（**补列之后**建索引）／持久化手写 SQL／限额 CRUD + 当日聚合／Fernet／备份恢复（`VACUUM INTO` + integrity_check + 0600） |
| `src/auth/` | `users.py` `bootstrap.py` `session.py` `api_key.py` `csrf.py` `throttle.py` `rbac.py` `access.py` | DbUserStore + users.txt 导入／引导（导入 → `ADMIN_USERNAMES` 提权 → 无活跃 admin 报错）／会话 HMAC（含 `session_epoch`）／`sk-` 摘要 + 常量时间校验／CSRF／登录三级限流 + PBKDF2 并发上限／三角色／IP 与模型白名单（纯函数） |
| `src/provider/` | `base.py` `proxy.py` `naming.py` `token_expiry.py` `openai_chunk.py` `fixtures/` | Provider 协议·Event·ErrKind·Quota·HealthScore／按渠道代理（`PROVIDER_PROXIES`）／模型名归一／到期提取（`expires_at` → JWT `exp`）／OpenAI chunk 共享原语／真实样本 |
| `src/provider/codebuddy/` | `client.py` `intl.py` `events.py` `credential.py` `headers.py` `oauth.py` `checkin.py` `activity.py` `growth.py` `growth_runner.py` `refresh.py` | 上游 + SSE + 额度探测 + 风控指纹清洗／国际版子类（覆渠道 id 与能力 None）／SSE → Event + 分类／凭证解析／常量与头／设备码／签到／活跃上报／成长中心（15 端点）／成长编排（7 类领取）／刷新 + 多账号切换 |
| `src/provider/trae/` | `client.py` `events.py` `credential.py` `pricing.py` `backfill.py` `callback.py` | 上游 + 额度／自定义 SSE → Event／凭证解析 + 原子写回 + 签到设备号／单请求积分推算／历史回填／登录 URL + 回调解析 |
| `src/provider/zen/` | `client.py` `events.py` | 上游 + 门禁伪装 + 注入工具回包过滤／标准 SSE → Event |
| `src/provider/kilo/` | `client.py` `events.py` | 上游（标准 OpenAI）+ `isFree` 过滤（不探活）／SSE → Event（`delta.reasoning`） |
| `src/provider/qoder/` | `cosy.py` `client.py` `events.py` `credential.py` `auth.py` | COSY（自定义 Base64、AES/RSA、签名与会话头缓存）／上游 + 信封解包 + 额度·签到／信封 → chunk → Event／凭证解析／设备码 PKCE |
| `src/provider/codearts/` | `signer.py` `dpop.py` `client.py` `events.py` `credential.py` `auth.py` `oauth.py` | SDK-HMAC-SHA256（纯函数）／ES256 DPoP（低 S）／上游 + 私有 SSE 还原 + 积分额度／SSE → 增量 Event（双形状）／凭证解析／OAuth2 PKCE／回调粘贴换 token |
| `src/provider/officeace/` | `signer.py` `client.py` `events.py` `credential.py` `oauth.py` | 双签名（V11 + SDK-HMAC，纯函数）／上游 + 积分·签到·费率／错误分类 + `max_tokens` 改写／两套凭据 + 一次性 refresh／云端授权轮询（PKCE + DPoP + 开通校验） |
| `src/engine/` | `scheduler.py` `executor.py` `limits.py` `inflight.py` `affinity.py` `continuation.py` `compress.py` `model_resolver.py` `sse.py` | 选号 + 冷却状态机 + pin／执行 + 目录收窄 + 轮换 + fallback + 统计埋点／限额判定 + 快照缓存／在途租约／会话粘性／截断续写／上下文压缩／模型名解析／SSE 帧解析 |
| `src/compat/` | `openai/{request,response,errors}.py` `responses/{request,response}.py` `anthropic/{request,response}.py` | Chat 校验·聚合·error shape／Responses 出口／Anthropic Messages 出口 |
| `src/tasks/` | `pacer.py` `quota_probe.py` `checkin.py` `growth.py` `activity.py` `refresh.py` `retention.py` `backup.py` `alerting.py` `upstream_status.py` `model_probe.py` `status.py` `runner.py` | 全局节流器／额度探测／签到（`CHECKIN_TIMES` 空则每 10 分钟）／成长（仅 CB）／活跃上报／预刷新／小时汇总 + 90 天清理／备份／告警（四规则 + webhook）／上游状态聚合／模型探活／`TASK_SPECS` + 运行态／调度接入 lifespan |
| `src/stats/` | `backfill.py` `collector.py` `live.py` `query.py` `csv.py` | 历史回填／明细写入 + 小时双写 + 提交后广播／进程内实时广播／各类统计查询／CSV（公式注入防护） |
| `src/` 顶层 | `sorting.py` `benchmarks.py` `pricing.py` `model_match.py` `request_context.py` `upstream_status.py` | `sort`/`order` 白名单／OpenRouter 目录（价 + 元数据 + 能力分 + 快照）／成本估算（USD→CNY）／模型名匹配／请求 ID contextvar／状态页聚合 |
| `src/api/` | `deps.py` `endpoints.py` `chat.py` `responses.py` `messages.py` `context.py` `models.py` `model_merge.py` `model_catalog.py` `balance.py` `authorize.py` `admin_credentials.py` `admin_keys.py` `admin_settings.py` `admin_users.py` `admin_audit.py` `admin_alerts.py` `admin_badges.py` `admin_upstream.py` `admin_model_catalog.py` `admin_model_availability.py` `logs.py` `activate.py` `admin_stats.py` `admin_auth.py` `playground.py` `streaming.py` `metrics.py` | Services 容器 + 鉴权依赖／`dispatch_compat` 骨架／三个 /v1 出口／压缩接线／`GET /v1/models`（SWR + 落盘 + 白名单）／归一·合并·别名（纯函数）／目录快照／余额／TRAE 回调／凭证运维／Key CRUD／配置与任务／用户／审计／告警／角标／上游状态／模型目录 + 探活／激活／统计／登录 + 上游登录／Playground／SSE 包装／`/metrics` |
| `src/audit/actions.py` | — | 审计动作常量 + 中文标签 |
| `web/` | — | React 前端。**`.npmrc` 的 `auto-install-peers=false` 与 lock 的 `settings.autoInstallPeers` 强耦合**（避免拉 `@lobehub/ui`/`antd` 子树）；`Dockerfile` 必须 `COPY web/.npmrc`，否则 `ERR_PNPM_LOCKFILE_CONFIG_MISMATCH` |
| `deploy/` | — | launchd / newsyslog / logrotate / systemd 模板 |
| `diagrams/` | — | 架构图 / 时序图 / 状态机（HTML + 源 JSON） |
| `scripts/` | — | 建号、回填、运维脚本 |
| `tests/` | — | 按 `src/` 子包镜像 |

## 3. 核心类型
### 3.1 中立事件层（`provider/base.py`）
```python
class EventKind(StrEnum):
    CONTENT = "content"        # 正文增量
    REASONING = "reasoning"    # 思考链增量
    TOOL_CALLS = "tool_calls"  # 工具调用增量（上游原样，index 由 engine 补齐）
    USAGE = "usage"            # prompt/completion/reasoning/cached tokens；credit 可空
    FINISH = "finish"          # finish_reason
    ERROR = "error"            # 流内业务错误（provider 解析时写 error_kind）

@dataclass(slots=True)
class Event:
    kind: EventKind
    content: str | None = None
    tool_calls: list[dict] | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error_code: int | None = None
    error_message: str | None = None
    error_kind: ErrKind | None = None   # 流内错误的分类，executor 直接消费

@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None   # 输入中命中缓存的 token（缺省 None）
    credit: float | None = None        # 上游可选字段，两边都经常为 None
    credit_estimated: bool = False     # credit 是否为本服务推算（True 时展示标 ≈）
```

- **事件名过滤**：TRAE `metadata` / `timing_cost` / `extra_info` / `progress_notice` 无语义 → 不在 `KNOWN_EVENT_NAMES` → 跳过。
- **解析失败不静默**：已知事件 JSON 非对象 / 字段类型错 → `UpstreamProtocolViolation`，不退回默认值。
- **tool_call 续块须保留**：TRAE 按 `index` 分片；首片带 `function_call.name`，续片只有 `arguments`。只丢「无 name **且** arguments 空」噪声（`_is_blank_solo_tool_call`）。「无 name 即丢」→ 吃光续片 → 截断 JSON → 死循环。`provider/openai_chunk.py` 的 `is_blank_tool_call` 同判据。

### 3.2 错误分类（定冷却时长与作用域）
```python
class ErrKind(StrEnum):
    PLAN = "plan"        # 权益耗尽（1005）→ 冷却 12h
    CREDIT = "credit"    # 余额不足（402/14018）→ 冷却到次日 04:00（等签到恢复）
    SOFT = "soft"        # 限流/404 → 冷却 60s，不累计错误数
    DEAD = "dead"        # session 失效（重登可恢复）→ 硬禁用 disabled=1
    RESTRICTED = "restricted"  # 账号被上游风控限制（11140）→ 账号级 2h 冷却，当前请求不再换号
    OTHER = "other"      # 其他 4xx/5xx → 累计，连续 3 次 → 冷却 10m
    INVALID = "invalid"  # 请求无效（模型不存在等）→ 不冷却凭证，跳过该上游；全拒 → 400
    MODEL = "model"      # 该模型用量超限（429+6004）→ 只冷却 (凭证, 模型)，10m 起翻倍封顶 2h
    CONCURRENCY = "concurrency"  # 并发会话打满 → 固定 60s 模型级短冷却，不翻倍
    BLOCKED = "blocked"  # 该账号无此模型（400/404+11102）→ (凭证, 模型) 负缓存，6h 起翻倍封顶 24h
    REQUEST = "request"  # 请求级错误（11101/11115/11128/11135）→ 零动作：不冷却、不累计，仅换号
    QUEUE = "queue"      # 排队中（isQueued / RESOURCE_LIMIT）→ 零动作：等 retryAfterSeconds 后原号重开

MODEL_SCOPED_KINDS = frozenset({MODEL, BLOCKED, CONCURRENCY})
```

`MODEL_SCOPED_KINDS` 只写 `credential_model_cooldowns`（§6.3），不碰账号级 `cooling_until`。账号级冷却出现 → 清该凭证模型级条目；不清 → 切模型绕过账号级限流。

**账号被上游风控限制（RESTRICTED，CodeBuddy 11140）**：`403 request illegal` 的字面像「这条请求非法」，实际是上游按账号（多号同源时按出口 IP/设备）拉黑——换模型、换请求形态、换代理都无效（techysy/10router、yetone/magpie、maiphucgiang/codebuddy2api 三个项目的实测结论一致）。

- **必须拦在 `401/403 → DEAD` 之前**：判 DEAD 会写不可逆的 `disabled=1`，而这类限制由上游自行解除，写成禁用就变成「必须有人点恢复」，还会诱使运维反复 revive 去撞。
- **账号级 2h 冷却**（`RESTRICTED_COOLDOWN_SECONDS`），上游给 `Retry-After` 时采用并夹在 `[now + 该方法常量, now + 24h]` 内（比常量还短的上游值一律回落常量，防止 `Retry-After: 0` 让同一个号在冷却刚写下的瞬间又变可选）；到期自动回到候选，不做任何人工动作。
- **当前请求不再换号**：`executor` 命中后 `_skip_provider` 跳过该渠道剩余凭证，继续试其它渠道。逐个号换着撞会扩大标记面（实测一次请求连打 3 个号全 403，新号加入 3 分钟即被封）。
- **窗口内第二个不同凭证也命中 → 升级为渠道级暂停 24h**（`_pause_provider_on_burst` + `cool_provider`）：只冷却、不写 `disabled`，所有号一起退避。判据是窗口内**不同凭证的个数**而不是命中次数——同一个号重复中招不算整批同源（单号 2h 冷却已挡住重复撞），按命中次数升级会让单一凭证/单一租户的两个连续请求把整渠道停掉。

**硬禁用的可操作原因（`disabled_reason`）**：真 session 失效（401 / token 过期）仍是 DEAD，`disabled_reason` 写「session dead」或 provider 识别出的具体原因（CodeBuddy 的 `disabled_reason()` 识别 11140 文案，作为非 RESTRICTED 入口的兜底）。管理台 `CredentialsPage` 原样展示该字段，写错会把人引向错误的修复动作。只回受控文案，不拼接上游正文。

**排队语义（QUEUE）**：上游回排队中 + 建议等待秒数 = 号满，等 N 秒重打。非账号故障、非请求错误。

- **不冷却**：`scheduler.note_error` 对 QUEUE 零动作（同 REQUEST）。
- **不换号**：`executor` 用 `pinned` 记住被点名凭证，等完原号重开。
- **不占轮换预算**：排队轮次不计 `attempts`；计则三次排队吃满 `max_rotate` → 503。
- **有界**：`QUEUE_MAX_ROUNDS = 3` 轮、单轮封顶 `QUEUE_MAX_WAIT_SECONDS = 60s`。
- **已出帧不重开**：半截输出不可回滚 → 降级 SOFT 收尾。

识别 `provider.base.parse_queue_hint`（信封可嵌套，最多下钻 3 层）：`isQueued: true` / `subCode ∈ {RESOURCE_LIMIT, RESOURCE_LIMIT_EXCEEDED}` 任一成立即认定。**宁可漏判不可误判**：识别不出走原分类，绝不把普通失败当排队无限等。Qoder `_decode_frame` 须**先于** `classify_error_code` 拦下——上游把排队包成 403，按状态码分类判 DEAD 硬禁用凭证。`Event.retry_after_seconds` 承载上游点名秒数。

**排队标记可能藏在**序列化字符串**里**：上游常把内层报文再 `json.dumps` 一次（Qoder 排队报文串两层：`{"code":"403","message":"{\"code\":\"10605\",\"message\":\"{…isQueued…}\"}"}`）。只认 dict/list 会漏判 → 403 落 DEAD → **可自愈的凭证被硬禁用**。故 `parse_queue_hint` 下钻字符串；串里套串**不额外消耗深度预算**（深度只按 dict/list 的协议层级计，秒数常在最内层）。判据同时放进 `classify_error_code`（而非只在调用点），保证任何入口都不漏判。

| 上游信号 | CB | TRAE | Kilo | ErrKind |
|---|---|---|---|---|
| 权益耗尽 | `code=1005` | `code:1005` | — | PLAN |
| 余额不足 | 402 / `code=14018` | 402 / `code=14018` | — | CREDIT |
| 模型级限流 | 429 + `code=6004` | 429 + `code=6004` | 429 / 502/503/504（点名模型） | MODEL |
| 并发打满 | — | — | — | CONCURRENCY（CodeArts `TM.00001041`） |
| 该账号无此模型 | 400/404 + `code=11102` | 400/404 + `code=11102` | — | BLOCKED |
| 请求级错误 | 400 + 11101/`Unmarshal chat params failed`/11115/11128/11135 | — | 403 | REQUEST |
| 限流 | 429（无 6004） | 429（无 6004） | — | SOFT |
| 不存在 | 404 | 404 | — | SOFT（CodeArts 404 亦 SOFT） |
| 会话失效 | 401/403 | 401 | — | DEAD |
| 服务端错误 | 5xx | 5xx | 500 | OTHER |
| 请求自身无效 | 400 | 400（含 4001） | 400/404/422 | INVALID |
| 排队中 | — | — | — | QUEUE（Qoder `isQueued` / `RESOURCE_LIMIT`） |
| 流内错误事件 | SSE error 事件 | `event:error` 业务码 | `{"error":{...}}` 帧 | 同上映射（单一来源 `Event.error_kind`，缺失回落 OTHER） |

- **zen / kilo 401 → `INVALID`**：两者无凭证；`401 Missing API key.` 只说明该模型要付费 key。归 `DEAD` → 一次强制付费请求硬禁用整条免费渠道。
- **kilo 429 与 502/503/504 → `MODEL`**：免费池 = OpenRouter 共享池，报错点名具体模型（`limit_source: upstream_provider_shared_pool`）；此时其他免费模型仍可用。
- **CodeArts 临时凭证过期 → `SOFT`**：`HTTP 400` + `APIG.0602 security token has expired`，识别先于 `401/403 → DEAD`（`_STS_EXPIRED_MARKERS`）。判 `DEAD` → `RefreshTask` 跳过该凭证 → 断唯一自愈路径。
- **Qoder 401 与 403 不对称**：`401` 一律 `DEAD`（实测只有凭据/会话失效一种形态：`TOKEN_EXPIRE` 等；放宽等于让被吊销的 token 在 60s 轮换里反复撞上游）；`403` 只在带会话失效标记时 `DEAD`，其余 `SOFT`。排队信号优先于两者（见上）。`classify_status` 只是 `classify_error_code` 的转发，判据只写一处，HTTP 与流内信封不可能漂移。
- **TLS 中断（`ssl.SSLError`）→ `SOFT`**：`executor._classify` 与 `httpx.TransportError` 并列兜住。anyio 只把 `SSLEOFError` 转 `BrokenResourceError`；其余（`record layer failure`、证书链握手失败）原样上抛，既非 `httpx.TransportError` 也不在「只认具体类」白名单。不单列 → 逃到 `stream_guarded`，一帧 `internal_error`：不换号、不冷却、`usage_events` 零痕迹。不放开到 `OSError` 整体（磁盘满、句柄耗尽误冷却健康凭证）。

**业务码只从 `"code": N` 键值形态提取**（`provider.base.business_codes`）：裸搜数字 → `"code":111020` 命中 `11102`。判定顺序即优先级：`429+6004` 先于 `429`；`402/14018` 先于状态码兜底。

**内容风控指纹（11128）**：`400 + 11128 Illegal API invocation from an unapproved channel` → REQUEST。主因 `system`/`assistant` 正文含「伪装其他厂商官方客户端」指纹串（身份声明行、billing 头字段名、git 上下文行）。特征：与凭证无关、确定性复现、只在两角色 `content` 命中（`tool_calls` 与 reasoning 不触发）；写进历史 → 后续每轮都 11128。处置：出站前 `sanitize_channel_markers` 替换占位符（`CODEBUDDY_SANITIZE_CHANNEL_MARKERS=false` 关闭）；客户端历史不受影响；文本块列表逐块处理。清单在 `provider/codebuddy/client.py::CHANNEL_MARKERS`——刻意不写原文，以免污染读本文件的会话。

### 3.3 三态健康度
```python
@dataclass(slots=True)
class Quota:
    remaining: float | None = None
    total: float | None = None
    cycle_end: int | None = None      # 最早到期 epoch；无到期信息为 None
    expiry_ladder: list[tuple[int, float]] | None = None  # [(到期 epoch, 该包剩余积分)]
    packages: list[dict] | None = None  # [{"name","total","used","end"}]，仅展示
    probed_at: int | None = None
    probe_failed: bool = False

UNKNOWN: int | None = None
EXHAUSTED = -1
# HealthScore：0-100 已知；None 未知；-1 已耗尽

def health_score(q: Quota | None) -> int | None:
    if q is None or q.probe_failed:
        return UNKNOWN
    if q.total is None or q.total <= 0:
        return EXHAUSTED
    return max(0, min(100, round((q.remaining or 0.0) / q.total * 100)))
```

排序：`known DESC > unknown > exhausted(-1)`。`probe_failed` 与「探测到 0」须分开——Zen/Kilo 虚拟凭证恒 `probe_failed=True`。**CB 个人版 `remaining` = 逐包 `CycleCapacityRemainPrecise` 之和，不用顶层 `TotalDosage`**：后者上周期口径 → `remaining == total`、健康度恒 100、与到期阶梯自相矛盾。企业版走 `limitNum - credit`。

### 3.4 截断续写
`finish_reason == "length"` → 同凭证续写，最多 `AUTO_CONTINUE_MAX`（默认 10，0 关闭）。实现 `engine/continuation.py::ContinuationStream`：追加「已产出正文 + 续写指令」重发；输出上限两键归零；跨轮累计 usage；末端补一条累计 usage + 最后一轮真实 `finish_reason`（上游未报 usage 不补，避免误报 0）。事件流包装器而非 executor 重跑：凭证固定 → 轮换 / 记账 / 统计零改动。

只做 `length` 一个判据：「仅 reasoning 无正文」只在客户端下发 `max_tokens` 时出现，本网关不做 max 键映射 → 不可达；「空正文」「代码块未闭合」无证据 → 不做。

**输出上限键**：CB 忽略 `max_completion_tokens`、只认 `max_tokens`（精确截断 + `length`）；两键同发后者胜。TRAE 两键都不生效。`enable_thinking:false` 亦被 CB 忽略。

**`reasoning_effort` 缺失补 `medium`**：不发该字段 → CB 把思考写进 `delta.content`（`reasoning_content` 恒空、`reasoning_tokens` 恒 0）。`stream_chat` 用 `setdefault`，不覆盖显式值。

### 3.5 会话粘性键
（`engine/affinity.py`）优先级：① 请求体 `conversation_id` > `conversationId` > `prompt_cache_key`（同键名先 `metadata` 再顶层）→ ② 请求头 `x-session-id` / `x-opencode-session` / `x-session-affinity` / `conversation-id` / `conversation_id`（首个合法值，须匹配 `[A-Za-z0-9._:-]{1,128}`）→ ③ 消息增量前缀指纹。

- 头经 `request_context` contextvar 传入（`RequestIdMiddleware` 设置、请求结束复原）；键带 `header:` 前缀避免与 body 撞键。
- 带 `user_id`（顶层或 `metadata`）**不派生前缀兜底键**（头照常生效）；否则同用户并行对话误钉同一凭证。
- 键掺用户名（**头值同样掺**）：头是客户端可写输入；不掺就能靠猜占用他人账号。
- TTL `CONVERSATION_STICKY_SECONDS` 内存态，重启只影响一轮选号；条目上限 512，超量先淘汰过期再按最旧。

### 3.6 模型元数据与黑名单
| 字段 | CB 来源 | TRAE 来源 | 取值 |
|---|---|---|---|
| `supports_reasoning` | `supportsReasoning` | `display_config.model_capability`（`reasoning_model`→true、`chat_model`→false），缺失回落 `reasoning_effort_config.support_thinking` | true / false / null |
| `default_effort` | `reasoning.effort` | 无对应字段 | CB: `high` / `medium`；TRAE 恒 null |

- `supported_efforts` **不实现**：上游只给默认档不给清单。CB `onlyReasoning` / `canDisableThinking` 同样不透传（语义未核实）。
- **唯一例外 Playground 入参控件**（`web/src/lib/playgroundParams.ts`）：档位候选固定 OpenAI 三档（`low`/`medium`/`high`），上游 `default_effort` 不在其中则追加；`supports_reasoning !== true` 且无 `default_effort` 不渲染，默认「不发送」。三个高级参数留空即不发键；前端校验边界与 `compat/openai/request.py` 对齐（`temperature` ≤ 1000、`max_tokens` 1–4e6）。
- 逐字段补缺：多上游同名模型先到先填，后到只补 None。
- **黑名单**（`MODEL_BLOCKLIST`，fnmatch glob，只影响展示，直连不受影响）。默认只展示在售新模型，三类滤掉：① 上游内部/代理模型——`custom_model_*` / `*sub*agent*` / `summary` / `browser_use_*` / `file_search_agent`，及各渠道自动路由占位 `default` / `default-model` / `fast-model` / `balanced-model` / `primary-model` / `deep-model` / `auto`；② 已被取代的老模型——`glm-4.*` / `glm-5` / `glm-5.0*` / `glm-5.1` / `glm-5.2*` / `glm-5v-*` / `glm-5-turbo`、`kimi-k2.*`、`minimax-m2.5`/`m2.7`/`m3`、`deepseek-v3-*` / `deepseek-v4-*`（`v4.1` 不受影响）、`qwen3.7-*`、`gpt-5.4`/`gpt-5.5`、`hy3*` / `hy4-preview`、`hunyuan-*`、`seed-2.1*` / `seed-code-pro-*` / `seed-evolving`、`qwen3-vl-235b`、`nemotron-3*` / `lfm2*` / `laguna*` / `north-mini-code` / `inkling-small` / `xing4.0-29b-a4` / `openrouter-models-router`；③ 实测不可用——`glyph-cluster`（121s 后 408）、`qwen3.8-flash`（Qoder `qfmodel` 400）。在售主力刻意不加：`deepseek-v4.1-flash` / `kimi-k3` / `glm-5.3*` / `mimo-v2.6*` / `qwen3.8-max` / `openpangu-2.0-*`。
- **按归一后的对外写法匹配**（`_block_names`）：原代号、原代号归一键、展示名、展示名归一键四种取并集（默认规则带下划线；归一键把下划线换成连字符后不命中）。清洗后为空的名字不参与。
- **非模型级问题不进黑名单**：CodeArts `InferHub.4291.200 insufficient quota` 与 `TM.00001041` 是账号级；Kilo `poolside/laguna-xs-2.1` / `nemotron-3-nano-omni` 的 429·502 与 `thinkingmachines/inkling-small` 的 `limit_rpd` 自行恢复。

### 3.7 Responses 出口（Codex CLI）
`src/api/responses.py` + `compat/responses/{request,response}.py`。与 chat **共用同一 executor**，唯一改动 = `Executor.stream(..., translator=...)` 注入缝。字段形状取自官方 `openai` SDK（3.x），并用该 SDK 做端到端契约验证。

| Responses | chat |
|---|---|
| `instructions` | 首条 `system` |
| `input` 字符串 | `user` 消息 |
| `input[].type=message`（`input_text`/`output_text`） | `messages[]`；`developer` 改写为 `system` |
| `input[].content[].type=input_image` | `image_url` part（`detail` 丢弃）；纯文本折成字符串 |
| `input[].type=function_call` | assistant `tool_calls[]` |
| `input[].type=function_call_output` | `tool` 消息（part 可含 `input_image`） |
| `input[].type=reasoning` | assistant `reasoning_content`（仅明文） |
| `tools[].type=function`（扁平） | 嵌套 `{type:function, function:{...}}` |
| `tool_choice` 字符串 / `{type:function,name}` | 同名 / 嵌套 |
| `max_output_tokens` | `max_tokens` |
| `reasoning.effort` | `reasoning_effort` |
| `text.format` | `response_format`（委托 `normalize_response_format`，`prefix="text.format"` 只改报错路径） |
| `temperature` / `top_p` / `parallel_tool_calls` / `prompt_cache_key` / `text.verbosity` | 同名透传 |

**出站序列**（流式）：`response.created` → `output_item.added`（message / reasoning / function_call）→ `content_part.added` → `output_text.delta × N` → `output_text.done` → `content_part.done` → `output_item.done` → `response.completed | incomplete`。思考独立 `reasoning` item（`response.reasoning_summary_text.delta/.done`）；工具 `function_call` item（`response.function_call_arguments.delta/.done`）。**无 `[DONE]` 哨兵**，终止事件即流结束（`done_sent` 对齐）。`finish_reason=length` → `response.incomplete`（`max_output_tokens`），`content_filter` 同理；流内错误 → `response.failed`（`no_healthy_credential` → Codex 认识的 `server_is_overloaded`）。

取舍：`include=["reasoning.encrypted_content"]` **接受并忽略**（400 打死 Codex CLI）；其他 `include` 值 400。`previous_response_id` 与 `store=true` 保持 400（无状态）。`event:` 与 `data.type` 双发。回传历史 `reasoning`/`compaction` 只有密文 → **有意丢弃**；其余私有 item（`local_shell_call`/`custom_tool_call`）显式 400。**未经真实 Codex CLI 端到端验证**（开发环境无 CLI）。

### 3.8 Anthropic Messages 出口（Claude Code）
`POST /v1/messages` + `/v1/messages/count_tokens`，同模式复用 executor。

- `system`（字符串或 text 块）→ 首条 `system`；空则丢。
- `messages[].content` 字符串直传；块数组：`text` → 文本；`tool_result` → `tool` 消息（`tool_use_id` → `tool_call_id`，内含图片同样归一）；`image` → `image_url`（`base64` 拼 `data:{media_type};base64,{data}`，`url` 直传）；`document` / `source.type=file` → 显式 400。user 连续块按类型**拆多条**；assistant 块**合并一条**（`thinking` → `reasoning_content`、`tool_use` → `tool_calls`、`redacted_thinking` 丢弃）。**纯文本仍折成字符串**——无图请求映射结果与加图片支持前逐字节一致。
- `tools[].input_schema` → `function.parameters`；`tool_choice` `auto`/`none` 直传、`any` → `required`、`tool` → 嵌套；`stop_sequences` → `stop`；`max_tokens`/`temperature`/`top_p` 直传；`top_k` 丢弃。
- **`cache_control` 原样透传**（默认开、无开关）：`system` 块、user/assistant 块、`tool_result`（挂 tool 消息顶层）、assistant `tool_calls` 元素、`tools` 元素。只校验结构（对象 + 非空 `type`，`ttl` 若给须字符串），取值不解释。无标记 → 映射逐字节不变。实测 6/8 渠道接受（codebuddy / codebuddy_intl / trae / qoder / codearts / kilo）；zen 因 `FreeTierError` 不能验证、OfficeAce 未跑探针（`scripts/probe_cache_control.py`）。

**出口翻译**（`response.py`，实现 `StreamSink`）：`message_start` → `content_block_start`/`delta`/`stop`（`text_delta` / `thinking_delta` / `input_json_delta`）→ `message_delta`（`stop_reason` + usage）→ `message_stop`。无 `[DONE]`。thinking 块在 `content_block_stop` 前补占位 `signature_delta`（本网关不产真签名）。错误走 `event: error`。

**心跳**：`StreamSink.keepalive()` 各出口协议自实现；`dispatch_compat` 交 `with_keepalive` 作空闲填充帧。Anthropic 用具名事件 `event: ping` + `data: {"type": "ping"}`（官方规范；只解析 `event:` 行的客户端会忽略注释帧）；chat / Responses 用 SSE 注释帧 `: keepalive`——Responses **不能**改 ping：该协议无此事件类型，严格解析器遇未知事件直接报错。

**非流式保活**（`NONSTREAM_KEEPALIVE_SECONDS`，默认 0 = 关闭）：`stream=false` 请求聚合完成前一个字节都不发 → CDN / 反代空闲超时直接切连接。`api/streaming.py::respond_with_keepalive` 用**宽限期**：先等 N 秒。

- 期间完成 → 普通 `JSONResponse`：状态码 / `Content-Length` / 字节内容与不开保活**全一致**；异常照常抛给异常处理器（400/503 不变）。
- 超时 → 才提交 200 + chunked，先吐 `b"\n"` 再补正文。

空白字节合法：RFC 8259 §2 规定 `JSON-text = ws value ws`，值前允许任意空白 → `"\n\n{...}"` 仍是合法 JSON（`json.loads` / `JSON.parse` 已实测接受）。**不用 `StreamingResponse` 起手**：200 会在聚合前发出 → 之后任何失败改不回来；宽限期把该窗口压到只有真超时请求。代价：提交 200 后才失败只能把错误写进正文（`LATE_FAILURE_PAYLOAD`）→ **默认关闭**。本部署 nginx `proxy_read_timeout 3600s` + frp TCP，无短空闲超时；30 天实测 >60s 成功请求 1741 个全成功。

**usage 缓存字段**：内部 `input_tokens` 是 OpenAI 口径（含缓存命中），Anthropic 语义互斥。命中时 `input_tokens = max(0, prompt_tokens − cached_tokens)` 并另记 `cache_read_input_tokens`；命中为 0/None 不补占位。

**鉴权**：先 `x-api-key`（`ANTHROPIC_API_KEY`），空则回落 `Authorization: Bearer`（`ANTHROPIC_AUTH_TOKEN`），共用 `_api_key_principal`。Base URL 填到根，SDK 自拼 `/v1/messages`。`count_tokens` 本地启发式估算，不转发。

**CC Switch 深链**（`web/src/lib/ccSwitch.ts`）：`ccswitch://v1/import?resource=provider&…`，`app` = `claude`/`codex`/`opencode`。`endpoint` 形态随 `apiFormat`：`anthropic` 填根不带 `/v1`；`openai_chat`/`openai_responses` 填 `/v1`。深链含 Key 明文 → 只在用户主动操作时生成（创建成功对话框 / 每行「导入 CC Switch」，后者需粘贴完整 Key，仅存内存）。需 CC Switch v4.0.10+。无 Gemini 出口。

### 3.9 上下文压缩
动机：上游输入硬限（CB `11115 prompt is too long`），换号无意义。只做确定性「估算 → 裁剪」，不做超限后压缩重试。纯函数 `engine/compress.py` + 装配闭包 `api/context.py`。

- **估算**：中文 0.55 tok/字、数字 0.33、其他 0.25（英文「3 字符 ≈ 1 token」把中文低估 1.6 倍）。**图片 part 按 URL/base64 长度计入**（按 100 字符估 → 图片会话永不触发压缩）。
- **预算** = `模型上限 × safety_ratio − reserve_for_output`。
- **裁剪**：`system` 永久保留；`assistant.tool_calls` 与其后连续 `tool` 结果**同生共死**；至少留最近 `min_keep_messages` 条；其外从新到老回填，**只保留连续后缀**——装不下就停，不跳着捞更老的小消息。跳着留会在保留集合里留洞，下一轮重算时洞的位置一变，发往上游的前缀整段失效（缓存只剩 system 段）。仍超限 → 截断最长**非 system** 消息（留头尾 + 标记）。
- **未知上限不压缩**：目录查不到 `max_input_tokens` → 直接跳过，零副作用。
- **声明值 ≠ 可用量**：TRAE 声明 200000/232768，实测 1,450,000 字符（约 1,035,544 token）仍可读，1,470,000 才报 `4022`/`4026`。故 `_CHANNEL_WINDOW_FLOOR` 按渠道抬到实测下限（TRAE `1_048_576`），**只抬不压**，只影响压缩预算（`/v1/models` 仍宣告声明值）。TRAE `context_window_tokens` 有 `{dev, max}` 两档，`dev` 保守档：取 `max`（`max > dev` 时）；否则 1M 上下文模型被报成 200K。
- **按实际渠道取值**：压缩在 executor **选号之后**执行（`ExecutorDeps.context_compress`）；路由前只能取 min 口径 → 反复误裁大窗口渠道。每轮从原文重压；affinity 指纹读未压缩原文。

热更：`CONTEXT_COMPRESS_ENABLED`（true）/ `CONTEXT_COMPRESS_RESERVE_TOKENS`（4096）/ `CONTEXT_COMPRESS_MIN_KEEP_MESSAGES`（4）/ `CONTEXT_COMPRESS_SAFETY_RATIO`（0.95）/ `CONTEXT_COMPRESS_STICKY_ENABLED`（true）。

**tool 输出压缩**（`engine/tool_output.py`，默认关）：只压 `role=tool` 且超过 `TOOL_OUTPUT_COMPRESS_MAX_CHARS`（8000）的纯文本 content——去 ANSI 转义、折叠连续重复行（`xxx (×N)`）、超长时保留「头 40 行 + 尾 40 行 + 命中错误关键字的行」并标注 `[... 省略 K 行 ...]`。错误行永不丢（避免把「为什么失败」压没）。压缩是**确定性**的：同输入同输出，客户端每轮重发完整历史时压出的前缀逐字相同，不破坏上游前缀缓存；不做模型化摘要。与窗口无关，窗口未知/未超限也生效（省的是 token 不是长度）；关闭时载荷逐字节不变。每次真正压缩会写一行 INFO（`tool 输出压缩 provider=… 字符 A→B`），验收收益直接看日志，不必翻库。

**粘性裁剪**（`engine/sticky_trim.py`，默认开）：上面「每轮按当轮预算重算最近 N 条」有一个致命副作用——客户端每轮**重发完整历史**时，多一轮就多一条消息，保留窗口的起点每轮前移一条，发往上游的前缀从第 2 条消息起就变了，上游前缀缓存只剩 `system` 段可命中。实测 qoder 单号 32 条请求里 21 条 `reusable=1`（命中率 25%）。

改为：只在超预算时裁，且一次裁到 `budget × 0.6`，把保留窗口的起点（正文绝对序号）与该条消息摘要记进进程内 LRU（按「渠道 + 模型 + 会话身份」）。之后每轮只要起点消息没变、仍是消息组边界、裁后总量仍在预算内，就沿同一起点——前缀逐字相同，缓存正常命中；直到新增消息把总量顶回预算才再裁一次。历史被改写（摘要对不上）或分组变了就丢弃记忆重裁，宁可多裁一次也不发错上下文。关闭开关即回到旧行为。

配套前提是保留集合**连续**（见上「裁剪」）：跳着捞更老的小消息会留洞，洞的位置每轮都在变，粘性起点也无法复用。实测（qoder / deepseek-v4.1-flash，客户端每轮重发 1.88M token 全量历史）：修复前该会话 32 条请求里 21 条 `reusable=1`、单号命中率 25%；修复后每次裁剪到 97K token（预算的 58%），后续每轮 `reusable=257/259`，上游 `cached_tokens/input_tokens` 稳定在 **99%+**，单号聚合从 63% 升到 99%。日志里 `reason=no_memory|digest_changed|not_group_start|over_budget` 可直接看出为什么没复用。

### 3.9.1 前缀缓存诊断

命中率按**单请求**看才有意义：`cached_tokens / input_tokens` 聚合后，一条「每轮都裁掉一条老消息、只命中 system 段」的长会话就能把单号拉低一半以上（实测 qoder 单号 25.4%，同时刻同渠道其他号 62%）。网关只落 `input_tokens` / `cached_tokens` 两个数字，看不到前缀在哪条消息分叉，所以提供按需诊断（`PREFIX_DEBUG_ENABLED`，默认关）。

- **只记摘要**：逐条消息 `sha256` 前 16 位、system/tools 摘要、`lcp`（与**同凭证同模型**上一条请求连续相同的消息条数）、`reusable`、`split`（分叉点消息摘要）。不落正文，避免把用户代码写进日志。
- **`reusable` 与 `lcp` 分开**：tools 定义排在 system 之后，`tools` 变了等于正文全部失效，故 `reusable = min(lcp, 1)`。
- **首条没有基准**：`lcp` / `reusable` / `split` 记 `None`，不把「这条是第一条」误报成「前缀完全变了」。
- **压缩会留证据**：`api/context.py` 裁剪成功时另写一行 `前缀诊断·压缩 … messages=N→M 估算=A→B sticky=… reason=…`；同一条流连续多轮都在裁 ⇒ 就是它把命中打没了，`reason` 直接给出为什么没复用上一次的窗口起点（`no_memory` / `digest_changed` / `not_group_start` / `over_budget`）。
- **开销**：关时每请求只多一次属性读取；开时 INFO 级、每请求一行，状态是进程内 LRU（64 个 `凭证+模型` 键）。

### 3.10 API Key 策略
| 列 | 取值 | 空值语义 |
|---|---|---|
| `provider_binding` | 见 `KNOWN_PROVIDERS` | 空 = 跨渠道 |
| `allowed_ips` | 逗号分隔 IP/CIDR | 空 = 不限 |
| `allowed_models` | fnmatch glob、逗号分隔 | 空 = 不限 |
| `expires_at` | epoch 秒 | NULL = 永不过期 |

- **渠道绑定**：只在绑定渠道选号；模型属他渠道 → 400 并指出归属；`模型@渠道` 冲突同样 400。执行 `Executor._apply_binding`，与 `@provider` 共用收窄路径并置 `forced`（跳过目录收窄）。目录未就绪 → 保守放行。
- **来源 IP**：`deps.api_key_user` 当场判。**默认不采信 `X-Forwarded-For`**；仅 `TRUST_PROXY=true` 取**最后一个**条目。写入 `normalize_allowed_ips` 规范化（`10.0.0.1` → `10.0.0.1/32`）；读取宽松，整份白名单解析不出 → **拒绝**（fail closed）。失败 403。
- **模型白名单**：三个 /v1 出口解析后校验；空名 / `auto` 按 `default_model` 判定（不能借空名绕过）；大小写不敏感，`@渠道` 后缀不参与；`/v1/models` 同样过滤。
- **到期**：过期与「Key 不存在」**统一 401 文案**。

### 3.11 reasoning 控制
只给机制不给策略：三项默认关 / 留空。内置默认矩阵（哪个渠道认哪些档位）一律不做（未实测即编造）。

**① `FORCE_DISABLE_THINKING`**（默认 false）：响应侧剥离。过滤点 = executor 消费事件的循环（`_stream_loop` 与 `_complete_model` 聚合前），三个出口一致生效。**刻意不放进 `_stream_source`**：`ContinuationStream` 在那层内，续写靠 reasoning 原文 → 剥在外层才既保质量又对客户端隐藏。只过滤响应不改请求体（CB `enable_thinking:false` 被忽略；不发 `reasoning_effort` 反退化成可见推演）。`usage.reasoning_tokens` 不受影响。

**② `REASONING_EFFORT_MAP`**（默认空）：格式 `渠道=from:to,from:to;*=from:to`，目标写 `none` 即删键。应用点在选号之后（`_map_effort`，`provider_id` 已知）。**只动客户端显式值**（渠道 `setdefault` 在 provider 侧、晚于此处）。需改写时返回**新 dict**，不改 `request.raw`（后者是粘性指纹输入）。

**③ `ANTHROPIC_THINKING_EFFORTS`**（默认空）：`budget_tokens` → `reasoning_effort`，格式 `预算:档位,…,disabled:档位`；取「阈值 ≤ budget 的最大档」，低于最小阈值不映射。**留空 = 完全不映射**。Anthropic 只规定 `budget_tokens ≥ 1024`、未给对应表 → 阈值须显式配置。未知 `thinking.type` 与非法预算不映射。结果写入 `ChatRequest.raw` → 再经 ② 映射。

解析宽容：`engine/reasoning.py` 忽略空段 / 缺分隔符 / 非整数阈值 → 一行笔误不连累其他条目，不抛异常。

**④ 档位自适应降级**（默认行为，无需配置）：上游明确拒收 `reasoning_effort`（HTTP 400 或流内 error 事件，文案命中 `reasoning_effort` / `effort level` / `budget_tokens` / 「推理档位」等标记）时，在**原号**上降一档重试一次（`xhigh→high→medium→low→minimal`），不换号、不冷却、不计入轮换预算。降级后的档位作为本次请求剩余尝试的上限（`_clamp_effort`）。只降一档：第二次仍被拒或已是最低档/未知档位 → 走既有 INVALID 路径（换上游，全拒才 400）。已出帧（`emitted`）后不重试（半截输出不可回滚）。

## 4. Provider 协议
```python
class Provider(Protocol):
    id: str

    # 凭证生命周期（上游协议私有，必然在 provider 内）
    def start_auth(self, callback_url: str) -> AuthSession: ...   # flow=poll|callback|paste
    def poll_auth(self, state: str) -> AuthResult | None: ...     # 仅 poll 轨道
    async def complete_callback(self, raw_url: str, state: str) -> dict: ...  # 仅 TRAE
    def import_credential(self, raw: dict) -> dict: ...
    def refresh(self, credential_data: dict) -> dict: ...

    # 额度探测（调度器依赖：健康度 + 到期阶梯）
    async def probe_quota(self, credential_data: dict) -> Quota: ...

    # 执行与分类
    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]: ...
    def classify(self, status: int, body: bytes) -> ErrKind: ...
    def list_models(self, credential_data: dict) -> list[Model]: ...
    async def aclose(self) -> None: ...
```

> 与 `src/provider/base.py` 严格一致，改一处须同步另一处。

- 凭证以 dict 传递（解密后的 `data_enc`）；`credential_from` 等能力还原为私有 dataclass，provider 不接触 sqlite。
- `stream_chat` 只产 `Event`，产出前先查 HTTP 状态码；`classify` 由 executor 调用。
- 可选能力**不实现即不定义**（不抛 `NotImplementedError`）：调用方 `getattr` 探测，缺失 → 400「该凭证不支持此操作」。当前可选集：`start_auth`/`poll_auth`、`complete_callback`（仅 TRAE）、`list_accounts`/`switch_account`（仅 CB）、`credential_from`/`checkin_scope`、`checkin`/`checkin_status`、`growth`（仅 CB）、`host`。
- CodeBuddy / TRAE / Zen / Kilo / Qoder / OfficeAce 共用 `engine/sse.py` 帧解析；CodeArts 累计全文形状自实现 `iter_data_lines`，只复用 `MAX_SSE_LINE_BYTES` / `SSEFrameTooLarge`。
- Zen 与 Kilo 最薄：只实现 `import_credential`/`classify`/`probe_quota`/`list_models`/`stream_chat`/`aclose`；签到 / 成长 / 刷新靠方法缺失自然跳过。

## 5. 各渠道协议实现
### 5.1 OpenCode Zen（`zen`）
标准 OpenAI 流式（`chat.completion.chunk` + `[DONE]`）。`events.py` 很薄，唯一要点：**末段正文与 `finish_reason` 同帧** → `parse_all_events` 同帧产出 CONTENT 与 FINISH。`[DONE]` 后 `{"choices": [], "cost": "0"}` 跳过。

渠道私有只有门禁伪装（`zen/client.py`）：

| 条件 | 要求 | 违反后果 |
|---|---|---|
| `User-Agent` | `opencode/<version>`，version ≥ 1.18.0 | 低于阈值 → 426；无版本号 → 403 |
| `x-opencode-session` | `^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$`，**前 12 位 hex 须解码回近期时间戳** | 403 |
| body `stream` | 必须 `true` | 403 |
| body `tools` | 必须同时含 `bash` 与 `read`（参数可空壳） | 403 |
| `Authorization` | `Bearer public` 可带可不带 | 带伪造 key → 401 |

会话前缀编码照官方 `Identifier.create`（`sst/opencode` `src/id/id.ts`）：`ms * 0x1000 + 1`，按位取反，取低 48 位写 12 位小写 hex；后 14 位自由。复刻见 `new_session_id` / `_session_time_prefix`。**门禁在鉴权之前**：带真实 `oc_sk_*` 反而 401。

- UA 走 `ZEN_OPENCODE_VERSION`，端点走 `ZEN_API_ENDPOINT`（受 `ZEN_ALLOWED_ENDPOINTS` 校验）。Zen **不携带任何用户 Token**，白名单只为防误配。
- **403 FreeTierError 抖动客户端内消化**：403 归 `REQUEST`（不冷却不跳渠道），但 zen 只有一条虚拟凭证 → 轮换立刻耗尽 → 503。故 `stream_chat` 退避 `GATE_403_RETRY_DELAY`（1s）后换新 session id 重发一次（`GATE_403_RETRIES=1`），仍 403 才上抛；其余状态码不重试。
- **门禁三处可配**（移动靶，改完重启即换、随时回退）：UA 模板 `ZEN_USER_AGENT`（`{version}` 代入 `ZEN_OPENCODE_VERSION`，空 = `opencode/<version>`）、API 根 `ZEN_API_ROOT`（默认 `/zen/v1`，可切 `/inference/v1`；2026-10-28 实测两路径 `GET /models` 均 200）、工具集合 `ZEN_GATE_TOOLS`（默认 `bash,read`，可切第三方实测的 `bash,glob,grep,read`）。默认值 = 本仓实测通过的形态；tools 逐字节 hash 上游是否校验**未核实**，故不实现假 hash。
- **注入空壳 + 改写/过滤回包**：门禁要 `bash`/`read`（客户端工具叫 `shell`，注入可能伪造）。① 有同义工具（`TOOL_SYNONYMS`：`bash`→`shell`/`execute`、`read`→`read_file`）→ 注入名 tool_call **改写**成同义工具透传，注入时复制其 description/parameters；② 无同义工具 → 整块丢弃 + WARNING。只在**缺失时**补骨架；按 `index` 跟踪分片；整条流无真 tool_call → `finish_reason=tool_calls` 收敛为 `stop`。
- **免费清单全动态**：`/zen/v1/models` 免鉴权但返回全部模型且**无免费标记**（`owned_by` 恒 `opencode`，无 cost）。唯一权威信号 = 匿名可用性（付费恒 401 `Missing API key.`）。两步过滤：`-free` 后缀收窄 → 并发探活只留 2xx。无静态白名单；探活为空抛协议错，列表用缓存兜底。**判活缓存 30 分钟**（`MODELS_CACHE_TTL_SECONDS`，比服务层 300s 长一档）。免费模型标 `credit_rate=0.0`（非 `None`）。

**虚拟凭证行**：`credential_data={}`、`added_by="system"`，复用调度 / 冷却 / 统计 / 粘性。`probe_quota` 恒 `Quota(probe_failed=True)` → health `None`（**未知**，非 `EXHAUSTED`）。种子幂等，**删除后重启复活**，永久停用用「暂停」。只在默认装配路径种子。

### 5.2 Kilo（`kilo`）
标准 OpenAI 兼容，无私有信封、无门禁。`events.py` 与 Zen 同薄，区别：**思考在 `delta.reasoning`**（另带 `reasoning_details`，忽略）。同帧 finish 处理，补 usage（常与收尾 `choices` 同帧）。

- **免费有权威标记故不探活**：`/models` 每条带 `isFree`（含无 `:free` 后缀的 `kilo-auto/free`、`stealth/space-bunny-alpha`）。探活白耗极小配额（网关级约 200 req/h/IP）且结果波动。过滤为空抛协议错，列表用缓存兜底。
- 元数据透传：`name` / `context_length` / `top_provider.max_completion_tokens` / `supported_parameters` / `architecture.input_modalities`。免费模型标 x0。
- 虚拟凭证行同 Zen。**错误分类**：401 / 400 / 404 / 422 → `INVALID`（401 只表示该模型需付费 key / BYOK）；403 → `REQUEST`；**429 与 502/503/504 → `MODEL`**。限流交引擎，本包不自建熔断。
- 独立 pacer `KILO_CHAT_MIN_INTERVAL`（热更，默认 0），`finally` 里 `pacer.release("kilo")`。

### 5.3 Qoder（`qoder`）
真实账号渠道，凭证须登录获得、加密入库，不种虚拟凭证。**私有 COSY 协议**：

- 推理 `POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation`。请求体 = **自定义 Base64 变体**（三段轮转 + 自定义字母表 + `=`→`$`）；认证 `Authorization: Bearer COSY.<payload_b64>.<md5sig>` + 整套 `cosy-*` 头。
- 签名 `md5(payload_b64 + "\n" + cosy_key + "\n" + date + "\n" + body + "\n" + path)`（`path` 去 `/algo`，query 不参与）。`payload` = 键排序紧凑 JSON `{cosyVersion,ideVersion,info,requestId,version}`；`cosy_key` = 临时 AES 密钥经服务端 RSA 公钥加密；`info` = AES-128-CBC 加密的身份 JSON。复用 `cryptography`。
- `cosy-machineid`/`cosy-machinetoken` 按 `uid` 稳定派生（随机机器码触发风控）。
- **信封式 SSE**：`data:{"headers":…,"body":"<内层 OpenAI chunk>","statusCodeValue":200}`。逐帧解 `body`；`body=="[DONE]"` 结束；`statusCodeValue != 200` 判上游错误（HTTP 仍可能 200）。`clean_delta` 剔除空噪声 delta。
- **保活帧 `body:"null"` 必须跳过**：上游在流中发 `{"headers":…,"body":"null","statusCodeValue":200,"statusCode":"OK"}` 保活（实测 2026-10-26）。它不含内容，早先当「内层不是对象」→ 抛 `UpstreamProtocolViolation` → 整条流判失败（16:29-16:53 连续 13 次「流式响应失败: Qoder inner chunk is not an object」），而模型其实已在正常输出——客户端只看到回复被截断。判据用**结构**（`json.loads` 后是 `None` / 空 dict / 空 list）而非字符串前缀，见 `events.is_keepalive_body`。
- **模型发现** `GET {gateway}/algo/api/v2/model/list?Encode=1`，**须带整套 COSY 头**（签名 body = `qoder_encode("")`）；裸 GET 403，方法固定 GET（POST/PUT 400）。`id` 用 `key`（如 `qmodel_38max`）、`name` 用 `display_name`。`price_factor` = credit 消耗倍率（相对值，免费给 `0.0`）。
- **签到（活动制）**：`GET /sash/api/v1/me/campaigns`（**须带 `Cosy-ClientType: 10`**，否则静默空列表）→ 筛 `actionType=="CLAIM_BENEFIT"` 且 `claimStatus=="CLAIMABLE"` → `POST .../{campaignId}/claim`（同样带头）。`replayed:true` 与 `BLOCKED`+`SAME_PERSON_ALREADY_CLAIMED` 归一为已签；其它 `BLOCKED` 才是失败。新协议无连续天数（`streak_days` 恒 None）。仅活动制接口 404/405/410 时回退旧 `daily-check-in`；两者都不可用记 skipped（`ok=False` 但不算失败）。**国际版该端点 404**。
- **签到窗口 ≠ 自然日**：上游每日 **10:00（UTC+8）** 轮换 `campaignId`，窗口 `10:00 → 次日 09:59`。按自然日封账会在凌晨封掉整天 → `CheckinResult.seal_until` 回填 `checkin_window_end(payload)`（签到类活动里**最晚**的 `endAt`，不采信 `VIEW_DETAILS`）。`CheckinTask._done_scopes` = `dict[done_key → 截止时刻]`，判据 `_sealed()`（`until > now`）。
- **额度** `GET {openapi}/api/v2/quota/usage`：`userQuota` + `addOnQuota` 合成 `remaining`/`total`，`cycle_end` 取最早 `expiresAt`。
- **节点故障归类**：Qoder 把自身节点故障包成 400（`{"code":"400","message":"[FAIL]node:… msg:Execution failed: null"}`，HTTP 与 `statusCodeValue` 都是 400）。`classify_error_code` 对 400/404/422 加响应体判据：命中 `NODE_FAILURE_MARKERS = ("[FAIL]node:", "Execution failed")` → `ErrKind.MODEL`；否则 `INVALID`。乱码模型名不带该标记 → 不误伤。全候选只因该模型冷却 → 503 文案 `model 'x' temporarily unavailable on upstream`。
- **403 判定统一（HTTP 与流内信封同一判据）**：`statusCodeValue` 403 早先由 `_decode_frame` 直接调 `classify_error_code` 判 DEAD（硬禁用），而 HTTP 403 走 `classify_status` 判 SOFT——同一个码两条路径两种后果。现在信封也走 `classify_status`，优先级：排队 → QUEUE、`DAILY_QUOTA_MARKERS`（`Billing daily count exceeded`，`code 110`「每日计费次数用尽」）→ CREDIT（冷却到次日 04:00）、带 `SESSION_DEAD_MARKERS`（`TOKEN_EXPIRE` / `12153` / `Offline user session not found`）→ DEAD、其余 → SOFT。实测 2026-10-27 15:39 `{"code":"110","message":"Billing daily count exceeded"}` 被流内路径判死，qoder `nick5080166916` 硬禁用，只能人工 revive。
- **`content: null` 须归一为空串**：Qoder 拒收带 `tool_calls` 的 assistant 把 `content` 置 `null`（OpenAI 允许）。上游把该消息当「无正文」跳过 → 紧随 `role:"tool"` 成孤儿；报文却是 `Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`——指向 tool 配对，真因在 assistant 的 null content。出站前 `build_chat_body` 的 `_normalize_null_content` 把 assistant/tool 的 `null` 改成 `""`（语义等价）。2026-10-23 实测：同一请求体只把 `null` 换成 `""` 即 400 → 200；与 `reasoning_content` / 并行调用 / 轮数无关。codebuddy_intl、trae、officeace 不敏感。
- 白名单 `QODER_ALLOWED_ENDPOINTS` 含国内 openapi+gateway 与国际版，启动时校验；`QODER_CHAT_MIN_INTERVAL`（热更，默认 5s）独立 pacer。

### 5.4 CodeArts（`codearts`）
**SDK-HMAC-SHA256**：AK/SK + `X-Security-Token`，`Authorization: SDK-HMAC-SHA256 Access=<AK>, SignedHeaders=…, Signature=…`。三处逐字节对齐：signedHeaders = **全部头**（小写、字典序）；CanonicalURI **每段 percent-encode 且末尾补 `/`**（漏尾斜杠 = `APIG.0301` 常见原因）；payload hash 取 `X-Sdk-Content-Sha256`。`signer.py` 纯函数（不含当前时间）以便固定向量测试。

**DPoP 刷新（刚性）**：`POST {sts}/v1/oauth2/tokens` `grant_type=refresh_token` 需带 ES256/P-256 JWS。refresh_token 与 `client_id=codearts-agent` + DPoP 私钥**三者绑定、一次性**——刷后须回写新 `refresh_token`（`RefreshTask` 先落库再同步账号）。ES256 用 `cryptography`（`ec.SECP256R1` + `ECDSA(SHA256)`）并需**低 S 归一化**（`s > n/2` 取 `n - s`；实测 2000 次有 991 次 high-S）。私钥随 refresh_token 持久化复用。

**刷新节流**：`REFRESH_SKEW_HOURS` 默认 24h ≫ STS 寿命 2h → `CodeArtsCredential.refresh_skew_cap_seconds = 2700`，`needs_refresh` 取 `min(skew, cap)`。**窗口须宽于轮询周期**（`REFRESH_INTERVAL_MINUTES` 默认 30）：窗口 ≤ 周期 → 整轮落在窗口外，拖到到期才刷（实测 30min 封顶 + 60min 周期 → 距到期 26s 才刷，上游回 `APIG.0602`）。45min 封顶 > 30min 周期 → 实际在剩余约 30min 处刷。

**SSE 双形状**：逐行 `data:` JSON（也有裸 JSON 行），`data:[DONE]` 结束。v2 `/api/v2/chat/completions` = 标准 OpenAI chunk（增量在 `delta`；收尾帧 `choices:[]` + `usage`；`tool_stream:true` 时分片在 `delta.tool_calls`）。旧形状 `{"text":"<累计全文>"}` = 替换语义 → `TextSnapshot` 做差（新文本以旧为前缀则只发后缀）；结束帧 `{"text":"[DONE]","error_code":"0"}`。错误两路：HTTP 非 2xx，或流内 `error_code`（`ChatAgent.*` / `TM.00001041`，HTTP 仍 200）。业务码是字符串，`Event.error_code` 只有 int 槽位 → 原始码进 `error_message`、分类进 `error_kind`。

**签到**：三个端点挂 snap 引擎根、**不带 `snap-manager`**（`/v1/statistics/plugin`、`/v1/current/user` 反之须带）：

| 用途 | 端点 | 备注 |
|---|---|---|
| 查活动 | `GET {snap}/v1/ops/delivery?channel=IDE` | 回 `data.items[]` |
| 领取 | `POST {snap}/v1/ops/claim` | body `{campaignId:1, idempotentKey:"claim_1_<ms>", channel:"IDE"}` |
| 确认 | `POST {snap}/v1/ops/confirm` | body `{campaignId:1}` |

`idempotentKey` 格式 `claim_<campaignId>_<Date.now()>`；头 `agent-type: PromptCenter`。**四态**：`ELIGIBLE` → claim + confirm；**`CLAIMED`（已领未确认）→ 只补 confirm**；`CONFIRMED`/`CONSUMED` → 已签。**claim 时积分即到账**，confirm 只推进状态 → confirm 失败不丢积分但仍返回 `ok=False` 让下轮补。活动不存在 / 已过期 `ok=True`；`ELIGIBLE` 但 `claimable=false` 或未知 status → `ok=False`。业务码走 HTTP 200（如 `40001`）。`seal_until` 留空。`checkin_scope` 用 `uid`。**签到之外的活动操作未接入**。

**额度＝积分**（`parse_credits`）：`GET {snap}/snap-manager/v1/statistics/plugin`（**前缀须带 `snap-manager`**，头 `Agent-Type: PromptCenter`）回 `metrics[]`；`usageTotalPackageCredit` = 合计，其余（Basic/OnDemand/Bonus）= 分项包。数值即积分，**不做单位折算**；合计进 `Quota.remaining/total`，分项进 `Quota.packages`；顶层 `end_date`（如 `2026-11-02`）登记进 `expiry_ladder`。取不到合计项或数值无效 → `probe_failed=True`（绝不填 0）。官方只给积分系数（GLM-5.2 0.7 / OpenPangu-2.0-Flash 0.32）无公式 → **不猜**，逐请求 `credit` 保 `None`。「保活」只由 `RefreshTask` 承担（一次性 token 两处消费会烧成废票）；`probe_quota` 只读余额。

**倍率**：`GET {snap}/v1/model/builtin`（头 `Agent-Type: PromptCenter`）每条带 `credit[]`，取首档 `ratio_display`（如 `"0.7x"`）。空列表按协议违规上抛。

**登录**：OAuth2 PKCE → `POST {snap-manager}/v1/oauth2/tokens`（authorization_code）换 `{access_key_id, secret_access_key, security_token, expiration, refresh_token}`；DPoP 私钥随 credential 生成并加密入库。回调 302 到 `http://127.0.0.1:{port}/oauth/callback`（服务端监听不到）→ **paste 轨道**：用户把打不开的回调链接粘回，`POST /api/auth/upstream/complete` 用 code + `code_verifier` + DPoP 私钥换 token。登录后用 `caller_identity`（不可用回落 `current_user`）尽力补 `uid`/`user_name`/`nickname`；失败不阻断。

**一次性票终态**：`client.refresh_token` 捕获 `TokenEndpointError` 后经 `events.relogin_required(body)` 判定，只认 `_RELOGIN_MARKERS`（`STS5.1806 the refresh token has been used` / `invalid client id` / `InvalidDPoPHeader` / `invalid_grant`）。网络 / 5xx / 限流 / 普通 4xx 仍可重试（误判会让偶发故障永久废掉好凭证）。`RefreshTask` 捕获后 `mark_relogin_required`：写可操作 `disabled_reason`、硬禁用、清模型级冷却。

**并发上限与滑动窗口**：上游每账号每约 60s 最多 3 个会话；超出 `HTTP 400` + `TM.00001041 并发会话数已达上限(3个)`（会话在 HTTP 结束后仍滞留，实测约 68s 恢复）。`Pacer` 按桶在途上限 `max_concurrency`（满则挂起等 `release`）+ 滑动窗口 `window_seconds`（同桶最近 N 秒最多放行 M 次）。装配 `lambda: runtime.codearts_max_concurrency`（默认 3）与 `lambda: runtime.codearts_request_window_seconds`（默认 60s，`0` 关闭窗口）。窗口模式配 `max_concurrency=0` 不生效。

**名额归还须显式关流**：`async for ... break` 不关闭 async generator（只在耗尽 / `aclose()` / GC 时跑 `finally`）。executor 遇 `ERROR` 的 `break` 让名额推迟归还 → `_inflight` 单调累积 → 表现「用了三次就限制」。故 `provider.base.aclose_stream` 统一关闭；`stream_guarded` / `stream` / `_stream_loop` / `complete` / `ContinuationStream.__aiter__` 五处提前结束处全部显式关闭。

白名单 `CODEARTS_ALLOWED_ENDPOINTS` 含 snap 引擎、STS、门户；`CODEARTS_CHAT_MIN_INTERVAL`（热更，默认 5s）独立 pacer。

### 5.5 CodeBuddy 国际（`codebuddy_intl`）
**独立渠道而非切 `CODEBUDDY_API_ENDPOINT`**：endpoint 是进程级单值（`Settings` 单字段）；切过去 → 存量国内凭证立刻打到国际站（两站独立账号体系，额度与模型清单不通用）。

**协议零复制**：同一套后端（同一 Keycloak realm `copilot`、同一组路径 `/v2/chat/completions`、`/v3/config`、`/v2/plugin/auth/*`、`/v2/billing/*`、`/billing/meter/*`）→ `CodeBuddyIntlProvider` = `CodeBuddyProvider` 的数据类子类（`provider/codebuddy/intl.py`），只覆渠道 id 与三项能力。**路由存在性判据**：已注册路径回 401（openresty/APISIX）；未注册回 `{"error_msg":"404 Route Not Found"}`。前缀级挂认证时 401 不带信息（`/v2/activity/*` 整前缀挂认证）→ 须另找同前缀对照路径。

**唯一行为差异：首条 system 强制注入**。国际站要求 `messages[0]` 是 system，否则 `400` + `{"code":11128,"msg":"first message is not system prompt"}`。client 构造参数 `require_leading_system` 只在国际站开；`ensure_leading_system` 仅在首条非 system 时补 `DEFAULT_LEADING_SYSTEM = "You are a helpful assistant."`。三约束：① 判首条须在 `developer → system` 归一**之后**；② 客户端自带 system 不注入、不覆盖、不重排；③ 正文不带厂商指纹（否则撞 11128）。只改 deepcopy 的出站副本，不穿透回 `request.raw`。

**能力边界**：
- **签到渠道级缺席**：路径存在（`/billing/meter/checkin-activity-status` 回 `code=0`），活动没有（`active: false`，`season`/`theme_name`/`start_time`/`end_time`/`activity_name` 全空；CLAIM 回 `{"code":10001,"msg":"签到活动未开启或已过期"}`）→ `checkin` / `checkin_scope` / `checkin_status` 三项置 `None`（保留只会每 10 分钟失败一次，触发 `task_failed` 噪音）。
- **成长中心未验证**：`/v2/activity/*` 整前缀挂认证，401 无法证明子路由存在；成长含不可逆动作 → `growth` 置 `None`，调用方 `getattr(..., None)` 自动跳过。管理台菜单按 `credential.provider === "codebuddy"` 判定，天然不出现。
- **活跃上报启用**：`/v2/report` 与 `/v2/activity/*` 不同路径，国际站回 `200 {"code":0}`（`/v2/report-nope` 回 404）。`activity_scope` 含 endpoint，两站不互相占位。

**模型合并：修渠道内重名导致的伪渠道后缀**：上游常共用展示名（`hy4-preview` / `hy4-preview-x` 都叫「Hy4 preview」）。改判据：重名时若本渠道**存在 id 归一后等于该 label key 的模型** → 用该归一键当合并键；无 id 自洽候选时（TRAE 的 `sagitta`/`aquila`）仍退回限定键。`-x` / `-sg` 是**真实不同模型**，不可合并。

**白名单互不含**：`CODEBUDDY_INTL_ALLOWED_ENDPOINTS` 默认只有国际站；填国内站地址在**启动时**抛 `ValueError`（跨站发送即凭证泄露）。**pacer 与 OAuth 各自独立**：`CODEBUDDY_INTL_CHAT_MIN_INTERVAL`（热更，默认 5s）独立 `Pacer`，`_pacer_key` 带渠道 id；两渠道各建一套 `CodeBuddyOAuth`。

### 5.6 OfficeAce（`officeace`）
**两套凭据、两个面、两种签名**。聊天面（`{gateway}/v2/chat/completions`、`/v2/models`）用**永不过期**的 `Authorization: Basic <model_app_key>:<model_app_secret>`；控制面（`/v1/subscription*`、`/v1/model-token-rates`、`/v1/claw/*`）用**约 2h** 临时 AK/SK + `X-Security-Token`。`has_control_plane` 判控制面能力（只有 app key 的凭证能聊天，但不能签到 / 探额度 → `probe_quota` 回 `probe_failed` 而非 0）。

**两条签名轨道不能互换**：控制面 `/v1/claw/*` 用 `SDK-HMAC-SHA256`（复用 `codearts.signer`）；模型网关的 `/v1/subscription*` 与 `/v1/model-token-rates` 只认 **`V11-HMAC-SHA256`**（多一层 CredentialScope `YYYYMMDD/{region}/apic`，密钥要派生）。V11 三处须原样：① 派生外层 key=**AK**、msg=**SK**；② `HMAC(HMAC(AK,SK), scope+0x01)[:32]` 转 **hex 文本**当下一轮密钥；③ **不加** `X-Sdk-Content-Sha256` 头。`CanonicalURI` 每段 percent-encode 且末尾补 `/`；**query 须参与签名**；`X-Project-ID` 有则计入 SignedHeaders。写错恒 401 `verify ak sk signature fail` / `APIG.0301`。

**网关地址归一**：`client-permission-validate` 下发的 `api_base` 带 `/v2` → `_gateway_of()` 去尾部 `/v2` 再拼端点（否则 `/v2/v2/chat/completions`）。每账号一个网关主机 → 不进 `OFFICEACE_ALLOWED_ENDPOINTS`（白名单只管控制面）。

**错误分类**：限流走**伪 200**（HTTP 200 + `data:{"error":{...}}`），且有「首帧正常、第二帧才 error」形态 → 不能首帧成功后停检查。`81004`（无模型权限）/ `81009`（模型名不认）→ `BLOCKED` 模型级负缓存；`81111`/`81112`/`81114` → 限流；`14018` → `CREDIT`。不做主动探活（耗积分）；模型目录按 `/v2/models` + 实际请求成败收敛。

**`max_tokens` 改写**（`clamp_max_tokens`）：`>= 16` 追加 1024 headroom 再夹到模型上限；`< 16` 原样；未给不补。`max_completion_tokens` 归一成 `max_tokens`。上限表：`glm-5*` 131072、`qwen3*` 65536、其余 393216。

**积分即主账**：单位 `officeace_points`；`Quota.remaining`/`total` 直接是积分。来源：新用户一次性 4000 + 每日签到 1000，各自独立到期（口径「逐笔到期」）。**不做 credit 估算**：`cost_coefficient` 与 `tokens_per_point` 的叠乘关系官方未说明。签到 `POST {gateway}/v1/subscription/bonus/claim` 是**写操作**；body 必须为空 JSON `` + `Content-Type: application/json`（无 body 回 500 `OfficeAce.11020001`）；当日重复领取幂等。收获按 `bonus_skus` 的**剩余量增量**算（`points` 是累计发放量，会虚高）。`seal_until` = 下个北京时间 0 点。`checkin_scope` 用 `account_id`；身份未知返回**空串**。

**登录走 poll 轨道**：回调由**云端**承接（`/v1/claw/auth/callback`），无需本地端口或粘链接。`POST /v1/claw/auth/state` 要 state（本地造的回 `AgentArts.11000004`）→ PKCE challenge + DPoP 公钥拼 authorize URL，套 `{auth_host}/authui/login.html?service=<encode(...)>`；轮询 `GET /v1/claw/auth/code?state=`（202 = 未完成）；拿 code 后 `POST /v1/oauth2/tokens`（authorization_code + DPoP）换临时 AK/SK；再调 `client-permission-validate` 校验真开通（未开通 `subscription: null` 时明确报错）；存 `model_app_key`/`model_app_secret` 与网关地址。`AuthStateStore` 按 **username** 分键 reservation（TTL + pending 上限）：单槽全局态会让两管理员并发登录互相覆盖；并发轮询同一 state 只有一路 `consume` 成功。`force_login` 走 `/authui/logout?service=…`，默认 False。

**配置**：`OFFICEACE_API_ENDPOINT` / `OFFICEACE_ALLOWED_ENDPOINTS` 只管控制面；`OFFICEACE_CHAT_MIN_INTERVAL`（热更，默认 0）独立 pacer。登录流量与聊天用**独立连接池**，共用出站代理。**单账号单用户**（官方《套餐管理》限制）。

## 6. 调度器
### 6.1 选号规格
```python
class Scheduler:
    MAX_ROTATE = 3
    EXPIRY_WINDOW = 36h          # 主到期排序窗口，QUOTA_EXPIRY_WINDOW_SECONDS 覆盖；≤0 关闭整套
    SECONDARY_EXPIRY_WINDOW = 7d # 次到期排序窗口（主窗口打平时才比较），QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS 覆盖
    COOLDOWN = {ErrKind.PLAN: 12h, ErrKind.SOFT: 60s, ErrKind.OTHER: 10m}
    ERR_THRESHOLD = 3                    # 连续 OTHER 错误 → 冷却

    def select(self, candidates, tried: set[str], now: int) -> str | None: ...
    def note_success(self, cred_id: str) -> None: ...
    def note_error(self, cred_id, kind, now, *, model=None) -> ErrorOutcome: ...
```

排序键（`_free_first_rank` 起，字典序）：
1. **免费优先档**：候选含 `credit_rate == 0` → 免费渠道整体排前（`None` 不算免费）。仅候选带倍率数据生效。
2. **到期额度（两级字典序）**：`expiry_ladder` 中「距到期 ≤ 主窗口」（`QUOTA_EXPIRY_WINDOW_SECONDS`，默认 36h）额度加总，多者先用；打平比次窗口（默认 7 天）；无到期信息计 0；主窗口 ≤0 → 次窗口失效。CB / TRAE / CodeArts（积分包按 `end_date`）均落阶梯。
3. **健康度三态**：`known(0-100) DESC > unknown > exhausted(-1)`。
4. **账户剩余积分**多者优先——只作健康度**打平键**。
5. 仍同分按 `credential_id` 稳定。

**手动 pin 优先**：有 `pinned` → 只在 pinned 间排序。到期积分只算一处 `expiring_credits()`：选号走 `Candidate.expiry_credits()`；列表走 `GET /api/credentials` 的 `quota_expiring_credits` / `_secondary`；共用 `expiry_windows()`。无到期信息 → `null`；窗口关闭或确无临近过期 → `0`（均不显示）。

> **TRAE 也落 `quota_expiry_ladder`**（权益包各自独立到期），两列同时填，口径「未过期 + 有余额」。`quota_cycle_end` 仍 `NULL`（各包未必共享重置点）。

### 6.2 候选收窄
`Executor._narrow_providers`：**候选只信模型目录缓存**。目录能证明归属 → 候选渠道只有它们；全不可用直接 503，不再逐渠道试（各上游只回 11102 / 4001 / 401，还会写下数小时负缓存）。目录未就绪或模型不在目录 → 按全部候选路由；`模型@渠道` 与 Key 绑定不受影响。新鲜度 TTL 300s（`MODEL_LIST_TTL_SECONDS`）+ 后台兜底刷新。

**「目录登记」的口径含探活判死的渠道**（§9.1.1）：判死只把它从**展示**里摘掉，别名登记保留——否则「没登记」回退成全部渠道扇出，正是收窄要防的那件事。

**「目录登记」的口径含探活判死的渠道**（§9.1.1）：判死只把它从**展示**里摘掉，别名登记保留——否则「没登记」回退成全部渠道扇出，正是收窄要防的那件事。

### 6.3 模型级冷却
`credential_model_cooldowns(credential_id, model, cooling_until, hits, reason)` 按 **(凭证, 模型)** 建表——账号级 `cooling_until` 放不下「同账号其他模型仍可用」。`model_probes(...)` 是**观测记录**：冷却表为调度权威（真实失败驱动）；探活表只供展示（探测失败未必是模型问题）。

- 登记名 = **该凭证所属上游的原始模型名**（`_upstream_model`）；CB 与 TRAE 大小写变体不同，归一名漏判。
- 选号路径：`executor._select` 逐凭证过滤 `is_selectable(now, 模型名)` + 注入 `Candidate.credit_rate` → 交 `Scheduler.select`；同时完成收窄、粘性命中与排序（`_sticky(...) or scheduler.select(...)`）。
- 退避：`MODEL` 10m 起翻倍封顶 2h；`BLOCKED` 6h 起翻倍封顶 24h；`CONCURRENCY` 固定 60s 不翻倍。换 reason 重新计数。
- **上游点名恢复时刻优先**：`Retry-After` → `parse_retry_after`；头缺失时 CB 走 `parse_reset_at` 读正文 `reset at … UTC+8`（`retry_hints(headers, body=raw)`，仅 CodeBuddy 传 body）。两者经 `clamp_retry_at` 钳在 `[now + 常量, now + 24h]` → `MODEL` 的 600s 只是下限，上游说 22:34 恢复即冷却到 22:34。日志按 `_scope_label` 打作用域：模型级「模型 <名>」，账号级「作用域 账号」——账号级分类的调用方同样传模型名，一律打 `模型` 会让 zen 429 读起来像模型级限流。
- 清除：`save_success(..., model=)` 只删 `reason='blocked'`；`revive` / 删除凭证 / 账号级冷却出现都清模型条目。留存任务每轮 `purge_expired_model_cooldowns()`（删行）+ `purge_expired_account_cooldowns()`（`cooling_until` 置 NULL）；列表只下发未过期。账号级那条是**卫生清理**非调度依赖——选号按 `cooling_until > now` 判断，但陈旧值让管理台「冷却」排序失真。
- 无模型名可归因 → 退化为账号级 SOFT。
- **模型级冷却来源不止模型限流**：Qoder 节点故障（§5.3）也归 `MODEL`。全部候选只因该模型被排除（`_all_model_cooled`）→ 503 文案 `model 'x' temporarily unavailable on upstream`；统计落 `error_type=model_unavailable`，其余耗尽仍 `no_healthy_credential`；**对外错误码不变**。

### 6.4 跨渠道 fallback 兼容组
热更 `MODEL_FALLBACK_GROUPS`（默认 `""` 关闭）：`组名=成员1,成员2;组名2=成员3`。

- **纯函数层**（`model_resolver.py`）：`parse_fallback_groups` 宽容解析（空段 / 缺 `=` / 空组名 / 空成员跳过）；`ordered_fallback_chain` 只映射不校验存在性：组名是**入口别名、不进链**；请求成员置首，其余按配置顺序跟随（大小写不敏感）。
- **执行层**：`@渠道` 与 Key 绑定（`target.forced`）不参与回退；目录可用时剔除不在任何候选渠道登记的回退成员，未就绪全放行；成员按名去重；链首即请求模型无条件保留。
- **非流式** `complete` 逐链项走完整选号 / 冷却 / 轮换 / 粘性 / 统计，捕获 `NoHealthyCredential` / `InvalidRequest` 续，整链耗尽才抛最后一项错误。
- **流式** `stream` 逐链项跑 `_stream_loop`，**只有本项尚未产出任何响应帧前**才允许切换（已出帧会混两模型输出）。`_ModelExhausted` 传「未出帧即用尽」；整链用尽用**最后链项**的 `translator` 产终帧。
- `preflight` 按整条链判断是否有注册上游。

### 6.5 凭证限额护栏
`credential_limits(credential_id PK, keep_credits, daily_tokens, daily_credits, per_model_tokens JSON, updated_at, updated_by)`——**缺行 = 不受限**。

- **判定**（`engine/limits.py::evaluate`，纯函数）：`keep_credits` 用 `quota_remaining ≤ keep_credits` 停整号（余额未知不猜）；`daily_tokens` / `daily_credits` 用当日累计 `≥ 上限` 停整号；`per_model_tokens` 只停该模型。`None` / `≤0` = 关闭。
- **当日口径**：北京自然日 0 点（`ZoneInfo("Asia/Shanghai")`）。
- **用量聚合**（`db/limits.py::day_usage`）：按 `credential_id` 聚合 `usage_events`（token、credit、按模型 token）。**不用 `usage_hourly`**（主键无 `credential_id`）。失败请求也计入。
- **挂钩点**：`Executor._selectable` 在 `is_selectable` 后按 `deps.limits.blocked(...)` 二次过滤。**不写进 `Candidate.is_selectable`**（后者纯函数，塞进去会让 `pool_counts` 也查用量）。按模型比对**归一名**。
- **快照缓存**：`LimitGuard` 对「限额表 + 当日用量」做进程内快照（TTL 15s）；无配置不查用量。写入后 `invalidate()` 立即生效。
- **错误语义**：全部因限额跳过 → `no_healthy_credential`（`_all_model_cooled` 判据不含限额）。

### 6.6 凭证在途租约
`MAX_INFLIGHT_PER_CREDENTIAL`（全局热更，默认 `0` 不限）：达上限后该凭证不再参与选号。**在途计数兼作负载键**：`Executor._selectable` 注入 `Candidate.inflight`，调度器按「最少连接」排（免费档之后、到期额度之前）。上限 0 时 `blocked()` 恒 False 但租约照常登记。

- **实现**（`engine/inflight.py`）：`InflightTracker` = `credential_id → 活跃租约开始时刻列表` 的进程内计数；`InflightGuard` 包一层热更上限。租约只记开始时刻、不绑请求标识，先入先出，释放任一即可。
- **为什么同步**：`acquire` / `release` / `count` 只在事件循环调用，且「选号 → 登记」间无 `await`；加异步锁会让 `_selectable` 变 async。
- **TTL 兜底**（`LEASE_TTL_SECONDS = 600`）：正常由 `finally` 释放；漏走时租约滞留，超 TTL 在计数 / 登记时回收。
- **挂钩点**：`_selectable` 在 `is_selectable` / 限额之后过滤；`_stream_loop` 与 `_complete_model` 创建上游迭代器后 `_lease`，`finally` 里 `_release`。全满时与限额同口径 `no_healthy_credential`。
- **与 pacer `max_concurrency` 的区别**：pacer 桶按渠道分、只覆盖走 pacer 的渠道；这里是**跨渠道的凭证级**上限，与渠道节流正交。

## 7. 请求时序（chat completions 主链路）
> 可视化：[`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html)。`POST /v1/responses` 同链路：先映射成 `ChatRequest`，其后全一致，出口形状由 translator 决定。

```
客户端 → POST /v1/chat/completions (Bearer sk-)
  1. deps.require_api_key：摘要查 api_keys 表 → username（+ IP/到期/模型白名单校验）
  2. request.py：校验 body → ChatRequest；model_resolver 解析候选集
     - "glm-5.2" → 各 provider 都可能；"glm-5.2@trae" → 仅 trae；auto/空 → DEFAULT_MODEL
  3. 选号（executor._select → scheduler.select）：
     a. 候选 = 模型目录能证明归属时收窄到登记了该模型的渠道（_narrow_providers）；
        收窄后若没有任何可用凭证 → 直接判「无可用凭证」出 503（候选只信模型目录）
     b. 模型级冷却过滤：逐凭证按自己所属上游的原始模型名查 (凭证, 模型) 冷却表
     c. 会话粘性：存在可选的 pinned 凭证时跳过（pin 优先），否则粘性命中且仍可选的
        凭证直接复用、不参与排序
     d. 过滤 healthy（enabled=1, disabled=0, 非冷却中）。enabled=0 只作用于本条对话
        路径：后台任务只检查 disabled
     e. 免费优先档 → 到期额度两级排序 → 健康度三态 → 账户剩余积分 → credential_id
  4. executor：解密凭证 → 按实际渠道压缩上下文 → provider.stream_chat()（≤ AUTO_CONTINUE_MAX 续写）
     - 上游 HTTP ≥400 → classify → scheduler.note_error → tried 加入 → 回到 3（最多 3 次）
     - 流内 Event.ERROR → 同上映射 → 注入出口错误帧 + 冷却 + 轮换
     - 上游 400（INVALID，如模型不存在）不冷却凭证，跳过该上游全部凭证；
       全拒时 400 invalid_request（未知模型名 ≡ 无上游提供，不走 503）
     - REQUEST 类换号但不落库：既不冷却也不累计，否则会顺手把已有的 cooling_until 写成 NULL
  5. response.py：Event → OpenAI chunk（流式）或聚合（非流式）
     - 首块补 role:assistant；上游无 index 的 tool_calls 补稳定 index，
       无 `id` 的补稳定 `id`（`call_{index}_{seq}` 记忆化）——OpenAI 客户端按
       `id` 聚合流式分片，缺 id 的分片会被丢弃，表现为工具调用静默消失
     - 收尾帧序列：finish chunk → usage 帧（choices: []）→ [DONE]；上游未报 usage
       则不补该帧（不发 0 占位）。非流式/流式收尾共用 usage_payload()
     - 直连 CodeArts v2 每 chunk 都带 usage，取收尾帧真值（取第一个会记成 0/空）
  6. stats.collector：写 usage_events（username/provider/model/tokens/latency/ttfb/ok）
  7. scheduler.note_success：清 err_count，并把本对话重新粘到实际服务的凭证
```

客户端断连：生成器关闭 / 取消 → 统计 `error_type=client_disconnect` + 关闭上游流；例外：`[DONE]` 已产出后的收尾断开（框架在结束帧 `more_body=False` 前有一拍竞态）按成功记账。

流截断：上游 HTTP 200 正常关闭、但本次尝试从未产出终态事件（`EventKind.FINISH`）→ 响应不完整。凭证照常 `save_success`（上游没报错，冷却只会误伤健康号），统计记 `ok=False` + `error_type=truncated_stream`，客户端仍收到收尾帧与 `[DONE]`。

上游模型观测：`Event.model`（各渠道协议里有 `model` 就填）与请求模型做一次按尝试的比较；双向都不构成前缀关系（`glm-5.2` vs `glm-5.2-20261001` 视为一致）时只写 WARNING「疑似静默替换」——不改响应、不冷却凭证、不写 `error_type`（与用量对账 `_check_usage_consistency` 同口径：观测信号不进失败归因，避免成功率虚低）。占位名（`auto` / `default` / `unknown` / `none`）不算证据。**TRAE 的真实模型只在元数据帧回显**（`extra_info.model`、`timing_cost.provider_model_name`），这些帧本身无下游语义，故 `TraeClient` 在流循环里先取提示、再贴到后续事件（output/done/usage 帧都不带模型）。**Qoder 实测只有 `model:"auto"`**（信封 headers 与 chunk 其它字段均无模型名），该渠道天然是观测盲区。

## 8. 数据库
DDL 以 `src/db/schema.sql` 为准。`SCHEMA_VERSION = 29`，13 张表：`api_keys` / `credentials` / `credential_limits` / `users` / `audit_events` / `usage_events` / `usage_hourly` / `growth_events` / `credit_events` / `credential_model_cooldowns` / `model_probes` / `runtime_settings` / `alert_events`。

```sql
-- conn.py 打开时执行
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;      -- 仅 api_keys 之外无外键（users 与 api_keys 仍在应用层校验）
```

- 连接：`threading.local()` 每线程一个 `sqlite3.Connection(row_factory=sqlite3.Row)`。写入统一走 `Database.transaction()`，无应用层写锁，并发由 SQLite 串行化。
- 加密：`Fernet(base64.urlsafe_b64encode(sha256(APP_SECRET).digest()))`；APP_SECRET 丢失 = 凭证全部不可解。启动按熵拒弱密钥（长度 ≥16、不同字符数 ≥8，拒重复字符 / 短周期重复串）。
- **解密失败按「这条凭证用不了」处理，不得连累整批**：早先 `credential_data` 抛 `CredentialDecryptError` 直达顶层 → 一条坏凭证就让整个聊天请求 500、整轮后台任务（额度探测 / 预刷新 / 签到 / 成长中心 / 活跃上报 / 模型探活）挂掉、整页模型列表 500（实测 2026-10-26 14:48）。现按路径分两种口径：**批处理路径**（选号、模型列表、后台任务、即时探测）捕获后**跳过该凭证**（记 skipped + 一条 ERROR 日志），其余照常；**单条探测端点**照旧如实报可行动的 500（`credential_decrypt_failed`，管理员该去处理这个号）。
- `runtime_settings(key, value, updated_at)`：纯 key/value，只存管理台改过的 key，未出现回落 env；值由 `HOT_SETTINGS` 白名单校验，非法行读取时忽略并记日志。
- 模型列表与目录不占表：进程内 TTL 缓存 + `DATA_DIR/model_catalog.json` / `openrouter_catalog.json`（§9.4）；签到去重由 `CheckinTask` 当日作用域集合实现。

**备份与恢复**（`db/backup.py`，任务与 `scripts/` 共用）：`VACUUM INTO` 写一致性快照（WAL 下直接 `cp` 丢未 checkpoint 事务）；落盘后立即 `PRAGMA integrity_check`（不过则删半成品并抛错）；产物 `0600` 目录 `0700`；每 `BACKUP_INTERVAL_MINUTES` 备份、按 mtime 保留 `BACKUP_KEEP` 份。恢复 `scripts/restore_db.py`：校验 → `.pre-restore-<ts>` 安全副本 → `os.replace` 原子替换 → 删 `-wal`/`-shm` 残留；默认预览，`--apply` 才写。**不做「服务在跑」探测**；文档要求恢复前停服、恢复后重启。

**凭证标签**（`credentials.tags`，v22；纯函数 `src/tags.py`）：逗号分隔，`MAX_TAGS=16` / `MAX_TAG_LENGTH=24`。**写入校验、读取容错**——`parse_tags` 对越界项抛 `ValueError`（端点转 400）；`tags_value` 只丢弃坏项不抛错。去重大小写不敏感、保留首次写法。半角/全角逗号与分号都当分隔符；含分隔符的列表项写入前被拒。**标签不参与调度与鉴权**。端点 `PUT /api/credentials/{id}/tags`（整体替换，`require_operator`），审计 `credential.tags`。列表响应 `tags` 是数组。

**迁移纪律**（`migrate.py`）：
- 启动读 `schema.sql` 逐条 `CREATE TABLE IF NOT EXISTS`（只加不改）。
- 新增**列**写进 `_MIGRATION_COLUMNS`，走 `ALTER TABLE ... ADD COLUMN`；`SCHEMA_VERSION + 1`。
- 新增**表**不需 `_MIGRATION_COLUMNS`：建表即完成迁移，只需 `SCHEMA_VERSION + 1` + 老库升级测试。
- 删除**表**写进 `_MIGRATION_DROPS`，走 `DROP TABLE IF EXISTS`；`SCHEMA_VERSION + 1`。
- 版本记在 `PRAGMA user_version`；库版本高于本程序 → 启动报错。
- 删除凭证显式清理其模型级冷却行（无外键级联），否则重建同 id 继承旧冷却。
- **升级只在启动时发生**：迁移与 bootstrap 挂 `build_app`；「改了 schema 但没重启」= 老库 + 新前端。

## 9. 模型目录与模型名
### 9.1 三字段与归一键
八渠道每模型统一三字段，规则在 `src/provider/naming.py`（纯函数，不 import provider 子模块）：

| 字段 | 函数 | 例 |
|---|---|---|
| 原代号 `raw_id`（渠道请求 key，**永不改动**） | — | `kmodel_latest`、`kilo-auto/free`、`longcat-2.5-preview-free` |
| 归一键（合并键 / 多渠道对外 id） | `normalize_model_key` | `kilo-auto`、`longcat-2.5-preview`、`nemotron-3-ultra` |
| 展示名 `name` | `display_model_name` | `LongCat 2.5 Preview`、`Qwen3.8 Max` |

- `normalize_model_key` 剥上游噪声再 slug 化（空格/下划线/冒号/括号 → 连字符、小写）。**免费标记**四形态：尾缀 `-free`/`_free`、路径段 `.../free`、冒号尾缀 `:free`、括号词 `(free)`；同类还有 `new`。**命名空间前缀**非身份：路径式取末段；冒号式（冒号后须有空白）削前缀。只删整段/整词标记（`freeplay`/`newson` 不动）。点号非分隔（`glm-5.2` → `glm-5-2` 就换了模型）；非 ASCII 字母数字保留。剥完只剩噪声 → 回退原串小写。
- `display_model_name` 清洗可读名（`Qwen3.8-Max` → `Qwen3.8 Max`）或由 id 派生；品牌按表纠正（机械 title-case 写错 `DeepSeek`/`MiMo`/`GLM`/`GPT`）。**幂等**。
- **合并键 = 归一键，八渠道统一**：同一模型各渠道代号互不相同（Qoder `kmodel_latest` = TRAE `kimi-k3-1` = CB `kimi-k3`）→ 纯 id 规则无法对齐，只能靠展示名。zen / kilo 也参与（Zen `nemotron-3-ultra-free` vs Kilo `nvidia/nemotron-3-ultra-550b-a55b:free`，展示名都清洗成「Nemotron 3 Ultra」）。
- **同模型异名显式规范**（`MODEL_SYNONYMS`）：如 Qoder `dfmodel` 上游名 `DeepSeek-Flash` vs 别家 `DeepSeek-V4.1-Flash`；表把「异名归一键 → (规范归一键, 规范展示名)」。另一类是空格式厂商前缀（`Xiaomi MiMo-V2.6-Pro` → `mimo-v2.6-pro`）。**只登记人工核实的，不做模糊匹配**。
- **哨兵名不跨渠道合并**（`_SENTINEL_LABELS` = `auto` / `default`）：语义只在本渠道成立 → 退回原 id 归一键、各自单列。**同渠道内归一键重复 → 退回原 id 归一键**，且不登记歧义别名。
- **对外 id 一律归一键**：多渠道取最短展示名的归一键；单渠道取该条；兜底/哨兵取原代号归一键。原代号不露面，转发经 `services.model_aliases` 映射回（`executor.upstream_model_name` 消费）；每渠道登记三键（对外 id、原代号、展示名及 slug 形式）。
- **撞车消歧**（`_disambiguate`）：按 `_sort_key` 序保留先到者，后者依次取 `_fallback_ids`（原代号归一键 → 加渠道后缀 → 数字后缀）。兜底/哨兵条目用 `_scoped_key`（`渠道\0归一原代号`）隔离，对外 id 仍是原代号归一键。
- **展示顺序**：`_PROVIDER_RANK`（codebuddy 0 → codebuddy_intl 1 → trae 2 → qoder 3 → codearts 4 → officeace 5 → 其余 6），多渠道取 `min`，同级按归一键字典序。只影响展示；调度选号顺序（`KNOWN_PROVIDERS`）不变。

### 9.2 按凭证加载与 SWR
`list_models` 先用 `credential_providers()` 求「当前有可用凭证」的渠道（`candidates(selectable_only=True)`：未暂停、未硬禁用；冷却中仍算）；`provider_id not in connected` 直接 `continue`——**没凭证不读缓存、不拉上游、不展示**。zen / kilo 有虚拟凭证。

- **失败也进 TTL**（负缓存）：`model_list_fetched_at` 记上次尝试（成功或失败）。**连续失败按 2^(n-1)×TTL 退避**（`model_list_failures`，封顶 `MODEL_LIST_BACKOFF_MAX_SECONDS` 2h，成功清零）；日志首次 WARNING、其后 INFO。
- **HTTP 出口 SWR**：TTL 到期后 `/v1/models` 与 `/api/playground/models` 走 `serve_models`：有缓存直接回旧列表，过期渠道丢后台（`_schedule_refresh`）。两条纪律：① 某渠道一条缓存都没有时才同步等（冷启动 / 新渠道）；② 后台刷新收敛在 `services.model_refresh_tasks`，`model_refreshing` 去重，`lifespan` 关闭时 `cancel()` + `await`。代价：列表最多滞后一个 TTL。
- **逐渠道增量 publish**：每拉完一条渠道 `publish_aliases()` 一次（就地更新，executor 闭包引用同一 dict），不等最慢那条（zen 逐个探活可占 12–15s；期间别名表为空会让 `_narrow_providers` 按全部渠道放行）。合并逻辑收敛到 `merged_entries()`。

### 9.1.1 探活判死只影响展示，不影响归属
`model_probes.failed()`（只看 `state='failed'`，**不再**要求负缓存仍生效）命中的 (渠道, 模型) 从**展示**里摘掉（`merged_entries` 逐渠道抽掉），但**别名登记保留**（`api/models._hidden_aliases` → `_register_entry_alias`）。

- **为什么去掉「负缓存仍生效」这个条件**：人工「立即探活」判死时通常没有负缓存（负缓存只由真实请求写入），旧口径下这些模型会一直留在 Playground——上游目录撒谎（officeAce 的 `/v2/models` 挂着 26 个模型，其中多个推理端点 404 `ModelArts.81009` / 403 `ModelArts.81004`）时最明显。
- **到期口径改为复探**：隐藏没有时间上限，恢复入口只有「再探一次成功」——`ModelProbeTask.run_once` 每轮把 `state='failed'` 的行一起复探（`ModelProbeRepository.failed_rows`，用上游原名），`ok` 即恢复展示并清负缓存。若仍挂到期在负缓存上，负缓存过期后没人再探 = 永久隐藏。
- **为什么别名必须保留**：别名表是执行侧 `_narrow_providers` 收窄候选的唯一依据。摘掉后「没有任何渠道登记该模型」→ 回退成**全部渠道扇出** → 请求被发到一堆根本没有该模型的上游，各回「无此模型」（CodeBuddy 11102 / officeAce 81004·81009 / CodeArts InferHub.002002009），并被写上 (凭证, 模型) 负缓存——污染的是**从没有该模型**的渠道。
- **实测**（2026-10-26 `deepseek-v4.1-flash-1017`）：officeAce 的 `/v2/models` 挂着它（26 个之一）但推理端点直接 404 `ModelArts.81009`（上游目录撒谎）；人工探活判死后别名表丢了归属 → 扇出到 codebuddy / codearts / codebuddy_intl → 8 条负缓存写在了从没有该模型的渠道上。
- **修好后**：收窄仍指向真正持有它的渠道（多半有负缓存兜底 → 快速 503「该模型暂时不可用」），其余渠道零调用、零负缓存。
- **补登记的键推导与真实条目同源**（`_register_entry_alias`）：两处各写一份迟早漂移，漂移表现是「用户按展示名请求时收窄失效」。**不覆盖**已有映射（真实条目优先）；**只登记有可用凭证的渠道**（与 `merged_entries` 同口径）。
- **兜底清理**：`scripts/cleanup_stale_blocked.py` 删「该渠道目录里根本没有这个模型」的负缓存行（扇出留下的噪声）。判据保守：目录里**有**这个模型的行一律保留（那是上游真把它下架了，负缓存正是应有的避让）；目录快照读不出来 → 一律不删。

### 9.1.2 「上游没这个模型」独立成 error_type
`ErrKind.BLOCKED`（上游明确说没有这个模型）统计落 `error_type=model_not_found`，**不再并进 `invalid_request`**。后者是「请求无效」（`ErrKind.INVALID` / `ErrKind.REQUEST`），归因面板显示「请求无效」，读起来像客户端参数写错了——实际是上游把该模型下架了（实测 2026-10-26 officeAce 404 `ModelArts.81009` 被标成「请求无效」）。

- 受控枚举 `CONTROLLED_ERROR_TYPES` 增 `model_not_found`（前端 `USAGE_ERROR_LABEL` 同步：「上游无此模型」）。
- 可用性视图（`GET /api/models/availability`）的计入口径增 `model_not_found`：它**仍属**模型级失败，只是分类更准。
- **收尾行不变**：轮换耗尽时那一行仍按 `_exhaust_error_type` 落 `model_unavailable` / `no_healthy_credential`——「上游没这个模型」（尝试级归因）与「这个模型现在全都不可用」（收尾行）是两件事。

**单模型查询** `GET /v1/models/{model_id:path}`：与列表**共用** `_revalidated_providers` + `_served_entries`。命中三种写法（对外 id / 展示名 / 任一渠道 `raw_ids`），`strip().lower()` 较。`:path` 容纳带命名空间的原代号（`dots-studio/dots-3-note-preview:free`）。**白名单外与不存在同形 404**（`model_not_found`），客户端无法探测池子。能力分与列表同源。

### 9.3 落盘快照与启动回灌
每次成功拉取后把**未过滤原始表**原子写进 `DATA_DIR/model_catalog.json`（tmp + `os.replace`，格式 `{"version":1,"providers":{pid:{"saved_at":…,"models":[…]}}}`）。启动时 `main._restore_model_list` **同步**读回并立即 publish 别名表（零上游请求），预热退化为后台刷新；`saved_at` 折进 `model_list_fetched_at` → 不重拉刚恢复的表。

三条纪律：① 落盘/读回一律宽容（损坏、版本不符、字段缺失、记录非 dict 只丢该渠道；`saved_at` 超 7 天整条丢弃）；② 存原始表不过滤（黑名单热更立即生效）；③ 恢复只覆盖「已注册且当前有可用凭证」的渠道。**启动预热不阻塞**：`lifespan` 把预热丢后台（zen 最慢探活十几秒，内联会让 `/health` 不响应）。另有 `MODEL_CATALOG_MINUTES` 兜底刷新。

### 9.4 模型目录（OpenRouter 单一来源）
**数据源** `https://openrouter.ai/api/v1/models`（`OPENROUTER_MODELS_URL`，匿名）。一次抓取得刊例价、明细元数据（`architecture` / `top_provider` / `supported_parameters`）与 Artificial Analysis 三项指数，供成本估算、模型列表页与 Playground 能力分。

- 后台任务 `openrouter_catalog`（`OPENROUTER_CATALOG_MINUTES`，默认 1440、下限 60、可热更）一轮建三张表：`build_price_table` / `build_model_catalog` / `build_benchmark_table`。空价表视为失败，不覆盖已有表。
- **能力分**：`intelligence_index` / `coding_index` / `agentic_index`，经 OpenRouter 公开接口分发（AA 官方 API 需 key）。字段类型意外安静跳过。
- **浮动别名取同族最新快照的分**（`_snapshot_info`）：OpenRouter 同挂浮动别名与带日期快照，用 `canonical_slug` 声明同属一次发布，且只对同一快照发一个评测变体（实测浮动别名 `Non-reasoning` 24.7、`-1017` `Max` 39.5）。本项目默认开推理，与 Max 同口径 → 按 `canonical_slug` 日期取同族最新快照；显式带日期戳的 id 仍用自己的分。家族只从带 `-YYYYMMDD` 的 canonical 派生（`mistral-large` vs `mistral-large-2512` 是不同模型）。
- **失败安静降级**：网络 / 结构坏 / 空表 / 快照损坏 / 版本不符 / 超龄一律退化为空表；条目不**带** `benchmarks` 字段（不是 `null`），页面显示 `—`。
- 落盘 `data/openrouter_catalog.json`（`CATALOG_VERSION=2`，三表同文件同 `saved_at`，超 `MAX_AGE_SECONDS`=7 天过期 → 全退空），启动同步回灌；无快照时后台补拉（`_warm_model_catalog`）。
- 挂 `app.state.price_table` / `models_dev_catalog` / `model_benchmarks` / `price_saved_at`。刷新是**就地 `clear()` + `update()`**，不换引用。

**匹配纪律**（`src/model_match.py`，三处共用）：`match_keys(*texts)` 把任意写法展开成候选键集合（归一键 + `_ALIAS_RULES` 显式等价 + 剥 `_DROP_SUFFIXES` + 版本号点/连字符互换）；`lookup` **只做等值匹配**，命中不唯一返回 `None`（`glm-5.3` 绝不匹配 `glm-5.3-flash`）。规则全显式登记并附实测来源——渠道分发代号逐 id 登记（`deepseek-v4.1-flash-0930` / `-sg` → `deepseek-v4.1-flash`），因 OpenRouter 的 `-1017` 快照有自己的价位，通用剥日期会把两套价格混成一个。

**展示范围**（同一份表）：`/v1/models` 每行可选 `benchmarks`；`/api/playground/models` 同上；`/api/model-catalog` 每行可选 `benchmarks` + `available` + 顶层 `saved_at`。**`available`** = 当前渠道真能提供（口径同 `/v1/models`），供默认关闭的「只看当前渠道」过滤。**排序**：`sort=intelligence` 按综合智能排（无分排末尾，不冒充 0 分）。**不做排序**处标注「Artificial Analysis 指数，经 OpenRouter 公开接口；非本服务实测」。**已知取舍**：`family` / `open_weights` / `release_date` 上游不给（后端固定 `null` / `false`）。

### 9.5 token 到期展示与预警
**来源**：TRAE `expires_at` 有值（= JWT `exp`）；**CB 恒为 0**（OAuth 登录与刷新都不带），只能从 JWT `exp` 解析；CodeArts 用 `expiration`，签发时间借 `refresh_token` JWT 的 `iat`。实现集中在 `provider/token_expiry.py`（渠道中立，不 import provider 子模块，否则把 `db` 层拖进来）。

- `credential_token_times(data)` 返回 `(签发, 到期)` epoch。到期优先显式 `expires_at`/`expiresAt`/`expiration`；缺失 / 非法时遍历 token 键解析 JWT `exp`；签发只来自 `iat`。拿不到返回 **0 = 未知**，**不猜本地 TTL**。**只借 `iat` 的兜底**：CodeArts access token 是 STS AK/SK（非 JWT），但 STS 响应同时轮转出 `refresh_token` JWT，其 `iat` 即签发时刻 → `_ISSUED_ONLY_KEYS` **只借 `iat`、不借 `exp`**（后者 30 天寿命会把 2h 凭证画成 30 天）。
- `jwt_times` / `jwt_expiry` 只 base64url 解码不验签；异常一律 0。`normalize_epoch()` 归一毫秒。

**落库**：`token_expires_at`（`add()` 与 `save_credential_data()` 都写回；JWT 派生值**不写回凭证 JSON**，否则刷新后旧值残留成「权威」）+ `token_issued_at`（仅诊断）。**老库不批量回填**：列表读到时按需从 `data_enc` 派生（`_token_times_from_blob()`，失败返回 `(0,0)`）。**读路径**只下发绝对 epoch，**不代前端算剩余秒数**；前端 `tokenExpiryView()` 用同一时钟现算，阈值经 `TOKEN_EXPIRY_WARNING_SECONDS` 下发。**短寿命凭证不标「即将到期」**：整段寿命 ≤ 预警窗 2 倍（CodeArts STS 2h、窗口 1h）恒标红会淹真告警；真过期（剩余 ≤0）仍标红。未知显示 `—`。接口 `GET /api/credentials` 响应带 `token_expiry_warning_seconds`，每条带 `token_expires_at` / `token_issued_at`。

### 9.6 积分变动流水
**做法：额度探测写回时比对余额，记一条流水**（`credit_events`），写入放 `save_quota` **同一事务**（分两次读改写会与并发探测交错）。

- **账本由渠道决定**（`quota_ledger`）：`pool` = 套餐/每日余额（CodeArts 之外），`credits` = 积分（CodeArts）。两本账量级与语义不同，混读会被当成同一本账。
- **归因纪律**：diff 只能看到 `[上次探测, 本次探测]` 的**净变化**（其间签到、成长、对话可能同时发生）。`source` 只表达归因已知度：`observed`（净变化，delta 可正可负或 NULL）、`sync`（首次建基线，delta NULL）。前端文案一律「净变化」，不写「签到 +5」。
- **只记有信息量的行**：首次记 `sync`；余额未变不记；变化记 `observed`；任一端未知记（delta NULL）；两端都未知不记；凭证被并发删除不记。`window_start` = **上次探测时刻**。
- 接口 `GET /api/credentials/{id}/credit-events?limit=20&ledger=pool|credits`（未知凭证 400，未知 `ledger` 回落 `pool`）。保留期 90 天（`purged_credit_events`）。

### 9.7 用量统计与成本
- **口径**：`overview` / `by_provider` / `by_model` / `by_user` / `timeline` / `model-timeline` 读**小时汇总**（`usage_hourly`，永久）；只有 `events` 读明细（90 天）。代价：最近 ≤5 分钟未进汇总的不计入。**凭证维度读明细表**（小时汇总主键无 `credential_id`），只覆盖 90 天。
- **双写**：`record()` 写明细同时增量累加当前小时行；`rollup_hourly`（每 5 分钟）全量重算对账，两者幂等。
- **TRAE credit 为推算值**（`credit_estimated`）：上游 `token_usage` 只给 token，按官方公式折算（`src/provider/trae/pricing.py`），展示加 `≈`。CB 是上游真值。与刊例不符走 `pricing.MEASURED_EFFECTIVE_OVERRIDE`。
- **成本（估算）**：`token × 公开刊例价`，单价取自 OpenRouter（抓取时乘 1e6 转 USD/百万）；按 `(输入−命中)×输入价 + 命中×缓存价 + 输出×输出价` 折美元，再按**写入时汇率**（`USD_CNY_RATE`）折人民币。**写入时定值**，历史不重算。**匹配不到就不计**（`cost_known=0` → `None`、显示 `—`）→ 成本是下限。
- **缓存命中率** = 命中 ÷ 输入 token（1 位小数）；未上报或输入 0 显示 `—`。`formatCacheRate` 唯一实现，三处共用。分组表该列是**派生只读列**（不给排序键）。
- **分组表合并列**：`请求`（数 · 成功率）、`Token`（输入/输出 · 命中率）、`速度`（正文/总产出）、`平均延迟`（耗时/首字）、`P95 延迟`，14 → 8 列。排序走 `SortMenuHead`，子列键即 `GROUP_SORT_COLUMNS` 白名单键。延迟拆两列：均值读小时汇总、P95 读明细，口径与覆盖期不同。
- **明细合并列**：`Token` / `延迟` / `速度` / `渠道 / 凭证`，15 → 约 10 列。`时间` 拆两行（`HH:mm:ss` 在上、日期在下），请求 ID 提示挂该格。`SortMenuHead` 让一个列头承载多个排序键。命中率走 `EVENT_SORT_COLUMNS["cache_rate"]`：`CASE WHEN e.input_tokens > 0 THEN CAST(e.cached_tokens AS REAL) / e.input_tokens ELSE NULL END`；速度两列由 `_row_speed_sql("e.")` 生成，与 `events()` 的 SELECT **同一片段**。
- **请求 ID**：`X-Request-ID`（`webapp/request_id.py` 最外层）。入站合法（`[A-Za-z0-9._:-]{1,128}`）沿用，否则 `uuid4().hex`；同一中间件把会话标识头写进 contextvar 供粘性读取；ID 经 `request_context` 传进 `usage_events.request_id`。**不透传上游**。
- **失败归因**（`GET /api/stats/errors`，operator+）：按 `(渠道, 模型, 错误类型)` 聚合窗口内 `ok=0` 明细。`failures` 是**尝试**口径（每次上游拒绝一行）；`rescued` = 同用户同 `request_id` 另有成功行的行数（不叠加时间窗，走 `idx_usage_request`）；`failures − rescued` = 未被救回的失败尝试数（同一请求多次失败算多次）。DEAD（硬禁用）额外落一行 `credential_unavailable`；账号被上游风控限制（11140）落 `account_restricted`，该凭证 2h 冷却且当前请求不再换号；同渠道窗口内**第二个不同凭证**也中招才整渠道退避 24h，不写 `disabled`；轮换耗尽时不再重复落收尾行，避免 `requests` 与 Key 日额度双计。
- **延迟均值只算成功请求**（`SUM(latency_ms WHERE ok=1)` / `ok_count`）。
- **图表时序指标**（`timeline` / `model-timeline`）：`METRIC_KINDS` 一张表定语义，查询侧固定取齐 10 列聚合（`_METRIC_AGGREGATES`），由 `_metric_value(metric, row)` 归一。派生指标**在聚合后相除**（成功率 `ok_count/requests`、缓存命中率 `cached/input`、单请求成本 `cost/requests`），不是逐小时比率的平均——后者会被低量小时等权带偏。无样本一律给 `null` 而非 0（均值给 0 会画成「0 ms」的假低谷，比率给 0 会显示成「0%」）。
- **图表时间轴稠密**：`usage_hourly` 只在有请求时才有行，稀疏下发会让前端把「空档 5 小时」和「相邻 1 小时」画成一样宽（分类轴压缩时间）。`_dense_hours()` 按 `since`/`until` 补齐小时桶（都不给则补首末观测之间），跨度超 `MAX_DENSE_HOURS`（92 天）时退回观测小时。缺失桶取值由 `_empty_value()` 按语义给：可加量补 0，均值/比率/成本补 `null`（前端 `connectNulls={false}` 断线）。前端 X 轴用 `type="number"` + `scale="time"` + 毫秒时间戳，按真实时间定位。
- **`timeline` 的 `__total`**：全渠道合计单独按小时聚合一次（`GROUP BY hour_utc`），**不是前端把各渠道值相加**——均值与比率相加没有意义，必须用同一小时的 `sum/count` 重新相除。
- **延迟分位时序**（`percentile-timeline`，`metric=latency|ttfb`）：按小时在 `usage_events` 上算 P50/P95（`ROW_NUMBER() OVER (PARTITION BY 小时 ORDER BY col)` + `COUNT(col) OVER`，nearest-rank `(95*n+99)/100`），只统计成功请求，仅覆盖明细保留期（90 天）。供前端「P50 中位数线 + P95 上界带」——均值会把长尾摊平（实测均值 10 s 时 P95 达 21 s）。
- **图表可读性约束**（前端）：一张图一个系列，多系列改为控件切换（渠道切换读 `__total` / 单渠道）；模型趋势最多 3 条线，其余交给分组表；曲线一律 `type="linear"`（`monotone` 会凭空造值）；比率轴在数据全部 ≥ 90% 时截断下界并在图例标注「Y 轴自 90% 起」（否则 98%–100% 的波动贴在顶边看不出，固定 0–100% 又是一条直线）。Y 轴必须显式给 `dataKey` 或数值 `domain`，否则 recharts 会把毫秒时间戳纳入定义域（曾算出 400% 的上界）；模型趋势**不能只给单条线的 `dataKey` + `auto`**——上限会按那一条算，Top2/Top3 峰值更高时被裁掉，故按所有在画线的公共最大值定 `domain`（`modelTrendAxis`，比率 / 单请求成本复用主图口径）。
- **分组表延迟四列**（`avg_latency_ms` / `avg_ttfb_ms` / `p95_latency_ms` / `p95_ttfb_ms`）：均值读小时汇总；**分位只能从明细算**（`usage_hourly` 只有 `latency_sum` / `ttfb_sum`）。`_percentile_join()` 生成「按维度分组在 `usage_events` 排名」的 LEFT JOIN 子查询，**独立**应用作用域与时间窗（列 `ts`）；用 LEFT JOIN 而非 INNER（全失败组仍要出现）。排名用 `ROW_NUMBER() OVER (PARTITION BY key ORDER BY (col IS NULL), col)` + `COUNT(col) OVER (...)`（SQLite ≥3.25）；nearest-rank 用整数 `(95 * n + 99) / 100`。`ttfb_ms` NULL 时 latency 与 ttfb **各自独立排名**。实测本地 5.7 万行按渠道 P95 约 0.09s，加 24h 窗约 0.006s。
- **输出速度**（`tps` / `tps_visible` / `tps_estimated`）：`tokens ÷ 生成时间总和`，**非逐条比值平均**。同分母两列：`tps_visible` = `MAX(output_tokens - reasoning_tokens, 0)` 之和；`tps` = `output_tokens` 之和（思考计入总产出；占 50–67% 时两者约 190 与 63 tok/s）。主口径只算经路由的成功请求且 `output_tokens > 0`，分母 `latency_ms - ttfb_ms`；两道下限：窗口 < `SPEED_MIN_GEN_MS`（200ms）或单样本 > `SPEED_MAX_TPS`（1000 tok/s）剔除（实测 901 token / 3ms = 30 万 tok/s 属测量失效）。`ttfb_ms` NULL 不进主口径。某组无样本 → 回落端到端 `latency_ms` + `tps_estimated=1`（前端加 `≈`）。两套聚合在 `_percentile_join()` 同一次扫描算完；`overview()` 另在 `usage_events` 跑 `_speed_aggregates(ref="")`。`_speed_value()` 对每分子单独判空（正文 0 → `None`，显示 `—`）。
- **逐请求速度**：`events()` 每行带 `tps` / `tps_visible`，由 `_row_speed_sql("e.")` 同时供 SELECT 与排序键。规则同源（`_routed_guard`）但**不回落估算**：无生成窗口（`ttfb_ms` NULL、输出 < 100、窗口 < 200ms 或 > 1000 tok/s）给 `NULL`；正文 0 用 `NULLIF(…, 0)` 给 `NULL`，不拿 0 冒充速度。实时流不查库，`StatsCollector._notify` 广播前调 `row_speed()`（Python 版，同一判定）。
- **回填脚本**：`scripts/backfill_trae_credit.py` / `backfill_cost.py` / `rollup_hourly.py`，均先报缺口再 `--apply`。
- **自定义时间窗**：所有统计端点接受 `since` + `until`（epoch 秒），**半开区间** `[since, until)`；可单边或都不给。取值列分流：小时汇总 `hour_utc`、明细 `ts`（`_where` 里按 `since_col` / `until_col` 指定），与 `before`（rowid 游标）分开。前端「自定义」两个 `datetime-local`（`src/lib/customRange.ts` 本地时区解析）；起点不早于终点整窗作废；`overview` 挂 `keepPreviousData`。
- **钻取与导出**：`events` 接受 `provider` / `model` / `credential_id`（AND）；分组表点行即钻取（用户维度走 `username`）。`?format=csv` 仅 admin/operator，固定 `rowid ASC`，多取一条判截断（`EXPORT_MAX_ROWS = 10000`，超限置 `X-Export-Truncated: 1`）。`src/stats/csv.py` 纯函数：以 `= + - @`（及制表符/回车）开头的**文本**单元格加前导单引号（数值原样）；UTF-8 BOM；ISO 8601 UTC；`\r\n`。CSV 与页面共用 `_ROW_SPEED_VISIBLE` / `_ROW_SPEED_TOTAL`，不可测行留空。
- **实时流（SSE）** `GET /api/stats/events/stream`：**进程内广播**而非轮询；`StatsCollector.record` 在**事务提交后**调 `LiveEventHub.publish` → 回滚行不漏出。**背压**：每订阅者有界 deque（`MAX_BUFFERED = 200`），满则丢**最旧**；唤醒用 `asyncio.Event`。**并发上限** `MAX_SUBSCRIBERS = 8`，超出 429 + `Retry-After: 10`。**作用域与过滤在订阅者侧**（`_scope` + 三过滤，`None` 不限制）；`since`/`until` 不参与。**脱敏**：payload 不含 `credential_name`，仅 admin/operator 由端点逐条补。15s 注释帧保活；`Cache-Control: no-store` + `X-Accel-Buffering: no`。生成器 `finally` 注销订阅。**单进程限制**：多进程各推自己写入的行。

### 9.8 列表排序
所有 `GET` 列表接受 `sort`（白名单键）+ `order`（`asc`/`desc`）。**未知 `sort` 或非法 `order` 回落默认，不报错**；`sort` 是白名单键而非 SQL 列名 → 用户输入永不进 SQL。契约在 `src/sorting.py`：`parse_sort_order`（唯一入口）、`sql_order`（`tiebreak` 稳定次序）、`sort_rows`（`None` 值无论升降序都排最后）。明细默认（`time` 降序）走 rowid 游标返回 `next_before`；按其它列 / 时间升序 / 显式 `offset` 改 `LIMIT/OFFSET` 并返回 `total`。适用：`/api/credentials`、`/api/users`、`/api/api-keys`、`/api/audit`、`/api/alerts`、`/api/model-catalog`、`/api/credentials/{id}/credit-events`、`/api/credentials/{id}/accounts`、`/api/stats/by-*`、`/api/stats/events`。

### 9.9 列表分页
扁平列表统一后端分页，前端共享 `Pagination`（`PAGE_SIZE_OPTIONS = [10, 20, 50, 100]`，`DEFAULT_PAGE_SIZE = 50`）。契约：`GET` 接受 `limit` + `offset`；响应在原列表键旁新增 `total`（**筛选后**条数）。仓储 `limit=None` 表示不分页。实现分流：`users` / `api-keys` / `alerts` / `audit` 走 SQL `LIMIT/OFFSET` + 独立 `count`；`model-catalog` 排序键含派生字段 → 先 `sort_rows` 再内存切片（`_filter_catalog` 支持 `q` 与 `only_available`；顶层 `count` 仍是目录全量）。游标分页的明细端点无总页数，文案退化为 `第 N 页`。分组类表不分页。

## 10. 后台任务
`TaskRunner`（`runner.py`）每类任务一个独立 asyncio 循环，失败只记日志；间隔有安全下限。`Pacer` 两模式：后台任务**严格模式**（单锁串行化）；聊天**并发模式**（`allow_concurrent=True`，按「渠道 + 凭证身份」分桶放行，只同凭证顺序连发才补最小间隔）。`stream_chat` 的 `finally` 调 `release(key)`。**抖动（`JITTER_RATIO = 0.25`）**：定值间隔（`min == max`）只向上抖 `[low, low × 1.25]`——`low` 是频率风控下限，不可击穿；`min < max` 的区间本就均匀随机，不再叠加。抖动源为构造期 `random.Random(0)`（测试可复现）。

| 任务 | 周期 | 行为 |
|---|---|---|
| 额度探测（quota_probe） | 启动立即一轮（不节流）+ 每 `QUOTA_PROBE_MINUTES`（默认 60） | 写 `credentials.quota_*` / `quota_expiry_ladder` / `quota_packages` / `health` |
| token 预刷新（refresh） | 每 `REFRESH_INTERVAL_MINUTES`（默认 30，下限 5） | 到期前 `REFRESH_SKEW_HOURS`（默认 24h）窗口内轮换；短寿命渠道自行封顶（CodeArts 2700s，必须**严格宽于**本周期）；`UpstreamReloginRequired` 时标记并硬禁用 |
| 每日签到（checkin） | 每 10 分钟 | 成功即封账（`日期:scope` → 截止时刻，内存态，跨天清理）；渠道可回填 `seal_until`（Qoder 10:00 换窗口）；失败重试 |
| 成长中心（growth） | 每 `GROWTH_INTERVAL_MINUTES`（默认 60，下限 5） | 仅 CB：7 类领取；落 `growth_events` + 回写 `growth_last_result` |
| 活跃上报（activity，默认关） | 每 10 分钟醒，仅 `ACTIVITY_REPORT_HOUR`（默认 10 点）窗口内执行 | CB + 国际版：补发 `chat_request_send`；按「endpoint + userId」隔离、当日封账 |
| 明细清理（retention） | 每 5 分钟 | `usage_events` 全量重算小时汇总（幂等 upsert）+ 90 天清理；同期限回收 `credit_events` / `audit_events` / `alert_events` / `growth_events`；顺带清过期冷却（模型级删行 + 账号级 `cooling_until` 置 NULL）。跑在 `asyncio.to_thread` |
| 数据库备份（backup） | 每 `BACKUP_INTERVAL_MINUTES`（默认 1440，下限 60） | `VACUUM INTO` + `integrity_check` + 0600，保留 `BACKUP_KEEP`（默认 7）。跑在 `asyncio.to_thread` |
| 渠道模型列表刷新（注入协程 `api/models.py::list_models`） | 每 `MODEL_CATALOG_MINUTES`（默认 30，下限 5） | 兜底重拉（TTL 门禁 + 逐渠道 publish + 落盘）。周期跟 zen 判活缓存（30 分钟）对齐 |
| 模型目录刷新（注入协程） | 每 `OPENROUTER_CATALOG_MINUTES`（默认 1440，下限 60） | 拉 OpenRouter → 三张表 → 落盘 → 就地换入 |
| 运维告警（alert） | 每 `ALERT_INTERVAL_MINUTES`（默认 5，下限 1） | 评估四类风险，命中落 `alert_events` 并可推 webhook；同 `(规则, 对象)` 静默窗内只报一次 |
| 上游状态聚合（upstream_status） | 每 `UPSTREAM_STATUS_MINUTES`（默认 5，下限 1） | 并发抓官方状态（单源超时 `UPSTREAM_STATUS_TIMEOUT_SECONDS`）→ 归一 → 原子替换内存快照；单源失败只记该源 `error`，**整轮永不抛**。启动预热一轮 |
| 模型主动探活（model_probe） | 每 `MODEL_PROBE_MINUTES`（默认 0 关闭，下限 1） | 只探仍被 `blocked` 锁住的 (渠道, 模型)：`max_tokens=16` 最小对话 → 三态判定 → 落 `model_probes`。不写 `usage_events`、不动冷却、不改健康度。`manual != ''` 跳过 |

**两个刷新任务是「注入协程」而非 tasks 模块**：`tasks/` 不 import `api/` / `benchmarks`（反向依赖）→ `TaskRunner` 接 `Callable[[], Awaitable[object]]`，由 `main.lifespan` 闭包注入；`None` 时不装配、管理台不展示。

**并发保护**：HTTP 出口与后台循环都调 `list_models` → `_refresh_providers` 持模块级 `asyncio.Lock`（粒度是整次列表刷新，跨渠道合并与别名 publish 需看到一致全集）。

**签到 / 成长的同账号隔离键**：`checkin_scope(data) or f"credential|{credential_id}"`（provider 身份未知时返回空串；共享空 scope 会让第二个账号被 `seen` 永久跳过）。

**记录口径**：`_guarded` 在任务返回 `None` 时**不入账**（`sleep` 到点但 `due()=False` 是 no-op；当成成功会让页面显示「签到刚跑过」而当天一次没签）。`TaskRun` 只在真实执行或抛异常时写入；计数 `runs` 同样只涨真实执行。运行态只在本进程内，重启归零。`TASK_SPECS` 是静态描述，与 `TaskRunner.start()` 循环一一对应；`group` 字段归属 `TASK_GROUPS`（账号维护 / 模型与渠道 / 运维与数据），随 `/api/tasks` 的 `groups` 下发，供前端二级 tab 使用；周期与开关运行时现算。`GET /api/tasks` 返回带 `server_time`（前端用服务端时钟算「距今多久」）。

**手动触发** `POST /api/tasks/{key}/run`（admin + CSRF）返回 `{...运行态, ran, busy}`，未知 key 400。复用 `_loop_specs()`；差别只有 `manual=True` 时 `activity` 绕过 `due()`，以及开关**不绕**（`run_once` 返回 `None` 记 `ran=false`）。**并发**：`self._running: set[str]` 占 key（asyncio 单线程下「查 + 加」原子），撞上直接跳过本轮；手动触发在 `_run_guarded` 之前先查，命中回 `busy=true` 不等结果。`ran` 口径 = 「真跑了一轮（含失败）」。

**成长失败判定**（三层）：协议层非 2xx → `GrowthRejected`；业务码非 0 / 缺 data / 非 JSON → `UpstreamProtocolViolation`（HTTP 200 也失败）。编排层 4xx 业务规则（名额用完、未解锁、抽奖没次数、能量不足）→ `IDLE`；5xx + 协议违规 → `FAILED`；401/403 → `session_dead`，停后续。整体失败 = `failed 且 gained=False`。接单失败三类：`prerequisite not met: <task_code>` 归一条 + `reportable=True`（IDLE）；`task does not require acceptance` 正常应答；其余逐条 `FAILED`。**契约五态**：`not_accepted` → 接单；`accepted`/`in_progress` → 等完成；`completed` → 领奖；`claimed` → 跳过。接单 body **复数数组** `{"task_codes": [...]}`。**403 非登录失效**：未解锁档位 403 +「连续登录天数不足」→ 先于 session 判定。不可逆动作由 `GROWTH_IRREVERSIBLE_ACTIONS` 总控；只挡消耗不挡查询。**清理切点对齐小时边界**（`purge_expired`）：`rollup_hourly` 整行 REPLACE → 边界小时删一半会被下轮覆盖、永久丢；代价 = 明细多留 ≤1 小时。**告警判定纯函数**（`tasks/alerting.evaluate_alerts`），阈值「≤0 即关闭」。

| 规则 | 触发 | 信号来源 |
|---|---|---|
| `pool_empty`（critical） | `total>0` 且 `ready < ALERT_POOL_READY_MIN` | `CredentialRepository.pool_counts`（与 `/healthz` 同口径） |
| `task_failed` | 某任务连续失败 ≥ `ALERT_TASK_FAILURES` | `TaskStatusStore.failing`（成功一轮清零） |
| `token_expiring` | token 剩余落在 `(now, now+ALERT_TOKEN_EXPIRY_HOURS]` | `CredentialRepository.expiring_tokens` |
| `error_rate` | 窗内失败占比 ≥ `ALERT_ERROR_RATE_THRESHOLD` 且样本 ≥ `ALERT_ERROR_RATE_MIN_REQUESTS` | `StatsCollector.window_error_rate` |

- `pool_empty` 取 `total>0`：一个凭证都没有是「没配」而非「耗尽」。
- `task_failed` 只列真跑过且达阈值的 key。
- `token_expiring`：NULL 时从密文派生，派生不出视为未知；硬禁用排除；**寿命本就短于窗口**的排除（CodeArts STS 2h vs 默认窗口 24h）。
- `error_rate` 读 `usage_events` **明细**（告警窗是分钟级）。

**投递**：每条命中落 `alert_events`（落库而非只留内存）；webhook 配置 `ALERT_WEBHOOK_URL` 时逐地址 POST（多个逗号分隔，全部成功才算 delivered）；**投递失败只记 `delivery_error`**。**静默去重**：同 `(rule, scope)` 在 `ALERT_SILENCE_MINUTES` 内只落库 / 推送一次。`AlertTask` **与运行态共享同一个 `TaskStatusStore`**（自建 store 会让「任务连续失败」永远读到 0）。

**迟滞与恢复**（`ALERT_FOR_TICKS` / `ALERT_CLEAR_TICKS`）：「连续 for_ticks 轮命中」才告警（默认 1）；「连续 clear_ticks 轮不命中」才确认恢复（默认 2）。恢复时给该 `(rule, scope)` **全部未恢复**行写 `resolved_at` 并外推 `status="resolved"` webhook；管理台把 `resolved_at` 非空显示为「已恢复」。**迟滞计数**进程内、重启归零；「哪些仍未恢复」是落库事实 → `AlertTask` 构造时从 `unresolved()` 重建 `_active`（不重建 → 重启后恢复分支遍历空集，历史行永远等不到 `resolved_at`）。重建只恢复集合与通知文案（PII 片段按 `_PII_DETAIL_KEYS` 反推），不恢复连击计数。

**签名**：webhook 恒带 `X-Coding2API-Signature: sha256=<HMAC-SHA256(APP_SECRET, body)>`，签名算在**实际发送字节**上（故发预序列化 `content` 而非 `json=`）。不设开关。

**上游状态聚合**（`src/upstream_status.py` + `src/tasks/upstream_status.py`）：**只做解释不做决策**，不进选号 / 熔断。**只存内存、不落盘**（重启后拿几小时前的「一切正常」比显示「尚未抓取」更危险）。

- **声明式源清单** `SOURCE_SPECS`（key / 展示名 / `api_url` / `page_url` / `kind` / `scope` / `related_providers`），加源加一行。`scope=channel` 与本站渠道一一对应（opencode→zen、kilo→kilo）；`scope=model` 靠 `related_providers` 标注，**不做模型名模糊匹配**。
- **四种解析器**（纯函数，`ValueError` = 顶层形状不可用）：`statuspage`（`/api/v2/summary.json`，Kilo / Moonshot / MiniMax / OpenAI / Anthropic）、`opencode`（`/api/status`，逐模型 TTFT / tps，`breakdown` 展开多行）、`betterstack`（`/index.json`，JSON:API）、`atom`（DeepSeek `/history.atom`，只有事故史）。
- **六档归一**：`operational` / `degraded` / `partial` / `major` / `maintenance` / `unknown`；Statuspage `critical` → `major`。`aggregate_level` 取最差，`unknown` 权重 -1（一个源连不上不该压过真实故障）；空输入 `unknown`。
- **失败隔离**：`fetch_source` 捕获一切 → `SourceStatus(level=unknown, error=...)`；`run_once` 全源失败也只返回不抛。页面用 `summary.abnormal`（除 operational / unknown）与 `summary.unknown`（抓取失败）分开展示。
- **事故归一**：各源生命周期写法不同 → 统一成 `Incident.ongoing` 布尔；先过滤再截断到 `MAX_INCIDENTS = 5`。
- **启动预热**：`_warm_upstream_status` 后台跑一轮，失败仅记日志；关闭时取消任务。

## 11. 出口辅助端点与错误语义
### 11.1 余额与健康检查
- `GET /v1/user/balance`：DeepSeek 形状，Bearer `sk-...`，供 Cherry Studio / cc-switch。余额来自额度探测缓存，按可用凭证汇总，单位上游 credits。从未探测成功 → `balance_known` 为 `false`（未知，不是 0）。
- `GET /health`：`{"status":"ok"}`，存活探针，不鉴权。
- `GET /healthz`：额外给池计数，`ready=0` 时 503（就绪）。两者都不鉴权，都接受 `HEAD`。

```json
{
  "status": "ok", "service": "coding2api", "version": "0.10.0",
  "credentials": {"total": 5, "ready": 4, "cooling": 1, "paused": 0, "disabled": 0}
}
```

五类计数互斥、合计 = `total`；判定优先级 `disabled → paused → cooling → ready`。口径**复用调度器的 `Candidate.is_selectable`**（`CredentialRepository.pool_counts`），不另写 SQL。

> ⚠️ **`/healthz` 的 `ready` 不看 `health`**：额度耗尽的号（`health < 0`）仍算 `ready`。侧栏「凭证管理」角标是**另一套口径**（`attention_counts`：`disabled + health < 0`）——一个问「能不能打上游」，一个问「有没有待办」，不一致不是 bug。

### 11.2 侧栏数据提醒（`GET /api/nav-badges`）
一个聚合端点覆盖整个侧栏（避免 `Layout` 挂 7 个页面级 hook），60s 轮询。

- **按角色裁剪 payload**，**缺失的键 = 无权看**（不是 0）：admin-only 的 `alerts`/`tasks`/`users` 不下发给 operator/viewer；operator 的 `credentials`/`upstream`/`upstream_level`/`model_catalog_stale` 不下发给 viewer；`api_keys` 全员但只数本人。**不做整体 403**（viewer 也要看自己的 Key）。前端 `lib/navBadges.ts` 先判 `undefined` 再判值。
- **计数与页面数据不是同一口径**：`alerts` 只数 `resolved_at IS NULL`；`tasks` 用 `last_ok is False`（`last_ok is None` 与已关闭不计）；`users` 数 `activation_digest IS NOT NULL`（含已过期）；`api_keys` 数 `expires_at <= now + 7d`（含已过期，NULL 不计）。
- **上游角标**读 `summary.abnormal`（含 maintenance）；`upstream_level` 只用于定色调。
- **模型目录陈旧**：`price_saved_at` 空 → `false`；否则 `now - price_saved_at > 2 × OPENROUTER_CATALOG_MINUTES` 才算。
- **未装配回退**：`task_runner` / `upstream_status` 未挂时回 0 / `unknown` 而非 500。
- **前端防御**：`count()` 显式校验 `typeof === "number"` + 有限正数；非数字静默不渲染。

### 11.3 Prometheus 指标（`GET /metrics`）
`src/api/metrics.py` 零依赖手写 text 格式（不引 `prometheus_client`）。默认开、**不鉴权**；`METRICS_ENABLED=false`（可热更）时 404。路由注册在 SPA catch-all 之前。

| 指标 | 类型 | 标签 | 数据源 |
|---|---|---|---|
| `coding2api_requests_total` | counter | `provider` / `model` / `status`（ok / error） | `usage_hourly`（`error = requests − ok_count`） |
| `coding2api_tokens_total` | counter | `provider` / `type`（input / output / reasoning / cached） | `usage_hourly` |
| `coding2api_credentials` | gauge | `state`（ready / cooling / paused / disabled） | `pool_counts()`（与 `/healthz` 同口径） |
| `coding2api_upstream_errors_total` | counter | `provider` / `kind` | `StatsCollector.error_totals()`（进程内，重启归零） |
| `coding2api_task_failures_total` | counter | `task` | `TaskStatusStore.failures()`（进程内） |

要点：① **禁高基数标签**（不打 `credential_id` / `api_key_id` / `username` / `request_id`）；② 前两项读**永久**小时汇总，计数跨重启单调；③ 进程内计数器随重启归零，Prometheus 按 counter reset 处理。文本转义只处理 `\` / `"` / 换行。**已知偏离**：`kind` 用进程内计数（小时汇总无 `error_type` 维度；用明细会随裁剪下降）。

### 11.3b 请求级结构化日志（`src/webapp/access_log.py`）
`RequestLogMiddleware`（纯 ASGI）每个**业务请求**结束写一行 logfmt：`event=request request_id=… method=… route=… status=… duration_ms=…`。补充而非替代 uvicorn access log（后者带 query string、无请求 ID、无法与 `usage_events.request_id` 对上）。

- **脱敏**：只取 `scope["path"]`，**从不读 `query_string`**；不碰 body / 头；**不写用户名**（靠 `request_id` 关联）。路由用**模板**（`scope["route"].path`）；未匹配回落清洗过的原始路径（`[A-Za-z0-9._~/%:@-]` 之外替换 `?`、截断 120 字符）。
- **只记业务请求**：`/api/*`、`/v1/*`、`/authorize`；静态资源与 `/health`、`/healthz`、`/metrics` 不记。
- **级别**：`5xx` → WARNING，其余 INFO。`finally` 里发；`status` 取不到按 500。
- **装配顺序**：`RequestIdMiddleware` 内侧、`BodySizeLimitMiddleware` 外侧。热更 `REQUEST_LOG_ENABLED`。

### 11.3c 服务端日志页（`src/webapp/log_buffer.py` + `src/api/logs.py`）
`LogBufferHandler` 把本进程记录写进**有界环形缓冲**（容量现读 `LOG_BUFFER_LINES`，调小立即裁最旧）。出口只有两个 admin 端点：`GET /api/logs?limit=`（默认 200）与 `GET /api/logs/stream`（SSE）。`LOG_BUFFER_LINES == 0` → 两者 404 且 root handler 摘掉。

- **脱敏是硬前提**：`redact_or_drop(line)` 命中即**整行丢弃**：独立敏感词（`token` / `secret` / `authorization` / `cookie` / `password` / `passwd` / `api_key` 及 `apikey` / `api-key` / 复数，任意大小写）、键值形态（`access_token=`、`X-Api-Key:`、`set-cookie:`、`client_secret =`、JSON 键）、密钥形状（`sk-…`，前面不能是词字符故 `task-run` 不误伤）、裸凭证（`Bearer <值>`）。空行也丢。代价：「token 已刷新」这类正常行一起被吞——**宁可漏一条，也不把凭证推进浏览器**。
- **只收本进程**：attach 到 root logger，不读文件、不 tail。
- **SSE 同范式**：先发注释帧，再回放快照转实时；回放与实时按 `seq` 去重。15s 保活；订阅者上限 4，超限 429 + `Retry-After: 10`；响应带 `Cache-Control: no-store` 与 `X-Accel-Buffering: no`。`finally` 注销订阅。
- **行内容**：`seq` / `ts` / `level` / `logger` / `message`（已脱敏、截断 4000 字符）/ `request_id`。前端只做筛选、搜索、跟随最新、暂停与清空，**无写入入口**。

### 11.3e 预算分层告警
`ALERT_BUDGET_CNY` 非空时按**本地自然月**累计 `usage_hourly.cost_cny_sum`，达到 `ALERT_BUDGET_LEVELS`（默认 50/80/95/100）中任一档即告警：

- 只报**当前达到的最高档**（`rule=budget`, `scope=budget_{level}`）。升级到更高档时旧档不再命中，由迟滞状态机写成「已恢复」，不会四条并存。100%（含超支）为 critical。
- 成本只覆盖匹配到价表与汇率的明细；未定价模型不计入 → 预算消耗会被低估。`window_cost` 同时返回可定价小时数，供后续在 detail 里暴露覆盖率。
- 月初成本归零 → 上月末的预算告警自然恢复。

### 11.3d 月度账单（`GET /api/billing`）
`require_operator`。`?month=YYYY-MM`（默认当月，按**服务器本地时区**整月）+ `?format=json|csv|jsonl`：

- `json`：`{month, start, end, users, keys, totals}`。按人读小时汇总（永久保留）；按 Key 读明细（小时汇总主键无 `api_key_id`，明细只留 90 天）并补 `username` / `key_name`。
- `csv`：按人汇总，带 BOM（Excel 中文表头）；`jsonl`：该月逐条明细（`export_events`，一行一个对象）。
- 两种导出都写 `ACTION_DATA_EXPORT` 审计；文件名 `billing-<month>.<ext>`；`Cache-Control: no-store`。
- `month` 格式 / 取值范围非法 → 400 `invalid_request`；未知 `format` → 400。

### 11.4 面向用户的错误语义
管理端点失败原因须是**稳定的机器可读枚举**，不能是异常类名。`POST /api/credentials/{id}/probe` 的失败响应：

```json
{ "probed": false, "reason": "credential_rejected", "detail": "upstream http 401" }
```

| `reason` | 含义 | 用户该做什么 |
|---|---|---|
| `credential_rejected` | 上游 401/403 拒绝凭证 | 重新登录 |
| `rate_limited` | 上游 429 | 稍后重试 |
| `upstream_unavailable` | 上游 5xx | 等上游恢复 |
| `upstream_rejected` | 其他 4xx | 检查账号状态 |
| `upstream_response_invalid` | 响应结构不符 | 可能官方接口变更 |
| `upstream_timeout` | 请求超时 | 重试 |
| `network_unreachable` | 连不上（DNS / 拒绝 / 建连超时 / TLS 中断） | 检查网络或代理 |
| `unknown_error` | 未归类 | 查 `detail` |

**客户端读体中途断开**（`ClientDisconnect`）不是服务端错误：回 `499` + `client_disconnected`，不落 500（以前会冒泡到 `ServerErrorMiddleware`，刷屏且污染错误统计）。

`detail` 仅供排查，界面不得当主提示。前端 `probeFailureLabel()` 翻中文并对未知值兜底。对外 400/503 文案只带受控标识，**不回显上游正文**。

### 11.4b 出口 IP 粘性（一号一出口，`provider/proxy.py`）
`CREDENTIAL_PROXY_POOL`（启动期，分号分隔）非空时，同一凭证固定走同一出口：

- 分配 = `sha256(credential_id) → int % len(pool)`，**不依赖进程状态**，重启后不漂移（进程内轮转会等于没做）。池成员增删会让部分凭证换桶——改池子本身就是要换出口。
- 凭证 id 走 contextvar（`request_context.set_credential_id`），由 engine / 任务层设置：推理（executor `_stream_source`，每次换号重试都重设）、额度探测、签到、token 预刷新、成长中心、管理台单条 probe/checkin。**不依赖凭证 data 里的账号字段**（八个渠道字段名不统一，缺失就会静默回落直连）。
- `httpx` 的代理是连接池级参数，所以 `LazyClients` 按 `(流/短, 代理)` 维护多份池（`_pools`）；未配池时只有一条池，行为与之前逐字节一致。显式注入的 client（测试替身）永远优先。
- 优先级：凭证池分配 → `PROVIDER_PROXIES[渠道]` → 直连。**OAuth 登录阶段不覆盖**：登录时凭证还没入库（没有 id），仍走渠道级代理/直连。

### 11.5 工具调用健壮性（`src/compat/openai/`）
- **`id` 缺失**：按 index 生成 `call_{index}_{seq}` 并记忆，每片复用同一个值（聚合型客户端按 id 拼片，空 id 会把一次调用拆成两条）。
- **DSML 泄漏回读**（`dsml.py`）：DeepSeek V3.2/V4 偶发把工具调用写成 `〈｜｜DSML｜｜tool_calls〉…` 文本。解析单/双竖线两种方言、`parameter` 标签与 V4 的「自闭合 `invoke` 带 `string` 属性」、以及缺外层包装的裸 `invoke` 块；可见正文剥掉标记。流式用增量过滤器：普通文本零缓冲，只有出现标记（或结尾停在标记前缀上）才缓冲；不完整的标记在收尾时原样放行，绝不吞正文。回读出调用时 `finish_reason` 从 `stop` 改写为 `tool_calls`（否则客户端不执行）。
- **坏参数 fail-closed**：非流式 `aggregate` 校验每个调用的 `arguments`（空串 → 合法；其余必须是 JSON **对象**），非法 → `InvalidToolArguments`（`kind()=INVALID`）：换上游重试、全部失败对外 400，**不冷却凭证**、不把坏 JSON 递给客户端。流式分片天然不完整，不逐片校验。
- **用量对账**（`engine/executor.py::_check_usage_consistency`）：成功请求比较正文体量与上报的 `output_tokens`——≥200 字符却无 token、或 >12 字符/token → WARNING（上游吞 usage / 静默替换模型的间接信号）。只告警，不改响应、不写 `error_type`。

## 12. 测试策略
**目录**：`tests/` 按 `src/` 子包镜像（`api/ auth/ compat/ config/ db/ deploy/ engine/ provider/<渠道>/ scripts/ stats/ tasks/ webapp/`），每目录一个 `__init__.py`。跨模块回归集中 `tests/regression/`；逐字节等价的常量/纯函数收敛 `tests/helpers/`；夹具按作用域放 `conftest.py`；私有替身放非收集模块 `_support.py`。**真连上游豁免**：默认全 mock；需真探活的用例标 `@pytest.mark.real_upstream("zen" | "kilo" | "openrouter")`（已注册，`--strict-markers` 生效）。

| 目标 | 覆盖 | 方式 |
|---|---|---|
| 调度器（到期/冷却/三态排序/轮换/pin） | 100% | 纯单元，注入假 provider |
| 鉴权（users/apikey/session/rbac） | 100% | 单元 + TestClient |
| SSE 解析、事件映射、OpenAI 适配、OAuth/回调 | 100% | 单元 + 真实样本 fixture（codebuddy / trae） |
| HTTP 客户端 | 100% | respx mock 状态码 + body → classify |
| 统计/查询 | 100% | sqlite 内存库集成 |
| 其余 | 100% | — |

**全量 100%（行+分支）硬门槛**：`--cov-fail-under=100`；缺口补测试，禁 pragma 达标。例外（注释说明不可达）：src/ 构造不可达兜底（`__main__`、协议演进防御、前置 return 死分支）；tests/ 替身实现宽于本用例的方法。**平台差异分支禁 pragma，须 monkeypatch 测全**。**测试不得依赖本地 `.env`**：`Settings()` 须显式传值（`Settings(_env_file=None, APP_SECRET=...)`），验证时临时移走 `.env`。**fixture** 存 `src/provider/fixtures/`，断言两方向：解析正确 + **不静默**（畸形 → `UpstreamProtocolViolation`）。codebuddy / trae 有落盘样本；zen / kilo / qoder / codearts 用逐帧单测（含畸形帧），**尚未补真实样本**——不用合成样本冒充。

## 13. 部署与配置热更
### 13.1 运行时配置覆盖层
| 类别 | 例子 | 位置 | 变更方式 |
|---|---|---|---|
| 启动期不可变 | `APP_SECRET` / `HOST` / `PORT` / `DATA_DIR` / `USERS_FILE` / 上游端点白名单 / `PROVIDER_PROXIES` | `config.Settings`（frozen） | 改 env + 重启；**不进白名单** |
| 运行时可覆盖 | `runtime_settings.HOT_SETTINGS`（60 项） | `RuntimeSettings` 覆盖层 | 管理台改，立即生效 |

启动期项拒绝热更：它们决定进程如何启动或作用于连接池；运行期变更只会让「当前进程」与「磁盘配置」静默分叉。**优先级**：`runtime_settings` 表 > `.env`。覆盖行只有被改过的 key；「恢复默认」= 删行。**读取**：`RuntimeSettings` 对热更 key 返回覆盖值，其余 `__getattr__` 委托 `Settings`；覆盖值内存缓存，写入后 `reload()`，读取不查库。**消费端** `config.live(value)` 把标量或零参 callable 统一成取值器；生产传 `lambda: runtime.xxx`。已接线点：`Scheduler.expiry_window` / `secondary_expiry_window`、`ConversationAffinity.ttl_seconds`、`Pacer.min/max_seconds`、`Executor.default_model` / `max_auto_continues` / `force_disable_thinking` / `reasoning_effort_map`、`GrowthTask.allow_irreversible`、`TaskRunner` 周期与活跃上报时点、上下文压缩四项、`USD_CNY_RATE`、`anthropic_thinking_efforts`。

**校验**：白名单外 key 拒绝；类型与范围写入前校验；跨字段组合（`pacer_min ≤ pacer_max`）用「这批写完之后」的对端值比较，失败整批不落库。坏行读取时跳过并记警告。**接口**：`GET /api/settings` 返回 `snapshot()`（`key`/`env_name`/`label`/`description`/`kind`/`value`/`default`/`overridden`/`task`/`group`）+ 覆盖计数 + `groups`；`PUT /api/settings` body `{"values": {key: 标量 | null}}`，`null` 恢复默认，写操作记审计。网关卡组表即 `GATEWAY_GROUPS`。不变量：每项**要么**属一个任务、**要么**落一个网关卡组，互斥且都有归属。

### 13.2 后台任务可视化
`SettingsPage` 两级 tabs：一级「后台任务 / 网关配置」；任务按后端下发的业务分组（`TASK_GROUPS`：账号维护 / 模型与渠道 / 运维与数据）二级切换，组内每个任务一个 tab（运行态 + 配置项），组内只有一个任务时省掉第三排；网关配置按 `GATEWAY_GROUPS` 二级切换。无配置项的分组、无已装配任务的分组不出 tab；归属对不上落「其他」，绝不吞掉配置。任务卡片内的配置项可按后端下发的 `section` 分节（运维告警 19 项 → 基础与迟滞 / 规则阈值 / 单 Key 与时段 / 月度预算），避免一长列。保存按钮与「N 项待保存」常驻顶部，草稿跨 tab 保留。`useTasks` 以 `refetchInterval: 30_000` 刷新；`/api/settings` 不自动刷新。任务卡片「立即运行」，运行中 / 已关闭时禁用；结果三态——**已跑一轮**（带 `last_report`）、**没排上**（`busy=true`）、**跑了但 no-op**（`ran=false`）。把第三种说成「已运行」就是谎报。

### 13.3 按渠道出站代理
`PROVIDER_PROXIES`（启动期，默认 `""` 直连）：`渠道=代理URL;渠道2=代理URL2`，渠道取 `KNOWN_PROVIDERS`，协议 `http/https/socks5/socks5h`。作用于该渠道**全部出站请求**（聊天、额度/模型、后台任务、OAuth）。**严格解析**：未知渠道 / 非法协议 / 缺 `=` / 空 URL 一律 `ValueError`，只忽略空段。工厂 `build_client(timeout, proxy)` 统一构造 `httpx.AsyncClient(..., trust_env=False, proxy=proxy)`。**为什么启动期**：代理作用于连接池；运行中改需重建 6 个客户端 + 3 个 OAuth 流。

**`trust_env=False` 是全项目纪律**：httpx 默认读宿主环境与 macOS 系统代理（`scutil --proxy`）→ 本机开着 Clash 时状态页 / OpenRouter 目录这类公共只读出站会被交给该代理，握手失败即整批失败（实测「上游状态全部抓取失败」「模型目录全空」）。凡自建 `httpx.AsyncClient` 的公共出站点都必须显式关闭：`upstream_status.fetch_all`、`benchmarks.fetch_openrouter_models`、`tasks/alerting` 的 webhook（`provider/proxy.build_client` 已内建）。

### 13.4 重启是必须的
- **前端**：后端对 `web/dist` 用 `FileResponse` **每次请求现读磁盘**，重建后刷新浏览器即生效。
- **后端**：`src/` 下一切只在**进程启动时**装载；进程管理器只在进程退出时拉起、不监听源码变化。

于是出现 **「新前端 + 旧后端」**：页面能打开，但新端点全 404（`webapp/static.py` 的 catch-all 排除 `/api` 与 `/v1`）→ 前端归一成通用失败，弹出与真实原因无关的兜底文案。

```bash
docker compose up -d --force-recreate   # Docker / compose
sudo systemctl restart coding2api       # systemd
```

诊断顺序（先确认版本再查业务）：

```bash
# 1) schema 版本已迁移（期望 29）且账号已导入
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version; SELECT username, role, enabled FROM users;"
# 2) 路由存在：期望 401（未登录），404 = 旧后端进程
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users
```

**结论**：改 `src/` 须重启；改 `web/` 只需重建。升级 schema 时重启同时完成迁移与引导（幂等）。

### 13.5 日志
只写 **stdout / stderr**，不写文件、不自行轮转（轮转交 Docker json-file / journald / logrotate，模板见 `deploy/`）。**须在 `build_app` 配 root logger**：uvicorn 默认 `LOGGING_CONFIG` 只配 `uvicorn` / `uvicorn.access`（`propagate=false`），root 默认 WARNING 且无 handler → `logging.getLogger(__name__)` 的 INFO 静默丢失；生产路径不经 `run()`，故配置挂 `build_app`（幂等）。级别 `LOG_LEVEL`（默认 `INFO`）；**审计日志是 INFO**，调到 WARNING 会一并关掉。

### 13.6 账号体系与审计
三层引导：① 导入 `users.txt`；② `ADMIN_USERNAMES` 点名提权为 `admin`（只剩引导期语义）；③ **仍无活跃 admin → 启动失败**。`require_admin` 管用户管理、配置、审计、告警；`require_operator` 管凭证写与只读运维页；`Principal.is_operator` 是派生 property，统计口径同界。

**会话吊销不建会话表**：`users.session_epoch` 进 Cookie 的 `ep` 声明，改密 / 降级 / 启停 / 硬删一律 `+1`；`principal_from_request` 每请求比一次。**角色不进 Cookie，每请求现读 DB**（塞进已签名 Cookie 会让降级延迟生效）。老 Cookie 无 `ep` 按 0 处理。

**一次性激活令牌**：建号 / 重置写随机占位密码 + 32 字节 URL-safe 令牌；库里只存 SHA-256 摘要 + 过期时间（TTL 24h）；明文只在响应回显一次，前端拼 `/activate?token=...`。**首登强制改密**：`must_change_password=1` 时精确白名单放行（`GET /api/auth/session`、`POST /api/auth/password`、`POST /api/auth/logout`），其余 403 `password_change_required`。

**审计** `audit_events(id, ts, actor, action, target, detail, ip, ok)`，动作常量在 `audit/actions.py`。覆盖登录成功/失败、账号建/激活/改角色/启停/改密/重置/硬删、凭证导入/删除/启停/pin/复活/切换账号、告警人工恢复，以及**API Key 发/删**（`api_key.create` / `api_key.delete`：明文只回显一次，事后只能靠审计行追责）、**用户策略**（`user.policy`）、**运行时配置**（`settings.update`，含 kill switch 与请求体落盘开关）、**明细导出**（`data.export`）。**绝不记密码或令牌明文**。保留期同 `usage_events`。

**用户级策略（`users.allowed_models` / `users.quota`）**：Playground 是免 Key 的会话直连入口，`PUT /api/users/{username}/policy` 给每个登录用户再判一次出站策略——模型白名单走与 Key 相同的 `ensure_model_allowed_for`（含「省略模型名即默认模型」的绕过口子），配额走独立的 `services.user_quota`，用量按**用户名**聚合（该用户经任何入口的流量都计入）。缺这层时 viewer 用 Cookie 就能直调任意模型、绕过 Key 的白名单与配额。

**kill switch（`gateway_paused` / `gateway_pause_reason`，热更）**：拦在 API Key 鉴权依赖里（`api/deps._ensure_not_paused`）而不是各端点——新端点不会漏拦；返 503 `service_paused` 受控文案（原因只进日志），管理台走会话鉴权因此**暂停期间仍可关掉开关**。开关动作进审计并单独记 WARNING。

**请求体诊断落盘（`dump_request_bodies`，热更）**：全服务唯一把对话写进磁盘的开关，目录 0700 / 文件 0600 （含对已存在目录的收紧）、保留 200 份且不超过 24h；开关动作进审计并单独记 WARNING。**防锁死三层**：Web 端 `_guard_last_admin` + `SelfTargetError`；CLI 删最后活跃 admin 拒绝；bootstrap 无活跃 admin 启动失败。硬删只在 `scripts/create_user.py --delete --force`（`usage_events.username` 裸文本无外键 → 硬删留孤儿统计 → UI 不暴露 DELETE）。

## 14. 已知取舍
- **同步 sqlite3 而非 aiosqlite**：本地微秒级操作，asyncio 封装开销大于收益。
- **双 httpx 客户端**：聊天流 `read=None` 防长流截断；短请求总超时 30s 防悬挂；共享 `trust_env=False`。非流式由引擎聚合同一流式上游；悬挂兜底 = `UPSTREAM_COMPLETE_TIMEOUT_SECONDS`（默认 600s，超时按瞬态错误换号）+ 事件条数上限（`complete_max_events`）。
- **手写 SQL 而非 ORM**：11 张表规模下 ORM 收益为负。
- **polling OAuth 不转回调**：上游协议决定。TRAE 回调走主端口 + `PUBLIC_BASE_URL`（基址为回环 / `0.0.0.0` 时用请求自身 origin）。`/authorize` 无鉴权，防滥用三条：无进行中登录一律拒；pending 600s TTL；回调须带匹配 `loginTraceID`。绑定优先会话 Cookie；Cookie 不在回调入口（LAN 发起 / 隧道回调）时按 `loginTraceID` 反查——trace 只见于发起那次 auth_url，等价一次性凭证。浏览器顶层导航（`Accept: text/html`）返回中文说明页，程序化客户端拿 JSON。**刻意不用 Origin/Referer 判同源**（回调本就跨源 302，`no-referrer` 可绕过）。`app.state` 只保 pending state。回调地址在浏览器侧打不开（NAS / 远程部署常见「网络错误」，refreshToken 只留在地址栏）时走粘贴兜底：`POST /api/auth/upstream/complete` 已鉴权，按会话用户名取同一条 pending，校验 state 与 `loginTraceID` 后调 `provider.complete_callback`；`/upstream/cancel` 对回调轨道只清 pending。
- **Responses / Anthropic 出口只做各自客户端子集**：不做 `store=true` / `previous_response_id`；`include=["reasoning.encrypted_content"]` 接受并忽略；不发 `[DONE]`。**未经真实 Codex CLI 端到端验证**。
- **effort 映射默认不做，但缺省补值 + 可选机制**：`setdefault` 补 `medium`，只补缺省不覆盖显式值；内置默认矩阵不做，改为默认关闭的三项机制（§3.11）。
- **Zen / Kilo 用虚拟凭证行而非特判**：调度 / 冷却 / 统计 / 粘性全按 `credentials` 行工作；种一条空凭证比在每个环节加「无凭证 provider」分支代价小。`probe_quota` 恒 `probe_failed=True`。种子只在默认装配路径且幂等；删除后重启复活是刻意的。
- **Zen 注入 `bash`/`read` 是「对上游撒谎、对客户端诚实」**：只补缺；回包按 `index` 跟踪分片，有同义工具就改写透传，没有才过滤并收敛 `finish_reason`。
- **内容风控指纹清洗只改出站副本**：客户端历史不受影响。
- **统计一律以 `usage_hourly` 为准**：代价是最近 ≤5 分钟未进汇总的不计入。
- **成本是下限**：匹配不到或未上报输入 token 的不计入（`null` 显示 `—`，不拿 0 冒充免费）。
- **应用日志只写 stderr**：自己写文件会与平台轮转争抢，容器里还会写进镜像层。
- **批量运维只收「已有单条端点」的动作**：`probe` / `toggle` / `checkin` / `revive`。`refresh` 无单条入口且强刷白耗一次性票据；`delete` 破坏性太强需二次确认。批量与单条共用实现（`probe_credential_once` / `checkin_credential_once`）。
- **批量 `toggle` 传显式 `enabled` 而非各自取反**：混选时取反会让一半号走向相反方向。
- **批量审计只记一条汇总**（`toggle` / `revive`）：50 个号是一次操作、一个意图。`probe` / `checkin` 只读、不写审计。
- **批量 `probe` / `checkin` 用信号量限制在途**（`BATCH_CONCURRENCY=4`），不复用后台 Pacer：全串行会让 50 个号等几分钟。`toggle` / `revive` 是本地 DB 写，不受限。
- **窄屏表格卡片化改样式不改 DOM**：`<768px` 一行 = 一张卡；不做「按视口渲染两套 JSX」（页面测试按 testid 断言，DOM 一变换语义就没了）。字段名走 `TableCell label` → `data-label` + CSS `::before`。`display:block` 会让 Chromium/WebKit 丢隐式表格语义 → 卡片模式由 `TableCardsContext` 补显式 `role`；不开卡片模式的表格不加 `role`。断点对齐 Tailwind `md`（`767.98px`）。
- **一个渠道一个组**：`web/src/lib/credentialList.ts` 的 `groupByProvider()` 按真实 `provider` 聚合。组上 `ledger` 由 `providerLedger()` 派生：`codearts` → `credits`，其余 → `pool`；组头额度合计标签对 CodeArts 写「积分合计」。行内额度、口径悬浮（`quotaSemantics`）与流水入口都按 `ledger` 取；`credentialState()` 只认主账 `health`。**后端不真拆**：`refresh_token` 一次性且与 `client_id` + DPoP 私钥绑定 → 两条凭证行各刷一次、第二条必报 `the refresh token has been used` 并烧掉票据。
