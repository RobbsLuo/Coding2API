# Coding2API 设计文档

把 **CodeBuddy**（国内站 `codebuddy` + 国际版 `codebuddy_intl`）、**TRAE SOLO**、**OpenCode Zen**、**Kilo Gateway**、**Qoder**、**CodeArts** 七个上游渠道封装为 OpenAI 兼容 API，提供公共凭证池、统一模型名与调度、按人用量统计。

> 现状文档：只记录仍然成立的目标、上游事实、架构与约束。实现细节见 [TECHNICAL.md](TECHNICAL.md)，使用与部署见 [README.md](README.md)。

## 1. 目标与非目标

**目标**：单一 OpenAI 兼容出口后挂七个上游，支持 `模型@渠道` 精确指定；凭证由 admin 集中维护、全员共享，调度器自动挑健康号、上游死亡自动冷却；按人统计用量（请求数 / 成功率 / token / 耗时 / 首字延迟）与估算成本。

**非目标（明确不做）**：**不做配额/限流**（上游是订阅制通道，成本不随 token 线性增长；小规模团队靠统计页可见性约束滥用）；**不做通用 provider 网关**（只硬编码上述七个上游，不做插件系统）；**不做货币换算**（上游积分单位互不通用，分开记录）；不做旧项目数据迁移、不做自更新脚本、不做 mTLS。

## 2. 上游可行性核实

### 2.1 CodeBuddy 国内站（`codebuddy`）

- 端点 `https://copilot.tencent.com`；聊天 `POST /v2/chat/completions`，**只支持流式**（非流式由本地聚合）；头 `Authorization` + `X-User-Id` + `X-Domain` + `X-Enterprise-Id` + `X-Department-Info`（部门名须 UTF-8 百分号编码）。认证为设备码：`POST /v2/plugin/auth/state?platform=CLI` 拿 `authUrl`/`state`，轮询 `POST /v2/plugin/auth/token?state=...`；账号切换 `/v2/plugin/login/account`、`/v2/plugin/accounts`。
- 额度：个人版 `POST /v2/billing/meter/get-user-resource`（`CycleCapacity*Precise`），企业版 `POST /v2/billing/meter/get-enterprise-user-usage`（`credit` 已用、`limitNum` 总额）。签到 `POST /billing/meter/daily-checkin`，状态 `POST /billing/meter/checkin-activity-status`。
- **成长中心**（前缀 `/v2/activity/growth`）：只读 `buddy/travel/status`、`buddy/travel/config`、`tasks`、`streak`、`redeem/summary`、`lottery/chances`、`buddy/quota`、`energy`；写入 `buddy/travel/claim`、`buddy/travel/depart`、`tasks/accept`、`/tasks/{code}/claim`、`makeup-cards/use`、`redeem`、`lottery/draw`、`buddy/open`。契约：`accept_status` 五态、`/tasks/accept` 收复数数组 `{"task_codes": [...]}`（传单数一律 400）、`/redeem` 的 `tier` 是档位标识字符串（`"7d"`/`"14d"`/`"28d"`，传档位名得 400）、实发字段为 `*_granted`；进程内 OAuth bearer 可直连，无需桌面端凭据文件。
- **凭证身份可能为空**：OAuth 路径下上游未回填 `account_uid`/`user_id`，同账号隔离必须回落到 `credential_id`，否则第二个账号会被静默跳过。**活跃上报**（`POST /v2/report`，默认关闭）：body 为事件数组（`eventCode=chat_request_send`），`userId` 必填——缺失时上游 HTTP 200 `code:0` 但静默丢弃；身份为空时回落 bearer JWT 的 `sub`。与积分、调度无关。
- **reasoning 客户端给什么透传什么**（默认口径），`reasoning_content` 不剥离（实测 71 份真实 dump）；`reasoning_effort` 缺失时补 `medium`、显式值不覆盖（不发该字段上游退化为「可见推演」：思考写进 `delta.content`、`reasoning_content` 恒空、`reasoning_tokens` 恒 0），`reasoning_tokens` 在 `completion_tokens_details` 里、顶层恒缺。**可选机制（默认全关，不改默认口径）**：出站 `REASONING_EFFORT_MAP`（按渠道改写客户端显式档位）、Anthropic 入站 `ANTHROPIC_THINKING_EFFORTS`（`thinking.budget_tokens` → 档位）、`FORCE_DISABLE_THINKING`（响应侧剥离思考内容，只过滤响应、不改请求体）。三者默认关 / 留空时行为与上面逐字节一致；内置默认矩阵仍不做（未核实上游接受性），见 TECHNICAL §3.11。**输出上限键名不对称**：CB 上游完全忽略 `max_completion_tokens`，只认 `max_tokens`；TRAE 对两个键都不生效——本网关不做键映射，客户端限额原样透传。
- 错误码：`6004` 模型用量超限（模型级）、`14003` 模型饱和（模型级，国际版 `Model busy. Please switch models or retry later`）、`11102` 该后端无此模型（负缓存）、`14018` 积分耗尽、`1005` 权益/套餐耗尽、`11101/11115/11128/11135` 请求级（不罚号）。`6004` 与 `14003` 都是「换模型即可用」，必须落模型级；落账号级 SOFT 会让整张凭证的其他模型一起被 60s 冷却。

### 2.2 TRAE SOLO（`trae`）

- Host：Agent `https://trae-api-cn.mchost.guru`、UG `https://api.trae.cn`、OAuth `https://api.trae.com.cn`；聊天 `POST /api/agent/v3/llm_utils_chat`，模型 `POST /api/ide/v1/get_detail_param`，均仅流式。认证：浏览器登录 → 302 回调 → `ExchangeToken` → `GetUserInfo`；刷新 `POST /cloudide/api/v3/trae/oauth/ExchangeToken`（refreshToken 轮换）；凭证 JSON 里的 `apiHost` 是用户可控输入，导入时按官方地址白名单校验。签到 `/trae/api/v2/ug/checkin_credits/{status,claim}`；额度 `/trae/api/v2/pay/ide_user_ent_usage`。
- **签到成功必须「确认到账」，不能只看返回码**：`claim` 对当天已签过的账号也返回 `code:0 success`（幂等），判定需 `checked_in` 为真且回查 `credits` 确有增加。**签到 9074 按设备标识处理**：数字串是必要非充分条件；每次 claim 生成新的 16 位数字串，一轮内最多换号重试 2 次，其余交给 10 分钟周期。
- SSE 事件序列 `metadata` → `timing_cost` → `output`×N → `extra_info` → `token_usage` → `done`；`token_usage` 含 `cache_read_input_tokens` / `cache_creation_input_tokens`，映射为统计的 `cached_tokens`，**无 per-request credit**；流内 `error` 码 `1005` = 权益不足，`4008` = 请求额度/配额耗尽（实测 `Your requests have exceeded the quota.`，与 `14018` 同为 CREDIT：冷却到次日签到），`4001` = 参数/模型不可用。接受客户端传来的 `reasoning_effort`，不认 `thinking` 对象；`developer` 角色上游不认（静默空流），已归一为 `system`。

