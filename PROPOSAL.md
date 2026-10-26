# Coding2API 设计文档

**CodeBuddy**（国内 `codebuddy` + 国际 `codebuddy_intl`）、**TRAE SOLO**、**OpenCode Zen**、**Kilo**、**Qoder**、**CodeArts**、**OfficeAce**、**LobsterAI** 九个上游 → OpenAI 兼容 API：公共凭证池、统一模型名与调度、按人用量统计。

> 只记仍成立的目标、上游事实、架构与约束。实现见 [TECHNICAL.md](TECHNICAL.md)，使用与部署见 [README.md](README.md)。

## 1. 目标与非目标

**目标**：单一 OpenAI 兼容出口挂九个上游；`模型@渠道` 精确指定；凭证 admin 集中维护、全员共享；调度器自动挑健康号，上游死亡自动冷却；按人统计用量与估算成本。

**非目标**：不做配额/限流（上游订阅制，成本不随 token 线性增长，靠统计页可见性约束滥用）；不做通用 provider 网关（只硬编码这九个）；不做货币换算（上游积分互不通用，分开记）；不做数据迁移、自更新、mTLS。

## 2. 上游可行性核实

### 2.1 CodeBuddy 国内站（`codebuddy`）

- 端点 `https://copilot.tencent.com`；聊天 `POST /v2/chat/completions`，**只支持流式**（非流式本地聚合）；头 `Authorization` + `X-User-Id` + `X-Domain` + `X-Enterprise-Id` + `X-Department-Info`（部门名须 UTF-8 百分号编码）。设备码认证：`POST /v2/plugin/auth/state?platform=CLI` 拿 `authUrl`/`state`，轮询 `POST /v2/plugin/auth/token?state=`；账号切换 `/v2/plugin/login/account`、`/v2/plugin/accounts`。
- 额度：个人版 `POST /v2/billing/meter/get-user-resource`（`CycleCapacity*Precise`）；企业版 `POST /v2/billing/meter/get-enterprise-user-usage`（`credit` 已用、`limitNum` 总额）。签到 `POST /billing/meter/daily-checkin`，状态 `POST /billing/meter/checkin-activity-status`。
- **成长中心**（`/v2/activity/growth`）：只读 `buddy/travel/status|config`、`tasks`、`streak`、`redeem/summary`、`lottery/chances`、`buddy/quota`、`energy`；写入 `buddy/travel/claim|depart`、`tasks/accept`、`/tasks/{code}/claim`、`makeup-cards/use`、`redeem`、`lottery/draw`、`buddy/open`。契约：`accept_status` 五态、`/tasks/accept` 收复数数组、`/redeem` 的 `tier` 是档位标识（`"7d"`/`"14d"`/`"28d"`）、实发字段 `*_granted`；进程内 OAuth bearer 可直连。
- **凭证身份可能为空**：OAuth 路径上游未回填 `account_uid`/`user_id` → 同账号隔离回落 `credential_id`。**活跃上报**（`POST /v2/report`，默认关）：body 事件数组（`eventCode=chat_request_send`），`userId` 必填——缺失 → HTTP 200 `code:0` 但静默丢弃；身份为空回落 JWT `sub`。
- **reasoning 客户端给什么透传什么**；`reasoning_effort` 缺失补 `medium`（不发则上游把思考写进 `delta.content`、`reasoning_content` 恒空、`reasoning_tokens` 恒 0）。**可选机制（默认全关）**：`REASONING_EFFORT_MAP`、`ANTHROPIC_THINKING_EFFORTS`、`FORCE_DISABLE_THINKING`；全关行为逐字节不变。**输出上限键名不对称**：CB 忽略 `max_completion_tokens`，只认 `max_tokens`；TRAE 两键都不生效——不做键映射。
- 错误码：`6004` 模型用量超限、`14003` 模型饱和（均模型级）、`11102` 无此模型（负缓存）、`14018` 积分耗尽、`1005` 权益耗尽、`11101/11115/11128/11135` 请求级（不罚号）。`6004`/`14003` 落账号级 → 整条凭证其他模型一起冷却 60s。**`6004` 不给 `Retry-After` 头**，恢复时刻只在 `msg` 文案（`usage will reset at 2026-10-25 22:34:25 UTC+8`）→ 须解析正文（§6 冷却）。

### 2.2 TRAE SOLO（`trae`）

- Host：Agent `https://trae-api-cn.mchost.guru`、UG `https://api.trae.cn`、OAuth `https://api.trae.com.cn`；聊天 `POST /api/agent/v3/llm_utils_chat`，模型 `POST /api/ide/v1/get_detail_param`，均仅流式。认证：浏览器登录 → 302 回调 → `ExchangeToken` → `GetUserInfo`；刷新 `POST /cloudide/api/v3/trae/oauth/ExchangeToken`（refreshToken 轮换）；凭证 `apiHost` 是用户可控输入 → 导入时按官方白名单校验。签到 `/trae/api/v2/ug/checkin_credits/{status,claim}`；额度 `/trae/api/v2/pay/ide_user_ent_usage`。
- **签到成功必须确认到账**：`claim` 对当天已签也返回 `code:0` → 判定需 `checked_in` 为真且回查 `credits` 有增加。**9074 按设备标识处理**：每次 claim 生成新 16 位数字串，一轮内最多换号重试 2 次。
- SSE 序列 `metadata` → `timing_cost` → `output`×N → `extra_info` → `token_usage` → `done`；`token_usage` 含 `cache_read_input_tokens` / `cache_creation_input_tokens` → 映射 `cached_tokens`，**无 per-request credit**；流内 `1005` 权益不足、`4008` 配额耗尽（同为 CREDIT，冷却到次日签到）、`4001` 参数/模型不可用。接受 `reasoning_effort`，不认 `thinking` 对象；`developer` 角色归一 `system`。

