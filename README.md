# Coding2API

把 **CodeBuddy** 与 **TRAE SOLO** 两个 coding agent 渠道，统一封装为 OpenAI 兼容 API，
提供公共凭证池、统一调度与按人用量统计。

> [!WARNING]
> 逆向接口，仅供学习研究，未做安全审计；公网部署需反向代理 + 鉴权 + IP 白名单。

## 特性

- **OpenAI 兼容出口**：`/v1/chat/completions`（流式 + 非流式）、`/v1/responses`（Codex CLI）、`/v1/models`、`/v1/user/balance`（DeepSeek 兼容余额）
- **统一调度**：扁平模型名按健康度自动选号，`模型@渠道` 强制指定；三态健康度 + 分级冷却避开坏号。模型级限流或「该渠道无此模型」只避让那一个模型，同账号其他模型立刻可用
- **到期积分优先消化**：主窗口 36h 内将过期的积分多者先用（避免过期浪费），打平再比 7 天窗口；管理台凭证列表显示到期积分与逐个额度包明细
- **会话粘性**：同一对话多轮粘住同一凭证，出错才轮换
- **公共凭证池**：admin 集中维护、全员共享；按人统计用量
- **三角色账号体系**：`admin` / `operator` / `viewer`，用户存 SQLite；一次性激活链接自设密码（无共享初始密码）、首登强制改密、改角色/停用即时吊销会话；登录与写操作留审计
- **完整凭证运维**：设备码登录、多账号切换、额度探测、每日签到（含连续天数）、token 预刷新；凭证加密入库（`APP_SECRET`）
- **成长中心**（仅 CodeBuddy）：自动领 Buddy 旅行礼物、派 Buddy、领取新任务与任务奖、断登补登、连登奖励兑换、开盲盒、能量开 Buddy 盲盒；不可逆动作可用 `GROWTH_IRREVERSIBLE_ACTIONS=false` 关停；管理台可手动执行并查看逐条结果
- **脱敏统计**：不存对话内容；明细 90 天、小时汇总永久；按人/渠道/模型可视化
- **管理台安全加固**：登录限流、CSRF 校验、请求体上限、Host 白名单

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

> `scripts/create_user.py` 直接写 SQLite（`--db` 或 `DATA_DIR`，默认 `data/coding2api.sqlite3`）。
> 老部署若已有 `secrets/users.txt`，启动时会自动导入一次（已存在的用户不覆盖），
> 也可继续用 `scripts/hash_password.py` 写文件作引导。

## 用户与角色

账号存在 SQLite 的 `users` 表里，管理台「用户管理」页（仅管理员可见）负责日常增删改。

三档角色：

| 角色 | 能做什么 |
|---|---|
| `admin` | 用户管理、任务与配置、凭证读写、全部统计、审计日志 |
| `operator` | 凭证读写（含导入 / 删除 / 启停 / 签到 / 成长 / 切换账号）、全部统计 |
| `viewer` | 只读：看仪表盘、凭证列表、统计与 Playground |

**加人**：管理员在「用户管理」填用户名 + 角色，创建后得到**一次性激活链接**（有效 24 小时，只显示这一次）。把链接通过可信渠道发给本人，对方打开 `/activate` 自设密码即可登录——系统里不存在「管理员知道但用户不知道」的初始密码。忘记密码同理：点「重置密码」生成新链接。

**改角色 / 停用**：都是即时生效——改角色或停用会立刻作废该账号已签发的所有会话 Cookie（重新登录即可）。停用是默认的「删人」方式（可逆，用量统计仍归属该用户名）；**硬删只在 CLI**（`scripts/create_user.py <用户名> --delete --force`），因为硬删会让 `usage_events` 里留下查不到用户名的孤儿记录。

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

「审计日志」页（仅管理员可见）记录登录与写操作，可按操作者与动作筛选、分页查看：

- **登录**：成功与失败都记（失败登录留痕正是审计的价值——「谁在什么时候试了哪个账号」）
- **账号变动**：新建、激活、改角色、禁用/启用、改密、重置密码、硬删
- **凭证写操作**：导入、删除、启停、指定（pin）、复活、切换账号

每条含时间、操作者、动作、对象、说明与来源 IP。**审计记录绝不包含密码或令牌明文**（一次性激活令牌的明文只在创建响应里出现一次，库里只有 SHA-256 摘要）。保留期与请求明细一致（默认 90 天），由后台清理任务统一裁剪。

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
# 或指定版本：docker pull ghcr.io/robbsluo/coding2api:v0.2.0
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

（`-c key=value` 的写法与字段名取自 Codex 仓库自带的 `codex-rs/responses-api-proxy/README.md`，非凭记忆。）