### 2.3 Qoder（`qoder`，真实账号）

- 域：国内 `openapi.qoder.com.cn` / 网关 `gateway.qoder.com.cn`；国际 `openapi.qoder.sh` / `api1.qoder.sh`。聊天 `POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation`，**只支持流式**（信封式 SSE）。签名为自定义 Base64 变体（三段轮转 + 自定义字母表 + `=`→`$`），`Authorization: Bearer COSY.<payload_b64>.<md5sig>`，整套 `cosy-*` 请求头（含按 `uid` 稳定派生的 `cosy-machineid`/`cosy-machinetoken`）。登录为**设备码 PKCE（S256）**：`{website}/device/selectAccounts` 生成 challenge/nonce → 轮询 `GET {openapi}/api/v1/deviceToken/poll`（404/202 = 待授权）→ `POST /api/v1/deviceToken/refresh` 续期 → `GET /api/v1/userinfo` 补 `uid`/昵称/组织。
- 模型 `GET {gateway}/algo/api/v2/model/list?Encode=1`，**必须带整套 COSY 签名头**（裸 GET 403，带头 POST/PUT 被 400 拒绝，故方法固定 GET）；额度 `GET {openapi}/api/v2/quota/usage`（`userQuota` + `addOnQuota`）；套餐 `GET /api/v2/user/plan`。
- **入站消息归一**：`build_chat_body` 在转发前丢弃**孤立 `tool` 消息**（没有紧邻的、声明了该 `tool_call_id` 的 `assistant.tool_calls`）——上游对这类历史一律 400 `Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`。只丢弃、不重排、不合并、不改 content；丢弃条数记 warning，不落消息正文。并行 tool_calls（一个 assistant 多条 tool）与 `/v1/responses` 拆出的连续多条 assistant 声明都按并集识别，避免误删真实结果。流内错误正文保留到 2000 字符（200 会截在转义 `details` 中间，根因永久丢失）。
- 签到（**活动制**）：`GET /sash/api/v1/me/campaigns` → 筛 `actionType=="CLAIM_BENEFIT"` 且 `claimStatus=="CLAIMABLE"` → `POST /sash/api/v1/me/campaigns/{campaignId}/claim`；**两个请求都必须带 `Cosy-ClientType: 10`**（缺则上游静默返回空 campaign 列表）；`replayed:true` 与 `BLOCKED`+`SAME_PERSON_ALREADY_CLAIMED` 归一为当日已签。旧 `/sash/api/v1/me/daily-check-in/{status,claim}` 仅作回退（当前旧 status 返回 `DISABLED`）；新协议不提供连续天数。**活动每日 10:00（UTC+8）刷新**，故封账到当前窗口结束而非自然日。**国际版该端点 404 → 本区域无此接口，不算错误**。
- **上游节点故障包成 400**：`[FAIL]node:… msg:Execution failed` 归模型级瞬时故障（只冷却该模型），不误判为模型不存在。

### 2.4 CodeArts（`codearts`，真实账号）

- 域：snap 引擎 `snap-access.cn-north-4.myhuaweicloud.com`、STS `sts.cn-north-4.myhuaweicloud.com`、福利网关 `opengw.developer.huaweicloud.com`、门户 `codearts.huaweicloud.com`。聊天 `POST /api/v2/chat/completions`（福利模型追加头 `maas_type: benefit`）；鉴权为华为云 **`SDK-HMAC-SHA256`**（AK/SK + `X-Security-Token`）。OAuth2 PKCE → `POST {snap-manager}/v1/oauth2/tokens` 换 AK/SK/security_token/refresh_token。模型内置 `GET {snap}/v1/model/builtin`（头 `Agent-Type: PromptCenter`）；福利 `GET {opengw}/api/v1/gateway/config`；领取 `POST /api/v1/benefit/claim`；余额 `GET /api/v1/user/tokens/balance`（每日池）；积分账 `GET {snap-manager}/v1/statistics/plugin`（头 `Agent-Type: PromptCenter`）。
- **令牌刷新是刚性的**：`refresh_token` 与 `client_id=codearts-agent` + DPoP 私钥三者绑定、**一次性**，刷新走 **DPoP ES256/P-256**，刷后必须回写新 `refresh_token`，由 token 预刷新任务独占轮转。续期凭据被上游作废时标记「需重新登录」并停止自动重试。
- **每日签到**：`GET {snap}/v1/ops/delivery?channel=IDE` → `POST /v1/ops/claim`（`{campaignId:1, idempotentKey, channel:"IDE"}`）→ `POST /v1/ops/confirm`（`{campaignId:1}`）；**`/v1/ops/*` 挂在 snap 引擎根、不带 `snap-manager` 前缀**。`status` 四态必须区分：`ELIGIBLE`→claim+confirm；`CLAIMED`（已领未确认，非终态）→只补 confirm；`CONFIRMED`/`CONSUMED`→已签不写。**claim 时积分即到账**，confirm 只推进状态；业务码走 HTTP 200，不能只看状态码。
- **单位折算**：上游余额按 token 计量（每日池 1000 万），统一折算为积分，**1 积分 = 10000 token**。该「积分」是为跨渠道排序合成的折算值，与上游签到/套餐积分同名不同物。**积分与每日 token 池是两套并行的账**：token 池给福利模型用，签到积分给系统内置模型用。**两本账都探测并展示**：`GET {snap}/snap-manager/v1/statistics/plugin`（前缀必带 `snap-manager`）的 `usageTotalPackageCredit` 进 `credentials.quota_credits_*`，分项包（基础/按需/福利）走 `quota_packages` 明细，套餐 `end_date` 登记到期阶梯（积分用不完作废，故也参与「快过期的先用」）；积分账**不参与调度口径的 health**（选号仍按每日池），管理台内置组另用派生的 `quota_credits_health` 让健康度与该组额度同源。**福利模型单请求扣池**：上游 usage 缺额度字段时按「输入 + 输出 token」补 `credit` 并标 `credit_estimated`（展示加 ≈）。
- **凭证页把 CodeArts 拆成两个渠道**（纯前端展示拆分，后端仍一个 `codearts` provider）：**CodeArts 内置**（可写，承载全部操作与积分账）与 **CodeArts 福利**（只读，只展示每日 token 池），同一批账号各出现一次，**每组只讲一本账**（内置组额度列＝积分账、流水读 `ledger=credits`；福利组＝每日池）。**到期积分也按账本拆**：积分那一段另存 `quota_credits_expiry_ladder`（选号仍读两本账合并的 `quota_expiry_ladder`），管理台 `quota_expiring_credits` ＝「合并阶梯 − 积分账段」＝每日池段、`quota_credits_expiring` ＝积分账段，福利 / 内置两组各显各的。**为什么不在后端真拆**：`refresh_token` 一次性且与 `client_id` + DPoP 私钥绑定，两条凭证行会各刷一次、第二条必报 `the refresh token has been used` 并烧掉票据。两本账的区分轴是**模型**（`is_benefit_model()` 逐请求判断），不是渠道。
- **并发上限**：上游硬限**每账号每约 60s 最多 3 个会话**，超出即 `400 TM.00001041`；配在途上限 `CODEARTS_MAX_CONCURRENCY` 与滑动窗口 `CODEARTS_REQUEST_WINDOW_SECONDS`。流式 `text` 是**累计全文（替换语义）**，解析层还原为增量事件。
- **流内业务错误分类**：业务错误内嵌在 HTTP 200 的 SSE 里（`InferHub.<码>`），由 executor 转成冷却 + 轮换。分类统一由 `Event.error_kind` 承载，`UpstreamStreamError.kind()` 与 `executor._event_kind` 共用 `provider.base.event_error_kind` 兜底（缺失 → `OTHER`），避免两处各写一份而漂移。非多模态模型收到图片（`InferHub.001001020.406 The request model is not multimodal`）归 `REQUEST`：请求侧错误不冷却、不累计、不落库，仍换号重试。

