# Coding2API
把 **CodeBuddy**、**TRAE SOLO**、**OpenCode Zen**、**Kilo Gateway**、**Qoder**、**CodeArts** 六个上游渠道封装为 OpenAI 兼容 API，提供公共凭证池、统一模型名与调度、按人用量统计。

> [!WARNING]
> 逆向接口，仅供学习研究，未做安全审计；公网部署需反向代理 + 鉴权 + IP 白名单。

## 特性
- **OpenAI 兼容出口**：`/v1/chat/completions`（流式 + 非流式）、`/v1/responses`（Codex CLI）、`/v1/messages`（Anthropic / Claude Code）、`/v1/models`、`/v1/user/balance`（DeepSeek 兼容余额）
- **统一调度**：扁平模型名按健康度自动选号，`模型@渠道` 强制指定；候选含免费渠道（x0）时**免费优先**（免费档一条中等回复省下的积分多于付费号 36h 内到期的积分），之后按三态健康度 + 分级冷却避开坏号，健康度打平则**余额多者优先**。模型级限流或「该渠道无此模型」只避让该模型（同账号其他模型立即可用）；某模型**只有**一个渠道持有且它正在模型级冷却时，不扇给不认它的其他渠道（只是白打并写负缓存），直接回「该模型暂不可用，换模型即可」。同一对话多轮粘住同一凭证，出错才轮换
- **到期额度优先消化**：主窗口 36h 内将过期的额度多者先用（避免过期浪费），打平再比 7 天窗口；额度单位统一为积分
- **模型列表按渠道凭证加载**：只展示**当前有可用凭证**的渠道（未接入不出现幽灵模型；暂停或凭证失效时其模型暂时消失，恢复即回来）。模型目录落盘 + 启动同步回灌，重启第一秒归属表即可用（详见下文）
- **公共凭证池**：admin 集中维护、全员共享、加密入库（`APP_SECRET`）；设备码登录、多账号切换、额度探测、每日签到、token 预刷新
- **三角色账号体系**：`admin` / `operator` / `viewer`，用户存 SQLite；一次性激活链接自设密码（无共享初始密码）、首登强制改密、改角色/停用即时吊销会话；登录与写操作留审计
- **成长中心**（仅 CodeBuddy）：自动领 Buddy 旅行礼物、派 Buddy、领取新任务与任务奖、断登补登、连登奖励兑换、开盲盒；不可逆动作可用 `GROWTH_IRREVERSIBLE_ACTIONS=false` 关停；管理台可手动执行并查看逐条结果
- **脱敏统计**：不存对话内容；明细 90 天、小时汇总永久；按人/渠道/模型可视化；另按 OpenRouter 刊例价估算**成本**（人民币为主，Q70）
- **管理台安全加固**：登录限流、CSRF 校验、请求体上限、Host 白名单