### 2.3 Qoder（`qoder`）

- 域：国内 `openapi.qoder.com.cn` / 网关 `gateway.qoder.com.cn`；国际 `openapi.qoder.sh` / `api1.qoder.sh`。聊天 `POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation`，**只支持流式**（信封式 SSE）。签名自定义 Base64 变体（三段轮转 + 自定义字母表 + `=`→`$`），`Authorization: Bearer COSY.<payload_b64>.<md5sig>`，整套 `cosy-*` 头（含按 `uid` 稳定派生的 `cosy-machineid`/`cosy-machinetoken`）。登录设备码 PKCE（S256）：`{website}/device/selectAccounts` → 轮询 `GET {openapi}/api/v1/deviceToken/poll`（404/202 = 待授权）→ `POST /api/v1/deviceToken/refresh` → `GET /api/v1/userinfo`。
- 模型 `GET {gateway}/algo/api/v2/model/list?Encode=1`，**必须带整套 COSY 签名头**（裸 GET 403，带头 POST/PUT 400 → 方法固定 GET）；额度 `GET {openapi}/api/v2/quota/usage`（`userQuota` + `addOnQuota`）；套餐 `GET /api/v2/user/plan`。
- **入站消息归一**：`build_chat_body` 丢弃孤立 `tool` 消息（无紧邻声明该 `tool_call_id` 的 `assistant.tool_calls`）——上游对这类历史一律 400。只丢弃、不重排、不改 content；丢弃条数记 warning，不落正文。并行 tool_calls 与 `/v1/responses` 拆出的连续 assistant 声明按并集识别。流内错误正文保留到 2000 字符。
- 签到（活动制）：`GET /sash/api/v1/me/campaigns` → 筛 `actionType=="CLAIM_BENEFIT"` 且 `claimStatus=="CLAIMABLE"` → `POST .../{campaignId}/claim`；**两个请求都必须带 `Cosy-ClientType: 10`**（缺则静默返回空列表）；`replayed:true` 与 `BLOCKED`+`SAME_PERSON_ALREADY_CLAIMED` 归一为已签。旧 `daily-check-in` 仅作回退。**活动每日 10:00（UTC+8）刷新**，封账到窗口结束而非自然日。**国际版该端点 404 → 本区域无此接口**。
- **上游节点故障包成 400**（`[FAIL]node:…`）归模型级瞬时故障，不误判为模型不存在。

### 2.4 CodeArts（`codearts`）

- 域：snap 引擎 `snap-access.cn-north-4.myhuaweicloud.com`、STS `sts.cn-north-4.myhuaweicloud.com`、门户 `codearts.huaweicloud.com`。聊天 `POST /api/v2/chat/completions`；鉴权 `SDK-HMAC-SHA256`（AK/SK + `X-Security-Token`）。OAuth2 PKCE → `POST {snap-manager}/v1/oauth2/tokens` 换 AK/SK/security_token/refresh_token。模型 `GET {snap}/v1/model/builtin`（头 `Agent-Type: PromptCenter`）；额度 `GET {snap}/snap-manager/v1/statistics/plugin`。
- **令牌刷新刚性**：`refresh_token` 与 `client_id=codearts-agent` + DPoP 私钥三者绑定、一次性，走 DPoP ES256/P-256；刷后必须回写新 `refresh_token`，由预刷新任务独占轮转。被作废 → 标记「需重新登录」并停止自动重试。
- **每日签到**：`GET {snap}/v1/ops/delivery?channel=IDE` → `POST /v1/ops/claim` → `POST /v1/ops/confirm`；`/v1/ops/*` 挂 snap 引擎根、不带 `snap-manager`。`ELIGIBLE`→claim+confirm；`CLAIMED`（已领未确认，非终态）→只补 confirm；`CONFIRMED`/`CONSUMED`→已签。**claim 时积分即到账**，confirm 只推进状态；业务码走 HTTP 200。
- **额度＝积分**：`usageTotalPackageCredit` 直接进 `quota_remaining`/`quota_total`，不做单位折算；分项包走 `quota_packages`，套餐 `end_date` 进到期阶梯（积分用不完作废）。
- **凭证页一个渠道一个组**：不拆两条凭证行——`refresh_token` 一次性，两条会各刷一次、第二条必报 `the refresh token has been used` 并烧掉票据。
- **并发上限**：上游硬限每账号每约 60s 最多 3 个会话，超出 `400 TM.00001041`；配 `CODEARTS_MAX_CONCURRENCY` 与 `CODEARTS_REQUEST_WINDOW_SECONDS`。流式 `text` 是**累计全文（替换语义）**，解析层还原增量。
- **流内业务错误分类**：错误内嵌 HTTP 200 的 SSE（`InferHub.<码>`），分类由 `Event.error_kind` 承载，`UpstreamStreamError.kind()` 与 `executor._event_kind` 共用 `provider.base.event_error_kind` 兜底。非多模态模型收图片归 `REQUEST`：不冷却、不累计、不落库，仍换号重试。

### 2.5 OpenCode Zen（`zen`，虚拟凭证）

