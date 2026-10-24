# Coding2API

把 **CodeBuddy**（国内 + 国际）、**TRAE SOLO**、**OpenCode Zen**、**Kilo**、**Qoder**、**CodeArts**、**OfficeAce** 八个上游封装成 OpenAI 兼容 API，提供公共凭证池、统一模型名与调度、按人用量统计。

> [!WARNING]
> 逆向接口，仅供学习研究，未做安全审计；公网部署需反向代理 + 鉴权 + IP 白名单。

## 特性

- **OpenAI 兼容出口**：`/v1/chat/completions`（流式 / 非流式）、`/v1/responses`（Codex CLI）、`/v1/messages`（Claude Code）、`/v1/models`（含单模型查询）、`/v1/user/balance`。
- **统一调度**：扁平模型名按健康度选号，`模型@渠道` 强制指定。免费渠道优先 → 在途最少 → 到期额度 → 健康度与分级冷却 → 余额多者优先。模型级限流只避让该模型；多轮对话粘同一凭证，出错才轮换。
- **到期额度优先**：主窗口（默认 36h）内将过期的额度先用，打平再比 7 天窗口。额度单位统一为积分。
- **模型列表按凭证加载**：只展示当前有可用凭证的渠道，未接入不出现幽灵模型。
- **公共凭证池**：admin 集中维护、全员共享、加密入库（`APP_SECRET`）；设备码登录、多账号切换、额度探测、每日签到、token 预刷新。
- **三角色账号**：`admin` / `operator` / `viewer`，存 SQLite；一次性激活链接、首登强制改密、改角色或停用即时吊销会话；登录与写操作留审计。
- **成长中心**（仅 CodeBuddy）：自动领礼、派 Buddy、领任务奖、断登补登、连登兑换、开盲盒；不可逆动作可用 `GROWTH_IRREVERSIBLE_ACTIONS=false` 关停。
- **脱敏统计**：不存对话内容；明细 90 天、小时汇总永久；按人 / 渠道 / 模型 / 凭证可视化；成本按 OpenRouter 刊例价估算；输出速度分正文 / 总产出两口径；明细支持 SSE 实时流。
- **上游状态聚合**：并排展示各提供方官方状态页，区分「上游故障」与「本服务凭证 / 额度问题」。只做解释、不参与调度；快照只存内存。
- **侧栏数据提醒**：侧栏显示待办计数角标（需重登 / 耗尽、未恢复告警、任务失败、异常上游、待激活账号、将到期 Key、目录陈旧），按角色裁剪。见[下文](#侧栏数据提醒导航角标)。
- **管理台安全加固**：登录限流、CSRF 校验、请求体上限、Host 白名单。
- **窄屏表格转卡片**：<768px 时一行 = 一张卡，列名收成可点的排序片；DOM 不变，读屏语义一致。

协议层实现（私有信封、签名、门禁伪装等）见 [`TECHNICAL.md`](TECHNICAL.md)。

## 快速开始

前置：Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js 24+ 与 pnpm 10+（仅构建前端需要）。

```bash
uv sync
uv run python scripts/create_user.py admin --role admin   # 建首个管理员
cd web && pnpm install && pnpm build && cd ..
APP_SECRET="换成你自己的随机字符串" \
  uv run python -m uvicorn src.main:build_app --factory --port 8000
```

打开 <http://127.0.0.1:8000> 登录管理台。之后加人都在「用户管理」页。

`scripts/create_user.py` 直接写 SQLite（`--db` 或 `DATA_DIR`，默认 `data/coding2api.sqlite3`）。老部署的 `secrets/users.txt` 启动时导入一次（不覆盖已存在的）。

Docker：

```bash
cat > .env <<'EOF'
APP_SECRET=换成你自己的随机字符串
PUBLIC_BASE_URL=http://127.0.0.1:8000
EOF
docker compose up -d --build
docker compose exec coding2api python scripts/create_user.py admin --role admin
```

拉取镜像：`docker compose pull`（或 `docker pull ghcr.io/robbsluo/coding2api:v0.9.1`）。打 tag `v*` 推到 main 即触发 `.github/workflows/publish.yml`，同时发布 `<tag>` 与 `:latest`。

## 用户与角色

账号存在 SQLite 的 `users` 表，管理台「用户管理」页（仅管理员）负责增删改。

| 角色 | 能做什么 |
|---|---|
| `admin` | 用户管理、任务与配置、凭证读写、全部统计、审计日志、服务端日志、上游状态、模型列表 |
| `operator` | 凭证读写（导入 / 删除 / 启停 / 签到 / 成长 / 切换账号）、全部统计、上游状态、模型列表 |
| `viewer` | 只读：仪表盘、凭证列表、统计、Playground |

- **加人**：填用户名 + 角色，生成一次性激活链接（24 小时有效，只显示一次），对方打开 `/activate` 自设密码。系统里不存在「管理员知道但用户不知道」的初始密码。重置密码同理。
- **改角色 / 停用**：即时作废该账号所有会话 Cookie。停用是可逆的默认「删人」方式（用量统计仍归属该用户名）；硬删只在 CLI。
- **自助改密**：右上角用户菜单 →「修改密码」，需当前密码。
- **防锁死**：不能停用 / 降级最后一个活跃管理员，也不能对自己降级或停用。

CLI 兜底（无 Web / 全部管理员失联）：

```bash
python3 scripts/create_user.py alice --role admin   # 交互输入密码
python3 scripts/create_user.py --list
python3 scripts/create_user.py alice --delete --force   # 硬删，不可逆
python3 scripts/create_user.py --db data/x.sqlite3 ...
echo "$PASSWORD" | python3 scripts/create_user.py alice --role admin --password-stdin
```

`ADMIN_USERNAMES` 与 `secrets/users.txt` 只在引导期生效（首个管理员、老部署升级），此后角色只来自 DB。

## 审计

「审计日志」页（仅管理员）记录登录与写操作，可按操作者 / 动作筛选、分页：登录（成败都记）、账号变动、凭证写操作、告警人工处置。每条含时间、操作者、动作、对象、说明与来源 IP。**不含密码或令牌明文**（激活令牌只存 SHA-256 摘要）。保留期同请求明细（默认 90 天）。

## 服务端日志

「服务端日志」页（`/logs`，仅管理员）是只读的进程内实时日志视图：`GET /api/logs` 取最近若干行，`GET /api/logs/stream` 走 SSE 先回放再推新行。支持级别筛选、关键字过滤、跟随最新、暂停、清空，**无任何写入入口**。

- 只收本进程 `logging` 记录，不读文件、不 tail；多进程下各看各的。
- **脱敏是硬前提**：每行发出前过白名单，命中 `token` / `secret` / `authorization` / `cookie` / `password` / `api_key`、`sk-…` 形状或 `Bearer <值>` 的整行丢弃。
- 缓冲容量 `LOG_BUFFER_LINES`（默认 500，可热更），环形覆盖最旧；`0` 关闭整页（两个端点 404）。
- 单行上限 4000 字符；每行带 `seq`、时间、级别、logger、`request_id`。订阅者上限 4，超限 429 + `Retry-After`。

## 使用

1. 「凭证管理」添加凭证：
   - **CodeBuddy**（含国际）：设备码登录，或粘贴 `{"token":"..."}`。
   - **TRAE**：粘贴凭证 JSON（`accessToken`/`uid`/`refreshToken`）或回调链接（`apiHost` 只接受官方地址）。
   - **Qoder / CodeArts**：设备码 / OAuth2 授权码登录（CodeArts 授权后把打不开的本地回调链接粘回页面）。
   - **OfficeAce**：点「登录 OfficeAce」走华为云授权（回调由云端承接）。
   - **OpenCode Zen / Kilo**：无需添加，启动时自动出现虚拟凭证（删了可在同面板补回）。
2. 「API Key」创建 `sk-...`（明文仅显示一次）。
3. 调用：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'
```

`模型@渠道` 强制指定上游（如 `glm-5.2@trae`、`mimo-v2.5-free@zen`），不写则自动选。任意 OpenAI 兼容客户端可直接接入（Base URL `http://127.0.0.1:8000/v1`）。「Playground」页用登录会话直接测试，无需 API Key。

### 模型名三字段

每个模型统一三个字段：

1. **原代号**（`raw_id`）：渠道真正发的 key，永不改动。各渠道内部代号不同（Qoder `kmodel_latest` = TRAE `kimi-k3-1`），可能带命名空间前缀（Kilo 的 `kilo-auto/free`）。
2. **归一键**：剥掉免费档标记（`-free` / `free` 路径段）与命名空间前缀后 slug 化，如 `kilo-auto`。模型列表按它判断「是不是同一个模型」并合并。
3. **展示名**（`name`）：清洗后的可读文本，如 `LongCat 2.5 Preview`。上游给了可读名就用，否则从原代号派生；品牌写法按官方纠正（`DeepSeek` / `MiMo` / `GLM` / `GPT`）。

`GET /v1/models` 里所有 `id` 都是归一键，转发时自动换回原代号，`by_provider.{渠道}.raw_id` 可见。**三种写法都能直接请求**。同渠道内归一键重复时退回原代号。

**价格与档位**：条目可选带 `cost {input, output, cache_read}`（USD / 百万 token，与成本估算同一份价表）与 `reasoning_efforts`（该渠道生效的档位集合）。有才给，缺价不带该字段。

**自定义别名**（`MODEL_ALIASES`，可热更）：格式 `对外名=模型@渠道;对外名2=模型`，`@渠道` 省略则自动选。列表与请求解析用同一份，条目带 `custom_alias: true`。撞名、目标不存在、渠道不持有该模型时该段记 ERROR 并跳过；不支持别名链。

**单模型查询**（`GET /v1/models/{id}`）：三种写法 + 自定义别名都认，大小写不敏感，带命名空间的原代号也能查。找不到或不在 Key 白名单内一律 404（`code=model_not_found`），不泄露池里有哪些模型。

**`n` 参数只接受 1**：出口结构只能产生一个候选，`n != 1` 一律 400。

### 流式 usage 与思考 token

- 收尾时在 finish chunk 后、`data: [DONE]` 前补一帧 `choices: []` 的 usage；上游没上报就不补，未上报字段一律 `null`。
- 思考 token 需客户端带 `reasoning_effort`；CodeBuddy 渠道未给时补 `medium`（否则思考会写进正文），显式给的值原样透传。

### 流式超时（默认关闭）

八条渠道的上游流读取都是 `read=None`，网关自己兜：

- **首事件超时**（`STREAM_FIRST_EVENT_TIMEOUT_SECONDS`，默认 0）：客户端还没收到内容时超时，可安全换号重试，记 `upstream_first_event_timeout`。注释帧 / 心跳不重置计时。
- **流内空闲超时**（`STREAM_IDLE_TIMEOUT_SECONDS`，默认 0）：已出帧后超时只能断流并以错误帧收尾，**不换号**（半截输出无法回滚）。

两项默认关闭是刻意的：实测慢上下文首字可达 40–71s。要用请在管理台按部署实际打开（推荐 60 / 300）。

### Playground 高级参数

可选填 `temperature` / `max_tokens` / `reasoning_effort`，**留空即不发送该键**。前端校验边界与后端一致（`temperature` ≤ 1000、`max_tokens` ≤ 4,000,000）。`reasoning_effort` 只在模型支持推理时出现，默认「不发送」。

## 渠道

> CodeBuddy（含国际）/ TRAE / Qoder / CodeArts / OfficeAce 需填凭证；**OpenCode Zen 与 Kilo 是无凭证免费层**——池里是一条虚拟占位行（调度 / 冷却 / 统计照常），额度列显示「免费层（无额度接口）」，健康度恒为「无探测」。虚拟行删除后重启会复活，永久停用请用「暂停」。

### OpenCode Zen（`zen`）

接 [opencode.ai/zen](https://opencode.ai) 的免费模型，无需登录。上游清单混着付费模型且不带免费标记，本服务按 `-free` 后缀收窄再逐个探活，只把匿名真正可用的放进 `/v1/models`（判活缓存 30 分钟，免费模型标 **x0**）。

**免费层门禁**：上游要求伪装官方客户端（UA 版本、会话头、`stream:true`、tools 含 `bash`/`read`），本服务自动满足；注入的 `bash`/`read` 是空壳，模型真调用时被过滤。UA 用 `ZEN_OPENCODE_VERSION`，端点用 `ZEN_API_ENDPOINT`（须在 `ZEN_ALLOWED_ENDPOINTS` 内）。付费模型强制 `模型@zen` 也只报不可用。

### Kilo（`kilo`）

接 [Kilo](https://kilo.ai)（`api.kilo.ai/api/gateway`）的免费模型，无需登录。标准 OpenAI 协议，上游 `/models` 每条带 `isFree`，据此直接过滤，**不做探活**（省免费配额）。

**额度与限流**：网关级约 200 请求/小时/IP。上游 429 与 502/503/504 都是模型级——只冷却被点名的模型。端点用 `KILO_API_ENDPOINT`（须在白名单内）。

### Qoder（`qoder`）

接 [Qoder](https://qoder.com)，需真实账号：点「登录 Qoder」完成设备码授权（Token 不经过浏览器）。上游是私有 COSY 协议，本服务负责签名与解包，对外仍是标准 OpenAI。

**额度与签到**：支持额度探测（`/api/v2/quota/usage`）与每日签到（积分可累积）。签到为活动制（`/sash/api/v1/me/campaigns`），旧 `daily-check-in` 仅作回退。**签到活动每日 10:00（UTC+8）刷新**（领取后 30 天有效），后台按该窗口封账。新协议不提供连续天数。**国际站没有签到接口**（404），记为「本区域无此接口」。

**端点与节流**：默认国内版（`openapi.qoder.com.cn` + `gateway.qoder.com.cn`），国际站改 `QODER_API_ENDPOINT=https://openapi.qoder.sh` 并把域名加进白名单。`QODER_CHAT_MIN_INTERVAL` 默认 5，可热更。

**上游节点故障**：Qoder 有时把自身节点故障包成 400（`[FAIL]node:…`），本服务识别为模型级瞬时故障，只冷却该模型，耗尽候选时回 503（`no_healthy_credential`）。

### CodeArts（`codearts`）

接 [华为云 CodeArts](https://codearts.huaweicloud.com)，需真实账号：点「登录 CodeArts」，授权后浏览器跳到打不开的本地地址，把**整条链接**粘回页面换取 STS 凭证（AK/SK + security_token + refresh_token + DPoP 私钥）并加密入库。

**令牌刷新是刚性的**：refresh_token 与 `client_id=codearts-agent`、DPoP 私钥三者绑定且一次性，刷新后必须回写新 refresh_token。由预刷新任务自动完成（DPoP ES256/P-256）。预刷新窗口封顶 45 分钟且必须宽于轮询周期。凭据被作废时标记「需重新登录」并停止自动重试。

**每日签到**：`GET /v1/ops/delivery` → `POST /v1/ops/claim` → `POST /v1/ops/confirm`，后台每日自动领取。`CLAIMED` 是「已领未确认」，两步一起做。

**额度＝积分余额**：探测走 `GET {snap}/snap-manager/v1/statistics/plugin`（前缀必须带 `snap-manager`，头 `Agent-Type: PromptCenter`）。上游数值即积分，不做折算；分项包走悬浮明细，并登记套餐到期日（积分过期同样作废）。

**签名 / 累计全文 SSE / 节流**：华为云 `SDK-HMAC-SHA256`（AK/SK + `X-Security-Token`），白名单须含 snap 引擎、STS 与门户。流式 `text` 是累计全文（替换语义），本服务还原为增量。上游硬限每账号并发 3，故有 `CODEARTS_MAX_CONCURRENCY` 与 `CODEARTS_REQUEST_WINDOW_SECONDS`。

### OfficeAce（`officeace`）

接 [华为云 OfficeAce](https://www.huaweicloud.com/product/agentarts.html)，需真实账号：点「登录 OfficeAce」完成华为云授权即可，回调由云端承接，不监听本地端口。后端按 2.5s 轮询 `/v1/claw/auth/code` 取回授权码。

**开通前提**：账号必须先在 OfficeAce 客户端登录并兑换邀请码。未开通的账号登录「成功」但拿不到模型密钥，本服务会点名报出「华为云账号 xxx 还没开通 OfficeAce 服务」。

**两套凭据，寿命不同**：

| 凭据 | 寿命 | 用途 |
|---|---|---|
| `Basic model_app_key:model_app_secret` | 不过期 | 聊天转发 |
| 临时 AK/SK + `security_token` + `project_id` | 约 2 小时 | 积分查询、签到 |

混用会得到 `APIG.0303 app not found`。临时凭据由预刷新任务续期，`refresh_token` 一次性轮换且与 `client_id=pdp5_for_agentarts` + DPoP 私钥绑定（与 CodeArts 票不通用）。

上游就是标准 OpenAI 协议，但有三处特例：

- **限流走伪 200**：HTTP 200 + `data:{"error":…}`，错误帧照常交引擎分类，`81111/81114` 只冷却「该凭据 × 该模型」。
- **模型级拒绝不牵连整条凭据**：`81004` / `81009` 走 403/404，业务码判定先于状态码，归为模型级负缓存（成功即撤销）。
- **`max_tokens` 是推理 + 正文总预算**：`≥16` 时自动追加 1024 余量再夹到该模型硬上限，`<16` 原样透传，未给不补。

**积分即主账**：单位是 `officeace_points`，`remaining`/`total` 直接是积分。每笔奖励独立到期、不合并重置，故额度口径写作「逐笔到期」。

**每日签到**：`POST /v1/subscription/bonus/claim`（写操作，调用即真发积分；body 必须 `` + JSON `Content-Type`）。上游对当日重复领取幂等。收获按剩余量增量计算。封账到下个北京时间 0 点。

**不做主动探活 / 不做 credit 估算**：探活要真发推理消耗积分；`cost_coefficient` 与 `tokens_per_point` 的叠乘关系官方未说明。模型级拒绝改由真实请求按 `BLOCKED` 负缓存处理。

**签名与端点**：控制面 `/v1/claw/*` 用 `SDK-HMAC-SHA256`；模型网关只认 `V11-HMAC-SHA256`，用错回 `APIG.0624` 或 401。`OFFICEACE_ALLOWED_ENDPOINTS` 只需控制面主机（模型网关每账号不同，由上游签发存在凭证里）。`OFFICEACE_CHAT_MIN_INTERVAL` 默认 0。

**单账号单用户**：官方《套餐管理》限制单账号单用户，不做多人共用。

**换账号**：登录按钮旁有「换账号」（`force_login=true`），先清掉浏览器里的华为云会话再授权。

### CodeBuddy 国际（`codebuddy_intl`）

接 [www.codebuddy.ai](https://www.codebuddy.ai)，与国内站同协议，但是两套独立账号体系（各自注册、各自计费），故为独立渠道。登录链路与国内版一致（设备码）。

不合并成一个渠道的原因：`CODEBUDDY_API_ENDPOINT` 是进程级单值，切过去会让存量国内站凭证打到国际站。两站可同时持有凭证，各走各的端点、节流器与登录流程。

**白名单刻意互不含**：`CODEBUDDY_INTL_ALLOWED_ENDPOINTS` 默认只有国际站主机，填国内站地址会在启动时报错（跨站发送即凭证泄露）。

**能力差异**：聊天、模型列表、额度探测、token 刷新、活跃上报全部启用；**每日签到、成长中心未启用**（签到渠道侧就没有该活动，成长中心无法验证且含不可逆动作）。

**模型列表按套餐探活**：用当前凭证逐个发一次最小请求，只保留真正可用的模型；`11158`（套餐不允许）与 `11102`（无此模型）剔除，`429` / 5xx / 超时保留。结论按账号缓存 6 小时。

**同名模型不带渠道后缀**：两站共有的模型在 `/v1/models` 里是一条、挂两个渠道；`-x` / `-sg` 这类真实不同的模型各占一条。

**节流**：`CODEBUDDY_INTL_CHAT_MIN_INTERVAL` 默认 5s，独立节流器，可热更。

### 按渠道出站代理（可选）

```
PROVIDER_PROXIES="codebuddy=http://127.0.0.1:7890;qoder=socks5://127.0.0.1:1080"
```

格式 `渠道=代理URL;渠道2=代理URL2`，协议支持 `http/https/socks5/socks5h`，留空 = 全部直连。作用于该渠道全部出站请求（聊天、额度、模型拉取、后台任务、OAuth）。**启动期项**，改后需重启。未知渠道 / 非法协议 / 缺 `=` 一律启动失败。环境变量 `HTTP_PROXY` 等一律不采信（`trust_env=False`）。

> 容器内 `127.0.0.1` 指容器自身。代理跑在宿主时写 `host.docker.internal`：
> `PROVIDER_PROXIES="codebuddy=http://host.docker.internal:7897"`
> compose 已声明 `extra_hosts`。解析只校验格式不校验可达，写错不会启动失败。

## 出口协议

### Responses API（Codex CLI）

`POST /v1/responses` 提供 Responses 子集，与 chat 共用同一套选号 / 冷却 / 轮换 / 统计与会话粘性：

```bash
export CODING2API_KEY=sk-你的key
codex -c "model_providers.coding2api={ name='coding2api', base_url='http://127.0.0.1:8000/v1', wire_api='responses', env_key='CODING2API_KEY' }" \
      -c model_provider=coding2api -c model='glm-5.2' '你的任务'
```

支持文本、流式正文、思考摘要、函数工具调用、`finish_reason=length` → `response.incomplete`。`text.format` 映射到 chat 的 `response_format`。内容 part 映射：`input_text` / `text` / `output_text` → text，`input_image` → `image_url`。**不支持** `store=true`、`previous_response_id`、`input_file`、Responses 私有工具——一律显式 400。`include=["reasoning.encrypted_content"]` 接受但忽略。

### chat `response_format` 收口

只放行 OpenAI 定义的三种形状，畸形或未知显式 400：`{"type":"text"}`、`{"type":"json_object"}`、`{"type":"json_schema","json_schema":{name, schema, description?, strict?}}`（内部未知字段丢弃）。Responses 出口的 `text.format` 与 chat 入站共用同一段校验。

### Anthropic Messages API（Claude Code）

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN=sk-你的key     # 或 ANTHROPIC_API_KEY（走 x-api-key 头）
claude
```

- **鉴权**：`x-api-key` 与 `Authorization: Bearer` 都接受。
- **流式**：`message_start` → `content_block_*` → `message_delta` → `message_stop`；无 `[DONE]`，thinking 块在 `content_block_stop` 前补 `signature_delta`。
- **缓存命中**：出口 usage 拆两笔——`input_tokens` 只计未命中，命中另记 `cache_read_input_tokens`。
- **`count_tokens`**：本地估算，不转发上游。
- **图片**：`image` 块归一为 chat `image_url`；`tool_result.content` 内的图片同样保留。
- **不支持**：`document` 块、`source.type=file`、Anthropic 服务端工具，一律显式 400。
- **`cache_control` 透传**（默认开启）：落点与 Anthropic 一致（`system`、消息内容块、`tool_result` 顶层、`tool_calls` 顶层、`tools` 顶层）。只做结构校验，取值不解释。八渠道实测 6 个接受（codebuddy / codebuddy_intl / trae / qoder / codearts / kilo）；zen 未验证、OfficeAce 未单独验证。脚本 `scripts/probe_cache_control.py`。

### CC Switch 一键导入

「API Key 管理」页可生成 `ccswitch://` 深链（Claude Code / Codex CLI / OpenCode 各一个按钮）。入口两处：创建 Key 成功后的对话框、Key 列表每行的「导入 CC Switch」。深链含 Key 明文，只在用户主动打开时生成。需 CC Switch v4.0.10+。注意 endpoint 形态随 `apiFormat` 变：Anthropic 填到根（不带 `/v1`），OpenAI 填到 `/v1`。本服务无 Gemini 出口。

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

从未探测成功时 `balance_known` 为 `false`（未知，不是 0）。

### 池健康检查

- `GET /health`：只回 `{"status":"ok"}`，适合存活探针。
- `GET /healthz`：额外给凭证池计数，`ready == 0` 时回 503（就绪探针）。

两个端点都不鉴权，且都接受 `HEAD`。五类计数互斥且合计 = `total`，与调度器同口径。**把存活探针配在 `/health`**，`/healthz` 用作就绪 / 告警。

> 升级注意：`/healthz` 的状态从「恒 200」变为「ready=0 时 503」。

### Prometheus 指标（`GET /metrics`）

默认开启、**不鉴权**；`METRICS_ENABLED=false`（可热更）时 404。

| 指标 | 类型 | 标签 | 数据源 |
| --- | --- | --- | --- |
| `coding2api_requests_total` | counter | `provider` / `model` / `status` | `usage_hourly` |
| `coding2api_tokens_total` | counter | `provider` / `type` | `usage_hourly` |
| `coding2api_credentials` | gauge | `state` | 凭证池 |
| `coding2api_upstream_errors_total` | counter | `provider` / `kind` | 进程内计数（重启归零） |
| `coding2api_task_failures_total` | counter | `task` | 进程内任务运行态（重启归零） |

不含 `credential_id` / `api_key_id` / `username`（高基数）。`requests_total` / `tokens_total` 读永久保留的小时汇总，跨重启单调。小时汇总没有 `error_type` 维度，故 `kind` 由进程内计数器提供。

## API Key 的绑定、白名单、来源 IP 与到期时间

- **渠道绑定**：该 Key 只在该渠道凭证里选号；模型属另一渠道时 400 并指出实际归属。留空 = 跨渠道自动。
- **来源 IP 白名单**：逗号分隔 IP 或 CIDR，写入时规范化（`10.0.0.1` → `10.0.0.1/32`），不在白名单返回 403。默认不采信 `X-Forwarded-For`；仅 `TRUST_PROXY=true` 时按 XFF 取最后一个条目。
- **模型白名单**：逗号分隔模型名或 fnmatch glob（如 `glm-*,kimi-k3`），不区分大小写，`模型@渠道` 后缀不参与匹配。白名单外 400，`/v1/models` 也只列白名单内模型。
- **到期时间**：epoch 秒，留空 = 永不过期，到期后 401（与「Key 不存在」统一文案）。
- **配额**：六项独立，`0` 或留空 = 不限制——`daily_requests` / `daily_tokens` / `daily_cost_usd` / `rpm` / `tpm`（用预估值判定）/ `max_concurrency`。当日类按北京时间自然日，速率与并发是进程内滑动窗口。超限 429 + `quota_exceeded`，`Retry-After` 指向可重试时刻。判定出异常时一律放行并记 warning。
- **自助查询**：`GET /v1/usage?days=1|7|30` 返回自己的用量、剩余额度与当前速率占用（只读本地明细）。

## 凭证暂停

「暂停」把该凭证摘出对话流量，并让每日签到 / token 预刷新跳过它；其余后台任务（额度探测、成长中心、活跃上报）照常运行。适合「先不接聊天、但积分继续领」。「取消暂停」立即放回池子。与「已禁用」不同——后者是渠道判定会话失效后的系统禁用，需重新登录后「恢复」。同一开关也在 `POST /api/credentials/{id}/toggle`。

> Zen / Kilo 是虚拟占位行，**暂停是永久停用的唯一方式**（删除后重启会补回）。暂停也会把该渠道从模型列表摘掉。

## 凭证批量运维

凭证页支持多选 + 批量操作条（探测额度 / 每日签到 / 暂停 / 取消暂停 / 恢复）。

- **端点**：`POST /api/credentials/batch`，body `{action: "probe"|"toggle"|"checkin"|"revive", ids: [...], enabled?}`。一次最多 200 条，重复 id 去重。
- **逐条隔离**：单条失败不影响其余。响应 `{action, total, succeeded, failed, results: [{id, ok, error?, detail?}]}`。
- **暂停是显式状态而非取反**：`toggle` 的 `enabled` 由请求体显式给出，故操作条拆成「暂停」与「取消暂停」两个按钮。
- **「恢复」与「取消暂停」是两件事**：前者 `revive`（解系统硬禁用，需先重新登录），后者 `toggle enabled=true`。
- 只收已有单条端点的动作：`refresh` 与 `delete` 不走这个通道。
- **审计只记一条汇总**（`toggle` / `revive`）；审计写失败只记 warning，不影响操作结果。

## 凭证限额护栏

凭证级可选护栏，行内菜单「设置限额」。四项独立，全部留空 = 不受限。判定在选号前完成；后台任务照常运行。

- **保留积分**：剩余积分 ≤ 该值时整号停用。余额未知时不猜。
- **每日 token 上限** / **每日积分上限**：当日累计达到即整号停用。
- **按模型 token 上限**：`{模型: 上限}`，只停该模型。
- **当日 = 北京时间自然日**，0 点刷新。
- 全部候选因限额被跳过时回 `no_healthy_credential` 503；模型级冷却回 `model_unavailable`。
- 写入 `PUT /api/credentials/{id}/limits`（整体替换，四个全关时删除配置行）。即时生效。审计动作 `credential.limits`。

## 凭证在途上限

`MAX_INFLIGHT_PER_CREDENTIAL`（默认 0 = 不限）：同一凭证并发达到该值后不再参与选号。

- 计数是进程内的（单实例语义）。
- 租约在请求结束时释放，另有 600s TTL 兜底。
- 全部占满时与限额护栏同口径回 503。
- 可热更。
- **在途计数兼作负载键**：插在「免费优先」之后、到期额度之前，并发自动摊到空闲号。

## 凭证池的筛选与分组

- **筛选工具条**：搜索（昵称 / 渠道 / id）× 渠道 × 状态 × 标签，四者 AND，纯前端收敛。显示「筛选后 N / 总数」并提供「清空」。标签下拉只在池里真出现过标签时渲染。标签不参与搜索词匹配。
- **标签**：逗号分隔，最多 16 个、单个 ≤ 24 字符。只用于筛选与展示，不参与选号。行内最多列两个，多余收成 `+N`。
- **组排序**：只重排渠道组，不动组内行序。默认额度从高到低，可切回固定渠道顺序。无额度接口的免费层垫底。
- **按渠道分组**：组头是紧凑仪表盘——图标 + 渠道名 + 账号数、模型（数量 + 最低倍率，只算收费模型；整组免费才写「全免」；自定义别名不算独立模型）、状态分布、额度合计、到期积分（主窗口 1.5 天一块，次窗口 7 天仅在更多时再出一块）。默认全折叠，筛选生效时自动展开。点组头折叠 / 展开。
- **列头跟着展开区走**：列头渲染在每个展开组内部并吸顶，各组共用同一套排序状态。「成长中心」列只在 CodeBuddy 出现，其余渠道留空占位以保持列宽。
- **长列表可滚**：表格限高 70vh，吸顶列头与「操作」按钮常驻。

额度列数字带虚线下划线，悬浮显示本行的周期口径。

## 模型列表加载

`GET /v1/models` 与 Playground 下拉只合并**当前有可用凭证**的渠道（至少一条未暂停、未会话失效的记录）。冷却中的凭证仍算有凭证。展示顺序：CodeBuddy、TRAE、Qoder、CodeArts、OfficeAce 在前，组内按名字典序，多渠道模型按最高优先级渠道归位。

**请求路由只认这份模型缓存列表**：目录能证明归属时，候选只有那些渠道；全部不可用时直接 503，不再逐渠道试。模型完全不在目录里时仍按全部候选路由；直连指定与 Key 渠道绑定不受影响。

### 模型目录落盘与启动即用

模型表除进程内 TTL 缓存（300s）外，每次拉取成功还写快照到 `DATA_DIR/model_catalog.json` 并在启动时同步读回，重启那一刻归属表就已就绪。快照损坏或超 7 天只丢缓存。另有 `MODEL_CATALOG_MINUTES` 后台任务兜底。

Playground 与 `/v1/models` 走 stale-while-revalidate：有缓存立刻回旧列表，TTL 到期后台刷新；某渠道一条缓存都没有时才同步等。连续失败时重试间隔按 2^(n-1)×TTL 退避（封顶 2 小时），日志只在首次失败打 WARNING。

### 管理台「模型列表」页

（`GET /api/model-catalog`，管理员 / 操作员）只读展示 OpenRouter 公开目录：刊例价、上下文 / 输出上限、模态、能力、知识截止、Artificial Analysis 三项指数（非本服务实测，不做排序）。每行带 `available` 布尔，页面提供默认关闭的「只看当前渠道」开关。

**疑似下线视图**：`GET /api/models/availability`（operator）把最近 N 天（默认 7，可调 1–30）`upstream_blocked` / `invalid_request` 的失败按 `(渠道, 模型)` 聚合。行内「加入黑名单」把该模型追加到 `model_blocklist`，走既有设置接口并落审计。

**浮动别名取同族最新快照的分**：OpenRouter 对同一模型同时挂浮动别名与带日期快照，并用 `canonical_slug` 声明同属一次发布。本项目转发时推理默认开着，与 Max 变体同口径，故按 `canonical_slug` 的日期取同族最新快照的分；显式带日期戳的 id 仍用自己的分。家族只从带 `-YYYYMMDD` 的 canonical 派生。

### 主动探活与人工接管

主动探活面板（`GET /api/models/probes`，operator）只探**仍被 `blocked` 负缓存锁住**的 (渠道, 模型)。

三态判定：

- **成功** → 立即清该渠道的 `blocked` 负缓存（唯一的提前恢复入口）。
- **`failed`**（上游明确 `BLOCKED` / `INVALID`）→ 保留负缓存并累加连续失败。
- **`inconclusive`**（额度、限流、网络、超时、空流、任何异常）→ 绝不禁用，并把连续失败归零。

探活与生产完全隔离：直连 `provider.stream_chat`，不写 `usage_events`、不动冷却表、不改健康度。结论落独立的 `model_probes` 表。面板不列出命中 `MODEL_BLOCKLIST` 的模型。

**判死的模型同时从模型列表摘掉**：`failed` 且仍被负缓存锁着时，只抽该 (渠道, 模型)；负缓存过期即自动回列表。

**人工接管**（`POST /api/models/probes/manual`）：接管后自动探活跳过该行，但不改负缓存。要立刻恢复点「立即探活」。后台周期由 `MODEL_PROBE_MINUTES` 控制（默认 0 = 关闭）。

### 管理台「上游状态」页

（`GET /api/upstream-status`，管理员 / 操作员）聚合各提供方官方状态页，回答「这个报错是上游挂了，还是我这边凭证 / 额度问题」。

- **来源**：OpenCode Zen（逐模型 TTFT / tps）、Kilo、Moonshot / Kimi、MiniMax、OpenAI、Anthropic、Together AI、DeepSeek。按 `scope` 分两组：`channel` 在前，`model` 在后。
- **归一化六档**：`operational` / `degraded` / `partial` / `major` / `maintenance` / `unknown`。整体取最差源，`unknown` 排最后。
- **只做解释、不做决策**，不参与选号 / 熔断。
- **只存内存、不落盘**；抓取由后台任务（`UPSTREAM_STATUS_MINUTES`，默认 5 分钟）负责，单源失败只标该行。
- **版式先结论后明细**：顶部概况条，下面按 scope 分两组，异常源默认展开；「需要关注」条把非正常源提前。组件明细超过 6 项时截断（异常项排前）。
- **不覆盖**仅提供 HTML 的源（Groq、腾讯云、阿里云、华为云、SiliconFlow、Qoder、智谱、TRAE）。

### token 到期展示

凭证列表的 **token 剩余**列显示 access token 距到期还有多久，低于 `TOKEN_EXPIRY_WARNING_SECONDS`（默认 1 小时）标红。优先取上游 `expires_at`，缺失回落 JWT `exp`；都取不到显示 `—`，不猜本地 TTL。

## 侧栏数据提醒（导航角标）

侧栏在每个页面都在。多个不点进去就发现不了的待办态在这里显示成计数角标，由单个聚合端点 `GET /api/nav-badges` 返回，侧栏 60s 轮询。

| 导航项 | 门槛 | 角标口径 |
|---|---|---|
| 凭证管理 | operator+ | `disabled` 或 `health < 0` 的凭证数 |
| 运维告警 | admin | 仍在告警中的条数 |
| 任务与配置 | admin | 上次执行失败的任务数 |
| 上游状态 | operator+ | 异常源数（含维护中） |
| 用户管理 | admin | 有激活令牌的账号数 |
| API Key | 全员（仅本人） | 已过期或 7 天内到期的 Key 数 |
| 模型列表 | operator+ | 快照陈旧（布尔文字） |

- **按角色裁剪**：后端只下发有权看的键，缺失 = 无权看（不是 0）。凭证角标不下发给 viewer。
- **只数「待办」**：例如告警页 `total` 含已恢复历史，角标只数未恢复的；任务用「上次失败」而非「连续失败 ≥ 阈值」。
- **凭证角标 ≠ `/healthz` 的可用数**：后者不看 health，额度耗尽的号仍算 `ready`。
- **告警角标会自愈**：`AlertTask` 启动时从库里未恢复的行重建集合。
- **模型目录陈旧**：从未拉取不算陈旧；超过 2 倍 `OPENROUTER_CATALOG_MINUTES` 未刷新才算。

## 统计

### 口径

时间范围默认**最近 24 小时**（另有 7 天 / 30 天 / 全部 / 自定义）。自定义给两个 `datetime-local`（按浏览器本地时区），可只填一边；起点不早于终点时整窗作废并提示。所有统计端点接受 `until`（epoch 秒，半开上界 `[since, until)`）。

**明细失败类型**是受控枚举（后端 `CONTROLLED_ERROR_TYPES`）：模型级冷却耗尽记 `model_unavailable`，凭证池真耗尽记 `no_healthy_credential`（对外错误码相同，仅统计口径区分）。

**请求 ID**：每个响应都带 `X-Request-ID`。入站合法则沿用，否则自生成；合法字符集 `[A-Za-z0-9._:-]`、长度 1–128。该 ID 落进 `usage_events.request_id`，明细时间列悬停可见。**不透传到上游**（各渠道出站头语义不同）。

**Credit 消耗**：CodeBuddy 是上游真值；TRAE 按官方单价推算并标 `≈`（单价表在 `src/provider/trae/pricing.py`，未收录不推算）。历史可用 `scripts/backfill_trae_credit.py` 补齐。

**成本（估算，≈）**：token × 公开刊例价，不是上游真实扣费。单价取自 OpenRouter 公开目录，按 `(输入−命中)×输入价 + 命中×缓存价 + 输出×输出价` 折美元，再按写入时生效的 `USD_CNY_RATE` 折人民币。

- **写入时定值**：成本在写明细时算好落库，历史行不随价表或汇率重算。上线前历史两列为 `NULL`，可用 `scripts/backfill_cost.py --apply` 补齐。
- **匹配不到就不计**：本地模型名按 OpenRouter `id` / 展示名等值匹配（命中不唯一就不配），聚合值 `null` 显示 `—`，故成本是下限。渠道分发代号按显式等价规则归到基础模型（只登记实测过的 id，不做通用剥日期 / 剥区域尾缀）。

**缓存命中率**：命中 token ÷ 输入 token（1 位小数）。未上报或输入为 0 时显示 `—`。分组表与明细表把命中率与 token 放同一格。

**输出速度（tok/s）**：`token 总量 ÷ 生成时间总和`，不是逐条比值再平均。两个口径分母相同、分子不同：**正文速度**用 `output_tokens - reasoning_tokens`，**总产出速度**含思考 token。前端并成一格显示 `正文 / 总产出`。只算经本服务路由且产出 ≥ 1 token 的成功请求。生成时间 = `latency_ms - ttfb_ms`；生成窗口 < 200 ms 或单条 > 1000 tok/s 的按不可测剔除。`ttfb_ms` 为 `NULL` 时不进主口径；某行 / 组在主口径下没有样本时回落估算口径（端到端耗时当分母，展示前置 `≈`）。两口径都没样本时显示 `—`。

聚合速度只能从逐请求明细算，故只覆盖最近 90 天（总览卡片同样如此，其余卡片读小时汇总）。明细表给每条请求自己的速度，不回落估算口径，无生成窗口的行显示 `—`。速度列可点表头排序（`sort=tps_visible` / `sort=tps`）。

### 积分记录

凭证额度列数字后的下箭头展开「变动流水」，记录**两次额度探测之间的净变化**，不是动作归因：签到、成长、对话都会改余额而上游不打日志，界面一律写「净变化」。首次探测只建基线；余额没变不记；余额变「未知」仍记但不填变化量；保留 90 天。

**按账本分流**：流水带 `ledger` 列（`pool` / `credits`）。CodeArts 记 `credits`，其余渠道记 `pool`。接口 `GET /api/credentials/{id}/credit-events?ledger=`（缺省 `pool`）。两本账量级与语义不同，必须分开读。

### 分组维度与重新汇总

分组统计用 tabs 切四个维度（渠道 / 模型 / 用户 / 凭证），共用一张表（渠道 / 请求 / Credit / 成本 / Token / 速度 / 平均延迟 / P95 延迟）。端点 `GET /api/stats/by-provider|by-model|by-user|by-credential`。默认按请求数降序，点列头切换排序；合并列点列头弹菜单选子列。**延迟分位可切 P50 / P90 / P95 / P99**（默认 P95，nearest-rank，n 为有值样本数）。按用户 / 按凭证只对 admin 与 operator 展示。

**口径差异**：渠道 / 模型 / 用户三维读小时汇总（永久，最近 5 分钟尚未计入）；凭证维度读明细表（只覆盖 90 天）。

**延迟两类口径**：只统计成功请求。平均延迟 = `latency_sum ÷ 成功数`（小时汇总）；分位延迟从逐请求明细算（只覆盖 90 天）。`ttfb_ms` 为 `NULL` 的两列各自独立排名。无样本显示 `—`。

**失败归因**：分组表下方的面板，`GET /api/stats/errors`（operator+）按 `(渠道, 模型, 错误类型)` 聚合窗口内 `ok=0` 的明细，给失败次数与最近失败时刻（按失败数降序，`limit` ≤ 5000）。点行即钻取到明细并加「只看失败」（`GET /api/stats/events?ok=0`）。只在 admin/operator 下渲染。`/metrics` 的 `upstream_errors_total` 仍取进程内计数，两者不合并。

**点行钻取**：点分组表任意行，明细即按该行过滤（渠道 / 模型 / 凭证，用户维度走作用域参数），条件可叠加。`GET /api/stats/events` 接受 `provider` / `model` / `credential_id`（AND）与 `ok=0`。

**明细表列合并**：Token、延迟、速度、渠道 / 凭证各并成一列，默认值一眼可读、悬停展开子项。时间列拆成两行（时刻 / 日期）。点合并列头弹出下拉选子列排序（`sort=cache_rate` / `tps_visible` / `tps` / `provider` / `credential`）。

**导出 CSV**：`GET /api/stats/events?format=csv`（仅 admin / operator），按当前时间范围 + 钻取过滤导出。UTF-8 BOM、`\r\n`、时间 ISO 8601（UTC），对以 `= + - @` 开头的文本单元格加前导单引号防公式注入。单次上限 10000 行，超出截断并带 `X-Export-Truncated: 1`。

**实时流（SSE）**：`GET /api/stats/events/stream`，展示当前筛选范围内刚落库的请求。事件在 `StatsCollector.record` 事务提交后由进程内 `LiveEventHub` 广播，前端保留最近 50 条。

- 作用域与过滤同 `/api/stats/events`；换过滤会重建连接。
- 广播 payload 不含凭证昵称；仅 admin/operator 的连接在服务端按需补 `credential_name`。
- 每订阅者有界缓冲 200 条（满则丢最旧）；并发上限 8，超出 429。
- 15 秒注释帧保活；`Cache-Control: no-store` + `X-Accel-Buffering: no`。

历史数据偏小时用 `python3 scripts/rollup_hourly.py --apply` 重算（默认预览、写库前备份、幂等）。

### 列表排序

列头可点排序：点一次用该列首次方向（数值降序、文本升序），再点切换。排序由后端完成，所有列表端点接受 `sort`（白名单键）与 `order`（`asc` / `desc`）。未知 `sort` 或非法 `order` 回落到默认排序，不报错。

适用：`/api/credentials`、`/api/users`、`/api/api-keys`、`/api/audit`、`/api/alerts`、`/api/model-catalog`、`/api/credentials/{id}/credit-events`、`/api/credentials/{id}/accounts`、`/api/stats/by-*`、`/api/stats/events`。

### 列表分页

统一分页：`第 N / M 页`、每页 20 / 50 / 100（默认 50）。扁平列表由后端分页——端点接受 `limit` + `offset` 并返回筛选后的 `total`。`/api/model-catalog` 因排序键含派生字段，先内存排序再切片，另接受 `q` 与 `only_available`。

分组类表（凭证页按渠道分组、用量统计分组表）不分页。

## 后台任务

额度探测、token 预刷新、每日签到、成长中心、活跃上报、明细清理、数据库备份、渠道模型列表刷新、模型目录刷新、运维告警、上游状态页聚合、模型主动探活共 12 类，由 `TaskRunner` 调度，失败互不影响。「任务与配置」页查看上次执行时间、最近结果与错误（30 秒刷新），运行态只存在于本次进程。

每个任务卡片带「立即运行」（admin + CSRF）：绕过时点窗口但绕过不了开关；已在跑时回 `busy=true` 不排队。

**渠道模型列表刷新**（`MODEL_CATALOG_MINUTES`，默认 30）是兜底保鲜，保证没人访问时也会更新。

**成长中心**仅 CodeBuddy。周期默认 60 分钟（`GROWTH_INTERVAL_MINUTES`）。不可逆动作由 `GROWTH_IRREVERSIBLE_ACTIONS=false` 关停。

### 活跃上报（可选，默认关闭）

`ACTIVITY_REPORT_ENABLED=true` 时，每天 `ACTIVITY_REPORT_HOUR`（默认 10 点）整点窗口为每个账号补发一条 `chat_request_send` 事件续连登。与积分、调度无关。适用于 CodeBuddy 与国际站。

> **风险**：官方条款禁止用脚本篡改活动数据，处罚为取消资格并追回礼品。默认关闭，开启前自行评估。上游改版即失效。缺少 `userId` 时上游会静默丢弃；OAuth 凭证的 `user_id` 为空时回落取 JWT `sub`，仍缺失则跳过。

### 运维告警

后台周期评估四类风险并落 `alert_events`（「运维告警」页，admin-only）：

| 规则 | 触发条件 |
|---|---|
| `pool_empty` | 可用数少于 `ALERT_POOL_READY_MIN` |
| `task_failed` | 某任务连续失败达到 `ALERT_TASK_FAILURES` 次 |
| `token_expiring` | token 剩余寿命落在 `ALERT_TOKEN_EXPIRY_HOURS` 内 |
| `error_rate` | 统计窗内失败占比达到 `ALERT_ERROR_RATE_THRESHOLD` 且样本足够 |

- **静默窗**：同一 `(规则, 对象)` 窗口内只落库 / 推送一次。
- **迟滞**：连续命中若干轮才告警，连续未命中若干轮才确认恢复。恢复时给该 `(规则, 对象)` 全部未恢复的行写 `resolved_at`，并外推 `status="resolved"` 的 webhook。
- **重启自愈**：迟滞计数进程内维护；「哪些告警未恢复」从库里重建。
- **人工处置**：「标记恢复」按钮（admin + CSRF + 审计），只标该行。
- **保留期**：同请求明细（默认 90 天）。

### 数据库备份与恢复

**自动备份**（`BACKUP_INTERVAL_MINUTES`，默认每日）把一致性快照写进 `DATA_DIR/backups/`，只保留最近 `BACKUP_KEEP` 份（默认 7）。

- 用 `VACUUM INTO` 而不是复制文件（WAL 模式下直接 `cp` 会丢未 checkpoint 的事务）。
- 落盘即 `PRAGMA integrity_check`，不过则删掉半成品。产物 `0600`、目录 `0700`。
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

恢复流程：校验完整性 → 给现有主库做 `.pre-restore-<时间戳>` 副本 → 原子替换 → 删除 `-wal`/`-shm` 残留。**恢复后重启服务。**

## 配置

完整项见 `src/config.py`（权威），每一项都已透传到 `docker-compose.yml`。容器里改监听地址用 `HOST`/`PORT`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | **必填** | 凭证加密密钥，≥16 字符；丢失 = 凭证全部作废。拒绝低熵值 |
| `ADMIN_USERNAMES` | 空 | 仅引导期：逗号分隔，首次启动提权为 `admin` |
| `USERS_FILE` | `secrets/users.txt` | 仅引导期：老式用户文件，启动时一次性导入 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 浏览器可达地址；TRAE 登录回调用；https 时下发 HSTS。不决定会话 Cookie 的 `Secure`。配成回环 / `0.0.0.0` 时回调基址改用当前请求的 origin（远程与容器部署忘改也能回调，见「部署注意」） |
| `DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `DEFAULT_MODEL` | `deepseek-v4.1-flash` | 模型为空 / `auto` 时的默认 |
| `QUOTA_PROBE_MINUTES` | `60` | 额度探测周期（下限 1） |
| `REFRESH_INTERVAL_MINUTES` | `30` | token 预刷新轮询周期（下限 5） |
| `MODEL_CATALOG_MINUTES` | `30` | 渠道模型列表兜底刷新周期（下限 5） |
| `OPENROUTER_CATALOG_MINUTES` | `1440` | 模型目录刷新周期（下限 60） |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | 模型目录来源（匿名可读）；改后需重启 |
| `USD_CNY_RATE` | `6.70` | 成本估算汇率；可热更，只影响之后写入的请求 |
| `GROWTH_INTERVAL_MINUTES` | `60` | 成长中心周期（下限 5） |
| `GROWTH_IRREVERSIBLE_ACTIONS` | `true` | 是否允许抽奖 / 连登兑换 / 开盲盒 / 消耗补登卡 |
| `ACTIVITY_REPORT_ENABLED` | `false` | 活跃上报，默认关闭 |
| `ACTIVITY_REPORT_HOUR` | `10` | 活跃上报的本地整点窗口 |
| `CHECKIN_TIMES` | `""` | 签到时点 `HH:MM` 逗号分隔；留空 = 全天每 10 分钟检查一次。可热更 |
| `CHECKIN_JITTER_MINUTES` | `0` | 签到抖动（分钟），在时点之前随机提前。可热更 |
| `LOG_BUFFER_LINES` | `500` | 服务端日志页缓冲行数；`0` 关闭该页。可热更 |
| `ALERT_ENABLED` | `true` | 运维告警开关 |
| `ALERT_WEBHOOK_URL` | 空 | 告警 Webhook，逗号分隔（≤16，仅 http(s)，拒内网 / 回环防 SSRF），带 `X-Coding2API-Signature` |
| `ALERT_INTERVAL_MINUTES` | `5` | 告警评估周期 |
| `ALERT_SILENCE_MINUTES` | `30` | 同 `(规则, 对象)` 静默窗；`0` 关闭 |
| `ALERT_FOR_TICKS` | `1` | 进入迟滞轮数 |
| `ALERT_CLEAR_TICKS` | `2` | 恢复迟滞轮数 |
| `ALERT_POOL_READY_MIN` | `1` | 池耗尽阈值；`0` 关闭 |
| `ALERT_TASK_FAILURES` | `3` | 任务连续失败次数阈值；`0` 关闭 |
| `ALERT_TOKEN_EXPIRY_HOURS` | `24` | token 剩余小时阈值；`0` 关闭 |
| `ALERT_ERROR_RATE_THRESHOLD` | `0.5` | 错误率阈值；`0` 关闭 |
| `ALERT_ERROR_RATE_MIN_REQUESTS` | `20` | 错误率最小样本数 |
| `ALERT_ERROR_RATE_WINDOW_MINUTES` | `15` | 错误率统计窗 |
| `BACKUP_INTERVAL_MINUTES` | `1440` | 备份周期（下限 60） |
| `BACKUP_KEEP` | `7` | 备份保留份数；`0` = 不清理 |
| `UPSTREAM_STATUS_ENABLED` | `true` | 上游状态页开关 |
| `UPSTREAM_STATUS_MINUTES` | `5` | 状态抓取周期（下限 1） |
| `UPSTREAM_STATUS_TIMEOUT_SECONDS` | `10.0` | 单源 HTTP 超时 |
| `MODEL_PROBE_MINUTES` | `0` | 主动探活周期（`0` = 关闭）。只探被 `blocked` 负缓存锁住的 (渠道, 模型)；成功即清负缓存，其余一律不禁用。可热更 |
| `QUOTA_EXPIRY_WINDOW_SECONDS` | `129600` | 主到期排序窗口；`≤0` 关闭 |
| `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` | `604800` | 次到期窗口；`≤0` 关闭 |
| `CONVERSATION_STICKY_SECONDS` | `3600` | 会话粘性 TTL。优先级：请求体会话标识 > 请求头 > 消息前缀指纹；pin 最高。`≤0` 关闭 |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,default,seed-code-pro-*,sagitta,aquila,glm-4.*,glm-5,glm-5-turbo,glm-5.0*,glm-5.1,glm-5v-*,kimi-k2.5,kimi-k2.6,kimi-k2-thinking,minimax-m2.5,deepseek-v3-*,hunyuan-*,hy3*,deepseek-v4-*,qwen3.7-*,auto,nemotron-3-*,glm-5.2*,nemotron-3*,lfm2*,north-mini-code,openrouter-models-router,fast,balanced,deep,primary,hy4-preview,inkling-small,minimax-m2.7,kimi-k2.*,minimax-m3,gpt-5.4,gpt-5.5,laguna*,qwen3-vl-235b,seed-2.1*,xing4.0-29b-a4,glyph-cluster,qwen3.8-flash,seed-evolving` | 模型列表黑名单（fnmatch，仅影响展示），按归一后写法匹配 |
| `MODEL_FALLBACK_GROUPS` | `""` | 跨渠道 fallback 组：`组名=成员1,成员2;…`。留空不启用 |
| `ALLOWED_HOSTS` | 空 | Host 白名单，防 DNS rebinding |
| `ALLOW_TEST_HOST` | `false` | 放行 `testserver`，仅测试用 |
| `TRUST_PROXY` | `false` | 是否采信 `X-Forwarded-For`；仅「前恰好一层受信反代」时开 |
| `ENABLE_DOCS` | `false` | 是否暴露 `/docs` 与 `/openapi.json` |
| `DUMP_REQUEST_BODIES` | `false` | 把 `/v1` 原始请求体落盘（含对话内容，仅排查） |
| `LOG_LEVEL` | `INFO` | 日志级别；审计日志是 INFO，调到 WARNING 会一并关掉 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址与端口（compose 默认 `0.0.0.0`）；`PORT` 同时决定宿主映射 |
| `CODEBUDDY_API_ENDPOINT` | `https://copilot.tencent.com` | 上游地址，改动须同步白名单 |
| `CODEBUDDY_ALLOWED_ENDPOINTS` | 官方两站（见 compose） | 端点白名单，真实 Token 只发往白名单内 |
| `CODEBUDDY_INTL_API_ENDPOINT` | `https://www.codebuddy.ai` | 国际站地址 |
| `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` | `https://www.codebuddy.ai` | 国际站白名单，刻意不含国内站 |
| `ZEN_API_ENDPOINT` | `https://opencode.ai` | Zen 上游地址 |
| `ZEN_ALLOWED_ENDPOINTS` | `https://opencode.ai` | Zen 白名单 |
| `ZEN_OPENCODE_VERSION` | `1.18.0` | 门禁伪装 UA 版本（低于阈值会被 426 拒绝） |
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
| `CODEBUDDY_CHAT_MIN_INTERVAL` | `5` | CB 聊天节流（按凭证分桶） |
| `CODEBUDDY_INTL_CHAT_MIN_INTERVAL` | `5` | CB 国际节流，独立 |
| `ZEN_CHAT_MIN_INTERVAL` | `0` | Zen 节流 |
| `KILO_CHAT_MIN_INTERVAL` | `0` | Kilo 节流 |
| `QODER_CHAT_MIN_INTERVAL` | `5` | Qoder 节流 |
| `CODEARTS_CHAT_MIN_INTERVAL` | `5` | CodeArts 节流 |
| `CODEARTS_MAX_CONCURRENCY` | `3` | CodeArts 每账号在途并发上限；`0` 关闭 |
| `CODEARTS_REQUEST_WINDOW_SECONDS` | `60` | CodeArts 账号滑动窗口；`0` 关闭 |
| `CODEBUDDY_SANITIZE_CHANNEL_MARKERS` | `true` | 出站正文命中「伪装官方客户端」指纹串时替换（上游 11128 风控） |
| `REFRESH_SKEW_HOURS` | `24` | token 到期前该窗口内预刷新；短寿命渠道自行封顶 |
| `TOKEN_EXPIRY_WARNING_SECONDS` | `3600` | token 到期预警阈值；`≤0` 关闭。纯展示 |
| `PACER_MIN_SECONDS` / `PACER_MAX_SECONDS` | `5` / `20` | 全局节流随机等待区间 |
| `AUTO_CONTINUE_MAX` | `10` | `finish_reason=length` 时同凭证自动续写次数；`0` 关闭 |
| `CONTEXT_COMPRESS_ENABLED` | `true` | 按模型输入上限裁剪过长对话 |
| `CONTEXT_COMPRESS_RESERVE_TOKENS` | `4096` | 为回复预留的输出 token |
| `CONTEXT_COMPRESS_MIN_KEEP_MESSAGES` | `4` | 无论多长都保留的最近消息条数 |
| `CONTEXT_COMPRESS_SAFETY_RATIO` | `0.95` | 压缩预算占模型上限的比例 |
| `UPSTREAM_COMPLETE_TIMEOUT_SECONDS` | `600` | 非流式聚合整体超时；`≤0` 关闭 |
| `STREAM_FIRST_EVENT_TIMEOUT_SECONDS` | `0` | 首事件超时（出帧前可换号）。**默认必须保持关闭**，推荐 60。可热更 |
| `STREAM_IDLE_TIMEOUT_SECONDS` | `0` | 流内空闲超时（已出帧只断流不换号），推荐 300。可热更 |
| `SHORT_COOLDOWN_WAIT_SECONDS` | `0` | 全池短冷却时等待续上；`0` = 快速失败。可热更 |
| `NONSTREAM_KEEPALIVE_SECONDS` | `0` | 非流式出口 N 秒未完成则转 chunked 吐空白心跳（JSON 前导空白合法）；`0` = 关闭，仅在 CDN 后需要，推荐 15。可热更 |
| `MODEL_ALIASES` | `""` | 自定义别名；留空不启用。可热更 |
| `MAX_INFLIGHT_PER_CREDENTIAL` | `0` | 单账号在途上限；`0` = 不限。600s TTL 兜底 |
| `METRICS_ENABLED` | `true` | 是否暴露 `/metrics`（不鉴权）。可热更 |
| `REQUEST_LOG_ENABLED` | `true` | 请求级结构化日志（logfmt）；不写 query / 请求体 / 用户名。可热更 |
| `FORCE_DISABLE_THINKING` | `false` | 出站剥离思考内容，只过滤响应不改请求。可热更 |
| `REASONING_EFFORT_MAP` | `""` | 出站档位映射 `渠道=from:to;*=from:to`（渠道取 `KNOWN_PROVIDERS`）；目标写 `none` = 删键。可热更 |
| `ANTHROPIC_THINKING_EFFORTS` | `""` | Anthropic `thinking.budget_tokens` → 上游 `reasoning_effort`。可热更 |

### 管理台热更（「任务与配置」页）

上表中带「可热更」语义的 **60 项**可不改 `.env`、不重启，直接在「任务与配置」页修改（完整列表见该页）。

要点：**优先级 `DB 覆盖值 > .env`**——改过后 .env 对该项不再生效，页面标「DB 覆盖」，点「恢复默认」删掉覆盖行才回落 .env。值存 `runtime_settings` 表，新增可热更项无需迁移；白名单外的 key、非法类型 / 越界值写入前即拒。启动期项（`APP_SECRET` / `PORT` / `DATA_DIR` / `USERS_FILE` / `OPENROUTER_MODELS_URL` / 上游端点白名单）不在白名单。接口 `GET /api/settings` 读快照、`PUT /api/settings` 写（admin + CSRF），传 `null` 恢复默认。该页同时展示后台任务运行态（`GET /api/tasks`，进程内，30 秒刷新）。

## 部署注意

- **挂载目录属主**：容器内以 uid 1001（`appuser`）运行，`./data` 与 `./secrets` 必须可写 / 可读：

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **时区**：镜像默认 `TZ=Asia/Shanghai`，`.env` 里的 `TZ` 会透传。签到 / 额度周期按「今天 9 点」判断，务必与宿主一致。
- **TRAE 登录回调地址**：默认 `PUBLIC_BASE_URL=http://127.0.0.1:8000` 只在本机访问时可用。远程 / 容器部署请改成浏览器能打开的地址；**忘记改也不会静默失败**——基址落在回环 / `0.0.0.0` 时，回调改用当前请求的 origin（LAN IP 或隧道域名），并在「添加凭证」对话框里显示实际回调地址，授权超时也会点名检查 `PUBLIC_BASE_URL`。
- **端口**：compose 里 `PORT` 同时决定容器内监听端口与宿主机映射端口。
- **改代码后重建镜像**：`docker compose up -d` 不带 `--build` 会用本地已有镜像，跑旧代码。改 `src/` 或前端后务必 `docker compose up -d --build`。

### 升级后必须重启后端

**改动 `src/` 后必须重启进程**，否则出现「新前端 + 旧后端」错配：页面能打开，但新端点全部 404。前端不需重启（后端每次请求现读 `web/dist`），后端不是。

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

后端把前端产物放在 `/` 直接服务（每次请求现读磁盘），**只改前端时构建完刷新即可**。`localhost:5173` 是 Vite 开发态，两者可并存。

```bash
./scripts/build-web.sh            # 仅在产物过期时构建
./scripts/build-web.sh --force    # 无条件重建
```

> **`web/.npmrc` 不能删**：它关掉了 `auto-install-peers`（`@lobehub/icons` 会把 `@lobehub/ui` / `antd` 拖进来）。该设置与 lockfile 的 `settings.autoInstallPeers` 强耦合，`Dockerfile` 也必须 `COPY web/.npmrc`，否则 `--frozen-lockfile` 报 `ERR_PNPM_LOCKFILE_CONFIG_MISMATCH`。

> **部署相关操作记在本地文档**：进程管理方式、路径与工具版本见 `docs/local-environment.md`（已 gitignore）。

## 公网入口（VPS + frp 反向隧道）

家宽在 CGNAT 后没有可入站地址，公网入口只能由内网侧主动连出。Cloudflare Tunnel 免费版会把大陆流量绕到洛杉矶（实测 1.1–2.4s、丢包约 60%）；「境外 VPS 做入口 + frp 隧道回源」可降到 80–150ms。只改入站链路，出站仍走 `PROVIDER_PROXIES`。

```
客户端 ──443/TLS──> VPS(nginx) ──frp 隧道──> 家中 Mac:8000
```

产物在 `deploy/frp/`、`deploy/nginx/`、`deploy/systemd/frps.service`、`deploy/launchd/com.coding2api.frpc.plist`（frp v0.71.1 TOML 格式）。

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

`frpc.toml` 用 `{{ .Envs.FRP_SERVER_ADDR }}` 模板取值；地址与 token 由 plist 注入，token 落在 `secrets/frpc-token`（600，已 gitignore）。

`.env` 要同步三处：

| 变量 | 值 | 漏了会怎样 |
|---|---|---|
| `ALLOWED_HOSTS` | 追加隧道域名 | 隧道侧全部 400 `invalid host header` |
| `PUBLIC_BASE_URL` | `https://<隧道域名>` | TRAE 登录回调基址错、不下发 HSTS |
| `HOST` | 保持 `127.0.0.1` | 隧道回源到本机回环即可 |

`TRUST_PROXY`：VPS 上恰好一层 nginx 可置 `true`，让 Key 的 `allowed_ips` 看到真实 IP。若仍需局域网直连入口，保持 `false`。

## 日志

应用只写 **stdout / stderr**，不写文件、不自行轮转。

| 部署形态 | 日志到哪 | 轮转 | 需做什么 |
|---|---|---|---|
| Docker / compose | `docker logs coding2api` | json-file（10m × 5） | 无需操作 |
| Linux（systemd） | `journalctl -u coding2api` | journald 自带 | 无需操作 |
| Linux（非 systemd） | 重定向到文件 | `logrotate` | `sudo cp deploy/logrotate/coding2api /etc/logrotate.d/` |
| 裸跑 | 终端 stderr | 无 | 自行重定向并轮转 |

级别用 `LOG_LEVEL`（默认 `INFO`）。**审计日志是 INFO 级**，调到 `WARNING` 会一并关掉。

### 请求级结构化日志

每个业务请求（`/api/*`、`/v1/*`、`/authorize`）额外写一行 logfmt：

```
event=request request_id=9f2c… method=POST route=/v1/chat/completions status=200 duration_ms=1234
```

- `route` 是路由模板（`/api/credentials/{credential_id}`），资源 ID 不进日志；未匹配到路由时回落为清洗过的原始路径。
- 不写 query string、请求体、响应体、请求头、用户名；身份靠 `request_id` 去 `usage_events` 关联。
- `request_id` 与响应头 `X-Request-ID` 同值。
- 级别：`5xx` 记 WARNING，其余 INFO。
- 不记静态资源与探针（`/health`、`/healthz`、`/metrics`）。
- uvicorn 自带 access log 已关闭。
- 开关 `REQUEST_LOG_ENABLED`（默认 `true`，可热更）。

> 容器里 `data/dumps/`（`DUMP_REQUEST_BODIES=true` 写入）不在 docker 日志体系内，应用自行保持最多 200 份。

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
| [`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html) | 请求主链路时序图 |
| [`diagrams/coding2api-credential-lifecycle.html`](diagrams/coding2api-credential-lifecycle.html) | 凭证调度状态机 |

## 授权协议

MIT，见 [LICENSE](LICENSE)。借鉴的上游项目署名见 [NOTICE](NOTICE)。