六个渠道的接入方式与注意事项见下文「[渠道](#渠道)」；协议层实现（私有信封、签名、门禁伪装等）见 [`TECHNICAL.md`](TECHNICAL.md) §3.14–§3.17。

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

> `scripts/create_user.py` 直接写 SQLite（`--db` 或 `DATA_DIR`，默认 `data/coding2api.sqlite3`）。老部署若已有 `secrets/users.txt`，启动时自动导入一次（已存在的不覆盖），也可继续用 `scripts/hash_password.py` 写文件作引导。

## 用户与角色
账号存在 SQLite 的 `users` 表里，管理台「用户管理」页（仅管理员可见）负责日常增删改。三档角色：

| 角色 | 能做什么 |
|---|---|
| `admin` | 用户管理、任务与配置、凭证读写、全部统计、审计日志 |
| `operator` | 凭证读写（含导入 / 删除 / 启停 / 签到 / 成长 / 切换账号）、全部统计 |
| `viewer` | 只读：看仪表盘、凭证列表、统计与 Playground |

**加人**：管理员在「用户管理」填用户名 + 角色，创建后得到**一次性激活链接**（有效 24 小时，只显示这一次）。把链接经可信渠道发给本人，对方打开 `/activate` 自设密码即可登录——系统里不存在「管理员知道但用户不知道」的初始密码。忘记密码同理：点「重置密码」生成新链接。

**改角色 / 停用**：即时生效——立刻作废该账号所有已签发会话 Cookie（重新登录即可）。停用是默认的「删人」方式（可逆，用量统计仍归属该用户名）；**硬删只在 CLI**（`scripts/create_user.py <用户名> --delete --force`），因为硬删会让 `usage_events` 留下查不到用户名的孤儿记录。

**自助改密**：右上角用户菜单 →「修改密码」，需要当前密码。

**防锁死**：不能停用/降级**最后一个活跃管理员**，也不能对自己降级或停用——否则下一次请求就没人能管了。要交接权限，先加另一个 `admin` 再降自己。

**CLI 兜底**（无 Web 场景 / 全部管理员失联时）：

```bash
python3 scripts/create_user.py alice --role admin   # 交互输入密码
python3 scripts/create_user.py --list
python3 scripts/create_user.py alice --delete --force   # 硬删，不可逆
python3 scripts/create_user.py --db data/x.sqlite3 ...  # 指定库
```

`ADMIN_USERNAMES` 与 `secrets/users.txt` 只在**引导期**起作用（首个管理员、老部署升级）：启动时把 `users.txt` 里的用户导入 DB（已存在的用户名不覆盖），再把 `ADMIN_USERNAMES` 点名的人提权为 `admin`；此后两者都不再是角色来源。

## 审计
「审计日志」页（仅管理员可见）记录登录与写操作，可按操作者/动作筛选、分页查看：**登录**（成败都记——失败留痕正是审计的价值：「谁在什么时候试了哪个账号」）、**账号变动**（新建、激活、改角色、禁用/启用、改密、重置密码、硬删）与**凭证写操作**（导入、删除、启停、指定 pin、复活、切换账号）。每条含时间、操作者、动作、对象、说明与来源 IP。**绝不包含密码或令牌明文**（一次性激活令牌明文只在创建响应出现一次，库里只有 SHA-256 摘要）。保留期与请求明细一致（默认 90 天），由后台清理任务裁剪。

### Docker
本地构建：

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
# 或指定版本：docker pull ghcr.io/robbsluo/coding2api:v0.4.1
```

推送新版本：打 tag `v*` 推到 main 即触发 publish workflow（见 `.github/workflows/publish.yml`），同时打 `<tag>` 与 `:latest` 到 GHCR。也可在 Actions 页面手动触发（填版本号）。

## 使用
1. 「凭证管理」添加凭证：CodeBuddy 走设备码登录（或粘贴 `{"token":"..."}`）；TRAE 粘贴凭证 JSON（`accessToken`/`uid`/`refreshToken`）或回调链接（凭证里的 `apiHost` 只接受官方地址，其他值拒绝导入）；Qoder 走设备码登录；CodeArts 走 OAuth2 授权码登录（授权后把打不开的本地回调链接粘回页面）。OpenCode Zen / Kilo Gateway 无需添加——启动时自动出现对应虚拟凭证（被删除可在同面板点「添加」补回）
2. 「API Key」创建 `sk-...`（明文仅显示一次）
3. 调用：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'
```

`模型@渠道` 强制指定上游（如 `glm-5.2@trae`、`mimo-v2.5-free@zen`），不写则自动选健康渠道。

**模型名三字段**：六条渠道（CodeBuddy / TRAE / Zen / Kilo / Qoder / CodeArts）每个模型统一为三个字段——

1. **原代号**（`raw_id`）：渠道请求时真正发的 key，**永不改动**。各渠道内部代号互不相同（Qoder `kmodel_latest` = TRAE `kimi-k3-1`），也可能带命名空间前缀（Kilo 的 `kilo-auto/free`、`stealth/space-bunny-alpha`）。
2. **归一键**：剥掉免费档标记（`-free` / 路径段 `free`）与命名空间前缀，再 slug 化——`kilo-auto`、`longcat-2.5-preview`。模型列表按它判断**哪个是同一个模型**并合并，故 Zen 的免费档能与 TRAE 的付费档并成一条。
3. **展示名**（`name`）：清洗后的可读文本，如 `LongCat 2.5 Preview`、`Kimi K3`。上游给了可读名就用（Qoder 的 `Qwen3.8-Max` → `Qwen3.8 Max`），没有就从原代号派生；品牌与缩写按官方写法纠正（`DeepSeek` / `MiMo` / `GLM` / `GPT`）。

`GET /v1/models` 里**所有条目的 `id` 都是归一键**（`kimi-k3`、`qwen3.8-max`、`longcat-2.5-preview`、`kilo-auto`）——去 free、去厂商前缀后的干净 slug，六条渠道一个口径。转发到某渠道时自动换回该渠道原代号，`by_provider.{渠道}.raw_id` 可见。**原代号、展示名、归一键三种写法都能直接请求**（`kmodel_latest`、`Kimi K3`、`kimi-k3` 指向同一模型）。同渠道内归一键重复时退回原代号，避免撞名误并。任意 OpenAI 兼容客户端可直接接入（Base URL `http://127.0.0.1:8000/v1`、Key 用 `sk-...`、模型名以 `GET /v1/models` 为准）；「Playground」页用登录会话直接测试，无需 API Key。模型列表只展示**当前有可用凭证**的渠道，详见下文。

几点要知道：**流式带 usage 帧**——收尾时在 finish chunk 之后、`data: [DONE]` 之前补一帧 `choices: []` 的 usage（OpenAI 标准形态），只从流里读用量的客户端（如 pi-ai / DSH）据此显示 tok/s、上下文占用率与会话 token 统计；上游没上报时不补（不发 0 占位），非流式出口的 `usage` 同理，未上报字段一律为 `null`。**思考 token 需客户端带 `reasoning_effort`**——CodeBuddy 渠道在客户端**未给**时补 `medium`（上游缺此字段会把整段思考以「可见推演」写进正文，思考与正文混成一块）；显式给的值（如 `low`）原样透传、不被覆盖，可据此压低思考量。

## 渠道
六个上游渠道的接入方式与各自注意事项（协议层实现细节见 [`TECHNICAL.md`](TECHNICAL.md) §3.14–§3.17）。

> CodeBuddy / TRAE / Qoder / CodeArts 需填凭证；**`OpenCode Zen` 与 `Kilo Gateway` 是无凭证的免费层渠道**——池里是一条虚拟占位行（让调度 / 冷却 / 统计照常工作），额度列显示「免费层（无额度接口）」，健康度恒为「无探测」（免费层无额度接口，探也没用；区别于付费渠道探测失败的「未探测」），管理台不提供「探测」按钮（未知 ≠ 耗尽）。虚拟行**删除后重启会复活**，也可在「凭证管理 → 登录渠道账号」点「添加 OpenCode Zen」/「添加 Kilo Gateway」补回；永久停用请用「暂停」（见「暂停单个凭证」），暂停也会让它暂时从模型列表消失。

### OpenCode Zen 免费层（第三个渠道 `zen`）
`zen` 渠道接的是 [opencode.ai/zen](https://opencode.ai) 的**免费模型**，无需账号或登录。上游清单混着付费模型且不带免费标记，本服务按 `-free` 后缀收窄候选、再逐个探活，**只把匿名真正可用的免费模型**放进 `GET /v1/models`（每次动态拉取并探活，判活缓存 30 分钟，不维护静态白名单；免费模型显式标 **x0**）；用 `模型@zen` 强制指定，或由调度器自动路由。上游清单不提供模型名（`owned_by` 恒为 `opencode`），故展示名由模型 id 派生（`longcat-2.5-preview-free` → `LongCat 2.5 Preview`）。`-free` 后缀只是上游的可用性命名约定，不是模型名的一部分，展示与归一时都去掉（见上文「模型名三字段」）。

**免费层门禁**：上游要求伪装成官方客户端（UA 版本、会话头、`stream:true`、tools 含 `bash`/`read`），本服务自动满足；注入的 `bash`/`read` 是空壳，模型真调用时这类 tool_call 被过滤掉，不泄漏你没声明过的函数，你声明了则原样透传。**上游会改**：门禁阈值与免费清单都可能变，UA 版本用 `ZEN_OPENCODE_VERSION` 调，端点用 `ZEN_API_ENDPOINT`（须在 `ZEN_ALLOWED_ENDPOINTS` 内）；付费模型即使强制 `模型@zen` 也只报「不可用」，不拖垮渠道。

### Kilo Gateway 免费层（第四个渠道 `kilo`）
`kilo` 渠道接的是 [Kilo Gateway](https://kilo.ai)（`api.kilo.ai/api/gateway`）的**免费模型**，无需账号或登录。它对外是**标准 OpenAI 兼容协议**（`/chat/completions` + `/models`），无私有信封、无门禁伪装；上游 `/models` 每条带权威 `isFree` 布尔（实测 395 个模型中 17 个为 true），本服务**据此直接过滤**免费集、**不做探活**——探活白耗本就极小的免费配额，且结果随上游池波动不稳定；免费集随上游增删自动跟随，不维护静态白名单。免费模型显式标 **x0**；用 `模型@kilo` 强制指定，或由调度器自动路由。

**额度与限流**：免费层额度很小（网关级约 200 请求/小时/IP），且免费池实为 OpenRouter 免费池的转发（上游 429 原文含 `limit_source: upstream_provider_shared_pool` 并**点名具体模型**），随上游池波动；上游 **429 与 502/503/504 都是模型级**——只冷却被点名的那个模型（其余免费模型照常可用），冷却期内再请求它返回 `no_healthy_credential`，本服务不自建熔断。**上游会改**：端点用 `KILO_API_ENDPOINT`（须在 `KILO_ALLOWED_ENDPOINTS` 内）；付费模型即使强制 `模型@kilo` 也只报「不可用」，不拖垮渠道。

### Qoder（第五个渠道 `qoder`，真实账号）
`qoder` 渠道接的是 [Qoder](https://qoder.com)，**需要真实账号**：在「凭证管理 → 登录渠道账号」点「登录 Qoder」，按提示在浏览器完成设备码授权即可（授权链接由服务端生成，登录全程后端轮询完成，Token 不经过浏览器）。上游是私有 COSY 协议（自定义 Base64 编码 + 信封式 SSE），本服务负责签名与解包，对外仍是标准 OpenAI 协议。

**额度与签到**：支持额度探测（`GET /api/v2/quota/usage`）与每日签到（积分可累积）。签到自 2026-10 起为**活动制**（`/sash/api/v1/me/campaigns`，需带 `Cosy-ClientType`），旧的 `daily-check-in` 仅作回退；本服务自动查询并领取可领活动，由后台任务执行、管理台可手动触发，当日已签（含重复领取 / 同一自然人已领）归一为「已签」而非失败。**签到活动每日 10:00（UTC+8）刷新**（领取后 30 天有效），后台任务按该窗口而非自然日封账：凌晨看到的「已领」只封到当天 09:59，10:00 新一轮出现后自动补签，无需人工点。新协议不提供连续天数，界面不再显示「连续 N 天」。**国际版没有签到接口**（该端点 404），签到记为「本区域无此接口」，不是错误。

**端点与节流**：默认国内版（`openapi.qoder.com.cn` + `gateway.qoder.com.cn`），国际版改 `QODER_API_ENDPOINT=https://openapi.qoder.sh` 并确认 `QODER_ALLOWED_ENDPOINTS` 含国际版域名——签名携带完整 `cosy-*` 头，白名单防止误发往未授权主机。默认 `QODER_CHAT_MIN_INTERVAL=5`（真实账号渠道，独立节流器，与其它渠道互不排队），可在「任务与配置」热更。

**上游节点故障**：Qoder 有时把自身推理节点故障包成 400（报错原文形如 `[FAIL]node:… msg:Execution failed`）。本服务识别为**模型级瞬时故障**——只冷却该模型（同账号其他模型照常可用），耗尽候选时返回「该模型暂时不可用」的 503（错误码 `no_healthy_credential`），而非误报「模型不存在」或「无可用凭证」；稍后重试通常自愈。

### CodeArts（第六个渠道 `codearts`，真实账号）
`codearts` 渠道接的是 [华为云 CodeArts](https://codearts.huaweicloud.com)，**需要真实账号**：在「凭证管理 → 登录渠道账号」点「登录 CodeArts」，浏览器完成 OAuth2 授权后会跳到一个打不开的本地地址（`http://127.0.0.1:12800/oauth/callback?code=…`）——把地址栏里的**整条链接复制粘贴**到页面出现的输入框里，由服务端换取 STS 凭证（AK/SK + security_token + refresh_token + DPoP 私钥）并加密入库。

**令牌刷新是刚性的**：refresh_token 与 `client_id=codearts-agent`、DPoP 私钥三者绑定且**一次性**——刷新后必须回写新 refresh_token，否则账号失效；由 token 预刷新任务自动完成（DPoP ES256/P-256 签名），即该渠道的「保活」手段。为降低烧票风险，预刷新窗口封顶 45 分钟且必须**宽于**轮询周期（`REFRESH_INTERVAL_MINUTES`，默认 30 分钟）——否则窗口整轮漏过、凭证拖到到期才刷（全局 `REFRESH_SKEW_HOURS` 默认 24h ≫ 凭证 2h 寿命，不封顶会每轮都刷）。续期凭据一旦被上游作废（票被消费 / 绑定项不符），该凭证标记「需重新登录」并停止自动重试，重新登录后按状态列「恢复」解除。

**每日签到**：上游有「每日签到领 1000 积分」活动（`GET /v1/ops/delivery` → `POST /v1/ops/claim` → `POST /v1/ops/confirm`），2026-12-31 截止。后台任务每日自动领取，管理台可手动触发；`claim` 后**必须** `confirm`（上游 `CLAIMED` 是「已领未确认」而非终态），本服务两步一起做，已领未确认自动补确认，当日已领归一为「已签」而非失败。**这套积分与每日 token 池是两套并行的账**：token 池给福利模型（`deepseek-v4.1-flash` 等）用，积分给系统内置模型（GLM-5.2 / OpenPangu 等）用，互不通气。

**优先消耗**：每日池用完即弃，故当日剩余登记为「次日本地 0 点到期」的到期额度，调度器优先排在其它渠道之前——只要 CodeArts 还有额度就先走它，用尽后自动回落。上游按 token 计量，管理台统一显示为**积分**（1 积分 = 10000 token，每日池满额 = 1000 积分）；升级前落库的历史数据由 `scripts/convert_codearts_credit_unit.py` 一次性折算。**该「积分」是为跨渠道排序合成的折算值，与上游签到/套餐积分同名但不同物**，不要混。

**签名 / 累计全文 SSE / 节流与并发**：上游要求华为云 `SDK-HMAC-SHA256`（AK/SK + `X-Security-Token`），白名单 `CODEARTS_ALLOWED_ENDPOINTS` 必须含 snap 引擎、STS、福利网关与门户四个主机，改 `CODEARTS_API_ENDPOINT` 时同步调整。流式 `text` 是**累计全文（替换语义）**而非增量，本服务在解析层还原为增量事件，对客户端透明。默认 `CODEARTS_CHAT_MIN_INTERVAL=5`（独立节流器）；上游硬限**每账号并发会话数 3**，故配在途上限 `CODEARTS_MAX_CONCURRENCY` 与滑动窗口 `CODEARTS_REQUEST_WINDOW_SECONDS` 两项（口径见配置表），把突发也挡在 `400 TM.00001041` 之前。均可在「任务与配置」热更。

### 按渠道出站代理（可选）
`PROVIDER_PROXIES` 给每个渠道单独指定出站代理，适合「某渠道需经代理才能访问」的场景：

```
PROVIDER_PROXIES="codebuddy=http://127.0.0.1:7890;qoder=socks5://127.0.0.1:1080"
```

格式 `渠道=代理URL;渠道2=代理URL2`，渠道取 `codebuddy/trae/zen/kilo/qoder/codearts`，协议支持 `http/https/socks5/socks5h`（SOCKS 由 `httpx[socks]` 提供），留空 = 全部直连（默认，行为不变）。作用于该渠道**全部出站请求**（聊天流、额度/模型拉取、后台任务（签到/成长/刷新/活跃上报）与 OAuth 登录）。**启动期项**：代理作用于连接池，改后需重启后端，不做热更（与上游端点同类）。**严格解析**：未知渠道 / 非法协议 / 缺 `=` 一律启动失败——代理常带合规/隐私意图，「以为走了代理其实直连」比启动报错更糟。环境变量 `HTTP_PROXY` 等一律**不采信**（`trust_env=False`，防部署环境全局代理意外劫持带 Token 的上游请求），只有这里显式配置才生效。

### Responses API（Codex CLI）
`POST /v1/responses` 提供 Responses 子集，供 [Codex CLI](https://github.com/openai/codex) 这类只走 Responses 的客户端接入。与 `/v1/chat/completions` 共用同一套选号 / 冷却 / 轮换 / 统计与会话粘性，只换入站映射与出口翻译（实现与取舍见 [TECHNICAL.md §3.7](TECHNICAL.md)）：

```bash
export CODING2API_KEY=sk-你的key
codex -c "model_providers.coding2api={ name='coding2api', base_url='http://127.0.0.1:8000/v1', wire_api='responses', env_key='CODING2API_KEY' }" \
      -c model_provider=coding2api \
      -c model='glm-5.2' \
      '你的任务'
```

（`-c key=value` 的写法与字段名取自 Codex 仓库自带的 `codex-rs/responses-api-proxy/README.md`，非凭记忆。）

支持文本、流式正文、思考摘要（`reasoning` item）、函数工具调用与 `finish_reason=length` → `response.incomplete`。`text.format` 映射到 chat 的 `response_format`（`text` / `json_object` 直通，`json_schema` 展开成 `{type, json_schema:{name, description?, schema, strict?}}` 嵌套结构），结构化输出不再被静默吞掉。**不支持** `store=true`、`previous_response_id`（服务端无状态，不假装支持）、Responses 私有工具（`web_search` / `computer` / `custom` 等）——一律显式 400，不静默降级。`include=["reasoning.encrypted_content"]`（Codex 每轮必带）接受但忽略，本网关不产加密推理内容。

> 验证边界：开发环境无 Codex CLI；协议形状取自官方 `openai` SDK 类型，以其为客户端跑通全部契约并对真实上游冒烟，未经真实 Codex CLI 端到端验证。

### Anthropic Messages API（Claude Code）
`POST /v1/messages` 提供 Anthropic Messages 子集，供 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 这类只走 Anthropic 协议的客户端接入。与 `/v1/chat/completions` / `/v1/responses` 共用同一套选号 / 冷却 / 轮换 / 统计与会话粘性，只换入站映射与出口翻译（实现见 [TECHNICAL.md §3.18](TECHNICAL.md)）：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN=sk-你的key     # 或 ANTHROPIC_API_KEY（走 x-api-key 头）
claude
```

**鉴权**：`x-api-key`（`ANTHROPIC_API_KEY`）与 `Authorization: Bearer`（`ANTHROPIC_AUTH_TOKEN`）都接受。**流式**：`message_start` → `content_block_start/delta/stop` → `message_delta`（含 `stop_reason` 与 usage）→ `message_stop`；无 `[DONE]` 哨兵，`message_stop` 即流结束，thinking 块在 `content_block_stop` 前补 `signature_delta`。**缓存命中**：出口 usage 按 Anthropic 语义拆两笔——`input_tokens` 只计未命中，命中另记 `cache_read_input_tokens`（内部 `input_tokens` 是 OpenAI 口径的 `prompt_tokens`，含命中），Claude Code 据此显示缓存读 token，不再恒为 0。**非流式**：复用 `executor.complete` 后转 `message` 形状。**`count_tokens`**：`POST /v1/messages/count_tokens` 本地估算输入 token（不转发上游，口径与上下文压缩共用）。**不支持**：图片 / 文档块、Anthropic 服务端工具（`web_search` / `computer` 等）一律显式 400，不静默降级。

> 验证边界：协议形状取自官方 `anthropic` Python SDK 类型并以其为客户端跑通全部契约，未经真实 Claude Code 端到端验证。

### 余额查询
`GET /v1/user/balance` 兼容 DeepSeek 余额接口的响应形状，Bearer `sk-...` 鉴权，供 Cherry Studio / cc-switch 等客户端显示余额。余额来自凭证池的额度探测缓存（`QUOTA_PROBE_MINUTES` 周期刷新），按可用凭证汇总，单位为上游 credits：

```bash
curl http://127.0.0.1:8000/v1/user/balance -H "Authorization: Bearer sk-你的key"
```

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
`GET /health` 只回 `{"status":"ok"}`（进程存活，适合容器存活探针）；`GET /healthz` 额外给出凭证池计数，供外部监控在池子耗尽时提前告警——那是「服务活着但用不了」的状态，存活探针看不出来。两个端点都不鉴权。

```json
{
  "status": "ok",
  "service": "coding2api",
  "version": "0.4.1",
  "credentials": {"total": 5, "ready": 4, "cooling": 1, "paused": 0, "disabled": 0}
}
```

五类计数互斥且合计 = `total`，与调度器同一口径（`disabled` / `paused` / `cooling` 依次优先归入各自桶，其余为 `ready`）。`ready=0` 时对话请求直接返回 503，值得配置告警。

### API Key 的渠道绑定、模型白名单、来源 IP 与到期时间
创建 Key 时可限定渠道、模型、来源 IP 与到期时间，适合「按出口分发 Key」（如一个给团队、一个只给某服务器、一个给试用者且带到期）。

**渠道绑定**：选 CodeBuddy / TRAE / OpenCode Zen / Kilo Gateway / Qoder / CodeArts 后，该 Key 只在该渠道凭证里选号；模型属另一渠道时直接 400 并指出实际归属（不静默改道，也不白打上游）。留空 = 自动（默认，跨渠道选健康凭证）。`模型@渠道` 与绑定冲突时同样 400。**来源 IP 白名单**：逗号分隔的 IP 或 CIDR（如 `203.0.113.9,10.0.0.0/8`），留空 = 不限制；写入时校验并规范化（`10.0.0.1` → `10.0.0.1/32`），非法值当场 400，来源不在白名单返回 403。**模型白名单**：逗号分隔的模型名或 fnmatch glob（如 `glm-*,kimi-k3`），留空 = 不限制；匹配不区分大小写，`模型@渠道` 后缀不参与匹配，白名单外的模型返回 400，`/v1/models` 也只列白名单内模型。**到期时间**：epoch 秒（如 `expires_at`），留空 = 永不过期，到期后该 Key 立即 401（与「Key 不存在」统一文案，不泄露可枚举信息）。**默认不采信 `X-Forwarded-For`**（客户端可写，信它白名单形同虚设）；仅 `TRUST_PROXY=true` 时按 XFF 判定、取**最后一个**条目（紧邻本服务的受信代理所见的地址），故只适用于「本服务前恰好一层受信反代」（多层反代或直连保持默认 `false`）。

### 暂停单个凭证
凭证列表行内菜单的「暂停」只把该凭证摘出**对话流量**：后台的额度探测、token 预刷新、每日签到、成长中心、活跃上报照常运行（这些任务只认系统硬禁用 `disabled`）。适合「先不接聊天、但积分继续领」；「取消暂停」立即放回池子。与状态列的「已禁用」不同——那是渠道判定会话失效后的系统禁用，需重新登录后用「恢复」解除。同一开关也在 `POST /api/credentials/{id}/toggle`。

> OpenCode Zen / Kilo Gateway 是没有凭证的渠道，池里是一条虚拟占位行。**暂停它是永久停用的唯一方式**——删除后下次启动会被种子重新补上。

暂停也会把该渠道从模型列表摘掉（见「模型列表按渠道凭证加载」）：故暂停后 Playground 与 `GET /v1/models` 不再列出它的模型，取消暂停即回来。

### 模型列表按渠道凭证加载
`GET /v1/models` 与 Playground 的模型下拉只合并**当前有可用凭证**的渠道：该渠道至少有一条**未暂停、未会话失效**的记录。判定在缓存之前——没凭证的渠道不读缓存、不拉上游、不展示，故**从未接入**的渠道不出现「幽灵模型」（此前无凭证也调上游，CodeBuddy/TRAE 回退静态表，把打不通的模型也列出）；**全部暂停 / 凭证失效**时其模型暂时消失，恢复或补一条凭证后立刻回来；冷启动默认只有 `zen` / `kilo`（自带虚拟凭证），接入 CodeBuddy / TRAE 后下一次列表请求即纳入；冷却中的凭证仍算「有凭证」——只是暂时限流，列表不闪没。

**展示顺序**：CodeBuddy、TRAE、Qoder、CodeArts 的模型依次排在列表前面（CB → TR → Qoder → CodeArts → 其余渠道），组内按模型名字典序；多渠道模型按最高优先级渠道归位（含 CB 即进第一段）。Playground 的模型选择器（渠道筛选 chips、分组列表与「强制指定渠道」选项）遵循同一顺序，每行展示该模型可用的全部渠道徽章与各自倍率。纯展示排序，不改变调度选号；直连指定一个被滤掉的模型名仍照常发起（成败由调度器决定）。

### 模型目录落盘与启动即用
模型表除进程内 TTL 缓存（300s）外，每次拉取成功还写快照到 `DATA_DIR/model_catalog.json` 并在**启动时同步读回**：重启那一刻「模型 → 渠道」归属表就已就绪，扁平名请求不会在预热（zen 免费模型探活要十几秒）跑完前扇出打错渠道；某渠道拉取失败时的缓存兜底也跨重启生效。快照存未过滤原始表（黑名单是热更项，出口现算），损坏 / 超 7 天只丢缓存、不影响启动；恢复只覆盖「已注册且当前有可用凭证」的渠道。另有 `MODEL_CATALOG_MINUTES`（默认 30、下限 5）后台任务兜底刷新（见「后台任务」）。实现与故障复盘见 [`TECHNICAL.md`](TECHNICAL.md) §3.5。

**请求路由只认这份模型缓存列表**：模型目录能证明某模型归属哪些渠道时，候选渠道就只有它们——登记了该模型的渠道全部不可用（都在冷却 / 都被暂停）时直接返回 503，不再把请求逐个渠道试过去（各上游只会回 11102 / 4001 / 401，还会给渠道写下数小时的「该后端无此模型」负缓存）。上游刚上线新模型的窗口内（模型列表 TTL 300s + 后台兜底刷新即新鲜度上限），它可能短暂「只有 zen 能跑」；模型完全不在目录里时仍按全部候选渠道路由，直连指定（`模型@渠道`）与 API Key 渠道绑定不受影响。

Playground 与 `GET /v1/models` 走 **stale-while-revalidate**：有缓存立刻回旧列表，TTL 到期的渠道丢后台异步刷新，不把 zen 探活的十几秒压在请求上；只有某渠道**一条缓存都没有**（冷启动无快照 / 新接入渠道）时才同步等一次。代价是列表最多滞后一个 TTL。见 [`TECHNICAL.md`](TECHNICAL.md) §3.5。

### token 到期展示
凭证列表的 **token 剩余** 列显示 access token 距到期还有多久，低于 `TOKEN_EXPIRY_WARNING_SECONDS`（默认 1 小时）标红。到期时间优先取上游 `expires_at`，缺失时回落 JWT `exp`；都取不到显示 `—`，**不猜本地 TTL**（否则是凭空捏造的预警）。

### 积分记录
凭证**额度**列数字后的**下箭头**展开「积分记录」，记录**两次额度探测之间的净变化**，不是动作归因：签到、成长、对话都会改余额而上游不打日志，探测只能看到区间净变化，界面因此一律写「净变化」而非「签到 +5」。首次探测只建基线；余额没变不记；余额变「未知」仍记但不填变化量；保留 90 天。

### 用量统计里的 credit 与 ≈
统计页 **Credit 消耗** 列：CodeBuddy 是上游**真值**；TRAE 的 `token_usage` 帧只有 token 数，credit 按官方单价**推算**并标 `≈`；CodeArts 福利模型按每日池 1:1 扣减推算，同样标 `≈`。单价表与推算逻辑集中在 `src/provider/trae/pricing.py`（TRAE）与 `src/provider/codearts/units.py`（CodeArts），未收录的模型不推算（显示 `—`）。历史明细可分别用 `python3 scripts/backfill_trae_credit.py` / `scripts/convert_codearts_credit_unit.py` 补齐（默认预览、`--apply` 才写）。单价 / 折扣实测细节见 [`TECHNICAL.md`](TECHNICAL.md) §9 与 PROPOSAL Q36 / Q52。

### 用量统计里的成本（估算，≈）
统计页 **成本（估算）** 卡片与各表的**成本**列是「按 token × 公开刊例价」的**估算**，不是上游真实扣费：单价取自 **OpenRouter 公开模型目录**（`pricing.prompt` / `completion` / `input_cache_read`，单位 **USD / 百万 token**），按 `(输入−命中)×输入价 + 命中×缓存价 + 输出×输出价` 折算成美元，再按**写入时生效的汇率**（`USD_CNY_RATE`，默认 `6.70`）折成人民币。人民币为主、美元为辅（明细单元格悬停显示美元）。

**写入时定值**：成本在写明细时算好落库（`usage_events.cost_usd` / `cost_cny`），历史行**不随价表或汇率重算**，改汇率只影响之后的写入；上线前的历史明细两列为 `NULL`（显示 `—`），可用 `python3 scripts/backfill_cost.py --apply` 按**当前**价表与汇率补齐 / 重算（默认预览、写库前自动备份、幂等，口径是「按今天重估」而非还原当时花费）。**匹配不到就不计**：本地模型名按 OpenRouter 的 `id`（含厂商前缀）/ 展示名等值匹配（`src/model_match.py`，**命中不唯一就不配**），未匹配定价或未上报输入 token 的不计入，聚合值 `null` 显示 `—`（**不拿 0 冒充免费**），故成本是**下限**。**价表来源与刷新**：后台任务 `OPENROUTER_CATALOG_MINUTES`（默认每日一次、下限 60 分钟）从 **OpenRouter 公开接口**（匿名可读、无需 key）一次抓取刊例价 + 明细元数据 + 能力指数，落盘 `data/openrouter_catalog.json`，启动时同步回灌（零上游请求）；**无快照时**（首次部署）启动后台补拉一次，避免成本空窗到下一轮；价表缺失时成本全显示 `—`，不影响聊天。**模型列表页**：管理台「模型列表」页只读展示该目录——含成本估算所用的输入 / 输出 / 缓存读 / 缓存写单价（USD / 百万 token，可切人民币按当前汇率折算），以及上下文·输出上限、输入→输出模态、能力（推理 / 工具调用 / 附件 / 结构化输出）、知识截止等元数据；支持按 id / 名称 / 渠道搜索，显示模型数与目录更新时间。OpenRouter 收录范围（数百条）小于此前的 models.dev 目录，且不提供开放权重 / 所属家族 / **原厂发布时间**三列——故本表只列知识截止、不列发布日期（上游的 `created` 是「收录进 OpenRouter 的时间」，不是发布日，标上去是错的）。四项单价合并一列两行（上行「输入 · 输出」，下行「缓存读 · 缓存写」），上下文/输出与模态/能力列允许折行，避免宽表横向滚动。目录可达数百条，故**分页渲染（每页 20/50/100，默认 50）**、容器内滚动、表头吸顶。目录缺失显示空态而非报错；`GET /api/model-catalog` 仅供登录会话读取。**能力分（2026-10 起）**：Playground 模型选择器行内徽章与选中卡、「模型列表」页「能力分」列、`GET /v1/models` 每行的可选 `benchmarks` 字段，展示 Artificial Analysis 的三项指数（`intelligence_index` 综合智能 / `coding_index` 编程 / `agentic_index` 智能体）。数据与价格、明细同一次抓取（同一个后台任务、同一份快照、同一个 `saved_at`）。与价表共用同一套匹配口径：本地归一键等值匹配上游 id / 展示名，**命中不唯一就不配**（宁可漏也不错配），匹配不到的模型不带该字段、页面显示 `—`。三处都不做排序——分数是第三方成绩，不是本服务实测，排序等于替用户下结论。

### 用量统计里的缓存命中率
**Token 消耗**卡片在 token 分项后追加**缓存命中率** = 命中 token ÷ 输入 token（保留 1 位小数），取自上游上报的 `cached_tokens`（TRAE 的 `cache_read_input_tokens` 映射）；**未上报或输入为 0 时显示 `—`**，不拿 0 冒充「0%」。

### 历史分组数据重新汇总
统计页**按渠道 / 按模型 / 按用户**三个维度读小时汇总表 `usage_hourly`（永久保留），新请求在写入时增量累加（`collector._bump_hourly`），后台 retention 任务每轮全量重算（`rollup_hourly`）。若历史数据偏小——老库升级前落的明细没被覆盖、或补过明细（`backfill_cost.py` 等）后没重算——用 `python3 scripts/rollup_hourly.py --apply` 一次性重算（默认预览、**写库前自动备份**、幂等）。脚本先报告缺口（缺汇总行的分组键、数值不符的分组键）与两侧时间跨度，并提示**明细已过保留期（90 天）无法还原**的区间：早于明细保留期且汇总也没有的小时不可逆补回，只能如实显示缺口。

### 用量统计里的分组维度
统计页**分组统计**面板用 tabs 切维度，四个维度共用同一张表（请求数 / 成功数 / 成功率 / 输入 / 输出 / 命中缓存 / Credit / 成本）：**按渠道**、**按模型**、**按用户**、**按凭证**。后三个分组键分别为 `usage_hourly.username`、`usage_hourly.model`、`usage_events.credential_id`，对应端点 `GET /api/stats/by-provider|by-model|by-user|by-credential`（`username` / `since` 参数与其它统计端点一致，租户隔离同样以 `_scope` 为准）。**默认按请求数降序**（并列时按分组键升序，次序稳定），点列头可在升序 / 降序间切换，`sort` / `order` 查询参数见下「列表排序」。**按用户 / 按凭证**只对 admin 与 operator 展示（viewer 只能看自己，恒是「我」一行，给这个 tab 只是噪音）。**口径差异**：渠道 / 模型 / 用户三维读**小时汇总**（永久保留，最近 5 分钟内的请求尚未计入）；**凭证维度读明细表**（小时汇总主键里没有 `credential_id`），故只覆盖**最近 90 天**，且无凭证的预热失败等请求不计入——面板底部按当前维度给出对应说明。凭证列显示**渠道 icon + 凭证昵称**，与请求明细同口径：昵称为空或凭证已删除时回落 ID 前 12 位（完整值在悬停 title 里）；icon 取凭证当前所属渠道，凭证已删除则回退该组明细里的渠道。

### 列表排序（可点列头）
管理台各列表**列头可点排序**：点一次用该列的「首次点击方向」（数值列先降序、文本列先升序），再点在同一列的升序 / 降序间切换；当前排序列在表头显示上 / 下箭头并经 `aria-sort` 标注。排序一律由**后端**完成——所有 `GET` 列表端点接受统一的 `sort`（白名单键）与 `order`（`asc` / `desc`）查询参数。**未知 `sort` 键或非法 `order` 值回落到该端点的默认排序，不报错**（前端缓存里的旧键不会把页面打挂）；`sort` 是白名单键而非 SQL 列名，注入面为零。适用端点：

- `GET /api/credentials`：`nickname` / `provider` / `health` / `enabled` / `pinned` / `quota_remaining` / `quota_total` / `quota_expiring_credits` / `token_expires_at` / `growth_last_run_at` / `created_at`（默认 `created_at` 升序）。含派生字段（到期额度等）在组装后排序，`token_expires_at` / `growth_last_run_at` 为「未知」（0 / 空）时始终排最后。
- `GET /api/users`：`username` / `role` / `enabled` / `created_by` / `created_at` / `updated_at`（默认 `username` 升序）。
- `GET /api/api-keys`：`name` / `provider_binding` / `created_at` / `last_used_at` / `expires_at`（默认 `created_at` 升序）。
- `GET /api/audit`：`ts` / `actor` / `action` / `target` / `detail` / `ip` / `ok`（默认 `ts` 降序）。
- `GET /api/alerts`：`ts` / `severity` / `rule` / `scope` / `message` / `delivered`（默认 `ts` 降序）。
- `GET /api/model-catalog`：`id` / `name` / `provider` / `context` / `max_output` / `knowledge` / `input` / `output` / `cache_read` / `cache_write`（默认 `id` 升序）。`family` / `open_weights` / `release_date` 上游不提供，字段保留但恒为 `null` / `false`，页面不展示。
- `GET /api/credentials/{id}/credit-events`：`ts` / `window_start` / `delta` / `source`（默认 `ts` 降序）。
- `GET /api/credentials/{id}/accounts`：`nickname` / `account_id` / `type`（默认 `nickname` 升序）。
- `GET /api/stats/by-provider|by-model|by-user|by-credential`：`group`（分组键）/ `requests` / `ok_count` / `input_tokens` / `output_tokens` / `cached_tokens` / `credit` / `cost_cny`（默认 `requests` 降序）。
- `GET /api/stats/events`（逐请求明细）：`time`（rowid）/ `ts` / `username` / `provider` / `credential` / `model` / `ok` / `input_tokens` / `output_tokens` / `cached_tokens` / `credit` / `cost_cny` / `ttfb_ms` / `latency_ms`。**两套分页**：默认（`time` 降序）仍走 rowid 游标分页、返回 `next_before`（null 表示到底，`total` 为 null）；按**其它列**排序或时间升序时改用 `LIMIT/OFFSET` 分页、额外返回 `total`（总数，供前端算还有没有下一页），此时忽略 `before`。换排序 / 改每页数量 / 切时间范围都会回到第一页。

## 后台任务
额度探测、token 预刷新、每日签到、成长中心、活跃上报、明细清理、渠道模型列表刷新、模型目录刷新（OpenRouter）、运维告警共 9 类，由 `TaskRunner` 自动调度，失败互不影响；周期见 TECHNICAL.md §6.2。每类任务的上次执行时间、最近结果与错误在管理台**「任务与配置」页**查看（30 秒自动刷新），运行态只存在于**本次进程**内，重启归零。

**渠道模型列表刷新**（`MODEL_CATALOG_MINUTES`，默认 30、下限 5）是兜底保鲜：模型表本身按「谁访问 `/v1/models` 或 Playground 谁刷新、TTL 300s」更新，这条只保证**没人访问时也会更新**。没有它，纯 API 用法（客户端自缓存模型列表）会让「模型 → 渠道」归属表与落盘快照一起变陈旧：上游新增模型不认识 → 扁平名请求按全部渠道扇出，各渠道回 11102 / 4001，还给每个凭证写 6 小时起步的负缓存；停机超 7 天落盘快照也会过期被丢弃。周期取 30 分钟是跟 zen 免费模型判活缓存（30 分钟）对齐——再密也不会让 zen 多探活，只是白打其余渠道的 `/models`。

**成长中心**仅 CodeBuddy 有（动作清单见上文「特性」）。Buddy 旅行 1–4 小时回来一次，故周期默认 60 分钟（`GROWTH_INTERVAL_MINUTES`），回来就领、不压到第二天。抽奖 / 连登兑换 / 开 Buddy 盲盒 / 消耗补登卡属**不可逆动作**，`GROWTH_IRREVERSIBLE_ACTIONS=false` 可全部跳过（仍领旅行礼物与任务奖励）。凭证列表「成长中心」列显示每个账号最近一轮结果，行内菜单可手动执行一次。

### 活跃上报（可选，默认关闭）
CodeBuddy 成长中心的「连登天数 / 活跃地图」按日统计客户端对话事件，账号长期只被本网关自动调用（无真实客户端对话）时会断连登。`ACTIVITY_REPORT_ENABLED=true` 时，后台任务在每天 `ACTIVITY_REPORT_HOUR`（默认 10 点）所在整点窗口为每个账号补发**一条** `chat_request_send` 事件续上连登——与积分、调度无关，上报失败不影响聊天请求。凭证行内菜单也可手动补报一次（不受该开关影响）。

> ⚠️ **风险与限制**：官方活动条款禁止用模拟器 / 脚本篡改活动数据，处罚为**取消资格并追回已发礼品**。默认关闭，开启前请自行评估账号风险。事件名与请求形状依赖上游实现，**上游改版即失效**，不承诺任何收益——故它只是验证与运维手段，不作为可靠性功能，也不承担「依赖产品内真实使用」的任务。实测（2026-09-21）：缺少 `userId` 时上游返回 HTTP 200 `{"code":0}` 却**静默丢弃**（连登不变）；OAuth 凭证的 `user_id`/`account_uid` 可能为空，此时回落取 bearer JWT 的 `sub`，仍缺失则跳过（不编造）。

## 配置
常用项如下；完整的可配置项见 `src/config.py`（权威），且**每一个都已透传到 `docker-compose.yml`**——`.env` 里写这些变量即可生效（compose 的 `.env` 只做插值，未透传的变量不会进容器）。容器里改监听地址用 `HOST`/`PORT`（`PORT` 同时决定宿主机映射端口），入口读 `config.py`，不硬编码。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | **必填** | 凭证加密密钥，≥16 字符；丢失 = 凭证全部作废，无密钥轮换 |
| `ADMIN_USERNAMES` | 空 | **仅引导期**：逗号分隔的用户名，首次启动时被提权为 `admin`。DB 里已有角色后不再生效——日常改角色在管理台「用户管理」页做（见下文「用户与角色」） |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 浏览器可达地址；TRAE 登录回调依赖它 |
| `DEFAULT_MODEL` | `glm-5.2` | 模型为空/`auto` 时的默认 |
| `USERS_FILE` | `secrets/users.txt` | **仅引导期**：老式用户文件路径，启动时一次性导入 SQLite（已存在的用户名不覆盖，幂等）。账号唯一源是 `users` 表 |
| `DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `QUOTA_PROBE_MINUTES` | `60` | 额度探测周期（下限 1 分钟） |
| `REFRESH_INTERVAL_MINUTES` | `30` | token 预刷新轮询周期（下限 5 分钟）；短寿命渠道的预刷新窗口封顶必须**宽于**它，否则整轮漏过、凭证拖到到期才刷 |
| `MODEL_CATALOG_MINUTES` | `30` | 渠道模型列表兜底刷新周期（下限 5 分钟）；只影响「没人访问列表时」的保鲜，正常仍按 TTL 随访问刷新 |
| `OPENROUTER_CATALOG_MINUTES` | `1440` | 模型目录刷新周期（下限 60 分钟）：后台从 OpenRouter 公开接口一次抓取刊例价（成本估算）、明细元数据（「模型列表」页）与 Artificial Analysis 三项指数（Playground /「模型列表」页 / `/v1/models` 能力分）并落盘；上游变动少，默认每日一次 |
| `USD_CNY_RATE` | `6.70` | 成本估算的美元兑人民币汇率（1 USD = 该值 CNY）；可热更，只影响之后写入的请求，历史成本不重算 |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | 模型目录来源地址（匿名可读）；改后需重启 |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | 能力分来源地址（匿名可读、无需 key）；改后需重启 |
| `GROWTH_INTERVAL_MINUTES` | `60` | 成长中心（仅 CodeBuddy）一轮领取的周期；下限 5 分钟 |
| `GROWTH_IRREVERSIBLE_ACTIONS` | `true` | 是否允许成长中心的不可逆动作：抽奖、连登兑换、开 Buddy 盲盒、消耗补登卡。`false` 时仍领取旅行礼物与任务奖励 |
| `ACTIVITY_REPORT_ENABLED` | `false` | 活跃上报（仅 CodeBuddy）：每天为账号补发一条对话事件续连登。**默认关闭**——官方条款禁止脚本篡改活动数据（处罚为取消资格并追回礼品），上游改版即失效，不作为可靠性功能（见上文「活跃上报」） |
| `ACTIVITY_REPORT_HOUR` | `10` | 活跃上报的本地（北京）时间整点窗口；仅在 `ACTIVITY_REPORT_ENABLED=true` 时生效 |
| `ALERT_ENABLED` | `true` | 运维告警开关：后台周期评估四类风险（池耗尽 / 任务连续失败 / token 临近到期 / 上游错误率骤升），命中落 `alert_events` 供管理台「运维告警」页回看。关闭后不评估、不落库、不推送 |
| `ALERT_WEBHOOK_URL` | 空 | 告警 Webhook 地址：命中时 POST JSON，多个用逗号分隔（≤16 个，仅 `http(s)`），全部成功才算投递成功；留空 = 只在管理台留站内记录、不外推 |
| `ALERT_INTERVAL_MINUTES` | `5` | 运维告警评估周期（下限 1 分钟） |
| `ALERT_SILENCE_MINUTES` | `30` | 同一 `(规则, 对象)` 的静默窗：窗口内只落库 / 推送一次，避免持续状态每轮刷屏；`0` 关闭静默（每轮都报） |
| `ALERT_POOL_READY_MIN` | `1` | 池耗尽阈值：可用凭证数少于该值时告警（服务活着但用不了）；`0` 关闭该规则 |
| `ALERT_TASK_FAILURES` | `3` | 后台任务连续失败达到该次数时告警（成功一轮清零）；`0` 关闭该规则 |
| `ALERT_TOKEN_EXPIRY_HOURS` | `24` | token 剩余时间少于该小时数时告警；`0` 关闭该规则 |
| `ALERT_ERROR_RATE_THRESHOLD` | `0.5` | 上游错误率阈值：统计窗内失败占比达到该值且样本足够时告警；`0` 关闭该规则 |
| `ALERT_ERROR_RATE_MIN_REQUESTS` | `20` | 错误率最小样本数：窗内请求数少于该值不判（样本太小无意义） |
| `ALERT_ERROR_RATE_WINDOW_MINUTES` | `15` | 错误率统计窗（分钟），数据源为 `usage_events` 明细（非小时汇总） |
| `QUOTA_EXPIRY_WINDOW_SECONDS` | `129600` | 主到期排序窗口：把距到期 ≤ 该秒数的积分加总，多的账号先用（避免积分过期浪费）；CodeBuddy 与 TRAE 都按包独立到期、都参与该排序；`≤0` 关闭整套到期排序（次窗口一并失效），退回纯健康度排序 |
| `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` | `604800` | 次到期排序窗口：主窗口打平（常见的是都为 0）时才比较，`7 天`覆盖一个完整的小包到期周期；`≤0` 关闭该级 |
| `CONVERSATION_STICKY_SECONDS` | `3600` | 会话粘性 TTL：优先按请求体显式会话标识（`conversation_id`/`conversationId`/`prompt_cache_key`，metadata 或顶层），无则回落消息前缀指纹，多轮请求固定用同一凭证（手动 pin 的凭证优先，粘性让位）；带 `user_id` 时不派生前缀兜底键（避免并行对话误钉同一号）；凭证出错仍会轮换，成功后重新粘定；`≤0` 关闭 |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,default,hunyuan-image-*,hy4-preview-x,qwen3.8-flash,glyph-cluster` | 模型列表黑名单（fnmatch，仅影响列表展示，直连指定不受影响）；按**归一后的对外写法**匹配（原代号、其归一键、展示名、展示名归一键四种任一命中即滤），故可照列表里看到的 `kimi-k3` / `Kimi K3` 或老口径的原代号写。默认值按上游实测清单补入内部/不可用模型（`default` 零内容、`hunyuan-image-*` 400 11103、`hy4-preview-x` 400 11102、Qoder `qfmodel` 400、kilo `stealth/glyph-cluster` 408），刻意不含 `*-volc` 与 `aquila`/`sagitta`/`seed-code-pro-0430`/`hy4-preview`/`qwen3.8-max`（实测可正常 chat）（见 TECHNICAL.md §3.5） |
| `ALLOWED_HOSTS` | 空 | Host 白名单，防 DNS rebinding |
| `TRUST_PROXY` | `false` | 是否采信 `X-Forwarded-For` 判定来源 IP（白名单、登录限流与审计共用同一解析）。默认关闭——该头客户端可写；仅「本服务前恰好一层受信反代」时开启，取 XFF 最后一个条目。详见上文「API Key 的渠道绑定、模型白名单、来源 IP 与到期时间」 |
| `CODEBUDDY_API_ENDPOINT` | `https://copilot.tencent.com` | CodeBuddy 上游地址；改动时必须同时把它加入 `CODEBUDDY_ALLOWED_ENDPOINTS` |
| `CODEBUDDY_ALLOWED_ENDPOINTS` | 官方两站（见 compose） | 上游端点白名单，真实 Token 只发往白名单内地址 |
| `ZEN_API_ENDPOINT` | `https://opencode.ai` | OpenCode Zen 上游地址；改动时必须同时把它加入 `ZEN_ALLOWED_ENDPOINTS`（Zen 不带真实 Token，白名单仅防误配） |
| `ZEN_ALLOWED_ENDPOINTS` | `https://opencode.ai` | Zen 端点白名单 |
| `ZEN_OPENCODE_VERSION` | `1.18.0` | 门禁伪装用的 `opencode/<version>` UA 版本；上游阈值上移时改这里（低于阈值会被 426 拒绝） |
| `KILO_API_ENDPOINT` | `https://api.kilo.ai/api/gateway` | Kilo Gateway 上游地址；改动时必须同时把它加入 `KILO_ALLOWED_ENDPOINTS`（Kilo 不带真实 Token，白名单仅防误配） |
| `KILO_ALLOWED_ENDPOINTS` | `https://api.kilo.ai/api/gateway` | Kilo 端点白名单 |
| `QODER_API_ENDPOINT` | `https://openapi.qoder.com.cn` | Qoder openapi 地址（额度/登录走这里）；国际版设为 `https://openapi.qoder.sh`，并确认白名单含国际版域名 |
| `QODER_GATEWAY_ENDPOINT` | `https://gateway.qoder.com.cn` | Qoder 推理网关地址（聊天/模型发现走这里）；国际版设为 `https://api1.qoder.sh` |
| `QODER_ALLOWED_ENDPOINTS` | 国内 openapi+gateway + 国际版（见 compose） | Qoder 端点白名单，带 COSY 签名的请求只发往白名单内地址 |
| `CODEARTS_API_ENDPOINT` | `https://snap-access.cn-north-4.myhuaweicloud.com` | CodeArts snap 引擎地址；改动时必须同时把它加入 `CODEARTS_ALLOWED_ENDPOINTS` |
| `CODEARTS_ALLOWED_ENDPOINTS` | snap 引擎 + STS + 福利网关 + 门户（见 compose） | CodeArts 端点白名单，AK/SK 签名请求只发往白名单内地址 |
| `PROVIDER_PROXIES` | `""` | **按渠道出站代理**（启动期项，改后需重启）：格式、协议、作用范围与严格解析见上文「按渠道出站代理」；留空 = 全部直连，`trust_env=False` 不受环境代理影响 |
| `CODEBUDDY_CHAT_MIN_INTERVAL` | `5` | CB/TRAE 聊天节流器的最小间隔（秒）：按凭证分桶、**桶内允许并发**（同渠道同模型并发不排队、立即发出），只在同凭证「上一请求已结束、紧接着又来一个」的顺序连发时补足间隔；`0` 关闭 |
| `ZEN_CHAT_MIN_INTERVAL` | `0` | Zen 聊天最小间隔（秒），独立于 CB/TRAE 的节流器，默认关闭。zen 是匿名免费层、无账号级频率风控；若与 CB/TRAE 共享会排在它们之后空等满 5s（并发/连发时每个请求 +5s），故不共享 |
| `KILO_CHAT_MIN_INTERVAL` | `0` | Kilo 聊天最小间隔（秒），独立于 zen / CB/TRAE 的节流器，默认关闭。同为匿名免费层，与 zen 各自独立、互不排队 |
| `QODER_CHAT_MIN_INTERVAL` | `5` | Qoder 聊天最小间隔（秒），独立节流器（真实账号渠道，上游有账号级频率风控）；`0` 关闭 |
| `CODEARTS_CHAT_MIN_INTERVAL` | `5` | CodeArts 聊天最小间隔（秒），独立节流器（真实账号渠道）；`0` 关闭 |
| `CODEARTS_MAX_CONCURRENCY` | `3` | CodeArts 每账号**在途并发上限**（热更项）。上游硬限每账号并发会话数 3，超出即 `400 TM.00001041`；桶内名额满时请求挂起直到有请求结束；`0` 关闭上限（回到「有在途即放行」，会再次击穿）。名额在每次尝试结束时**同步**归还，不依赖 GC |
| `CODEARTS_REQUEST_WINDOW_SECONDS` | `60` | CodeArts **账号滑动窗口**（秒，热更项）。与上一项组合成「每账号每 60s 最多启动 3 次」——实测上游限制的是「每账号每约 60s 最多 3 个会话」（会话在流结束后仍滞留数十秒，实测约 68s 才恢复），纯在途上限挡不住「3 个刚结束就再发 3 个」的突发。窗口内满额时请求挂起到最早一次启动滑出窗口；`0` 关闭窗口口径，退回纯在途上限 |
| `CODEBUDDY_SANITIZE_CHANNEL_MARKERS` | `true` | 出站 `system`/`assistant` 正文命中「伪装其他厂商官方客户端」指纹串时替换为占位符（上游 11128 内容风控：换号无效、会话带入即持续报错）；只改出站副本，客户端历史不受影响；`false` 关闭（见 TECHNICAL.md §3.2） |
| `REFRESH_SKEW_HOURS` | `24` | token 到期前该小时数窗口内预刷新。到期时间取凭证显式 `expires_at`/`expiration`，缺失时回落 access token 的 JWT `exp`（CodeBuddy 实测不带显式到期字段）。短寿命渠道自行封顶该窗口：CodeArts STS 只有 2h，封顶 45min（否则 24h 窗口对它恒为真、每轮都烧一张一次性 refresh_token；且必须宽于 `REFRESH_INTERVAL_MINUTES` 默认 30min） |
| `TOKEN_EXPIRY_WARNING_SECONDS` | `3600` | 管理台 token 到期预警阈值：剩余低于该值时标红；`≤0` 关闭预警（仍显示剩余时间）。纯展示，不参与调度 |
| `PACER_MIN_SECONDS` / `PACER_MAX_SECONDS` | `5` / `20` | 全局节流器随机等待区间（秒） |
| `LOG_LEVEL` | `INFO` | 日志级别；审计日志是 INFO 级，调到 `WARNING` 会一并关掉 |
| `ENABLE_DOCS` | `false` | 是否暴露 `/docs` 与 `/openapi.json`（默认关闭：匿名可拉全量 API 结构）；本地调试需 Swagger 时置 `true` |
| `DUMP_REQUEST_BODIES` | `false` | 诊断：把 `/v1` 原始请求体落盘到 `data/dumps/`（**含对话内容**，仅排查用） |
| `AUTO_CONTINUE_MAX` | `10` | 上游以 `finish_reason=length` 截断时同凭证自动续写的最多次数；`0` 关闭（见 TECHNICAL.md §3.4） |
| `CONTEXT_COMPRESS_ENABLED` | `true` | 按模型目录里的输入上限裁剪过长对话，避免撞上游硬限制（CodeBuddy `11115 prompt is too long`）。目录里查不到上限的模型不受影响（宁可不裁剪也不猜）。见 TECHNICAL.md §3.19 |
| `CONTEXT_COMPRESS_RESERVE_TOKENS` | `4096` | 压缩预算里为模型回复预留的输出 token 数 |
| `CONTEXT_COMPRESS_MIN_KEEP_MESSAGES` | `4` | 无论多长都保留的最近消息条数（保证当前这轮对话完整） |
| `CONTEXT_COMPRESS_SAFETY_RATIO` | `0.95` | 按模型上限的百分比计算压缩预算，给 token 估算误差留余量 |
| `UPSTREAM_COMPLETE_TIMEOUT_SECONDS` | `600` | 非流式聚合整体超时（秒）：上游连接半开停滞会让非流式请求无限悬挂并占住凭证，超时按瞬态错误换号重试（流式有心跳兜底不受影响）；`≤0` 关闭 |
| `MODEL_FALLBACK_GROUPS` | `""` | 跨渠道 fallback 兼容组（热更项，见 TECHNICAL.md §3.20）。格式 `组名=成员1,成员2;组名2=成员3`；成员可带 `@渠道` 固定渠道，组名只是入口别名（不进链）。请求任一成员时该成员置首、组内其余依次回退；主渠道全不可用才回退，**仅在本次尝试尚未产出任何响应帧前**切换（已出帧绝不换模型）。留空 = 不启用 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址与端口（compose 默认 `0.0.0.0`，`PORT` 同时决定宿主机映射端口） |

### 管理台热更（「任务与配置」页）
上表中带「可热更」语义的 38 项可不改 `.env`、不重启，直接在管理台「任务与配置」页修改：

`DEFAULT_MODEL`、`MODEL_BLOCKLIST`、`CONTEXT_COMPRESS_ENABLED`、`CONTEXT_COMPRESS_RESERVE_TOKENS`、`CONTEXT_COMPRESS_MIN_KEEP_MESSAGES`、`CONTEXT_COMPRESS_SAFETY_RATIO`、`MODEL_FALLBACK_GROUPS`、`QUOTA_EXPIRY_WINDOW_SECONDS`、`QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS`、`CONVERSATION_STICKY_SECONDS`、`GROWTH_IRREVERSIBLE_ACTIONS`、`GROWTH_INTERVAL_MINUTES`、`REFRESH_INTERVAL_MINUTES`、`QUOTA_PROBE_MINUTES`、`MODEL_CATALOG_MINUTES`、`OPENROUTER_CATALOG_MINUTES`、`OPENROUTER_MODELS_URL`、`USD_CNY_RATE`、`CODEBUDDY_CHAT_MIN_INTERVAL`、`ZEN_CHAT_MIN_INTERVAL`、`KILO_CHAT_MIN_INTERVAL`、`QODER_CHAT_MIN_INTERVAL`、`CODEARTS_CHAT_MIN_INTERVAL`、`CODEARTS_MAX_CONCURRENCY`、`CODEARTS_REQUEST_WINDOW_SECONDS`、`PACER_MIN_SECONDS`、`PACER_MAX_SECONDS`、`ACTIVITY_REPORT_ENABLED`、`ACTIVITY_REPORT_HOUR`、`ALERT_ENABLED`、`ALERT_WEBHOOK_URL`、`ALERT_INTERVAL_MINUTES`、`ALERT_SILENCE_MINUTES`、`ALERT_POOL_READY_MIN`、`ALERT_TASK_FAILURES`、`ALERT_TOKEN_EXPIRY_HOURS`、`ALERT_ERROR_RATE_THRESHOLD`、`ALERT_ERROR_RATE_MIN_REQUESTS`、`ALERT_ERROR_RATE_WINDOW_MINUTES`。

要点：**优先级 `DB 覆盖值 > .env`**——改过后 .env 对该项不再生效，页面标「DB 覆盖」，点「恢复默认」删掉覆盖行才回落 .env，日志记录改动者。值存 `runtime_settings` 表（纯 key/value），新增可热更项无需迁移；白名单外的 key、非法类型 / 越界值写入前即拒，读取时坏行跳过并记警告。启动期项（`APP_SECRET` / `PORT` / `DATA_DIR` / `USERS_FILE` / 上游端点白名单）**不在**白名单：它们决定进程如何启动，运行期改只会让内存与磁盘静默分叉。接口 `GET /api/settings` 读快照、`PUT /api/settings` 写（admin + CSRF），body `{"values": {key: value}}`，传 `null` 恢复默认。该页同时展示**后台任务运行态**：9 类任务的周期、上次执行时间与最近结果；页面按 tab 组织，每个任务一个 tab，无任务归属的配置（默认模型、黑名单、到期窗口、节流等）按后端下发的网关卡组各占一个 tab。**tab 为紧凑单行**（`text-xs`，放不下横向滚动、不折行）；长任务名用简称：正式名「渠道模型列表刷新」显示「渠道模型刷新」、「模型目录刷新（OpenRouter）」显示「模型目录刷新」，卡片标题、告警与接口字段仍用完整名。运行态是**进程内**的（`GET /api/tasks`，admin，页面每 30 秒刷新）：只显示「本次启动以来跑过没有」，**重启归零**，不落库、不留历史；未到点或未开启的轮次不算执行——否则签到会显示成「刚刚跑过」，而当天一次都没签。

## 部署注意
- **挂载目录属主**：容器内以 uid 1001（`appuser`）运行，`./data` 与 `./secrets` 必须可写/可读，否则 SQLite 打不开：

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **时区**：镜像默认 `TZ=Asia/Shanghai`；如需其它时区显式覆盖 `TZ`。

### 升级后必须重启后端
**改动 `src/` 后必须重启进程，否则出现「新前端 + 旧后端」错配。** 典型症状：管理台页面能打开（前端产物本就是静态文件），但调用新端点全部失败——旧后端没有该路由，未匹配的 `/api/*` 按约定返回 JSON `404`，前端当成通用失败，弹出与真实原因无关的提示。前端不需重启（后端每次请求现读 `web/dist`），**后端不是**——进程管理器只在进程**退出**时重新拉起，不监听源码变化。排查顺序与 B5 教训见 [`TECHNICAL.md`](TECHNICAL.md) §6.4。

```bash
# Docker / compose
docker compose up -d --force-recreate

# systemd
sudo systemctl restart coding2api

# 裸跑 / 其他进程管理器：按你自己的方式重启该进程
```

**确认升级已生效**（先查版本，再查路由）：

```bash
# 1) schema 版本已迁移（期望 18）且账号已导入
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version; SELECT username, role, enabled FROM users;"
# 2) 路由存在：期望 401（未登录），404 = 旧后端进程
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users
# 3) 启动日志里能看到引导结果（日志位置见下文「日志」）
```

### 前端产物与后端在同一端口
后端会把前端产物放在 `/` 直接服务（`src/webapp/static.py` 每次请求现读磁盘），因此**只改前端时构建完刷新即可，无需重启后端**；`localhost:5173` 是 Vite 开发态（HMR），两者可并存。**这条只对前端成立**——改了 `src/` 下的后端代码必须重启进程（见上一节）。

`scripts/build-web.sh` 是构建入口，会自动处理「依赖未装」与「node 不在 PATH」两类常见情况：

```bash
./scripts/build-web.sh            # 仅在产物过期时构建
./scripts/build-web.sh --force    # 无条件重建
```

> **部署相关操作记在本地文档**：当前开发机的进程管理方式（启动 / 重启 / 日志位置）、路径与工具版本等，见 `docs/local-environment.md`。该目录已在 `.gitignore` 中，**克隆仓库的人不会有这个文件**，按自己的部署方式自建即可。

## 日志
应用只写 **stdout / stderr**，不写文件、不自行轮转（原因见 [`src/webapp/logging.py`](src/webapp/logging.py) 顶部说明）：部署形态与采集方式各异，都靠这两个 fd 对接，轮转交给平台工具。因此**日志不会自行停止增长**——按下面对应形态配一次即可。

| 部署形态 | 日志到哪 | 轮转机制 | 需做什么 |
|---|---|---|---|
| **Docker / compose** | `docker logs coding2api` | json-file 驱动（已在 compose 配好 `10m × 5`） | **无需操作** |
| **Linux（systemd）** | `journalctl -u coding2api` | journald 自带 | **无需操作**（模板见 `deploy/systemd/`） |
| **Linux（非 systemd）** | 重定向到 `/var/log/coding2api/*.log` | `logrotate` | `sudo cp deploy/logrotate/coding2api /etc/logrotate.d/` |
| **裸跑**（`uv run python -m src.main`） | 终端 stderr | 无 | 自己重定向并自备轮转 |

级别用 `LOG_LEVEL`（默认 `INFO`）。**审计日志（登录、账号变动、凭证增删改 / pin / 账号切换）是 INFO 级**，调到 `WARNING` 会把它一并关掉。

```bash
docker logs -f coding2api             # 容器
journalctl -u coding2api -f           # systemd
```

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
| [`PROPOSAL.md`](PROPOSAL.md) | 立项决策、目标与非目标、可行性核实、风险清单 |
| [`TECHNICAL.md`](TECHNICAL.md) | 技术栈、模块规格、Provider 协议、请求时序、测试策略 |
| [`diagrams/coding2api-architecture.html`](diagrams/coding2api-architecture.html) | 系统架构图（浏览器打开） |
| [`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html) | 请求主链路时序图（浏览器打开） |
| [`diagrams/coding2api-credential-lifecycle.html`](diagrams/coding2api-credential-lifecycle.html) | 凭证调度状态机（浏览器打开） |

## 状态
M0–M3 及后续迭代全部完成，`main` 分支可运行，当前版本 v0.4.1。后续批次（B1–B11）已按批准计划落地：

**B1 请求质量**：错误分类细分 + 模型级冷却、出站指纹清洗（11128 内容风控）、截断续写、会话粘性键、模型元数据/黑名单。**B2 协议出口**：`/v1/responses`（Codex CLI 子集）；Anthropic `/v1/messages`（Claude Code，含 `count_tokens`，见 P0-1）。**B3 运维**：凭证暂停语义、运行时配置热更、token 到期展示、积分变动流水、池健康 `/healthz` + 多 Key 出口/IP 绑定。**B4 任务可视化**：后台任务运行态并入「任务与配置」页；模型黑名单热更延迟修复。

**B5 账号体系**：用户从 `users.txt` 迁入 SQLite、三角色 RBAC、会话吊销（epoch）、一次性令牌激活 + 首登强制改密、用户管理页、审计日志页、硬删降为 CLI。**B6 新渠道**：接入 **Qoder**（COSY 私有协议 + 设备码登录 + 签到/额度）与 **CodeArts**（华为云 SDK-HMAC 签名 + DPoP 刷新 + 累计全文 SSE + 福利领取）；`KNOWN_PROVIDERS` 扩到六个，展示排序、渠道绑定、前端图标与文档同步。**B7 竞品能力补齐（P0）**：Anthropic `/v1/messages` 出口（Claude Code）、上下文压缩（按模型目录输入上限裁剪过长对话）、API Key 模型白名单 + 到期时间（对比与迁移分档见本地存档 `docs/competitor-comparison.md`，该目录已 gitignore、克隆仓库不含此文件）。

**B8 智能路由（P1）**：跨渠道 fallback 兼容组（`MODEL_FALLBACK_GROUPS`，主渠道全不可用时按组顺序回退、仅在未出帧前切换）。**B9 运维告警（P1）**：后台周期评估四类风险（凭证池耗尽 / 后台任务连续失败 / token 临近到期 / 上游错误率骤升），命中落 `alert_events` 并在管理台「运维告警」页回看，可选推送 webhook（`ALERT_WEBHOOK_URL`），同一告警在静默窗内只报一次。**B10 按渠道代理（P1）**：`PROVIDER_PROXIES` 为每个渠道单独指定出站代理（HTTP/SOCKS5），作用于该渠道全部出站请求（聊天 / 额度 / 模型 / 后台任务 / OAuth 登录）；留空直连，默认行为不变。**B11 安全审查加固**：全项目 code review + 安全扫描后的修复批次——热更 float 拒绝 NaN/±inf、字符串类长度上限、告警 webhook 限 `http(s)`；INVALID 跳过的上游不再占满换号预算（跨渠道 fallback 恢复）；SSE 单行/单帧与非 2xx 错误体有界；对外错误文案不再回流上游正文；三个出站协议补齐入站数值/JSON 校验；`/api/stats/events` 凭证昵称仅 admin/operator 可见；续写中途断开的用量记账；OAuth 弹窗 scheme 校验 + `opener` 置空；CSP 补 `object-src`/`base-uri`/`form-action`。

规划与实测收窄的完整记录见 `PROPOSAL.md`（Q1–Q74）与 `TECHNICAL.md`（§3.1–§3.23、§6.1–§6.4）。

## 授权协议
MIT，见 [LICENSE](LICENSE)。借鉴的上游项目署名见 [NOTICE](NOTICE)。