### 2.5 OpenCode Zen（`zen`，虚拟凭证）

- 端点 `https://opencode.ai`，`/zen/v1/chat/completions` 与 `/zen/v1/models`。协议是标准 OpenAI SSE，**不逆向**。
- 渠道私有的是**免费层门禁伪装**：UA `opencode/<version>` ≥1.18.0（否则 426；无版本号 403）、`x-opencode-session`、body `stream:true`、body `tools` 必须同时含 `bash` 与 `read`。对策：只在缺失时补骨架工具；模型调用注入名时改写为客户端声明的同义工具（`bash`→`shell`/`execute`、`read`→`read_file`）并复制其 schema，无同义工具才丢弃并记 WARNING。
- **免费模型清单完全动态**：免鉴权 `/zen/v1/models` 返回全部模型（含付费）且不带免费标记，唯一权威信号是匿名可用性（付费恒 401 `Missing API key.`），按 `-free` 后缀收窄 + 并发探活只留 2xx，判活按 30 分钟缓存；免费模型标 **x0 倍率**（`credit_rate=0.0`）。服务层列表拉取**连续失败按 2^(n-1)×TTL 退避重试**（封顶 2h、成功即清零），日志首次 WARNING 其后降 INFO——上游按出口 IP 长期拦截时不至每 5 分钟重探十几秒再刷一条同样的告警。
- **凭证用虚拟凭证行**：无凭证/无额度接口，`probe_quota` 恒 `probe_failed=True` → health「未知」而非耗尽；删除后重启复活，永久停用请用「暂停」。401 归 `INVALID`；**403 FreeTierError 抖动在客户端内退避重试一次**，否则单虚拟凭证下 REQUEST 类错误会耗尽轮换预算、直接 503。

### 2.6 Kilo Gateway（`kilo`，虚拟凭证）

- 端点 `https://api.kilo.ai/api/gateway`，`/chat/completions` + `/models`。**标准 OpenAI 兼容**，无私有信封、无门禁伪装。**免费模型有权威标记**：`/models` 每条带 `isFree` 布尔，据此直接过滤、**不做探活**（探活会真发推理、白耗网关级约 200 req/h/IP 的极小配额）；免费模型标 **x0 倍率**。思考字段是 **`delta.reasoning`**（不是 Zen 的 `reasoning_content`）。凭证用虚拟凭证行（同 Zen）。
- 错误分类：401→`INVALID`、400/404/422→`INVALID`、403→`REQUEST`、**429 与 502/503/504→`MODEL`（模型级冷却）**——实测 429/5xx 点名具体模型、同批其他免费模型仍 200，归账号级会因单模型问题冷却整条渠道；本包不自建熔断。

### 2.7 CodeBuddy 国际版（`codebuddy_intl`）

- 端点 `https://www.codebuddy.ai`。**与国内站同协议**（同一套 `/v2/chat/completions`、`/v3/config`、`/v2/plugin/auth/*`、`/v2/billing/*` 路径，同一 Keycloak realm），但是**两套独立账号体系**（各自注册、各自计费），故作为独立渠道存在。**为什么不是「改环境变量切站点」**：`CODEBUDDY_API_ENDPOINT` 是进程级单值，切过去会让所有存量国内站凭证立刻打到国际站（额度错配 / 403）；两站可同时持有凭证，各走各的端点、节流器与登录流程。
- **白名单刻意互不含**：`CODEBUDDY_INTL_ALLOWED_ENDPOINTS` 默认只有国际站主机，**不含**国内站主机。两站 Token 由不同网关签发，跨站发送即凭证泄露，填错会在**启动时**报错。
- **能力差异**：聊天、模型列表、额度探测、token 刷新、**活跃上报**全部启用；**每日签到、成长中心未启用**——签到是渠道侧就没有这个活动（`checkin-activity-status` 回 `active: false`），成长中心**无法验证**（整个 `/v2/activity/*` 前缀挂在网关认证插件后，连不存在的子路径也返回 401 而非 404）且含不可逆动作，不盲跑。活跃上报端点 `/v2/report` 与成长中心不是同一路径，实测国际站可用。
- **模型列表按套餐探活**：上游 `/v3/config` 返回全量模型，免费号也会看到 `gpt-5.5` / `gpt-5.6-luna` 等调用时才回 `400 / 11158` 的模型。国际站列表用当前刷新凭证逐个发一次最小请求（只读状态行即断开），只保留可用模型；`11158`（套餐不允许）与 `11102`（无此模型）剔除，`429` / 5xx / 超时等不确定结果**保留模型**。结论按账号缓存 6 小时。
- **唯一协议行为差异：首条 system 强制注入**——国际站网关要求 `messages[0]` 是 system，否则整单 400 + `{"code":11128,"msg":"first message is not system prompt"}`；国内站无此要求。同名模型在两站是**一条**、同时挂两个渠道。

### 2.8 冲突与陷阱

| 问题 | 事实 | 对策 |
|---|---|---|
| 模型 ID 撞车 | 多渠道都有 `glm-5.2`、`DeepSeek-V4-Pro`、`kimi-k3` 等 | 扁平名 + 归一键合并 + 健康度路由 + `@渠道` 后缀 |
| 积分语义不同 | CB/Qoder/CodeArts 有周期会重置；TRAE 各权益包独立到期 | 健康分统一为百分比；展示层标注周期语义 |
| credit 可得性 | CB 有 per-request；TRAE 只有账户总额 | 统计表 credit 可空；TRAE 按官方单价、CodeArts 福利模型按每日池 1:1 推算并标 `credit_estimated` |
| 登录机制 | CB/Qoder/CodeArts 轮询或粘贴（后端出网）；TRAE 浏览器回调 | 三轨（poll / callback / paste），回调统一走主端口 |
| 上游 SSE 格式变更 | 各渠道 SSE 形状不同 | 各渠道解析成中立事件；样本存 fixture 做契约测试 |
| 图片能力标志不可信 | 七渠道出站均为 OpenAI `image_url` 内容块原样透传，上游全部接受（实测 2026-10-15）；而模型目录的 `supportsImages` 与实际行为不一致（CB `glm-5.0` 标 false 却能识图；`hunyuan-chat` 标 false 却接受并自行降级） | 入站统一归一为 `image_url` 透传，**不做模型能力门禁**，能否识图交上游裁决 |