- 端点 `https://opencode.ai`，`/zen/v1/chat/completions` 与 `/zen/v1/models`。标准 OpenAI SSE，**不逆向**。
- 渠道私有：**免费层门禁伪装**——UA `opencode/<version>` ≥1.18.0（否则 426；无版本号 403）、`x-opencode-session`、body `stream:true`、`tools` 必须同时含 `bash` 与 `read`。只在缺失时补骨架工具；注入名改写为客户端声明的同义工具并复制 schema，无同义工具才丢弃并记 WARNING。
- **免费清单完全动态**：免鉴权 `/zen/v1/models` 返回全部模型且不带免费标记，唯一权威信号是匿名可用性（付费恒 401）；按 `-free` 后缀收窄 + 并发探活只留 2xx，缓存 30 分钟；免费模型标 **x0 倍率**。列表拉取**连续失败按 2^(n-1)×TTL 退避**（封顶 2h，成功清零），日志首次 WARNING 其后 INFO。
- **虚拟凭证行**：无额度接口，`probe_quota` 恒 `probe_failed=True` → health「未知」而非耗尽；删除后重启复活 → 永久停用用「暂停」。401 归 `INVALID`；**403 FreeTierError 在客户端内退避重试一次**，否则单虚拟凭证下 REQUEST 类错误会耗尽轮换预算直接 503。

### 2.6 Kilo（`kilo`，虚拟凭证）

- 端点 `https://api.kilo.ai/api/gateway`，`/chat/completions` + `/models`。标准 OpenAI 兼容，无私有信封、无门禁伪装。**免费模型有权威标记**：`/models` 每条带 `isFree` → 据此过滤、**不探活**（网关级约 200 req/h/IP 的配额极小）；免费模型标 **x0**。思考字段 `delta.reasoning`。凭证用虚拟凭证行。
- 错误分类：401 / 400 / 404 / 422→`INVALID`，403→`REQUEST`，**429 与 502/503/504→`MODEL`**（实测点名具体模型，同批其他免费模型仍 200）。本包不自建熔断。

### 2.7 CodeBuddy 国际（`codebuddy_intl`）

- 端点 `https://www.codebuddy.ai`。与国内站同协议（同一套 `/v2/chat/completions`、`/v3/config`、`/v2/plugin/auth/*`、`/v2/billing/*`，同一 Keycloak realm），但两套独立账号体系 → 独立渠道。**不合并的原因**：`CODEBUDDY_API_ENDPOINT` 是进程级单值，切过去会让存量国内站凭证打到国际站。
- **白名单刻意互不含**：`CODEBUDDY_INTL_ALLOWED_ENDPOINTS` 默认只有国际站主机，填国内站启动时报错（跨站发送即凭证泄露）。
- **能力差异**：聊天、模型列表、额度探测、token 刷新、活跃上报启用；**签到、成长中心未启用**（签到渠道侧无此活动；成长中心无法验证且含不可逆动作）。活跃上报端点 `/v2/report` 与成长中心不同路径，国际站可用。
- **模型列表按套餐探活**：上游 `/v3/config` 返回全量模型，免费号也会看到调用时才回 `400/11158` 的模型 → 用当前凭证逐个发一次最小请求（只读状态行即断开），`11158` 与 `11102` 剔除，`429`/5xx/超时**保留**。结论按账号缓存 6 小时。
- **唯一协议差异：首条 system 强制注入**——国际站要求 `messages[0]` 是 system，否则 400 + `11128`；国内站无此要求。同名模型两站是一条、挂两个渠道。

### 2.8 OfficeAce（`officeace`）

- 端点：控制面 `https://agentarts.cn-southwest-2.myhuaweicloud.com`（OAuth + 开通校验），模型网关 `modelgw-XXXX.officeace.*` **每账号一个**、由 `client-permission-validate` 下发并存在凭证里——不进配置、不进白名单。
- **两套凭据、两套签名、两种寿命**：

  | 凭据 | 寿命 | 用途 | 签名 |
  |---|---|---|---|
  | `Basic model_app_key:model_app_secret` | 不过期 | 聊天 | HTTP Basic |
  | 临时 AK/SK + `security_token` + `project_id` | 约 2h | 积分 / 签到 / 费率 | `V11-HMAC-SHA256` |
  | 同上，打控制面 | 同上 | 开通校验 | `SDK-HMAC-SHA256` |

  混用得 `APIG.0303 app not found`；现象「聊天正常但积分空白、签到一直失败」。
- **V11 的三个必须原样**（写错任一处恒 401）：① 派生密钥外层 key=AK、msg=SK；② `HMAC(HMAC(AK,SK), scope+0x01)[:32]` 转 **hex 文本** 当下一轮密钥；③ **不要**加 `X-Sdk-Content-Sha256` 头。query 必须参与签名，`CanonicalURI` 每段 percent-encode 且末尾补 `/`。
- **登录（poll 轨道）**：`POST /v1/claw/auth/state` → 本地生成 PKCE + DPoP → 浏览器授权 → `GET /v1/claw/auth/code?state=`（202 = 未登完）→ STS `POST /v1/oauth2/tokens` → `client-permission-validate` 取网关与 app key。回调由云端承接，不监听端口。`client_id=pdp5_for_agentarts`，refresh_token 与 CodeArts **互不通用**（混用回 `STS5.1806`），只复用 `codearts/dpop.py` 的 ES256 低 S 签名原语。
- **未开通的账号要可行动**：`subscription: null` → 提示「请先在客户端兑换邀请码」，不用含糊的登录失败。
- **三种上游特例**：① 限流走伪 200（HTTP 200 + `data:{"error":…}`，也见过首帧正常、第二帧才错）；② `81004`/`81009` 走 403/404 但属模型级，业务码判定先于状态码；③ `max_tokens` 是推理 + 正文总预算，`≥16` 追加 1024 再夹到模型硬上限，`<16` 原样透传，未给不补。
- **积分即主账**：单位 `officeace_points`，`Quota.remaining`/`total` 直接是积分；每笔奖励独立到期。签到 `POST /v1/subscription/bonus/claim` 是写操作，body 必须给 `` + JSON `Content-Type`（空 body 会 500），上游对当日重复领取幂等；收获按 `bonus_skus` 剩余量增量算。
- **不做主动探活 / 不做 credit 估算**：探活要真发推理消耗积分；`cost_coefficient` 与 `tokens_per_point` 叠乘关系官方未说明。
- **单账号单用户**：官方《套餐管理》限制单账号单用户，每人各加自己凭证。

