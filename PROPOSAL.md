# Coding2API 立项决策

把 CodeBuddy、TRAE SOLO、OpenCode Zen、Kilo Gateway、Qoder 与 CodeArts 六个上游通道，统一封装为 OpenAI 兼容 API，并提供公共凭证池、统一调度与按人用量统计。

> 本文档记录立项决策与可行性核实。实现细节见 [TECHNICAL.md](TECHNICAL.md)，使用与部署见 [README.md](README.md)。

## 1. 决策摘要

| 编号 | 决策 | 选择 |
|---|---|---|
| Q1 | 目标场景 | 2–10 人小团队共享 |
| Q2 | 技术栈 | Python / FastAPI，从零新架构 |
| Q3 | 仓库 | 全新仓库，新建架构 |
| Q4/Q20 | 项目名 | `coding2api` |
| Q5 | 前端 | React + Tailwind，从零新写 |
| Q6 | 迁移方式 | 从零重写，旧项目仅作参考 |
| Q7 | 凭证归属 | 公共池 + 按人统计 |
| Q8 | 协议出口 | v1 仅 OpenAI；v1.1 加 Anthropic（**已由 Q59 落地**：P0-1 新增 `POST /v1/messages`，供 Claude Code 接入） |
| Q9 | 存储 | SQLite，schema 重新设计，凭证入加密列 |
| Q10 | 配额 | 不做配额，只做按人统计 |
| Q11 | 前端范围 | Dashboard / 凭证 / API Key / 统计 / Playground / 任务与配置 / 登录（Q34 推翻「无设置页」：可热更配置走管理台，其余配置仍走 env；Q38 把该页与后台任务合并，页名「任务与配置」） |
| Q12 | 调度策略 | 统一健康度 + 到期积分指标 + 冷却状态机，保留手动 pin |
| Q13 | Anthropic | v1.1，架构预留中立事件层 |
| Q14 | 测试 | 全量 100%（行 + 分支），契约测试优先 |
| Q15/Q26 | 健康度归一化 | 剩余积分百分比，跨 provider 可比 |
| Q16 | 抽象边界 | 细接口：Provider 只管发请求 + 解析 + 分类错误 |
| Q17 | 登录 | 双轨（CB 轮询 / TRAE 回调），前端统一状态机 |
| Q18 | 权限 | 三角色 `admin` / `operator` / `viewer`（B5 由「单 admin + 普通用户」升级，见 Q39）：admin 管用户与配置，operator 管凭证，viewer 只读 |
| Q19 | 交付顺序 | 串行：骨架 → TRAE → CB 基础 → CB 完整化 |
| Q21 | 模型名 | 扁平，自动路由 |
| Q22 | 数据迁移 | 不迁移，全新开始 |
| Q23 | 发布 | 源码 + Dockerfile + compose，CI 跑测试 |
| Q24 | 统计 | 统一 token，credit 可空；成本为 models.dev 刊例价估算（Q70） |
| Q25 | 抽象风险 | M0 mock 冻结接口；M1b 结束做双 provider 对比验证 |
| Q27 | 指定上游 | `model@provider` 后缀覆盖 |
| Q28 | 容器 | Dockerfile + compose 双份，CI 验证 compose |
| Q29 | 文档 | 中文为主 + 英文 README |
| Q30 | License | MIT + NOTICE 三方溯源，不做自更新 |
| Q31 | 到期积分排序 | 两级字典序：主窗口（36h）+ 次窗口（7 天）内到期积分总量依次做排序键（均 env 可配）；落库到期阶梯而非单一日期 |
| Q32 | Responses 出口 | v1 只做 `chat/completions` 子集：`POST /v1/responses` 与 chat 共用同一 executor，出口 translator 可注入；`include`/`store`/`previous_response_id` 按 Codex CLI 实测取舍（见 TECHNICAL §3.7） |
| Q33 | 凭证暂停语义 | 复用现有 `enabled`（不新增 `manual_disabled` 列）：实测 `enabled=0` 只摘对话流量，后台任务（签到/刷新/成长/探测）只认 `disabled`；UI 文案统一为「暂停/取消暂停」以区别于系统禁用后的「恢复」（见计划 B3.1） |
| Q34 | 运行时配置热更 | **推翻 Q11 的「无设置页」**：新增 `runtime_settings` 表 + `RuntimeSettings` 覆盖层 + 管理台配置页（Q38 后与后台任务合并为「任务与配置」，导航第 6 项、管理员专属）。白名单项改完立即生效，无需重启（当前项数见 `TECHNICAL.md`）；**DB 覆盖值优先于 .env**，UI 与日志明示，可「恢复默认」。启动期项（密钥 / 端口 / 数据目录 / 上游白名单）不进白名单——它们决定进程如何启动，运行期改只会让内存与磁盘静默分叉。**B4 修正**：模型黑名单原先「改完最长 300s 才生效」（列表缓存存的是过滤后结果，且被滤模型会从失败兜底缓存复活）；现缓存改存未过滤表、过滤在每个出口现做（过滤按归一后的对外写法匹配：原代号、其归一键、展示名、展示名归一键四种任一命中即滤） |
| Q35 | token 到期展示与预警 | `credentials` 增列 `token_expires_at` / `token_issued_at`（`SCHEMA_VERSION` 10→11）。到期优先取显式 `expires_at`，缺失/非法时回落 access token 的 **JWT `exp`**；签发时间取 JWT `iat`（新增渠道中立的 `provider/token_expiry.py`）。**实测 CodeBuddy 的 token 响应（OAuth 登录与刷新）不带任何到期字段**，只看 `expires_at` 会恒为 0，既让管理台看不到到期，也让 `needs_refresh` 永不触发（只能等 401 硬禁用）。两边都取不到时为 0 = 未知，**不猜本地 TTL**。展示为独立「token 剩余」列；**进度条与「最后续期」的最初设计已移除**（两渠道寿命 55 天 vs 14 天无可比性），`iat` 仍落库供诊断。老库不批量回填，列表读到时按需从密文派生 |
| Q36 | 积分变动流水 | 新增 `credit_events` 表（`SCHEMA_VERSION` 11→12），在额度探测写回的**同一事务**里比对余额、只增记一条。**计划原要求 `source` 标注来源（签到/成长/对话），但实测三类证据都拿不到真实归因**：diff 只见区间净变化，其间签到、成长领取与对话消耗可能同时发生。故 `source` **改为只表达归因已知度**（`observed` / `sync`），另加 `window_start` 记变化覆盖时段，前端一律说「净变化」而非「签到 +N」。余额未变不记；任一端未知仍记但 `delta` 为空（绝不量化成 0）。保留期同 `usage_events`（90 天） |
| Q37 | 池健康与多 Key 出口 | `GET /healthz` 返回 `{status, service, version, credentials:{total,ready,cooling,paused,disabled}}`（保留 `GET /health` 作纯存活探针）：`ready` 复用调度器 `Candidate.is_selectable` 口径，五类互斥且合计 = total——**计划原文只列 4 类**，但项目已区分「系统禁用」与「用户暂停」（Q33），少一类会让计数对不上，故补 `paused`。`api_keys` 增 `provider_binding`（`codebuddy`/`trae`/空 = 自动）与 `allowed_ips`（`SCHEMA_VERSION` 12→13）；`deps.api_key_user` 升级为返回 `ApiKeyPrincipal`，并**在鉴权当场**判定来源 IP。IP 白名单**默认不信 `X-Forwarded-For`**（客户端可写），仅 `TRUST_PROXY=true` 时采信且取 XFF **最后一个**条目，故只适用于「本服务前恰好一层受信反代」。绑定渠道在 `executor` 收窄候选上游：模型归属别家渠道时 400 并给出实际归属，目录未就绪时保守放行。**不做**每 Key 配额 / 多租户（与 Q10 冲突） |
| Q38 | 后台任务可视化（「任务与配置」页） | 管理台原「运行时配置」页与后台任务**合并**：可热更配置按 `HotSetting.task` 归属进任务卡片，无归属的按 `HotSetting.group` 进网关卡组（模型路由 / 选号与会话 / 渠道节流 / 后台任务节流），前端每个任务 + 每个网关卡组各一个 tab；新增 `GET /api/tasks`（admin）下发 7 类任务运行态，前端 30s 刷新。**运行态只存进程内、不落库**（`tasks/status.py`）：重启归零比编造重启前记录更诚实，也省掉新表 + 保留期清理 + 老库迁移，**无 schema 变更**。**no-op 轮次不入账**（返回 `None` = 未到点 / 未开启），否则签到会显示成「刚刚跑过」而当天其实没签；异常入账（`last_error`），否则「一直在失败」会显示成「尚未执行」。周期与开关取**当前生效值**，不是装配快照 |
| Q39 | 用户账号体系（B5） | **用户从 `users.txt` 迁入 SQLite**（`users` + `audit_events` 两表，`SCHEMA_VERSION` 13→14）：`users.txt` 降级为**一次性引导导入**（首个 admin 仍可用 `scripts/hash_password.py` 或新 `scripts/create_user.py` 建），老文件不删、可作为回滚；`ADMIN_USERNAMES` 只标**引导期**提权。三角色（S1）：`admin` 管用户与配置、`operator` 管凭证写操作、`viewer` 只读。会话吊销（S3）不建会话表，用 `users.session_epoch` 进签名 Cookie 的 `ep`——改密/降级/禁用/硬删一律 bump，**角色每请求现读 DB**，不进 Cookie。删除语义（S6）：**禁用是主路径**（可逆、保住用量归属），硬删只在 `scripts/create_user.py --delete --force`；管理台故意不暴露 `DELETE`。建号/重置（S7）走**一次性激活令牌** + `/activate` 自设密码，明文仅响应回显一次、库里只存 SHA-256 摘要（无邮件设施下的最优解，与「API Key 明文仅一次」同一心智模型）。审计（S8）：登录 + 账号变动 + 凭证写操作入 `audit_events`，**绝不记密码/令牌明文**。防锁死三层：Web 端 self_target + last-admin 守卫，CLI 端删最后活跃 admin 拒绝，bootstrap 无活跃 admin 直接启动失败 |
| Q40 | OpenCode Zen 免费层（第三渠道 `zen`） | 把 `opencode.ai/zen` 免费层接成第三个 provider（`KNOWN_PROVIDERS` 加 `"zen"`，**无 schema 变更**）。**协议是标准 OpenAI SSE，不逆向**：无需私有信封解析，唯一渠道私有的是**免费层门禁伪装**——UA `opencode/<version>`（≥1.18.0，低于阈值 426、无版本号 403）、`x-opencode-session`（`ses_` + 12 hex + 14 base62）、body `stream:true`、body `tools` 必须**同时**含 `bash` 与 `read`。门禁要求的这两个工具客户端从未声明过，故**注入空壳 + 过滤回包**：只在缺失时补骨架工具，模型若真的调用注入名（含按 `index` 跟踪的流式分片）则整条丢弃，且该流无其他真 tool_call 时把 `finish_reason` 收敛为 `stop`（用户自带同名工具时不注入、不过滤）。**展示名从 id 派生**：上游 `/zen/v1/models` 不返回任何模型名字段（只有 `id` 与 `owned_by`，后者恒为厂商名 `opencode`，直接透传会让 Zen 所有模型在列表里都叫「opencode」），故 `pretty_model_name` 去免费后缀后按段 title-case 并修正品牌/缩写写法（`longcat-2.5-preview-free` → `LongCat 2.5 Preview`；2026-10-01 起该函数为渠道中立的 `provider/naming.py` 的薄封装，六渠道共用同一套清洗规则）。**凭证模型用虚拟凭证行**：Zen 无凭证/无额度接口，池里种一条空凭证复用现有调度/冷却/统计（`probe_quota` 恒 `probe_failed=True` → health NULL「未知」，**不是耗尽**）；删除后重启复活，也可在凭证页「登录渠道账号」点「添加 OpenCode Zen」立即补回（复用通用导入端点），永久停用请用「暂停」。**免费模型清单完全动态（现拉现探）**：上游 `/zen/v1/models` 免鉴权但返回全部模型（含 70 多个付费模型）且**不带任何免费标记**（`owned_by` 恒 `opencode`、无 cost 字段）。唯一权威信号是匿名可用性（付费恒 401 `Missing API key.`）。故两步过滤：按 `-free` 后缀收窄候选 → 并发探活只保留 2xx（下线 400 / 区域 403 / 故障 5xx / 超时全剔除）。无静态白名单，上游增删自动跟随；候选全灭时抛错让 `/v1/models` 用缓存兜底。判活结果按 30 分钟缓存（`MODELS_CACHE_TTL_SECONDS`），避免服务层每 300s 重拉列表就重探一轮。免费模型显式标 **x0 倍率**（`credit_rate=0.0`），列表 UI 显示 x0 且排序时排最省一档。zen 的 401 归 `INVALID` 而非 `DEAD`（无凭证，401 只表示该模型需要付费 key），避免强制付费模型把整条渠道硬禁用。`timeline()` 从硬编码两列改为按 `(hour, provider)` 动态 pivot（键名 `codebuddy`/`trae` 不变，无 zen 数据时旧契约不变）。新增 `ZEN_API_ENDPOINT` / `ZEN_ALLOWED_ENDPOINTS`（端点白名单，Zen 不带真实 Token）/ `ZEN_OPENCODE_VERSION`（门禁 UA 版本，上游改阈值时改 env） |
| Q41 | 模型列表按渠道凭证加载 | `/v1/models` 与 `/api/playground/models` 只合并「当前有可用凭证」的渠道（`candidates(selectable_only=True)`：未暂停、未硬禁用）——**从未接入 / 全部暂停 / 会话失效的渠道既不拉取上游也不展示**，避免把根本打不通的渠道模型混进列表（此前无凭证也调用 `list_models({})`，CB/TRAE 回退静态表、zen 匿名拉取，导致幽灵模型）。判定在缓存分支之前：没凭证就不读缓存也不合并；冷却中的凭证仍算「有凭证」，渠道接了只是暂时限流，列表不跟着闪没。冷启动只有 zen（自带虚拟凭证）在列，接入 CB/TRAE 后下一次列表请求（该渠道无缓存）才拉取；由此启动预热也不再对无凭证渠道白打上游。渠道重新接入后若缓存仍在 TTL 内则直接复用，不重复打上游 |
| Q42 | TRAE tool_call 分片续块保留（issue #1 死循环根因） | TRAE 上游工具调用是**按 `index` 的分片流**：每个 index 首片带 `function_call.name`，后续片**只有 `arguments` 增量、没有 name**。`_normalize_solo_tool_call` 原按「无 name 即丢弃」过滤，把续片全部吃掉 → 客户端按 index 拼出**截断的参数 JSON** → 工具执行报错 → 原样重试同一轮 → 死循环（WorkBuddy / Cherry Studio 都复现）。改为与 CodeBuddy/Zen 同一条规则：**只丢「无 name 且 arguments 为空」的噪声**（`_is_blank_solo_tool_call`：name 空，且 arguments 为 `None`、空串、空 JSON 串或空对象），带实际 arguments 的续片保留并归一为 OpenAI `function{arguments}` 形状。此前 32d8cc8 的「空名噪声过滤」误伤续片，本轮把判据收敛到「空名且空参」。无 schema / 配置变更 |
| Q43 | macOS 部署模板去本地路径（占位符 + 安装脚本渲染） | `deploy/launchd/com.coding2api.plist` 与 `deploy/newsyslog/coding2api.conf` 原把开发机家目录绝对路径与用户名（`<user>:staff` 属主）写死在仓库里——克隆到别处不可用，还泄露本机目录结构。launchd 与 newsyslog **都不展开 `$HOME` / 环境变量**，路径必须写死，无法像 systemd 模板那样用约定路径，所以改成模板占位符 `__PROJECT_ROOT__`（路径）+ `__LOG_OWNER__`（属主），由 `scripts/install-launchd.sh`（新增，渲染 plist → `~/Library/LaunchAgents` → bootout/bootstrap，bootout 异步需重试）与 `scripts/install-newsyslog.sh`（改为渲染后写入 `/etc/newsyslog.d/`）在安装时替换成本机实际值。`test_deployment_assets.py` 锁三条不变量：全仓库无个人家目录路径、模板含占位符、安装脚本渲染占位符；newsyslog 与 launchd 模板的路径一致性改为**模板对模板**比对（渲染后仍校验本机已装 plist）。systemd / logrotate 的 `/opt/coding2api`、`/var/log/coding2api` 是约定部署路径非个人路径，保留不动。无 schema / 配置变更 |
| Q44 | Zen 独立聊天节流（不再与 CB/TRAE 共享 pacer） | 原先 zen 的 `ZenProvider(pacer=chat_pacer)` 与 CodeBuddy/TRAE 共用同一个全局 pacer（`codebuddy_chat_min_interval`，默认 5s，min=max=5 → 固定 5s）。该 pacer 的存在理由是避开 CB 11128 / TRAE 流内错误的**账号级频率风控**，而 zen 是匿名免费层、无账号、无此类约束。共享的后果是**自伤式延迟**：任何 CB/TRAE 请求刚发出，紧随的 zen 请求就要在 pacer 里空等满 5s 才打上游；单一用户连发或 IDE 并发多个 zen 请求时，第 2、3 个请求 TTFB 实测 +5s、+10s（并发 3 个 zen：9.2s / 13.5s / 18.1s，去掉节流后应基本齐平）。实测确认**不是网络问题**：首 token 直连与走本机代理（127.0.0.1:7897）互有胜负、无稳定收益（`GET /models` 直连 0.29s vs 代理 0.60s；chat TTFB 直连 ≈ 代理），故不引入代理。改为 zen 用独立 `Pacer`，新增热更项 `ZEN_CHAT_MIN_INTERVAL`（默认 **0** = 不节流）；仍保留可调旋钮，若上游日后对匿名层限流可调大。CB/TRAE 继续共享原 pacer，互不影响。无 schema 变更 |
| Q45 | 聊天节流按凭证分桶并允许桶内并发（同渠道同模型并发不再串行台阶） | Q44 给 zen 拆了独立 pacer 后，CB/TRAE 的 `chat_pacer` 仍是**一把全局 `asyncio.Lock` + 单个 `_last_started`**：任何两个请求（哪怕不同账号、不同模型）都串行排队，后到者按 `interval - elapsed` 补足等待。实测 3 个并发 CB 请求 TTFB ≈ 1.55 / 6.71 / 11.47s（正好 +5s、+10s 台阶）；把间隔热更为 0 后 6 并发 TTFB ≈ 1.48–1.84s、总 1.84s → 延迟完全来自节流排队而非上游。**关键**：并发请求常被会话粘性/健康度排序收敛到**同一个凭证**（DB 里 6 条并发全部命中 `cred_75e8edcf`），所以只按凭证分桶、桶内继续排队并不能解决，必须同时允许桶内并发。改为 `Pacer(min, max, *, allow_concurrent=False)`：`allow_concurrent=True`（仅聊天 pacer）时按桶（渠道前缀 + 凭证身份摘要）维护**在途计数**——同桶已有在途请求则新请求**立即放行**，只有桶空闲、且距上次请求开始不足最小间隔时才补足等待（即只有「上一请求已结束、紧接着又来一个」的顺序连发才节流）。请求结束由 provider `stream_chat` 的 `finally` 调 `pacer.release(key)` 归还名额。**注意**：此处原写「async generator 被提前关闭时依赖 asyncio 的 asyncgen finalize，延迟归还只会让节流略松、不会误排队」——该结论在后来叠加 `max_concurrency`（见 Q48）后失效：`break` 不关闭 async generator，名额会推迟到 GC 才归还甚至永久丢失，桶停在满载使新请求在 `wait_turn` 无限阻塞（表现为「用了三次就限制」而非「并发三」）。现由 executor/continuation 在所有提前结束消费处显式 `aclose_stream`。`allow_concurrent=False`（后台任务 pacer）保持原严格串行语义不变。桶键用 `stable_key(provider, identity)`：CB 取 `account_uid or user_id or bearer_token`、TRAE 取 `uid or access_token`、zen 用渠道常量；`identity` 缺失回落该渠道单桶。CB/TRAE 仍共享同一 pacer 实例，但桶键带渠道前缀 + 身份摘要，彼此不互堵；`CODEBUDDY_CHAT_MIN_INTERVAL` 语义从「跨渠道全局间隔」变为「同渠道同凭证的顺序连发间隔」（默认 5s 不变）。无 schema 变更 |
| Q46 | Kilo Gateway 免费层（第四渠道 `kilo`） | 把 [Kilo Gateway](https://kilo.ai)（`api.kilo.ai/api/gateway`）免费层接成第四个 provider（`KNOWN_PROVIDERS` 加 `"kilo"`，**无 schema 变更**）。**协议是标准 OpenAI 兼容**（`/chat/completions` + `/models`），既无私有信封也无门禁伪装——与 Zen 的关键差异正在此：Zen 要伪造 UA/session/tools 并过滤伪工具调用，Kilo 完全不需要。**免费模型有权威标记**：`/models` 每个条目带 `isFree` 布尔（实测 2026-09-29 共 395 个模型、17 个 `isFree=true`，含 `kilo-auto/free`、`stealth/space-bunny-alpha`、`openrouter/free` 等无 `:free` 后缀者），据此**直接过滤**免费集——**不做探活**（与 Zen 相反）：探活会真发一次推理、白耗本就极小的免费配额（网关级约 200 req/h/IP），且结果随上游免费池波动不稳定，`isFree` 已足够权威。免费模型显式标 **x0 倍率**（`credit_rate=0.0`）；`name`/`context_length`/`top_provider.max_completion_tokens`/`supported_parameters`（含 `tools`）/`architecture.input_modalities`（含 `image`）透传为中立 `Model` 元数据。思考字段是 **`delta.reasoning`**（**不是** Zen 的 `reasoning_content`）。**凭证模型用虚拟凭证行**（同 Zen）：Kilo 无凭证/无额度接口，池里种一条空凭证复用现有调度/冷却/统计（`probe_quota` 恒 `probe_failed=True` → health NULL「未知」，**不是耗尽**）；删除后重启复活，也可在凭证页「登录渠道账号」点「添加 Kilo Gateway」立即补回，永久停用请用「暂停」。**错误分类**：401→`INVALID`（无凭证，401 只表示该模型需要付费 key/BYOK，避免强制付费模型硬禁用整条渠道）、400/404/422→`INVALID`、403→`REQUEST`、**429 与 502/503/504→`MODEL`（模型级冷却）**。429 与上游 5xx 归模型级而非账号级：实测（2026-09-30）429 报错点名具体模型（`<model> is temporarily rate-limited upstream`，`limit_source: upstream_provider_shared_pool`），429 消退后同一模型转 503 `no endpoints available`，两种情况下**同一时刻其他免费模型仍 200**——免费池实为 OpenRouter 共享池转发，拥塞/端点缺失按模型隔离，归账号级会因单模型问题把整条 kilo 渠道冷却（429→60s；5xx 累计 3 次→10m；单虚拟凭证下均即 `all credentials unavailable`）。对齐 CB/TRAE 的 `429+6004 → MODEL` 口径。限流交引擎处理，**本包不自建熔断**。新增 `KILO_API_ENDPOINT` / `KILO_ALLOWED_ENDPOINTS`（端点白名单，Kilo 不带真实 Token）/ `KILO_CHAT_MIN_INTERVAL`（热更项，独立 pacer、默认 0 = 不节流，与 zen / CB / TRAE 互不排队） |
| Q47 | Qoder（阿里，第五渠道 `qoder`） | 把 [Qoder](https://qoder.com) 接成第五个 provider（`KNOWN_PROVIDERS` 加 `"qoder"`，**无 schema 变更**）。**真实账号渠道**（区别于 zen/kilo 的匿名免费层），走设备码 PKCE 登录，凭证入加密列。协议为私有 COSY：推理 `POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation`，body 用**自定义 Base64 变体**编码（三段轮转 + 自定义字母表 + `=`→`$`），头为整套 `cosy-*`，`Authorization: Bearer COSY.<payload_b64>.<md5sig>`，`x-model-key` 路由；签名为 `md5(payload_b64 \n cosy_key \n date \n body \n path)`（`path` 去 `/algo` 前缀，payload 为键排序紧凑 JSON），`cosy_key`/`info` 由临时 AES 密钥经服务端 RSA 公钥加密而来。响应是**信封式 SSE**（`data:{"headers":…,"body":"<内层 chunk>","statusCodeValue":200}`，`body=="[DONE]"` 结束，非 200 判上游错误）。模型发现 `GET {gateway}/algo/api/v2/model/list?Encode=1`（**必须带整套 COSY 签名头**，签名 body 为 `qoder_encode("")`；裸 GET 403、带头 POST/PUT 400，故固定 GET）。额度 `GET {openapi}/api/v2/quota/usage`（`userQuota`+`addOnQuota`），套餐 `/api/v2/user/plan`。签到 `/sash/api/v1/me/daily-check-in/{status,claim}`（409/`ALREADY_CLAIMED` → 已签；**国际版该端点 404 → 视为本区域无此接口，不算错误**）。域：CN `openapi.qoder.com.cn`/`gateway.qoder.com.cn`；Intl `openapi.qoder.sh`/`api1.qoder.sh`。密码学复用项目已有 `cryptography`（不移植参考仓库的手写纯 Python 实现）。新增 `QODER_API_ENDPOINT`（openapi）/`QODER_ALLOWED_ENDPOINTS`（含国内 openapi+gateway 与国际版）/`QODER_CHAT_MIN_INTERVAL`（热更项，独立 pacer、默认 5s） |
| Q48 | CodeArts（华为云码道，第六渠道 `codearts`） | 把 [华为云 CodeArts](https://codearts.huaweicloud.com) 的盘古引擎接成第六个 provider（`KNOWN_PROVIDERS` 加 `"codearts"`，**无 schema 变更**）。**真实账号渠道**，走 OAuth2 PKCE 登录换 STS，凭证入加密列。推理 `POST /api/v2/chat/completions`（福利模型追加头 `maas_type: benefit`）；鉴权为华为云 **`SDK-HMAC-SHA256`**（AK/SK + `X-Security-Token`，signedHeaders=请求全部头小写排序，CanonicalURI 每段 encode 且**末尾补 `/`**，payload hash 取 `X-Sdk-Content-Sha256`）。**令牌刷新与 `client_id=codearts-agent` + DPoP 私钥三者绑定、一次性**：`POST {sts}/v1/oauth2/tokens` `grant_type=refresh_token` + **DPoP(ES256/P-256)**，刷后**必须回写新 `refresh_token`**（DPoP 低 S 归一化用 `cryptography` 实现，不移植 Go/手写 ECDSA）。模型：内置 `GET {snap}/v1/model/builtin`（头 `Agent-Type: PromptCenter`）+ 福利 `GET {opengw}/api/v1/gateway/config`；领取 `POST {opengw}/api/v1/benefit/claim`（幂等，启动/定时保活）；余额 `GET {opengw}/api/v1/user/tokens/balance`。SSE 为**逐行 `data:` JSON**（v2 实测为标准 OpenAI chunk：`choices[].delta` + `data:[DONE]`；旧形状/legacy 才是累计全文 `text`，用快照做差），错误 `error_code` 形如 `ChatAgent.*`。**CodeArts 没有每日签到接口**（额度为每日 1000 万免费 token、当日 0 点清零、用完即弃），故不实现 `checkin`，签到语义由 token 自动 refresh 续期承担（且只由 `RefreshTask` 独占轮转，一次性 `refresh_token` 不得被额度探测等旁路顺手消费）；当日剩余登记为「次日本地 0 点到期」的 `expiry_ladder`，调度器据此优先消耗（用尽自动回落）。**福利模型单请求扣池计费**：福利模型虽不给倍率（`credit_rate=None`），但实测按每日池 1:1 扣减（输入 32 + 输出 694 = 726 token → 池余额 −726），故上游 usage 缺额度字段时按「输入 + 输出 token」补 `credit` 并标 `credit_estimated`（统计页加 ≈；**2026-10-01 起该值再折成积分**，见 Q52）；内置模型不扣该池、不补。**并发上限**：上游硬限每账号并发会话数 3，超出即 `400 TM.00001041`，故 CodeArts pacer 在 `allow_concurrent` 之上加在途上限（`CODEARTS_MAX_CONCURRENCY`，热更、默认 3；`classify_status` 把带限流标记的 400/429 统一判成可重试的 `MODEL`）。后续实测确认该限制实为「每账号每约 60s 最多 3 个会话」（会话在流结束后仍滞留数十秒），纯在途上限挡不住突发，再叠加滑动窗口 `CODEARTS_REQUEST_WINDOW_SECONDS`（热更、默认 60），见 Q55。新增 `CODEARTS_API_ENDPOINT` / `CODEARTS_ALLOWED_ENDPOINTS`（snap 引擎 + STS + 福利网关 + 门户）/ `CODEARTS_CHAT_MIN_INTERVAL`（热更项，独立 pacer、默认 5s）/ `CODEARTS_MAX_CONCURRENCY`（热更项，默认 3） |
| Q49 | 模型列表按可读名合并 + 请求名与上游 id 分离（Qoder `display_name` 归一） | 首次引入「对外合并名 ≠ 上游原 id」：`/v1/models` 按可读名合并同一模型、对外 `id` 用可读名小写，转发时经 `services.model_aliases` 换回各渠道登记的原 id（`executor.upstream_model_name` 消费）；Qoder `parse_models` 把 `display_name` 透传为 `name`、`price_factor` 映射为 `credit_rate`。**2026-10-01 已被 §4.4.1「模型名三字段」取代**：合并键由「可读名小写」改为归一键，zen/kilo 不再排除，并新增对外 id 撞车消歧；`§4.4.1` 与 [TECHNICAL.md §3.5](TECHNICAL.md) 为准 |
| Q50 | Qoder 上游节点故障归类模型级瞬时（不误判模型不存在） | Qoder 会把**自身推理节点故障**也包成 400 返回：实测（2026-09-30）免费模型 `qfmodel`（展示名 Qwen3.8-Flash，`price_factor=0.0`）被路由到 `oa_qwen-plus-main` 节点后持续返回 `{"code":"400","message":"[FAIL]node:… msg:Execution failed: null"}`（HTTP 与信封 `statusCodeValue` 均为 400），同批其他模型正常出流——即上游节点挂了，不是请求/模型无效。原分类把 400 一律归 `INVALID`（换凭证没用、跳过该渠道），文案落成误导性的 `all credentials unavailable`。改为：`classify_error_code` 增加响应体判据——400/404/422 命中 `NODE_FAILURE_MARKERS = ("[FAIL]node:", "Execution failed")` 时归 **`ErrKind.MODEL`**（模型级瞬时冷却：只锁该 (凭证, 模型)，同渠道其他模型照常可用，自动路由暂避），否则维持 `INVALID`。安全前提：探测未知模型名（乱码/空串）上游均**不**返回 ERROR、真「模型不存在」也不带该标记，故识别不会误伤。配套 executor 文案：耗尽轮换且**所有候选都因该模型处于模型级冷却**时（`_all_model_cooled`），503 改说「model `x` temporarily unavailable on upstream（换模型即可用）」而不是「凭证不可用」；错误码仍 `no_healthy_credential`（前端已映射）。无 schema / 配置变更 |
| Q51 | 探测失败原因分类按基类分派 + 网络不可达单列（修 Qoder 误报 unknown_error） | 故障背景：Qoder 探测恒报 `unknown_error`，前端只显示「未知错误」，但实测根因是**网络层连不上**（`httpx.ConnectTimeout`，2026-10-01 本机 CN 端点 `openapi.qoder.com.cn` TLS 握手超时、`qoder.com.cn` 直连超时；国际版 `openapi.qoder.sh` 则 401 可达）。原 `describe_probe_failure` **逐渠道硬编码** `(CodeBuddyHTTPError, TraeHTTPError)` 与两家 `UpstreamProtocolViolation`，于是 Qoder/zen/kilo/codearts 的 `UpstreamHTTPError`（401/403/429/5xx）与协议违规全落 `unknown_error`；`httpx` 的 `ConnectTimeout`/`ConnectError` 也不是内置 `TimeoutError` 子类，同样落 `unknown_error`。改为**按基类分派**：所有渠道 `UpstreamHTTPError` 已继承 `base.UpstreamHTTPError`、`UpstreamProtocolViolation` 收归 `base`（各渠道 `events.py` 改为 `from provider.base import UpstreamProtocolViolation`，删除各自重复定义），故新增渠道自动被覆盖；另新增原因是 **`network_unreachable`**（`httpx.ConnectError`/`ConnectTimeout`/其余 `TransportError`/`OSError`）与既有 `upstream_timeout`（已连上但读超时，`httpx.TimeoutException`）区分——前者动作是「检查本机网络或代理」，后者是「重试」。Qoder `fetch_models` 主机全灭时也按异常类型区分：传输层失败抛 `base.UpstreamTransportError`（→`network_unreachable`），解析失败才是协议违规（→`upstream_response_invalid`），不再一律报「响应格式不符」误导用户查上游改版。`webapp/handlers.py` 的 4 个重复 `UpstreamProtocolViolation` 异常处理器收敛为 1 个。无 schema / 配置变更 |
| Q52 | CodeArts 额度单位由 token 折成积分 | 上游余额是每日免费 **token** 池（1000 万量级），直接展示既难看、也让「窗口内到期额度多者先用」拿它跟其它渠道的几百积分硬比（CodeArts 恒占优）。统一口径：**每日池满额 1000 万 token ≡ 1000 积分，1 积分 = 10000 token**，常量与换算只在 `src/provider/codearts/units.py`（`TOKENS_PER_CREDIT` / `tokens_to_credits`）。`parse_balance` 折 `remaining`/`total`/`expiry_ladder`，`_fill_estimated_credit` 折单请求 `credit`，保证额度、到期阶梯、单请求扣池同单位；前端 `quotaUnit()` 取消按渠道换词、统一「积分」（`display.QUOTA_UNIT`），CodeArts 的 `quotaSemantics` 文案随之改为「每日积分额度（当日 0 点清零）」。改动前已落库的**历史数据**（`credentials` 的额度/阶梯/额度包、`usage_events.credit`、`usage_hourly.credit_sum`、codearts 凭证的 `credit_events` 变动）由一次性脚本 `scripts/convert_codearts_credit_unit.py` 折算（默认预览、`--apply` 才写且先备份；除法不可逆，**只能跑一次**），新增模块 `src/provider/codearts/backfill.py` 承载折算逻辑。无 schema / 配置变更 |
| Q53 | 模型目录落盘快照 + 逐渠道增量 publish（修「启动窗口扁平名扇出」） | 实测故障（2026-10-01 12:03）：重启后请求 `stealth/space-bunny-alpha`（kilo 免费层唯一持有）却先打了 CodeBuddy/TRAE/CodeArts——日志 `codebuddy 400 11102 model [stealth/space-bunny-alpha] service info not found` / `trae 4001 param is invalid`。根因不在选号逻辑：模型 → 渠道归属表 `services.model_aliases` 只在 `list_models` **末尾**统一 publish，而启动预热里 zen 的 `fetch_models` 要逐个免费模型真发探活（12–15s），窗口期内 `executor._narrow_providers` 拿不到归属就按「全部渠道」保守放行。附带第二个洞：进程内缓存重启即丢，某渠道拉取失败时连兜底都没了（qoder/codearts 拉不通期间模型整体从 `/v1/models` 消失）。两处一并解决：**①** `list_models` 每拉完一条渠道就 `publish_aliases()`（从 `model_list_cache` 重建 + 就地更新，合并逻辑收敛到 `merged_entries()`，缓存兜底/TTL 复用/落盘恢复三条路径共用）；**②** 新模块 `src/api/model_catalog.py`：成功拉取后把**未过滤原始表**原子写 `DATA_DIR/model_catalog.json`（tmp + `os.replace`，`{"version":1,"providers":{pid:{"saved_at":…,"models":[…]}}}`，字段取 `dataclasses.fields(Model)`），启动时 `main._restore_model_list` **同步**读回并立即 publish（零上游请求，异常只记日志），预热退化为纯后台刷新；快照的 `saved_at` 随表交回并折进 `model_list_fetched_at`（快照不只是数据，还带新鲜度），因此预热只补「无快照 / 超 TTL」的渠道，不把刚恢复的表重拉一遍（kilo 实测一次 10–22s、zen 探活 12–15s），也无须再向渠道客户端注入回填钩子。纪律：落盘/读回一律宽容（原子写已排除半截写，故读回整份文件一个 `try`，解析不了就当没有缓存；`saved_at` 超 `MAX_AGE_SECONDS`=7 天整条丢弃；条目级只丢自己那个模型）；存原始表不过滤（`MODEL_BLOCKLIST` 热更要立即生效）；恢复只覆盖「已注册且当前有可用凭证」的渠道（用户暂停的渠道不因快照复活）。**不建表**（沿用被废弃的 `model_cache` 表教训：这份数据可丢、可重建，落 `DATA_DIR` 文件而非 schema），无配置项变更 |
| Q54 | 模型目录兜底刷新后台任务（`MODEL_CATALOG_MINUTES`，默认 30） | Q53 落盘快照解决了「重启那一刻别名表为空」，但刷新仍然**只由访问驱动**：`list_models` 只在有人调 `/v1/models` / Playground 时按 TTL（300s）跑。纯 API 用法的部署（客户端自己缓存了模型列表）会让归属表与快照一起变陈旧，三处会烂：① 上游新增模型时别名表无归属 → 扁平名请求按全部渠道扇出，各渠道回 11102/4001，并给每个凭证写 6 小时起步的 (凭证, 模型) 负缓存（`_note_upstream_error` → BLOCKED）；② 停机超 `MAX_AGE_SECONDS`（7 天）后快照被丢弃，退回 Q53 之前的行为；③ 模型下线后旧归属仍在（代价最小，有负缓存兜底）。故新增第 7 条后台循环 `model_catalog`，跑的就是同一条 `list_models`（TTL 门禁 + 逐渠道 publish + 落盘快照都复用，不另写一套）。三条设计约束：**① 注入而非新模块**——`tasks/` 不 import `api/`，故 `TaskRunner` 接 `Callable[[], Awaitable[object]] | None`，由 `main.lifespan` 闭包注入，`None` 时不装配也不展示卡片（与 growth / activity 同处理）；运行态只回报 `{"models": N}`，整份列表有几百条不能塞进管理台。**② 周期 30 分钟、下限 5**——与 zen 免费模型判活缓存 `MODELS_CACHE_TTL_SECONDS`（1800s）对齐，再密也不会让 zen 多探一次，只是白打其余渠道的 `/models`。**③ `list_models` 整段加模块级 `asyncio.Lock`**——HTTP 出口与后台循环并发时不串行会对同一条渠道重复打上游（zen 那次是十几秒真推理）；锁粒度取「整次刷新」而非单渠道，因为跨渠道合并与别名表 publish 需要一致全集。新增热更项 `model_catalog_minutes`（下限 5）+ compose 透传；无 schema 变更 |
| Q55 | CodeArts 节流窗口对齐上游会话口径（`CODEARTS_REQUEST_WINDOW_SECONDS`） | Q48 的 `max_concurrency`（在途上限）+ Q48b/名额泄漏修复后，实测**仍偶发** `400 TM.00001041`。受控实验（单账号、真实上游）给出上游真实口径：① 3 并发结束后名额已全部归还，**紧接着**再发 3 个全部 400；② 打满 3 并发后每 6s 探单请求，约 **68s** 才恢复；③ **完全顺序、零并发**、每请求换全新 `httpx` client 并显式关闭，仍前 3 OK、第 4 起全 400；④ 间隔 30s 顺序 6 发则 6/6 OK。结论：上游限制的不是「同时在途 HTTP 数」而是「**每账号每约 60s 最多 3 个会话**」，会话在 HTTP 流结束后仍滞留数十秒才释放，故 `release` 一让位、新请求立刻再击穿——`max_concurrency` 只对齐了「在途」这一半。**修复**：`Pacer` 增 `window_seconds`（构造：`window_seconds: float | Callable = 0`，热更取值器；`_windowed()` 要求 `allow_concurrent` + `max_concurrency>0` + `window_seconds>0` 三者齐备）。窗口模式下 `wait_turn` 按桶维护 `_starts`（升序启动时刻），先 `_prune_starts` 丢掉滑出窗口的，再判「窗口内启动数 ≥ 上限」：满则睡 `starts[0] + window_seconds - now`（最早一次滑出即醒）重查；不满则登记 `now` 后照常 `_reserve`。`release` **只减在途计数**、不再让出窗口配额（否则退回纯在途口径）。取消路径回滚在途名额并抹掉刚登记、未真正发起的启动时刻（防白占窗口）；窗口登记在睡眠期间被别的等待者 `_prune` 掉后再取消，抹除操作幂等、不动他人登记。`max_concurrency=0` 或 `window_seconds=0` 时退回原行为（向后兼容，两者都可热更）。CodeArts pacer 装配 `window_seconds=lambda: runtime.codearts_request_window_seconds`（默认 60 = 实测恢复区间 60.7–68.6s 的下沿，略偏激进）。窗口准入**同时**受窗口与在途两个闸门约束：窗口满则等到最早一次启动滑出，仅因在途满（长流超过窗口）则等 `release` 唤醒（否则长响应会绕过在途上限再超发）；取消路径回滚在途名额并抹除未真正发起的窗口登记。取 60s 的代价：高频使用时账号吞吐降到约 3 次/分钟，超出部分**排队**而非报错（不再触发 60s 模型级冷却）。新增热更项 `codearts_request_window_seconds`（下限 0）+ compose 透传；无 schema 变更 |
| Q56 | 模型列表 HTTP 出口 stale-while-revalidate（修 Playground「卡在载入模型中」） | 实测故障（2026-10-04）：`GET /api/playground/models` 缓存命中 0.015s，但 TTL（300s）到期后 **18.9s**——`list_models` 在请求路径上同步串行重拉全部渠道，逐渠道合计 30–31s（zen 免费模型逐个探活占 23s）。修复：HTTP 出口（`/v1/models` 与 `/api/playground/models`）改走新的 `serve_models`——**有缓存就立即回旧列表**，过期渠道丢后台任务（`_schedule_refresh`）异步刷新；只有某渠道**一条缓存都没有**时（冷启动无落盘快照 / 新接入渠道）才同步等它一次，否则列表会缺该渠道模型。后台刷新任务收敛在 `Services.model_refresh_tasks` / `pending_model_refreshes`，`model_refreshing` 去重（同一渠道不在途才排），`lifespan` 关闭时逐个 `cancel()` + `await`，不把 in-flight 上游请求带出事件循环。后台预热（`_warm_model_list`）与兜底循环（Q54）仍走同步 `list_models`（它们要的是「真的刷新」，且本就在后台）。代价：入口列表最多滞后一个 TTL；实测修复后 Playground 稳态请求 13–22ms，且后台刷新完成后落盘快照按时更新（03:32:24 请求 21ms 返回旧表 → 03:32:32 快照被后台重写）。无 schema / 配置变更 |
| Q57 | 归一规则新增 `new` 新版标记（模型名去 `-new` 后缀） | 实测数据（2026-10-05 落盘目录）里 kilo 有一档展示名带新版标记：`inclusionAI: Ling 3.1 Flash (new)`，清洗后归一键带尾缀 `ling-3.1-flash-new`。`new` 与 `free` 同性质——**新旧标记**、不是模型身份的一部分；不削则带标记与不带标记的两种写法互相对不上，同一模型在跨渠道合并时被拆成两条（两个对外 id，用户得选对才路由得通）。并入 `provider/naming.py` 现有的噪声标记体系：把 `FREE_MARK` + 新增的 `NEW_MARK` 合成 `NOISE_MARKS`，路径段过滤、尾部正则 `_TRAILING_MARK`、分段整词过滤 `_is_noise_segment` 全部改为按集合判定（**只删整段 / 整词**，`freeplay` / `gpt-newest` / `newson` 这类内嵌形式不动，与 free 口径一致）。`_strip_noise` 的尾部削改**循环**执行：标记可叠加（`Vendor: Model (new) (free)`），单次 `re.sub` 只去最外层一个，残留的会留在归一键里。归一键与展示名**都**去 `new`（上游打这个标记只是告诉用户「这是新版」，去掉不丢信息；反之保留会让新旧两条共存）。只影响展示与对外 id，`raw_id` 与别名表不动，用户仍可按原始 id 直连。无 schema / 配置变更 |
| Q58 | 竞品对比与可迁移功能分档（P0） | 与 workbuddy-openai-proxy 逐项对比后落档 `docs/competitor-comparison.md`（该目录 gitignore，本地存档），按「价值 / 契合现有架构 / 迁移成本」分档。**P0 三项**（本轮落地）：① Anthropic `/v1/messages` 出口；② chat 侧上下文压缩；③ API Key 模型白名单 + 到期时间。**P1 五项**（后续按需）：模型能力元数据、跨渠道 fallback、按渠道代理、告警、Playground 增强。**P2 不迁**：自更新、多语言、服务端工具执行、Web Search 注入、TLS 指纹绕过、零依赖形态、CC Switch 导入 |
| Q59 | Anthropic `/v1/messages` 出口（P0-1） | 复用同一 `executor`（选号 / 冷却 / 轮换 / 统计 / 粘性不动），新增 `compat/anthropic/{request,response}.py` + `api/messages.py`；提供 `POST /v1/messages`（流式 + 非流式）与 `POST /v1/messages/count_tokens`（本地估算，不转发上游）。入站把 Anthropic 请求映射成 `ChatRequest`（`system` 注入为 `system` 消息、`tool_result` → `tool`、`tool_use` → `tool_calls`、`input_schema` → `parameters`、`tool_choice.any` → `required`）；出口 `AnthropicStreamTranslator` 产出 `message_start` → `content_block_start/delta/stop` → `message_delta` → `message_stop`（Anthropic 无 `[DONE]` 哨兵，`message_stop` 即结束；thinking 块 stop 前补占位 `signature_delta`）。鉴权 `deps.api_key_user_anthropic` 先读 `x-api-key`（`ANTHROPIC_API_KEY`）再回落 `Authorization: Bearer`（`ANTHROPIC_AUTH_TOKEN`）。**不支持**图片/文档块与服务端工具，显式 400。无 schema 变更 |
| Q60 | 上下文压缩（P0-2） | 按模型目录里的输入上限裁剪过长对话，避免撞上游硬限制（CodeBuddy `11115 prompt is too long`）。动机来源见 `docs/competitor-comparison.md`。实现为纯函数 `engine/compress.py` + API 层接线 `api/context.py`（在 chat / responses / messages / playground 四个入站处调用）。**只做确定性「估算 → 裁剪」**，不做「超限后压缩再重试」的放大路径：token 估算沿用实测口径（中文 0.55/字、数字 0.33、其他 0.25，「3 字符 ≈ 1 token」的英文口径会把中文低估约 1.6 倍）；预算法 `模型上限 × safety_ratio − reserve_for_output`；超限时 `system` 永久保留、`assistant.tool_calls` 与其 `tool` 结果**同组同生共死**（只删一半会让上游报 tool_call_id 找不到）、至少保留最近 `min_keep_messages` 条、其外从最新往最老贪心回填；裁剪后仍超限则截断最长的那条非 system 消息（留头尾 + 标记）。**目录里查不到输入上限的模型不压缩**（宁可不裁剪也不猜一个数字去砍上下文），故对未知模型零副作用。新模块 `api/context.py` 的 `context_window_for` 同名多渠道取**最小**上限（调度可能落到任一候选渠道）。热更项 `context_compress_enabled`（默认 true）/`context_compress_reserve_tokens`（4096）/`context_compress_min_keep_messages`（4）/`context_compress_safety_ratio`（0.95），compose 透传；无 schema 变更 |
| Q61 | API Key 模型白名单 + 到期时间（P0-3） | `api_keys` 增 `allowed_models TEXT NOT NULL DEFAULT ''`（fnmatch glob，逗号分隔，`''`=不限制）与 `expires_at INTEGER`（epoch 秒，`NULL`=永不过期），`SCHEMA_VERSION` 15→16，`_MIGRATION_COLUMNS` 幂等补列（老库补列后行为不变）。策略是纯函数（`auth/access.py`：`normalize_allowed_models` / `model_allowed`）；`ApiKeyPrincipal` 增 `allowed_models`，`deps._api_key_principal` 在鉴权当场判过期（过期与「Key 不存在」统一 401 文案，不泄露可枚举信息），三个 /v1 出口（chat / responses / messages）在解析后校验白名单（省略模型名时按 `default_model` 判定，不能借空名绕过），`/v1/models` 也按白名单过滤展示。匹配大小写不敏感，`模型@渠道` 的后缀不参与匹配。**不做**每 Key 配额（与 Q37 结论一致）。管理台「创建 API Key」对话框已暴露模型白名单与到期时间字段（`web/src/pages/ApiKeysPage.tsx`） |
| Q62 | 跨渠道 fallback 兼容组（P1-5） | 主渠道全不可用时按「兼容组」回退到同义模型。动机来源见 `docs/competitor-comparison.md`（P1）。配置热更项 `MODEL_FALLBACK_GROUPS`（默认 `""` 关闭），格式 `组名=成员1,成员2;组名2=成员3`。纯函数 `model_resolver.parse_fallback_groups`（宽容解析，坏段跳过、重名后者覆盖）与 `ordered_fallback_chain`（组名只是**入口别名、不进链**；请求成员时该成员置首、组内其余成员按配置顺序跟随；大小写不敏感）。执行层 `Executor._fallback_chain` 把组解析成 `ModelTarget` 链：`@渠道` 与 API Key 渠道绑定（`target.forced`）**不参与回退**（用户已把渠道钉死）；目录可用时剔除不在任何候选渠道登记的回退成员（「兼容组白名单」，避免给不存在的模型白打上游），目录未就绪则全部放行交给执行层兜底候选。`complete` 逐链项调 `_complete_model`（原逻辑整体下移），捕获 `NoHealthyCredential` / `InvalidRequest` 继续下一项，整链耗尽以最后一项错误抛出。流式 `stream` 逐链项跑 `_stream_loop`：**仅在尚未产出任何响应帧前**允许切换（已出帧后换模型会让客户端看到两个模型的混合输出），用私有信号 `_ModelExhausted` 在链内传递「本项未出帧即用尽」，整链耗尽才用最后链项的 `translator` 产出终帧；注入的 translator（responses / anthropic）跨链项复用同一实例。`preflight` 按整条链判断是否有注册上游。无 schema 变更，compose 透传 |
| Q63 | 运维告警（P1-7） | 后台周期评估四类风险，命中落库 + 可选 webhook（用户选定「Webhook + 站内」）。**规则**（纯函数 `tasks/alerting.evaluate_alerts`，阈值 ≤0 即关闭该规则）：① 池耗尽 `pool_empty`（`total>0` 且 `ready < ALERT_POOL_READY_MIN`，severity critical——服务活着但用不了，`/health` 探针看不出来）；② 任务连续失败 `task_failed`（`TaskStatusStore` 新增连续失败计数，成功一轮清零，`failing(threshold)` 只列真跑过且达阈值的 key，避免「从未运行」误报）；③ token 临近到期 `token_expiring`（`CredentialRepository.expiring_tokens` 收 `(now, now+窗口]`，`token_expires_at` 列 NULL 时从密文按需派生，0=未知不报，硬禁用凭证排除）；④ 上游错误率 `error_rate`（`StatsCollector.window_error_rate` 读 `usage_events` 明细而非小时汇总——告警窗是分钟级，小时粒度要么整点才更新、要么跨小时口径错乱；样本数 ≥ `ALERT_ERROR_RATE_MIN_REQUESTS` 才判）。**投递**：每条命中落 `alert_events`（`AlertRepository.record/recent/last_ts/prune`，`SCHEMA_VERSION` 16→17，新表只进 schema.sql 无需迁移动作）供管理台「运维告警」页（admin-only `GET /api/alerts`）回看；配置 `ALERT_WEBHOOK_URL` 时逐地址 POST JSON，多个逗号分隔、全部成功才算 delivered。**静默去重**：同一 `(rule, scope)` 在 `ALERT_SILENCE_MINUTES` 窗内只落库/推送一次（`last_ts` 判），窗口过后仍命中再报一次——否则池耗尽这类持续状态会每轮刷屏。**隔离**：webhook 失败只记 `delivery_error`、绝不抛错（否则「webhook 挂了」会被误报成「告警任务连续失败」制造假信号）。**为什么落库**（与进程内的 `TaskStatusStore` 不同）：告警的价值在「错过的那段时间发生了什么」，夜里池空/任务连挂，运维醒来要能看见；保留期由 `RetentionTask` 按明细同一策略（90 天）清理。**不迁**：告警恢复（resolved）事件、告警确认/静默操作（记录只读）。10 个热更项全部归到「运维告警」任务卡片（`task="alert"`），compose 透传；新增 `AlertTask` 接入 `TaskRunner`，与运行态共享同一 `TaskStatusStore`（自建 store 会让「任务连续失败」规则永远读到 0） |
| Q64 | 按渠道出站代理（P1-6） | `PROVIDER_PROXIES`（启动期项，默认 `""` 直连，行为不变）为每个渠道单独指定出站代理：格式 `渠道=代理URL;渠道2=代理URL2`，协议 `http/https/socks5/socks5h`（SOCKS 由 `httpx[socks]` 提供）。**作用范围**：该渠道**全部**出站请求——聊天流、额度/模型拉取、后台任务（签到/成长/刷新/活跃上报，经共享 `_short` 客户端）与 OAuth 登录（`CodeBuddyOAuth`/`QoderOAuth`/`CodeArtsOAuth` 也收 `proxy`）。**注入点**：各 provider client 增 `proxy` 参数，惰性构造 `httpx.AsyncClient` 时经 `provider/proxy.build_client` 传入 `proxy=`；`main.build_app` 与 `_upstream_auth` 在装配处按渠道取值注入。**为什么启动期而非热更**：代理作用于连接池，运行中改值需重建在途连接池（涉及 6 个客户端 + 3 个 OAuth 流），风险与测试量都大；与上游端点同属启动期传输层配置。**为什么严格解析**（未知渠道/非法协议/缺 `=` 一律 `ValueError` 启动失败，只容忍空段）：代理常带合规/隐私意图，「以为走了代理其实直连」比启动报错更糟；渠道集合由调用方传入（`KNOWN_PROVIDERS`），不硬编码。**为什么 `trust_env=False`**：既有约定，不吃 `HTTP_PROXY` 等环境变量，避免部署环境全局代理意外劫持带 Token 的上游请求；按渠道代理只经 `proxy=` 显式生效。无 schema 变更，compose 透传；新增 `tests/test_provider_proxy.py`（30 例） |
| Q65 | 同模型异名显式规范（`MODEL_SYNONYMS`） | 归并键 = 展示名归一键，但上游未必给同一模型同一个名字：Qoder 的 `dfmodel` 上游名只叫 `DeepSeek-Flash`（不带版本号），归一后是 `deepseek-flash`，与 CodeBuddy/TRAE/CodeArts 的 `DeepSeek-V4.1-Flash`（`deepseek-v4.1-flash`）对不上，同一 DeepSeek V4.1 Flash 被拆成两条、用户得选对渠道才路由得通。清洗规则（去 free/new、去厂商前缀）对这类「缺版本号」无能为力，故引入**显式身份规范表** `provider/naming.MODEL_SYNONYMS`（异名归一键 → (规范归一键, 规范展示名)），`normalize_model_key` 与 `display_model_name` **都**过它——键与展示名一起收敛（只收键的话合并了、展示名仍是旧名，前端两个名字不一致），合并在 `api/models._merge_key` 层自然发生，无需改合并逻辑。**只登记人工核实的同模型异名，不做模糊匹配**：`deepseek-flash` 只收敛到 `deepseek-v4.1-flash`，不碰真正的 `deepseek-v4-flash`；Qoder 原代号 `dfmodel` 不变，仍进 `by_provider.qoder.raw_id`，请求 `deepseek-v4.1-flash` 经别名表换回 `dfmodel`。无 schema 变更 |
| Q66 | 调度「免费优先」档（修 space-bunny 落到 CodeBuddy 烧积分） | 实测故障（2026-10-08）：请求 `space-bunny` 被调度到 CodeBuddy（x0.08）而不是 zen 免费（x0）。根因是两层叠加：① 归一合并后 zen 的 `space-bunny-free` 与 CodeBuddy 的 `space-bunny` 并成一条（providers = {codebuddy, zen}），归属收窄没错；② 调度排序「36h 到期积分多者先用」——CodeBuddy 凭证窗口内恒有几百积分到期（每日 100 积分包），zen 无到期阶梯恒 0 分，付费渠道**永远**赢过免费渠道，免费优先无从谈起。修复：**排序链最前加「免费优先」二元档**——`Candidate` 增 `credit_rate` 字段（executor 从模型目录现查当前模型的倍率、按渠道注入候选快照），候选池中存在 `credit_rate == 0` 的渠道时免费渠道整体排到所有付费渠道之前，之后才轮到 pin → 到期积分 → 健康度 → 余额。设计取舍：**不做「按现价折算再比」的软比较**（折算系数是拍的、随倍率漂移，二元档语义清晰可测）；代价是付费凭证 36h 内到期积分要给免费渠道让路，但免费渠道跑一条中等回复就省几十积分、长会话累计破百，让路是值的。**数据流**：`model_list_cache`（目录原始表）+ `model_aliases`（归一键 → 渠道原 id）→ `Executor._model_rates`（对外 id → 各渠道 credit_rate）→ `_selectable` 注入 `Candidate.credit_rate` → `Scheduler._free_first_rank`。**降级口径**：目录未就绪 / 该渠道模型表缺该模型 / 上游未给倍率（CodeArts 福利模型）→ credit_rate=None，**不算免费**、与付费同档（宁可不优先也不误判；None 与 0.0 严格区分）；候选池无免费渠道时该档恒 0，排序链与旧版逐字节一致（向后兼容，老测试零破坏）；会话粘性 / pin 在免费档之**前**（`_sticky` 先于 `select`，用户显式意图不被覆盖）。`ExecutorDeps` 增 `model_list_cache`（与 `Services.model_list_cache` **同一 dict 引用**，`main.build_app` 装配时先建共享空 dict 分别传入，目录恢复 / TTL 刷新就地更新后聊天路径立即可见）；`Services` 字段注释同步。无 schema / 配置变更 |

## 2. 目标与非目标

### 目标

- 单一 OpenAI 兼容端点，后面挂 CodeBuddy、TRAE、OpenCode Zen、Kilo Gateway、Qoder 与 CodeArts 六个上游
- 凭证由 admin 集中维护，全员共享，调度器自动挑健康的号
- 按人统计用量（请求数、成功率、token、耗时与首字延迟）
- 上游死亡自动冷却，不反复踩死号
- 支持 `model@provider` 精确指定上游

### 非目标（明确不做）

- **不做配额/限流**：上游是订阅制通道，成本不随 token 线性增长；10 人规模靠统计页可见性约束滥用
- **不做通用 provider 网关**：只支持 CodeBuddy / TRAE / OpenCode Zen / Kilo Gateway / Qoder / CodeArts 这几个明确接入的上游，硬编码，不做插件系统
- **v1 不做 Anthropic 协议**
- **不做旧项目数据迁移**
- **不做自更新脚本**
- **不做货币/积分换算**：上游的积分单位不互通，分开记录

## 3. 关键事实（已核实）

### 3.1 CodeBuddy（腾讯）

- 端点：`https://copilot.tencent.com`（国际站 `https://www.codebuddy.ai`）
- 聊天：`POST /v2/chat/completions`，**只支持流式**，非流式需本地聚合
- 认证：`POST /v2/plugin/auth/state?platform=CLI` → 拿 `authUrl`/`state` → 轮询 `POST /v2/plugin/auth/token?state=...`（设备码模式）
- 账号切换：`/v2/plugin/login/account`、`/v2/plugin/accounts`
- 额度：个人版 `POST /v2/billing/meter/get-user-resource`（`CycleCapacity*Precise`），企业版 `POST /v2/billing/meter/get-enterprise-user-usage`（`credit` 已用、`limitNum` 总额）
- 签到：`POST /billing/meter/daily-checkin`；状态：`POST /billing/meter/checkin-activity-status`（连续天数 / 今日是否已签）
- **成长中心**（逆向自 WorkBuddy 桌面端，前缀 `/v2/activity/growth`）：只读 `buddy/travel/status`、`buddy/travel/config`、`tasks`、`streak`、`redeem/summary`、`lottery/chances`、`buddy/quota`、`energy`；写入 `buddy/travel/claim`、`buddy/travel/depart`、`tasks/accept`、`/tasks/{code}/claim`、`makeup-cards/use`、`redeem`、`lottery/draw`、`buddy/open`。契约为 `accept_status` 五态、`/tasks/accept` 收复数数组 `{"task_codes": [...]}`（单数一律 400）、`/redeem` 的 `tier` 是档位标识（`"7d"/"14d"/"28d"`）、实发字段 `*_granted`（细节与坑见 [TECHNICAL.md §6.2](TECHNICAL.md)）
- 鉴权头与聊天一致（`Authorization` + `X-User-Id` + `X-Domain`）；实测可用项目内的 OAuth bearer 凭证直连成长中心，无需桌面端凭据文件
- **活跃度驱动来源**（实测）：活动类操作（领取成长中心奖励等）**计入**（两天无桌面端使用、仅差别在有无领取动作，`score` 从 0 变 5），纯 `/v2/chat/completions` 对话**不计入**（3 次完整对话后 `today.score` 与 `updated_at` 均不动）。连登天数含 1 天容忍窗口，每月清零；热力墙按 score 分 5 档，每日 02:00 批算。**与积分无关，不参与调度决策**（数据见 [TECHNICAL.md §6.2](TECHNICAL.md)）
- **活跃上报（B1.7，默认关闭）**：`POST /v2/report`，body 为事件数组（`eventCode=chat_request_send`），`userId` 必填——缺失时上游 HTTP 200 `code:0` 但静默丢弃。OAuth 凭证 `account_uid`/`user_id` 实测为空，回落 bearer JWT 的 `sub`；实测一条即点亮连登（1→2）。风险与开关语义见 [README.md](README.md)（条款明禁脚本篡改；事件形状改版即失效，不作为可靠性功能）
- **凭证身份可能为空**：OAuth 路径下上游账号接口未回填 `account_uid`/`user_id`（实测），签到 / 成长中心的同账号隔离必须回落到 `credential_id`，否则第二个账号会被静默跳过
- 请求头需 `X-Domain`、`X-User-Id`、`X-Enterprise-Id`、`X-Department-Info`（部门名须 UTF-8 百分号编码）
- **reasoning 字段客户端给什么就透传什么，`reasoning_content` 不剥离**（实测 71 份真实 dump）：客户端自带 `reasoning_effort`（69/71，仅 `low`/`medium`，非推理模型如 `hy3` 不带）并在历史 assistant 消息里回传 `reasoning_content`（51/71，含带 `tool_calls` 的消息），上游原样接受（`deepseek-v4.1-flash` 4260 次请求 99.6% 成功）。故无需「effort 档位映射」，也不存在「客户端丢弃 reasoning_content」的前提
- **`reasoning_effort` 缺失时补 `medium`，显式值不覆盖**（2026-10-09）：不发该字段时上游退化成「可见推演」——整段思考写进 `delta.content`、`delta.reasoning_content` 恒空、`usage` 的 `reasoning_tokens` 恒 0（同一 prompt 带 `low` 立即恢复独立思考通道，实测思考 6594 tokens 全在 `reasoning_content`）。官方 CLI 从不发这种形态（69/71 份 dump 都带），但非官方 CLI 客户端（如 DSH / pi-ai）不发，故补缺省。`enable_thinking` 同样只在未给时补 `true`。**只补缺省、不改写客户端意图**——参考实现 IceeAn/codebuddy2api 走「白名单模型强制 `max`」，那是覆盖客户端值，不采纳。客户端可显式传 `reasoning_effort: "low"` 控制思考量（难题上 `medium` 单次实测 6594 思考 tokens，总成本与此前「可见推演」大体相当——此前也计入输出 token）
- **CB 的 `reasoning_tokens` 在 `completion_tokens_details` 里，顶层恒缺**（2026-10-09 修正）：早先只读顶层 `reasoning_tokens`，实测恒为 0，据此误判为「CB 不回思考 token、属上游口径差异」。补上 details 优先的解析链后统计页 CB 思考 token 恢复真值。TRAE 侧在顶层正常回（`qwen-3.7-plus` 单请求 6~114）
- **输出上限键名不对称**（2026-09-21 直连实测）：CB 上游**完全忽略 `max_completion_tokens`**（`=1` 仍出 59 tokens），只认 `max_tokens`（精确截断 + `finish_reason=length`），两键同发时后者胜出；TRAE 对两个键**都不生效**。本网关不做键映射，客户端限额原样透传——若客户端只发 `max_completion_tokens`，输出不会被截断；`enable_thinking: false` 亦被上游忽略（细节见 [TECHNICAL.md §3.4](TECHNICAL.md)）

### 3.2 TRAE SOLO（字节）

- Agent Host `https://trae-api-cn.mchost.guru`、UG Host `https://api.trae.cn`、OAuth Host `https://api.trae.com.cn`
- 聊天：`POST /api/agent/v3/llm_utils_chat`；模型：`POST /api/ide/v1/get_detail_param`
- 认证：浏览器登录 → 302 回调 `/authorize` → `ExchangeToken` → `GetUserInfo`；刷新 `POST /cloudide/api/v3/trae/oauth/ExchangeToken`（refreshToken 轮换）
- 签到：`/trae/api/v2/ug/checkin_credits/{status,claim}`；额度：`/trae/api/v2/pay/ide_user_ent_usage`
- **签到成功必须「确认到账」，不能只看返回码**：`claim` 对当天已签过的账号也返回 `code:0 success`（幂等），此时 `status.credits` 前后不变、`checked_in` 已是 true。用「claim 返回 0」判断会把「什么都没发生」报成成功（本项目曾据此返工）。正确判定：`checked_in` 为真且回查 `credits` 确有增加；CB 侧同规矩（`code=0` 且 `credit` 是有限数值）
- **签到 9074 按设备标识处理**：数字串是必要条件、非充分条件（同账号 hex32 与确定性派生值失败、随机新数字串成功）；`X-Device-Id` 空串返回 9004。本项目每次 claim 生成新的 16 位数字串，一轮内最多换号重试 2 次（`CHECKIN_ATTEMPTS`），其余交给 10 分钟周期。**注意**：某账号当天签到成功后，任何 device_id 的 claim 都会返回 `code:0`（幂等），所以必须看 `status.checked_in` 判断成功
- SSE 事件序列：`metadata` → `timing_cost` → `output`×N → `extra_info` → `token_usage` → `done`
- `token_usage` 含缓存字段 `cache_read_input_tokens` / `cache_creation_input_tokens`（未命中为 0，非缺失），映射为统计的 `cached_tokens`；**无 per-request credit**
- 错误码 `1005` = 权益不足；仅流式，非流式需聚合
- **接受客户端传来的 `reasoning_effort`**（实测透传 `low`/`medium` 均 200 且正常出流）：不认 `thinking` 对象，也无需服务端注入；`developer` 角色上游不认（静默空流），已归一为 `system`

### 3.3 Qoder（阿里，第五渠道 `qoder`）

- 域：国内 `openapi.qoder.com.cn` / 网关 `gateway.qoder.com.cn`；国际 `openapi.qoder.sh` / `api1.qoder.sh`（回落 api2/api3）
- 聊天：`POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation`，**只支持流式**（信封 SSE），非流式需本地聚合
- 登录：**设备码 PKCE（S256）** —— `{website}/device/selectAccounts` 生成 challenge/nonce → 轮询 `GET {openapi}/api/v1/deviceToken/poll`（404/202 = 待授权）→ `POST /api/v1/deviceToken/refresh` 续期 → `GET /api/v1/userinfo` 补 `uid`/昵称/组织
- 模型：`GET {gateway}/algo/api/v2/model/list?Encode=1`，**必须带整套 COSY 签名头**（签名 body 为 `qoder_encode("")`）；不带头的裸 GET 会 403，带头用 POST/PUT 会被上游 400「Request method ... not supported」拒绝，故方法固定 GET
- 额度：`GET {openapi}/api/v2/quota/usage`（`userQuota` + `addOnQuota`）；套餐 `GET /api/v2/user/plan`
- 签到（2026-10 起为**活动制**）：`GET /sash/api/v1/me/campaigns` → 筛 `actionType=="CLAIM_BENEFIT"` 且 `claimStatus=="CLAIMABLE"` → `POST /sash/api/v1/me/campaigns/{campaignId}/claim`。**两个请求都必须带 `Cosy-ClientType: 10`**（缺则上游静默返回空 campaign 列表）；claim 的 `replayed:true` 与 `BLOCKED`+`SAME_PERSON_ALREADY_CLAIMED` 归一为当日已签。旧 `/sash/api/v1/me/daily-check-in/{status,claim}` 仅作回退（活动制接口 404/405/410 时），旧 `409`/`ALREADY_CLAIMED` = 当日已签（幂等，不是错误）；**当前旧 status 返回 `DISABLED`**。新协议不提供连续天数（`streak_days=None`）。活动制与旧接口都不可用（国际版端点 404）→ 视为本区域无此接口，`checkin` 归为 skipped 而非 failed
- 签名：自定义 Base64 变体（三段轮转 + 自定义字母表 + `=`→`$`）；`Authorization: Bearer COSY.<payload_b64>.<md5sig>`；整套 `cosy-*` 请求头（含稳定派生的 `cosy-machineid`/`cosy-machinetoken`）

### 3.4 CodeArts（华为云码道，第六渠道 `codearts`）

- 域：snap 引擎 `snap-access.cn-north-4.myhuaweicloud.com`；STS `sts.cn-north-4.myhuaweicloud.com`；福利网关 `opengw.developer.huaweicloud.com`；门户 `codearts.huaweicloud.com`
- 聊天：`POST /api/v2/chat/completions`（福利模型追加头 `maas_type: benefit`），SSE 逐行 `data:` JSON，v2 为标准 OpenAI chunk（`choices[].delta` + `data:[DONE]`），旧形状为累计全文（替换语义）
- 登录：OAuth2 PKCE → `POST {snap-manager}/v1/oauth2/tokens`（authorization_code）换 AK/SK/security_token/refresh_token
- 刷新：`POST {sts}/v1/oauth2/tokens`（`grant_type=refresh_token` + **DPoP ES256/P-256**）；**refresh_token 与 `client_id=codearts-agent` + DPoP 私钥三者绑定、一次性、刷后必须回写**
- 鉴权：华为云 **`SDK-HMAC-SHA256`**（`Authorization: SDK-HMAC-SHA256 Access=<AK>, SignedHeaders=…, Signature=…`）
- 模型：内置 `GET {snap}/v1/model/builtin`（头 `Agent-Type: PromptCenter`）；福利 `GET {opengw}/api/v1/gateway/config`；领取 `POST /api/v1/benefit/claim`（幂等）；余额 `GET /api/v1/user/tokens/balance`
- **无每日签到接口**（额度为每日 1000 万免费 token、当日 0 点清零、不累计）：不实现 `checkin`，保活语义由 token 自动 refresh 承担。**当日剩余登记为到期点＝次日 0 点的 `expiry_ladder`**（与 CodeBuddy/TRAE 同构），调度器「窗口内到期额度多者先用」据此优先消耗该池，用尽后自动回落其它渠道
- **单位折算（token → 积分，2026-10-01）**：上游按 token 计量、量级达千万，直接展示既难看、也让跨渠道的「到期额度多者先用」拿它跟几百积分硬比。统一口径：**每日池满额 1000 万 token ≡ 1000 积分，1 积分 = 10000 token**（常量与换算只在 `src/provider/codearts/units.py`）。`parse_balance` 折余额/额度/到期阶梯，`_fill_estimated_credit` 折单请求扣池，保证同单位；改动前已落库的历史数据（凭证额度、`usage_events.credit`、`usage_hourly.credit_sum`、`credit_events`）由 `scripts/convert_codearts_credit_unit.py` 一次性折算（默认预览、`--apply` 才写并先备份）
- **福利模型单请求扣池计费**：福利模型不给倍率，但消耗每日池——上游 usage 只给 token 数，按实测 1:1 扣池口径把 `credit` 补为「输入 + 输出 token 折成的积分」（1 积分 = 10000 token）并标 `credit_estimated`（统计页加 ≈）。只补福利模型：内置模型不扣这条每日池

### 3.5 冲突与陷阱

| 问题 | 事实 | 对策 |
|---|---|---|
| 模型 ID 撞车 | 六边都有 `glm-5.2`、`DeepSeek-V4-Pro`、`kimi-k3` 等 | 扁平名 + 归一键合并 + 健康度路由 + `@provider` 后缀 |
| 积分语义不同 | CB/Qoder/CodeArts 有周期会重置；TRAE 是单调余额 | 健康分统一为百分比，展示层标注周期语义 |
| credit 可得性 | CB 有 per-request；TRAE 只有账户总额；Qoder/CodeArts 走会话额度 | 统计表 credit 字段 nullable；TRAE 按官方单价、CodeArts 福利模型按每日池 1:1 扣减推算并标 `credit_estimated`，展示加 ≈ |
| 登录机制 | CB/Qoder/CodeArts 轮询（后端出网）；TRAE 回调 | 双轨，回调统一走主端口 |
| 媒体/工具 | 各边 SSE 都含工具调用 | v1 透传，不做语义转换 |

## 4. 架构

### 4.1 分层

一个 FastAPI 进程内按**三个平面**组织，三面共享同一份 SQLite 与 Provider 客户端：

```
╔═ 请求面 ═══ 外部数据流 · 鉴权 = API Key ═════════════════════════════╗
║ 客户端   POST /v1/chat/completions · /v1/responses                    ║
║          POST /v1/messages · GET  /v1/models · /v1/user/balance       ║
║             │                                                         ║
║ 协议层   OpenAI / Responses / Anthropic 请求规范化 / 响应适配         ║
║          模型名解析：name ｜ name@provider                           ║
║             │                                                        ║
║ 执行引擎  调度器 · 冷却状态机 · 会话粘性 · 截断续写 · 统计采集       ║
║             │ 中立事件流                                             ║
║ CodeBuddy Provider                TRAE SOLO Provider                 ║
╚══════════════════════════════════════════════════════════════════════╝
         ▲ 同进程调用（管理面复用引擎与仓储）
         │
╔═ 管理面 ═══ 内部管理流 · 鉴权 = 会话 Cookie + RBAC ══════════════════╗
║ React 管理台 ──会话 Cookie──▶ 会话鉴权 + RBAC ──▶ 管理 API  /api/*   ║
║ 用户管理 · 审计 · 凭证运维 · API Key · 统计 · 任务与配置             ║
╚══════════════════════════════════════════════════════════════════════╝

╔═ 后台面 ═══ 无 HTTP 入口 · 8 类循环 ═════════════════════════════════╗
║ 额度探测 · token 预刷新 · 每日签到 · 成长中心 · 活跃上报 · 明细清理  ║
║ · 模型目录刷新 · 运维告警                                            ║
║       └──────────▶ Provider 客户端 ──────────▶ 上游                  ║
╚══════════════════════════════════════════════════════════════════════╝
                    │ 三面共享
        SQLite（WAL）：账号 / 加密凭证 / 统计 / 审计
```

**为什么按平面切**（而不是按「模块」或「服务」切）：

- **请求面**面向外部客户端，鉴权是 **API Key**（SHA-256 摘要 + 来源 IP 白名单 + 渠道绑定）；链路「协议层 → 执行引擎 → Provider」全程无状态、可并发。它不知道「人」是谁，只认 Key 的归属用户（用于按人统计）。
- **管理面**面向浏览器，鉴权是**签名会话 Cookie + 三角色 RBAC**（Q18/Q39，见 §4.7）；角色每请求现读 DB，改密 / 降级 / 禁用立即吊销。管理与请求**共用同一个执行引擎与仓储**，不做成独立服务——2–10 人自托管实例里，进程隔离只换来部署复杂度。
- **后台面**没有 HTTP 入口，由 `TaskRunner` 起 8 类循环（[TECHNICAL.md §6.2](TECHNICAL.md)），与请求面**共用 Provider 客户端与节流器**：各渠道的风控按最小间隔生效，不能因为「后台签到」与「前台对话」是两条代码路径就各发各的。
- **三面共享一份 SQLite**（WAL + `busy_timeout=5000`）：账号、加密凭证、统计与审计同库，因此升级只需重启**一个**进程（[TECHNICAL.md §6.4](TECHNICAL.md)）。

两个鉴权面互不替代：API Key 进不了管理台，会话 Cookie 也进不了 `/v1`（各自独立依赖，见 [TECHNICAL.md §2](TECHNICAL.md) 的 `deps.py`）。

### 4.2 Provider 接口（Q16=A 细接口）

Provider 承担上游协议私有部分：发请求、解析事件、分类错误，以及凭证生命周期与健康度探测。调度、冷却、重试、统计全在共享引擎。协议定义见 [TECHNICAL.md §4](TECHNICAL.md)。

### 4.3 调度器（Q12=B + Q26 + Q31）

统一实现，各 provider 共用。选号优先级：

1. **手动 pin 优先**（粘性让位，见下）
2. **会话粘性命中且可选**时直接复用，不参与排序
3. 过滤 healthy，含**模型级**避让：逐凭证按自己所属上游的原始模型名查 (凭证, 模型) 冷却表（见 [TECHNICAL.md §6.1](TECHNICAL.md#61-模型级冷却b11)）
4. **到期额度两级字典序**：先比主窗口（`QUOTA_EXPIRY_WINDOW_SECONDS`，默认 36h）内将过期的额度，打平（含都为 0）再比次窗口（`QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS`，默认 7 天）内将过期的额度
5. **健康度三态排序**：`known` 降序 > `unknown` > `exhausted`；健康度打平时**账户剩余积分多者优先**（健康度是「剩余/总量」比例，同比例下多留些余额备用），再同分按 `credential_id` 稳定。余额只作打平键，不会越级把低健康度的高余额号顶上来

无可用返回 None。到期指标让快过期的额度先用掉，避免白丢；冷却与错误累计规则见 [TECHNICAL.md §6](TECHNICAL.md)。

**为什么冷却分「账号级」与「模型级」两层**。上游的拒绝语义并不都是账号级问题：`429 + 6004` 是「这个模型在当前账号上用超了」，`400/404 + 11102` 是「当前账号没有这个模型」。一律记成账号级冷却，会让一次模型级限流把整个账号踢出池（同账号其他模型明明可用）；而丢掉 `11102` 不管，坏组合又会被反复选中。因此账号级继续写 `credentials.cooling_until`，模型级另建 `credential_model_cooldowns`；账号级冷却出现时清空该凭证的模型级条目，防「切模型」绕过账号级限制。业务码识别只认 `"code": N` 键值形态，不搜裸数字（`"code":111020` 含 `11102` 子串）。

**为什么是「窗口内积分总量」而不是「是否即将过期」（Q31）**。实测 CodeBuddy 的额度不是一个整块周期，而是几十个各自独立到期的小包（每日 100 积分 × N，`get-user-resource` 一次返回 30~36 个套餐）。由此定下四个取舍：

- **只存一个日期没有区分度**：各账号的「最早到期」经常落在同一天同一时刻，布尔分组退化成健康度排序；统计窗口内的到期积分总量，账号之间才有可比的高低。
- **落库到期阶梯而非预计算数字**：窗口是运行时参数，存 `[(到期 epoch, 该包剩余积分)]` 后，改窗口阈值立刻生效，不必等下一轮探测。
- **过滤条件必须是 `end > now`**：上游会把已过期套餐一起返回（`PackageEndTimeRangeBegin` 过滤的是套餐有效期，不是积分周期），不过滤则「最早到期」永远是过去时间、指标恒为 0；已用完的包（剩余 0）同样排除，它不携带积分。
- **两级窗口而非一个**：只比 36h 会出现大量账号指标同为 0（36h 内没有包到期），排序退化成健康度，一周内本该先用掉的积分反而没人管。故主窗口打平后再比更宽的 7 天窗口，同级内仍是积分多者优先；两级都是 0 才轮到健康度。次窗口只在主窗口打平时参与，不会把「36h 内该先烧的」压下去。

窗口 `≤0` 等于关闭整套到期排序（主窗口是总开关，次窗口一并归零，`expiry_windows()` 统一折算），退回纯健康度排序；无到期信息的渠道（如 CodeBuddy 企业版）恒为 0 分。

**TRAE 也按包独立到期**（2026-09-30 修正）：早期实现按「TRAE 无周期概念」只填展示用的 `quota_packages`、`expiry_ladder` 恒为 `None`，导致「到期额度」行永不显示、快过期的积分拿不到优先消耗。实测 TRAE 的 `ide_user_ent_usage` 权益包**各自独立到期**（每月登录积分按月、签到奖励各有到期日，账号常见十几个包），与 CodeBuddy 同构，故 TRAE 也填 `expiry_ladder`（口径一致：只收「未过期 + 有余额」的包），把快过期的积分纳入选号。

**CodeArts 的每日池也落此阶梯**（2026-09-30）：每日池 0 点清零、不累计，用完即弃。登记 `[(次日本地 0 点, 当日剩余)]` 后，一级指标恒把它排在其它渠道之前——只要还有额度就先走它，用尽（`remaining=0`，阶梯为空）自然回落。金额已折成积分（1000 万 token ≡ 1000 积分），与其余渠道同单位。

**展示与调度指标分开存**（`quota_packages` vs `quota_expiry_ladder`）。管理台要展开「这个账号有哪些额度包、各自何时到期、用了多少」，而 `quota_expiry_ladder` 是选号指标：结构只有 `[到期, 剩余]`，装不下包名。故另存一列 `quota_packages`（`[{"name","total","used","end"}]`，JSON）仅供展示。两个渠道都同时填两列：TRAE 的权益包与 CodeBuddy 的套餐一样各自独立到期（见上），阶梯口径统一为「未过期 + 有余额」。口径差异也保留：阶梯只收「未过期 + 有余额」的包，展示明细额外含「已过期但仍有余额」（提醒浪费）与「已用完但仍有效」的包。

**会话粘性**（调度前置一步）。OpenAI 协议本身无会话概念，客户端「对话」的识别按可靠性分两级（B1.5）：

1. **显式会话标识**：`conversation_id` / `conversationId` / `prompt_cache_key`（`metadata` 内或请求体顶层）——客户端直接给出会话身份，消息被裁剪也能粘住。
2. **回落：消息增量前缀指纹**：以上一轮完整 messages 为前缀再追加，据此定位上一轮实际服务的凭证。

TTL（`CONVERSATION_STICKY_SECONDS`，默认 1h，≤0 关闭）内固定复用，不再按到期积分 / 健康度重排——对话中途换号会触发上游风控并丢掉上游侧提示词缓存。请求体带 `user_id`（顶层或 `metadata` 内）时**不派生**第 2 级兜底键：同一用户的并行对话消息前缀可能相同，派生会把它们误钉到同一凭证。

> 键名核实状态：`prompt_cache_key`（OpenAI 官方顶层参数）与 `metadata.user_id`（Anthropic Messages API 官方字段）已核实；`conversation_id`/`conversationId`/顶层 `user_id` 非两家标准键，属客户端惯用约定，开发环境 71 份真实 dump（PI 客户端）中**未观测到**，作为兼容探测接受（命中即用、未命中无害）。

**手动 pin 优先于粘性**：存在可选（enabled、未禁用、未冷却）的 pinned 凭证时粘性让位，否则管理员显式「指定」会在对话中途无形失效。粘住的凭证报错仍走正常轮换，成功后重新粘到实际服务的凭证。指纹链掺入用户名，防不同用户的相同消息数组串到同一凭证；条目纯内存，重启后丢粘性只影响一轮选号。

**健康度归一化**（Q26 核心）。两者都是积分制，但周期语义不同：

| | CodeBuddy | TRAE |
|---|---|---|
| 剩余 | `CycleCapacityRemainPrecise` | `credits_limit - credits_amount` |
| 总量 | `CycleCapacitySizePrecise` | `credits_limit` |
| 周期 | `CycleStartTime`/`CycleEndTime` | 无（单调余额） |

> **不要用响应顶层的 `TotalDosage`**（2026-09-28 线上修复）：实测三个真实账号，`TotalDosage = Σ(size − CapacityUsed)`，而 `CapacityUsed` 是**上周期**口径；本周期消耗记在 `CycleCapacityUsed`/`CycleCapacityRemain`。因此 `TotalDosage` 在整个周期内不变，且基准与按 `size` 累加的 `total` 不一致（会出现 `remaining == total`、健康度恒 100、`credit_events` 停止记录）。`remaining` 一律取逐包 `CycleCapacityRemainPrecise` 之和，与到期阶梯、额度明细同源。

```python
def health(q) -> HealthScore:   # known(0-100) | unknown | exhausted
    if q is None or q.probe_failed: return "unknown"
    if q.total <= 0:                 return "exhausted"
    return clamp(round(q.remaining / q.total * 100), 0, 100)
```

**为什么必须三态**：CB 允许 bearer-only 手动凭证（无额度信息），探测失败也会发生。把未知当成 0 分，这类凭证在有健康号时永远轮不到——探测失败被误判成「没额度」。`unknown` 排在 known 之后但仍参与调度；`exhausted` 才真正垫底。

**展示层必须标注周期语义**：CB 是「本周期剩余（到期回满）」，TRAE 是「账户剩余（单调递减）」——注意这是**余额聚合口径**，与「额度包各自独立到期」不矛盾：TRAE 的总余额随消耗单调递减（不像 CB 会周期回满），但其中各权益包仍有各自到期日，到期未用完即作废（故纳入到期优先排序）；`unknown` 显示为「未探测到额度」。

**credit 不可作为统计核心指标**：两边上游的 SSE 都不保证返回 per-request credit（CB 的 `usage.credit` 是可选字段、样本中基本不出现；TRAE 只有 `token_usage`）。健康度的唯一可靠来源是额度探测接口的 `remaining`；统计页 credit 只做辅助展示，主指标是 token。

### 4.4 模型名解析（Q21=C + Q27=A + Q41）

```
"glm-5.2"          → 健康度路由，自动选 provider
"glm-5.2@trae"     → 强制 TRAE
"glm-5.2@codebuddy" → 强制 CodeBuddy
```

边界行为：

- model 为空或 `"auto"` → 路由到 `DEFAULT_MODEL`（env，默认 `glm-5.2`）
- 未知模型名 → 400 `invalid_request`，不回退到列表首项
- `@` 后缀的 provider 不存在 → 400
- TRAE 动态模型拉取失败 → 回退内置静态模型表，失败负缓存 5 分钟
- **收窄后无可用凭证 → 回退全量候选**：目录能证明归属时会把候选收窄到登记了该模型的渠道；若收窄后的渠道**全都没有可用凭证**（目录陈旧/降级——某渠道新增了该模型但别名表还没更新，或该渠道模型列表回退了静态表而丢掉它），退回原始候选集再选一次，让真正持有该模型且有可用凭证的渠道兜底，避免「CodeArts 无凭证 → 直接 503」而 CodeBuddy/TRAE 明明可服务。`@provider` 强制 / Key 绑定的候选本就是单一渠道，不受此兜底影响（保持强制语义）
- **列表只按渠道凭证加载**（Q41）：`/v1/models` 与 `/api/playground/models` 只合并「当前有可用凭证」的渠道，未接入 / 全部暂停 / 会话失效的渠道不拉取也不展示；列表是展示口径，直连已滤模型不受影响
- **列表展示顺序**：CodeBuddy / TRAE 的模型排前（`_PROVIDER_RANK`：codebuddy 0 → trae 1 → qoder 2 → codearts 3 → 其余 4），组内按归一键字典序，多渠道模型按最高优先级渠道归位；Playground 分组与「强制指定渠道」下拉按同一顺序（`PROVIDER_ORDER`）。纯展示排序，不影响调度选号

### 4.4.1 模型名三字段（`src/provider/naming.py`）

六条渠道的每个模型统一成三个字段，同一条链派生（`(raw_id, 上游 name) → 清洗 → 展示名 → slug → 归一键`）：

| 字段 | 规则 | 例 |
|---|---|---|
| **原代号**（`raw_id`） | 渠道请求时真正发的 key，**永不改动**；转发时经 `services.model_aliases` 换回 | `kmodel_latest`、`kilo-auto/free` |
| **归一键**（`normalize_model_key`） | 剥掉免费标记（`-free`/`_free`/`:free`/路径段 `free`/括号词 `(free)`）与命名空间前缀（`厂商/模型` 取末段、`厂商: 模型` 削前缀），再 slug 化 | `kilo-auto`、`longcat-2.5-preview` |
| **展示名**（`display_model_name`） | 清洗后的可读文本；上游可读名优先，无则由原代号派生；品牌/缩写按官方写法纠正 | `LongCat 2.5 Preview`、`Qwen3.8 Max` |

要点：**合并键 = 归一键**（六渠道统一，靠展示名对齐各渠道互不相同的内部代号；zen/kilo 也参与）；**`/v1/models` 对外 `id` 一律是归一键**，原代号只进 `by_provider.{渠道}.raw_id` 并用于转发，用户按原代号 / 展示名 / 归一键三种写法都能命中；**同模型异名**由 `MODEL_SYNONYMS` 显式规范（上游给同一模型起了不带版本号的名字，如 Qoder `DeepSeek-Flash` = DeepSeek V4.1 Flash，键与展示名一起收敛）；**哨兵名**（`auto` / `default`）语义只在本渠道内成立，不跨渠道合并；同渠道内归一键重复、或对外 id 撞车时按序退回原 id 归一键 / 加后缀。

清洗规则、合并 / 消歧 / 别名登记与黑名单匹配的完整实现见 [TECHNICAL.md §3.5](TECHNICAL.md)。

### 4.5 登录双轨（Q17=C）

```python
class AuthSession(BaseModel):
    flow: Literal["poll", "callback"]
    # poll: 后端轮询上游
    auth_url: str | None
    interval: int | None
    # callback: 浏览器 302 回本服务
    callback_url: str | None
    state: str
```

**回调统一走主端口** `/authorize`，废弃 TRAE 的 18080 独立端口。远程部署只需暴露一个端口。回调地址要写进登录 URL，因此必须可配：`PUBLIC_BASE_URL`（默认 `http://127.0.0.1:8000`），远程部署设为浏览器可达的公网地址。

前端在「凭证管理」页内实现两种 flow（`CredentialsPage` 的 `startLogin`/`cancelLogin`，无独立组件）：
- **CB poll**：拿到 `authUrl` 开新窗 → 后端轮询 `/api/auth/upstream/poll`，得到成功/失败/超时
- **TRAE callback**：开授权窗，浏览器 302 回 `/authorize` 直接落库；前端轮询凭证列表出现新条目即视为完成（上游不回传 state，无法直接轮询登录状态）
- **取消**：重新 `start` 拿 state 后调 `/api/auth/upstream/cancel`

两轨的失败/超时以通知文案呈现（无统一 `failed`/`expired` 状态机）。

### 4.6 中立事件层（Q13=B 预留）

v1 只接 OpenAI 出口，但上游 SSE 解析到「中立事件」这一步独立成层（`Event` 定义见 [TECHNICAL.md §3.1](TECHNICAL.md)）。v1.1 加 Anthropic 出口时，只新增一个 `Event → Anthropic SSE` 适配器，不动上游逻辑。

### 4.7 账号与权限面（B5，Q39）

管理面的身份来自 SQLite `users` 表，不再是 `users.txt` + `ADMIN_USERNAMES` env。三档角色与判定面：

| 角色 | 能做什么 | 判定依赖 | 典型用途 |
|---|---|---|---|
| `admin` | 用户管理、运行时配置、凭证写操作、全量统计、审计日志 | `require_admin` | 维护者 |
| `operator` | 凭证写操作（导入 / 删除 / 启停 / pin / 切换账号 / 签到 / 成长）、全量统计 | `require_operator` | 日常运维 |
| `viewer` | 只读（看仪表盘、凭证列表、自己的统计、Playground；审计为 admin-only） | 无（登录即可） | 观察者 |

**鉴权链**（每个管理面请求）：签名 Cookie（HMAC，payload 带用户名与 `session_epoch`）→ 用户仍存在且启用 → epoch 与 DB 一致 → 角色现读 DB。四者缺一即 401；**角色不进 Cookie**，所以降级立即生效，不必等 Cookie 过期。

**吊销不建会话表**：`users.session_epoch` 在改密 / 改角色 / 启停 / 硬删时 `+1`，旧 Cookie 当场失效；老 Cookie 无 `ep` 声明按 0 处理，升级不打断已登录会话。

**建号与重置走一次性激活令牌**：库里只存 SHA-256 摘要 + 过期时间，明文仅在创建 / 重置的响应里回显一次，用户在 `/activate` 自设密码。项目没有邮件设施，这是「不出现管理员已知的共享密码」的最优解；心智模型与「API Key 明文仅一次」一致。被重置的用户 `must_change_password=1`，改密前只放行「看会话 / 改密 / 登出」三条精确白名单。

**审计**：登录成败、账号建 / 激活 / 改角色 / 启停 / 改密 / 重置 / 硬删、凭证写操作全部入 `audit_events`，**绝不记密码或令牌明文**；查询走 admin-only 的 `GET /api/audit`。

**三层防锁死**：Web 端「最后一个活跃 admin」守卫 + 自我降级 / 自禁用守卫；CLI 拒绝硬删最后一个活跃 admin；bootstrap 发现无活跃 admin 直接启动失败（并给出恢复路径）。

**删除语义**：**禁用是主路径**（可逆、保住用量归属），硬删只在 `scripts/create_user.py --delete --force`，管理台不暴露 `DELETE`。

实现细节（引导三层、防锁死顺序、白名单端点、schema）见 [TECHNICAL.md §3.13](TECHNICAL.md)。

## 5. 数据模型

- **用户建表**（B5，Q39）：`users`（PBKDF2 密码哈希 + `role` + `enabled` + `must_change_password` + `session_epoch` + 一次性激活令牌摘要）与 `audit_events`（登录/账号变动/凭证写操作）是唯一源。`users.txt` 仅在启动时**一次性导入**（已存在的用户名不覆盖，幂等），路径仍走 `USERS_FILE`（`config.py` 的 `users_file`，默认 `secrets/users.txt`）；角色改由 `users.role` 决定，`ADMIN_USERNAMES` 只剩引导期提权作用。`api_keys.username` 仍由应用层校验存在性，不加外键
- **API Key 存摘要**：SHA-256，明文仅创建时返回一次
- **凭证加密列**：`data_enc` 走 Fernet；调度状态（`health` / `cooling_until` / `err_count` / `pinned` / `quota_expiry_ladder`）落库，重启不丢冷却状态与到期阶梯
- **用量脱敏**：`usage_events`（明细 90 天）+ `usage_hourly`（小时汇总永久），`credit`/`cached_tokens`/`cost_usd`/`cost_cny` 可空、仅辅助展示；成本为估算（Q70）
- **成长中心**：`growth_events` 只存汇总行（一轮一行人话汇报 + 积分/能量/连签 + trigger），不存活动内部结构；`credentials.growth_last_run_at`/`growth_last_result` 供列表直接显示；活跃上报（B1.7）复用该表记一行，不新增表
- **token 到期**（Q35）：`credentials.token_expires_at`（显式 `expires_at` 优先，缺失回落 JWT `exp`；0 = 未知）与 `token_issued_at`（JWT `iat`，仅落库供诊断）。派生逻辑在渠道中立的 `provider/token_expiry.py`，**不猜本地 TTL**
- **积分流水**（Q36）：`credit_events` 记两次额度探测之间的净变化（含 `window_start` 与归因已知度 `source`）；**不是动作归因**——上游不打日志，diff 分不出分数是谁加的。保留期同 `usage_events`（90 天）
- 签到去重与模型列表缓存均进程内实现，不进库
- **后台任务运行态**（Q38）同样进程内、**不建表**：跨重启的历史价值有限（业务留痕已有 `growth_events` / `credit_events` / `usage_events`），落库反而要新表 + 保留期清理 + 老库迁移

DDL 以 [src/db/schema.sql](src/db/schema.sql) 为准，补充实现细节见 [TECHNICAL.md §7](TECHNICAL.md)。

**脱敏纪律**（继承 CB）：不存提示词、回答、请求头、Token、工具参数、原始错误体、会话 ID。

唯一例外：诊断开关 `DUMP_REQUEST_BODIES=true`（默认关）会把 `/v1` 原始请求体落盘到 `data/dumps/`（有界保留 200 份）。这是排查客户端差异的临时手段，**含完整对话内容**，不得长期开启、不得随库交付。

## 6. 目录结构

以 [TECHNICAL.md §2](TECHNICAL.md) 为准（随代码同步维护）。

## 7. API 契约

外部（API Key 鉴权）：`POST /v1/chat/completions`（流式 + 非流式）、`POST /v1/responses`（Responses 子集，Codex CLI；与 chat 共用同一调度 / 选号 / 统计链路）、`POST /v1/messages` 与 `POST /v1/messages/count_tokens`（Anthropic Messages 子集，Claude Code；同样共用调度链路）、`GET /v1/models`（扁平模型名 + `providers` 字段 + 清洗后的 `name` + 多渠道模型的 `by_provider.{渠道}.{credit_rate,raw_id}`，字段语义见 §4.4.1）、`GET /v1/user/balance`（DeepSeek 兼容余额，读探测缓存聚合，不实时打上游）、`GET /health`（纯存活）、`GET /healthz`（存活 + 凭证池计数，无鉴权）。

管理台（会话 Cookie）：凭证管理、API Key 管理、用量统计、Playground、用户管理（admin-only）、审计日志（admin-only）、任务与配置（admin-only）。admin 管用户、配置与凭证，operator 管凭证写操作（含导入/删除），viewer 只读；用量统计按角色决定是否展示全量。账号端点：`GET|POST /api/users`、`PATCH /api/users/{username}`、`POST /api/users/{username}/{disable|enable|reset-password}`（**不提供 DELETE**，硬删走 CLI）；自助改密 `POST /api/auth/password`；无鉴权的一次性激活流 `GET|POST /api/auth/activate`；审计查询 `GET /api/audit`。凭证运维端点含 `POST /api/credentials/{id}/checkin`（签到）、`GET|POST /api/credentials/{id}/growth`（成长中心状态与手动执行，仅 CodeBuddy）；运行时配置与任务运行态走 `GET|PUT /api/settings` + `GET /api/tasks`。回调（无鉴权，TRAE 浏览器 302 不带 key）：`GET /authorize`。

实现以代码为准，使用说明见 [README.md](README.md)。

## 8. 安全边界

沿用 codebuddy2api 的既有约定：

- 上游 endpoint 白名单：**只接受明确配置的地址**，真实 Token 绝不转发到未授权站点
  - CodeBuddy：`CODEBUDDY_API_ENDPOINT` 启动时强制校验，不在白名单直接失败
  - TRAE：凭证 JSON 里的 `apiHost` 是用户可控输入，导入时按官方地址白名单校验，不在白名单直接拒绝；旧库里已存的越界 `apiHost` 在刷新 / 取用户信息前退回官方地址（校验在 `TraeClient` 内部，不只 HTTP 边界）
- TLS 校验默认开启，公网部署必须保持
- Host / Origin 白名单，CSP `frame-ancestors`（另含 `object-src`/`base-uri`/`form-action` 限制）
- 登录三级限流（全局 / IP / 用户名）+ PBKDF2 并发上限
- 请求体上限 16MB、登录接口 8KB（ASGI 层按实际字节计数，`chunked` 不能绕过）
- API Key 仅存摘要，明文只在创建时返回一次；可按 Key 限定渠道绑定与来源 IP 白名单（见 [README.md](README.md)）
- 凭证内容加密入库，密钥走 `APP_SECRET`：最短 16 字符，弱密钥拒绝启动；**丢失 = 已存凭证全部不可解，只能重录**，不做密钥轮换。解密失败返回可行动错误码 `credential_decrypt_failed`，不暴露裸 500
- 管理台会话 Cookie `SameSite=Lax` + 写操作自定义头校验（CSRF，含 logout）
- 会话与 API Key 除签名 / 摘要外**校验用户仍存在、启用且会话 epoch 一致**（B5）：删用户、禁用、改角色或改密码（bump epoch）都会让已签发的 Cookie 当场失效
- 未匹配的 `/api`、`/v1` 路径返回 JSON 404（不落到 SPA 的 200 + HTML）
- 日志脱敏：不打印 Token、完整请求体
- 审计：凭证增删改、pin、账号切换、登录与账号变动写 INFO 日志（含操作人）；**绝不记密码/令牌明文**
- 审计与统计的租户隔离：`/api/stats/events` 返回的**凭证昵称**取自全局共享池（常含邮箱/手机），仅 admin/operator 可见；viewer 即便只看自己的记录也不下发该字段
- 错误响应不回流上游正文：`/v1` 与外层的错误文案只带受控标识（HTTP 状态码 / 上游业务码 / 异常类名），上游错误体摘要只进服务端日志
- 热更配置（B3.2）写入即校验：float 拒 NaN/±inf（NaN 会同时绕过 min/max），字符串类有长度上限；告警 webhook 只接受 `http(s)` 地址
- 入站解析把关：三个出站协议（OpenAI / Anthropic / Responses）在入口处拒绝越界/非法的数值字段（`max_tokens` ≤0 或超上限、`temperature`/`top_p` 为 NaN/Inf）与 JSON 里的 `NaN`/`Infinity`——不透传给上游、不污染凭证健康度与统计
- 上游流解析有界：SSE 单行与单帧缓冲设上限，非 2xx 错误体有界读取（防恶意/异常上游用超大 body 撑爆内存）
- OAuth 登录弹窗：后端返回的授权地址只允许 `http(s)` scheme，弹窗 `opener` 置空（防 `javascript:` / `data:` URL 在管理台 origin 执行脚本）
- **部署契约**：前端产物改动后刷新即生效；**后端 `src/` 改动必须重启进程**——进程管理器只在进程退出时重拉，不监听源码，保活策略不是热重载。两者独立更新会产生「新前端 + 旧后端」错配（新端点 `404` → 前端报无关的兜底文案），故升级后必须重启（详见 [TECHNICAL.md §6.4](TECHNICAL.md) 与 [README.md「部署注意」](README.md)）

不做的：mTLS；**面向管理台与端口的** IP 限制（交给反向代理）。注意与上文的 API Key 来源 IP 白名单区分——后者是应用层能力，已内建。审计覆盖登录、账号变动与凭证管理写操作，不做全量请求审计（统计表已是脱敏的请求级记录）。

env 完整清单见 [README.md「配置」](README.md)（以 `src/config.py` 为准）。

## 9. 里程碑

M0 骨架 → M1a TRAE → M1b CB 基础 → M1.5 CB 完整化 → M2 前端 → M3 收尾，**已全部完成**。状态见 [README.md「状态」](README.md)。

## 10. 技术选型

见 [TECHNICAL.md §1](TECHNICAL.md)。

## 11. 风险清单

| 风险 | 等级 | 对策 |
|---|---|---|
| 从零重写丢失旧项目踩坑经验 | 高 | 关键约束抄进 AGENTS.md；M1 结束做双 provider 对比验证 |
| CB 协议复杂度是 TRAE 的 2.4 倍 | 高 | M1 拆成 M1a/M1b/M1.5 串行消化；CB 先 bearer-only 跑通再补 OAuth |
| 上游 credit 不保证可得 | 低 | credit 仅辅助展示；健康度只依赖额度探测接口 |
| 上游 SSE 格式变更 | 中 | 每个 provider 的 SSE 样本存 fixture 做契约测试；解析失败不静默 |
| 抽象设计错误（Q25） | 中 | M0 用 mock provider 先冻结调度接口，100% 覆盖 |
| 模型 ID 撞车导致路由错误 | 中 | 扁平名 + `@provider` 后门；统计行强制带 provider 字段 |
| 积分语义混淆（周期 vs 余额） | 低 | 健康分仅用于调度；展示层标注周期语义 |
| 容器环境特殊（Apple container，无 compose） | 低 | Dockerfile 本地构建验证；compose 靠 CI 验证 |
| License 溯源不全 | 中 | NOTICE 列明 5 个参考项目（含各自上游共 7 条来源）的署名与协议 |

## 12. NOTICE 三方溯源

署名清单以仓库根目录的 [`NOTICE`](NOTICE) 为唯一真源（本文件不再复制一份，
避免两处各自漂移）。当前列出的参考项目：

| 项目 | 借鉴方向 |
|---|---|
| [IceeAn/codebuddy2api](https://github.com/IceeAn/codebuddy2api) | CodeBuddy 上游协议、凭证管理、脱敏统计 |
| [connectedGraph/trae2api-web](https://github.com/connectedGraph/trae2api-web) | TRAE SOLO 上游协议、账号池冷却状态机 |
| [88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin) | 成长中心与签到接口的逆向结论与运行经验 |
| [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) | 活跃上报 `/v2/report` 的协议形状与实测结论 |
| [ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) | 后台任务可视化页面的设计参考 |
| [shuishuipingan/qoder2api-hub](https://github.com/shuishuipingan/qoder2api-hub) | Qoder COSY 签名、自定义 Base64、信封 SSE、设备码登录、签到与额度 |
| [HITZY2002/codearts2api](https://github.com/HITZY2002/codearts2api) | CodeArts SDK-HMAC-SHA256、DPoP(ES256) 刷新、累计全文 SSE、福利模型 |

均为 MIT License，完整版权行与「其上游」子项见 `NOTICE`。本项目的代码为独立实现，
不复制上述项目的源代码；上游服务的协议细节来自对客户端行为的观察，不属于上述项目
的版权范围。
| Q67 | 收窄后全不可用时区分成因：模型级冷却不再扇出 | 实测故障（2026-10-08 15:53）：`qwen3.8-flash` 只有 Qoder 登记（内部原代号 `qfmodel`），Qoder 节点故障 `[FAIL]node:oa_qwen-plus-main Execution failed`（按 Q50 归 MODEL）让两条 Qoder 凭证进 600s 模型级冷却；此后每个请求收窄到 `{qoder}` → 候选空 → 触发 `_select` 的 broader 回退，扇出到**不持有该模型**的渠道。它们别名表没有这个键，外发的仍是原始名 `qwen3.8-flash`（没机会换成对方原代号），必然被拒：CodeBuddy 11102 / TRAE 4001 / zen 401 / kilo 401。实测 6 次轮换全废、客户端拿到 400 `invalid_request`（而非「模型暂不可用，换模型即可」），还给 CodeBuddy 三条凭证各写下 6 小时起步的 BLOCKED 负缓存（`credential_model_cooldowns` 至今可见）。修复：回退前分清「收窄后为什么空」——**账号级原因**（无凭证 / 硬禁用 / 暂停 / 账号冷却）说明目录可能陈旧或降级，仍退回原始候选集重试；**模型级冷却**说明持有方是对的、只是上游暂时不可用，此时放宽即扇出，是纯浪费，故**不扇出**，直接交既有 `_unavailable_text` 出模型级 503（含相似模型建议）。判定抽成 `_model_cooled_only(providers, target, now)`，`_all_model_cooled`（文案分流用）改为复用它，两处口径不再分叉；判定为「纯模型冷却」的前提是候选集非空且每条候选都账号级可选——混入账号级不可用或该上游零凭证时判 False，照旧放宽（目录陈旧兜底语义零变化，已有 3 个回退用例不受影响）。连带修掉的是「扇出噪音」：不再给不认该模型的渠道打上游、不再产生 `invalid_request` 统计、不再写 BLOCKED 负缓存。无 schema / 配置变更 |
| Q68 | CodeArts 凭证生命周期修复（刷新节流 / STS 过期分类 / 一次性票终态 / 到期预警） | 实测故障（2026-10-08）：CodeArts 凭证每约 60 分钟被预刷新任务轮转一次，`refresh_token` 是**一次性**的，多烧一次就多一分「被并发消费掉」的风险；一旦烧废，聊天侧收到 `APIG.0602 security token has expired`（HTTP 400），被 `classify_status` 判成 `INVALID`（换 provider 也没用、且不触发冷却），客户端只看到 `model not available on any configured upstream`；同时预刷新任务此后每小时拿同一张废票重试、各刷一条一模一样的 warning，而凭证早已死透。四处修复：① **刷新节流**——`refresh_skew_hours` 默认 24h ≫ STS 寿命 2h，`needs_refresh` 对该渠道**恒为真**，故给 `CodeArtsCredential` 加 `refresh_skew_cap_seconds=1800` 上限，把「一进窗口就每轮都刷」降成「离到期 30min 内才刷」（下界硬约束是预刷新轮询周期，窗口窄于周期会整轮漏过导致凭证静默过期）。② **`APIG.0602` 分类**——`classify_status` 在 `400 → INVALID` 之前识别 `security token has expired`，判 `SOFT`（账号级**可恢复**：下一轮预刷新换上新 STS 即自愈）。不判 `DEAD`：硬禁用会让预刷新任务跳过该凭证（`candidates()` 的 disabled 分支），反而断掉唯一的自愈路径；不判 `INVALID`：语义是「请求本身无效」，换 provider 也没用且不冷却。③ **一次性票终态**——新增 `provider.base.UpstreamReloginRequired`；CodeArts 令牌端点把 `STS5.1806 the refresh token has been used` / `invalid client id` / `InvalidDPoPHeader` / `invalid_grant` 翻译成该异常（`events.relogin_required(body)` 判定，只认上游点名的几类终态，网络/5xx/限流仍走可重试路径），CodeBuddy 401/403 同样归入；`RefreshTask` 捕获后调 `CredentialRepository.mark_relogin_required` 写**可操作**的 `disabled_reason`（「刷新令牌已失效，需重新登录」）并硬禁用 + 清模型级冷却，本轮记 failed、下轮直接跳过——不再每小时刷同样的 warning，管理台按原因提示人工重新登录。④ **到期预警修正**——`_EXPIRY_KEYS` 补 `expiration`（此前漏掉使 CodeArts 的 `token_expires_at` 恒为 0，管理台无到期预览、`token_expiring` 告警对它完全失效）；`credential_token_times` 增 `_ISSUED_ONLY_KEYS`，access token 非 JWT 时借**同时签发**的 `refresh_token` JWT 的 `iat`（只借 iat 不借 exp——后者是「登录 +30 天」，会把 2h 临时凭证画成 30 天）；`expiring_tokens` 静音「寿命本就短于窗口」的凭证（CodeArts 2h 寿命 vs 默认 24h 窗口会恒真命中，把真告警淹掉；签发时间未知时不猜、按原语义照报）。无 schema / 配置项变更 |
| Q69 | CodeArts 预刷新窗口与轮询周期错配修正 + 短寿命 token 不再恒标「即将到期」 | 实测故障（2026-10-08 二次）：用户反馈「CodeArts 显示又要到期」。Q68 把预刷新窗口封顶 30min，但预刷新轮询周期写死 60min（`TaskRunner.refresh_interval_minutes` 默认 60 且 `build_runner` 未从配置接线）——**窗口 ≤ 周期时轮询点会整轮落在窗口之外**，凭证一直拖到**到期瞬间**才刷：实测 `issued 19:07:32 → expires 21:07:32`，刷新发生在 21:07–21:08（晚了约 26s），管理台最后一小时恒标红、刷新瞬间短暂显示「已过期」，上游还回了 `APIG.0602`。两端修复：① **后端**——新增可热更项 `REFRESH_INTERVAL_MINUTES`（默认 30、下限 5）并在 `build_runner` 接线（此前该形参从未接线、实际恒用默认 60），把轮询缩到 30min；`CodeArtsCredential.refresh_skew_cap_seconds` 从 1800 抬到 2700（> 30min 周期）留出余量，轮询粒度下实际在**剩余约 30min** 处刷新（每约 90min 一轮）——既不贴边也不过密。② **前端**——`tokenExpiryView()` 增 `issuedAt` 参数：整段寿命 ≤ 预警窗口 2 倍时该 token 天生在窗口附近（CodeArts STS 寿命 2h、窗口 1h），「即将到期」是常态而非异常，不再标红（**真过期仍标红**，那是刷新失败的信号），与后端 `expiring_tokens` 对短寿命凭证静音同口径；`issuedAt` 缺省时保持旧语义（只看剩余时间），避免未知签发时间时误静音。配置项变更：新增 `REFRESH_INTERVAL_MINUTES`（compose 同步透传）；无 schema 变更 |
| Q70 | 统计新增「成本」估算（models.dev 刊例价 × 汇率） | 用户需求：在现有 token/credit 之外，直观看到每个渠道/请求的**估算花费**。**数据源是 [models.dev](https://models.dev) 的模型目录**（`https://models.dev/api.json`，每模型 `cost.input`/`cost.output`/`cost.cache_read`，USD / 百万 token），**不用**渠道 `credit`（那是额度单位，不是钱）。新模块 `src/pricing.py`：`build_price_table` 把原始 JSON 压成 `{model.id.lower(): (input, output, cache_read)}`，同一 id 挂在多个 provider 时**优先原厂**（`canonical_model_id` 前缀 == provider id，如 `zhipuai/glm-5.3-flash`），否则取 `input` 价最高者（避开 0 价套餐/折扣）；`cache_read` 缺失按 `input` 原价计；`estimate_cost_usd` = `(输入−命中)×输入价 + 命中×缓存价 + 输出×输出价`（命中夹到 `[0, 输入]`），输入 token 缺失或模型未收录回 `None`。**写入时定值**：`StatsCollector.record()` 按当时价表与汇率算好 `usage_events.cost_usd`/`cost_cny` 落库，历史行不随价表/汇率变化重算。**schema 变更（17→18）**：`usage_events` 加 `cost_usd`/`cost_cny`，`usage_hourly` 加 `cost_usd_sum`/`cost_cny_sum`/`cost_known`（两列同生同灭共用 `cost_known`，=0 时查询回 `None` 显示 `—`，不拿 0 冒充免费——成本天然是**下限**）；老库经 `_MIGRATION_COLUMNS` 幂等补列，历史行为 NULL。**汇率**是热更项 `USD_CNY_RATE`（默认 6.70，仅影响之后写入的行）；价表由后台任务 `price_catalog`（`PRICE_CATALOG_MINUTES`，默认每日、下限 60 分钟，注入式协程）拉取并落盘 `DATA_DIR/model_prices.json`，启动同步回灌（零上游请求），失败只记日志；**无快照时**（首次部署）启动另起后台预热任务补拉一次（不阻塞 `/health`），避免成本空窗到下一轮。查询侧 `overview`/`by_provider`/`events` 回 `cost_usd`/`cost_cny`，图表新增 `cost` 指标（人民币口径）；前端总览加「成本（估算）」卡片、按渠道表与明细表加成本列、图表加成本 tab，人民币为主、美元为辅，一律前置 `≈`。**不做的**：不做 credits→yuan 的换算（与成本无关）、不逐渠道对齐 provider（按模型 id 全局匹配，覆盖不到的显示 `—`）。**历史明细回填**：功能上线前的明细为 NULL，用 `scripts/backfill_cost.py --apply` 按当前价表+生效汇率一次性补齐/重算（默认预览、写库前自动备份、幂等；口径是「按今天重估」而非还原当时花费） |