## 3. 架构

### 3.1 分层

一个 FastAPI 进程内按**三个平面**组织，三面共享同一份 SQLite（WAL + `busy_timeout=5000`：账号 / 加密凭证 / 统计 / 审计）与 Provider 客户端：

- **请求面**（鉴权 = API Key）：`/v1/chat/completions` · `/v1/responses` · `/v1/messages` · `/v1/models` · `/v1/user/balance` → 协议层（OpenAI / Responses / Anthropic 规范化与适配、模型名解析 `name` ｜ `name@provider`）→ 执行引擎（调度器 · 冷却状态机 · 会话粘性 · 截断续写 · 统计采集）→ 中立事件流 → 七个 Provider。
- **管理面**（鉴权 = 会话 Cookie + RBAC）：React 管理台 → `/api/*`，含用户管理、审计、凭证运维、API Key、统计、任务与配置、上游状态、模型列表。
- **后台面**（无 HTTP 入口，`TaskRunner` 起 11 类循环）：额度探测 · token 预刷新 · 每日签到 · 成长中心 · 活跃上报 · 明细清理 · 数据库备份 · 渠道模型列表刷新 · 模型目录刷新（OpenRouter）· 运维告警 · 上游状态页聚合 → Provider 客户端 → 上游。

**为什么按平面切**：请求面全程无状态、可并发，且不知道「人」是谁（只认 Key 的归属用户）；管理面与请求面**共用同一个执行引擎与仓储**，不做独立服务——自托管小实例里进程隔离只换来部署复杂度。两个鉴权面互不替代：API Key 进不了管理台，会话 Cookie 也进不了 `/v1`。后台面与请求面**共用 Provider 客户端与节流器**：各渠道风控按最小间隔生效，不能因为「后台签到」与「前台对话」是两条代码路径就各发各的。三面共享一份 SQLite，故升级只需重启**一个**进程。

### 3.2 Provider 接口边界

Provider 承担上游协议私有部分：发请求、解析事件、分类错误，以及凭证生命周期与额度探测；调度、冷却、重试、统计全在共享引擎。接口分三类——凭证生命周期（`start_auth` / `poll_auth` / `complete_callback` / `import_credential` / `refresh`）、额度探测（`probe_quota`）、执行与分类（`stream_chat` / `classify` / `list_models` / `aclose`）。

### 3.3 调度器

统一实现，各 provider 共用。**选号前先在 `Executor._selectable` 做两道过滤**（不进 `Candidate.is_selectable`）：① 凭证限额护栏（触顶的号不参与，见 §限额护栏）；② 在途租约 `MAX_INFLIGHT_PER_CREDENTIAL`（全局热更，默认 0 = 不限）——同一账号并发达到上限后**整号不参与选号**，避免把单账号打进上游限流/封号。在途计数进程内、租约在请求结束时释放（含断开/异常），另有 600s TTL 兜底漏释放；跨渠道全局，与 CodeArts 的渠道内 `CODEARTS_MAX_CONCURRENCY` 正交。**在途计数兼作负载键**：`Executor._selectable` 把在途数注入 `Candidate.inflight`，调度器按「最少连接」排（见下）；上限为 0 时也照常登记，故不限并发时负载键仍可用。过滤后按下列排序链出号：


1. **免费优先档**：候选池存在 `credit_rate == 0` 的渠道时，免费渠道整体排在付费渠道之前。`credit_rate=None`（目录未就绪 / 缺该模型 / 上游未给倍率）**不算免费**，与 `0.0` 严格区分。**为什么排在到期额度之前**：省下的真金白银比「几百积分快过期先用」值钱。
2. **手动 pin 优先**；会话粘性命中且可选时直接复用，不参与排序。
3. **在途最少优先**（最少连接负载均衡）：在途数少的号排前，并发请求摊到空闲号。**为什么排在到期额度之前**：到期/健康度是静态指标，按它们排出的次序确定，所有并发会算出同一个最优号并全压上去（实测 codebuddy_intl 六号全打平、2 小时内一个号吃 180+ 次、其余闲置）；在途把它拆开，顺序请求（在途恒 0）不受影响。免费档仍压过在途。
4. **到期额度两级字典序**：先比主窗口（`QUOTA_EXPIRY_WINDOW_SECONDS`，默认 36h）内将过期的额度，打平再比次窗口（`QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS`，默认 7 天）。
5. **健康度三态**：`known` 降序 > `unknown` > `exhausted`；健康度打平时**账户剩余积分多者优先**，再同分按 `credential_id` 稳定。无可用返回 None。

**为什么冷却分「账号级」与「模型级」两层**：上游拒绝语义不都是账号级问题——`429 + 6004` 是「这个模型在当前账号上用超了」，`400/404 + 11102` 是「当前账号没有这个模型」。一律记账号级会让一次模型级限流把整个账号踢出池。故账号级写 `credentials.cooling_until`，模型级另建 `credential_model_cooldowns`；账号级冷却出现时清空该凭证的模型级条目，防「切模型」绕过。业务码识别只认 `"code": N` 键值形态，不搜裸数字。冷却时长：`PLAN` 12h、`SOFT` 60s（不累计）、`OTHER` 连续 3 次→10m、`MODEL` 600s 起翻倍封顶 2h、`BLOCKED`（无此模型负缓存）6h 起翻倍封顶 24h、`CONCURRENCY` 固定 60s（不翻倍）、`CREDIT` 冷却到次日 04:00。**传输层失败归 `SOFT`**：`httpx.TransportError`（连接超时 / 写请求体超时 / 读超时）与 `UpstreamTransportError` 统一在 `executor._classify` 兜住——这类异常从 `client.stream()` 的 `__aenter__`、`aiter_bytes()`、写请求体等多个位置抛出，逐点包装必漏，集中在一处才不会让「链路瞬态」逃成 `internal_error` 帧（逃逸后既不换号也不冷却、不记账）。刻意不泛化到 `OSError` / `ConnectionError`：磁盘满、句柄耗尽也是它们的子类，误判会去冷却一个完全健康的凭证。**上游给出的重置时刻可覆盖上述常量**：响应头 `Retry-After`（整秒 / HTTP-date）或 `Try again in 17h 59m` 这类文本经 `parse_retry_after` 解析后，按该错误分类的作用域写入冷却，并**双端钳制**在 `[now + 该 ErrKind 现有常量, now + 24h]`——上游给 0 或过去时刻回落常量，否则一个 `Retry-After: 0` 就能把所有号瞬间打爆。无该头时行为与常量完全一致。