支持文本、流式正文、思考摘要（`reasoning` item）、函数工具调用与 `finish_reason=length` → `response.incomplete`。**不支持** `store=true`、`previous_response_id`（服务端无状态，不假装支持）、Responses 私有工具（`web_search` / `computer` / `custom` 等）——一律显式 400，不静默降级。`include=["reasoning.encrypted_content"]`（Codex 每轮必带）接受但忽略，本网关不产加密推理内容。

> 验证边界：开发环境无 Codex CLI；协议形状取自官方 `openai` SDK 类型并以其为客户端跑通全部契约，另对真实上游冒烟，未经真实 Codex CLI 端到端验证。

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
  "version": "0.2.0",
  "credentials": {"total": 5, "ready": 4, "cooling": 1, "paused": 0, "disabled": 0}
}
```

五类计数互斥且合计 = `total`，与调度器同一口径（`disabled` / `paused` / `cooling` 依次优先归入各自桶，其余为 `ready`）。`ready=0` 时对话请求直接返回 503，值得配置告警。

### API Key 的渠道绑定与来源 IP 白名单

创建 Key 时可限定它只能走某个渠道、只能从某些 IP 调用，适合「按出口分发 Key」：一个给团队用，另一个只给某台服务器或某个客户端。

- **渠道绑定**：选 CodeBuddy 或 TRAE 后，该 Key 只在对应渠道的凭证里选号；模型属于另一渠道时直接 400 并指出实际归属（不静默改道，也不白打一次上游）。留空 = 自动（默认，跨渠道选健康凭证）。`模型@渠道` 与绑定冲突时同样 400。
- **来源 IP 白名单**：逗号分隔的 IP 或 CIDR（如 `203.0.113.9,10.0.0.0/8`），留空 = 不限制。写入时校验并规范化（`10.0.0.1` 存为 `10.0.0.1/32`），非法值当场 400；来源不在白名单内返回 403。

**默认不采信 `X-Forwarded-For`**（客户端可写，信它等于白名单形同虚设）。仅 `TRUST_PROXY=true` 时按 XFF 判定，且取**最后一个**条目（紧邻本服务的受信代理实际看到的地址）。故该开关只适用于「本服务前恰好一层受信反代」；多层反代或直连请保持默认 `false`。

### 暂停单个凭证

凭证列表行内菜单的「暂停」只把该凭证摘出**对话流量**：后台的额度探测、token 预刷新、每日签到、成长中心、活跃上报照常运行（这些任务只认系统硬禁用 `disabled`）。适合「先不接聊天、但积分继续领」；「取消暂停」立即放回池子。与状态列的「已禁用」不同——那是渠道判定会话失效后的系统禁用，需重新登录后用「恢复」解除。同一开关也在 `POST /api/credentials/{id}/toggle`。

### token 到期展示

凭证列表的 **token 剩余** 列显示 access token 距到期还有多久，低于 `TOKEN_EXPIRY_WARNING_SECONDS`（默认 1 小时）时标红并提示「即将到期」。

到期时间优先取上游显式 `expires_at`，缺失时回落 access token 的 JWT `exp`——**实测 CodeBuddy 的 token 响应（OAuth 登录与刷新）不带任何到期字段**，只看 `expires_at` 会恒为 0；正是靠 JWT 回落补上，否则 CodeBuddy 的 token 预刷新永不触发，只能等过期后被上游 401 硬禁用。两边都取不到时显示 `—`，**不猜本地 TTL**（否则就是一个凭空捏造的到期预警）。`iat`（最后续期）仍落库（`credentials.token_issued_at`）供诊断，但不在列表展示：它与剩余天数需一起做二次推理才有意义，不值一行。

### 积分记录

凭证**额度**列数字（如 `4,872.77 / 4,950`）后面的**下箭头**展开「积分记录」，记录的是**两次额度探测之间的净变化**，不是动作归因：签到、成长中心、对话消耗都会改余额，而**上游这些接口不打日志**，探测只能看到区间净变化。所以界面一律写「净变化」，不写「签到 +5」——把净变化说成某个动作的成果就是拿猜测当事实。

下箭头是「在本行内展开」而非弹层：表格行本身就是最好的上下文，弹层会遮掉额度列。展开后箭头翻转，再点一次收起；只读视图不显示该入口。

首次探测只建立基线（`sync`，不算积分）；余额没变不记（否则每轮探测落一行 0）；余额变成「未知」（探测失败后）仍记一行且不填变化量——「余额变未知」是该追的异常，不能当成「没有变化」。记录与请求明细同样保留 90 天。

## 后台任务

额度探测、token 预刷新、每日签到、成长中心、活跃上报、明细清理由 `TaskRunner` 自动调度，失败互不影响；周期见 TECHNICAL.md §6.2。每类任务的上次执行时间、最近结果与错误在管理台**「任务与配置」页**查看（30 秒自动刷新），运行态只存在于**本次进程**内，重启归零。

成长中心仅 CodeBuddy 有（动作清单见上文「特性」）。Buddy 旅行 1–4 小时回来一次，故周期默认 60 分钟（`GROWTH_INTERVAL_MINUTES`），回来就领、不把礼物压到第二天。抽奖 / 连登兑换 / 开 Buddy 盲盒 / 消耗补登卡属**不可逆动作**，`GROWTH_IRREVERSIBLE_ACTIONS=false` 可全部跳过（仍领旅行礼物与任务奖励）。凭证列表的「成长中心」列显示每个账号最近一轮的结果，行内菜单可手动执行一次。

### 活跃上报（可选，默认关闭）

CodeBuddy 成长中心的「连登天数 / 活跃地图」按日统计客户端对话事件，账号长期只被本网关自动调用（没有真实客户端对话）时会断连登。`ACTIVITY_REPORT_ENABLED=true` 时，后台任务在每天 `ACTIVITY_REPORT_HOUR`（默认 10 点）所在的整点窗口内为每个账号补发**一条** `chat_request_send` 事件，续上连登——与积分、调度无关，上报失败不影响任何聊天请求。管理台凭证行内菜单也可手动补报一次（不受该开关影响）。

> ⚠️ **风险与限制**：官方活动条款禁止使用模拟器 / 脚本篡改活动数据，处罚为**取消资格并追回已发礼品**。默认关闭，开启前请自行评估账号风险。事件名与请求形状依赖上游实现，**上游改版即失效**，不承诺任何收益——因此它是验证与运维手段，不作为可靠性功能，也不承担「依赖产品内真实使用」的任务。
>
> 实测（2026-09-21）：缺少 `userId` 时上游返回 HTTP 200 `{"code":0}` 却**静默丢弃**（连登不变）。OAuth 凭证的 `user_id`/`account_uid` 可能为空，此时回落取 bearer JWT 的 `sub`，仍缺失则跳过（不编造）。

## 配置

常用项如下；完整的可配置项见 `src/config.py`（权威），且**每一个都已透传到 `docker-compose.yml`**——`.env` 里写这些变量即可生效（compose 的 `.env` 只做插值，未透传的变量不会进容器）。

> 容器里改监听地址用 `HOST`/`PORT`（`PORT` 同时决定宿主机映射端口），入口读 `config.py`，不硬编码。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | **必填** | 凭证加密密钥，≥16 字符；丢失 = 凭证全部作废，无密钥轮换 |
| `ADMIN_USERNAMES` | 空 | **仅引导期**：逗号分隔的用户名，首次启动时被提权为 `admin`。DB 里已有角色后不再生效——日常改角色在管理台「用户管理」页做（见下文「用户与角色」） |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 浏览器可达地址；TRAE 登录回调依赖它 |
| `DEFAULT_MODEL` | `glm-5.2` | 模型为空/`auto` 时的默认 |
| `USERS_FILE` | `secrets/users.txt` | **仅引导期**：老式用户文件路径，启动时一次性导入 SQLite（已存在的用户名不覆盖，幂等）。账号唯一源是 `users` 表 |
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
| `ENABLE_DOCS` | `false` | 是否暴露 `/docs` 与 `/openapi.json`（默认关闭：匿名可拉全量 API 结构）；本地调试需 Swagger 时置 `true` |
| `DUMP_REQUEST_BODIES` | `false` | 诊断：把 `/v1` 原始请求体落盘到 `data/dumps/`（**含对话内容**，仅排查用） |
| `AUTO_CONTINUE_MAX` | `10` | 上游以 `finish_reason=length` 截断时同凭证自动续写的最多次数；`0` 关闭（见 TECHNICAL.md §3.4） |
| `UPSTREAM_COMPLETE_TIMEOUT_SECONDS` | `600` | 非流式聚合整体超时（秒）：上游连接半开停滞会让非流式请求无限悬挂并占住凭证，超时按瞬态错误换号重试（流式路径有心跳兜底不受影响）；`≤0` 关闭 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址与端口（compose 默认 `0.0.0.0`，`PORT` 同时决定宿主机映射端口） |

### 管理台热更（「任务与配置」页）

上表中带「可热更」语义的 13 项可不改 `.env`、不重启，直接在管理台「任务与配置」页修改：

`DEFAULT_MODEL`、`MODEL_BLOCKLIST`、`QUOTA_EXPIRY_WINDOW_SECONDS`、`QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS`、`CONVERSATION_STICKY_SECONDS`、`GROWTH_IRREVERSIBLE_ACTIONS`、`GROWTH_INTERVAL_MINUTES`、`QUOTA_PROBE_MINUTES`、`CODEBUDDY_CHAT_MIN_INTERVAL`、`PACER_MIN_SECONDS`、`PACER_MAX_SECONDS`、`ACTIVITY_REPORT_ENABLED`、`ACTIVITY_REPORT_HOUR`。

要点：

- **优先级 `DB 覆盖值 > .env`**：改过后 .env 对该项不再生效，页面标「DB 覆盖」；「恢复默认」删掉覆盖行才回落 .env。日志记录改动者。
- 值存 `runtime_settings` 表（纯 key/value），新增可热更项无需迁移；白名单外的 key、非法类型 / 越界值写入前即拒，读取时坏行跳过并记警告。
- 启动期项（`APP_SECRET` / `PORT` / `DATA_DIR` / `USERS_FILE` / 上游端点白名单）**不在**白名单：它们决定进程如何启动，运行期改只会让内存与磁盘静默分叉。
- 接口：`GET /api/settings` 读快照，`PUT /api/settings` 写（admin + CSRF），body `{"values": {key: value}}`，传 `null` 恢复默认。
- 该页同时展示**后台任务运行态**：6 类任务的周期、上次执行时间、最近结果与错误；配置项按所属任务分组进卡片，其余（默认模型、黑名单、到期窗口、节流等）归「网关与调度」区。
- 运行态是**进程内**的（`GET /api/tasks`，admin，页面每 30 秒刷新）：只显示「本次启动以来跑过没有」，**重启归零**，不落库、不留历史。未到点或未开启的轮次不算执行——否则签到会显示成「刚刚跑过」，而当天一次都没签。

## 部署注意

- **挂载目录属主**：容器内以 uid 1001（`appuser`）运行，`./data` 与 `./secrets`
  必须可写/可读，否则 SQLite 打不开：

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **时区**：镜像默认 `TZ=Asia/Shanghai`；如需其它时区显式覆盖 `TZ`。

### 升级后必须重启后端

**改动 `src/` 后必须重启进程，否则会出现「新前端 + 旧后端」的错配。**

典型症状：管理台页面能打开（前端产物本就是静态文件，浏览器直接拿新的），但调用新端点全部失败——旧后端没有该路由，未匹配的 `/api/*` 按约定返回 JSON `404`，前端把它当成通用失败，于是弹出与实际原因无关的提示。B5 上线时就踩过：`/api/users` 在旧进程里返回 `404`，新建用户报「用户名可能已存在，或角色非法」，而真正原因是**服务跑的还是迁移前的代码**（老库 `PRAGMA user_version` 仍是 13、没有 `users` 表）。

前端不需要重启的原因见下文（后端每次请求现读 `web/dist`）；**后端代码不是**——进程管理器只在进程**退出**时重新拉起，不监听源码变化。

```bash
# Docker / compose
docker compose up -d --force-recreate

# systemd
sudo systemctl restart coding2api

# 裸跑 / 其他进程管理器：按你自己的方式重启该进程
```

**确认升级已生效**（先查版本，再查路由）：

```bash
# 1) schema 版本已迁移（期望 14）且账号已导入
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

应用只写 **stdout / stderr**，不自己写文件也不自己轮转（原因见 [`src/webapp/logging.py`](src/webapp/logging.py) 顶部说明）：部署形态不同、采集方式不同，但都靠这两个 fd 对接，轮转交给各自的平台工具。因此**日志自己不会停止增长**——按下面对应形态配一次即可。

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

## 状态

M0–M3 及后续迭代全部完成，`main` 分支可运行，当前版本 v0.2.0。

后续批次（B1–B4）已按批准计划落地：

- **B1 请求质量**：错误分类细分 + 模型级冷却、出站指纹清洗（11128 内容风控）、截断续写、会话粘性键、模型元数据/黑名单
- **B2 协议出口**：`/v1/responses`（Codex CLI 子集）；Anthropic `/v1/messages` **暂不做**（当前无 Claude Code 场景，架构已预留中立事件层，后续按需补）
- **B3 运维**：凭证暂停语义、运行时配置热更、token 到期展示、积分变动流水、池健康 `/healthz` + 多 Key 出口/IP 绑定
- **B4 任务可视化**：后台任务运行态并入「任务与配置」页；模型黑名单热更延迟修复
- **B5 账号体系**：用户从 `users.txt` 迁入 SQLite、三角色 RBAC、会话吊销（epoch）、一次性令牌激活 + 首登强制改密、用户管理页、审计日志页、硬删降为 CLI

规划与实测收窄的完整记录见 `PROPOSAL.md`（Q32–Q39）与 `TECHNICAL.md`（§3.4–§3.13、§6.1–§6.4）。

## 授权协议

MIT，见 [LICENSE](LICENSE)。借鉴的上游项目署名见 [NOTICE](NOTICE)。