### 2.9 LobsterAI（`lobsterai`）

- 端点三个、各自独立配置但共用一张白名单：API 域 `lobsterai-server.youdao.com`（对话/积分/签到/令牌）、门户域 `lobsterai.youdao.com`（登录页 SPA）、版本域 `api-overmind.youdao.com/.../prod/update`（客户端版本号）。
- **身份三元组是凭证的一部分**：`uuid` / `firstKeyfrom` / `latestKeyfrom` 由客户端生成、随凭证持久化，续期时**原样回放**（上游据此认定「同一台客户端」）。少一个 → 永远续不了期，故导入时当场拒绝。
- **登录（paste 轨道）**：门户只接受**回环地址** `redirect_uri` → 授权后浏览器跳到打不开的 `127.0.0.1`，用户把地址栏整条链接粘回来。无 PKCE、无 DPoP：上游只认 `authCode` + 身份三元组。
- **三种上游特例**：① **只接受流式**（`stream:false` 回 500），非流式由本服务聚合；② **业务错误藏在 HTTP 200 的流首**（额度 40201 / 未知模型 40300）→ 解析成错误事件交引擎换号，不当空回复放过；③ **能力头必须带两个**（`kimi-k3-agentic-v1` 是模型目录准入条件、`thinking-level-control-v1` 是关闭思考的前提，不带则 500）。
- **思考档位翻译**：远端把产品档位（`off`/`high`/`max`）映射成 wire 值（`off`/`high`/`xhigh`），两者不是同一套命名。按远端 `thinkingConfig` 翻译，**翻译不出就删掉该字段**——发上游不认的档位会直接 500。
- **积分逐笔到期**：上游没有「总量」这个概念（积分来自每日签到、活动、订阅、加油包，各有各的到期日）→ `Quota.total` 恒 `None`，额度列只有「剩余」。签到发的积分**当天有效**。
- **每日签到**：三段式（`slot` → `context` → `actions/check_in`），带 `idempotencyKey` + 复核 `claimedToday`（上游可能静默拒绝）。签到按 `clientVersion` 门禁——写死旧版本号会拿不到活动，版本号动态取。
- **额度耗尽等签到恢复**：额度不足归「等次日签到恢复」的冷却语义，与本项目既有口径天然对齐，无需新增错误分类。
- **上游无官方开放 API**：本渠道走客户端私有协议，上游改协议即需同步跟进；模型目录可走**公开**端点 `pricing-catalog`（无需鉴权），故模型清单不依赖登录态。

### 2.10 冲突与陷阱

| 问题 | 事实 | 对策 |
|---|---|---|
| 模型 ID 撞车 | 多渠道都有 `glm-5.2`、`DeepSeek-V4-Pro`、`kimi-k3` | 扁平名 + 归一键合并 + 健康度路由 + `@渠道` |
| 积分语义不同 | CB 有周期重置；TRAE / CodeArts 各自权益包独立到期 | 健康分统一百分比；展示层标注周期 |
| credit 可得性 | CB 有 per-request；TRAE 只有账户总额 | credit 可空；TRAE 按官方单价推算并标 `credit_estimated` |
| 登录机制 | CB/Qoder/CodeArts/OfficeAce 轮询或粘贴；TRAE 浏览器回调；LobsterAI 粘贴回环回调 | 三轨（poll / callback / paste），回调统一走主端口 |
| 同一厂商两套票 | OfficeAce 与 CodeArts 同属华为云 STS，client_id 不同，refresh_token 互不通用 | 各自独立实现 OAuth 与凭证类型，只复用 DPoP 原语 |
| 上游 SSE 格式变更 | 各渠道形状不同 | 各渠道解析成中立事件；样本存 fixture 做契约测试 |
| 图片能力标志不可信 | 九渠道出站均为 `image_url` 原样透传（实测 2026-10-15）；目录 `supportsImages` 与实际不一致 | 入站统一归一 `image_url`，**不做模型能力门禁**，能否识图交上游裁决 |

## 3. 架构

### 3.1 分层

一个 FastAPI 进程内三平面，共享同一份 SQLite（WAL + `busy_timeout=5000`）与 Provider 客户端：

- **请求面**（API Key 鉴权）：`/v1/chat/completions` · `/v1/responses` · `/v1/messages` · `/v1/models` · `/v1/user/balance` → 协议层 → 执行引擎（调度 · 冷却 · 会话粘性 · 截断续写 · 统计采集）→ 中立事件流 → 九个 Provider。
- **管理面**（会话 Cookie + RBAC）：React 管理台 → `/api/*`。
- **后台面**（无 HTTP 入口，`TaskRunner` 起 11 类循环）：额度探测 · token 预刷新 · 签到 · 成长中心 · 活跃上报 · 明细清理 · 备份 · 渠道模型列表刷新 · 模型目录刷新 · 运维告警 · 上游状态聚合。

**按平面切的原因**：请求面无状态、可并发，不知「人」是谁（只认 Key 归属用户）；管理面与请求面共用执行引擎与仓储，不做独立服务。两鉴权面互不替代：API Key 进不了管理台，Cookie 进不了 `/v1`。后台面与请求面共用 Provider 客户端与节流器——风控按最小间隔生效，不能各发各的。三面共享一份 SQLite，升级只重启一个进程。