**为什么是「窗口内积分总量」而不是「是否即将过期」**：实测 CodeBuddy 额度不是整块周期，而是几十个各自独立到期的小包（每日 100 积分 × N），只存一个日期没有区分度。四个取舍：① 统计窗口内的到期积分总量，账号之间才有可比高低；② **落库到期阶梯**（`[(到期 epoch, 包剩余积分)]`）而非预计算数字，窗口是运行时参数，改阈值立刻生效；③ 过滤条件必须是 `end > now` 且排除已用完的包（上游会把已过期套餐一起返回）；④ **两级窗口**，只比 36h 会大量账号同为 0、排序退化成健康度。主窗口 `≤0` 等于关闭整套到期排序。TRAE 的权益包各自独立到期，同构填 `expiry_ladder`；CodeArts 每日池登记为「次日本地 0 点到期」。**展示与调度指标分开存**：`quota_packages`（含包名/用量，仅展示）与 `quota_expiry_ladder`（只有 `[到期, 剩余]`，选号指标）。

**健康度归一化**：`known(0-100) | unknown | exhausted`。CB 取 `CycleCapacityRemainPrecise`/`CycleCapacitySizePrecise`，TRAE 取 `credits_limit - credits_amount` / `credits_limit`。**不用响应顶层的 `TotalDosage`**：它 = Σ(size − CapacityUsed)，而 `CapacityUsed` 是上周期口径，本周期消耗记在 `CycleCapacityUsed`/`CycleCapacityRemain`，用它会出现 `remaining == total`、健康度恒 100。**为什么必须三态**：探测失败被算成 0 分会让这类凭证在有健康号时永远轮不到。

**会话粘性**：识别按可靠性分三级——① 请求体显式会话标识 `conversation_id` / `conversationId` / `prompt_cache_key`；② **请求头会话标识** `x-session-id` / `x-opencode-session` / `x-session-affinity` / `conversation-id`（按序取第一个合法值，须匹配 `[A-Za-z0-9._:-]{1,128}`，经 contextvar 传入不污染中间层签名，键带 `header:` 前缀避免与 body 同值撞键）；③ 回落「消息增量前缀指纹」。**头为什么夹在中间**：body 显式键最精确；头是客户端主动声明的会话身份，比指纹猜测可靠；而指纹只是启发式兜底。头值与其他键一样掺 `username` 再哈希——头是客户端可写的，不掺等于允许一个用户靠猜头值占用别人的账号。TTL（`CONVERSATION_STICKY_SECONDS`，默认 1h）内固定复用，避免中途换号触发上游风控并丢提示词缓存。请求体带 `user_id` 时**不派生**第 3 级前缀兜底键（同用户并行对话前缀可能相同，会误钉；头照常生效）。手动 pin 优先于粘性；凭证报错仍走正常轮换；条目纯内存。`prompt_cache_key`（OpenAI 官方顶层参数）与 `metadata.user_id`（Anthropic 官方字段）已核实；`conversation_id` / `conversationId` / 顶层 `user_id` 非两家标准键，属客户端惯用约定，作为兼容探测接受。

### 3.4 模型名三字段

七条渠道每个模型统一为三个字段，同一条链派生：**原代号**（`raw_id`，渠道请求时真正发的 key，**永不改动**，转发时经别名表换回）、**归一键**（剥掉免费档标记 `-free` / 路径段 `free`、新版标记 `-new` 与命名空间前缀后 slug 化，模型列表按它判断同一模型并合并）、**展示名**（`name`，清洗后的可读文本，上游可读名优先、无则由原代号派生、品牌缩写按官方写法纠正）。

**合并键 = 归一键**；`/v1/models` 对外 `id` 一律是归一键，原代号进 `by_provider.{渠道}.raw_id`；**原代号、展示名、归一键三种写法都能直接请求**。同模型异名由显式规范表 `MODEL_SYNONYMS` 收敛（键与展示名一起），**只登记人工核实的等价、不做模糊匹配**。哨兵名（`auto` / `default`）语义只在本渠道内成立，不跨渠道合并；同渠道内归一键重复或对外 id 撞车时退回原代号 / 加后缀。黑名单按**归一后的对外写法**匹配（原代号、其归一键、展示名、展示名归一键四种任一命中即滤）。

### 3.5 登录三轨

`AuthFlow` 三值：`poll`（后端轮询上游：CB / Qoder）、`callback`（浏览器 302 回本服务 `/authorize`：TRAE）、`paste`（用户把回调链接粘回：CodeArts，服务端监听不到本机回环端口）。

- **回调统一走主端口** `/authorize`；远程部署只需暴露一个端口。回调地址要写进登录 URL，故必须可配：`PUBLIC_BASE_URL`。
- 前端在「凭证管理」页内实现：**poll** 拿到 `authUrl` 开新窗、轮询 `/api/auth/upstream/poll`；**callback** 开授权窗、浏览器 302 回 `/authorize` 直接落库，前端轮询凭证列表出现新条目即视为完成（上游不回传 state）。`/authorize` 无鉴权（浏览器 302 不带 key），故按**发起人会话** + **`loginTraceID`**（TRAE 原样回显登录 URL 里的 `(machine_id + device_id)[-16:]`，攻击者拿不到）双重绑定，且待完成登录有 TTL（10 分钟）。

### 3.6 中立事件层

上游 SSE 解析到「中立事件」（`EventKind`：`content` / `reasoning` / `tool_calls` / `usage` / `finish` / `error`）独立成层。出口协议适配器（OpenAI / Responses / Anthropic）只消费中立事件，不动上游逻辑。

**入口语义收口**（不静默降级，也不把畸形请求打到上游把凭证打凉）：chat 入站的 `response_format` 只放行 `text` / `json_object` / `json_schema`（后者要求非空 `name` 与 `schema`，未知字段丢弃），Responses 的 `text.format` 复用**同一个**校验函数（`compat/openai/request.py: normalize_response_format`，`prefix` 只改报错路径）。Anthropic 的 `cache_control` **默认原样透传**（落点：`system` 块、user/assistant 块、`tool_result` 挂 tool 消息顶层、`tool_calls` 元素顶层、`tools` 元素顶层），只校验结构（对象 + 非空字符串 `type`，`ttl` 若给必须字符串），取值交给上游裁决；无标记时映射结果逐字节不变，会话粘性指纹与上游前缀缓存不受影响。

### 3.7 账号与权限面

管理面身份来自 SQLite `users` 表。三角色：`admin`（用户管理、运行时配置、凭证写操作、全量统计、审计、运维告警、`require_admin`）、`operator`（凭证写操作：导入 / 删除 / 启停 / pin / 切换账号 / 签到 / 成长，全量统计，上游状态、模型列表，`require_operator`）、`viewer`（只读：仪表盘、凭证列表、Playground、统计；审计为 admin-only，登录即可）。

