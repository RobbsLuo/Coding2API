# Coding2API

把 **CodeBuddy** 与 **TRAE SOLO** 两个 coding agent 渠道，统一封装为 OpenAI 兼容 API，
提供公共凭证池、统一调度与按人用量统计。

> [!WARNING]
> 逆向接口，仅供学习研究，未做安全审计；公网部署需反向代理 + 鉴权 + IP 白名单。

## 特性

- **OpenAI 兼容出口**：`/v1/chat/completions`（流式 + 非流式）、`/v1/responses`（Responses API，Codex CLI）、`/v1/models`、`/v1/user/balance`（DeepSeek 兼容余额查询）
- **统一调度**：扁平模型名按健康度自动选号，`模型@渠道` 强制指定；三态健康度 + 分级冷却自动避开坏号；模型级限流/「该渠道无此模型」只避让那一个模型，同账号其他模型立刻可用（管理台额度列下方直接显示当前避让的模型与剩余时间）；积分 36h 内即将到期多者优先（先用掉，避免过期浪费；36h 打平时再比 7 天内将过期的积分，两级字典序；管理台凭证列表直接显示每个账号的到期积分，悬浮可看逐个额度包明细）；同一对话多轮请求粘住同一凭证（对话进行中不换号，出错才轮换）
- **公共凭证池**：admin 集中维护、全员共享；按人统计用量
- **完整凭证运维**：设备码登录、多账号切换、额度探测、每日签到（含连续天数）、token 预刷新；凭证加密入库（`APP_SECRET`）
- **成长中心**（仅 CodeBuddy）：自动领 Buddy 旅行礼物、派 Buddy、领取新任务与任务奖、断登补登、连登奖励兑换、开盲盒、能量开 Buddy 盲盒；不可逆动作可用 `GROWTH_IRREVERSIBLE_ACTIONS=false` 一键关停；管理台可手动执行并查看每轮逐条结果
- **脱敏统计**：不存对话内容；明细 90 天、小时汇总永久；按人/渠道/模型可视化
- **管理台安全加固**：登录限流、CSRF 校验、请求体上限、Host 白名单

## 快速开始

### 前置要求

Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js 24+ 与 pnpm 10+（仅构建前端需要）。

### 本地运行

```bash
uv sync
uv run python scripts/hash_password.py admin   # 创建管理台用户（交互输入密码）
cd web && pnpm install && pnpm build && cd ..
APP_SECRET="换成你自己的随机字符串" ADMIN_USERNAMES=admin \
  uv run python -m uvicorn src.main:build_app --factory --port 8000
```

打开 <http://127.0.0.1:8000> 登录管理台。

### Docker

本地构建：

```bash
cat > .env <<'EOF'
APP_SECRET=换成你自己的随机字符串
ADMIN_USERNAMES=admin
PUBLIC_BASE_URL=http://127.0.0.1:8000
EOF
docker compose build
mkdir -p secrets
docker compose run --rm --entrypoint python coding2api scripts/hash_password.py admin \
  --output /app/secrets/users.txt
docker compose up -d
```

拉取 GHCR 镜像（推荐，跳过本地构建）：

```bash
docker compose pull
# 或指定版本：docker pull ghcr.io/robbsluo/coding2api:v0.1.2
```

推送新版本：打 tag `v*` 推到 main 即触发 publish workflow（见 `.github/workflows/publish.yml`），同时打 `<tag>` 和 `:latest` 到 GHCR。也可在 Actions 页面手动触发（填版本号）。

## 使用