### 3.2 Provider 接口边界

Provider 承担上游私有部分：发请求、解析事件、分类错误、凭证生命周期、额度探测；调度、冷却、重试、统计全在共享引擎。接口三类：凭证生命周期（`start_auth` / `poll_auth` / `complete_callback` / `import_credential` / `refresh`）、额度探测（`probe_quota`）、执行与分类（`stream_chat` / `classify` / `list_models` / `aclose`）。

### 3.3 调度器

**选号前 `Executor._selectable` 两道过滤**（不进 `Candidate.is_selectable`）：① 凭证限额护栏；② 在途租约 `MAX_INFLIGHT_PER_CREDENTIAL`——同账号并发达上限 → 整号不参与选号。在途计数进程内、请求结束释放（含断开/异常），600s TTL 兜底；跨渠道全局，与 `CODEARTS_MAX_CONCURRENCY` 正交。**在途计数兼作负载键**，上限为 0 也照常登记。

过滤后按序出号：

1. **手动 pin 优先**：候选池里存在 `pinned` → **只在 pinned 之间排序**（绝对过滤，不是排序档）。这是管理员的显式指定，优先于任何自动策略——指定了付费号就先用它，哪怕池里另有免费渠道。**排绝对过滤而非排序档的原因**：pin 的语义是「这个号我就是要用」，把它降级成「同档内提权」等于让免费档随时能顶掉指定，指定就变成了「偶尔生效」，与界面上「已指定」的语义不符。会话粘性命中且可选 → 直接复用，不参与排序。
2. **免费优先**：无 pin 时，候选池有 `credit_rate == 0` → 免费渠道整体排前。`credit_rate=None` **不算免费**，与 `0.0` 严格区分。**排在到期额度之前的原因**：省下的真金白银比「几百积分快过期先用」值钱。
3. **在途最少优先**：并发摊到空闲号。**排在到期额度之前的原因**：到期/健康度是静态指标，按它们排出的次序确定，并发会全压同一个号（实测 2 小时内一个号吃 180+ 次、其余闲置）；在途把它拆开，顺序请求（在途恒 0）不受影响。免费档仍压过在途。
4. **到期额度两级字典序**：先比主窗口（`QUOTA_EXPIRY_WINDOW_SECONDS`，默认 36h）内将过期的额度，打平再比次窗口（`QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS`，默认 7 天）。
5. **健康度三态**：`known` > `unknown` > `exhausted`；打平比剩余积分多者优先，再按 `credential_id` 稳定。

**冷却分两层的原因**：`429 + 6004` 是「此模型在当前账号用超」，`400/404 + 11102` 是「当前账号没此模型」；一律记账号级 → 一次模型级限流踢出整号。账号级写 `credentials.cooling_until`，模型级另建 `credential_model_cooldowns`；账号级冷却出现时清空该凭证模型级条目，防「切模型」绕过。业务码只认 `"code": N` 键值形态。冷却时长：`PLAN` 12h、`SOFT` 60s、`OTHER` 连续 3 次→10m、`MODEL` 600s 起翻倍封顶 2h、`BLOCKED` 6h 起封顶 24h、`CONCURRENCY` 固定 60s、`CREDIT` 到次日 04:00。**传输层失败归 `SOFT`**：`httpx.TransportError`、`ssl.SSLError` 与 `UpstreamTransportError` 统一在 `executor._classify` 兜住（异常从 `__aenter__`、`aiter_bytes()`、写请求体多处抛出，逐点包装必漏）；不泛化到 `OSError` / `ConnectionError`。`ssl.SSLError` 必须单列：anyio 只把 `SSLEOFError` 转 `BrokenResourceError`，其余 TLS 中断（`record layer failure` 等）原样上抛，既非 `TransportError` 也非白名单具体类 → 不列就逃到 `stream_guarded` 变 `internal_error`：不换号、不冷却、统计零痕迹。**上游重置时刻可覆盖常量**：`Retry-After`（整秒/HTTP-date）或 `Try again in 17h 59m` 经 `parse_retry_after` 解析后按作用域写入，双端钳制 `[now + 常量, now + 24h]`。**头缺失看响应体**：CB 国际站 `429+6004` 不给 `Retry-After`，恢复时刻只在正文（`your usage will reset at 2026-10-25 22:34:25 UTC+8`），由 `parse_reset_at` 解析同一种绝对时刻；头优先于正文，正文无偏移（`UTC±N`）则放弃。不解析正文 → 固定 600s 到期重试、再撞 429（实测同一号一天被爆 42 次）。

**「窗口内积分总量」的原因**：实测 CB 额度是几十个各自独立到期的小包（每日 100 积分 × N），只存一个日期没有区分度。四个取舍：① 统计窗口内到期积分总量，账号间才可比；② 落库到期阶梯 `[(到期 epoch, 包剩余积分)]` 而非预计算数字；③ 过滤条件必须 `end > now` 且排除已用完的包；④ 两级窗口，只比 36h 会大量账号同为 0。**展示与调度指标分开存**：`quota_packages`（含包名/用量）与 `quota_expiry_ladder`（只有 `[到期, 剩余]`）。

**健康度归一化**：`known(0-100) | unknown | exhausted`。CB 取 `CycleCapacityRemainPrecise`/`CycleCapacitySizePrecise`，TRAE 取 `credits_limit - credits_amount` / `credits_limit`。**不用 `TotalDosage`**：它 = Σ(size − CapacityUsed)，`CapacityUsed` 是上周期口径 → 出现 `remaining == total`、健康度恒 100。**必须三态**：探测失败算 0 分会让其永远轮不到。