- **鉴权链**：签名 Cookie（HMAC，payload 带用户名与 `session_epoch`）→ 用户仍存在且启用 → epoch 与 DB 一致 → 角色现读 DB。四者缺一即 401；**角色不进 Cookie**，降级立即生效。
- **吊销不建会话表**：`users.session_epoch` 在改密 / 改角色 / 启停 / 硬删时 `+1`，旧 Cookie 立即失效。
- **建号与重置走一次性激活令牌**：库里只存 SHA-256 摘要 + 过期时间，明文仅在响应里回显一次，用户在 `/activate` 自设密码。被重置的用户改密前只放行「看会话 / 改密 / 登出」。
- **删除语义与审计**：**禁用是主路径**（可逆、保住用量归属），硬删只在 CLI（`scripts/create_user.py --delete --force`），管理台不暴露 `DELETE`；登录成败、账号变动、凭证写操作全部入 `audit_events`，**绝不记密码或令牌明文**。
- **防锁死三层**：Web 端「最后一个活跃 admin」+ 自我降级 / 自禁用守卫；CLI 拒绝硬删最后一个活跃 admin；bootstrap 发现无活跃 admin 直接启动失败。**引导**：`users.txt` 启动时一次性导入（幂等、不覆盖已存在），`ADMIN_USERNAMES` 只标引导期提权。

## 4. 数据模型

DDL 以 [src/db/schema.sql](src/db/schema.sql) 为准，共 **12 张表**，`SCHEMA_VERSION = 27`。变更纪律：schema.sql **只加不改**；新增列走 `_MIGRATION_COLUMNS` 幂等补列，删表须在 `_MIGRATION_DROPS` 补一条。

| 表 | 用途 |
|---|---|
| `api_keys` | API Key（SHA-256 摘要 + 预览 + 渠道绑定 `provider_binding` + 来源 IP 白名单 `allowed_ips` + 模型白名单 `allowed_models` + 到期 `expires_at` + 配额 `quota`） |
| `credentials` | 加密凭证（`data_enc` 走 Fernet）+ 调度状态（`enabled`/`disabled`/`pinned`/`health`/`cooling_until`/`err_count`）+ 额度与到期阶梯 + token 到期列 + 成长中心最近结果 + 运维标签 `tags`（仅展示筛选，不参与调度） |
| `credential_limits` | 凭证级限额护栏（保留积分 / 每日 token / 每日 credit / 按模型 token）；**缺行 = 不受限** |
| `usage_events` | 请求明细（token / credit / 缓存命中 / 成本 / 耗时 / 首字延迟 / 请求 ID / 发起请求的 `api_key_id`），保留 90 天 |
| `usage_hourly` | 小时汇总（永久保留），渠道 / 模型 / 用户维度统计读此表 |
| `growth_events` | 成长中心每轮一行汇报（仅 CodeBuddy）；活跃上报复用同表 |
| `credit_events` | 两次额度探测之间的**净变化**（含 `window_start`、归因已知度 `source` 与账本 `ledger`＝`pool`/`credits`），不是动作归因 |
| `credential_model_cooldowns` | (凭证, 模型) 级冷却与「无此模型」负缓存，含 `hits` 供指数退避 |
| `runtime_settings` | 运行时配置覆盖（纯 key/value，只存管理台改过的 key），白名单校验在 `runtime_settings.HOT_SETTINGS` |
| `users` | 账号（PBKDF2 密码哈希 + `role` + `enabled` + `must_change_password` + `session_epoch` + 一次性激活令牌摘要） |
| `audit_events` | 审计流水（登录 / 账号变动 / 凭证写操作） |
| `alert_events` | 运维告警事件（四类规则命中落一行；`resolved_at` 非空 = 迟滞确认恢复） |

- 调度状态与到期阶梯**落库**，重启不丢冷却状态与到期阶梯。签到去重、模型列表缓存、后台任务运行态均**进程内实现、不建表**（运行态重启归零，不编造重启前记录）。
- 模型目录落盘到 `DATA_DIR/model_catalog.json`（未过滤原始表），启动同步读回；OpenRouter 目录落盘 `data/openrouter_catalog.json`。
- **脱敏纪律**：不存提示词、回答、请求头、Token、工具参数、原始错误体、会话 ID。唯一例外是诊断开关 `DUMP_REQUEST_BODIES=true`（默认关）把 `/v1` 原始请求体落盘到 `data/dumps/`（有界保留 200 份），**含完整对话内容**，不得长期开启、不得随库交付。

## 5. API 契约

**外部（API Key 鉴权）**：`POST /v1/chat/completions`（流式 + 非流式，`n` 只接受 1——出口结构只能产生单个 choice，其余值 400 `invalid_request`）；`POST /v1/responses`（Responses 子集，Codex CLI）；`POST /v1/messages` 与 `POST /v1/messages/count_tokens`（Anthropic Messages 子集，`count_tokens` 本地估算不转发上游）——三者共用同一调度链路；`GET /v1/models`（对外 `id` 为归一键 + `name` + `providers` + `by_provider.{渠道}.{raw_id, credit_rate, benefit}` + 可选 `benchmarks` / `cost`（`input`/`output`/`cache_read`，USD/1M，缺价不带该字段）/ `reasoning_efforts`（该渠道生效档位去重集合，未配置不带）；`benefit` 标福利模型，单渠道在顶层；自定义别名条目另带 `custom_alias: true`）；`GET /v1/models/{id}`（单模型查询，`:path` 容纳带命名空间的渠道原代号；对外 id / 展示名 / 任一渠道原 id / 自定义别名四种写法、大小写不敏感；不存在**或不在该 Key 模型白名单内**一律 404 + `model_not_found`，不泄露池中模型）；`GET /v1/usage?days=1|7|30`（自助用量：只查**该 Key 自己**的本地明细，不触发上游，返回 `requests/ok/failed/token/cost_usd` + `limits`/`remaining`/`rate`/`resets_at`，不含凭证 id、昵称或池级信息）；`GET /v1/user/balance`（DeepSeek 兼容余额，读探测缓存聚合，不实时打上游）；`GET /health`（纯存活）、`GET /healthz`（存活 + 凭证池五类计数，无鉴权）、`GET /metrics`（Prometheus text 格式指标，默认开、无鉴权，`METRICS_ENABLED=false` 关闭）。

**管理台（会话 Cookie + RBAC）**：凭证管理、API Key 管理（含 Key 级配额 `quota`：日请求 / 日 token / 日成本 / RPM / TPM / 并发，`0`=不限；超限 429 + `Retry-After`，判定异常一律**放行**并记 warning）、用量统计（`/api/stats/*`）、Playground、模型列表（operator，含 `GET /api/models/availability` 疑似下线只读视图）、用户管理（admin-only）、审计日志（admin-only）、运维告警（admin-only）、上游状态（operator）、任务与配置（admin-only）、**服务端日志（admin-only，只读）**。