1. 「凭证管理」添加凭证：CodeBuddy 走设备码登录（或粘贴 `{"token":"..."}`）；TRAE 粘贴凭证 JSON（`accessToken`/`uid`/`refreshToken`）或回调链接（凭证里的 `apiHost` 只接受官方地址，其他值会被拒绝导入）
2. 「API Key」创建 `sk-...`（明文仅显示一次）
3. 调用：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'
```

`模型@渠道` 强制指定上游（如 `glm-5.2@trae`），不写则自动选健康渠道。

任意 OpenAI 兼容客户端可直接接入（Base URL `http://127.0.0.1:8000/v1`、Key 用 `sk-...`、模型名以 `GET /v1/models` 为准）；「Playground」页用登录会话直接测试，无需 API Key。

### Responses API（Codex CLI）

`POST /v1/responses` 提供 Responses 子集，供 [Codex CLI](https://github.com/openai/codex) 这类只走 Responses 的客户端接入。与 `/v1/chat/completions` 共用同一套选号 / 冷却 / 轮换 / 统计与会话粘性，只换入站映射与出口翻译（实现与取舍见 [TECHNICAL.md §3.7](TECHNICAL.md)）：

```bash
export CODING2API_KEY=sk-你的key
codex -c "model_providers.coding2api={ name='coding2api', base_url='http://127.0.0.1:8000/v1', wire_api='responses', env_key='CODING2API_KEY' }" \
      -c model_provider=coding2api \
      -c model='glm-5.2' \
      '你的任务'
```

（`-c key=value` 覆盖配置的写法与字段名取自 Codex 仓库自带的
`codex-rs/responses-api-proxy/README.md`，非凭记忆。）

支持文本、流式正文、思考摘要（`reasoning` item）、函数工具调用与 `finish_reason=length` → `response.incomplete`。**不支持**：`store=true`、`previous_response_id`（服务端无状态，不假装支持）、Responses 私有工具（`web_search` / `computer` / `custom` 等）；这些一律显式 400，不静默降级。`include=["reasoning.encrypted_content"]`（Codex 每轮必带）接受但忽略——本网关不产加密推理内容。

> 验证边界：本机无 Codex CLI，协议形状取自官方 `openai` SDK 类型并以其为客户端跑通全部契约，另对真实上游冒烟；未经真实 Codex CLI 端到端验证。

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

池内凭证从未探测成功时 `balance_known` 为 `false`（余额未知，不是 0）。管理台右上角「功能说明」按钮可随时查各功能入口。

### 池健康检查

`GET /health` 只回 `{"status":"ok"}`（进程存活，适合容器存活探针）；`GET /healthz`
额外给出凭证池计数，供外部监控在池子耗尽时提前告警——那是「服务活着但用不了」的
状态，存活探针看不出来。两个端点都不鉴权。

```json
{
  "status": "ok",
  "service": "coding2api",
  "version": "0.1.2",
  "credentials": {"total": 5, "ready": 4, "cooling": 1, "paused": 0, "disabled": 0}
}
```

五类计数互斥且合计 = `total`：`ready` 为当前可被调度选中的凭证（`disabled` /
`paused` / `cooling` 依次优先归入各自桶，与调度器同一口径）。`ready=0` 时对话请求
会直接返回 503，值得配置告警。

### API Key 的渠道绑定与来源 IP 白名单

创建 Key 时可以限定它只能走某个渠道、只能从某些 IP 调用，适合「按出口分发 Key」：
一个 Key 给团队用，另一个只给某台服务器 / 某个客户端。

- **渠道绑定**：选 CodeBuddy 或 TRAE 后，该 Key 的请求只在对应渠道的凭证里选号；
  请求的模型属于另一渠道时直接 400 并指出实际归属（不会静默改道，也不会白打一次
  上游）。留空 = 自动（默认，跨渠道选健康凭证）。`模型@渠道` 的强制指定与绑定冲突
  时同样 400。
- **来源 IP 白名单**：逗号分隔的 IP 或 CIDR（如 `203.0.113.9,10.0.0.0/8`），留空 =
  不限制。写入时会校验并规范化（`10.0.0.1` 存为 `10.0.0.1/32`），非法值当场 400。
  来源 IP 不在白名单内时返回 403。

**默认不采信 `X-Forwarded-For`**（该头由客户端可写，信它等于白名单形同虚设）。
只有在 `TRUST_PROXY=true` 时才按 XFF 判定，且取**最后一个**条目——那是紧邻本服务的
受信代理实际看到的地址。因此该开关只适用于「本服务前面恰好一层受信反代」的部署；
多层反代或直连请保持默认 `false`。

### 暂停单个凭证
凭证列表行内菜单的「暂停」只把该凭证摘出**对话流量**：后台的额度探测、token
预刷新、每日签到、成长中心、活跃上报照常运行（这些任务只认系统硬禁用 `disabled`）。
适合「这个号先别接聊天、但积分还要继续领」的场景；「取消暂停」立即放回池子。
与状态列的「已禁用」不同——那是渠道判定会话失效后的系统禁用，需要重新登录后用
「恢复」解除。同一个开关也存在于 `POST /api/credentials/{id}/toggle`。

### token 到期展示

凭证列表有独立的 **token 剩余** 列，显示该账号 access token 距离到期还有多久，剩余时间
低于 `TOKEN_EXPIRY_WARNING_SECONDS`（默认 1 小时）时标红并提示「即将到期」。

到期时间优先取上游显式给的 `expires_at`，缺失时回落到 access token 的 JWT `exp`——
**实测 CodeBuddy 的 token 响应（OAuth 登录与刷新）不带任何到期字段**，只看 `expires_at`
会恒为 0，这里正是靠 JWT 回落补上的（否则 CodeBuddy 的 token 预刷新永远不会触发，
只能等过期后被上游 401 硬禁用）。

两边都取不到时该格显示 `—`，**不猜本地 TTL**——否则管理台会显示一个凭空捏造的到期预警。
`iat`（最后续期）仍会落库（`credentials.token_issued_at`）供诊断，但不在列表展示：它需要
与剩余天数一起做二次推理才有意义，不适合占表格里的一行。

### 积分记录

凭证行内菜单的「积分记录」看每次余额变化的来龙去脉。它记录的是**两次额度探测之间的
净变化**，不是动作归因：签到、成长中心、对话消耗都会改余额，而**上游这些接口不打日志**，
探测只能看到区间净变化，分不出这几分是谁加的。所以界面一律写「净变化」，不写「签到 +5」
——把净变化说成某个动作的成果就是拿猜测当事实。

首次探测只建立基线（`sync`，不算积分）；余额没变不记（否则每轮探测落一行 0）；
余额变成「未知」（探测失败后）仍会记一行且不填变化量——「余额变未知」是该追的异常，
不能当成「没有变化」。记录与请求明细同样保留 90 天。

## 后台任务

额度探测、token 预刷新、每日签到、成长中心、明细清理由 `TaskRunner` 自动调度（失败互不影响），周期见 TECHNICAL.md §6.1。

成长中心仅 CodeBuddy 有：自动领取 Buddy 旅行礼物、派 Buddy 出发、领取新任务与任务奖、
断登补登、连登奖励兑换、开盲盒、能量开 Buddy 盲盒。Buddy 旅行 1–4 小时回来一次，
所以周期默认 60 分钟（`GROWTH_INTERVAL_MINUTES`），回来就领、不把礼物压到第二天。
抽奖 / 连登兑换 / 开 Buddy 盲盒 / 消耗补登卡属于**不可逆动作**，设
`GROWTH_IRREVERSIBLE_ACTIONS=false` 可全部跳过（仍会领旅行礼物与任务奖励）。
管理台凭证列表的「成长中心」列显示每个账号最近一轮的结果，行内菜单可手动执行一次。

### 活跃上报（可选，默认关闭）

CodeBuddy 成长中心的「连登天数 / 活跃地图」按日统计客户端对话事件。若账号长期
只被本网关自动调用（没有真实客户端对话），连登会断。`ACTIVITY_REPORT_ENABLED=true`
时，后台任务每天在 `ACTIVITY_REPORT_HOUR`（默认 10 点）所在的整点窗口内，为每个
账号补发**一条** `chat_request_send` 事件，续上连登——与积分、调度无关，上报失败
不影响任何聊天请求。管理台凭证行内菜单也可手动补报一次（不受该开关影响）。

> ⚠️ **风险与限制**：官方活动条款禁止使用模拟器/脚本篡改活动数据，处罚为**取消
> 资格并追回已发礼品**。本功能默认关闭，开启前请自行评估账号风险。上报的事件名与
> 请求形状依赖上游实现，**上游改版即失效**，不承诺任何收益。因此它**不作为可靠性
> 功能**，也不接收依赖「产品内真实功能使用」的任务。
>
> 实测（2026-09-21）：上游对缺少 `userId` 的上报返回 HTTP 200 `{"code":0}` 但
> **静默丢弃**（连登不变）。本网关的 OAuth 凭证 `user_id`/`account_uid` 可能为空，
> 此时从 bearer JWT 的 `sub` 取 userId，缺失则跳过（绝不编造）。

## 配置

常用项如下；完整的可配置项见 `src/config.py`（权威），且**每一个都已透传到 `docker-compose.yml`**——`.env` 里写这些变量即可生效（compose 的 `.env` 只做插值，未透传的变量不会进容器）。

> 容器里改监听地址用 `HOST`/`PORT`（`PORT` 同时决定宿主机映射端口），入口读 `config.py`，不硬编码。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | **必填** | 凭证加密密钥，≥16 字符；丢失 = 凭证全部作废，无密钥轮换 |
| `ADMIN_USERNAMES` | 空 | 管理员用户名，逗号分隔；空则全员只读 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 浏览器可达地址；TRAE 登录回调依赖它 |
| `DEFAULT_MODEL` | `glm-5.2` | 模型为空/`auto` 时的默认 |
| `USERS_FILE` | `secrets/users.txt` | 用户文件路径（`config.py` 的 `users_file`） |
| `DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `QUOTA_PROBE_MINUTES` | `60` | 额度探测周期 |
| `GROWTH_INTERVAL_MINUTES` | `60` | 成长中心（仅 CodeBuddy）一轮领取的周期；下限 5 分钟 |
| `GROWTH_IRREVERSIBLE_ACTIONS` | `true` | 是否允许成长中心的不可逆动作：抽奖、连登兑换、开 Buddy 盲盒、消耗补登卡。`false` 时仍会领取旅行礼物与任务奖励 |
| `ACTIVITY_REPORT_ENABLED` | `false` | 活跃上报（仅 CodeBuddy）：每天为账号补发一条对话事件续连登。**默认关闭**——官方条款禁止脚本篡改活动数据（处罚为取消资格并追回礼品），上游改版即失效，不作为可靠性功能（见上文「活跃上报」） |
| `ACTIVITY_REPORT_HOUR` | `10` | 活跃上报的本地（北京）时间整点窗口；仅在 `ACTIVITY_REPORT_ENABLED=true` 时生效 |
| `QUOTA_EXPIRY_WINDOW_SECONDS` | `129600` | 主到期排序窗口：把距到期 ≤ 该秒数的积分加总，多的账号先用（避免积分过期浪费）；`≤0` 关闭整套到期排序（次窗口一并失效），退回纯健康度排序 |
| `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` | `604800` | 次到期排序窗口：主窗口打平（常见的是都为 0）时才比较，`7 天`覆盖 CodeBuddy 一个完整的小包到期周期；`≤0` 关闭该级 |
| `CONVERSATION_STICKY_SECONDS` | `3600` | 会话粘性 TTL：优先按请求体显式会话标识（`conversation_id`/`conversationId`/`prompt_cache_key`，metadata 或顶层），无则回落消息前缀指纹，多轮请求固定用同一凭证（手动 pin 的凭证优先，粘性让位）；带 `user_id` 时不派生前缀兜底键（避免并行对话误钉同一号）；凭证出错仍会轮换，成功后重新粘定；`≤0` 关闭 |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,default,hunyuan-image-*` | 模型列表黑名单（fnmatch，仅影响列表展示，直连指定不受影响）；默认值按两边上游实测清单补入内部/不可用模型（`default` 零内容、`hunyuan-image-*` 400 11103），刻意不含 `*-volc` 与 `aquila`/`sagitta`/`seed-code-pro-0430`（实测可正常 chat）（见 TECHNICAL.md §3.5） |
| `ALLOWED_HOSTS` | 空 | Host 白名单，防 DNS rebinding |
| `TRUST_PROXY` | `false` | 是否采信 `X-Forwarded-For` 判定 API Key 的来源 IP（`allowed_ips` 白名单用）。默认关闭——该头由客户端可写；仅在「本服务前恰好一层受信反代」时开启，届时取 XFF 最后一个条目（见上文「API Key 的渠道绑定与来源 IP 白名单」） |
| `CODEBUDDY_API_ENDPOINT` | `https://copilot.tencent.com` | CodeBuddy 上游地址；改动时必须同时把它加入 `CODEBUDDY_ALLOWED_ENDPOINTS` |
| `CODEBUDDY_ALLOWED_ENDPOINTS` | 官方两站（见 compose） | 上游端点白名单，真实 Token 只发往白名单内地址 |
| `CODEBUDDY_CHAT_MIN_INTERVAL` | `5` | CodeBuddy 聊天最小间隔（秒），与 TRAE 共享节流；`0` 关闭 |
| `CODEBUDDY_SANITIZE_CHANNEL_MARKERS` | `true` | 出站 `system`/`assistant` 正文命中「伪装其他厂商官方客户端」指纹串时替换为占位符（上游 11128 内容风控：换号无效、会话带入即持续报错）；只改出站副本，客户端历史不受影响；`false` 关闭（见 TECHNICAL.md §3.2） |
| `REFRESH_SKEW_HOURS` | `24` | token 到期前该小时数窗口内预刷新。到期时间取凭证显式 `expires_at`，缺失时回落 access token 的 JWT `exp`（CodeBuddy 实测不带显式到期字段） |
| `TOKEN_EXPIRY_WARNING_SECONDS` | `3600` | 管理台 token 到期预警阈值：剩余低于该值时标红；`≤0` 关闭预警（仍显示剩余时间）。纯展示，不参与调度 |
| `PACER_MIN_SECONDS` / `PACER_MAX_SECONDS` | `5` / `20` | 全局节流器随机等待区间（秒） |
| `LOG_LEVEL` | `INFO` | 日志级别；审计日志是 INFO 级，调到 `WARNING` 会一并关掉 |
| `DUMP_REQUEST_BODIES` | `false` | 诊断：把 `/v1` 原始请求体落盘到 `data/dumps/`（**含对话内容**，仅排查用） |
| `AUTO_CONTINUE_MAX` | `10` | 上游以 `finish_reason=length` 截断时同凭证自动续写的最多次数；`0` 关闭（见 TECHNICAL.md §3.4） |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址与端口（compose 默认 `0.0.0.0`，`PORT` 同时决定宿主机映射端口） |

### 管理台热更（运行时配置）

上表中带「可热更」语义的 13 项可以不改 `.env`、不重启，直接在管理台「运行时配置」页修改：

`DEFAULT_MODEL`、`MODEL_BLOCKLIST`、`QUOTA_EXPIRY_WINDOW_SECONDS`、`QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS`、`CONVERSATION_STICKY_SECONDS`、`GROWTH_IRREVERSIBLE_ACTIONS`、`GROWTH_INTERVAL_MINUTES`、`QUOTA_PROBE_MINUTES`、`CODEBUDDY_CHAT_MIN_INTERVAL`、`PACER_MIN_SECONDS`、`PACER_MAX_SECONDS`、`ACTIVITY_REPORT_ENABLED`、`ACTIVITY_REPORT_HOUR`。

要点：

- **优先级 `DB 覆盖值 > .env`**：改过之后 .env 对该项不再生效，页面会标「DB 覆盖」；「恢复默认」删掉覆盖行，才重新回落 .env。日志同步记录是谁改的。
- 值存 `runtime_settings` 表（纯 key/value），新增可热更项不需要迁移；白名单外的 key、非法类型/越界值在写入前被拒，读取时坏行跳过并记警告。
- 启动期项（`APP_SECRET` / `PORT` / `DATA_DIR` / `USERS_FILE` / 上游端点白名单）**不在**白名单，改它们仍需重启：它们决定进程如何启动，运行期变更只会让内存与磁盘静默分叉。
- 接口：`GET /api/settings` 读快照，`PUT /api/settings` 写（admin + CSRF），body 形如 `{"values": {"QUOTA_PROBE_MINUTES 对应的 key": 15}}`，传 `null` 表示恢复默认。

## 部署注意

- **挂载目录属主**：容器内以 uid 1001（`appuser`）运行，`./data` 与 `./secrets`
  必须可写/可读，否则 SQLite 打不开：

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **时区**：镜像默认 `TZ=Asia/Shanghai`；如需其它时区显式覆盖 `TZ`。

## 日志

应用只写 **stdout / stderr**，不自己写文件也不自己轮转（原因见
[`src/webapp/logging.py`](src/webapp/logging.py) 顶部说明）：三种部署形态的
采集方式不同，但都靠这两个 fd 对接，轮转交给各自的平台工具。因此**日志自己
不会停止增长**——按下面对应形态配一次即可。

| 部署形态 | 日志到哪 | 轮转机制 | 需做什么 |
|---|---|---|---|
| **Docker / compose** | `docker logs coding2api` | json-file 驱动（已在 compose 配好 `10m × 5`） | **无需操作** |
| **macOS（launchd）** | `logs/launchd.{out,err}.log` | 系统自带 `newsyslog` | 跑一次 `./scripts/install-newsyslog.sh`（需 sudo） |
| **Linux（systemd）** | `journalctl -u coding2api` | journald 自带 | **无需操作**（模板见 `deploy/systemd/`） |
| **Linux（非 systemd）** | 重定向到 `/var/log/coding2api/*.log` | `logrotate` | `sudo cp deploy/logrotate/coding2api /etc/logrotate.d/` |
| **裸跑**（`uv run python -m src.main`） | 终端 stderr | 无 | 自己重定向并自备轮转 |

级别用 `LOG_LEVEL`（默认 `INFO`）控制。**审计日志（凭证增删改/pin/账号切换）
是 INFO 级**，把 `LOG_LEVEL` 调到 `WARNING` 会把它一并关掉。

```bash
# macOS：装轮转规则（单文件超 10MB 转，留 7 份，bzip2 压缩）
./scripts/install-newsyslog.sh
./scripts/install-newsyslog.sh --uninstall   # 卸载

# 看日志
tail -f logs/launchd.err.log          # macOS
docker logs -f coding2api             # 容器
journalctl -u coding2api -f           # systemd
```

> 容器里的 `data/dumps/`（诊断开关 `DUMP_REQUEST_BODIES=true` 写入）不在
> docker 日志体系内，由应用自行保持最多 200 份。

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

## 状态

M0–M3 及后续迭代（运维化、成长中心自动化、额度包明细）全部完成，当前版本 v0.1.2，`main` 分支可运行。

## 授权协议

MIT，见 [LICENSE](LICENSE)。借鉴的上游项目署名见 [NOTICE](NOTICE)。