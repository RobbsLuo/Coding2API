# Coding2API

把 **CodeBuddy**（国内站 + 国际版）、**TRAE SOLO**、**OpenCode Zen**、**Kilo Gateway**、**Qoder**、**CodeArts**、**officeAce** 八个上游渠道封装为 OpenAI 兼容 API，提供公共凭证池、统一模型名与调度、按人用量统计。

> [!WARNING]
> 逆向接口，仅供学习研究，未做安全审计；公网部署需反向代理 + 鉴权 + IP 白名单。

## 特性

- **OpenAI 兼容出口**：`/v1/chat/completions`（流式 + 非流式）、`/v1/responses`（Codex CLI）、`/v1/messages`（Anthropic / Claude Code）、`/v1/models` 与 `/v1/models/{id}`（单模型查询）、`/v1/user/balance`（DeepSeek 兼容余额）。
- **统一调度**：扁平模型名按健康度自动选号，`模型@渠道` 强制指定。候选含免费渠道时**免费优先**，之后**在途最少优先**（并发摊到空闲号），再按到期额度、三态健康度 + 分级冷却避开坏号，打平则**余额多者优先**。模型级限流只避让该模型（同账号其他模型立即可用）；某模型只有一个渠道持有且它在模型级冷却时，直接回「该模型暂不可用」。同一对话多轮粘住同一凭证，出错才轮换。
- **到期额度优先消化**：主窗口（默认 36h）内将过期的额度多者先用，避免过期浪费；打平再比 7 天窗口。额度单位统一为积分。
- **模型列表按渠道凭证加载**：只展示**当前有可用凭证**的渠道，未接入不出现幽灵模型；暂停或凭证失效时其模型暂时消失，恢复即回来。
- **公共凭证池**：admin 集中维护、全员共享、加密入库（`APP_SECRET`）；设备码登录、多账号切换、额度探测、每日签到、token 预刷新。
- **三角色账号体系**：`admin` / `operator` / `viewer`，用户存 SQLite；一次性激活链接自设密码、首登强制改密、改角色/停用即时吊销会话；登录与写操作留审计。
- **成长中心**（仅 CodeBuddy）：自动领 Buddy 旅行礼物、派 Buddy、领任务奖、断登补登、连登兑换、开盲盒；不可逆动作可用 `GROWTH_IRREVERSIBLE_ACTIONS=false` 关停；管理台可手动执行。
- **脱敏统计**：不存对话内容；明细 90 天、小时汇总永久；按人/渠道/模型/凭证可视化；按 OpenRouter 刊例价估算成本（人民币为主）；明细面板带「实时」开关（SSE 推送刚落库的请求）。
- **上游状态聚合**：把各模型 / 网关提供方的官方状态页（OpenCode Zen、Kilo、DeepSeek、Together、Moonshot、MiniMax、OpenAI、Anthropic）归一化后并排展示，用于区分「上游整体故障」与「本服务凭证 / 额度问题」。只做解释、不参与选号 / 熔断；快照只存内存、重启归零。
- **侧栏数据提醒**：侧栏对应项上显示待办计数角标（凭证需重登/耗尽、未恢复告警、上次失败任务、异常上游源、待激活账号、本人将到期 Key、模型目录陈旧），不点进去也能看到；只数「待办」、按角色裁剪，见[下文](#侧栏数据提醒导航角标)。
- **管理台安全加固**：登录限流、CSRF 校验、请求体上限、Host 白名单。
- **窄屏表格转卡片**：列多的表格（凭证 / 明细 / 模型列表等）在 <768px 自动由「一行 = 一张卡」呈现——列名收成一排可点的排序片，每个字段在自己行首显示字段名；DOM 不变，读屏语义与宽屏一致。

协议层实现（私有信封、签名、门禁伪装等）见 [`TECHNICAL.md`](TECHNICAL.md)。

## 快速开始

### 前置要求

Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js 24+ 与 pnpm 10+（仅构建前端需要）。

### 本地运行

```bash
uv sync
# 建首个管理员（之后都在管理台「用户管理」里加人）
uv run python scripts/create_user.py admin --role admin
cd web && pnpm install && pnpm build && cd ..
APP_SECRET="换成你自己的随机字符串" \
  uv run python -m uvicorn src.main:build_app --factory --port 8000
```

打开 <http://127.0.0.1:8000> 登录管理台。

> `scripts/create_user.py` 直接写 SQLite（`--db` 或 `DATA_DIR`，默认 `data/coding2api.sqlite3`）。老部署若已有 `secrets/users.txt`，启动时自动导入一次（已存在的不覆盖）。

### Docker

```bash
cat > .env <<'EOF'
APP_SECRET=换成你自己的随机字符串
PUBLIC_BASE_URL=http://127.0.0.1:8000
EOF
docker compose build
docker compose up -d
# 建首个管理员（容器内执行；之后在管理台加人，无需再进容器）
docker compose exec coding2api python scripts/create_user.py admin --role admin
```

拉取 GHCR 镜像（推荐，跳过本地构建）：

```bash
docker compose pull
# 或指定版本：docker pull ghcr.io/robbsluo/coding2api:v0.9.0
```

推送新版本：打 tag `v*` 推到 main 即触发 publish workflow（`.github/workflows/publish.yml`），同时打 `<tag>` 与 `:latest` 到 GHCR；也可在 Actions 页面手动触发。

## 用户与角色

账号存在 SQLite 的 `users` 表里，管理台「用户管理」页（仅管理员可见）负责日常增删改。

| 角色 | 能做什么 |
|---|---|
| `admin` | 用户管理、任务与配置、凭证读写、全部统计、审计日志、服务端日志、上游状态、模型列表 |
| `operator` | 凭证读写（含导入 / 删除 / 启停 / 签到 / 成长 / 切换账号）、全部统计、上游状态、模型列表 |
| `viewer` | 只读：看仪表盘、凭证列表、统计与 Playground |

**加人**：管理员填用户名 + 角色，创建后得到**一次性激活链接**（有效 24 小时，只显示这一次）。把链接经可信渠道发给本人，对方打开 `/activate` 自设密码即可登录——系统里不存在「管理员知道但用户不知道」的初始密码。忘记密码同理：点「重置密码」生成新链接。

**改角色 / 停用**：即时生效——立刻作废该账号所有已签发会话 Cookie（重新登录即可）。停用是默认的「删人」方式（可逆，用量统计仍归属该用户名）；**硬删只在 CLI**（会让 `usage_events` 留下查不到用户名的孤儿记录）。

**自助改密**：右上角用户菜单 →「修改密码」，需要当前密码。

**防锁死**：不能停用/降级**最后一个活跃管理员**，也不能对自己降级或停用。要交接权限，先加另一个 `admin` 再降自己。

**CLI 兜底**（无 Web 场景 / 全部管理员失联时）：

```bash
python3 scripts/create_user.py alice --role admin   # 交互输入密码
python3 scripts/create_user.py --list
python3 scripts/create_user.py alice --delete --force   # 硬删，不可逆
python3 scripts/create_user.py --db data/x.sqlite3 ...  # 指定库
echo "$PASSWORD" | python3 scripts/create_user.py alice --role admin --password-stdin  # 密码不进 argv / shell 历史
```

`ADMIN_USERNAMES` 与 `secrets/users.txt` 只在**引导期**起作用（首个管理员、老部署升级）：启动时把 `users.txt` 里的用户导入 DB（已存在的不覆盖），再把 `ADMIN_USERNAMES` 点名的人提权为 `admin`；此后角色只来自 DB。

## 审计

「审计日志」页（仅管理员可见）记录登录与写操作，可按操作者/动作筛选、分页查看：**登录**（成败都记）、**账号变动**（新建、激活、改角色、禁用/启用、改密、重置密码、硬删）、**凭证写操作**（导入、删除、启停、指定 pin、复活、切换账号）与**告警人工处置**（标记恢复）。每条含时间、操作者、动作、对象、说明与来源 IP。**绝不包含密码或令牌明文**（一次性激活令牌明文只在创建响应出现一次，库里只有 SHA-256 摘要）。保留期与请求明细一致（默认 90 天），由后台清理任务裁剪。

## 服务端日志

「服务端日志」页（`/logs`，仅管理员可见）是**只读**的进程内实时日志视图：`GET /api/logs?limit=` 取最近若干行，`GET /api/logs/stream` 走 SSE 先回放缓冲、再推新行。页面上可做级别筛选、关键字过滤、跟随最新、暂停与清空——**没有任何写入或执行入口**，订阅者只能读。

- **只收本进程 `logging` 记录**，不读文件、不 tail：多进程 / 容器下各进程只看得见自己那份，这是刻意的（日志文件轮转与权限差异会让「看文件」变成一个会骗人的视图）。
- **脱敏是硬前提**：每行发出前过白名单，命中 `token` / `secret` / `authorization` / `cookie` / `password` / `api_key`（含 `X-Api-Key:`、`client_secret=`、JSON 键形态）、`sk-…` 形状或 `Bearer <值>` 的**整行丢弃**。宁可漏一条日志，也不把凭证推进浏览器。
- **缓冲有界**：容量是 `LOG_BUFFER_LINES`（默认 500，可热更），环形覆盖最旧；调小立即裁掉多余行。`0` 关闭整页（两个端点都 404，handler 也摘下），与 `/metrics` 关闭同口径。
- 单行上限 4000 字符；每行带 `seq`（回放与实时去重）、时间、级别、logger、`request_id`（有则带上）。订阅者上限 4，超限 429 + `Retry-After`。

## 使用

1. 「凭证管理」添加凭证：
   - **CodeBuddy**（含国际版）：设备码登录，或粘贴 `{"token":"..."}`。
   - **TRAE**：粘贴凭证 JSON（`accessToken`/`uid`/`refreshToken`）或回调链接（`apiHost` 只接受官方地址）。
   - **Qoder / CodeArts**：设备码 / OAuth2 授权码登录（CodeArts 授权后把打不开的本地回调链接粘回页面）。
   - **officeAce**：点「登录 officeAce」走华为云授权（回调由云端承接，无需粘链接、无需开本地端口）。
   - **OpenCode Zen / Kilo Gateway**：无需添加——启动时自动出现虚拟凭证（被删可在同面板点「添加」补回）。
2. 「API Key」创建 `sk-...`（明文仅显示一次）。
3. 调用：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'
```

`模型@渠道` 强制指定上游（如 `glm-5.2@trae`、`mimo-v2.5-free@zen`），不写则自动选健康渠道。任意 OpenAI 兼容客户端可直接接入（Base URL `http://127.0.0.1:8000/v1`、Key 用 `sk-...`、模型名以 `GET /v1/models` 为准）；「Playground」页用登录会话直接测试，无需 API Key。

### 模型名三字段

八条渠道每个模型统一为三个字段：

1. **原代号**（`raw_id`）：渠道请求时真正发的 key，**永不改动**。各渠道内部代号互不相同（Qoder `kmodel_latest` = TRAE `kimi-k3-1`），也可能带命名空间前缀（Kilo 的 `kilo-auto/free`）。
2. **归一键**：剥掉免费档标记（`-free` / 路径段 `free`）与命名空间前缀，再 slug 化——`kilo-auto`、`longcat-2.5-preview`。模型列表按它判断**哪个是同一个模型**并合并，故 Zen 的免费档能与 TRAE 的付费档并成一条。
3. **展示名**（`name`）：清洗后的可读文本，如 `LongCat 2.5 Preview`、`Kimi K3`。上游给了可读名就用（`Qwen3.8-Max` → `Qwen3.8 Max`），没有就从原代号派生；品牌与缩写按官方写法纠正（`DeepSeek` / `MiMo` / `GLM` / `GPT`）。

`GET /v1/models` 里**所有条目的 `id` 都是归一键**，转发到某渠道时自动换回该渠道原代号，`by_provider.{渠道}.raw_id` 可见。**原代号、展示名、归一键三种写法都能直接请求**（`kmodel_latest`、`Kimi K3`、`kimi-k3` 指向同一模型）。同渠道内归一键重复时退回原代号，避免撞名误并。**福利模型标记 `benefit`**：CodeArts 走每日免费 token 池的模型上游不给倍率，条目会带 `benefit: true`（单渠道在顶层、多渠道在 `by_provider.{渠道}`），Playground 据此在渠道徽章上打「福」标，与按积分计费的内置模型区分。

**价格与档位**：条目可选带 `cost {input, output, cache_read}`（USD / 百万 token，与统计侧成本估算同一份 OpenRouter 价表）与 `reasoning_efforts`（该渠道生效中的 `REASONING_EFFORT_MAP` 目标档位去重集合）。两者都是**有才给**：缺价、未配置档位映射时**不带该字段**，不填 0 也不编造。

**自定义模型别名**（`MODEL_ALIASES`，可热更）：给客户端一个稳定的对外名，上游改 id 时只改配置。格式 `对外名=模型@渠道;对外名2=模型`，`@渠道` 省略则自动选。**列表与请求解析用同一份别名表**（不会出现「列表有、请求 404」），条目带 `custom_alias: true`。与真实模型 id 或自动别名撞车、目标模型不存在、钉的渠道不持有该模型时，该段记 ERROR 并**跳过**（不静默覆盖真实 id，也不因一条笔误让整份配置失效）；不支持别名链。`GET /v1/models/{id}` 同样认别名。

**`GET /v1/models/{id}` 单模型查询**（OpenAI 同形）：上面三种写法都认（外加自定义别名），大小写不敏感，路径参数用 `:path` 故带命名空间的原代号（`dots-studio/dots-3-note-preview:free`）也能查。找不到、或该模型**不在当前 Key 的模型白名单内**，一律 404 + OpenAI 错误形状（`code=model_not_found`）——白名单外与不存在同形，客户端无法用单查探测「池子里还有哪些模型」。能力分与列表条目同源（同一份合并结果），刷新时机也一致（列表有、单查不会 404）。

**`n` 参数只接受 1**：本网关的出口结构（`choices[0]` + 单 choice 响应体）只能产生一个候选，`n != 1` 一律 400 `invalid_request` 并说明原因，不做「假装支持、静默只返回一个」。

### 流式 usage 与思考 token

- **流式带 usage 帧**：收尾时在 finish chunk 之后、`data: [DONE]` 之前补一帧 `choices: []` 的 usage（OpenAI 标准形态），只从流里读用量的客户端据此显示 tok/s、上下文占用率与会话 token 统计；上游没上报时不补（不发 0 占位），非流式出口的 `usage` 同理，未上报字段一律为 `null`。
- **思考 token 需客户端带 `reasoning_effort`**：CodeBuddy 渠道在客户端**未给**时补 `medium`（上游缺此字段会把整段思考以「可见推演」写进正文）；显式给的值原样透传、不被覆盖。

### 流式超时（默认关闭）

八条渠道的上游流读取都设了 `read=None`（长回答不能被读超时打断），因此「上游接了连接却一直不吐数据」只能由网关自己兜：

- **首事件超时**（`STREAM_FIRST_EVENT_TIMEOUT_SECONDS`，默认 `0`）：客户端还**没收到任何内容**时超时，安全地换号重试——记 `upstream_first_event_timeout`，按瞬态错误处理（短冷却、不累计）。注释帧 / 心跳**不**重置计时。
- **流内空闲超时**（`STREAM_IDLE_TIMEOUT_SECONDS`，默认 `0`）：**已出帧**后超时只能断流并以错误帧收尾（记 `upstream_idle_timeout`），**不换号**——半截输出无法回滚，换号会把两个账号的内容拼给客户端。
- 实现上**不能**用 `asyncio.wait_for(anext(...))`：超时会 cancel 上游生成器直接断流。这里用可复用的 Task + `asyncio.wait`，超时只放弃等待、任务继续跑，下一次继续等同一个任务。

两项都默认关闭是刻意的：实测慢上下文首字可达 40–71s，给一个「看起来合理」的默认值会静默杀掉正常长请求。要用请在管理台按部署实际打开（推荐 `60` / `300`）。

### Playground 高级参数

Playground 请求表单可选填 `temperature` / `max_tokens` / `reasoning_effort`，**留空即不发送该键**（各渠道自取其默认）。前端校验边界与 `compat/openai/request.py` 一致（`temperature` ≤ 1000、`max_tokens` 为 ≤ 4,000,000 的正整数），不会出现「界面放行、后端 400」。`reasoning_effort` 控件只在模型元数据给出 `supports_reasoning` 或 `default_effort` 时出现，候选为标准三档（`low`/`medium`/`high`，上游声明的默认档若不在其中则追加），默认选中「不发送」——某个档位是否被接受由上游裁决。

## 渠道

> CodeBuddy（含国际版）/ TRAE / Qoder / CodeArts / officeAce 需填凭证；**OpenCode Zen 与 Kilo Gateway 是无凭证的免费层渠道**——池里是一条虚拟占位行（让调度 / 冷却 / 统计照常工作），额度列显示「免费层（无额度接口）」，健康度恒为「无探测」（未知 ≠ 耗尽）。虚拟行**删除后重启会复活**，永久停用请用「暂停」。

### OpenCode Zen 免费层（`zen`）

接 [opencode.ai/zen](https://opencode.ai) 的**免费模型**，无需账号或登录。上游清单混着付费模型且不带免费标记，本服务按 `-free` 后缀收窄候选、再逐个探活，**只把匿名真正可用的免费模型**放进 `GET /v1/models`（判活缓存 30 分钟，不维护静态白名单；免费模型标 **x0**）。上游清单不提供模型名，展示名由模型 id 派生。

**免费层门禁**：上游要求伪装成官方客户端（UA 版本、会话头、`stream:true`、tools 含 `bash`/`read`），本服务自动满足；注入的 `bash`/`read` 是空壳，模型真调用时这类 tool_call 被过滤掉，不泄漏你没声明过的函数。UA 版本用 `ZEN_OPENCODE_VERSION` 调，端点用 `ZEN_API_ENDPOINT`（须在 `ZEN_ALLOWED_ENDPOINTS` 内）；付费模型即使强制 `模型@zen` 也只报「不可用」。

### Kilo Gateway 免费层（`kilo`）

接 [Kilo Gateway](https://kilo.ai)（`api.kilo.ai/api/gateway`）的**免费模型**，无需账号或登录。对外是**标准 OpenAI 兼容协议**，无私有信封、无门禁伪装；上游 `/models` 每条带权威 `isFree` 布尔，本服务**据此直接过滤**免费集、**不做探活**（探活白耗本就极小的免费配额）。免费模型标 **x0**。

**额度与限流**：免费层额度很小（网关级约 200 请求/小时/IP），实为 OpenRouter 免费池的转发；上游 **429 与 502/503/504 都是模型级**——只冷却被点名的那个模型，本服务不自建熔断。端点用 `KILO_API_ENDPOINT`（须在 `KILO_ALLOWED_ENDPOINTS` 内）。

### Qoder（`qoder`，真实账号）

接 [Qoder](https://qoder.com)，**需要真实账号**：在「凭证管理 → 登录渠道账号」点「登录 Qoder」，按提示在浏览器完成设备码授权（登录全程后端轮询完成，Token 不经过浏览器）。上游是私有 COSY 协议（自定义 Base64 编码 + 信封式 SSE），本服务负责签名与解包，对外仍是标准 OpenAI 协议。

**额度与签到**：支持额度探测（`GET /api/v2/quota/usage`）与每日签到（积分可累积）。签到为**活动制**（`/sash/api/v1/me/campaigns`，需带 `Cosy-ClientType`），旧 `daily-check-in` 仅作回退；本服务自动查询并领取可领活动，管理台可手动触发。**签到活动每日 10:00（UTC+8）刷新**（领取后 30 天有效），后台任务按该窗口封账：凌晨看到的「已领」只封到当天 09:59，10:00 新一轮出现后自动补签。新协议不提供连续天数，界面不再显示「连续 N 天」。**国际版没有签到接口**（该端点 404），记为「本区域无此接口」，不是错误。

**端点与节流**：默认国内版（`openapi.qoder.com.cn` + `gateway.qoder.com.cn`），国际版改 `QODER_API_ENDPOINT=https://openapi.qoder.sh` 并确认白名单含国际版域名（签名携带完整 `cosy-*` 头，白名单防误发）。默认 `QODER_CHAT_MIN_INTERVAL=5`（独立节流器），可在「任务与配置」热更。

**上游节点故障**：Qoder 有时把自身推理节点故障包成 400（`[FAIL]node:… msg:Execution failed`）。本服务识别为**模型级瞬时故障**——只冷却该模型，耗尽候选时返回「该模型暂时不可用」的 503（`no_healthy_credential`），而非误报「模型不存在」；稍后重试通常自愈。

### CodeArts（`codearts`，真实账号）

接 [华为云 CodeArts](https://codearts.huaweicloud.com)，**需要真实账号**：点「登录 CodeArts」，浏览器完成 OAuth2 授权后会跳到打不开的本地地址（`http://127.0.0.1:12800/oauth/callback?code=…`）——把**整条链接**粘回页面，服务端换取 STS 凭证（AK/SK + security_token + refresh_token + DPoP 私钥）并加密入库。

**令牌刷新是刚性的**：refresh_token 与 `client_id=codearts-agent`、DPoP 私钥三者绑定且**一次性**，刷新后必须回写新 refresh_token，否则账号失效；由 token 预刷新任务自动完成（DPoP ES256/P-256 签名）。预刷新窗口封顶 45 分钟且必须**宽于**轮询周期（`REFRESH_INTERVAL_MINUTES`，默认 30 分钟）。续期凭据被上游作废时，该凭证标记「需重新登录」并停止自动重试，重新登录后按「恢复」解除。

**每日签到**：上游有「每日签到领 1000 积分」活动（`GET /v1/ops/delivery` → `POST /v1/ops/claim` → `POST /v1/ops/confirm`）。后台任务每日自动领取，管理台可手动触发；`claim` 后**必须** `confirm`（`CLAIMED` 是「已领未确认」而非终态），本服务两步一起做。**这套积分与每日 token 池是两套并行的账**：token 池给福利模型（`deepseek-v4.1-flash` 等）用，积分给系统内置模型（GLM-5.2 / OpenPangu 等）用，互不通气——两本账都在管理台显示（见下）。

**优先消耗**：每日池用完即弃，故当日剩余登记为「次日本地 0 点到期」的到期额度，调度器优先排在其它渠道之前。上游按 token 计量，管理台统一显示为**积分**（1 积分 = 10000 token，每日池满额 = 1000 积分）；升级前落库的历史数据由 `scripts/convert_codearts_credit_unit.py` 一次性折算。**该「积分」是为跨渠道排序合成的折算值，与上游签到/套餐积分同名但不同物**。

**两本账的展示**：CodeArts 在凭证页拆成两组，**每组只讲一本账**——**内置**组显示**积分账**（`GET {snap}/snap-manager/v1/statistics/plugin`，内置模型 GLM-5.2 / OpenPangu 等用），**福利**组显示**每日额度池**（token 折算，福利模型用）。两本账在上游是两个独立接口、互不通气、数值量级差一个数量级，故分到两组、各带悬浮口径说明，不混在一个数字里。积分账的分项包（`基础积分` / `按需积分` / `福利积分`）走「套餐 N 个」悬浮明细，并登记**套餐到期日**（`end_date`，实测每账号不同）的到期阶梯——积分用不完同样作废，故调度「快过期的先用」也会先烧积分。**积分账不参与调度口径的健康度**（`health` 仍只看每日池，选号不变），积分接口失败只记日志、不影响余额探测结果；但**内置组的健康度徽章按积分账算**（后端派生 `quota_credits_health`），与该组额度列同源，不会出现「积分还剩 99%、健康度 0%」。**变动流水按账本分流**（`credit_events.ledger`）：内置组展开的是积分账流水，其余渠道与福利组展开的是每日池流水，两本账各记各的，混读会被当成同一本账的连续读数。

**凭证页拆成两个渠道**：管理台把 CodeArts 拆成 **CodeArts 内置**（可写，承载全部操作与积分账）与 **CodeArts 福利**（只读，只展示每日 token 池）两组，同一批账号各出现一次。**这是纯前端的展示拆分，后端仍是一个 `codearts` 渠道**——`refresh_token` 一次性且与 `client_id` + DPoP 私钥绑定，拆成两条凭证行会让两条各自刷新一次、第二条必报 `the refresh token has been used` 并烧掉票据。两本账的区分轴是**模型**（`is_benefit_model()` 逐请求判断），不是渠道，故这一拆分只影响展示与操作入口。

**签名 / 累计全文 SSE / 节流与并发**：上游要求华为云 `SDK-HMAC-SHA256`（AK/SK + `X-Security-Token`），白名单 `CODEARTS_ALLOWED_ENDPOINTS` 必须含 snap 引擎、STS、福利网关与门户四个主机。流式 `text` 是**累计全文（替换语义）**而非增量，本服务在解析层还原为增量事件，对客户端透明。默认 `CODEARTS_CHAT_MIN_INTERVAL=5`；上游硬限**每账号并发会话数 3**，故配在途上限 `CODEARTS_MAX_CONCURRENCY` 与滑动窗口 `CODEARTS_REQUEST_WINDOW_SECONDS`（每账号每 60s 最多启动 3 次），把突发挡在 `400 TM.00001041` 之前。均可热更。

### officeAce（`officeace`，真实账号）

接 [华为云 officeAce](https://www.huaweicloud.com/product/agentarts.html)（果办 / AgentArts 办公智能体），**需要真实账号**：点「登录 officeAce」，浏览器完成华为云登录与授权即可——**回调由云端承接**（`/v1/claw/auth/callback`），本服务不监听任何端口、用户也不需要粘链接，后端按 2.5s 轮询 `/v1/claw/auth/code` 取回授权码（与官方桌面端同一机制）。

**开通前提**：账号必须先在 OfficeAce 客户端里登录并兑换邀请码完成开通。未开通的账号登录会「成功」但拿不到模型密钥（云端 `subscription: null`），本服务据此报出「这个账号还没开通 officeAce 服务」而不是含糊的登录失败。

**两套凭据，寿命完全不同**（这是本渠道最容易踩错的地方）：

| 凭据 | 寿命 | 用途 |
|---|---|---|
| `Basic model_app_key:model_app_secret` | **不过期** | 聊天转发（`{网关}/v2/chat/completions`） |
| 临时 AK/SK + `security_token` + `project_id` | **约 2 小时** | 积分查询、签到（`/v1/subscription*`） |

混用会得到 `APIG.0303 app not found`。丢了区别的现象也很迷惑：聊天一直正常（Basic 不过期），但积分列空白、签到一直失败（临时凭据过期）。临时凭据由预刷新任务自动续期，`refresh_token` 一次性轮换且与 `client_id=pdp5_for_agentarts` + DPoP 私钥三者绑定——**与 CodeArts 的票互不通用**（混用回 `STS5.1806 invalid client id`）。

**上游就是标准 OpenAI 协议**，故对话层不写私有信封解析；但有三处上游特例必须处理：

- **限流走伪 200**：HTTP 200 + `data:{"error":…}`，且实测出现过「首帧正常、第二帧才是错误」。错误帧照常产出交引擎分类，`81111/81114` 只冷却「该凭据 × 该模型」，其它模型不受影响。
- **模型级拒绝不牵连整条凭据**：`81004`（该号没这个模型）/ `81009`（名字不认）走 403/404，若按状态码判会把整条凭据打死。故业务码判定先于状态码，归类为模型级负缓存（成功即撤销）。
- **`max_tokens` 是「推理 + 正文」总预算**：可用模型里 9/11 是推理模型，给小了正文直接为空。`≥16` 时自动追加 1024 余量再夹到该模型硬上限（`glm-5*` 131072 / `qwen3*` 65536 / `deepseek-v4.1-flash` 393216），`<16` 原样透传（探活/测通道的精确请求），未给则不补。

**积分即主账**：额度单位是 `officeace_points`（新用户一次性 + 每日签到各发一笔），与 CodeArts 的「每日 token 池 + 积分账」双账不同——这里没有并行的 token 池，`remaining`/`total` 直接就是积分。每笔奖励**有独立到期时间、不一并重置**，故额度口径写作「逐笔到期」，调度仍按到期阶梯「快过期的先用」。

**每日签到**：`POST /v1/subscription/bonus/claim`（**写操作，调用即真发积分**；body 必须给 {} + JSON `Content-Type`，空 body 会回 500 `OfficeAce.11020001`）。上游对当日重复领取是幂等的，故不需要本地记「今天领过」。领取收获按**剩余量增量**计算（`points` 是活动累计发放量，由多个渠道共同贡献，拿它当本次收获会虚高）。封账到下个北京时间 0 点：次日新活动一出现就自动补签。

**不做主动模型探活**：目录 34 个里实测只有 11 个真能用，但探活要真发推理、消耗积分，而额度是稀缺资源。模型级拒绝改由真实请求按 `BLOCKED` 负缓存处理，比开机烧 34 次推理省得多。同理**不做 credit 估算**：`cost_coefficient` 与 `tokens_per_point` 的叠乘关系官方未说明，凭空折算会与真实扣点对不上。

**签名与端点**：控制面 `/v1/claw/*` 用华为云 `SDK-HMAC-SHA256`；模型网关 `/v1/subscription*`、`/v1/model-token-rates` 只认 **`V11-HMAC-SHA256`**（多一层 region 作用域 + 签名密钥派生），用错回 `APIG.0624` 或 401。`OFFICEACE_ALLOWED_ENDPOINTS` 只需含控制面主机——模型网关每个账号不同，是上游 `client-permission-validate` 签发并存在凭证里的值，不是本服务能配错的东西。默认 `OFFICEACE_CHAT_MIN_INTERVAL=0`（上游没有 CodeArts 那种硬并发限），可热更。

**单账号单用户**：官方《套餐管理》明确「个人版面向个人用户，限制单账号单用户使用」，故本渠道不做多人共用，每个使用者各自添加自己的凭证。

**换账号**：华为云会话按**浏览器**走——浏览器里还留着上一个账号时，云端会直接放行、把「新增账号」变成「同一个账号再登一遍」。故登录按钮旁额外给一个**「换账号」**（`force_login=true`），它先走 `/authui/logout?service=…` 清掉会话再授权。未开通的账号会明确报出是哪个号没开通，而不是笼统的「登录失败」。

### CodeBuddy 国际版（`codebuddy_intl`，真实账号）

接 CodeBuddy 国际站 [www.codebuddy.ai](https://www.codebuddy.ai)，**与国内站同协议**（同一套 `/v2/chat/completions`、`/v3/config`、`/v2/plugin/auth/*`、`/v2/billing/*` 路径，同一 Keycloak realm），但**是两套独立账号体系**：各自注册、各自计费，故作为**独立渠道**存在。登录链路与国内版完全一致（设备码，Token 不经过浏览器）。

**为什么不是「改一个环境变量切站点」**：`CODEBUDDY_API_ENDPOINT` 是进程级单值，切过去会让所有存量国内站凭证立刻打到国际站（额度错配 / 403）。两个站点可同时持有凭证，各走各的端点、节流器与登录流程。

**白名单刻意互不含**：`CODEBUDDY_INTL_ALLOWED_ENDPOINTS` 默认只有 `https://www.codebuddy.ai`，**不含**国内站主机。两站 Token 由不同网关签发，跨站发送即凭证泄露，因此把国内站地址填进来会在**启动时**直接报错。

**能力差异**：聊天、模型列表、额度探测、token 刷新、**活跃上报**全部启用；**每日签到、成长中心未启用**——签到是**渠道侧就没有这个活动**（`checkin-activity-status` 回 `active: false`），成长中心则**无法验证**（国际站把整个 `/v2/activity/*` 前缀挂在网关认证插件后，连不存在的子路径也返回 401 而非 404），而其含抽奖 / 连登兑换 / 盲盒等**不可逆**动作，不盲跑。管理台的「签到」「成长中心」按钮对该渠道不显示。活跃上报端点 `/v2/report` 与成长中心不是同一路径，实测国际站可用，故照常启用。

**模型列表按套餐探活**：上游 `/v3/config` 返回全量模型，免费号也会看到 `gpt-5.5` / `gpt-5.6-luna` 等调用时才回 `400 / 11158` 的模型。国际站列表会用当前刷新凭证逐个发一次最小请求（拿到状态行即断开），只保留真正可用的模型；`11158`（套餐不允许）与 `11102`（无此模型）剔除，`429`、5xx、超时等不确定结果**保留模型**。结论按账号缓存 6 小时，避免按 5 分钟的列表 TTL 重复探测。

**同名模型不带渠道后缀**：两站共有的模型在 `/v1/models` 里是**一条**、同时挂两个渠道；`-x` / `-sg` 这类**真实存在的不同上游模型**仍各占一条。

**节流**：`CODEBUDDY_INTL_CHAT_MIN_INTERVAL` 默认 5s，**独立节流器**，与国内站互不排队，可热更。

### 按渠道出站代理（可选）

`PROVIDER_PROXIES` 给每个渠道单独指定出站代理：

```
PROVIDER_PROXIES="codebuddy=http://127.0.0.1:7890;qoder=socks5://127.0.0.1:1080"
```

格式 `渠道=代理URL;渠道2=代理URL2`，渠道取八个渠道名，协议支持 `http/https/socks5/socks5h`，留空 = 全部直连。作用于该渠道**全部出站请求**（聊天流、额度/模型拉取、后台任务与 OAuth 登录）。**启动期项**，改后需重启。**严格解析**：未知渠道 / 非法协议 / 缺 `=` 一律启动失败——「以为走了代理其实直连」比启动报错更糟。环境变量 `HTTP_PROXY` 等一律**不采信**（`trust_env=False`，防部署环境全局代理意外劫持带 Token 的上游请求）。

> ⚠️ **容器部署时 `127.0.0.1` 指容器自身，不是宿主**。代理跑在宿主上（Clash / mihomo 等）时必须写成 `host.docker.internal`：
>
> ```
> PROVIDER_PROXIES="codebuddy=http://host.docker.internal:7897"
> ```
>
> compose 已声明 `extra_hosts: host.docker.internal:host-gateway`（Docker Desktop 自带，Linux / Colima 需显式加）。解析只校验格式、**不校验可达**，所以地址写错不会启动失败，只会让该渠道全部请求连不上——务必用 `docker logs` 确认没有 `All connection attempts failed`。

## 出口协议

### Responses API（Codex CLI）

`POST /v1/responses` 提供 Responses 子集，供 [Codex CLI](https://github.com/openai/codex) 这类只走 Responses 的客户端接入。与 `/v1/chat/completions` 共用同一套选号 / 冷却 / 轮换 / 统计与会话粘性，只换入站映射与出口翻译：

```bash
export CODING2API_KEY=sk-你的key
codex -c "model_providers.coding2api={ name='coding2api', base_url='http://127.0.0.1:8000/v1', wire_api='responses', env_key='CODING2API_KEY' }" \
      -c model_provider=coding2api \
      -c model='glm-5.2' \
      '你的任务'
```

支持文本、流式正文、思考摘要（`reasoning` item）、函数工具调用与 `finish_reason=length` → `response.incomplete`。`text.format` 映射到 chat 的 `response_format`（`text` / `json_object` 直通，`json_schema` 展开成 `{type, json_schema:{name, description?, schema, strict?}}`）。内容 part 映射为：`input_text` / `text` / `output_text` → chat text，`input_image` → chat `image_url`（URL 原样透传，含 `detail` 时一并带上）。**不支持** `store=true`、`previous_response_id`（服务端无状态，不假装支持）、`input_file`（需服务端文件存储）、Responses 私有工具——一律显式 400，不静默降级。`include=["reasoning.encrypted_content"]`（Codex 每轮必带）接受但忽略。

### chat `response_format` 收口

`POST /v1/chat/completions` 的 `response_format` 不再整包透传，只放行 OpenAI 定义的三种形状，畸形或未知形状**显式 400**（不静默降级、也不打到上游把凭证打凉）：

- `{"type": "text"}`
- `{"type": "json_object"}`
- `{"type": "json_schema", "json_schema": {"name": <非空字符串>, "schema": <任意 JSON Schema>, "description"?: <字符串>, "strict"?: <布尔>}}` —— `json_schema` 内的未知字段丢弃（既非 chat 规范、上游也不认），`name` 缺失 / `schema` 缺失 / `type` 未知分别回对应的 400 文案。

Responses 出口的 `text.format` → `response_format` 映射与 chat 入站**共用同一段校验**（`src/compat/openai/request.py: normalize_response_format`），两条入站协议不会各写一份而漂移；`prefix` 参数只改报错里的字段路径（Responses 侧是 `text.format`）。

### Anthropic Messages API（Claude Code）

`POST /v1/messages` 提供 Anthropic Messages 子集，供 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 接入。与其它出口共用同一套选号 / 冷却 / 轮换 / 统计与会话粘性：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN=sk-你的key     # 或 ANTHROPIC_API_KEY（走 x-api-key 头）
claude
```

**鉴权**：`x-api-key` 与 `Authorization: Bearer` 都接受。**流式**：`message_start` → `content_block_start/delta/stop` → `message_delta`（含 `stop_reason` 与 usage）→ `message_stop`；无 `[DONE]` 哨兵，thinking 块在 `content_block_stop` 前补 `signature_delta`。**缓存命中**：出口 usage 按 Anthropic 语义拆两笔——`input_tokens` 只计未命中，命中另记 `cache_read_input_tokens`（内部 `input_tokens` 是 OpenAI 口径的 `prompt_tokens`，含命中），Claude Code 据此显示缓存读 token。**`count_tokens`**：`POST /v1/messages/count_tokens` 本地估算输入 token（不转发上游）。**图片**：`image` 块（`source.type=base64` 拼成 `data:` URL、`source.type=url` 直传）归一为 chat 的 `image_url` 内容块，`tool_result.content` 内的图片同样保留——纯文本内容仍折成字符串，故无图请求的映射结果与旧版逐字节一致。**不支持**：`document` 块（PDF，chat 上游无对应表达）、`source.type=file`、Anthropic 服务端工具一律显式 400。

**`cache_control` 透传**（默认开启，无开关）：入站的缓存提示标记原样带给上游，落点与 Anthropic 一致——`system` 内容块、user/assistant 消息内容块、`tool_result`（挂 tool 消息**顶层**，不造空 text 块）、assistant `tool_calls` 元素顶层、`tools` 元素顶层。只做**结构**校验（必须是对象、必须有非空字符串 `type`、`ttl` 若给必须是字符串），取值不解释（未知 `type` 也透传，上游将来新增的缓存类型不该被本网关挡掉）；结构不合法显式 400 而不是静默丢弃。**不带标记时映射结果与旧版逐字节一致**（纯文本仍折成字符串），故会话粘性指纹与上游前缀缓存不受影响。八渠道实测 6 个接受（codebuddy / codebuddy_intl / trae / qoder / codearts / kilo）；**zen 未验证**（上游返 `FreeTierError` 403，`--plain` 对照同样失败，与标记无关）、**officeAce 未验证**（账号未开通模型网关，拿不到可聊凭证）；实测脚本 `scripts/probe_cache_control.py`。

### CC Switch 一键导入

「API Key 管理」页可生成 [CC Switch](https://github.com/farion1231/cc-switch) 的 `ccswitch://` 导入深链（Claude Code / Codex CLI / OpenCode 三个目标各一个按钮），点一下即由桌面端弹确认框写入地址、Key 与模型。两处入口：**创建 Key 成功后的对话框**（直接用刚生成的一次性明文 Key，关闭对话框即不再保留）与 **Key 列表每行的「导入 CC Switch」**（列表只存脱敏 `preview`，需粘贴创建时保存的完整 Key，只留在对话框内存里）。两处都可用「默认模型」下拉选择要写入的模型——与 Playground 同源（`/api/playground/models`），按渠道分组。深链**只能由点击触发**（浏览器拦截自动跳转），且**含 Key 明文**，故仅在用户主动打开、且已给出 Key 时生成。协议与 `app` / `apiFormat` 取值见 CC Switch 用户手册「Deep Link Protocol」，需 CC Switch v4.0.10+。注意 **endpoint 形态随 `apiFormat` 而变**：Anthropic 格式（Claude Code）填到根、**不带 `/v1`**；两个 OpenAI 格式填到 `/v1`。本服务没有 Gemini 出口，故不提供 `app=gemini` 入口。

### 余额查询

`GET /v1/user/balance` 兼容 DeepSeek 余额接口形状，Bearer `sk-...` 鉴权，供 Cherry Studio / cc-switch 等客户端显示余额。余额来自凭证池的额度探测缓存（`QUOTA_PROBE_MINUTES` 周期刷新），按可用凭证汇总，单位为上游 credits：

```json
{
  "is_available": true,
  "balance_infos": [{"currency": "credits", "total_balance": "60.00",
                     "granted_balance": "0.00", "topped_up_balance": "0.00"}],
  "balance_known": true,
  "providers": [{"provider": "codebuddy", "remaining": 50.0, "total": 150.0, "credentials": 2}]
}
```

池内凭证从未探测成功时 `balance_known` 为 `false`（余额未知，不是 0）。

### 池健康检查

`GET /health` 只回 `{"status":"ok"}`（进程存活，适合容器存活探针）；`GET /healthz` 额外给出凭证池计数，**并在 `ready == 0` 时回 HTTP 503**（就绪探针）——那是「进程活着但没有可用凭证」的状态，对话请求必然 503，负载均衡/外部监控应据此摘流量。计数在两种状态下都返回，监控可读到「为什么不可用」。两个端点都不鉴权。**两者同时接受 `HEAD`**（`curl -I` / 多数监控默认发 HEAD）：FastAPI 的 `APIRoute` 不像 Starlette 那样自动给 GET 补 HEAD，只注册 GET 时探针会一直吃 405，表现为「服务正常但监控永远红灯」。

```json
{
  "status": "ok", "service": "coding2api", "version": "0.9.0",
  "credentials": {"total": 5, "ready": 4, "cooling": 1, "paused": 0, "disabled": 0}
}
```

五类计数互斥且合计 = `total`，与调度器同一口径。`ready=0` 时 `/healthz` 返回 503 且 `status` 为 `unavailable`；请把**存活**探针配在 `/health`（恒 200），把 `/healthz` 用作就绪/告警——把容器 healthcheck 指到 `/healthz` 会在池子耗尽时把容器标成 unhealthy，而重启并不能修好一个没有可用凭证的池子。

> 升级注意：`/healthz` 的 HTTP 状态从「恒 200」变为「ready=0 时 503」。只判 HTTP 200 的外部监控会在此后开始报警，这是预期行为；若你只想要计数、不想被状态码影响，改读 `/health` + 自行解析计数即可。

### Prometheus 指标（`GET /metrics`）

`GET /metrics` 输出 Prometheus text 格式（`text/plain; version=0.0.4`），供 Prometheus/Grafana 直接抓取。默认开启，**不鉴权**——与同样裸暴露的 `/healthz` 同口径；`METRICS_ENABLED=false`（可热更）时端点返回 404。

导出五个指标家族：

| 指标 | 类型 | 标签 | 数据源 |
| --- | --- | --- | --- |
| `coding2api_requests_total` | counter | `provider` / `model` / `status`(ok\|error) | `usage_hourly`（永久，跨重启单调） |
| `coding2api_tokens_total` | counter | `provider` / `type`(input\|output\|reasoning\|cached) | `usage_hourly` |
| `coding2api_credentials` | gauge | `state`(ready\|cooling\|paused\|disabled) | 凭证池（与 `/healthz` 同口径） |
| `coding2api_upstream_errors_total` | counter | `provider` / `kind`（受控失败原因） | 进程内计数（重启归零） |
| `coding2api_task_failures_total` | counter | `task` | 进程内任务运行态（重启归零） |

**禁用高基数标签**：不含 `credential_id` / `api_key_id` / `username` —— 一个账号一条时间序列会让 Prometheus 侧内存与查询失控。`requests_total` / `tokens_total` 读**永久保留**的小时汇总（不是 90 天的明细），因此 scrape 时不做明细全表扫描、且计数跨重启单调；进程内计数（失败原因、任务失败）重启归零，由 Prometheus 的 counter reset 语义吸收。

> 已知偏离：扫描报告列了 `upstream_errors_total{provider,kind}` 且数据源写 `usage_hourly`，但小时汇总没有 `error_type` 维度，用 90 天明细当 `_total` 会随裁剪下降——故 `kind` 改由进程内计数器提供（重启归零），其余家族仍取小时汇总。

## API Key 的绑定、白名单、来源 IP 与到期时间

创建 Key 时可限定渠道、模型、来源 IP 与到期时间，适合「按出口分发 Key」。

- **渠道绑定**：选定渠道后该 Key 只在该渠道凭证里选号；模型属另一渠道时直接 400 并指出实际归属（不静默改道）。留空 = 自动（跨渠道选健康凭证）。`模型@渠道` 与绑定冲突时同样 400。
- **来源 IP 白名单**：逗号分隔的 IP 或 CIDR（如 `203.0.113.9,10.0.0.0/8`），留空 = 不限制；写入时校验并规范化（`10.0.0.1` → `10.0.0.1/32`），来源不在白名单返回 403。**默认不采信 `X-Forwarded-For`**（客户端可写）；仅 `TRUST_PROXY=true` 时按 XFF 判定、取**最后一个**条目，故只适用于「本服务前恰好一层受信反代」。
- **模型白名单**：逗号分隔的模型名或 fnmatch glob（如 `glm-*,kimi-k3`），留空 = 不限制；匹配不区分大小写，`模型@渠道` 后缀不参与匹配，白名单外返回 400，`/v1/models` 也只列白名单内模型。
- **到期时间**：epoch 秒，留空 = 永不过期，到期后立即 401（与「Key 不存在」统一文案，不泄露可枚举信息）。
- **配额**：一个 JSON 字段，六项各自独立、`0` 或留空 = 不限制：`daily_requests`（当日请求数）、`daily_tokens`（当日 token）、`daily_cost_usd`（当日成本）、`rpm`（每分钟请求）、`tpm`（每分钟 token，用**预估**值判定——真实用量要等响应结束才知道）、`max_concurrency`（在途并发）。当日类按**北京时间自然日**统计（与凭证护栏同一口径），速率与并发是**进程内**滑动窗口（重启清零，不落库）。超限返回 429 + `{"error":{"type":"quota_exceeded",...}}`，`Retry-After` 指向可重试时刻（当日类 = 距次日 0 点；RPM/TPM = 最早一条滑出窗口；并发 = 1 秒）。**判定本身出异常时一律放行并记 warning**——配额是拒绝语义，但计量故障不能把正常请求打死。持 Key 的调用方可用 `GET /v1/usage?days=1|7|30` 自助查询自己的用量、剩余额度与当前速率占用（只读本地明细，不触发上游，不含凭证昵称或池级信息）。

**导入 CC Switch**：Key 列表每行的「导入 CC Switch」按钮打开对话框，粘贴创建时保存的完整 Key（列表只存脱敏 `preview`，服务端不再保留明文，故必须由用户提供），再选一个默认模型，即可生成 Claude Code / Codex CLI / OpenCode 三个 `ccswitch://` 导入深链；默认模型下拉与 Playground 同源（`/api/playground/models`），按渠道分组。粘贴的 Key 只留在对话框内存中，关闭即清除。创建 Key 成功后的对话框内也提供同样的导入按钮（那里直接用刚生成的明文 Key）。

## 凭证暂停

凭证列表行内菜单的「暂停」把该凭证摘出**对话流量**，并让**每日签到 / token 预刷新**跳过它（这两类任务对 `disabled` 与 `enabled=0` 一视同仁）。其余后台任务（额度探测、成长中心、活跃上报）仍照常运行（只认系统硬禁用 `disabled`），适合「先不接聊天、但积分继续领」；「取消暂停」立即放回池子。与状态列的「已禁用」不同——那是渠道判定会话失效后的系统禁用，需重新登录后用「恢复」解除。同一开关也在 `POST /api/credentials/{id}/toggle`。

> OpenCode Zen / Kilo Gateway 是无凭证渠道，池里是一条虚拟占位行。**暂停它是永久停用的唯一方式**——删除后下次重启会被种子重新补上。暂停也会把该渠道从模型列表摘掉。

## 凭证批量运维

池子一大，「逐个点签到 / 探测」就不现实。凭证页提供多选 + 批量操作条：每行一个 checkbox、组头一个「全选本渠道」，勾选后顶部出现操作条（探测额度 / 每日签到 / 暂停 / 取消暂停 / 恢复 / 取消选择）。

- **后端批量端点**：`POST /api/credentials/batch`，body `{action: "probe"|"toggle"|"checkin"|"revive", ids: [...], enabled?}`（`enabled` 仅 `toggle` 用）。一次请求最多 200 条，重复 id 自动去重。
- **逐条隔离**：单条失败不影响其余条——凭证不存在、渠道不支持该能力、上游报错都只体现在该条结果里，其余条照常执行。响应为 `{action, total, succeeded, failed, results: [{id, ok, error?, detail?}]}`：`ok` 表示该条动作是否成功；`error` 表示这条根本没执行到上游（`credential not found` / `credential does not support ...` / `action_failed`），`detail` 是动作自身的返回体（probe 的剩余额度、checkin 的积分与状态）。
- **暂停是显式状态而非取反**：`toggle` 的 `enabled` 由请求体显式给出，不会让「已启用 + 已暂停」的混选走向相反方向。因此操作条把 `toggle` 拆成**「暂停」（`enabled=false`）与「取消暂停」（`enabled=true`）两个按钮**——只给一个按钮的话，批量暂停后再无入口放回池子（`revive` 清的是硬禁用 `disabled`，不碰 `enabled`）。
- **「恢复」与「取消暂停」是两件事**：前者是 `revive`（解除渠道判定会话失效后的系统硬禁用，需先重新登录），后者是 `toggle enabled=true`（把管理员手动暂停的号放回调度）。
- **只收已有单条端点的动作**：`refresh`（token 预刷新）没有单条入口且由后台按到期窗口驱动，批量强刷会白耗一次性票据；`delete` 破坏性太强，需要单独的二次确认流，都不走这个通道。
- **审计只记一条汇总**（`toggle` / `revive`）：批量暂停 50 个号是一次操作、一个意图，逐条落库会把审计流水刷成噪声。`probe` / `checkin` 与单条端点同为只读语义，不写审计。`toggle` 的 detail 带方向（`批量 toggle（暂停）：N/M 成功`），否则暂停与取消暂停在流水里无法区分。
- **审计写失败不影响操作结果**：审计落库失败只记 warning，端点仍返回逐条结果。逐条变更此时已提交生效，因留痕失败返回 500 会让操作员误以为整批没跑。

## 凭证限额护栏

凭证级**可选**护栏，给「这个号别用超」一个显式开关：行内菜单「设置限额」配置，四个护栏各自独立、全部留空 = 不受限（老库升级后行为不变）。判定在**选号前**完成，触顶的凭证不再参与对话流量；后台任务（签到 / 探测 / 成长中心）照常运行——限额只管「对话用量」，与「暂停」（连签到/预刷新一并停掉）不是一回事。

- **保留积分**：账户剩余积分 **≤** 该值时整号停用——给号留底，避免把额度跑空。与当日用量无关；余额未知（渠道不上报）时不猜、不停用。
- **每日 token 上限**：当日累计 `输入 + 输出` token **达到**该值时整号停用。
- **每日积分上限**：当日累计 credit 达到该值时整号停用。
- **按模型 token 上限**：`{模型: 上限}`，某模型当日累计达到上限时**只停该模型**，其余模型照常可用。
- **当日 = 北京时间自然日**（与签到、活跃上报同一时区），0 点刷新计数，避免与上游「每日重置」出现半天错位。
- **无可用凭证 503**：当所有候选都因限额被跳过时，复用 `no_healthy_credential`（账号级原因，换个模型也未必能用）；与「模型级冷却」的 `model_unavailable` 区分开。
- 写入 `PUT /api/credentials/{id}/limits`（整体替换：只传要开的护栏，缺省字段 = 关闭该护栏；四个全关时删除配置行）。改动即时生效（写入后主动失效选号侧快照，不等 TTL）。审计动作 `credential.limits`。

> 实现注：判定需要「当日聚合用量」，那是每请求都变的动态数据，因此不进 `Candidate.is_selectable`（纯函数，只吃凭证自身状态），而在 executor 选号前用 `LimitGuard` 过滤；`LimitGuard` 对「限额表 + 当日用量」做进程内快照缓存（默认 15s），避免每次选号都查一次聚合。

## 凭证在途上限

`MAX_INFLIGHT_PER_CREDENTIAL`（默认 `0` = 不限）：同一凭证的**并发请求**达到该值后不再参与选号，避免把单账号打进上游限流 / 封号——多配几个号本就是为了分摊，把一个号打满不如把请求分出去。跨渠道全局生效（与 CodeArts 的渠道内 `CODEARTS_MAX_CONCURRENCY` 正交）。

- 计数是**进程内**的：单实例语义，多实例部署各自计数（与 `/api/tasks` 运行态同口径）。
- 租约在请求结束时释放（含成功、轮换、异常、客户端断开）；另有 **600s TTL 兜底**——漏释放的租约过期后被回收，宁可偶尔多放一个请求，也不能让号被静默占死。
- 全部候选都占满时与限额护栏同口径报「无可用凭证」503。
- 可在管理台「任务与配置」页热更，下一请求即生效（无快照缓存）。

**在途计数兼作负载键**（不限并发时也生效）：在途数是调度器的「最少连接」排序键，插在「免费优先」之后、到期额度之前——并发请求自动摊到空闲号，避免全部压到排序确定的同一个号上（`Scheduler.select`）。上限为 `0`（不限）时 `blocked()` 恒 False，但租约**照常登记**，负载键照常可用；只有顺序请求（在途恒 0）才退回纯到期/健康度排序。

## 凭证池的筛选与分组

凭证多了以后，列表页把「找到某个号」这件事拆成三层，默认首屏只铺渠道级总览：

- **筛选工具条**：搜索（昵称 / 渠道 / id，忽略大小写）× 渠道 × 状态 × 标签四件套。四者是 **AND** 关系，纯前端收敛，不改后端排序（后端仍按 `sort` / `order` 给全量）。命中时显示「筛选后 N / 总数」，并提供「清空」；无匹配给空态而非空白表格。渠道 / 状态 / 标签下拉里的计数来自当前全量，切换筛选不会自我缩窄。**标签下拉只在池里真的出现过标签时渲染**（没有标签时不留一个恒为「全部标签」的空控件）；标签是结构化维度，**不参与搜索词匹配**——否则「查一个恰好叫『生产』的昵称」会变得不可能。
- **标签**：每条凭证可贴任意个运维标签（逗号分隔，最多 16 个、单个 ≤ 24 字符；半角/全角逗号与分号都当分隔符），入口在行内「更多操作 → 设置标签」。标签**只用于列表筛选与展示**，不参与选号、不改变任何请求行为——把标签当策略用会让改名静默改变线上流量。行内最多列两个标签，多余收成 `+N` 并在悬浮里给全量。
- **组排序**：单独一个控件，只重排「渠道组」，不动组内行序（行序仍由列头 / 后端 `sort` 决定）。默认**额度从高到低**，按组内额度剩余合计排名，让还能打最多的渠道排前面；可切回**渠道顺序**（固定，30s 轮询不会让组头乱跳）。无额度接口的免费层垫底（不当 `0` 参与比较），额度相同（含都为 null）时回落到渠道顺序。
- **按渠道分组**：列表按渠道聚合，组头做成一张紧凑的「渠道仪表盘」——品牌图标 + 渠道名 + 账号数，加上**状态分布**（图标 + 计数，只列出现过的态，口径与行内状态徽章同一套 `credentialState`）、**额度合计**（占比环 + 数字；环是纯图形仪表，精确百分比写在「额度合计 / 积分合计」标签行，对组内非 null 值求和，免费层 zen / kilo 无额度接口时显示 `—` 而不是 `0`，也不出环），以及**到期积分**（主窗口 1.5 天一块，次窗口 7 天仅在**比主窗口多**时再出一块——次窗口是主窗口的超集，数字相同说明没有超出 1.5 天的部分）。点组头折叠 / 展开该渠道，**默认全折叠**（首屏先看结构，要看具体账号再展开）；筛选生效时自动展开命中渠道，清空筛选回到折叠总览。组顺序默认「额度从高到低」，也可用工具条切到固定的全站渠道顺序（CodeBuddy、TRAE 在前），不随行排序键变化。渠道名只出现在组头，行内不重复。**额度合计按本组那本账取**：CodeArts 内置组是积分账合计（标签写「积分合计」），其余组是每日池合计。
- **列头跟着展开区走**：没有全局表头，列头（含排序按钮与列说明）渲染在每个展开的渠道组内部，并在滚动容器里吸顶——折叠态下组头本身就是总览，列名没有对应内容，只有展开某组时才需要「这一列是什么」的锚点。同一时间可能有多个组展开，各组列头共用同一套排序状态（点任一处都写回同一个后端 `sort` 参数）。**「成长中心」列只在支持的渠道（CodeBuddy）出现**，其余渠道留一个空占位格——列数保持恒定，否则 auto 布局会重算列宽、下面展开的组整列错位。
- **CodeArts 拆成两个渠道组**：**CodeArts 内置**（可写，承载全部操作、积分入口、积分账与套餐明细）与 **CodeArts 福利**（只读，只展示每日 token 池），同一批账号各出现一次，组头 label 带「内置 / 福利」后缀，内置在前。**两组各讲一本账**：内置组额度列是积分账（`积分 x / y`，排序键 `quota_credits_remaining`），福利组是每日 token 池；变动流水入口也按账本读（内置组读 `ledger=credits`，福利组不渲染入口）。**到期积分也按账本拆**：后端把两段合并阶梯分开存（积分那一段另存 `quota_credits_expiry_ladder`），福利组读每日池那一段、内置组读积分账那一段，两组各显各的、互不串账。福利组收掉探测 / 签到 / 更多操作、积分记录入口与套餐明细，但**列数不变**：组头与行内的选择框仍渲染，只是置为**禁用**——空占位格的内容宽度与内置组不同，表格 auto 布局下本行各列起点会与内置组错开，同尺寸的禁用框才能真正对齐。这是**纯前端展示拆分，后端仍是一个 `codearts` 渠道**（`refresh_token` 一次性，拆两条会互相烧票），两本账的区分轴是模型而非渠道。
- **长列表可滚**：表格限高（70vh），滚到第 20 行时该组的吸顶列头与「操作」按钮仍在。

额度列的数字带虚线下划线：悬浮显示**本行的周期口径**（本周期剩余 / 账户剩余 / 每日额度池 / 免费层无额度接口），逐行的同款文案收进悬浮说明。

## 模型列表加载

`GET /v1/models` 与 Playground 的模型下拉只合并**当前有可用凭证**的渠道：该渠道至少有一条**未暂停、未会话失效**的记录。判定在缓存之前——没凭证的渠道不读缓存、不拉上游、不展示，故**从未接入**的渠道不出现「幽灵模型」；**全部暂停 / 凭证失效**时其模型暂时消失，恢复或补一条凭证后立刻回来；冷启动默认只有 `zen` / `kilo`（自带虚拟凭证）。冷却中的凭证仍算「有凭证」——只是暂时限流，列表不闪没。

**展示顺序**：CodeBuddy、TRAE、Qoder、CodeArts、officeAce 的模型依次排在前面，组内按模型名字典序；多渠道模型按最高优先级渠道归位。纯展示排序，不改变调度选号。

**请求路由只认这份模型缓存列表**：模型目录能证明某模型归属哪些渠道时，候选渠道就只有它们——登记了该模型的渠道全部不可用（都在冷却 / 都被暂停）时直接返回 503，不再把请求逐个渠道试过去（各上游只会回 11102 / 4001 / 401，还会给渠道写下数小时的「该后端无此模型」负缓存）。模型完全不在目录里时仍按全部候选渠道路由；直连指定（`模型@渠道`）与 Key 渠道绑定不受影响。

### 模型目录落盘与启动即用

模型表除进程内 TTL 缓存（300s）外，每次拉取成功还写快照到 `DATA_DIR/model_catalog.json` 并在**启动时同步读回**：重启那一刻「模型 → 渠道」归属表就已就绪，扁平名请求不会在预热（zen 免费模型探活要十几秒）跑完前扇出打错渠道。快照存未过滤原始表，损坏 / 超 7 天只丢缓存、不影响启动。另有 `MODEL_CATALOG_MINUTES` 后台任务兜底刷新。

Playground 与 `GET /v1/models` 走 **stale-while-revalidate**：有缓存立刻回旧列表，TTL 到期的渠道丢后台异步刷新；只有某渠道**一条缓存都没有**时才同步等一次。代价是列表最多滞后一个 TTL。拉取**连续失败**时重试间隔按 2^(n-1)×TTL 退避（封顶 2 小时，成功一次即恢复），日志只在首次失败打 WARNING、其后降 INFO——上游长期不可用（如 zen 免费层按出口 IP 拦截）时不会每 5 分钟重探十几秒再刷一条同样的告警。

### 管理台「模型列表」页

管理台「模型列表」页（`GET /api/model-catalog`，管理员 / 操作员可见）只读展示 OpenRouter 公开目录：刊例价（输入 / 输出 / 缓存读 / 缓存写，USD / 百万 token，可切人民币）、上下文·输出上限、输入→输出模态、能力（推理 / 工具调用 / 附件 / 结构化输出）、知识截止，以及 **Artificial Analysis 三项指数**（综合智能 / 编程 / 智能体，经 OpenRouter 公开接口，**非本服务实测**，不做排序）。目录缺失时页面显示空态，不影响聊天与成本。

OpenRouter 收录数百条，其中多数本服务没接。每行带 `available` 布尔（该模型是否**当前渠道真能提供**，口径与 `/v1/models` 一致），页面提供**默认关闭**的「只看当前渠道」开关，勾选后本地收窄到当前渠道获取到的模型（不重新请求）。

**疑似下线视图**：同页可切到「疑似下线」筛选——`GET /api/models/availability`（operator）把最近 N 天（默认 7、可调 1–30）`usage_events` 里 `error_type` 为 `upstream_blocked` / `invalid_request` 的失败按 `(渠道, 模型)` 聚合，给出失败次数、最近失败时刻与「是否已在黑名单」。行内「加入黑名单」把该模型**追加**到现行 `model_blocklist`（读现值再写，不覆盖既有项；已命中的行按钮置灰），写入走既有设置接口并落审计 `settings.update`，下一次 `/v1/models` 即生效。这个视图本身**只读聚合**（真发请求的那部分见下节「主动探活与人工接管」）。

「能力分」列头可点排序，按**综合智能指数**（智）排；无分数的行排末尾（不冒充 0 分）。只给这一个分项排序入口——表头一列对应一个入口，三项各排会把表头挤成一排按钮。

**浮动别名取同族最新快照的分**：OpenRouter 对同一个模型会同时挂**浮动别名**（`deepseek/deepseek-v4.1-flash`）与**带日期快照**（`deepseek/deepseek-v4.1-flash-1017`），并用 `canonical_slug` 声明同属一次发布。上游会把浮动别名钉在旧快照上、并对同一快照只发布一个评测变体（2026-10-19 实测：浮动别名标 `Non-reasoning` 24.7，`-1017` 标 `Max` 39.5）——分数看着「掉」了 15 分，其实只是换了变体。本项目转发时推理默认开着（渠道 `default_effort` 多为 `high`），与 Max 变体同口径，故按 `canonical_slug` 的日期取同族最新快照的分；显式带日期戳的 id（`deepseek-v4-pro-0813`）仍用自己的分。`source_model` 一律指向真正提供分数的上游 id。家族只从**带 `-YYYYMMDD` 的 canonical** 派生：`mistralai/mistral-large`（Feb '24）与 `mistralai/mistral-large-2512`（Mistral Large 3）削完日期同基却是两个模型，不互相借分。

### 主动探活与人工接管

真实请求失败只能证明「这次没打通」，分不清「上游真没这个模型」与「额度耗尽 / 限流 / 网络抖动」。同页的**主动探活**面板（`GET /api/models/probes`，operator）只探**仍被 `blocked` 负缓存锁住**的 (渠道, 模型)——那是唯一会静默避让一个模型 6–24 小时的状态，也是唯一值得花额度去确认的目标；不遍历全量模型表。

三态判定：**成功**（拿到内容或正常收尾）→ 立即清该渠道的 `blocked` 负缓存，模型马上回到池子，这是 TTL 走完之外的**唯一提前恢复入口**；**`failed`**（上游明确 `BLOCKED` / `INVALID`）→ 保留负缓存并累加连续失败；**`inconclusive`**（额度、限流、并发打满、网络、超时、空流、任何异常）→ **绝不禁用**，并把连续失败归零——一串无关故障不该凑成「连续失败」把好模型判死。

探活与生产完全隔离：直连 `provider.stream_chat`，不经 executor，不写 `usage_events`、不动冷却表（除成功时清 `blocked`）、不改健康度、不参与任何出口判断；结论落独立的 `model_probes` 表，只供页面展示（探测失败 ≠ 上游拒绝）。`reason='model'`（6004 限流）不探——它有自己的重置墙钟。

**人工接管**（`POST /api/models/probes/manual`，`manual=on|off`）：接管后自动探活整行跳过，避免后台与人工结论互相打架；接管**不**改负缓存——要立刻恢复请点「立即探活」（`POST /api/models/probes/probe`，不受接管标记限制）。后台周期由 `MODEL_PROBE_MINUTES` 控制（默认 `0` = 关闭，可热更）。

### 管理台「上游状态」页

管理台「上游状态」页（`GET /api/upstream-status`，管理员 / 操作员可见）聚合各模型 / 网关提供方的**官方状态页**，回答「这个报错是上游整体挂了，还是我这边凭证 / 额度的问题」——本服务自己的 429/5xx 冷却只能回答「这个号现在不行」，官方状态页补上另一半。

- **来源**：OpenCode Zen（自定义 JSON，**逐模型 TTFT / tps**）、Kilo、Moonshot / Kimi、MiniMax、OpenAI、Anthropic（Statuspage v2）、Together AI（Better Stack JSON:API）、DeepSeek（Atom 事故史）。按 `scope` 分两组展示：`channel`（与本站渠道一一对应，如 zen / kilo）排在 `model`（经渠道间接使用）之前。
- **归一化六档**：`operational` / `degraded`（性能下降）/ `partial`（部分中断）/ `major`（重大故障）/ `maintenance`（维护中）/ `unknown`（抓取失败）。Statuspage 的 `critical` 并入 `major`。整体档位取**最差**的源，但 `unknown` 排最后——一个源连不上不该压过其余源报出的真实故障。
- **只做解释、不做决策**：官方状态页滞后于本服务的实时冷却（模型级冷却见 `src/provider/kilo/client.py`），把它并入选号 / 熔断只会让调度更慢更糊涂。
- **只存内存、不落盘**：状态数据分钟级易变，重启后拿一份几小时前的「一切正常」比显示「尚未抓取」危险得多。抓取由后台任务（`UPSTREAM_STATUS_MINUTES`，默认 5 分钟）负责，单源失败只在该源那一行标错误原因，绝不让整轮抛异常。
- **版式按「先结论、后明细」收敛**：顶部概况条给整体档位 + 档位分布 + 抓取运行态；下面按 `scope` 分两组、一行一个源（**异常源默认展开**，正常源折成一行摘要），另有「需要关注」条把非正常源提到最前面并锚点跳转。OpenCode Zen 逐模型展开 TTFT / tps，其余源给逐组件档位；单源组件明细超过 6 项时截断（**异常项排前面**，不会被折在「显示全部」后面），事故列表里进行中默认铺开、已恢复折进计数按钮。
- **本页不覆盖**仅提供 HTML、无稳定机器可读端点的源（Groq、腾讯云、阿里云、华为云、SiliconFlow、Qoder、智谱、TRAE）——加源前先确认端点能稳定返回 JSON / XML，否则等于每天修一次 HTML 解析。

### token 到期展示

凭证列表的 **token 剩余**列显示 access token 距到期还有多久，低于 `TOKEN_EXPIRY_WARNING_SECONDS`（默认 1 小时）标红。到期时间优先取上游 `expires_at`，缺失时回落 JWT `exp`；都取不到显示 `—`，**不猜本地 TTL**（否则是凭空捏造的预警）。

## 侧栏数据提醒（导航角标）

管理台侧栏（`web/src/components/Sidebar.tsx`）在每个页面都在。多个**不点进去就发现不了**的待办态（告警未恢复 / 后台任务失败 / 凭证需重登）在这里显示成计数角标，由单个聚合端点 `GET /api/nav-badges` 一次返回，侧栏 60s 轮询（不挂 7 个页面级 hook，避免反复拉全量列表）。

| 导航项 | 门槛 | 角标口径 |
|---|---|---|
| 凭证管理 | operator+ | `disabled`（需重新登录）**或** `health < 0`（额度耗尽）的凭证数 |
| 运维告警 | admin | `resolved_at IS NULL`（**仍在告警中**）的条数 |
| 任务与配置 | admin | `enabled` 且**上次执行失败**（`last_ok === false`）的任务数 |
| 上游状态 | operator+ | `summary.abnormal`（含维护中）；色调随整体档位（维护=黄，部分/整体故障=红） |
| 用户管理 | admin | 有激活令牌（`activation_digest IS NOT NULL`）的账号数 |
| API Key | 全员（仅本人） | 已过期或 **7 天内**到期的 Key 数 |
| 模型列表 | operator+ | 快照陈旧标记（布尔文字「陈旧」，非计数） |

- **按角色裁剪**：后端只下发当前角色有权看的键，**缺失 = 无权看**（不是 0），前端据此不渲染。凭证角标刻意不下发给 viewer——能看列表但无写权限，露出角标只会误导。
- **与各页面数据不是同一口径**：角标只数「待办」。例如告警页 `total` 含已恢复历史，角标只数未恢复的；任务用「上次失败」（首次失败即提示）而非告警规则的「连续失败 ≥ 阈值」；`last_ok === null`（从未运行）与已关闭的任务都不计。
- **凭证角标 ≠ `/healthz` 的可用数**：后者复用调度器 `is_selectable`，只看 disabled / enabled / cooling、**不看 health**，所以额度耗尽的号在 `/healthz` 里仍算 `ready`。两者不一致是刻意的。
- **告警角标会自愈**：`AlertTask` 启动时从库里未恢复的行重建「告警中」集合，所以重启前落库、条件已消失的告警仍会被正常标记恢复；同一次故障期间的「提醒」行在恢复时一并收尾。详见 TECHNICAL.md 的「迟滞与恢复」。
- **模型目录陈旧**：从未拉取（`price_saved_at` 为空）不算陈旧（新装部署不该一上来就报警）；已拉过但超过 **2 倍** `OPENROUTER_CATALOG_MINUTES` 未刷新才算。
- 角标窗口（API Key 7 天）是后端模块常量，不做热更；不点进去也能看到待办，点进去即是可行动页面。

## 统计

### 口径

时间范围默认**最近 24 小时**（另有 7 天 / 30 天 / 全部 / **自定义**）：打开统计页的第一问题是「这个池子现在怎么样」，7 天会把当天的波动摊平。**自定义**给两个 `datetime-local` 输入（精确到分钟、按**浏览器本地时区**解析），两端可只填一边（另一边不限）；起点不早于终点时整窗作废并提示，不静默退回全量。所有统计端点因此多接受一个 `until`（epoch 秒，半开上界）：`[since, until)`，`until` 本身不计入——用户填的「结束时间」是瞬时右端点，两次相邻查询不重叠。

**明细失败类型**是受控枚举（后端 `CONTROLLED_ERROR_TYPES`，界面只展示中文翻译）：模型级冷却耗尽记「该模型暂时不可用」（`model_unavailable`，换个模型即可用），凭证池真耗尽才记「无可用凭证」（`no_healthy_credential`）——两者对外错误码相同（`no_healthy_credential` 503），仅统计口径区分。

**请求 ID（`X-Request-ID`）**：每个请求都在响应头带一个 `X-Request-ID`（任何响应都带，含被拒的 413 / 400）。入站 `X-Request-ID` 合法则沿用（便于与客户端日志对齐），否则自生成；合法字符集 `[A-Za-z0-9._:-]`、长度 1–128，越界即丢弃改用自生成（客户端可写，不校验会被塞入换行/超长串）。该 ID 随请求落进 `usage_events.request_id`，统计页「请求明细」的**时间列悬停**可见——用于把用户报的一个请求与这行明细对齐。老库历史行为 `NULL`（不回填）。**不透传到上游**：各渠道出站头语义不同（qoder 的 `X-Request-ID` 是 uid 派生的防重放值，trae/codearts 有指纹敏感头），统一改写会破坏既有契约。

统计页 **Credit 消耗**列：CodeBuddy 是上游**真值**；TRAE 的 `token_usage` 帧只有 token 数，credit 按官方单价**推算**并标 `≈`；CodeArts 福利模型按每日池 1:1 扣减推算，同样标 `≈`。单价表集中在 `src/provider/trae/pricing.py` 与 `src/provider/codearts/units.py`，未收录的模型不推算（显示 `—`）。历史明细可分别用 `scripts/backfill_trae_credit.py` / `scripts/convert_codearts_credit_unit.py` 补齐。

**成本（估算，≈）**：按 token × 公开刊例价估算，不是上游真实扣费。单价取自 **OpenRouter 公开模型目录**（`pricing.prompt` / `completion` / `input_cache_read`，USD / 百万 token），按 `(输入−命中)×输入价 + 命中×缓存价 + 输出×输出价` 折算美元，再按**写入时生效的汇率**（`USD_CNY_RATE`）折人民币。

- **写入时定值**：成本在写明细时算好落库（`usage_events.cost_usd` / `cost_cny`），历史行**不随价表或汇率重算**；上线前历史两列为 `NULL`，可用 `scripts/backfill_cost.py --apply` 补齐（默认预览、写库前自动备份、幂等）。
- **匹配不到就不计**：本地模型名按 OpenRouter 的 `id` / 展示名等值匹配（`src/model_match.py`，**命中不唯一就不配**），未匹配或未上报输入 token 的不计入，聚合值 `null` 显示 `—`（**不拿 0 冒充免费**），故成本是**下限**。

**缓存命中率**：**Token 消耗**卡片在 token 分项后追加缓存命中率 = 命中 token ÷ 输入 token（保留 1 位小数）；**未上报或输入为 0 时显示 `—`**。**分组统计**表的 Token 合并列与**请求明细**表的 Token 合并列都把命中率与 token 放同一格（「输入 / 输出 · 命中率」），同公式、同占位规则（派生列不参与排序）。

### 积分记录

凭证**额度**列数字后的下箭头展开「变动流水」，记录**两次额度探测之间的净变化**，不是动作归因：签到、成长、对话都会改余额而上游不打日志，探测只能看到区间净变化，界面一律写「净变化」而非「签到 +5」。首次探测只建基线；余额没变不记；余额变「未知」仍记但不填变化量；保留 90 天。

**按账本分流**：CodeArts 有两本账（每日 token 池 / 积分账），流水表因此带 `ledger` 列（`pool` / `credits`），同一轮探测为每本账各记一条。内置组展开的是积分账流水（`ledger=credits`），其余渠道与福利组展开的是每日池流水（`ledger=pool`）；接口 `GET /api/credentials/{id}/credit-events` 用 `ledger` 查询参数选账本（缺省 `pool`，未知取值回落 `pool`）。**必须分开读**：两本账数值量级差一个数量级，混在一个列表里会被当成同一本账的连续读数。老库升级时历史流水一律归 `pool`——它们本就由每日池的探测 diff 产生。

### 分组维度与重新汇总

**分组统计**面板用 tabs 切四个维度（按渠道 / 按模型 / 按用户 / 按凭证），共用同一张表（渠道 / 请求（请求数 · 成功率，悬停看成功数）/ Credit / 成本 / Token（输入 ÷ 输出 · 命中率，悬停看命中 token）/ 平均延迟（耗时 ÷ 首字）/ P95 延迟（耗时 ÷ 首字）），共 7 列。对应端点 `GET /api/stats/by-provider|by-model|by-user|by-credential`。默认按请求数降序，点列头可切换排序；请求 / Token / 平均延迟 / P95 延迟四个合并列点列头弹菜单选子列再排序（`sort=requests|ok_count|input_tokens|output_tokens|cached_tokens|avg_latency_ms|avg_ttfb_ms|p50_latency_ms|p90_latency_ms|p95_latency_ms|p99_latency_ms|p50_ttfb_ms|p90_ttfb_ms|p95_ttfb_ms|p99_ttfb_ms`），成功率与命中率是派生只读值不参与排序。**延迟分位可切换**：P95 列的表头有 P50 / P90 / P95 / P99 四档切换（默认 P95，不铺开四列以免表头拥挤）；四档由同一次窗口函数子查询算出，取整口径一致（nearest-rank，n 为**有值**样本数）。**按用户 / 按凭证**只对 admin 与 operator 展示。**口径差异**：渠道 / 模型 / 用户三维读**小时汇总**（永久保留，最近 5 分钟内的请求尚未计入）；**凭证维度读明细表**（小时汇总主键里没有 `credential_id`），故只覆盖**最近 90 天**。

**延迟两类口径**：**只统计成功请求**（失败请求的耗时会拉偏「典型耗时」）。**平均延迟列** = 平均耗时 / 平均首字 = `latency_sum ÷ 成功数`（读小时汇总，与总览卡片、图表同口径，永久保留）；**P95 延迟列** = P95 耗时 / P95 首字 = 第 95 百分位，按 **nearest-rank**（第 `⌈0.95·n⌉` 小的值，n = 该组有值样本数）从**逐请求明细**算出——小时汇总只有和与计数，数学上还原不出分布，故分位列只覆盖**最近 90 天**。P50 / P90 / P99 与 P95 同一套 SQL（一次子查询算完四档），口径完全一致，只是百分位参数不同。`ttfb_ms` 对非流式 / 未产出首字的成功请求为 `NULL`，两列各自独立排名（只数有值样本）。无成功样本 / 未上报延迟时显示 `—`（不拿 0 冒充）。

**失败归因**：分组表下方另有「失败归因」面板，`GET /api/stats/errors`（operator+）按 `(渠道, 模型, 错误类型)` 聚合窗口内 `ok=0` 的明细，给出失败次数与最近失败时刻（按失败数降序，`limit` ≤ 5000）。点任意一行即**钻取**到下方明细并自动加上「只看失败」（`GET /api/stats/events?ok=0`，与 `provider`/`model`/`credential_id` 叠加）；键盘 Enter / Space 同样触发。该面板只在 admin/operator 下渲染与请求。`/metrics` 的 `upstream_errors_total` 仍取进程内计数（重启归零），与这里的 90 天明细口径不同，两者不合并。

**点行钻取**：点分组表任意一行，下方「请求明细」即按该行钻取（渠道 / 模型 / 凭证按同名列过滤，用户维度走作用域参数），条件叠加、可多个维度层层收窄，面板上方显示当前过滤标签并可一键清除。`GET /api/stats/events` 因此接受 `provider` / `model` / `credential_id` 三个过滤参数（`AND` 语义），另有 `ok=0` 只看失败。

**明细表列合并**：请求明细把同类列并成一列，默认值一眼可读、悬停展开全部子项：**Token**（默认「输入 / 输出 · 命中率」，悬停含命中 token）、**延迟**（默认「耗时 / 首字」）。列数从 14 压到约 10，窄屏卡片也少一半字段。排序能力不丢：点合并列头弹出下拉选子列（Token → 输入 / 输出 / 命中 / 命中率，延迟 → 耗时 / 首字），选中即按该子列排序；**命中率**子列走后端 `sort=cache_rate`（命中 ÷ 输入，输入为 0 或未上报时为 `NULL`，与其它可空列同排序语义）。

**导出 CSV**：明细面板右上「导出 CSV」（仅 admin / operator）按**当前时间范围 + 当前钻取过滤**导出逐请求明细，`GET /api/stats/events?format=csv`。文件带 UTF-8 BOM（Excel 认编码）、`\r\n` 换行、时间列 ISO 8601（UTC），并对以 `= + - @`（及制表符/回车）开头的**文本**单元格加前导单引号防公式注入（数值含负数原样）。单次导出上限 10000 行，超出截断并在响应头 `X-Export-Truncated: 1` 标出，页面提示收窄条件。

**实时流（SSE）**：明细面板右上「实时」开关打开后，`EventSource` 订阅 `GET /api/stats/events/stream`，展示**当前筛选范围内刚落库**的请求（与下方翻页的历史明细分开展示，避免混成一张会跳动的表）。事件在 `StatsCollector.record` **事务提交后**由进程内 `LiveEventHub` 广播（不轮询数据库），前端只保留最近 50 条、新→旧排列；关闭开关即断开连接。

- **作用域与过滤**：与 `/api/stats/events` 同口径——admin/operator 可传 `username`（不传 = 全量），viewer 恒回落到本人；`provider` / `model` / `credential_id` 三个过滤可叠加。换过滤会重建连接（`EventSource` 无法改查询串）。
- **凭证昵称**：广播 payload 不含昵称（与明细端点一致，只给已脱敏字段 + `rowid`）；仅 admin/operator 的连接在**服务端逐条按需补** `credential_name`，viewer 拿不到池里他人凭证的昵称。
- **背压与上限**：每个订阅者一个**有界**缓冲（200 条），满了丢**最旧**的一条（实时流看的是「现在」）；并发订阅上限 8，超出回 429 + `Retry-After`，不排队、不静默拖垮服务。
- **保活与缓存**：15 秒一条 SSE 注释帧（`:` 开头，`EventSource` 忽略）防止反向代理断掉静默连接；响应带 `Cache-Control: no-store` 与 `X-Accel-Buffering: no`，避免中间层缓存或整段缓冲把「实时」变成批量。断线由浏览器自动重连，页面在浏览器放弃重连（如 401 / 429）时提示断开。

若历史数据偏小（老库升级前落的明细没被覆盖、或补过明细后没重算），用 `python3 scripts/rollup_hourly.py --apply` 一次性重算（默认预览、**写库前自动备份**、幂等）。脚本先报告缺口与两侧时间跨度，并提示**明细已过保留期无法还原**的区间。

### 列表排序

管理台各列表**列头可点排序**：点一次用该列的「首次点击方向」（数值列先降序、文本列先升序），再点同一列在升序 / 降序间切换。排序一律由**后端**完成——所有 `GET` 列表端点接受统一的 `sort`（白名单键）与 `order`（`asc` / `desc`）参数。**未知 `sort` 键或非法 `order` 值回落到默认排序，不报错**；`sort` 是白名单键而非 SQL 列名，注入面为零。

适用端点：`/api/credentials`、`/api/users`、`/api/api-keys`、`/api/audit`、`/api/alerts`、`/api/model-catalog`、`/api/credentials/{id}/credit-events`、`/api/credentials/{id}/accounts`、`/api/stats/by-*`、`/api/stats/events`（逐请求明细：默认按 rowid 游标分页返回 `next_before`；按其它列排序或时间升序时改用 `LIMIT/OFFSET` 并返回 `total`）。各端点的白名单键与默认排序见 `src/api/`。

### 列表分页

管理台各列表**统一分页**：共享同一套页码文案（`第 N / M 页`）、每页选项（`20 / 50 / 100`，默认 `50`）与上一页 / 下一页按钮。扁平列表一律由**后端**分页——`GET` 列表端点接受 `limit`（每页条数）+ `offset`（偏移）并返回 `total`（**筛选后**的总条数，供前端算页数）；原列表键名不变，只新增 `total`，向后兼容。`/api/users`、`/api/api-keys`、`/api/alerts`、`/api/audit` 在 SQL 层 `LIMIT/OFFSET`；`/api/model-catalog` 因排序键含派生字段（能力分 / 可用性）先内存排序再切片。

`/api/model-catalog` 另接受两个筛选参数（服务端生效，前端搜索框去抖 250ms 后回第一页）：`q`（大小写不敏感地匹配模型 `id` 与名称）与 `only_available`（`true` 时只留 `available=true` 的行）。`total` 是筛选后的条数，顶层 `count` 仍是目录全量（页头展示「命中 N / 共 M」）。

**分组类表**（凭证页按渠道分组、用量统计分组表）不分页，保持一次性全量展示——它们的行数由渠道 / 分组维度决定，本就不受明细规模影响。

## 后台任务

额度探测、token 预刷新、每日签到、成长中心、活跃上报、明细清理、数据库备份、渠道模型列表刷新、模型目录刷新（OpenRouter）、运维告警、上游状态页聚合、模型主动探活共 12 类，由 `TaskRunner` 自动调度，失败互不影响。每类任务的上次执行时间、最近结果与错误在管理台**「任务与配置」页**查看（30 秒自动刷新），运行态只存在于**本次进程**内，重启归零。每个任务卡片带「立即运行」按钮（`POST /api/tasks/{key}/run`，admin + CSRF）：**绕过时点窗口但绕过不了开关**——`due()` 未到点也照跑，而任务被开关关掉时如实回 `ran=false`（不把「关了」谎报成「跑过了」）；同一任务已在跑时立刻回 `busy=true`、不排队、不并发打上游；同步等这一轮跑完再返回结果。

**渠道模型列表刷新**（`MODEL_CATALOG_MINUTES`，默认 30、下限 5）是兜底保鲜：模型表本身按「谁访问 `/v1/models` 或 Playground 谁刷新、TTL 300s」更新，这条只保证**没人访问时也会更新**。周期取 30 分钟是跟 zen 免费模型判活缓存对齐——再密也不会让 zen 多探活。

**成长中心**仅 CodeBuddy 有。Buddy 旅行 1–4 小时回来一次，故周期默认 60 分钟（`GROWTH_INTERVAL_MINUTES`）。抽奖 / 连登兑换 / 开 Buddy 盲盒 / 消耗补登卡属**不可逆动作**，`GROWTH_IRREVERSIBLE_ACTIONS=false` 可全部跳过。凭证列表「成长中心」列显示每个账号最近一轮结果，行内菜单可手动执行一次。

### 活跃上报（可选，默认关闭）

CodeBuddy 成长中心的「连登天数 / 活跃地图」按日统计客户端对话事件，账号长期只被本网关自动调用时会断连登。`ACTIVITY_REPORT_ENABLED=true` 时，后台任务在每天 `ACTIVITY_REPORT_HOUR`（默认 10 点）所在整点窗口为每个账号补发**一条** `chat_request_send` 事件续上连登——与积分、调度无关，上报失败不影响聊天请求。凭证行内菜单也可手动补报一次。适用于 CodeBuddy 与 CodeBuddy 国际版。

> ⚠️ **风险与限制**：官方活动条款禁止用模拟器 / 脚本篡改活动数据，处罚为**取消资格并追回已发礼品**。默认关闭，开启前请自行评估账号风险。事件名与请求形状依赖上游实现，**上游改版即失效**，不承诺任何收益，不作为可靠性功能。实测（2026-09-21）：缺少 `userId` 时上游返回 HTTP 200 `{"code":0}` 却**静默丢弃**；OAuth 凭证的 `user_id`/`account_uid` 可能为空，此时回落取 bearer JWT 的 `sub`，仍缺失则跳过（不编造）。

### 运维告警

后台周期评估四类风险并落 `alert_events`（管理台「运维告警」页回看，admin-only）：

| 规则 | 触发条件 |
|---|---|
| `pool_empty` | 池里有凭证但可用数少于 `ALERT_POOL_READY_MIN` |
| `task_failed` | 某后台任务连续失败达到 `ALERT_TASK_FAILURES` 次（成功一轮清零） |
| `token_expiring` | 某凭证 token 剩余寿命落在 `ALERT_TOKEN_EXPIRY_HOURS` 内 |
| `error_rate` | 统计窗内上游失败占比达到 `ALERT_ERROR_RATE_THRESHOLD` 且样本足够 |

- **静默窗**（`ALERT_SILENCE_MINUTES`）：同一 `(规则, 对象)` 窗口内只落库 / 推送一次，避免周期性评估把记录刷屏。
- **迟滞**（`ALERT_FOR_TICKS` / `ALERT_CLEAR_TICKS`）：连续命中若干轮才真正告警，连续未命中若干轮才确认恢复。恢复时给该 `(规则, 对象)` **全部尚未恢复**的行写 `resolved_at`（同一次故障的「提醒」行一起收尾），并外推一条 `status="resolved"` 的 webhook；页面把 `resolved_at` 非空的行显示为「已恢复」。
- **重启自愈**：迟滞**计数**进程内维护、重启归零重新累积；但「当前哪些告警未恢复」是已落库的事实，`AlertTask` 启动时从库里未恢复的行重建，所以重启前落库、条件已消失的告警仍会被正常标记恢复。
- **人工处置**：页面每行未恢复的告警带「标记恢复」按钮（`POST /api/alerts/{id}/resolve`，admin + CSRF + 审计），用于规则已下线 / 误报等后台无法自动判定的情况。只标该行，不按 `(规则, 对象)` 批量——同组可能还有仍在告警的记录。
- **保留期**：与请求明细一致（默认 90 天），由后台清理任务裁剪。
- webhook 地址与各阈值见「任务与配置」页的「运维告警」卡片。

### 数据库备份与恢复

**后台自动备份**（`BACKUP_INTERVAL_MINUTES`，默认 1440 分钟 = 每日一次）把主库一致性快照写进 `DATA_DIR/backups/`，并只保留最近 `BACKUP_KEEP` 份（默认 7）。

- **用 `VACUUM INTO` 而不是复制文件**：WAL 模式下主库文件不含尚未 checkpoint 的已提交事务，直接 `cp` 会丢数据。`VACUUM INTO` 读一份一致性快照并重写成**紧凑、无 WAL 的独立库**，恢复时只需原子替换主库。
- **落盘即校验**：写完立刻 `PRAGMA integrity_check`，不过则删掉半成品并记日志——一个写坏的备份比没有备份更危险。产物 `0600`、目录 `0700`（库内是凭证密文与明文用量/审计）。
- **失败不影响服务**：备份是普通后台任务，异常只记日志、进任务卡片，不拖垮聊天请求。

**手动备份**（升级/迁移前留一份）：

```bash
python3 scripts/backup_db.py                 # 备份到 DATA_DIR/backups/，保留 7 份
python3 scripts/backup_db.py --keep 14       # 保留 14 份（0 = 不清理）
python3 scripts/backup_db.py --out /path/dir # 指定备份目录
```

**恢复**（**先停服**，脚本不做「服务是否在跑」探测——容器/权限都可能误判）：

```bash
python3 scripts/restore_db.py data/backups/coding2api-20261014-030000.sqlite3            # 预览：只校验，不改动
python3 scripts/restore_db.py data/backups/coding2api-20261014-030000.sqlite3 --apply    # 校验 → 安全副本 → 原子替换
```

恢复流程：校验备份完整性（不过直接拒绝，绝不把坏文件换进生产库）→ 给现有主库做一份 `.pre-restore-<时间戳>` 安全副本（同样走 `VACUUM INTO`，不漏 WAL 中未 checkpoint 的事务）→ 原子替换主库 → 删除 `-wal`/`-shm` 残留（它们属于旧库，留着会损坏新库）。**恢复后重启服务。**

## 配置

常用项如下；完整的可配置项见 `src/config.py`（权威），且**每一个都已透传到 `docker-compose.yml`**。容器里改监听地址用 `HOST`/`PORT`（`PORT` 同时决定宿主机映射端口）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | **必填** | 凭证加密密钥，≥16 字符；丢失 = 凭证全部作废，无密钥轮换。启动时拒绝低熵值（全同字符 / 周期重复 / 字符集过小） |
| `ADMIN_USERNAMES` | 空 | **仅引导期**：逗号分隔的用户名，首次启动时被提权为 `admin`；DB 里已有角色后不再生效 |
| `USERS_FILE` | `secrets/users.txt` | **仅引导期**：老式用户文件路径，启动时一次性导入 SQLite（幂等）。账号唯一源是 `users` 表 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 浏览器可达地址；TRAE 登录回调（`+/authorize`）依赖它，scheme 为 https 时额外下发 HSTS。**不决定会话 Cookie 的 `Secure`**——那按请求实际协议动态判定（`X-Forwarded-Proto` 优先，无该头则看 Host），同一实例 LAN(http) 与隧道(https) 双入口才能都登录 |
| `DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `DEFAULT_MODEL` | `deepseek-v4.1-flash` | 模型为空/`auto` 时的默认 |
| `QUOTA_PROBE_MINUTES` | `60` | 额度探测周期（下限 1 分钟） |
| `REFRESH_INTERVAL_MINUTES` | `30` | token 预刷新轮询周期（下限 5 分钟）；短寿命渠道的预刷新窗口封顶必须**宽于**它 |
| `MODEL_CATALOG_MINUTES` | `30` | 渠道模型列表兜底刷新周期（下限 5 分钟） |
| `OPENROUTER_CATALOG_MINUTES` | `1440` | 模型目录刷新周期（下限 60 分钟）：后台从 OpenRouter 公开接口一次抓取刊例价（成本估算）、明细元数据（「模型列表」页）与 Artificial Analysis 三项指数（Playground /「模型列表」页 / `/v1/models` 能力分）并落盘；上游变动少，默认每日一次 |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | 模型目录来源地址（匿名可读、无需 key）；改后需重启 |
| `USD_CNY_RATE` | `6.70` | 成本估算的美元兑人民币汇率；可热更，只影响之后写入的请求 |
| `GROWTH_INTERVAL_MINUTES` | `60` | 成长中心（仅 CodeBuddy）一轮领取的周期（下限 5 分钟） |
| `GROWTH_IRREVERSIBLE_ACTIONS` | `true` | 是否允许成长中心的不可逆动作：抽奖、连登兑换、开盲盒、消耗补登卡 |
| `ACTIVITY_REPORT_ENABLED` | `false` | 活跃上报（CodeBuddy 与国际版）：每天为账号补发一条对话事件续连登。**默认关闭**——条款禁止篡改活动数据，上游改版即失效 |
| `ACTIVITY_REPORT_HOUR` | `10` | 活跃上报的本地（北京）时间整点窗口；仅在开关开启时生效 |
| `CHECKIN_TIMES` | `""` | 每日签到执行时点：本地时间 `HH:MM`，逗号分隔（如 `09:37,21:10`）。命中时点才跑一轮；**留空 = 全天每 10 分钟检查一次**（旧行为，升级后不做任何配置也不变节奏）。坏段忽略并记 warning，全是坏段等同留空。上游签到幂等，多设几个时点只是多几次重试机会。配了时点后循环改为**每 60 秒**醒一次（否则抖动=0 时窗只有一分钟，必然整天漏签）。可热更 |
| `CHECKIN_JITTER_MINUTES` | `0` | 签到抖动（分钟）：在时点**之前**提前一个 `0..N` 的随机偏移执行，把多部署摊开、避免同一分钟齐打上游。按「日期 + 时点」记去重键，抖动只改执行时刻，不会让同一时点跑两次。`0` = 不抖。可热更 |
| `LOG_BUFFER_LINES` | `500` | 管理台「服务端日志」页的进程内环形缓冲行数（只收本进程 `logging` 记录，不读文件）。`0` = 关闭该页（`GET /api/logs` 与 `/api/logs/stream` 均 404）。每行发出前过脱敏白名单，命中 `token` / `secret` / `authorization` / `cookie` / `password` / `api_key` 或 `sk-…` 形状的**整行丢弃**。可热更 |
| `ALERT_ENABLED` | `true` | 运维告警开关：后台周期评估四类风险（池耗尽 / 任务连续失败 / token 临近到期 / 上游错误率骤升），命中落 `alert_events` |
| `ALERT_WEBHOOK_URL` | 空 | 告警 Webhook：命中时 POST JSON（body 带 `status`：`firing` / `resolved`），多个用逗号分隔（≤16 个，仅 `http(s)`，拒内网/回环/链路本地/保留地址防 SSRF，投递前再按 DNS 解析复核一次、解析失败即跳过），并发投递、全部成功才算成功（总预算 15s）；请求恒带 `X-Coding2API-Signature: sha256=<HMAC-SHA256(APP_SECRET, body)>` 供收端验真；留空 = 只留站内记录 |
| `ALERT_INTERVAL_MINUTES` | `5` | 运维告警评估周期（下限 1 分钟） |
| `ALERT_SILENCE_MINUTES` | `30` | 同一 `(规则, 对象)` 的静默窗：窗口内只落库 / 推送一次；`0` 关闭静默 |
| `ALERT_FOR_TICKS` | `1` | 进入迟滞：同一告警连续命中这么多轮才真正告警（滤掉单轮毛刺）；`1` = 首轮即报 |
| `ALERT_CLEAR_TICKS` | `2` | 恢复迟滞：告警中的条目连续这么多轮不再命中才确认恢复（写 `resolved_at` 并外推 `status="resolved"` 通知），避免单轮抖动误撤 |
| `ALERT_POOL_READY_MIN` | `1` | 池耗尽阈值：可用凭证数少于该值时告警；`0` 关闭该规则 |
| `ALERT_TASK_FAILURES` | `3` | 后台任务连续失败达到该次数时告警（成功一轮清零）；`0` 关闭 |
| `ALERT_TOKEN_EXPIRY_HOURS` | `24` | token 剩余时间少于该小时数时告警；`0` 关闭 |
| `ALERT_ERROR_RATE_THRESHOLD` | `0.5` | 上游错误率阈值：统计窗内失败占比达到该值且样本足够时告警；`0` 关闭 |
| `ALERT_ERROR_RATE_MIN_REQUESTS` | `20` | 错误率最小样本数：窗内请求数少于该值不判 |
| `ALERT_ERROR_RATE_WINDOW_MINUTES` | `15` | 错误率统计窗（分钟），数据源为 `usage_events` 明细 |
| `BACKUP_INTERVAL_MINUTES` | `1440` | 数据库备份后台任务周期（分钟），下限 60；`VACUUM INTO` 一致性快照落 `DATA_DIR/backups/` |
| `BACKUP_KEEP` | `7` | 备份保留份数：只留最近这么多份，更旧的按时间删除；`0` 表示不清理 |
| `UPSTREAM_STATUS_ENABLED` | `true` | 上游状态页聚合开关：后台并发抓各模型 / 网关提供方的官方状态接口并归一化，管理台「上游状态」页只读展示。**只做解释不做决策**；快照只存内存、重启归零 |
| `UPSTREAM_STATUS_MINUTES` | `5` | 上游状态页抓取周期（下限 1 分钟） |
| `UPSTREAM_STATUS_TIMEOUT_SECONDS` | `10.0` | 单源 HTTP 超时（秒）：8 个源并发抓，慢的那个不该拖住整轮 |
| `MODEL_PROBE_MINUTES` | `0` | 模型主动探活周期（下限 1 分钟，`0` = 关闭）。只探仍被 `blocked` 负缓存（11102「该后端无此模型」）锁住的 (渠道, 模型)，用一条最小对话区分「模型真没了」与「额度/限流/网络」：成功即清该渠道的负缓存（唯一的提前恢复入口），上游明确说没这个模型才算失败，其余一律「无法判定」且**绝不禁用**。探活直连 provider、不写 `usage_events`、不动冷却、不参与选号；结论落独立的 `model_probes` 表。开启会真发上游请求、消耗额度，按余量决定；负缓存本身 6h 起步，实际建议 ≥60 分钟。可热更，也可在「模型列表」页对单个模型点「立即探活」 |
| `QUOTA_EXPIRY_WINDOW_SECONDS` | `129600` | 主到期排序窗口：把距到期 ≤ 该秒数的积分加总，多的账号先用；CodeBuddy 与 TRAE 都按包独立到期、都参与；`≤0` 关闭整套到期排序 |
| `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` | `604800` | 次到期排序窗口：主窗口打平时才比较；`≤0` 关闭该级 |
| `CONVERSATION_STICKY_SECONDS` | `3600` | 会话粘性 TTL：优先级 **请求体显式会话标识**（`conversation_id` / `prompt_cache_key`，`metadata` 优先）> **请求头会话标识**（`x-session-id` / `x-opencode-session` / `x-session-affinity` / `conversation-id`）> **消息前缀指纹**兜底；手动 pin 优先于全部。头与指纹值都掺 `username` 再哈希，两用户同值不串号；头必须过字符白名单（`[A-Za-z0-9._:-]`，≤128），非法则丢弃退回兜底。带 `user_id` 时不派生前缀兜底键（头照常生效）；`≤0` 关闭 |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,default,seed-code-pro-*,sagitta,aquila,glm-4.*,glm-5,glm-5-turbo,glm-5.0*,glm-5.1,glm-5v-*,kimi-k2.5,kimi-k2.6,kimi-k2-thinking,minimax-m2.5,deepseek-v3-*,hunyuan-*,hy3*,deepseek-v4-*,qwen3.7-*,auto,nemotron-3-*,glm-5.2*,nemotron-3*,lfm2*,north-mini-code,openrouter-models-router,fast,balanced,deep,primary,hy4-preview,inkling-small,minimax-m2.7,kimi-k2.*,minimax-m3,gpt-5.4,gpt-5.5,laguna*,qwen3-vl-235b,seed-2.1*,xing4.0-29b-a4,glyph-cluster,qwen3.8-flash,seed-evolving` | 模型列表黑名单（fnmatch，仅影响列表展示）；按**归一后的对外写法**匹配（原代号、归一键、展示名、展示名归一键四种任一命中即滤） |
| `MODEL_FALLBACK_GROUPS` | `""` | 跨渠道 fallback 兼容组：格式 `组名=成员1,成员2;组名2=成员3`；成员可带 `@渠道`。请求任一成员时该成员置首、组内其余依次回退；主渠道全不可用才回退，**仅在本次尝试尚未产出任何响应帧前**切换。留空 = 不启用 |
| `ALLOWED_HOSTS` | 空 | Host 白名单，防 DNS rebinding |
| `ALLOW_TEST_HOST` | `false` | 额外放行 FastAPI TestClient 的默认 Host `testserver`。**仅测试用**，生产保持 `false` |
| `TRUST_PROXY` | `false` | 是否采信 `X-Forwarded-For` 判定来源 IP（白名单、登录限流与审计共用）。仅「本服务前恰好一层受信反代」时开启 |
| `ENABLE_DOCS` | `false` | 是否暴露 `/docs` 与 `/openapi.json`（默认关闭） |
| `DUMP_REQUEST_BODIES` | `false` | 诊断：把 `/v1` 原始请求体落盘到 `data/dumps/`（**含对话内容**，仅排查用） |
| `LOG_LEVEL` | `INFO` | 日志级别；审计日志是 INFO 级，调到 `WARNING` 会一并关掉 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址与端口（compose 默认 `0.0.0.0`）。compose 里 `PORT` 同时决定**容器内监听端口与宿主机映射端口**（两侧一致，改一个即可） |
| `CODEBUDDY_API_ENDPOINT` | `https://copilot.tencent.com` | CodeBuddy 上游地址；改动时必须同时加入 `CODEBUDDY_ALLOWED_ENDPOINTS` |
| `CODEBUDDY_ALLOWED_ENDPOINTS` | 官方两站（见 compose） | 上游端点白名单，真实 Token 只发往白名单内地址 |
| `CODEBUDDY_INTL_API_ENDPOINT` | `https://www.codebuddy.ai` | CodeBuddy 国际版上游地址；改动时必须同时加入 `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` |
| `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` | `https://www.codebuddy.ai` | 国际版白名单，**刻意不含**国内站主机（跨站发送即凭证泄露，填错启动失败） |
| `ZEN_API_ENDPOINT` | `https://opencode.ai` | OpenCode Zen 上游地址；改动时必须同时加入 `ZEN_ALLOWED_ENDPOINTS` |
| `ZEN_ALLOWED_ENDPOINTS` | `https://opencode.ai` | Zen 端点白名单 |
| `ZEN_OPENCODE_VERSION` | `1.18.0` | 门禁伪装用的 `opencode/<version>` UA 版本（低于阈值会被 426 拒绝） |
| `KILO_API_ENDPOINT` | `https://api.kilo.ai/api/gateway` | Kilo Gateway 上游地址；改动时必须同时加入 `KILO_ALLOWED_ENDPOINTS` |
| `KILO_ALLOWED_ENDPOINTS` | `https://api.kilo.ai/api/gateway` | Kilo 端点白名单 |
| `QODER_API_ENDPOINT` | `https://openapi.qoder.com.cn` | Qoder openapi 地址（额度/登录）；国际版设为 `https://openapi.qoder.sh` |
| `QODER_GATEWAY_ENDPOINT` | `https://gateway.qoder.com.cn` | Qoder 推理网关地址（聊天/模型发现）；国际版设为 `https://api1.qoder.sh` |
| `QODER_ALLOWED_ENDPOINTS` | 国内 openapi+gateway + 国际版（见 compose） | Qoder 端点白名单，带 COSY 签名的请求只发往白名单内地址 |
| `CODEARTS_API_ENDPOINT` | `https://snap-access.cn-north-4.myhuaweicloud.com` | CodeArts snap 引擎地址；改动时必须同时加入 `CODEARTS_ALLOWED_ENDPOINTS` |
| `CODEARTS_ALLOWED_ENDPOINTS` | snap 引擎 + STS + 福利网关 + 门户（见 compose） | CodeArts 端点白名单，AK/SK 签名请求只发往白名单内地址 |
| `OFFICEACE_API_ENDPOINT` | `https://agentarts.cn-southwest-2.myhuaweicloud.com` | officeAce 控制面地址（OAuth / 开通校验）；改动时必须同时加入 `OFFICEACE_ALLOWED_ENDPOINTS` |
| `OFFICEACE_ALLOWED_ENDPOINTS` | `https://agentarts.cn-southwest-2.myhuaweicloud.com` | officeAce 端点白名单。**只需控制面**：模型网关每账号不同，由上游下发并存在凭证里 |
| `OFFICEACE_CHAT_MIN_INTERVAL` | `0` | officeAce 聊天最小间隔（秒），独立节流器；`0` 关闭 |
| `PROVIDER_PROXIES` | `""` | **按渠道出站代理**（启动期项，改后需重启）；留空 = 全部直连。容器内 `127.0.0.1` 指容器自身，代理跑在宿主时须写 `host.docker.internal` |
| `CODEBUDDY_CHAT_MIN_INTERVAL` | `5` | CB 聊天节流器最小间隔（秒）：按凭证分桶、**桶内允许并发**，只在同凭证顺序连发时补足间隔；`0` 关闭 |
| `CODEBUDDY_INTL_CHAT_MIN_INTERVAL` | `5` | CB 国际版聊天最小间隔（秒），**独立**节流器；`0` 关闭 |
| `ZEN_CHAT_MIN_INTERVAL` | `0` | Zen 聊天最小间隔（秒），独立节流器，默认关闭 |
| `KILO_CHAT_MIN_INTERVAL` | `0` | Kilo 聊天最小间隔（秒），独立节流器，默认关闭 |
| `QODER_CHAT_MIN_INTERVAL` | `5` | Qoder 聊天最小间隔（秒），独立节流器；`0` 关闭 |
| `CODEARTS_CHAT_MIN_INTERVAL` | `5` | CodeArts 聊天最小间隔（秒），独立节流器；`0` 关闭 |
| `CODEARTS_MAX_CONCURRENCY` | `3` | CodeArts 每账号**在途并发上限**：上升沿满额时请求挂起直到有请求结束；`0` 关闭 |
| `CODEARTS_REQUEST_WINDOW_SECONDS` | `60` | CodeArts **账号滑动窗口**（秒）：与上一项组合成「每账号每 60s 最多启动 3 次」；`0` 关闭窗口口径 |
| `CODEBUDDY_SANITIZE_CHANNEL_MARKERS` | `true` | 出站 `system`/`assistant` 正文命中「伪装其他厂商官方客户端」指纹串时替换为占位符（上游 11128 内容风控），只改出站副本；`false` 关闭 |
| `REFRESH_SKEW_HOURS` | `24` | token 到期前该小时数窗口内预刷新。短寿命渠道自行封顶（CodeArts STS 只有 2h，封顶 45min） |
| `TOKEN_EXPIRY_WARNING_SECONDS` | `3600` | 管理台 token 到期预警阈值；`≤0` 关闭预警。纯展示，不参与调度 |
| `PACER_MIN_SECONDS` / `PACER_MAX_SECONDS` | `5` / `20` | 全局节流器随机等待区间（秒）。区间相等时（聊天节流器的 `*_CHAT_MIN_INTERVAL` 形态）额外**向上**抖动 ±25%，避免多实例精确同拍；下限不击穿 |
| `AUTO_CONTINUE_MAX` | `10` | 上游以 `finish_reason=length` 截断时同凭证自动续写的最多次数；`0` 关闭 |
| `CONTEXT_COMPRESS_ENABLED` | `true` | 按模型目录里的输入上限裁剪过长对话，避免撞上游硬限制；目录里查不到上限的模型不受影响 |
| `CONTEXT_COMPRESS_RESERVE_TOKENS` | `4096` | 压缩预算里为模型回复预留的输出 token 数 |
| `CONTEXT_COMPRESS_MIN_KEEP_MESSAGES` | `4` | 无论多长都保留的最近消息条数 |
| `CONTEXT_COMPRESS_SAFETY_RATIO` | `0.95` | 按模型上限的百分比计算压缩预算，给 token 估算误差留余量 |
| `UPSTREAM_COMPLETE_TIMEOUT_SECONDS` | `600` | 非流式聚合整体超时（秒）：超时按瞬态错误换号重试；`≤0` 关闭 |
| `STREAM_FIRST_EVENT_TIMEOUT_SECONDS` | `0` | 流式**首事件**超时（秒）：上游接了连接却长时间不吐第一个数据帧时，在**出帧前**换号重试（此时切换安全，客户端没收到任何内容）。注释帧 / 心跳不重置该计时。`0` = 关闭。**默认必须保持关闭**——实测慢上下文首字可达 40–71s，默认值会误杀正常请求；要用请按部署实际填（推荐 `60`）。可热更 |
| `STREAM_IDLE_TIMEOUT_SECONDS` | `0` | 流式**空闲**超时（秒）：**已出帧**后上游长时间不再吐数据时断流，只发一帧错误收尾——半截输出不可回滚，换号会把两个账号的内容拼给客户端。`0` = 关闭（推荐 `300`）。可热更 |
| `SHORT_COOLDOWN_WAIT_SECONDS` | `0` | 短冷却「等待续上」（秒）：全池都在短冷却、没有任何立即可用候选时，最短剩余冷却不超过该值则等待后重取一次，而不是直接 503。等待期间不占在途租约、不计入轮换预算。`0` = 关闭（快速失败，旧行为）。可热更 |
| `MODEL_ALIASES` | `""` | 自定义模型别名：`对外名=模型@渠道;对外名2=模型`，`@渠道` 省略则自动选。列表与请求解析用同一份；与真实 id / 自动别名撞车、目标不存在、渠道不持有该模型时该段跳过并记 ERROR。留空 = 不启用。可热更 |
| `MAX_INFLIGHT_PER_CREDENTIAL` | `0` | 单账号在途请求上限（跨渠道全局）：并发占满的号不再参与选号；`0` = 不限。租约请求结束释放，另有 600s TTL 兜底漏释放 |
| `METRICS_ENABLED` | `true` | 是否暴露 `GET /metrics`（Prometheus text 格式，**不鉴权**，与 `/healthz` 同口径）；`false` 关闭（返回 404）。可热更 |
| `REQUEST_LOG_ENABLED` | `true` | 请求级结构化日志（logfmt）：每个业务请求（`/api/*`、`/v1/*`、`/authorize`）一行 `event=request request_id=… method=… route=… status=… duration_ms=…`。路由用模板（`/api/credentials/{credential_id}`）不用实际 ID；**不写 query string、请求体、用户名**，靠 `request_id` 关联 `usage_events`。5xx 记 WARNING、其余 INFO；静态资源与探针不记（uvicorn 自带 access log 已关闭）。`false` 关闭请求级日志。可热更 |
| `FORCE_DISABLE_THINKING` | `false` | 出站剥离思考内容：开启后响应里不再有 `reasoning_content` / Anthropic `thinking` 块 / Responses reasoning item，**只过滤响应、不改请求体**（上游仍照常思考）。三个出口、流式与非流式一致生效。可热更 |
| `REASONING_EFFORT_MAP` | `""` | 出站 `reasoning_effort` 档位映射：格式 `渠道=from:to,from:to;*=from:to`（渠道取 `KNOWN_PROVIDERS`，`*` 兜底）；目标写 `none` = 删除该键、走渠道默认。只改写客户端**显式**给的档位，渠道自身 `setdefault` 补的默认档不受影响。留空 = 原样透传。可热更 |
| `ANTHROPIC_THINKING_EFFORTS` | `""` | Anthropic 入站 `thinking.budget_tokens` → 上游 `reasoning_effort`：格式 `预算:档位,预算:档位,disabled:档位`（`disabled` 匹配 `thinking.type=="disabled"`；其余取「阈值 ≤ budget_tokens 中最大的那一档」，低于最小阈值不映射）。留空 = 完全不映射（`thinking` 照旧忽略）。可热更 |

### 管理台热更（「任务与配置」页）

上表中带「可热更」语义的 **58 项**可不改 `.env`、不重启，直接在管理台「任务与配置」页修改：

`DEFAULT_MODEL`、`MODEL_BLOCKLIST`、`MODEL_ALIASES`、`CONTEXT_COMPRESS_ENABLED`、`CONTEXT_COMPRESS_RESERVE_TOKENS`、`CONTEXT_COMPRESS_MIN_KEEP_MESSAGES`、`CONTEXT_COMPRESS_SAFETY_RATIO`、`MODEL_FALLBACK_GROUPS`、`STREAM_FIRST_EVENT_TIMEOUT_SECONDS`、`STREAM_IDLE_TIMEOUT_SECONDS`、`SHORT_COOLDOWN_WAIT_SECONDS`、`QUOTA_EXPIRY_WINDOW_SECONDS`、`QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS`、`CONVERSATION_STICKY_SECONDS`、`MAX_INFLIGHT_PER_CREDENTIAL`、`GROWTH_IRREVERSIBLE_ACTIONS`、`GROWTH_INTERVAL_MINUTES`、`REFRESH_INTERVAL_MINUTES`、`QUOTA_PROBE_MINUTES`、`MODEL_CATALOG_MINUTES`、`OPENROUTER_CATALOG_MINUTES`、`USD_CNY_RATE`、`CODEBUDDY_CHAT_MIN_INTERVAL`、`CODEBUDDY_INTL_CHAT_MIN_INTERVAL`、`ZEN_CHAT_MIN_INTERVAL`、`KILO_CHAT_MIN_INTERVAL`、`QODER_CHAT_MIN_INTERVAL`、`CODEARTS_CHAT_MIN_INTERVAL`、`CODEARTS_MAX_CONCURRENCY`、`CODEARTS_REQUEST_WINDOW_SECONDS`、`PACER_MIN_SECONDS`、`PACER_MAX_SECONDS`、`ACTIVITY_REPORT_ENABLED`、`ACTIVITY_REPORT_HOUR`、`ALERT_ENABLED`、`ALERT_WEBHOOK_URL`、`ALERT_INTERVAL_MINUTES`、`ALERT_SILENCE_MINUTES`、`ALERT_FOR_TICKS`、`ALERT_CLEAR_TICKS`、`ALERT_POOL_READY_MIN`、`ALERT_TASK_FAILURES`、`ALERT_TOKEN_EXPIRY_HOURS`、`ALERT_ERROR_RATE_THRESHOLD`、`ALERT_ERROR_RATE_MIN_REQUESTS`、`ALERT_ERROR_RATE_WINDOW_MINUTES`、`BACKUP_INTERVAL_MINUTES`、`BACKUP_KEEP`、`UPSTREAM_STATUS_ENABLED`、`UPSTREAM_STATUS_MINUTES`、`MODEL_PROBE_MINUTES`、`METRICS_ENABLED`、`REQUEST_LOG_ENABLED`、`FORCE_DISABLE_THINKING`、`REASONING_EFFORT_MAP`、`ANTHROPIC_THINKING_EFFORTS`、`CHECKIN_TIMES`、`CHECKIN_JITTER_MINUTES`、`LOG_BUFFER_LINES`。

要点：**优先级 `DB 覆盖值 > .env`**——改过后 .env 对该项不再生效，页面标「DB 覆盖」，点「恢复默认」删掉覆盖行才回落 .env。值存 `runtime_settings` 表（纯 key/value），新增可热更项无需迁移；白名单外的 key、非法类型 / 越界值写入前即拒。启动期项（`APP_SECRET` / `PORT` / `DATA_DIR` / `USERS_FILE` / `OPENROUTER_MODELS_URL` / 上游端点白名单）**不在**白名单。接口 `GET /api/settings` 读快照、`PUT /api/settings` 写（admin + CSRF），传 `null` 恢复默认。该页同时展示**后台任务运行态**，页面按 tab 组织；运行态是**进程内**的（`GET /api/tasks`，admin，页面每 30 秒刷新），重启归零。

## 部署注意

- **挂载目录属主**：容器内以 uid 1001（`appuser`）运行，`./data` 与 `./secrets` 必须可写/可读，否则 SQLite 打不开：

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **时区**：镜像默认 `TZ=Asia/Shanghai`，`.env` 里的 `TZ` 会透传进容器（compose 已声明该变量）。签到 / 额度周期按「今天 9 点」判断，务必与宿主一致；基础镜像已装 `tzdata`，否则 `TZ` 会被静默忽略、退回 UTC。

- **端口**：compose 里 `PORT` 同时决定容器内监听端口与宿主机映射端口，改一个即可（如 `PORT=9000` → 宿主 9000、容器内也监听 9000）。健康检查同步跟随，无需手工改。

- **改代码后重建镜像**：compose 同时声明 `build: .` 与 `image:`，`docker compose up -d` **不带 `--build`** 会直接用本地已存在的 `ghcr.io/robbsluo/coding2api:latest`，跑的是旧代码。改 `src/` 或前端后务必：

  ```bash
  docker compose up -d --build
  ```

### 升级后必须重启后端

**改动 `src/` 后必须重启进程，否则出现「新前端 + 旧后端」错配。** 典型症状：管理台页面能打开（前端产物本就是静态文件），但调用新端点全部失败——旧后端没有该路由，未匹配的 `/api/*` 按约定返回 JSON `404`，前端当成通用失败，弹出与真实原因无关的提示。前端不需重启（后端每次请求现读 `web/dist`），**后端不是**——进程管理器只在进程**退出**时重新拉起，不监听源码变化。

```bash
docker compose up -d --force-recreate   # Docker / compose
sudo systemctl restart coding2api       # systemd
```

**确认升级已生效**（先查版本，再查路由）：

```bash
# 1) schema 版本已迁移（期望 22）且账号已导入
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version; SELECT username, role, enabled FROM users;"
# 2) 路由存在：期望 401（未登录），404 = 旧后端进程
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users
```

### 前端产物与后端在同一端口

后端会把前端产物放在 `/` 直接服务（`src/webapp/static.py` 每次请求现读磁盘），因此**只改前端时构建完刷新即可，无需重启后端**；`localhost:5173` 是 Vite 开发态（HMR），两者可并存。**这条只对前端成立**——改了 `src/` 下后端代码必须重启进程。

`scripts/build-web.sh` 是构建入口，会处理「依赖未装」与「node 不在 PATH」两类常见情况：

```bash
./scripts/build-web.sh            # 仅在产物过期时构建
./scripts/build-web.sh --force    # 无条件重建
```

> **部署相关操作记在本地文档**：当前开发机的进程管理方式、路径与工具版本等见 `docs/local-environment.md`。该目录已在 `.gitignore` 中，**克隆仓库的人不会有这个文件**。

## 公网入口（VPS + frp 反向隧道）

家宽在 CGNAT 后没有可入站地址，公网入口只能由内网侧**主动连出**建隧道。
Cloudflare Tunnel 免费版会把大陆流量绕到洛杉矶边缘（实测端到端 1.1–2.4s、丢包约 60%），
换成「境外 VPS 做入口 + frp 隧道回源」后，端到端降到 80–150ms。**只改入站链路**：
出站到上游仍走本机 `PROVIDER_PROXIES`，不受影响。

```
客户端 ──443/TLS──> VPS(nginx) ──frp 隧道──> 家中 Mac:8000
```

产物在 `deploy/frp/`（frps/frpc 配置）、`deploy/nginx/`（反代 + TLS）、
`deploy/systemd/frps.service`（VPS 侧）、`deploy/launchd/com.coding2api.frpc.plist`
（Mac 侧）。配置格式为 frp v0.71.1 的 TOML。

### VPS 侧（Linux + systemd）

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
# 从 https://github.com/fatedier/frp/releases 取 linux_amd64 包，解出 frps
sudo install -m 755 frps /usr/local/bin/frps

sudo useradd --system --no-create-home --shell /usr/sbin/nologin frps
sudo install -d -m 755 /etc/frp
sudo install -m 644 deploy/frp/frps.toml /etc/frp/frps.toml
# 两侧共用的 token（客户端安装时用同一个值）
openssl rand -hex 32 | sudo tee /etc/frp/frps-token >/dev/null
sudo chmod 600 /etc/frp/frps-token && sudo chown frps:frps /etc/frp/frps-token

sudo install -m 644 deploy/systemd/frps.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now frps

# nginx：把 __TUNNEL_DOMAIN__ 换成实际域名后安装
sed 's/__TUNNEL_DOMAIN__/ai-api.example.com/g' deploy/nginx/coding2api.conf \
    | sudo tee /etc/nginx/sites-available/coding2api.conf >/dev/null
sudo ln -sf /etc/nginx/sites-available/coding2api.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ai-api.example.com     # 签发证书并自动配置续期
```

DNS 侧把域名 A 记录指向 VPS 公网 IP；境外 VPS 无需 ICP 备案。

### 家中 Mac 侧（launchd）

```bash
brew install frp                                # 或自行下载并设置 FRPC_BIN
./scripts/install-frpc.sh --server <VPS 公网地址> --token <与 VPS 一致的 token>
launchctl print gui/$(id -u)/com.coding2api.frpc | grep -E 'state|pid'
./scripts/install-frpc.sh --uninstall           # 卸载
```

`deploy/frp/frpc.toml` 用 frp 原生的 `{{ .Envs.FRP_SERVER_ADDR }}` 模板取值，
仓库里不含任何机器特定值；地址与 token 文件路径由 plist 注入，token 落在
`secrets/frpc-token`（600，已在 `.gitignore` 中），不进 plist——`launchctl print`
会把 plist 原样打印出来。

### `.env` 要同步的三处

| 变量 | 值 | 漏了会怎样 |
|---|---|---|
| `ALLOWED_HOSTS` | 追加隧道域名 | 隧道侧所有请求 400 `invalid host header` |
| `PUBLIC_BASE_URL` | `https://<隧道域名>` | TRAE 登录回调基址错、https 下不下发 HSTS |
| `HOST` | 保持 `127.0.0.1` | 隧道回源到本机回环即可，**不必**绑 `0.0.0.0` |

`TRUST_PROXY`：VPS 上恰好一层 nginx，可置 `true`，让 API Key 的 `allowed_ips`
看到真实客户端 IP（nginx 已转发 `X-Forwarded-For`）。若仍需保留局域网直连入口，
保持 `false`——否则同网段可伪造 XFF 绕过白名单。

> 改完 `.env` 必须重启服务才算生效（见上「部署注意」）。
> frpc 与 frps 的日志都走 stdout → Mac 侧 `logs/frpc.*.log`（newsyslog 轮转，
> `deploy/newsyslog/coding2api.conf` 已含两条规则）/ VPS 侧 journald。

## 日志

应用只写 **stdout / stderr**，不写文件、不自行轮转：部署形态与采集方式各异，都靠这两个 fd 对接，轮转交给平台工具。因此**日志不会自行停止增长**——按下面对应形态配一次即可。

| 部署形态 | 日志到哪 | 轮转机制 | 需做什么 |
|---|---|---|---|
| **Docker / compose** | `docker logs coding2api` | json-file 驱动（compose 配好 `10m × 5`） | **无需操作** |
| **Linux（systemd）** | `journalctl -u coding2api` | journald 自带 | **无需操作**（模板见 `deploy/systemd/`） |
| **Linux（非 systemd）** | 重定向到 `/var/log/coding2api/*.log` | `logrotate` | `sudo cp deploy/logrotate/coding2api /etc/logrotate.d/` |
| **裸跑** | 终端 stderr | 无 | 自己重定向并自备轮转 |

级别用 `LOG_LEVEL`（默认 `INFO`）。**审计日志是 INFO 级**，调到 `WARNING` 会把它一并关掉。

### 请求级结构化日志

除 uvicorn 自带的裸 access log 外，每个业务请求（`/api/*`、`/v1/*`、`/authorize`）额外写一行 logfmt：

```
event=request request_id=9f2c… method=POST route=/v1/chat/completions status=200 duration_ms=1234
```

- **`route` 是路由模板**（`/api/credentials/{credential_id}`），不是实际路径——资源 ID 不进日志，日志也不会按 ID 无限分叉；未匹配到路由（404、被安全中间件拒掉）时回落为清洗过的原始路径（白名单字符 + 120 字符截断）。
- **不写 query string、请求体、响应体、请求头、用户名**。query 里可能被客户端塞 token；身份靠 `request_id` 去 `usage_events.request_id` 关联（那里已有 `username` 列），日志系统保留更久、权限更宽，能少放就少放。
- `request_id` 与响应头 `X-Request-ID` 同值，可双向对照。
- **级别**：`5xx` 记 WARNING（服务端自己的问题，值得抓），`4xx` 与成功记 INFO（否则 401 探测、拼错路径会把告警日志淹没）。
- **不记**静态资源（`assets/*.js`）与探针（`/health`、`/healthz`、`/metrics`）：探针已有 `/healthz` 计数与 metrics 覆盖，静态资源量级大且无排障价值，记下来只会把业务行挤出视野。
- **uvicorn 自带 access log 已关闭**（`run()` 传 `access_log=False`）：它没有时间戳、没有 request_id、带完整 query string，与上面这层完全重复，只是把同样的请求再写满另一个轮转文件。排查探针/静态资源请用 `/healthz` 计数与 metrics。
- 开关 `REQUEST_LOG_ENABLED`（默认 `true`，可热更），关掉后不再有请求级日志。

> 容器里的 `data/dumps/`（诊断开关 `DUMP_REQUEST_BODIES=true` 写入）不在 docker 日志体系内，由应用自行保持最多 200 份。

## 开发

```bash
# 后端：lint + 测试（行/分支覆盖门槛 100%）
uv run ruff check src tests scripts
uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100

# 前端
cd web
pnpm exec tsc --noEmit
pnpm exec vitest run
pnpm build
```

## 文档

| 文档 | 内容 |
|---|---|
| [`PROPOSAL.md`](PROPOSAL.md) | 目标与非目标、上游可行性核实、架构、数据模型、安全边界 |
| [`TECHNICAL.md`](TECHNICAL.md) | 技术栈、模块规格、Provider 协议、请求时序、测试策略 |
| [`diagrams/coding2api-architecture.html`](diagrams/coding2api-architecture.html) | 系统架构图（浏览器打开） |
| [`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html) | 请求主链路时序图（浏览器打开） |
| [`diagrams/coding2api-credential-lifecycle.html`](diagrams/coding2api-credential-lifecycle.html) | 凭证调度状态机（浏览器打开） |

## 授权协议

MIT，见 [LICENSE](LICENSE)。借鉴的上游项目署名见 [NOTICE](NOTICE)。