- 账号：`GET|POST /api/users`、`PATCH /api/users/{username}`、`POST /api/users/{username}/{disable|enable|reset-password}`（**不提供 DELETE**）；自助改密 `POST /api/auth/password`；无鉴权的一次性激活流 `GET|POST /api/auth/activate`；审计查询 `GET /api/audit`。
- 凭证运维：`POST /api/credentials/{id}/checkin|toggle|revive|probe|accounts/select`、`PUT /api/credentials/{id}/limits`（限额护栏整体替换，全关时删行）、`GET|POST /api/credentials/{id}/growth`、`GET /api/credentials/{id}/credit-events|accounts`。
- 凭证批量运维：`POST /api/credentials/batch`（`{action: probe|toggle|checkin|revive, ids: [...]}`，可选 `enabled` 用于 `toggle`）。逐条隔离执行——单条失败不影响其余条，响应含 `{total, succeeded, failed, results[]}`；`toggle`/`revive` 落一条汇总审计，`probe`/`checkin` 与单条端点同为只读语义、不写审计。
- 运行时配置与任务运行态：`GET|PUT /api/settings` + `GET /api/tasks` + `POST /api/tasks/{key}/run`（手动跑一轮）。列表端点接受统一 `sort`（白名单键）+ `order`（`asc`/`desc`）；未知键 / 非法值回落到默认，不报错。回调（无鉴权）：`GET /authorize`。
- 运维告警：`GET /api/alerts`（倒序回看，分页）+ `POST /api/alerts/{id}/resolve`（人工标记一条已恢复，admin + CSRF + 审计；用于规则已下线 / 误报等状态机无法自动判定的情况）。后台正常路径会在规则不再命中时自动写 `resolved_at`，且 `AlertTask` 启动时从库里未恢复的行重建「告警中」集合，重启后仍能自愈。
- 上游状态聚合：`GET /api/upstream-status`（operator）返回后台任务抓取的内存快照（`summary` + `sources`）与任务的当前 `enabled` / `interval_seconds` / `server_time`；**不落盘**，重启后为「尚未抓取」。四种解析器（Statuspage v2 / OpenCode / Better Stack / Atom）归一化到六档，加源只需在 `SOURCE_SPECS` 加一行。
- 用量统计：`GET /api/stats/{overview,by-provider,by-model,by-user,by-credential,timeline,model-timeline}`（分组表的延迟列可选 P50/P90/P95/P99 四档，默认 P95；`p95_*` 字段与排序键保持兼容）、`GET /api/stats/errors`（**失败归因**：窗口内 `ok=0` 明细按 `(provider, model, error_type)` 分组计数 + 最近失败时刻，`limit` ≤ 5000）、`GET /api/stats/events`（rowid 游标 / LIMIT-OFFSET 双模式，`provider`/`model`/`credential_id` 钻取过滤，`ok=0` 只看失败，`?format=csv` 导出）、`GET /api/stats/events/stream`（**SSE 实时明细**：`StatsCollector.record` 事务提交后由进程内 `LiveEventHub` 广播刚写入的行，不轮询数据库；每订阅者有界缓冲 200 条、并发上限 8 超出回 429 + `Retry-After`；15s 注释帧保活，`Cache-Control: no-store` + `X-Accel-Buffering: no`；作用域/过滤在订阅者侧逐条判定，与 `/api/stats/events` 同口径）。
- 服务端日志：`GET /api/logs?limit=`（默认 200，上限 = 缓冲容量）+ `GET /api/logs/stream`（SSE，先回放缓冲再推实时，按 `seq` 去重）。**只读**、admin-only；`LOG_BUFFER_LINES=0` 时两端点均 404。只收本进程 `logging` 记录，不读文件；每行发出前过脱敏白名单。
- 侧栏数据提醒：`GET /api/nav-badges` 一次返回侧栏各项的待办计数（凭证需重登/耗尽、未恢复告警、上次失败任务、异常上游源、待激活账号、本人将到期 Key、模型目录陈旧）。**按角色裁剪 payload**——缺失的键 = 无权看（admin-only 的告警/任务/用户不下发给 operator/viewer，operator 的上游/目录不下发给 viewer）；这是聚合端点，**不做整体 403**（viewer 也要看自己的 API Key）。各计数与对应页面数据**不是同一口径**：只数「待办」（如告警只数 `resolved_at IS NULL`、任务用「上次失败」而非「连续失败达阈值」）。

实现以代码为准，使用说明见 [README.md](README.md)。

## 6. 安全边界

