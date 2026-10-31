# Coding2API

八个上游——**CodeBuddy**（国内 + 国际）、**TRAE SOLO**、**OpenCode Zen**、**Kilo**、**Qoder**、**CodeArts**、**OfficeAce**——封装成 OpenAI 兼容 API：公共凭证池、统一模型名与调度、按人用量统计。

> [!WARNING]
> 逆向接口，仅供学习研究，未做安全审计；公网部署需反向代理 + 鉴权 + IP 白名单。

## 特性

- **OpenAI 兼容出口**：`/v1/chat/completions`（流式 / 非流式）、`/v1/responses`（Codex CLI）、`/v1/messages`（Claude Code）、`/v1/models`（含单模型查询）、`/v1/user/balance`。
- **统一调度**：扁平名按健康度选号；`模型@渠道` 强制。优先级：免费渠道 → 在途最少 → 到期额度 → 健康度 + 分级冷却 → 余额多者。模型级限流只避让该模型；多轮对话粘同一凭证，出错才轮换。
- **到期额度优先**：主窗口（默认 36h）先烧将过期额度；打平再比 7 天窗口。单位统一积分。
- **模型列表按凭证加载**：只列有可用凭证的渠道 → 无幽灵模型。
- **公共凭证池**：admin 维护、全员共享、加密入库（`APP_SECRET`）；设备码登录、多账号切换、额度探测、签到、token 预刷新。
- **三角色**：`admin` / `operator` / `viewer`，存 SQLite；一次性激活链接、首登强制改密、改角色 / 停用即时吊销会话；登录与写操作留审计。
- **成长中心**（仅 CodeBuddy）：领礼、派 Buddy、领任务奖、断登补登、连登兑换、开盲盒。不可逆动作 `GROWTH_IRREVERSIBLE_ACTIONS=false` 关停。
- **脱敏统计**：不存对话；明细 90 天、小时汇总永久；按人 / 渠道 / 模型 / 凭证可视化；成本按 OpenRouter 刊例价估算；速度分正文 / 总产出；明细走 SSE。
- **月度账单**（`operator` 起）：按月按人 / 按 API Key 汇总请求、token 与成本；CSV（按人）/ JSONL（逐条明细）导出并写审计。按 Key 明细受 90 天保留期限制。
- **工具调用健壮性**：`id` 缺失按 index 补并记忆；DeepSeek DSML 泄漏回读成结构化调用并改写 `finish_reason`；非流式坏 `arguments` fail-closed（400，不冷却凭证）。
- **渠道 ToS 风险登记**：导入凭证时展示 green / gray / red 与理由（运营判断），审计留等级；只提示不阻断调度。
- **出口 IP 粘性**（可选）：`CREDENTIAL_PROXY_POOL` 非空时一号一出口（sha256 稳定分配，重启不漂移）；推理、探活、签到、预刷新、成长、管理台单条动作都走同一出口。
- **上游状态聚合**：并排官方状态页，区分「上游故障」与「本服务凭证 / 额度问题」。只解释、不调度；只存内存。
- **侧栏数据提醒**：待办角标（需重登 / 耗尽、未恢复告警、任务失败、异常上游、待激活账号、将到期 Key、目录陈旧），按角色裁剪。[口径](#侧栏数据提醒导航角标)
- **管理台加固**：登录限流、CSRF、请求体上限、Host 白名单。
- **窄屏表格转卡片**：<768px 一行 = 一卡；列名收成可点排序片。DOM 不变，读屏语义一致。

协议层（私有信封、签名、门禁伪装）见 [`TECHNICAL.md`](TECHNICAL.md)。

## 界面预览

管理台（截图取自本地实例，账号 / IP 已脱敏）：

| 凭证管理：池概况与渠道分组 | 用量统计：请求 / token / 成本 |
|---|---|
| ![凭证管理](assets/screenshots/credentials.webp) | ![用量统计](assets/screenshots/stats.webp) |
| **Playground：模型 × 渠道与倍率** | **模型列表：OpenRouter 刊例价与能力分** |
| ![Playground](assets/screenshots/playground.webp) | ![模型列表](assets/screenshots/models.webp) |
| **上游状态：区分上游故障与本服务问题** | **API Key：客户端接入说明与配额** |
| ![上游状态](assets/screenshots/upstream.webp) | ![API Key](assets/screenshots/apikeys.webp) |
| **用户管理：三角色与一次性激活** | **审计日志：登录成败都留痕** |
| ![用户管理](assets/screenshots/users.webp) | ![审计日志](assets/screenshots/audit.webp) |
| **服务端日志：环形缓冲 + 脱敏** | **运维告警：七类风险周期评估** |
| ![服务端日志](assets/screenshots/logs.webp) | ![运维告警](assets/screenshots/alerts.webp) |

**任务与配置：77 项可热更，DB 覆盖值优先于 `.env`**

![任务与配置](assets/screenshots/settings.webp)

## 快速开始

前置：Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js 24+、pnpm 10+（仅前端构建）。

```bash
uv sync
uv run python scripts/create_user.py admin --role admin   # 建首个管理员
cd web && pnpm install && pnpm build && cd ..
APP_SECRET="换成你自己的随机字符串" uv run python -m src.main
```

开 <http://127.0.0.1:8000> 登录；加人走「用户管理」页。

> 用 `python -m src.main` 而不是 `uvicorn src.main:build_app`：前者读 `.env` 的 `HOST`/`PORT`，并持有单实例锁（`data/.instance.lock`）。同一 `DATA_DIR` 只能起一个进程——两个实例会各自跑后台任务、重复探测凭证并互相覆盖统计。第二个实例会打印「另一个实例已在使用 data（PID xxx）」后干净退出。

`scripts/create_user.py` 直写 SQLite（`--db` 或 `DATA_DIR`；默认 `data/coding2api.sqlite3`）。老部署 `secrets/users.txt` 启动导入一次，不覆盖已存在。

Docker：

```bash
cat > .env <<'EOF'
APP_SECRET=换成你自己的随机字符串
PUBLIC_BASE_URL=http://127.0.0.1:8000
EOF
docker compose up -d --build
docker compose exec coding2api python scripts/create_user.py admin --role admin
```

拉镜像：`docker compose pull`（或 `docker pull ghcr.io/robbsluo/coding2api:v0.10.1`）。tag `v*` 推 main → `.github/workflows/publish.yml` → 发 `<tag>` + `:latest`。

## 用户与角色

账号存 SQLite `users` 表；「用户管理」页（仅管理员）增删改。

| 角色 | 能做什么 |
|---|---|
| `admin` | 用户管理、任务与配置、凭证读写、全部统计、审计日志、服务端日志、上游状态、模型列表 |
| `operator` | 凭证读写（导入 / 删除 / 启停 / 签到 / 成长 / 切换账号）、全部统计、上游状态、模型列表 |
| `viewer` | 只读：仪表盘、凭证列表、统计、Playground |

「月度账单」与「上游状态」「模型列表」同门槛（`operator` 起）：账单是池级数据，`viewer` 只在自己的统计里看到本人用量。

- **加人**：用户名 + 角色 → 一次性激活链接（24 小时有效，只显示一次）；对方开 `/activate` 自设密码。无「管理员知道、用户不知道」的初始密码；重置同理。
- **改角色 / 停用**：即时作废该账号全部会话 Cookie。停用 = 可逆的默认「删人」（用量统计仍归该用户名）；硬删只在 CLI。
- **自助改密**：右上角用户菜单 →「修改密码」，需当前密码。
- **防锁死**：不能停用 / 降级最后一个活跃管理员；不能对自己降级或停用。

CLI 兜底（无 Web / 管理员全失联）：

```bash
python3 scripts/create_user.py alice --role admin   # 交互输入密码
python3 scripts/create_user.py --list
python3 scripts/create_user.py alice --delete --force   # 硬删，不可逆
python3 scripts/create_user.py --db data/x.sqlite3 ...
echo "$PASSWORD" | python3 scripts/create_user.py alice --role admin --password-stdin
```

`ADMIN_USERNAMES` 与 `secrets/users.txt` 仅引导期生效（首个管理员、老部署升级）；此后角色只来自 DB。引导期的判据是「库里还没有活跃 admin」——有了之后 env 不再改写任何人的角色，管理台做的降级不会被下一次重启撤销。

## 审计

「审计日志」页（仅管理员）记登录 + 写操作，按操作者 / 动作筛选、分页。类型：登录（成败都记）、账号变动、凭证写、告警处置。每条：时间 / 操作者 / 动作 / 对象 / 说明 / 来源 IP。**不含密码 / 令牌明文**——激活令牌只存 SHA-256 摘要。留 90 天。

## 服务端日志

「服务端日志」页（`/logs`，仅管理员）= 只读进程内实时日志。`GET /api/logs` 取最近 N 行；`GET /api/logs/stream` SSE 先回放再推新行。级别筛选、关键字过滤、跟随最新、暂停、清空。**无写入入口**。

- 只收本进程 `logging`；不读文件、不 tail。多进程各看各的。
- **脱敏是硬前提**：每行发出前过白名单。命中 `token` / `secret` / `authorization` / `cookie` / `password` / `api_key`、`sk-…` 形状或 `Bearer <值>` → 整行丢弃。
- 缓冲 `LOG_BUFFER_LINES`（默认 500，可热更），环形覆盖最旧。`0` 关整页（两端点 404）。
- 单行上限 4000 字符；每行带 `seq`、时间、级别、logger、`request_id`。订阅者上限 4，超限 429 + `Retry-After`。

## 使用

1. 「凭证管理」添加凭证：
   - **CodeBuddy**（含国际）：设备码登录，或粘 `{"token":"..."}`。
   - **TRAE**：粘凭证 JSON（`accessToken`/`uid`/`refreshToken`）或回调链接（`apiHost` 只收官方地址）。
   - **Qoder / CodeArts**：设备码 / OAuth2 授权码登录（CodeArts 授权后把打不开的本地回调链接粘回页面）。
   - **OfficeAce**：点「登录 OfficeAce」走华为云授权（回调由云端承接）。
   - **OpenCode Zen / Kilo**：无需添加，启动自动出虚拟凭证（删了可在同面板补回）。
2. 「API Key」建 `sk-...`（明文仅显示一次）。
3. 调用：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'
```

`模型@渠道` 强制上游（`glm-5.2@trae`、`mimo-v2.5-free@zen`）；不写自动选。任意 OpenAI 兼容客户端直连（Base URL `http://127.0.0.1:8000/v1`）。「Playground」用登录会话测，无需 API Key。

### 模型名三字段

每个模型三个字段：

1. **原代号**（`raw_id`）：渠道真发的 key，永不改动。各渠道不同（Qoder `kmodel_latest` = TRAE `kimi-k3-1`），可带命名空间前缀（Kilo `kilo-auto/free`）。
2. **归一键**：剥免费档标记（`-free` / `free` 路径段）与命名空间前缀 → slug 化，如 `kilo-auto`。模型列表按它判同模型、合并。
3. **展示名**（`name`）：清洗后可读文本，如 `LongCat 2.5 Preview`。上游给就用，否则从原代号派生；品牌写法从官方（`DeepSeek` / `MiMo` / `GLM` / `GPT`）。

`GET /v1/models` 的 `id` 全是归一键；转发自动换回原代号，`by_provider.{渠道}.raw_id` 可见。**三种写法都能直接请求**。同渠道内归一键重复 → 退回原代号。

**价格与档位**：条目可选带 `cost {input, output, cache_read}`（USD / 百万 token，与成本估算同价表）与 `reasoning_efforts`（该渠道生效档位）。有才给；缺价不带。

**自定义别名**（`MODEL_ALIASES`，可热更）：`对外名=模型@渠道;对外名2=模型`，`@渠道` 可省。列表与请求解析共用一份；条目带 `custom_alias: true`。撞名 / 目标不存在 / 渠道不持有 → 该段 ERROR 跳过。无别名链。

**单模型查询**（`GET /v1/models/{id}`）：三种写法 + 自定义别名都认；大小写不敏感；带命名空间的原代号也能查。找不到 / 不在 Key 白名单 → 404（`code=model_not_found`），不泄露池内容。

**`n` 只接受 1**：出口结构只产生一个候选，`n != 1` → 400。

### 流式 usage 与思考 token

- 收尾：finish chunk 后、`data: [DONE]` 前补一帧 `choices: []` 的 usage。上游没上报就不补；未上报字段一律 `null`。
- 思考 token 需客户端带 `reasoning_effort`；CodeBuddy 未给补 `medium`（否则思考进正文）；显式值原样透传。

### 流式超时（默认关闭）

八条渠道上游流读取都是 `read=None` → 网关自兜：

- **首事件超时**（`STREAM_FIRST_EVENT_TIMEOUT_SECONDS`，默认 0）：客户端未收到内容时超时 → 可安全换号重试，记 `upstream_first_event_timeout`。注释帧 / 心跳不重置计时。
- **流内空闲超时**（`STREAM_IDLE_TIMEOUT_SECONDS`，默认 0）：已出帧后超时 → 断流 + 错误帧收尾，**不换号**（半截输出无法回滚）。

默认关是刻意的：实测慢上下文首字 40–71s。要用在管理台按部署打开（推荐 60 / 300）。

### Playground 高级参数

可选 `temperature` / `max_tokens` / `reasoning_effort`，**留空即不发该键**。前端校验边界同后端（`temperature` ≤ 1000、`max_tokens` ≤ 4,000,000）。`reasoning_effort` 仅推理模型出现，默认「不发送」。

## 渠道

> CodeBuddy（含国际）/ TRAE / Qoder / CodeArts / OfficeAce 需凭证；**Zen 与 Kilo 无凭证免费层**——池里一条虚拟占位行（调度 / 冷却 / 统计照常），额度列「免费层（无额度接口）」，健康度恒「无探测」。删虚拟行重启复活；永久停用 →「暂停」。

### OpenCode Zen（`zen`）

[opencode.ai/zen](https://opencode.ai) 免费模型，免登录。上游清单混付费、无免费标记 → `-free` 后缀收窄 → 逐个探活 → 匿名可用者进 `/v1/models`（判活缓存 30 分钟；免费模型 **x0**）。

**免费层门禁**：上游要伪装官方客户端（UA 形态、会话头、`stream:true`、tools 含门禁工具）→ 自动满足。注入空壳工具，真调用被过滤。门禁是**移动靶**，三处都做成配置（改完重启即换、随时回退）：UA 形态 `ZEN_USER_AGENT`（模板，`{version}` 代入 `ZEN_OPENCODE_VERSION`）、API 根 `ZEN_API_ROOT`、工具集合 `ZEN_GATE_TOOLS`。默认值 = 本仓实测通过的形态（`opencode/<version>`、`/zen/v1`、`bash,read`）。端点 `ZEN_API_ENDPOINT`（须在 `ZEN_ALLOWED_ENDPOINTS` 内）。付费模型强制 `模型@zen` 也只报不可用。

### Kilo（`kilo`）

[Kilo](https://kilo.ai)（`api.kilo.ai/api/gateway`）免费模型，免登录。标准 OpenAI 协议；上游 `/models` 每条带 `isFree` → 直接过滤，**不探活**（省免费配额）。

**额度与限流**：网关级约 200 请求/小时/IP。上游 429 与 502/503/504 均模型级 → 只冷却被点名模型。端点 `KILO_API_ENDPOINT`（须在白名单内）。

### Qoder（`qoder`）

[Qoder](https://qoder.com)，需真实账号：点「登录 Qoder」走设备码授权（Token 不经过浏览器）。上游私有 COSY 协议 → 本服务签名 + 解包；对外仍是标准 OpenAI。

**额度与签到**：探测 `/api/v2/quota/usage`；签到为活动制 `/sash/api/v1/me/campaigns`（积分累积），旧 `daily-check-in` 仅回退。**活动每日 10:00（UTC+8）刷新**，领取后 30 天有效；后台按该窗口封账。新协议无连续天数。**国际站无签到接口**（404）→「本区域无此接口」。

**端点与节流**：默认国内版（`openapi.qoder.com.cn` + `gateway.qoder.com.cn`）。国际站改 `QODER_API_ENDPOINT=https://openapi.qoder.sh`，域名加进白名单。`QODER_CHAT_MIN_INTERVAL` 默认 5，可热更。

**上游节点故障**：Qoder 有时把自身节点故障包成 400（`[FAIL]node:…`）→ 判模型级瞬时故障，只冷却该模型。候选耗尽 → 503（`no_healthy_credential`）。

**保活帧与排队**：上游在流中发 `body:"null"` 的保活帧（外层 200）→ 跳过、不打断输出；「排队中」包成 403 且排队标记藏在**序列化字符串**里 → 归 QUEUE 等上游点名的秒数后在原号重开，不硬禁用凭证。「每日计费次数用尽」（403 `code 110`，`Billing daily count exceeded`）→ 归额度类，冷却到次日 04:00，同样不硬禁用（HTTP 与流内信封同一判据）。

### CodeArts（`codearts`）

[华为云 CodeArts](https://codearts.huaweicloud.com)，需真实账号：点「登录 CodeArts」→ 授权后浏览器跳到打不开的本地地址 → **整条链接**粘回页面换 STS 凭证（AK/SK + security_token + refresh_token + DPoP 私钥），加密入库。

**令牌刷新刚性**：refresh_token 与 `client_id=codearts-agent`、DPoP 私钥三者绑定、一次性；刷新后必须回写新 refresh_token。预刷新任务自动完成（DPoP ES256/P-256）。窗口封顶 45 分钟，须宽于轮询周期。凭据作废 → 标「需重新登录」+ 停重试。

**每日签到**：`GET /v1/ops/delivery` → `POST /v1/ops/claim` → `POST /v1/ops/confirm`，后台每日自动领。`CLAIMED` =「已领未确认」→ 两步一起做。

**额度＝积分余额**：探测 `GET {snap}/snap-manager/v1/statistics/plugin`（前缀必须带 `snap-manager`；头 `Agent-Type: PromptCenter`）。上游数值即积分，不折算。分项包走悬浮明细；登记套餐到期日（积分过期同废）。

**签名 / 累计全文 SSE / 节流**：华为云 `SDK-HMAC-SHA256`（AK/SK + `X-Security-Token`）；白名单须含 snap 引擎、STS、门户。流式 `text` 为累计全文（替换语义）→ 还原增量。上游硬限每账号并发 3 → `CODEARTS_MAX_CONCURRENCY` + `CODEARTS_REQUEST_WINDOW_SECONDS`。

### OfficeAce（`officeace`）

[华为云 OfficeAce](https://www.huaweicloud.com/product/agentarts.html)，需真实账号：点「登录 OfficeAce」完成华为云授权即可；回调由云端承接，不监听本地端口。后端 2.5s 轮询 `/v1/claw/auth/code` 取授权码。

**开通前提**：账号必须先在 OfficeAce 客户端登录并兑换邀请码。未开通的账号登录「成功」但拿不到模型密钥 → 点名报「华为云账号 xxx 还没开通 OfficeAce 服务」。

**两套凭据，寿命不同**：

| 凭据 | 寿命 | 用途 |
|---|---|---|
| `Basic model_app_key:model_app_secret` | 不过期 | 聊天转发 |
| 临时 AK/SK + `security_token` + `project_id` | 约 2 小时 | 积分查询、签到 |

混用得 `APIG.0303 app not found`。临时凭据由预刷新任务续期；`refresh_token` 一次性轮换，与 `client_id=pdp5_for_agentarts` + DPoP 私钥绑定（与 CodeArts 票不通用）。

上游就是标准 OpenAI 协议，三处特例：

- **限流走伪 200**：HTTP 200 + `data:{"error":…}`。错误帧照常交引擎分类；`81111/81114` 只冷却「该凭据 × 该模型」。
- **模型级拒绝不牵连整条凭据**：`81004` / `81009` 走 403/404。业务码先于状态码判定 → 模型级负缓存（成功即撤销）。
- **`max_tokens` = 推理 + 正文总预算**：`≥16` 自动加 1024 余量，再夹到该模型硬上限；`<16` 原样透传；未给不补。

**积分即主账**：单位 `officeace_points`；`remaining`/`total` 直接是积分。每笔奖励独立到期、不合并重置 → 额度口径「逐笔到期」。

**每日签到**：`POST /v1/subscription/bonus/claim`——写操作，调用即真发积分；body 必须是 `{}` 且带 JSON `Content-Type`。上游对当日重复领取幂等。收获按剩余量增量算。封账到下个北京时间 0 点。

**不主动探活 / 不估 credit**：探活要真发推理烧积分；`cost_coefficient` × `tokens_per_point` 关系官方未说明。模型级拒绝改由真实请求按 `BLOCKED` 负缓存处理。

**签名与端点**：控制面 `/v1/claw/*` 用 `SDK-HMAC-SHA256`；模型网关只认 `V11-HMAC-SHA256`，用错 → `APIG.0624` 或 401。`OFFICEACE_ALLOWED_ENDPOINTS` 只需控制面主机（模型网关每账号不同，由上游签发存凭证）。`OFFICEACE_CHAT_MIN_INTERVAL` 默认 0。

**单账号单用户**：官方《套餐管理》限制单账号单用户 → 不共用。

**换账号**：登录按钮旁「换账号」（`force_login=true`），先清浏览器华为云会话再授权。

### CodeBuddy 国际（`codebuddy_intl`）

[www.codebuddy.ai](https://www.codebuddy.ai)，同国内站协议，但两套独立账号体系（各自注册、各自计费）→ 独立渠道。登录链路同国内版（设备码）。

不合并原因：`CODEBUDDY_API_ENDPOINT` 是进程级单值 → 切过去会让存量国内站凭证打到国际站。两站可同时持凭证，各走各的端点、节流器、登录流程。

**白名单刻意互不含**：`CODEBUDDY_INTL_ALLOWED_ENDPOINTS` 默认只有国际站主机；填国内站地址 → 启动报错（跨站发送即凭证泄露）。

**能力差异**：聊天、模型列表、额度探测、token 刷新、活跃上报全启用；**签到、成长中心未启用**（渠道侧无该活动；成长中心无法验证且含不可逆动作）。

**模型列表按套餐探活**：当前凭证逐个发一次最小请求，只留真可用模型；`11158`（套餐不允许）、`11102`（无此模型）剔除；`429` / 5xx / 超时保留。结论按账号缓存 6 小时。

**同名模型不带渠道后缀**：两站共有模型在 `/v1/models` 一条、挂两渠道；`-x` / `-sg` 这类真不同的各占一条。

**节流**：`CODEBUDDY_INTL_CHAT_MIN_INTERVAL` 默认 5s，独立节流器，可热更。

### 按渠道出站代理（可选）

```
PROVIDER_PROXIES="codebuddy=http://127.0.0.1:7890;qoder=socks5://127.0.0.1:1080"
```

格式 `渠道=代理URL;渠道2=代理URL2`；协议 `http/https/socks5/socks5h`；留空 = 全直连。作用该渠道全部出站（聊天、额度、模型拉取、后台任务、OAuth）。**启动期项**，改后重启。未知渠道 / 非法协议 / 缺 `=` → 启动失败。`HTTP_PROXY` 等不采信（`trust_env=False`）。

> 容器内 `127.0.0.1` = 容器自身。代理跑宿主 → 写 `host.docker.internal`：
> `PROVIDER_PROXIES="codebuddy=http://host.docker.internal:7897"`
> compose 已声明 `extra_hosts`。解析只校验格式、不校验可达 → 写错不启动失败。

## 出口协议

### Responses API（Codex CLI）

`POST /v1/responses` 提供 Responses 子集；与 chat 共用选号 / 冷却 / 轮换 / 统计 / 会话粘性：

```bash
export CODING2API_KEY=sk-你的key
codex -c "model_providers.coding2api={ name='coding2api', base_url='http://127.0.0.1:8000/v1', wire_api='responses', env_key='CODING2API_KEY' }" \
      -c model_provider=coding2api -c model='glm-5.2' '你的任务'
```

支持文本、流式正文、思考摘要、函数工具调用、`finish_reason=length` → `response.incomplete`。`text.format` → chat `response_format`。part：`input_text` / `text` / `output_text` → text；`input_image` → `image_url`。**不支持** `store=true`、`previous_response_id`、`input_file`、Responses 私有工具 → 400。`include=["reasoning.encrypted_content"]` 收但忽略。

### chat `response_format` 收口

只放行 OpenAI 三种形状；畸形 / 未知 → 400：`{"type":"text"}`、`{"type":"json_object"}`、`{"type":"json_schema","json_schema":{name, schema, description?, strict?}}`（内部未知字段丢弃）。Responses `text.format` 与 chat 入站共用这段校验。

### Anthropic Messages API（Claude Code）

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN=sk-你的key     # 或 ANTHROPIC_API_KEY（走 x-api-key 头）
claude
```

- **鉴权**：`x-api-key` 与 `Authorization: Bearer` 都接受。
- **流式**：`message_start` → `content_block_*` → `message_delta` → `message_stop`；无 `[DONE]`。thinking 块在 `content_block_stop` 前补 `signature_delta`。
- **缓存命中**：出口 usage 拆两笔——`input_tokens` 只计未命中，命中另记 `cache_read_input_tokens`。
- **`count_tokens`**：本地估算，不转发上游。
- **图片**：`image` 块归一为 chat `image_url`；`tool_result.content` 内图片同样保留。
- **不支持**：`document` 块、`source.type=file`、Anthropic 服务端工具 → 400。
- **`cache_control` 透传**（默认开）：落点同 Anthropic——`system`、消息内容块、`tool_result` 顶层、`tool_calls` 顶层、`tools` 顶层。只校验结构，不解释取值。八渠道实测 6 接受（codebuddy / codebuddy_intl / trae / qoder / codearts / kilo）；zen 未验证、OfficeAce 未单测。脚本 `scripts/probe_cache_control.py`。

### CC Switch 一键导入

「API Key 管理」页生成 `ccswitch://` 深链（Claude Code / Codex CLI / OpenCode 各一按钮）。入口：建 Key 成功后对话框、Key 列表每行「导入 CC Switch」。深链含 Key 明文 → 仅主动打开时生成。需 CC Switch v4.0.10+。endpoint 随 `apiFormat`：Anthropic 到根（无 `/v1`）；OpenAI 到 `/v1`。无 Gemini 出口。

### 余额查询

`GET /v1/user/balance` 兼容 DeepSeek 形状，Bearer `sk-...` 鉴权，供 Cherry Studio / cc-switch 显示余额。数据来自额度探测缓存，按可用凭证汇总：

```json
{
  "is_available": true,
  "balance_infos": [{"currency": "credits", "total_balance": "60.00",
                     "granted_balance": "0.00", "topped_up_balance": "0.00"}],
  "balance_known": true,
  "providers": [{"provider": "codebuddy", "remaining": 50.0, "total": 150.0, "credentials": 2}]
}
```

从未探测成功 → `balance_known` = `false`（未知，不是 0）。

### 池健康检查

- `GET /health`：只回 `{"status":"ok"}`，适合存活探针。
- `GET /healthz`：额外给凭证池计数，`ready == 0` → 503（就绪探针）。

两端点都不鉴权，都接受 `HEAD`。四类计数互斥（`ready` / `cooling` / `paused` / `disabled`），合计 = `total`，与调度器同口径。**存活探针配 `/health`**；`/healthz` 用作就绪 / 告警。

> 升级注意：`/healthz` 从「恒 200」→「ready=0 时 503」。

### Prometheus 指标（`GET /metrics`）

默认开启、默认**不鉴权**；`METRICS_ENABLED=false`（可热更）→ 404。设 `METRICS_TOKEN` 后抓取须带 `Authorization: Bearer <token>`（不匹配 401）。

| 指标 | 类型 | 标签 | 数据源 |
| --- | --- | --- | --- |
| `coding2api_requests_total` | counter | `provider` / `model` / `status` | `usage_hourly` |
| `coding2api_tokens_total` | counter | `provider` / `type` | `usage_hourly` |
| `coding2api_credentials` | gauge | `state` | 凭证池 |
| `coding2api_upstream_errors_total` | counter | `provider` / `kind` | 进程内计数（重启归零） |
| `coding2api_task_failures_total` | counter | `task` | 进程内任务运行态（重启归零） |

不含 `credential_id` / `api_key_id` / `username`（高基数）。`requests_total` / `tokens_total` 读永久小时汇总，跨重启单调。汇总无 `error_type` 维度 → `kind` 由进程内计数器提供。

## API Key 的绑定、白名单、来源 IP 与到期时间

- **渠道绑定**：该 Key 只在该渠道凭证里选号；模型属另一渠道 → 400 并指出实际归属。留空 = 跨渠道自动。
- **来源 IP 白名单**：逗号分隔 IP / CIDR；写入时规范化（`10.0.0.1` → `10.0.0.1/32`）；名单外 → 403。默认不采信 `X-Forwarded-For`；仅 `TRUST_PROXY=true` 按 XFF 取最后一条。
- **模型白名单**：逗号分隔模型名或 fnmatch glob（如 `glm-*,kimi-k3`）；大小写不敏感；`模型@渠道` 后缀不参与匹配。名单外 400；`/v1/models` 也只列名单内模型。
- **到期时间**：epoch 秒，留空 = 永不过期；到期后 401（与「Key 不存在」同文案）。
- **配额**：六项独立，`0` / 留空 = 不限——`daily_requests` / `daily_tokens` / `daily_cost_usd` / `rpm` / `tpm`（按预估值）/ `max_concurrency`。当日类按北京时间自然日；速率 / 并发为进程内滑动窗口。超限 429 + `quota_exceeded`，`Retry-After` 指向可重试时刻。异常一律放行 + warning。
- **自助查询**：`GET /v1/usage?days=1|7|30` 返回自己的用量、剩余额度、当前速率占用（只读本地明细）。

## 凭证暂停

「暂停」= 凭证摘出对话 + 签到 / token 预刷新跳过；其余后台任务（额度探测、成长中心、活跃上报）照常。适用「不接聊天、积分继续领」。「取消暂停」立即放回池子。≠「已禁用」——后者是渠道判定会话失效后的系统禁用，需重登后「恢复」。同一开关：`POST /api/credentials/{id}/toggle`。

> Zen / Kilo 是虚拟占位行，**暂停是永久停用的唯一方式**（删除后重启补回）。暂停也会把该渠道从模型列表摘掉。

## 凭证批量运维

凭证页支持多选 + 批量操作条（探测额度 / 每日签到 / 暂停 / 取消暂停 / 恢复）。

- **端点**：`POST /api/credentials/batch`，body `{action: "probe"|"toggle"|"checkin"|"revive", ids: [...], enabled?}`。一次 ≤200 条，重复 id 去重。
- **批量导入**：`POST /api/credentials/batch-import`，body `{items: [{provider, credential, nickname?, tags?}]}`。一次 ≤100 条；逐条校验，坏条目只进 `failed`（带 `index` 与原因）；同渠道解密后内容完全一致视为重复，进 `skipped`。管理台凭证页「批量导入」支持粘贴 JSON 数组 / JSONL 或选文件。
- **逐条隔离**：单条失败不影响其余。响应 `{action, total, succeeded, failed, results: [{id, ok, error?, detail?}]}`。
- **签到失败是结果不是故障**：上游拒绝（账号被禁用 / 登录过期 / 限流）回 `ok=false` + 可读 `message`，不 500——否则管理台只弹「签到失败」，看不出真实原因。
- **暂停是显式状态而非取反**：`toggle` 的 `enabled` 由请求体给出 → 操作条拆「暂停」/「取消暂停」两按钮。
- **「恢复」≠「取消暂停」**：前者 `revive`（解系统硬禁用，需先重新登录）；后者 `toggle enabled=true`。
- 只收已有单条端点的动作：`refresh` / `delete` 不走此通道。
- **审计只记一条汇总**（`toggle` / `revive`）。审计写失败只记 warning，不影响操作结果。

## 凭证限额护栏

凭证级可选护栏，行内菜单「设置限额」。四项独立，全留空 = 不受限。判定在选号前完成；后台任务照常跑。

- **保留积分**：剩余 ≤ 该值 → 整号停用。余额未知不猜。
- **每日 token 上限** / **每日积分上限**：当日累计达到即整号停用。
- **按模型 token 上限**：`{模型: 上限}`，只停该模型。
- **当日 = 北京时间自然日**，0 点刷新。
- 候选全因限额跳过 → `no_healthy_credential` 503；模型级冷却 → `model_unavailable`。
- 写入 `PUT /api/credentials/{id}/limits`（整体替换；四个全关 → 删配置行）。即时生效。审计动作 `credential.limits`。

## 凭证在途上限

`MAX_INFLIGHT_PER_CREDENTIAL`（默认 0 = 不限）：同一凭证并发达到该值后不再参与选号。

- 计数进程内（单实例语义）。
- 租约在请求结束时释放，另有 600s TTL 兜底。
- 全部占满 → 与限额护栏同口径 503。
- 可热更。
- **在途计数兼作负载键**：插在「免费优先」之后、到期额度之前 → 并发自动摊到空闲号。

## 凭证池的筛选与分组

- **筛选工具条**：搜索（昵称 / 渠道 / id）× 渠道 × 状态 × 标签，四者 AND，纯前端。显示「筛选后 N / 总数」+「清空」。标签下拉仅在池里真出现过标签时渲染；标签不参与搜索词匹配。
- **标签**：逗号分隔，≤16 个、单个 ≤24 字符。仅筛选 / 展示，不参与选号。行内最多两个，多余收 `+N`。
- **组排序**：只重排渠道组，不动组内行序。默认额度降序，可切回固定渠道顺序。无额度接口的免费层垫底。
- **按渠道分组**：组头 = 紧凑仪表盘——图标 + 渠道名 + 账号数、模型（数量 + 最低倍率，仅收费模型；整组免费才写「全免」；自定义别名不算独立模型）、状态分布、额度合计、到期积分（主窗口 1.5 天一块；次窗口 7 天仅在更多时出）。默认全折叠；筛选生效展开；点组头折叠 / 展开。
- **列头跟展开区走**：列头在各展开组内部渲染并吸顶，各组共用一套排序状态。「成长中心」列仅 CodeBuddy 出现，其余渠道留空占位保列宽。
- **长列表可滚**：表格限高 70vh，吸顶列头与「操作」常驻。

额度列数字带虚线下划线，悬浮显示本行周期口径。

## 模型列表加载

`GET /v1/models` 与 Playground 下拉只合并**当前有可用凭证**的渠道（至少一条未暂停、未失效）；冷却中的仍算。顺序：CodeBuddy、TRAE、Qoder、CodeArts、OfficeAce 在前；组内按名字典序；多渠道模型归最高优先级渠道。

**请求路由只认这份缓存**：目录能证明归属 → 候选只有那些渠道；全不可用 → 503，不逐渠道试。模型完全不在目录 → 仍按全部候选路由；直连指定与 Key 渠道绑定不受影响。

### 模型目录落盘与启动即用

模型表除进程内 TTL 缓存（300s）外，每次拉取成功还写快照到 `DATA_DIR/model_catalog.json`，启动时同步读回 → 重启那刻归属表已就绪。快照损坏或超 7 天只丢缓存。另有 `MODEL_CATALOG_MINUTES` 任务兜底。

Playground 与 `/v1/models` 走 stale-while-revalidate：有缓存立刻回旧列表，TTL 到期后台刷新；某渠道零缓存才同步等。连续失败重试间隔按 2^(n-1)×TTL 退避（封顶 2 小时）；日志只在首次失败打 WARNING。

### 管理台「模型列表」页

（`GET /api/model-catalog`，管理员 / 操作员）只读展示 OpenRouter 公开目录：刊例价、上下文 / 输出上限、模态、能力、知识截止、Artificial Analysis 三项指数（非本服务实测，不排序）。每行带 `available`；页面有默认关闭的「只看当前渠道」开关。

**疑似下线视图**：`GET /api/models/availability`（operator）把最近 N 天（默认 7，可调 1–30）`upstream_blocked` / `model_not_found` / `invalid_request` 失败按 `(渠道, 模型)` 聚合。行内「加入黑名单」→ 追加 `model_blocklist`，走既有设置接口 + 审计。

**浮动别名取同族最新快照的分**：OpenRouter 对同一模型同挂浮动别名与带日期快照，用 `canonical_slug` 声明同属一次发布。本项目转发推理默认开，与 Max 同口径 → 按 `canonical_slug` 日期取同族最新快照分；显式带日期戳的 id 仍用自身分。家族只从带 `-YYYYMMDD` 的 canonical 派生。

### 主动探活与人工接管

主动探活面板（`GET /api/models/probes`，operator）探两类 (渠道, 模型)：**仍被 `blocked` 负缓存锁住**的，以及**已判死**的（`state='failed'`）。

三态判定：

- **成功** → 立即清该渠道 `blocked` 负缓存 + 该模型回到列表（唯一恢复入口）。
- **`failed`**（上游明确 `BLOCKED` / `INVALID`）→ 留负缓存 + 累加连续失败。
- **`inconclusive`**（额度、限流、网络、超时、空流、任何异常）→ 绝不禁用，连续失败归零。

探活与生产完全隔离：直连 `provider.stream_chat`；不写 `usage_events`、不动冷却表、不改健康度。结论落独立 `model_probes` 表。面板不列命中 `MODEL_BLOCKLIST` 的模型。

**判死的模型同时从模型列表摘掉**：只抽该 (渠道, 模型)，其它渠道正常时同一模型照常展示。**隐藏没有到期时间**——恢复只靠复探成功，不再挂在负缓存的 `cooling_until` 上：负缓存过期后没人再探，等于永久隐藏。后台周期确实会把判死行一起探，但周期 `MODEL_PROBE_MINUTES` 默认 `0`（关闭），默认部署下唯一的恢复入口是面板上的「立即探活」。

**人工接管**（`POST /api/models/probes/manual`）：接管后自动探活跳过该行，但不改负缓存。要立刻恢复 → 点「立即探活」。

### 管理台「上游状态」页

（`GET /api/upstream-status`，管理员 / 操作员）聚合各提供方官方状态页，回答「这报错是上游挂了，还是我这边凭证 / 额度问题」。

- **来源**：OpenCode Zen（逐模型 TTFT / tps）、Kilo、Moonshot / Kimi、MiniMax、OpenAI、Anthropic、Together AI、DeepSeek。按 `scope` 两组：`channel` 先，`model` 后。
- **归一化六档**：`operational` / `degraded` / `partial` / `major` / `maintenance` / `unknown`。整体取最差源，`unknown` 殿后。
- **只解释、不决策**，不参与选号 / 熔断。
- **只存内存、不落盘**；抓取由后台任务（`UPSTREAM_STATUS_MINUTES`，默认 5 分钟）负责，单源失败只标该行。
- **先结论后明细**：顶部概况条 → 按 scope 两组，异常源默认展开；「需要关注」条提前非正常源。组件明细超 6 项截断（异常项排前）。
- **不覆盖**纯 HTML 源（Groq、腾讯云、阿里云、华为云、SiliconFlow、Qoder、智谱、TRAE）。

### token 到期展示

凭证列表 **token 剩余**列显示 access token 距到期还有多久；低于 `TOKEN_EXPIRY_WARNING_SECONDS`（默认 1 小时）标红。优先 `expires_at`，缺失回落 JWT `exp`；都取不到 → `—`，不猜本地 TTL。

## 侧栏数据提醒（导航角标）

侧栏每页都在。不点进去发现不了的待办态 → 计数角标，由单个聚合端点 `GET /api/nav-badges` 返回，侧栏 60s 轮询。

| 导航项 | 门槛 | 角标口径 |
|---|---|---|
| 凭证管理 | operator+ | `disabled` 或 `health < 0` 的凭证数 |
| 运维告警 | admin | 仍在告警中的条数 |
| 任务与配置 | admin | 上次执行失败的任务数 |
| 上游状态 | operator+ | 异常源数（含维护中） |
| 用户管理 | admin | 有激活令牌的账号数 |
| API Key | 全员（仅本人） | 已过期或 7 天内到期的 Key 数 |
| 模型列表 | operator+ | 快照陈旧（布尔文字） |

- **按角色裁剪**：后端只下发有权看的键；缺失 = 无权看（不是 0）。凭证角标不下发 viewer。
- **只数「待办」**：告警页 `total` 含已恢复历史，角标只数未恢复；任务用「上次失败」，非「连续失败 ≥ 阈值」。
- **凭证角标 ≠ `/healthz` 可用数**：后者不看 health → 额度耗尽的号仍算 `ready`。
- **告警角标自愈**：`AlertTask` 启动时从库里未恢复行重建集合。
- **目录陈旧**：从未拉取不算；超 2 倍 `OPENROUTER_CATALOG_MINUTES` 未刷新才算。

## 统计

### 口径

默认**最近 24 小时**（另有 7 / 30 天、全部、自定义）。自定义 = 两个 `datetime-local`（浏览器时区），可只填一边；起点不早于终点 → 整窗作废 + 提示。端点全收 `until`（epoch 秒，半开 `[since, until)`）。

**失败类型** = 受控枚举（`CONTROLLED_ERROR_TYPES`）：模型级冷却耗尽 → `model_unavailable`；池真耗尽 → `no_healthy_credential`；上游明确说「没这个模型」→ `model_not_found`（与「请求无效」分开：前者是模型下线，后者是请求写错）。对外码相同，仅统计口径不同。

**请求 ID**：响应带 `X-Request-ID`。入站合法沿用，否则自生成；字符集 `[A-Za-z0-9._:-]`、长 1–128。落 `usage_events.request_id`，明细时间列悬停可见。**不透传上游**——各渠道出站头语义不同。

**Credit 消耗**：CodeBuddy = 上游真值；TRAE 按官方单价推算并标 `≈`（表 `src/provider/trae/pricing.py`；未收录不推算）。历史用 `scripts/backfill_trae_credit.py` 补。

**成本（估算，≈）**：token × 公开刊例价，非上游真实扣费。单价取 OpenRouter 目录 → `(输入−命中)×输入价 + 命中×缓存价 + 输出×输出价` 折 USD → 按写入时 `USD_CNY_RATE` 折 CNY。

- **写入时定值**：写明细时算好落库；历史行不随价表 / 汇率重算。上线前两列 `NULL` → `scripts/backfill_cost.py --apply` 补。
- **匹配不到就不计**：本地名按 OpenRouter `id` / 展示名等值匹配（不唯一不配）→ 聚合 `null` 显示 `—` → 成本是下限。渠道分发代号按显式等价规则归到基础模型（只登记实测 id；不做通用剥日期 / 剥区域尾缀）。

**缓存命中率**：命中 token ÷ 输入 token（1 位小数）。未上报或输入 0 → `—`。分组表 / 明细表把命中率与 token 并一格。

**输出速度（tok/s）** = `token 总量 ÷ 生成时间总和`，非逐条比值再平均。两口径同分母、异分子：**正文速度** `output_tokens - reasoning_tokens`；**总产出速度**含思考 token；前端一格 `正文 / 总产出`。仅算经本服务路由、产出 ≥ 1 token 的成功请求。生成时间 = `latency_ms - ttfb_ms`；剔除窗口 < 200 ms 或单条 > 1000 tok/s。`ttfb_ms` = `NULL` 不进主口径；无样本 → 估算口径（端到端为分母，前置 `≈`）；两口径皆无 → `—`。

**图表指标**：请求次数 / Token / 成本 / 单请求成本 / 耗时 / 首字延迟 / 成功率 / 缓存命中率，切换看趋势。派生指标（成功率、缓存命中率、单请求成本）按**聚合后相除**算（`sum ÷ sum`），不是逐小时比率的平均；无样本给 `—`（断线），不画成 0。耗时 / 首字另配一张**分位图**（P50 中位数 + P95 上界，读明细表，仅 90 天）——均值会把长尾摊平。时间轴按小时补齐（空档保留位置，不压缩时间）。`metric` 参数取值域外显式 400（不静默回落到「请求次数」——否则拼错参数会拿到一份看起来正常的图）。自定义时间范围非法（起点 ≥ 终点）时**暂停取数**，不按「不限时间窗」发全量查询。

聚合速度只能从逐请求明细算 → 仅 90 天（总览卡片同；其余读小时汇总）。明细表给每条请求自身速度，不回落估算；无生成窗口 → `—`。速度列可点表头排序（`sort=tps_visible` / `sort=tps`）。

### 积分记录

额度数字后下箭头展开「变动流水」= **两次额度探测之间的净变化**，非动作归因：签到、成长、对话都改余额而上游不打日志 → 界面一律写「净变化」。首次探测只建基线；余额没变不记；变「未知」仍记但不填变化量；留 90 天。

**按账本分流**：流水带 `ledger`（`pool` / `credits`）。CodeArts 记 `credits`，其余 `pool`。接口 `GET /api/credentials/{id}/credit-events?ledger=`（缺省 `pool`）。两本账量级 / 语义不同 → 分开读。

### 分组维度与重新汇总

tabs 切四维（渠道 / 模型 / 用户 / 凭证），共用一表（渠道 / 请求 / Credit / 成本 / Token / 速度 / 平均延迟 / P95 延迟）。端点 `GET /api/stats/by-provider|by-model|by-user|by-credential`。默认请求数降序；点列头切排序；合并列弹菜单选子列。**延迟分位 P50 / P90 / P95 / P99**（默认 P95，nearest-rank，n = 有值样本）。用户 / 凭证维度仅 admin / operator。

**口径差异**：渠道 / 模型 / 用户读小时汇总（永久；近 5 分钟未计入）；凭证读明细表（仅 90 天）。

**延迟两类口径**：只统计成功请求。平均 = `latency_sum ÷ 成功数`（小时汇总）；分位从逐请求明细算（仅 90 天）。`ttfb_ms` = `NULL` 的两列各自独立排名。无样本 → `—`。

**失败归因**：`GET /api/stats/errors`（operator+）按 `(渠道, 模型, 错误类型)` 聚合窗口内 `ok=0` → 失败次数 / **未救回数** / 最近失败时刻（降序，`limit` ≤ 5000）。**失败数 = 尝试口径**：换号重试每次上游拒绝各落一行；同用户同 `request_id` 另有成功行 = 已救回。`未救回 = 失败数 − 救回数`（同请求多次失败算多次，≠ 受损请求数）。凭证 401 硬禁用（DEAD）也落 `credential_unavailable`；账号被上游风控限制（CodeBuddy 11140）落 `account_restricted`，账号级 2h 冷却、当前请求不再换号，同渠道窗口内**第二个不同凭证**也中招时整渠道退避 24h；上游明确说该号用不了这个模型（11102 无此模型 / 11158 套餐不允许）落 `model_not_found`，写 (账号, 模型) 负缓存而非冷却账号；一次都没选中凭证就耗尽时（候选全在冷却 / 池子为空）明细落**候选渠道**而不是 `-`；换号成功算已救回；轮换耗尽不重复落收尾行。点行钻取 +「只看失败」（`GET /api/stats/events?ok=0`）。仅 admin / operator。`/metrics` 的 `upstream_errors_total` 取进程内计数，不合并。

**点行钻取**：点分组表任一行 → 明细按该行过滤（渠道 / 模型 / 凭证；用户维度走作用域参数），条件可叠加。`GET /api/stats/events` 接受 `provider` / `model` / `credential_id`（AND）+ `ok=0`。

**明细表列合并**：Token、延迟、速度、渠道 / 凭证各并一列；默认值可读，悬停展开子项。时间列拆两行（时刻 / 日期）。点合并列头弹下拉选子列排序（`sort=cache_rate` / `tps_visible` / `tps` / `provider` / `credential`）。

**导出 CSV**：`GET /api/stats/events?format=csv`（仅 admin / operator），按当前范围 + 钻取过滤导出。UTF-8 BOM、`\r\n`、时间 ISO 8601（UTC）；`= + - @` 开头的单元格加前导单引号防公式注入。单次上限 10000 行；超出截断 + `X-Export-Truncated: 1`。

**实时流（SSE）**：`GET /api/stats/events/stream` 展示当前筛选范围内刚落库的请求。`StatsCollector.record` 提交后由进程内 `LiveEventHub` 广播；前端留最近 50 条。

- 作用域 / 过滤同 `/api/stats/events`；换过滤重建连接。
- 广播 payload 不含凭证昵称；仅 admin / operator 连接由服务端按需补 `credential_name`。
- 每订阅者有界缓冲 200 条（满则丢最旧）；并发上限 8，超出 429。
- 15 秒注释帧保活；`Cache-Control: no-store` + `X-Accel-Buffering: no`。

历史数据偏小 → `python3 scripts/rollup_hourly.py --apply` 重算（默认预览、写库前备份、幂等）。

### 列表排序

列头可点排序：首次用自然方向（数值降序、文本升序），再点翻转。排序后端完成；端点接受 `sort`（白名单键）与 `order`（`asc` / `desc`）。未知 `sort` / 非法 `order` → 回落默认，不报错。

适用：`/api/credentials`、`/api/users`、`/api/api-keys`、`/api/audit`、`/api/alerts`、`/api/model-catalog`、`/api/credentials/{id}/credit-events`、`/api/credentials/{id}/accounts`、`/api/stats/by-*`、`/api/stats/events`。

### 列表分页

统一分页：`第 N / M 页`、每页 10 / 20 / 50 / 100（默认 50）。扁平列表后端分页——端点接受 `limit` + `offset`，返回筛选后 `total`。`/api/model-catalog` 排序键含派生字段 → 内存排序再切片；另接受 `q` 与 `only_available`。

分组类表（凭证页按渠道分组、用量统计分组表）不分页。

## 后台任务

12 类任务由 `TaskRunner` 调度，失败互不影响：额度探测、token 预刷新、签到、成长中心、活跃上报、明细清理、DB 备份、渠道模型列表刷新、模型目录刷新、运维告警、上游状态聚合、模型主动探活。「任务与配置」页看上次执行 / 结果 / 错误（30s 刷新）；运行态只在本次进程。

每卡片带「立即运行」（admin + CSRF）：绕过时点窗口，绕不过开关；已在跑 → `busy=true` 不排队。

**渠道模型列表刷新**（`MODEL_CATALOG_MINUTES`，默认 30）兜底保鲜，无人访问也更新。

**成长中心**仅 CodeBuddy。周期默认 60 分钟（`GROWTH_INTERVAL_MINUTES`）。

### 活跃上报（可选，默认关闭）

`ACTIVITY_REPORT_ENABLED=true` → 每天 `ACTIVITY_REPORT_HOUR`（默认 10 点）整点窗口为每账号补发一条 `chat_request_send` 续连登。与积分、调度无关。适用 CodeBuddy 与国际站。

> **风险**：官方条款禁止脚本篡改活动数据；处罚 = 取消资格 + 追回礼品。默认关闭，开启前自行评估。上游改版即失效。缺 `userId` → 上游静默丢弃；OAuth `user_id` 为空 → 回落 JWT `sub`，仍缺则跳过。

### 运维告警

后台周期评估七类风险 → `alert_events`（「运维告警」页，admin-only）：

| 规则 | 触发条件 |
|---|---|
| `pool_empty` | 可用数少于 `ALERT_POOL_READY_MIN` |
| `task_failed` | 某任务连续失败达到 `ALERT_TASK_FAILURES` 次 |
| `token_expiring` | token 剩余寿命落在 `ALERT_TOKEN_EXPIRY_HOURS` 内 |
| `error_rate` | 统计窗内失败占比达到 `ALERT_ERROR_RATE_THRESHOLD` 且样本足够 |
| `key_burst` | 单 Key 窗口内 token 用量达到 `ALERT_KEY_BURST_TOKENS`（滥用 / 泄露信号，不是错误） |
| `off_hours` | 工作时间 `[ALERT_OFF_HOURS_START, ALERT_OFF_HOURS_END)` 之外请求数达到 `ALERT_OFF_HOURS_MIN_REQUESTS` |
| `budget` | 当月成本达 `ALERT_BUDGET_CNY` 的某一 `ALERT_BUDGET_LEVELS` 档，只报最高档；100% 为 critical |

- **静默窗**：同一 `(规则, 对象)` 窗口内只落库 / 推送一次。
- **迟滞**：连续命中 N 轮才告警；连续未命中 M 轮才确认恢复。恢复时给该 `(规则, 对象)` 全部未恢复行写 `resolved_at`，并外推 `status="resolved"` webhook。
- **多出口**：通用 Webhook（完整 JSON + 签名）+ 飞书机器人（文本消息）+ Slack Incoming Webhook 可同时配置；任一渠道失败不影响其它渠道发送，但整体记为未投递成功。
- **重启自愈**：迟滞计数进程内维护；「哪些未恢复」从库里重建。
- **人工处置**：「标记恢复」（admin + CSRF + 审计）只标该行。
- **保留期**：同请求明细（默认 90 天）。

### 数据库备份与恢复

**自动备份**（`BACKUP_INTERVAL_MINUTES`，默认每日）把一致性快照写进 `DATA_DIR/backups/`，只留最近 `BACKUP_KEEP` 份（默认 7）。

- 用 `VACUUM INTO`，不复制文件（WAL 下直接 `cp` 丢未 checkpoint 事务）。
- 落盘即 `PRAGMA integrity_check`，不过 → 删半成品。产物 `0600`、目录 `0700`。
- 失败只记日志，不影响服务。

手动备份：

```bash
python3 scripts/backup_db.py                 # 备份到 DATA_DIR/backups/，保留 7 份
python3 scripts/backup_db.py --keep 14       # 保留 14 份（0 = 不清理）
python3 scripts/backup_db.py --out /path/dir
```

恢复（**先停服**）：

```bash
python3 scripts/restore_db.py data/backups/coding2api-20261014-030000.sqlite3            # 预览：只校验
python3 scripts/restore_db.py data/backups/coding2api-20261014-030000.sqlite3 --apply    # 校验 → 安全副本 → 原子替换
```

恢复流程：校验完整性 → 现有主库 `.pre-restore-<时间戳>` 副本 → 原子替换 → 删 `-wal`/`-shm` 残留。**恢复后重启服务。**

管理台「备份与恢复」页（admin）提供同一能力的界面：

- `GET /api/backups`：列出 `backups/*.sqlite3` + 当前待恢复文件（`pending`）。
- `POST /api/backups`：立即备份一次。
- `GET /api/backups/{name}` / `DELETE /api/backups/{name}`：下载 / 删除（只认目录内普通文件，拒绝路径与符号链接）。
- `POST /api/backups/restore?filename=…`：原始字节上传 → 完整性 + 核心表 + schema 版本校验 → 暂存 `DATA_DIR/restore/pending.sqlite3`。**运行中不换库**，重启启动时在建连接之前原子替换；待恢复文件校验不过会留档 `.rejected-*` 并继续用当前库，不会把服务钉死在启动失败。

## 配置

完整项见 `src/config.py`（权威），每项都透传 `docker-compose.yml`。容器内改监听地址用 `HOST`/`PORT`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | **必填** | 凭证加密密钥，≥16 字符；丢失 = 凭证全废。拒低熵值与已知占位值（`.env.example` 里那些）。**换密钥后旧凭证解不开**：先用 `scripts/rotate_app_secret.py` 重加密（停机 + 备份），否则单条探测回可行动的错误码、对话 / 后台任务只跳过坏凭证 |
| `ADMIN_USERNAMES` | 空 | 仅引导期（库里还没有活跃 admin）：逗号分隔，首次启动提权 `admin` |
| `USERS_FILE` | `secrets/users.txt` | 仅引导期：老式用户文件，启动导入一次 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 浏览器可达地址；TRAE 登录回调用；https 下发 HSTS。不决定会话 Cookie 的 `Secure`。配回环 / `0.0.0.0` → 回调基址取当前请求 origin（见「部署注意」） |
| `DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `DEFAULT_MODEL` | `deepseek-v4.1-flash` | 模型为空 / `auto` 时默认 |
| `QUOTA_PROBE_MINUTES` | `60` | 额度探测周期（下限 1） |
| `REFRESH_INTERVAL_MINUTES` | `30` | token 预刷新轮询周期（下限 5） |
| `MODEL_CATALOG_MINUTES` | `30` | 渠道模型列表兜底刷新周期（下限 5） |
| `OPENROUTER_CATALOG_MINUTES` | `1440` | 模型目录刷新周期（下限 60） |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | 目录来源（匿名可读）；改后重启 |
| `TOOL_OUTPUT_COMPRESS_ENABLED` | `false` | 压缩 `role=tool` 的超长纯文本（去 ANSI + 折叠重复行）；可热更 |
| `TOOL_OUTPUT_COMPRESS_MAX_CHARS` | `8000` | 触发压缩的字符阈值，≤0 视为关闭；可热更 |
| `USD_CNY_RATE` | `6.70` | 成本估算汇率；可热更，只影响之后写入 |
| `GROWTH_INTERVAL_MINUTES` | `60` | 成长中心周期（下限 5） |
| `GROWTH_IRREVERSIBLE_ACTIONS` | `true` | 允许抽奖 / 连登兑换 / 开盲盒 / 消耗补登卡 |
| `ACTIVITY_REPORT_ENABLED` | `false` | 活跃上报，默认关闭 |
| `ACTIVITY_REPORT_HOUR` | `10` | 活跃上报本地整点窗口 |
| `CHECKIN_TIMES` | `""` | 签到时点 `HH:MM` 逗号分隔；留空 = 全天每 10 分钟查。可热更 |
| `CHECKIN_JITTER_MINUTES` | `0` | 签到抖动（分钟），时点前随机提前。可热更 |
| `LOG_BUFFER_LINES` | `500` | 服务端日志页缓冲行数；`0` 关该页。可热更 |
| `ALERT_ENABLED` | `true` | 运维告警开关 |
| `ALERT_WEBHOOK_URL` | 空 | 通用 Webhook，逗号分隔（≤16，仅 http(s)，拒内网 / 回环防 SSRF），带 `X-Coding2API-Signature` |
| `ALERT_FEISHU_WEBHOOK_URL` | 空 | 飞书群机器人地址；按 `msg_type=text` 发送，URL 校验同通用 Webhook |
| `ALERT_SLACK_WEBHOOK_URL` | 空 | Slack Incoming Webhook 地址；按 `{text}` 发送，URL 校验同通用 Webhook |
| `ALERT_INTERVAL_MINUTES` | `5` | 告警评估周期 |
| `ALERT_SILENCE_MINUTES` | `30` | 同 `(规则, 对象)` 静默窗；`0` 关闭 |
| `ALERT_FOR_TICKS` | `1` | 进入迟滞轮数 |
| `ALERT_CLEAR_TICKS` | `2` | 恢复迟滞轮数 |
| `ALERT_POOL_READY_MIN` | `1` | 池耗尽阈值；`0` 关闭 |
| `ALERT_TASK_FAILURES` | `3` | 任务连续失败次数阈值；`0` 关闭 |
| `ALERT_TOKEN_EXPIRY_HOURS` | `24` | token 剩余小时阈值；`0` 关闭 |
| `ALERT_ERROR_RATE_THRESHOLD` | `0.5` | 错误率阈值；`0` 关闭 |
| `ALERT_ERROR_RATE_MIN_REQUESTS` | `20` | 错误率最小样本 |
| `ALERT_ERROR_RATE_WINDOW_MINUTES` | `15` | 错误率统计窗 |
| `ALERT_KEY_BURST_TOKENS` | `0` | 单 Key 用量暴涨阈值（token）；`0` 关闭 |
| `ALERT_KEY_BURST_WINDOW_MINUTES` | `15` | 单 Key 用量统计窗；`0` 关闭 |
| `ALERT_OFF_HOURS_START` / `_END` | `0` / `0` | 工作时间 `[start, end)`（本地时区，可跨零点）；相等 = 关闭 |
| `ALERT_OFF_HOURS_MIN_REQUESTS` | `0` | 非工作时段请求数阈值；`0` 关闭 |
| `ALERT_BUDGET_CNY` | `0` | 当月成本预算（元）；`0` 关闭。仅计可定价明细 |
| `ALERT_BUDGET_LEVELS` | `50,80,95,100` | 预算分层档位；只报当前最高档，100% 为 critical |
| `BACKUP_INTERVAL_MINUTES` | `1440` | 备份周期（下限 60） |
| `BACKUP_KEEP` | `7` | 备份保留份数；`0` = 不清理 |
| `UPSTREAM_STATUS_ENABLED` | `true` | 上游状态页开关 |
| `UPSTREAM_STATUS_MINUTES` | `5` | 状态抓取周期（下限 1） |
| `UPSTREAM_STATUS_TIMEOUT_SECONDS` | `10.0` | 单源 HTTP 超时 |
| `MODEL_PROBE_MINUTES` | `0` | 主动探活周期（`0` = 关）。探仍被 `blocked` 负缓存锁住与已判死的 (渠道, 模型)；成功即恢复展示并清负缓存，其余绝不禁用。可热更 |
| `QUOTA_EXPIRY_WINDOW_SECONDS` | `129600` | 主到期排序窗口；`≤0` 关闭 |
| `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` | `604800` | 次到期窗口；`≤0` 关闭 |
| `CONVERSATION_STICKY_SECONDS` | `3600` | 会话粘性 TTL。优先级：请求体会话标识 > 请求头 > 消息前缀指纹；pin 最高。`≤0` 关 |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,default,fast,balanced,deep,primary,auto,sagitta,aquila,glm-4.*,glm-5,glm-5-turbo,glm-5.0*,glm-5.1,glm-5v-*,glm-5.2*,glm.5.2*,kimi-k2.*,kimi-k2.5,kimi-k2.6,kimi-k2-thinking,kimi-k3,minimax-m2.5,minimax-m2.7,minimax-m3,deepseek-v3-*,deepseek-v4-*,qwen3.7-*,qwen3-*,qwen3.6*,qwen3-vl-235b,gpt-5.4,gpt-5.5,gpt-5.6*,hy3*,hy4-preview,hunyuan-*,seed-2.1*,seed-2-1-turbo,doubao-seed-2.1-turbo,seed-code-pro-*,seed-evolving,space-bunny,nemotron-3-*,nemotron-3*,lfm2*,laguna*,north-mini-code,inkling-small,xing4.0-29b-a4,glyph-cluster,openrouter-models-router,deepseek-v4.1-flash-0930,deepseek-v4.1-flash-bnt,glm-5.3-test,glm-5.3-zx,kimi-k3-test-1009,kimi-k3-1019` | 列表黑名单（fnmatch，仅影响展示），按归一后写法匹配 |
| `MODEL_FALLBACK_GROUPS` | `""` | 跨渠道 fallback 组：`组名=成员1,成员2;…`。留空不启用 |
| `ALLOWED_HOSTS` | 空 | Host 白名单，防 DNS rebinding |
| `ALLOW_TEST_HOST` | `false` | 放行 `testserver`，仅测试用 |
| `TRUST_PROXY` | `false` | 是否采信 `X-Forwarded-For`；仅「前恰好一层受信反代」时开 |
| `ENABLE_DOCS` | `false` | 暴露 `/docs` 与 `/openapi.json` |
| `DUMP_REQUEST_BODIES` | `false` | `/v1` 原始请求体落盘（含对话，仅排查）。**可热更**（管理台「可观测性」），目录 0700 / 文件 0600 / 保留 200 份且不超 24h；改动进审计 `settings.update` |
| `GATEWAY_PAUSED` | `false` | 全站暂停（kill switch）：所有 API Key 鉴权的 `/v1` 端点返 503 （`code=service_paused`），管理台与登录照常可用。**可热更**，一键止血；改动进审计并单独记 WARNING |
| `GATEWAY_PAUSE_REASON` | 空 | 暂停原因，只进日志与审计（不回显给调用方）；留空按「未填原因」记 |
| `LOG_LEVEL` | `INFO` | 日志级别；审计日志是 INFO，调到 WARNING 一并关 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址与端口（compose 默认 `0.0.0.0`）；`PORT` 同时决定宿主映射 |
| `CODEBUDDY_API_ENDPOINT` | `https://copilot.tencent.com` | 上游地址；改动须同步白名单 |
| `CODEBUDDY_ALLOWED_ENDPOINTS` | 官方两站（见 compose） | 端点白名单；真实 Token 只发名单内 |
| `CODEBUDDY_INTL_API_ENDPOINT` | `https://www.codebuddy.ai` | 国际站地址 |
| `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` | `https://www.codebuddy.ai` | 国际站白名单，刻意不含国内站 |
| `ZEN_API_ENDPOINT` | `https://opencode.ai` | Zen 上游地址 |
| `ZEN_ALLOWED_ENDPOINTS` | `https://opencode.ai` | Zen 白名单 |
| `ZEN_OPENCODE_VERSION` | `1.18.0` | 门禁伪装 UA 版本（低于阈值 → 426） |
| `ZEN_API_ROOT` | `/zen/v1` | 上游 API 根；可切 `/inference/v1`（两路径当前都返回 200） |
| `ZEN_GATE_TOOLS` | `bash,read` | 门禁 tools 集合；上游收紧时可切 `bash,glob,grep,read` |
| `ZEN_USER_AGENT` | 空 | UA 模板（`{version}` 代入版本号）；空 = `opencode/<version>` |
| `KILO_API_ENDPOINT` | `https://api.kilo.ai/api/gateway` | Kilo 上游地址 |
| `KILO_ALLOWED_ENDPOINTS` | `https://api.kilo.ai/api/gateway` | Kilo 白名单 |
| `QODER_API_ENDPOINT` | `https://openapi.qoder.com.cn` | Qoder openapi（额度 / 登录）；国际站 `https://openapi.qoder.sh` |
| `QODER_GATEWAY_ENDPOINT` | `https://gateway.qoder.com.cn` | Qoder 推理网关；国际站 `https://api1.qoder.sh` |
| `QODER_ALLOWED_ENDPOINTS` | 国内 openapi+gateway + 国际站（见 compose） | Qoder 白名单 |
| `CODEARTS_API_ENDPOINT` | `https://snap-access.cn-north-4.myhuaweicloud.com` | CodeArts snap 引擎 |
| `CODEARTS_ALLOWED_ENDPOINTS` | snap 引擎 + STS + 门户（见 compose） | CodeArts 白名单 |
| `OFFICEACE_API_ENDPOINT` | `https://agentarts.cn-southwest-2.myhuaweicloud.com` | OfficeAce 控制面 |
| `OFFICEACE_ALLOWED_ENDPOINTS` | `https://agentarts.cn-southwest-2.myhuaweicloud.com` | 只需控制面（模型网关由上游下发） |
| `OFFICEACE_CHAT_MIN_INTERVAL` | `0` | OfficeAce 聊天最小间隔 |
| `PROVIDER_PROXIES` | `""` | 按渠道出站代理；启动期项 |
| `CREDENTIAL_PROXY_POOL` | `""` | 出口 IP 粘性：全局代理池（分号分隔），按凭证 id 稳定哈希分配；启动期项 |
| `CODEBUDDY_CHAT_MIN_INTERVAL` | `5` | CB 聊天节流（按凭证分桶） |
| `CODEBUDDY_INTL_CHAT_MIN_INTERVAL` | `5` | CB 国际节流，独立 |
| `ZEN_CHAT_MIN_INTERVAL` | `0` | Zen 节流 |
| `KILO_CHAT_MIN_INTERVAL` | `0` | Kilo 节流 |
| `QODER_CHAT_MIN_INTERVAL` | `5` | Qoder 节流 |
| `CODEARTS_CHAT_MIN_INTERVAL` | `5` | CodeArts 节流 |
| `CODEARTS_MAX_CONCURRENCY` | `3` | CodeArts 每账号在途并发上限；`0` 关闭 |
| `CODEARTS_REQUEST_WINDOW_SECONDS` | `60` | CodeArts 账号滑动窗口；`0` 关闭 |
| `CODEBUDDY_SANITIZE_CHANNEL_MARKERS` | `true` | 出站正文命中「伪装官方客户端」指纹 → 替换（上游 11128 风控） |
| `REFRESH_SKEW_HOURS` | `24` | token 到期前该窗口预刷新；短寿命渠道自封顶 |
| `TOKEN_EXPIRY_WARNING_SECONDS` | `3600` | 到期预警阈值；`≤0` 关。纯展示 |
| `PACER_MIN_SECONDS` / `PACER_MAX_SECONDS` | `5` / `20` | 全局节流随机等待区间 |
| `AUTO_CONTINUE_MAX` | `10` | `finish_reason=length` 时同凭证自动续写次数；`0` 关闭 |
| `CONTEXT_COMPRESS_ENABLED` | `true` | 按模型输入上限裁剪过长对话 |
| `CONTEXT_COMPRESS_RESERVE_TOKENS` | `4096` | 为回复预留的输出 token |
| `CONTEXT_COMPRESS_MIN_KEEP_MESSAGES` | `4` | 无论多长都保留的最近消息条数 |
| `CONTEXT_COMPRESS_SAFETY_RATIO` | `0.95` | 压缩预算占模型上限比例 |
| `CONTEXT_COMPRESS_STICKY_ENABLED` | `true` | 粘性裁剪：一次裁到预算 60% 并锁定窗口起点，跨轮前缀不变以命中上游缓存 |
| `PREFIX_DEBUG_ENABLED` | `false` | 前缀缓存诊断：每请求一行载荷指纹（只记摘要），定位命中率异常的分叉点。可热更 |
| `UPSTREAM_COMPLETE_TIMEOUT_SECONDS` | `600` | 非流式聚合整体超时；`≤0` 关闭 |
| `STREAM_FIRST_EVENT_TIMEOUT_SECONDS` | `0` | 首事件超时（出帧前可换号）。**默认必须保持关闭**，推荐 60。可热更 |
| `STREAM_IDLE_TIMEOUT_SECONDS` | `0` | 流内空闲超时（已出帧只断流不换号），推荐 300。可热更 |
| `SHORT_COOLDOWN_WAIT_SECONDS` | `0` | 全池短冷却时等待续上；`0` = 快速失败。可热更 |
| `NONSTREAM_KEEPALIVE_SECONDS` | `0` | 非流式出口 N 秒未完成 → 转 chunked 吐空白心跳（JSON 前导空白合法）；`0` = 关，仅 CDN 后需要，推荐 15。可热更 |
| `MODEL_ALIASES` | `""` | 自定义别名；留空不启用。可热更 |
| `MAX_INFLIGHT_PER_CREDENTIAL` | `0` | 单账号在途上限；`0` = 不限。600s TTL 兜底 |
| `METRICS_ENABLED` | `true` | 暴露 `/metrics`。可热更 |
| `METRICS_TOKEN` | 空 | 设置后 `/metrics` 要求 `Authorization: Bearer <token>`；空 = 公开（与 `/healthz` 同口径）。启动期项 |
| `REQUEST_LOG_ENABLED` | `true` | 请求级结构化日志（logfmt）；不写 query / 请求体 / 用户名。可热更 |
| `FORCE_DISABLE_THINKING` | `false` | 出站剥离思考内容；只过滤响应，不改请求。可热更 |
| `REASONING_EFFORT_MAP` | `""` | 出站档位映射 `渠道=from:to;*=from:to`（渠道取 `KNOWN_PROVIDERS`）；`none` = 删键。可热更 |
| `ANTHROPIC_THINKING_EFFORTS` | `""` | Anthropic `thinking.budget_tokens` → 上游 `reasoning_effort`。可热更 |

### 管理台热更（「任务与配置」页）

上表只给常用项标了「可热更」；管理台共 **77 项**可不改 `.env`、不重启，直接在「任务与配置」页改。

**优先级 `DB 覆盖值 > .env`**：覆盖后 .env 失效，页面标「DB 覆盖」；点「恢复默认」删覆盖行才回落 .env。值存 `runtime_settings` → 新增项免迁移。白名单外 key、非法类型 / 越界值写入前即拒。启动期项不在白名单：`APP_SECRET` / `PORT` / `DATA_DIR` / `USERS_FILE` / `OPENROUTER_MODELS_URL` / 上游端点白名单。`GET /api/settings` 读快照；`PUT /api/settings` 写（admin + CSRF），`null` 恢复默认。同页展示任务态（`GET /api/tasks`，30s 刷新）。

## 部署注意

- **挂载属主**：容器内 uid 1001（`appuser`）→ `./data` 与 `./secrets` 必须可写 / 可读：

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **时区**：镜像默认 `TZ=Asia/Shanghai`；`.env` 的 `TZ` 透传。签到 / 额度周期按「今天 9 点」判断 → 必须与宿主一致。
- **TRAE 回调地址**：默认 `PUBLIC_BASE_URL=http://127.0.0.1:8000` 只在本机可用。远程 / 容器部署改成浏览器能开的地址。**忘改也不会静默失败**：基址落回环 / `0.0.0.0` → 回调改用当前请求 origin（LAN IP 或隧道域名）；「添加凭证」显示实际地址；授权超时点名查 `PUBLIC_BASE_URL`。**回调地址在浏览器侧打不开时**（NAS / 远程部署常见 TRAE 跳转「网络错误」，refreshToken 只留在地址栏）：「添加凭证」里的 TRAE 粘贴框把地址栏整条链接粘回即可完成登录，不依赖浏览器能否访问本服务。
- **端口**：compose 里 `PORT` 同时决定容器内监听端口与宿主映射端口。
- **改代码后重建镜像**：`docker compose up -d` 不带 `--build` 用本地已有镜像跑旧代码。改 `src/` 或前端后必须 `docker compose up -d --build`。

### 轮换 APP_SECRET（仅在怀疑密钥外泄时）

`APP_SECRET` 是 Fernet 密钥的唯一来源（无盐 SHA-256），**直接换值会让已存凭证全部不可解**。要换必须重加密：

```bash
python3 scripts/backup_db.py                      # 1. 先留一致性备份
sudo systemctl stop coding2api                    # 2. 停机（运行中的进程会用旧密钥写库）
python3 scripts/rotate_app_secret.py \
    --db data/coding2api.sqlite3 \
    --old-secret-file /path/to/old.env \
    --new-secret-file /path/to/new.env            # 3. 全解 → 校验 → 单事务重写
# 4. .env 换新值、chmod 600 .env、重启
```

任一条解不开就整体回滚，库保持原样。脚本接受 `.env` 或只含密钥的单行文件；不要把密钥写在命令行（会进 history 与 `ps`）。

### 升级后必须重启后端
**改 `src/` 后必须重启进程**，否则「新前端 + 旧后端」错配：页面能开，新端点全 404。前端不需重启（后端每次请求现读 `web/dist`）；后端不是。

```bash
docker compose up -d --force-recreate   # Docker / compose
sudo systemctl restart coding2api       # systemd
```

确认升级已生效：

```bash
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version; SELECT username, role, enabled FROM users;"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users   # 期望 401；404 = 旧后端
```

### 前端产物与后端在同一端口

后端把前端产物放 `/` 直接服务（每次请求现读磁盘）→ **只改前端时构建完刷新即可**。`localhost:5173` 是 Vite 开发态，两者可并存。

```bash
./scripts/build-web.sh            # 仅在产物过期时构建
./scripts/build-web.sh --force    # 无条件重建
```

> **`web/.npmrc` 不能删**：它关掉 `auto-install-peers`（否则 `@lobehub/icons` 拖进 `@lobehub/ui` / `antd`）。该设置与 lockfile 的 `settings.autoInstallPeers` 强耦合；`Dockerfile` 必须 `COPY web/.npmrc`，否则 `--frozen-lockfile` 报 `ERR_PNPM_LOCKFILE_CONFIG_MISMATCH`。

> **部署操作见本地文档**：进程管理、路径、工具版本 → `docs/local-environment.md`（已 gitignore）。

## 公网入口（VPS + frp 反向隧道）

家宽 CGNAT 后无入站地址 → 公网入口只能内网侧主动连出。Cloudflare Tunnel 免费版把大陆流量绕洛杉矶（实测 1.1–2.4s、丢包约 60%）；「境外 VPS 入口 + frp 隧道回源」降到 80–150ms。只改入站；出站仍走 `PROVIDER_PROXIES`。

```
客户端 ──443/TLS──> VPS(nginx) ──frp 隧道──> 家中 Mac:8000
```

产物：`deploy/frp/`、`deploy/nginx/`、`deploy/systemd/frps.service`、`deploy/launchd/com.coding2api.frpc.plist`（frp v0.71.1 TOML）。

VPS 侧：

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo install -m 755 frps /usr/local/bin/frps          # 从 frp release 解出
sudo useradd --system --no-create-home --shell /usr/sbin/nologin frps
sudo install -d -m 755 /etc/frp && sudo install -m 644 deploy/frp/frps.toml /etc/frp/
openssl rand -hex 32 | sudo tee /etc/frp/frps-token >/dev/null
sudo chmod 600 /etc/frp/frps-token && sudo chown frps:frps /etc/frp/frps-token
sudo install -m 644 deploy/systemd/frps.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now frps
sed 's/__TUNNEL_DOMAIN__/ai-api.example.com/g' deploy/nginx/coding2api.conf \
    | sudo tee /etc/nginx/sites-available/coding2api.conf >/dev/null
sudo ln -sf /etc/nginx/sites-available/coding2api.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ai-api.example.com
```

DNS 把域名 A 记录指向 VPS 公网 IP；境外 VPS 无需 ICP 备案。

家中 Mac 侧：

```bash
brew install frp
./scripts/install-frpc.sh --server <VPS 公网地址> --token <与 VPS 一致的 token>
launchctl print gui/$(id -u)/com.coding2api.frpc | grep -E 'state|pid'
./scripts/install-frpc.sh --uninstall
```

`frpc.toml` 用 `{{ .Envs.FRP_SERVER_ADDR }}` 模板取值；地址与 token 由 plist 注入，token 落 `secrets/frpc-token`（600，已 gitignore）。

`.env` 同步三处：

| 变量 | 值 | 漏了会怎样 |
|---|---|---|
| `ALLOWED_HOSTS` | 追加隧道域名 | 隧道侧全部 400 `invalid host header` |
| `PUBLIC_BASE_URL` | `https://<隧道域名>` | TRAE 登录回调基址错、不下发 HSTS |
| `HOST` | 保持 `127.0.0.1` | 隧道回源到本机回环即可 |

`TRUST_PROXY`：VPS 上恰好一层 nginx 可置 `true` → Key 的 `allowed_ips` 看到真实 IP。仍需局域网直连入口则保持 `false`。

## 日志

应用只写 **stdout / stderr**，不写文件、不轮转。

| 部署形态 | 日志到哪 | 轮转 | 需做什么 |
|---|---|---|---|
| Docker / compose | `docker logs coding2api` | json-file（10m × 5） | 无需操作 |
| Linux（systemd） | `journalctl -u coding2api` | journald 自带 | 无需操作 |
| Linux（非 systemd） | 重定向到文件 | `logrotate` | `sudo cp deploy/logrotate/coding2api /etc/logrotate.d/` |
| macOS（launchd） | `logs/launchd.{out,err}.log` | `scripts/rotate-logs.sh` | `./scripts/install-logrotate.sh` |
| 裸跑 | 终端 stderr | 无 | 自行重定向并轮转 |

> **macOS 不要用 newsyslog**：它只做 rename 式轮转，而 launchd 给进程的 fd 仍指向旧 inode —— 轮转后进程继续往已归档的文件里写，新路径的文件直到下次重启才出现，中间日志全丢（本机实测丢过 21.7h）。`rotate-logs.sh` 走「拷贝 → 压缩 → 截断」，与 Linux 侧 `copytruncate` 同语义。

级别用 `LOG_LEVEL`（默认 `INFO`）。**审计日志 = INFO 级**，调到 `WARNING` 一并关掉。

### 请求级结构化日志

每个业务请求（`/api/*`、`/v1/*`、`/authorize`）额外一行 logfmt：

```
event=request request_id=9f2c… method=POST route=/v1/chat/completions status=200 duration_ms=1234
```

- `route` = 路由模板（`/api/credentials/{credential_id}`），资源 ID 不进日志。未匹配路由 → 回落清洗过的原始路径。
- 不写 query string、请求体、响应体、请求头、用户名。身份靠 `request_id` 关联 `usage_events`。
- `request_id` 与响应头 `X-Request-ID` 同值。
- 级别：`5xx` → WARNING，其余 INFO。
- 不记静态资源与探针（`/health`、`/healthz`、`/metrics`）。
- uvicorn access log 已关。
- 开关 `REQUEST_LOG_ENABLED`（默认 `true`，可热更）。

> 容器里 `data/dumps/`（`DUMP_REQUEST_BODIES=true` 写入）不在 docker 日志体系；应用自行保持最多 200 份**且不超过 24 小时**（按 mtime），目录 0700、文件 0600——这是唯一把对话写进磁盘的开关，排查完请立刻关闭。

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
| [`PROPOSAL.md`](PROPOSAL.md) | 目标与非目标、上游可行性、架构、数据模型、安全边界 |
| [`TECHNICAL.md`](TECHNICAL.md) | 技术栈、模块规格、Provider 协议、请求时序、测试策略 |
| [`diagrams/coding2api-architecture.html`](diagrams/coding2api-architecture.html) | 系统架构图 |
| [`diagrams/coding2api-request-flow.html`](diagrams/coding2api-request-flow.html) | 请求主链路（/v1/responses、/v1/messages 共用主链） |
| [`diagrams/coding2api-account-flow.html`](diagrams/coding2api-account-flow.html) | 账号与 API Key 生命周期 |
| [`diagrams/coding2api-data-model.html`](diagrams/coding2api-data-model.html) | 数据模型（13 张表 + 归属关系） |
| [`diagrams/coding2api-permission-matrix.html`](diagrams/coding2api-permission-matrix.html) | 三角色权限矩阵 |
| [`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html) | 请求主链路时序图 |
| [`diagrams/coding2api-credential-lifecycle.html`](diagrams/coding2api-credential-lifecycle.html) | 凭证调度状态机 |

## 授权协议

MIT，见 [LICENSE](LICENSE)。上游署名见 [NOTICE](NOTICE)。