**会话粘性**：三级识别——① 请求体 `conversation_id` / `conversationId` / `prompt_cache_key`；② 请求头 `x-session-id` / `x-opencode-session` / `x-session-affinity` / `conversation-id`（须匹配 `[A-Za-z0-9._:-]{1,128}`，经 contextvar 传入，键带 `header:` 前缀避免与 body 撞键）；③ 消息增量前缀指纹。**头夹中间的原因**：body 显式键最精确；头是客户端主动声明的身份，比指纹可靠。头值掺 `username` 再哈希——否则一个用户可靠猜头值占用别人账号。TTL（`CONVERSATION_STICKY_SECONDS`）默认 1h。带 `user_id` 时**不派生**前缀兜底键（同用户并行对话前缀可能相同）。手动 pin 优先；凭证报错仍走轮换；条目纯内存。

### 3.4 模型名三字段

同一链派生：**原代号**（`raw_id`，转发时经别名表换回，永不改动）、**归一键**（剥掉 `-free` / `free` 路径段、`-new` 与命名空间前缀后 slug 化，合并键）、**展示名**（`name`，上游可读名优先，无则从原代号派生）。

`/v1/models` 对外 `id` 一律归一键，原代号进 `by_provider.{渠道}.raw_id`；三种写法都能直接请求。同模型异名由 `MODEL_SYNONYMS` 收敛，只登记人工核实等价。哨兵名（`auto` / `default`）只在本渠道内成立。黑名单按归一后的对外写法匹配。

### 3.5 登录三轨

`AuthFlow` 三值：`poll`（CB / Qoder）、`callback`（TRAE，302 回 `/authorize`）、`paste`（CodeArts，服务端监听不到本机回环端口）。

- 回调统一走主端口 `/authorize`，远程部署只需暴露一个端口。回调地址写进登录 URL → 必须可配 `PUBLIC_BASE_URL`；基址是回环/`0.0.0.0`（默认值）时改用当前请求 origin，远程与容器部署忘配也不会「授权完成却不回调」。
- 前端在「凭证管理」页内实现：**poll** 拿 `authUrl` 开新窗、轮询 `/api/auth/upstream/poll`；**callback** 开授权窗、302 回 `/authorize` 落库，前端轮询列表出现新条目即完成（发起时把实际回调地址写进提示，超时提示指向 `PUBLIC_BASE_URL`）；浏览器打不开回调地址（NAS / 远程部署常见「网络错误」）时，同一对话框给出粘贴入口，把地址栏整条链接交给 `POST /api/auth/upstream/complete` 用同一条 pending 完成兑换。`/authorize` 无鉴权 → 按**发起人会话** + **`loginTraceID`** 双重绑定（Cookie 不在回调入口时按 trace 反查发起人），待完成登录 TTL 10 分钟；浏览器收到中文说明页而非 JSON。

### 3.6 中立事件层

上游 SSE 解析到中立事件（`EventKind`：`content` / `reasoning` / `tool_calls` / `usage` / `finish` / `error`），出口适配器（OpenAI / Responses / Anthropic）只消费中立事件。

**入口语义收口**：chat 的 `response_format` 只放行 `text` / `json_object` / `json_schema`（后者要求非空 `name` 与 `schema`，未知字段丢弃），Responses 的 `text.format` 复用同一校验函数。Anthropic 的 `cache_control` **默认原样透传**，只校验结构，取值交上游裁决；无标记时映射结果逐字节不变。

### 3.7 账号与权限面

三角色：`admin`（用户管理、运行时配置、凭证写、全量统计、审计、告警）、`operator`（凭证写、全量统计、上游状态、模型列表）、`viewer`（只读：仪表盘、凭证列表、Playground、统计）。

- **鉴权链**：签名 Cookie（HMAC，带用户名与 `session_epoch`）→ 用户仍存在且启用 → epoch 与 DB 一致 → 角色现读 DB。缺一即 401；**角色不进 Cookie**，降级立即生效。
- **吊销不建会话表**：`users.session_epoch` 在改密/改角色/启停/硬删时 `+1`。
- **建号与重置走一次性激活令牌**：库里只存 SHA-256 摘要 + 过期时间。被重置用户改密前只放行「看会话/改密/登出」。
- **删除语义**：禁用是主路径（可逆、保住用量归属），硬删只在 CLI，管理台不暴露 `DELETE`；登录成败、账号变动、凭证写操作全部入 `audit_events`，绝不记密码或令牌明文。
- **引导与防锁死**：`ADMIN_USERNAMES` 只在引导期提权（库里已有活跃 admin 就不再改写任何人的角色——否则管理台的降级会被下一次重启撤销）；三层防锁死 = Web 端守卫 + CLI 拒绝硬删最后一个活跃 admin + bootstrap 发现无活跃 admin 直接启动失败。

## 4. 数据模型

DDL 以 [src/db/schema.sql](src/db/schema.sql) 为准，共 13 张表，`SCHEMA_VERSION = 29`。变更纪律：schema.sql **只加不改**；新增列走 `_MIGRATION_COLUMNS` 幂等补列，删表须在 `_MIGRATION_DROPS` 补一条。