- **上游端点白名单**：真实 Token 只发往显式配置的地址。CB 启动时强制校验；国际版白名单刻意不含国内站主机；TRAE 的 `apiHost` 导入时按官方地址白名单校验；Zen / Kilo / Qoder / CodeArts 各有独立白名单。
- **凭证加密**：`data_enc` 走 Fernet，密钥走 `APP_SECRET`（≥16 字符，拒绝低熵值）；**丢失 = 已存凭证全部不可解，只能重录**，不做密钥轮换。解密失败返回可行动错误码，不暴露裸 500。
- TLS 校验默认开启；Host / Origin 白名单；CSP `frame-ancestors 'none'`、`object-src 'none'`、`base-uri 'self'`、`form-action 'self'`。**请求体上限**：登录 8KB、其余 16MB（ASGI 层按实际字节计数，`chunked` 不能绕过）。登录三级限流（全局 / IP / 用户名）+ PBKDF2 并发上限；失败键有数量上限 + 过期淘汰（防内存 DoS）。
- **API Key 仅存摘要**，明文只在创建时返回一次；可按 Key 限定渠道绑定、来源 IP 白名单（默认**不信** `X-Forwarded-For`，仅 `TRUST_PROXY=true` 时取 XFF 最后一个条目）、模型白名单（fnmatch，无法判定模型名时 fail closed）与到期时间。
- **会话**：Cookie `SameSite=Lax` + 写操作 CSRF 校验（含 logout）；`Secure` **按请求实际协议动态判定**（`X-Forwarded-Proto` 首个值优先，无该头则仅当请求 Host 命中 https 的 `PUBLIC_BASE_URL` 主机才判 Secure）——同一实例 LAN(http) 与隧道(https) 双入口写死任一侧都会打断另一侧；**无 Cookie 的写端点（登录 / `/authorize`）也做跨站防护**：登录按 Origin/Referer 同源校验（防 login CSRF），`/authorize` 按上游原样回显的 `loginTraceID` 绑定到**发起登录那一次**（TRAE 回调本就跨源，Origin/Referer 既拦不住真攻击也可能误杀真回调）；会话与 API Key 除签名 / 摘要外校验用户仍存在、启用且 epoch 一致。
- **错误响应不回流上游正文**：`/v1` 与外层的错误文案只带受控标识（HTTP 状态码 / 上游业务码 / 异常类名），上游错误体摘要只进服务端日志；未匹配的 `/api`、`/v1` 路径返回 JSON 404（不落到 SPA 的 200 + HTML）。**客户端读体中途断开不是服务端错误**：按网关惯例回 `499 client_disconnected`，不落 500 + ASGI Traceback（噪音 + 语义错）。**日志脱敏**：不打印 Token、完整请求体；审计写 INFO（含操作人），**绝不记密码/令牌明文**。
- **请求 ID 是客户端可写输入**：入站 `X-Request-ID` 按白名单字符集与长度（`[A-Za-z0-9._:-]{1,128}`）校验后才采信与回显，不合法一律丢弃改用自生成——否则日志与统计列会被人塞进换行/超长串。响应头回显最终 ID（含自生成值），不伪装成上游追踪。
- **服务端日志页同样只出不进**：`src/webapp/log_buffer.py` 的 `redact_or_drop` 在行离开进程前整行过滤——独立敏感词（token/secret/authorization/cookie/password/api_key 及复数和连字符形态）、键值形态（`access_token=`、`X-Api-Key:`、`set-cookie:`、JSON 键）、`sk-…` 形状、`Bearer <值>` 命中即丢弃；空行也丢。代价是 `token 已刷新` 这类正常行会被一起吞掉，取舍是**宁可漏一条日志，也不把凭证推进浏览器**。页面无任何写入/执行入口，订阅者只能读。
- **请求级结构化日志**（`src/webapp/access_log.py`，`REQUEST_LOG_ENABLED` 可热更）：每个业务请求（`/api/*`、`/v1/*`、`/authorize`）一行 logfmt，含 `request_id` / 路由**模板** / 状态码 / 耗时。**不写 query string**（可能被客户端塞 token）、**不写请求体 / 请求头 / 用户名**（身份靠 `request_id` 关联 `usage_events`）；未匹配路由时回落为清洗并截断的原始路径，防换行注入。静态资源与探针不记（uvicorn 已覆盖）。
- **租户隔离**：`/api/stats/events` 与 `/api/stats/by-credential` 的**凭证昵称**取自全局共享池（常含邮箱/手机），仅 admin/operator 可见；viewer 即便只看自己的记录也不下发该字段。`/api/stats/events?format=csv`（整份明细导出）同样仅 admin/operator；`/api/stats/events/stream` 的广播 payload 不含昵称，仅 admin/operator 的连接由服务端逐条补 `credential_name`。
- **热更配置写入即校验**：float 拒 NaN/±inf；字符串类有长度上限；告警 webhook 只接受 `http(s)` 并做 **SSRF 校验**（写入期拒字面量内网 / 回环 / 链路本地 / 保留 / 多播 / 未指定地址，**投递前再按 DNS 解析结果复核一次**、解析失败即阻断），外发载荷对 `nickname` 打码，多地址并发投递并加总预算。告警出站 client `trust_env=False`，不吃宿主代理环境变量。
- **上游流解析有界**：SSE 单行与单帧缓冲设上限，非 2xx 错误体有界读取；OAuth state store 的 owner/consume 有 TTL 判定。**入站解析把关**：三个出站协议在入口拒绝越界数值字段（`max_tokens` ≤0 或超上限、`temperature`/`top_p` 为 NaN/Inf）与 JSON 里的 `NaN`/`Infinity`。
- **OAuth 登录弹窗**只允许 `http(s)` scheme，弹窗 `opener` 置空。**库版本守卫**：`user_version` 高于本程序时 `apply_schema` 拒绝执行。`scripts/hash_password` / `create_user` 支持 `--password-stdin`；拒绝含 `:` / 换行的用户名。
- **不做**：mTLS；面向管理台与端口的 IP 限制（交给反向代理）。不做全量请求审计（统计表已是脱敏的请求级记录）。

## 7. 风险清单

| 风险 | 等级 | 对策 |
|---|---|---|
| CB 协议复杂度高（私有信封 + 多业务码 + 成长中心） | 高 | 先 bearer-only 跑通再补 OAuth；成长中心不可逆动作用配置开关总控 |
| 上游 SSE / 私有协议格式变更 | 中 | 各 provider 的 SSE 样本存 fixture 做契约测试；解析失败不静默 |
| 模型 ID 撞车导致路由错误 | 中 | 扁平名 + 归一键合并 + `@渠道` 后门；统计行强制带 provider 字段 |
| 上游 credit 不保证可得 | 低 | credit 仅辅助展示；健康度只依赖额度探测接口 |
| 积分语义混淆（周期 vs 余额 vs 合成折算） | 低 | 健康分仅用于调度；展示层标注周期与折算语义 |
| 第三方能力分被误读为本服务实测 | 中 | 三处 UI 均标注「Artificial Analysis 指数，经 OpenRouter 公开接口；非本服务实测」；不做排序 |
| 能力分取了旧快照的评测变体 | 中 | 上游把浮动别名钉在旧快照上并换变体（实测 24.7 vs 39.5）。按 `canonical_slug` 日期归族，浮动别名取同族最新快照；显式快照 id 不改写；`source_model` 指向真正出分的上游 id |
| 上游模型写法差异导致错配 | 中 | 只登记显式等价规则 + 命中不唯一即拒配 |
| CodeArts 一次性 refresh_token 烧废即账号失效 | 中 | 刷新由 `RefreshTask` 独占轮转、刷后回写；终态异常标记「需重新登录」并停重试 |
| 一次性激活令牌 / API Key 明文泄露 | 中 | 明文仅响应回显一次，库里只存 SHA-256 摘要 |
| 部署契约：只改前端时后端可能仍是旧进程 | 中 | 改 `src/` 必须重启进程；升级后按 README「升级后必须重启后端」核对 schema 版本与路由 |
| License 溯源不全 | 中 | NOTICE 列明 7 个参考项目（含各自上游来源）的署名与协议 |

## 8. NOTICE 三方溯源

署名清单以仓库根目录的 [`NOTICE`](NOTICE) 为唯一真源。当前列出的参考项目及其借鉴方向：

- [IceeAn/codebuddy2api](https://github.com/IceeAn/codebuddy2api)：CodeBuddy 上游协议、凭证管理、脱敏统计
- [connectedGraph/trae2api-web](https://github.com/connectedGraph/trae2api-web)：TRAE SOLO 上游协议、账号池冷却状态机
- [88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin)：成长中心与签到接口的逆向结论与运行经验
- [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)：活跃上报 `/v2/report` 的协议形状与实测结论
- [ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)：后台任务可视化页面的设计参考
- [shuishuipingan/qoder2api-hub](https://github.com/shuishuipingan/qoder2api-hub)：Qoder COSY 签名、自定义 Base64、信封 SSE、设备码登录、签到与额度
- [HITZY2002/codearts2api](https://github.com/HITZY2002/codearts2api)：CodeArts SDK-HMAC-SHA256、DPoP(ES256) 刷新、累计全文 SSE、福利模型

均为 MIT License，完整版权行与「其上游」子项见 `NOTICE`。本项目代码为独立实现，不复制上述项目的源代码；上游服务的协议细节来自对客户端行为的观察，不属于上述项目的版权范围。

## 9. 其它文档

技术栈、模块规格、Provider 协议、请求时序与测试策略见 [`TECHNICAL.md`](TECHNICAL.md)；使用与部署、配置清单见 [`README.md`](README.md)；架构图 / 请求时序图 / 凭证调度状态机见 [`diagrams/`](diagrams/)。