| 表 | 用途 |
|---|---|
| `api_keys` | Key（SHA-256 摘要 + 预览 + 渠道绑定 + IP 白名单 + 模型白名单 + 到期 + 配额） |
| `credentials` | 加密凭证（Fernet）+ 调度状态 + 额度与到期阶梯 + token 到期 + 成长结果 + 运维标签 |
| `credential_limits` | 凭证级限额护栏；**缺行 = 不受限** |
| `usage_events` | 请求明细，保留 90 天 |
| `usage_hourly` | 小时汇总（永久），渠道/模型/用户维度读此表 |
| `growth_events` | 成长中心每轮一行（仅 CodeBuddy）；活跃上报复用 |
| `credit_events` | 两次探测之间的**净变化**（`window_start`、`source`、`ledger`），不是动作归因 |
| `credential_model_cooldowns` | (凭证, 模型) 级冷却与「无此模型」负缓存，含 `hits` 供指数退避 |
| `runtime_settings` | 运行时配置覆盖（纯 key/value），白名单在 `runtime_settings.HOT_SETTINGS` |
| `users` | 账号（PBKDF2 + `role` + `enabled` + `must_change_password` + `session_epoch` + 激活令牌摘要） |
| `audit_events` | 审计流水 |
| `alert_events` | 告警事件（四类规则；`resolved_at` 非空 = 迟滞确认恢复） |

- 调度状态与到期阶梯**落库**，重启不丢；签到去重、模型列表缓存、任务运行态**进程内**（重启归零，不编造记录）。
- 模型目录落盘 `DATA_DIR/model_catalog.json`，OpenRouter 目录落盘 `data/openrouter_catalog.json`。
- **脱敏纪律**：不存提示词、回答、请求头、Token、工具参数、原始错误体、会话 ID。唯一例外 `DUMP_REQUEST_BODIES=true`（默认关，有界保留 200 份，含完整对话内容，不得长期开启）。

## 5. API 契约

**外部（API Key）**：`POST /v1/chat/completions`（`n` 只接受 1）、`POST /v1/responses`、`POST /v1/messages` + `POST /v1/messages/count_tokens`（本地估算）——三者共用同一调度链路；`GET /v1/models`（`id` 为归一键 + `name` + `providers` + `by_provider` + 可选 `benchmarks` / `cost` / `reasoning_efforts`；自定义别名带 `custom_alias: true`）；`GET /v1/models/{id}`（`:path` 容纳带命名空间的原代号；四种写法、大小写不敏感；不存在或不在 Key 白名单内一律 404 + `model_not_found`）；`GET /v1/usage?days=1|7|30`（只查该 Key 自己的本地明细）；`GET /v1/user/balance`（读探测缓存）；`GET /health`、`GET /healthz`、`GET /metrics`（均无鉴权，`METRICS_ENABLED=false` 关闭）。

**管理台（Cookie + RBAC）**：凭证管理、API Key（含六项配额，超限 429 + `Retry-After`，判定异常一律放行并记 warning）、用量统计、Playground、模型列表、用户管理、审计、告警、上游状态、任务与配置、服务端日志。

- 账号：`GET|POST /api/users`、`PATCH /api/users/{username}`、`POST /api/users/{username}/{disable|enable|reset-password}`（**无 DELETE**）；`POST /api/auth/password`；`GET|POST /api/auth/activate`；`GET /api/audit`。
- 凭证运维：`POST /api/credentials/{id}/checkin|toggle|revive|probe|accounts/select`、`PUT /api/credentials/{id}/limits`、`GET|POST /api/credentials/{id}/growth`、`GET /api/credentials/{id}/credit-events|accounts`。
- 批量：`POST /api/credentials/batch`（`probe|toggle|checkin|revive`）。逐条隔离，响应 `{total, succeeded, failed, results[]}`；`toggle`/`revive` 落一条汇总审计。
- 配置与任务：`GET|PUT /api/settings` + `GET /api/tasks` + `POST /api/tasks/{key}/run`。列表端点统一 `sort` + `order`；未知键回落默认。回调：`GET /authorize`。
- 告警：`GET /api/alerts` + `POST /api/alerts/{id}/resolve`。`AlertTask` 启动时从库里未恢复的行重建集合。
- 上游状态：`GET /api/upstream-status`（operator）返回内存快照 + 任务运行态；**不落盘**。四种解析器归一化到六档，加源只在 `SOURCE_SPECS` 加一行。
- 统计：`GET /api/stats/{overview,by-provider,by-model,by-user,by-credential,timeline,model-timeline,percentile-timeline}`（延迟分位 P50/P90/P95/P99 可切，默认 P95；输出速度 `tps_visible` / `tps` / `tps_estimated`）、`GET /api/stats/errors`（失败归因，`limit` ≤ 5000；`failures` 为尝试口径，`rescued` 为同用户同 `request_id` 另有成功行的次数，`failures − rescued` 是未被救回的失败尝试数）、`GET /api/stats/events`（游标/OFFSET 双模式，钻取过滤，`ok=0`，`?format=csv`）、`GET /api/stats/events/stream`（SSE，订阅者有界缓冲 200、并发上限 8）。
  - `timeline` / `model-timeline` 的 `metric` 取值：`requests` / `tokens` / `cost` / `latency` / `ttfb` / `success_rate` / `cache_rate` / `cost_per_request`；**取值域外的拼写显式 400**（不静默回落到 `requests`——否则客户端拼错参数会拿到一份「看起来正常」的请求数图，错误被完全吞掉）。`percentile-timeline` 只认 `latency` / `ttfb`。派生指标（成功率、缓存命中率、单请求成本）在**聚合后相除**（`sum/sum`），不是逐小时比率的平均；无样本给 `null` 而非 0。
  - 时间轴是**稠密**的：按小时补齐缺失桶（跨度上限 `MAX_DENSE_HOURS` = 92 天，超出则只回观测小时）。缺失桶按指标语义补值——可加量（`requests` / `tokens`）补 0，均值 / 比率 / 成本补 `null`（前端断线，不画成 0）。
  - `timeline` 每个点带 `__total`（全渠道合计）：均值与比率必须按同一小时的 `sum/count` 重新相除，前端不得把各渠道值相加。
  - `percentile-timeline`（`metric=latency|ttfb`）返回按小时的 `{hour, p50, p95, samples}`，读明细表（仅 90 天），只统计成功请求；供延迟图「中位数线 + P95 带」使用。
- 日志：`GET /api/logs` + `GET /api/logs/stream`（只读、admin-only，`LOG_BUFFER_LINES=0` 时 404）。
- 侧栏提醒：`GET /api/nav-badges`，**按角色裁剪 payload**（缺失 = 无权看），不做整体 403。各计数只数「待办」，与页面数据不是同一口径。

实现以代码为准，使用说明见 [README.md](README.md)。

## 6. 安全边界

- **上游端点白名单**：真实 Token 只发往显式配置地址。CB 启动时强制校验；国际版白名单刻意不含国内站（**白名单里出现国内站主机也一律拒绝**——只校验「端点在白名单里」不够，把国内站加进国际站白名单后端点校验会通过、Token 照样跨站发）；TRAE 的 `apiHost` 导入时校验；Zen / Kilo / Qoder / CodeArts 各有独立白名单。
- **凭证加密**：Fernet，密钥走 `APP_SECRET`（≥16 字符，拒绝低熵）；**丢失 = 凭证全部不可解**，不做轮换。解密失败返回可行动错误码。
- TLS 校验默认开；Host / Origin 白名单；CSP `frame-ancestors 'none'`、`object-src 'none'`、`base-uri 'self'`、`form-action 'self'`。**请求体上限**：登录 8KB、其余 16MB（ASGI 层按实际字节计数）。登录三级限流 + PBKDF2 并发上限；失败键有数量上限 + 过期淘汰。
- **API Key 仅存摘要**，明文只在创建时返回一次；可限渠道、IP（默认不信 `X-Forwarded-For`，仅 `TRUST_PROXY=true` 时取最后一个）、模型（fnmatch，无法判定时 fail closed）、到期。
- **会话**：`SameSite=Lax` + 写操作 CSRF（含 logout）；`Secure` 按请求实际协议动态判定；无 Cookie 的写端点（登录 / `/authorize`）也做跨站防护（登录按 Origin/Referer 同源校验，`/authorize` 按 `loginTraceID` 绑定）；会话与 Key 校验用户存在、启用且 epoch 一致。
- **错误响应不回流上游正文**：只带受控标识，上游错误体摘要只进日志；未匹配的 `/api`、`/v1` 返回 JSON 404（**任何 HTTP 方法**都如此——只注册 GET 时未匹配的 POST 会落到 Starlette 的 405 `{"detail":…}`，与统一错误体形状不符，调用方按 `error.code` 解析会踩空）。**客户端读体断开不是服务端错误** → 回 `499 client_disconnected`。**日志脱敏**：不打印 Token、完整请求体；审计写 INFO，绝不记明文。
- **请求 ID 是客户端可写输入**：按 `[A-Za-z0-9._:-]{1,128}` 校验后才采信，不合法改用自生成。
- **服务端日志页只出不进**：`redact_or_drop` 在行离开进程前整行过滤（独立敏感词、键值形态、`sk-…`、`Bearer <值>`），空行也丢。代价：「token 已刷新」这类正常行被一起吞掉——宁可漏一条，不把凭证推进浏览器。
- **请求级结构化日志**（`REQUEST_LOG_ENABLED`）：一行 logfmt，含 `request_id` / 路由模板 / 状态码 / 耗时；**不写 query string、请求体、请求头、用户名**；未匹配路由回落为清洗截断的原始路径。
- **租户隔离**：凭证昵称取自全局共享池，仅 admin/operator 可见；CSV 导出同样仅 admin/operator；SSE 广播 payload 不含昵称，仅 admin/operator 连接由服务端补 `credential_name`。
- **热更写入即校验**：float 拒 NaN/±inf；字符串有长度上限；webhook 只接受 `http(s)` 并做 SSRF 校验（写入期拒内网/回环/保留地址，**投递前再按 DNS 解析复核**），外发载荷对 `nickname` 打码。告警出站 `trust_env=False`。
- **上游流解析有界**：SSE 单行与单帧缓冲设上限，非 2xx 错误体有界读取；OAuth state store 有 TTL。**入站解析把关**：拒绝越界数值字段与 JSON 里的 `NaN`/`Infinity`——**含 JSON 溢出成 `inf` 的合法数字（`1e999`）**：字面量闸拦不住它，httpx 序列化时会抛 `ValueError` 变 500；按解析层拦（三个出口共用一个入口），逐字段补齐既写三遍又漏掉 chat 出口的未知透传键。
- **OAuth 弹窗**只允许 `http(s)`，`opener` 置空。**库版本守卫**：`user_version` 高于本程序时拒绝执行。`--password-stdin` 支持；拒绝含 `:` / 换行的用户名。
- **不做**：mTLS；管理台与端口的 IP 限制（交给反向代理）；全量请求审计。

## 7. 风险清单

| 风险 | 等级 | 对策 |
|---|---|---|
| CB 协议复杂度高 | 高 | 先 bearer-only 跑通再补 OAuth；不可逆动作用配置开关总控 |
| 上游 SSE / 私有协议变更 | 中 | 样本存 fixture 做契约测试；解析失败不静默 |
| 模型 ID 撞车导致路由错误 | 中 | 扁平名 + 归一键合并 + `@渠道`；统计行强制带 provider |
| 上游 credit 不保证可得 | 低 | credit 仅辅助展示；健康度只依赖额度探测接口 |
