"""启动配置：pydantic-settings 绑定 env。

立项定义的配置项在此定型，缺必填项在进程启动阶段失败。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

_CODEBUDDY_CN = "https://copilot.tencent.com"
_CODEBUDDY_INTL = "https://www.codebuddy.ai"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    # 安全
    app_secret: str
    admin_usernames: str = ""

    # 服务
    public_base_url: str = "http://127.0.0.1:8000"
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: str = "./data"
    # 用户文件（PBKDF2 users.txt）：唯一用户源，启动时必须存在且至少一个有效用户
    users_file: str = "secrets/users.txt"
    log_level: str = "INFO"
    # Host 白名单（防 DNS rebinding）：逗号分隔；空 = 本地默认 + PUBLIC_BASE_URL 主机
    allowed_hosts: str = ""
    # 额外放行 FastAPI TestClient 的默认 Host `testserver`。**仅测试用**，默认
    # 关闭：单标签主机名可能被本地 DNS 搜索域解析到本服务，等于给 DNS rebinding
    # 留了一个免校验入口。测试由 conftest 的 autouse 夹具置 true。
    allow_test_host: bool = False
    # 是否暴露 /docs 与 /openapi.json：默认关闭（匿名可拉全量 API 结构）。
    # 本地调试需要 Swagger 时置 true
    enable_docs: bool = False
    # 是否采信 X-Forwarded-For 判定来源 IP（API Key 的 allowed_ips 白名单用）。
    # 默认 false：XFF 由客户端可写，直连部署下信它等于白名单形同虚设。
    # 仅在「本服务前面恰好一层受信反代」时开启，届时取 XFF 最后一个条目。
    trust_proxy: bool = False

    # 上游（白名单内的地址才可接收真实 Token）
    codebuddy_api_endpoint: str = _CODEBUDDY_CN
    codebuddy_allowed_endpoints: str = f"{_CODEBUDDY_CN},{_CODEBUDDY_INTL}"
    # CodeBuddy 国际版（www.codebuddy.ai）：与国内站**同协议同账号体系**的独立
    # 渠道。为什么要独立渠道而不是改 CODEBUDDY_API_ENDPOINT 切站点：端点是
    # 进程级单值，切过去会让所有存量国内站凭证立刻打到国际站（403/额度错配）。
    # 两个站点可同时持有凭证，各走各的 endpoint、节流器、OAuth 设备码流程。
    codebuddy_intl_api_endpoint: str = _CODEBUDDY_INTL
    # 国际站自己的白名单：刻意**不含**国内站主机。两站 Token 由不同网关签发，
    # 混发等于把国内站 token 递到国际站（反之亦然），这是凭证泄露面。
    codebuddy_intl_allowed_endpoints: str = _CODEBUDDY_INTL
    # OpenCode Zen 免费层：端点固定，UA 版本是门禁的一部分（<阈值 → 426）。
    # Zen 无凭证、不携带任何用户 Token，端点的白名单校验只为防误配。
    zen_api_endpoint: str = "https://opencode.ai"
    zen_allowed_endpoints: str = "https://opencode.ai"
    zen_opencode_version: str = "1.18.0"
    # Kilo Gateway 免费层：标准 OpenAI 协议、无门禁、无凭证，端点白名单同样
    # 只为防误配。免费模型由上游 `isFree` 标记识别（见 provider/kilo/client.py）。
    kilo_api_endpoint: str = "https://api.kilo.ai/api/gateway"
    kilo_allowed_endpoints: str = "https://api.kilo.ai/api/gateway"
    # Qoder（阿里，COSY 私有协议）：真实账号渠道，端点白名单防止误把带 COSY
    # 签名的请求发往未授权主机。默认取国内版；国际版改这两个值并把国际版域名
    # 一并加入白名单。推理走 gateway.*，额度/登录走 openapi.*。
    qoder_api_endpoint: str = "https://openapi.qoder.com.cn"
    qoder_gateway_endpoint: str = "https://gateway.qoder.com.cn"
    qoder_allowed_endpoints: str = (
        "https://openapi.qoder.com.cn,https://gateway.qoder.com.cn,"
        "https://openapi.qoder.sh,https://api1.qoder.sh"
    )
    # CodeArts（华为云码道 / snap-access 盘古引擎）：AK/SK 签名渠道。白名单
    # 必须含 snap 引擎（推理/令牌/额度/签到）、STS（refresh）与门户（PKCE
    # 授权页）三个主机。
    codearts_api_endpoint: str = "https://snap-access.cn-north-4.myhuaweicloud.com"
    codearts_allowed_endpoints: str = (
        "https://snap-access.cn-north-4.myhuaweicloud.com,"
        "https://sts.cn-north-4.myhuaweicloud.com,"
        "https://codearts.huaweicloud.com"
    )
    # officeAce（华为云果办 / AgentArts）：OAuth + 开通校验走**控制面**
    # `agentarts.{region}.myhuaweicloud.com`；聊天走每账号各自的模型网关
    # `modelgw-XXXX.officeace.{region}.huaweicloud-agentarts.com`，该地址由
    # `client-permission-validate` 下发并存在凭证里（每个账号不同），故不在此配置，
    # 也不进白名单——它不是本服务能写错的值，而是上游签发的。这里只白名单控制面：
    # 带临时 AK/SK 签名的请求只会发往它。
    officeace_api_endpoint: str = "https://agentarts.cn-southwest-2.myhuaweicloud.com"
    officeace_allowed_endpoints: str = (
        "https://agentarts.cn-southwest-2.myhuaweicloud.com"
    )
    # 按渠道出站代理（启动期项，见 provider/proxy.py）：格式
    # `渠道=代理URL;渠道2=代理URL2`，协议 http/https/socks5/socks5h；留空 = 直连。
    # 代理作用于连接池，运行中改值需重启，故不做热更。
    provider_proxies: str = ""

    # 路由与调度
    # 默认模型：六条渠道都已上架 `deepseek-v4.1-flash`（CB / TRAE / CodeArts /
    # codebuddy_intl 同名，Qoder 走别名 `dfmodel`），故默认值取它而不是老模型
    default_model: str = "deepseek-v4.1-flash"
    # token 预刷新的提前量（小时）：距 access token 到期 ≤ 该值就提前换新。
    # 注意它对**短寿命渠道**会被自行封顶（CodeArts STS 只有 2h，见
    # `CodeArtsCredential.refresh_skew_cap_seconds`），所以调大它不会让短寿命
    # 渠道「提前更多」；反之把它调到远小于该渠道寿命的量级也没意义——真正的
    # 下界是预刷新任务的轮询周期，窗口窄于周期会整轮漏过导致凭证静默过期。
    refresh_skew_hours: int = 24
    # 到期排序窗口：把「距到期 ≤ 该秒数」的积分加总，作为选号第一排序指标（多者先用）；
    # CodeBuddy 是每日 100 积分 × N 的小包；≤0 关闭整套到期排序（次窗口一并失效）
    quota_expiry_window_seconds: int = 36 * 3600
    # 次要到期排序窗口：主窗口打平（含都为 0）时才比较，7 天覆盖一个完整的小包
    # 到期周期，避免只看 36h 而漏掉一周内仍会过期的积分；≤0 关闭该级
    quota_expiry_secondary_window_seconds: int = 7 * 86400
    # 会话粘性 TTL（秒）：同一对话（消息前缀延续）的多轮请求固定用同一凭证，
    # 对话进行中不换号（避免上游风控与丢失上游提示词缓存）；凭证出错仍会
    # 正常轮换，成功后重新粘定。≤0 关闭粘性
    conversation_sticky_seconds: int = 3600
    # token 到期预警阈值（秒）：access token 剩余时间低于该值时管理台标红。
    # 展示阈值，不参与调度；与 REFRESH_SKEW_HOURS 分开——预刷新窗口是「何时
    # 续期」，这里是「何时该看」。
    token_expiry_warning_seconds: int = 3600

    # 后台任务
    # token 预刷新轮询周期（分钟）。真正的「何时续期」由 REFRESH_SKEW_HOURS
    # 决定，但短寿命渠道会把它封顶（CodeArts STS 只有 2h，见
    # `CodeArtsCredential.refresh_skew_cap_seconds`）——此时轮询周期成了唯一
    # 保证：周期必须**窄于**封顶窗口，否则刷新窗口整轮落在两次轮询之间，凭证
    # 会一直拖到到期才刷（实测：封顶 30min、周期 60min 时在到期瞬间才刷，
    # 上游 401 + 管理台恒标红）。默认 30min 与封顶窗口同宽。
    refresh_interval_minutes: int = 30
    quota_probe_minutes: int = 60
    # 模型目录兜底刷新周期（分钟）：没有它，模型列表只在有人调 /v1/models /
    # Playground 时才按 TTL 刷新——纯 API 用法的部署（客户端自己缓存了模型
    # 列表）会让别名表与落盘快照一起变陈旧。取 30 分钟是跟着 zen 免费模型判活
    # 缓存（30 分钟）对齐：再密也不会让 zen 多探活一次，只是白打其余渠道的
    # /models。
    model_catalog_minutes: int = 30
    # 模型可用性探活周期（分钟）：探仍被负缓存（「上游无此模型」）与已判死的
    # (渠道, 模型)，一次最小对话。0 = 关闭（默认关闭）：探活会真发上游请求、
    # 消耗额度，是否开启应由部署者按额度余量决定。开启后建议 ≥60 分钟——
    # 负缓存本身 6h 起步，比它更密只是白耗额度。判死行也靠这条周期复探恢复
    # （判死即从列表摘掉，没有到期时间）。
    model_probe_minutes: int = 0
    # 成长中心（仅 CodeBuddy 有）：一轮领取的周期，以及是否允许不可逆动作
    # （抽奖/连登兑换/开 Buddy 盲盒/消耗补登卡）。这些动作无法撤销，
    # 需要保守部署时可关闭：关闭后仍会领取旅行礼物与任务奖励。
    growth_interval_minutes: int = 60
    growth_irreversible_actions: bool = True
    # 活跃上报（CodeBuddy 与 CodeBuddy 国际版）：默认关闭。给账号补发一条
    # 对话事件，续上成长中心连登天数/活跃地图，与积分、调度无关。官方条款禁止脚本
    # 篡改活动数据（处罚为取消资格并追回礼品），开启前请评估账号风险；上游改版即失效。
    activity_report_enabled: bool = False
    activity_report_hour: int = 10          # 本地（北京）时间整点窗口内执行一次
    # 每日签到的执行时点（本地时间，HH:MM，逗号分隔；留空 = 全天每 10 分钟
    # 检查一次的旧行为）。命中时点才跑一轮：上游签到本身幂等（回 ALREADY），
    # 但每次都要打 status+claim+回查三次，全天轮询纯属白打。抖动见下一项。
    checkin_times: str = ""
    # 签到抖动（分钟，0 = 不抖）：在时点**之前**提前一个随机偏移执行，避免
    # 一堆部署在同一分钟齐刷刷打上游。按「日期 + 时点」记去重键，抖动只改
    # 执行时刻、不改归属，不会让同一时点跑两次。
    checkin_jitter_minutes: int = 0
    # 服务端日志页（「任务与配置」→ 日志）：进程内环形缓冲的保留行数，
    # 0 = 关闭该页（端点返回 404）。只收本进程 logging 记录，不读文件。
    log_buffer_lines: int = 500
    # 运维告警：周期评估四类风险，命中即落 alert_events 并可选推送
    # webhook。webhook 留空 = 只留站内记录、不外推。
    alert_enabled: bool = True
    alert_webhook_url: str = ""
    alert_interval_minutes: int = 5         # 评估周期（分钟），下限 1
    alert_silence_minutes: int = 30         # 同一 (规则, 对象) 的静默窗（分钟）
    # 告警迟滞：连续命中 N 轮才告警、连续 M 轮未命中才确认恢复。
    # for_ticks=1 等价于不做进入迟滞（保持向后兼容的默认）；clear_ticks=2
    # 避免单轮抖动就把告警撤掉。恢复通知会写 alert_events.resolved_at 并外推。
    alert_for_ticks: int = 1
    alert_clear_ticks: int = 2
    # 池耗尽：ready 少于该值即告警（0 关闭该规则）
    alert_pool_ready_min: int = 1
    # 任务连续失败：连续失败达到该次数即告警（0 关闭该规则）
    alert_task_failures: int = 3
    # token 临近到期：剩余时间少于该小时数即告警（0 关闭该规则）
    alert_token_expiry_hours: int = 24
    # 上游错误率骤升：统计窗内失败占比超过该值、且样本数足够时告警（0 关闭）
    alert_error_rate_threshold: float = 0.5
    alert_error_rate_min_requests: int = 20
    alert_error_rate_window_minutes: int = 15
    # SQLite 备份后台任务：周期性把主库一致性快照（VACUUM INTO）写进
    # DATA_DIR/backups/，保留最近 BACKUP_KEEP 份。默认每日一次、留 7 份。
    # 备份周期取整日是因为库本身不大（几十 MB 量级），更密只是白占磁盘。
    backup_interval_minutes: int = 1440
    backup_keep: int = 7
    # 上游状态页聚合后台任务：并发抓各模型 / 网关提供方的公开状态接口，归一化
    # 后存内存快照，管理台「上游状态」页只读展示。**只做解释不做决策**（状态页
    # 滞后于我们自己的模型级冷却）。不落盘：状态分钟级易变，重启后拿旧快照
    # 比显示「尚未抓取」更危险。
    upstream_status_enabled: bool = True
    upstream_status_minutes: int = 5            # 抓取周期（分钟），下限 1
    # 单源 HTTP 超时（秒）：8 个源并发抓，慢的那个不该拖住整轮。
    upstream_status_timeout_seconds: float = 10.0
    pacer_min_seconds: float = 5
    pacer_max_seconds: float = 20
    # CodeBuddy 聊天最小间隔：同渠道同凭证的「顺序连发」之间的最小请求间隔，
    # 避开频率风控；0 关闭节流。按凭证分桶、桶内允许并发：同渠道同模型的
    # 并发请求不再被逐级串行化（旧实现把它们推到 +5s、+10s）；只有上一请求
    # 已结束、紧接着又来一个时才补足间隔。（注意 11128 主因是内容指纹风控，
    # 见 codebuddy_sanitize_channel_markers 与 TECHNICAL.md §3.2，调间隔救不了）
    codebuddy_chat_min_interval: float = 5
    # CodeBuddy 国际版聊天最小间隔：与国内站**各自独立**的节流器与阈值。
    # 不共享的原因与 zen/kilo 同：共用一个 Pacer 会让两站请求排进同一条队列，
    # 国际站请求白等国内站的间隔（反之亦然）。默认 5s 对齐国内站。
    codebuddy_intl_chat_min_interval: float = 5
    # Zen 聊天最小间隔：zen 是匿名免费层，没有 CB/TRAE 那种账号级频率风控，
    # 默认 0（不节流）。刻意**不**与 codebuddy_chat_min_interval 共享 pacer：
    # zen 排在 CB/TRAE 后面会白白等满最小间隔（并发/连发时每个请求 +5s）。
    # 若上游对匿名免费层限流，可在此调大。
    zen_chat_min_interval: float = 0
    # Kilo 聊天最小间隔：同为匿名免费层（网关级 200 req/h/IP），默认 0。
    # 与 zen 各自独立：两条免费渠道互不排队。
    kilo_chat_min_interval: float = 0
    # Qoder 聊天最小间隔：真实账号渠道，上游有账号级频率风控，默认 5s
    # （对齐 CodeBuddy）。与 CB/TRAE/zen/kilo 各自独立，互不排队。
    qoder_chat_min_interval: float = 5
    # CodeArts 聊天最小间隔：真实账号渠道，默认 5s；独立节流器。
    codearts_chat_min_interval: float = 5
    # officeAce 聊天最小间隔：真实账号渠道（单账号单用户条款），默认 0——
    # 上游没有像 CodeArts 那样「每 60s 最多 3 会话」的硬限，实测并发下
    # 直接放行即可；被限流时走伪 200 的 81111/81114（模型级短冷却），
    # 不需要靠本地节流兜。另有独立节流器，与其余渠道互不排队。
    officeace_chat_min_interval: float = 0
    # CodeArts 每账号在途并发上限：上游硬限「并发会话数 3」，超限的请求直接
    # 400 TM.00001041 并发超限（实测 77% 失败率的主因）。默认 3 对齐上游；
    # 0 关闭上限（回到「有在途就放行」，会再次击穿）。可热更。
    codearts_max_concurrency: int = 3
    # CodeArts 账号滑动窗口（秒，默认 60）：与上面的上限组合成「窗口内最多
    # 启动 N 次」。实测上游限制的不是「同时在途」而是「每账号每约 60s 最多
    # 3 个会话」——会话在 HTTP 流结束后仍滞留数十秒（打满 3 并发后，单请求
    # 直到约 68s 才恢复）。0 关闭窗口口径，退回纯在途上限。可热更。
    codearts_request_window_seconds: float = 60
    # 成本估算与模型目录（OpenRouter 公开接口，USD/百万 token）：人民币汇率
    # （1 USD = 该值 CNY）、目录刷新周期（分钟）与数据源 URL。汇率是运行时热更
    # 项（见 HOT_SETTINGS），来源是公开只读端点，无需密钥。一次抓取同时供成本
    # 估算、「模型列表」页与能力分（Artificial Analysis 指数）三处使用。
    usd_cny_rate: float = 6.70
    openrouter_catalog_minutes: int = 1440     # 模型目录刷新周期（分钟）：默认每日一次
    openrouter_models_url: str = "https://openrouter.ai/api/v1/models"
    # 内容风控自愈（11128）：出站 system/assistant 正文命中「伪装其他厂商
    # 官方客户端」指纹串时替换为占位符（客户端会话历史不受影响）。该拦截
    # 与凭证无关、换号无效，会话一旦带入指纹将持续 11128；false 关闭
    codebuddy_sanitize_channel_markers: bool = True
    # 模型列表黑名单（fnmatch glob，逗号分隔）：滤掉非用户模型与老模型，
    # 只影响 /v1/models 与 playground 列表，直连指定不受影响。
    # 模式按**归一后的对外写法**匹配（见 api/model_merge.py::_block_names）：原代号、
    # 原代号的归一键、展示名、展示名的归一键四种写法任一命中即滤——用户照列表
    # 里看到的 `kimi-k3` / `Kimi K3` 写就能生效，老规则按原代号写也仍生效。
    # 覆盖此值时为完全替换（含默认噪音规则），增删请重写全量。
    # 默认口径（2026-10-19 与线上生效值对齐）：**只展示当前在售的新模型**，
    # 三类一律滤掉——
    #   1. 上游内部/代理模型：custom_model_* / *sub*agent* / summary /
    #      browser_use_* / file_search_agent（chat 报 3003 / 非用户模型）；
    #      default 与 codebuddy_intl 的 default-model/fast-model/balanced-model/
    #      primary-model/deep-model、Qoder 的 auto 是各上游的「自动路由」占位。
    #   2. 已停售/被新一代取代的老模型：glm-4.* / glm-5 / glm-5.0* / glm-5.1 /
    #      glm-5.2* / glm-5v-* / glm-5-turbo、kimi-k2.*（含 k2.5/2.6/k2-thinking）、
    #      minimax-m2.5/m2.7/m3、deepseek-v3-* / deepseek-v4-*（v4.1 不受影响）、
    #      qwen3.7-*、gpt-5.4/5.5、hy3* / hy4-preview、hunyuan-*、
    #      seed-2.1* / seed-code-pro-* / seed-evolving、qwen3-vl-235b、
    #      openpangu 之外的开源小模型（nemotron-3* / lfm2* / laguna* /
    #      north-mini-code / inkling-small / xing4.0-29b-a4 / openrouter-models-router）。
    #   3. 实测不可用的噪音：glyph-cluster（Kilo 两次 121s 后 408）、
    #      hunyuan-*（含 hunyuan-image-* 的 400 11103 backend not supported）。
    # 刻意不加的：deepseek-v4.1-flash / kimi-k3 / glm-5.3* / mimo-v2.6* /
    #   qwen3.8-max / openpangu-2.0-*（在售主力，2026-10-19 实测正常 chat）。
    model_blocklist: str = (
        "custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,"
        "default,seed-code-pro-*,sagitta,aquila,glm-4.*,glm-5,glm-5-turbo,"
        "glm-5.0*,glm-5.1,glm-5v-*,kimi-k2.5,kimi-k2.6,kimi-k2-thinking,"
        "minimax-m2.5,deepseek-v3-*,hunyuan-*,hy3*,deepseek-v4-*,qwen3.7-*,auto,"
        "nemotron-3-*,glm-5.2*,nemotron-3*,lfm2*,north-mini-code,"
        "openrouter-models-router,fast,balanced,deep,primary,hy4-preview,"
        "inkling-small,minimax-m2.7,kimi-k2.*,minimax-m3,gpt-5.4,gpt-5.5,"
        "laguna*,qwen3-vl-235b,seed-2.1*,xing4.0-29b-a4,glyph-cluster,"
        "qwen3.8-flash,seed-evolving"
    )
    # 诊断：把 /v1 入口的原始请求体落到 data/dumps/（排查客户端差异用）
    dump_request_bodies: bool = False
    # 截断续写：上游以 finish_reason=length 截断时，同凭证自动续写，
    # 最多该次数后收尾；0 关闭。仅 length 触发（其余"空正文/代码块未闭合"
    # 判据经实测无支撑，未实现，见 TECHNICAL.md §3.4）
    auto_continue_max: int = 10
    # 非流式聚合整体超时（秒）：上游只支持流式，非流式由引擎聚合；上游连接
    # 半开停滞会让请求无限悬挂并占住凭证。超时按瞬态错误换号重试；≤0 关闭
    upstream_complete_timeout_seconds: int = 600
    # 流式首事件超时（秒）：上游接了连接却长时间不吐第一个**数据帧**时，
    # 在出帧前换号重试（此时切换安全，客户端还没收到任何内容）。注释帧/心跳
    # 不重置该计时。默认 0 = 关闭：实测慢上下文首字可达 40–71s，默认值会误杀
    # 正常请求，需在管理台显式打开（推荐 60）。可热更。
    stream_first_event_timeout_seconds: float = 0
    # 流式空闲超时（秒）：**已出帧**后上游长时间不再吐数据，只能以错误帧收尾
    # （半截输出不可回滚，换号会把两个账号的内容拼给客户端）。默认 0 = 关闭，
    # 推荐 300。可热更。
    stream_idle_timeout_seconds: float = 0
    # 短冷却「等待续上」（秒）：全池都在短冷却、没有立即可用候选时，若最短的
    # 剩余冷却不超过该值，则等待后重取一次（而不是直接 503）。0 = 关闭（快速
    # 失败）。等待期间不占在途租约，避免把并发请求堆成连接洪峰。可热更。
    short_cooldown_wait_seconds: float = 0
    # 非流式出口保活（秒）：`stream=false` 的请求在聚合完成前一个字节都不发，
    # 中间盒（CDN / 反代）的空闲超时会直接切连接。设为 N > 0 时，N 秒内没
    # 完成就改走 chunked，先吐空白字节再补 JSON 正文——空白是合法 JSON 前导
    # （RFC 8259 §2 `JSON-text = ws value ws`），解析器照常工作。
    # 默认 0 = 关闭：本部署 nginx `proxy_read_timeout 3600s` + frp TCP，
    # 链路上没有短空闲超时，实测 30 天 >60s 的成功请求 1741 个全部成功。
    # 只有在 CDN 后面（Cloudflare 100s 等）才需要打开，推荐 15。可热更。
    nonstream_keepalive_seconds: float = 0
    # 单账号在途请求上限（全局，跨渠道）：同一凭证并发达到该值后不再参与选号，
    # 避免把单账号打进上游限流/封号；0 = 不限。租约在请求结束后释放，另有
    # 600s TTL 兜底（见 src/engine/inflight.py）。
    max_inflight_per_credential: int = 0
    # 上下文压缩：按模型目录里的输入上限裁剪过长对话，避免撞上游硬
    # 限制（CodeBuddy 11115 prompt is too long）。enabled=false 完全关闭；
    # 目录里查不到上限的模型不受影响（宁可不裁剪也不猜一个数字去砍上下文）。
    # reserve=为模型回复预留的输出 token；safety_ratio=按上限的百分比留估算
    # 余量；min_keep=无论多长都保留的最近消息条数。
    context_compress_enabled: bool = True
    context_compress_reserve_tokens: int = 4096
    context_compress_min_keep_messages: int = 4
    context_compress_safety_ratio: float = 0.95
    # 跨渠道 fallback 链：请求模型在主渠道全部不可用时，按「兼容组」
    # 依次回退到组内其他模型（组内成员自行带上 @渠道 或依赖目录收窄）。
    # 格式 `组名=成员1,成员2;组名2=成员3`；留空 = 不启用（保持原行为）。
    model_fallback_groups: str = ""
    # 用户自定义模型别名（对外名 → 目标模型/渠道）：给客户端一个稳定对外名，
    # 上游改 id 时只改这里。格式 `对外名=模型@渠道;...`；留空 = 不启用。
    # 与真实对外 id 或自动别名撞车视为配置错误（不静默覆盖）。可热更。
    model_aliases: str = ""
    # Prometheus 指标出口（GET /metrics）：默认开启。与 /healthz 同口径**不鉴权**，
    # 指标已禁用高基数标签（不含凭证 ID / 用户名）。关闭后端点返回 404。可热更。
    metrics_enabled: bool = True
    # 请求级结构化日志（logfmt，见 src/webapp/access_log.py）：默认开启。
    # 只记 /api、/v1、/authorize 这些业务请求，每行含 request_id / 路由模板 /
    # 状态码 / 耗时；**不写 query string、请求体、用户名**（脱敏口径见
    # PROPOSAL §6）。关闭后回到只有 uvicorn 的裸 access log。可热更。
    request_log_enabled: bool = True
    # 全局剥离响应里的思考内容（`EventKind.REASONING`）：默认关闭。开启后
    # **只过滤出站响应**、不改写请求体——上游仍照常思考（`reasoning_effort`
    # 原样透传），只是不再把思考交给客户端。刻意不做请求侧改写：实测 CB
    # `enable_thinking:false` 被上游忽略（仍产 reasoning 且计入 max_tokens），
    # 而缺 `reasoning_effort` 反而退化成「可见推演」（思考混进 delta.content）；
    # 其余渠道的请求侧接受性未核实，写请求体就是凭猜测改客户端意图。可热更。
    force_disable_thinking: bool = False
    # 出站 `reasoning_effort` 档位映射（默认空 = 原样透传）。格式
    # `渠道=from:to,from:to;*=from:to`，渠道取 KNOWN_PROVIDERS，`*` 为兜底。
    # 只改写客户端**显式**给的档位（渠道各自的 setdefault 补值不动）；
    # 目标 `none` = 删除该键（走渠道默认）。只给机制不给策略：内置默认矩阵
    # 未经上游核实，写了就是编造。可热更。
    reasoning_effort_map: str = ""
    # Anthropic 入站 `thinking.budget_tokens` → `reasoning_effort` 的映射
    # （默认空 = 完全不映射，`thinking` 照旧被忽略）。格式
    # `预算:档位,预算:档位,disabled:档位`：`disabled` 匹配 `thinking.type==
    # "disabled"`，其余按「阈值 ≤ budget_tokens 中最大的一档」取值，低于最小
    # 阈值则不映射。Anthropic 只规定 budget ≥1024、未给档位对应表，故阈值表
    # 必须由使用者显式给出，不内置默认。可热更。
    anthropic_thinking_efforts: str = ""

    @cached_property
    def admin_set(self) -> frozenset[str]:
        return frozenset(u.strip() for u in self.admin_usernames.split(",") if u.strip())

    @cached_property
    def allowed_endpoints(self) -> tuple[str, ...]:
        return tuple(e.strip() for e in self.codebuddy_allowed_endpoints.split(",") if e.strip())

    @cached_property
    def codebuddy_intl_allowed(self) -> tuple[str, ...]:
        return tuple(e.strip().rstrip("/")
                     for e in self.codebuddy_intl_allowed_endpoints.split(",")
                     if e.strip())

    @cached_property
    def zen_allowed(self) -> tuple[str, ...]:
        return tuple(e.strip().rstrip("/") for e in self.zen_allowed_endpoints.split(",")
                     if e.strip())

    @cached_property
    def kilo_allowed(self) -> tuple[str, ...]:
        return tuple(e.strip().rstrip("/") for e in self.kilo_allowed_endpoints.split(",")
                     if e.strip())

    @cached_property
    def qoder_allowed(self) -> tuple[str, ...]:
        return tuple(e.strip().rstrip("/") for e in self.qoder_allowed_endpoints.split(",")
                     if e.strip())

    @cached_property
    def codearts_allowed(self) -> tuple[str, ...]:
        return tuple(e.strip().rstrip("/") for e in self.codearts_allowed_endpoints.split(",")
                     if e.strip())

    @cached_property
    def officeace_allowed(self) -> tuple[str, ...]:
        return tuple(e.strip().rstrip("/") for e in self.officeace_allowed_endpoints.split(",")
                     if e.strip())

    @cached_property
    def blocklist_patterns(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.model_blocklist.split(",") if p.strip())

    def is_admin(self, username: str) -> bool:
        return username in self.admin_set

    @property
    def db_path(self) -> str:
        import os

        return os.path.join(self.data_dir, "coding2api.sqlite3")


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """构造 Settings；env 非空时优先取传入值（测试用）。"""
    if env is None:
        return Settings()  # type: ignore[call-arg]
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def validate_endpoint_allowed(endpoint: str, settings: Settings) -> bool:
    """上游地址必须落在白名单内，否则拒绝发出真实 Token（PROPOSAL §6）。"""
    return endpoint.strip() in settings.allowed_endpoints


def validate_codebuddy_intl_endpoint_allowed(endpoint: str, settings: Settings) -> bool:
    """国际站端点白名单校验（防把国内站 Token 发往国际站或反之）。

    **白名单本身也校验**：PROPOSAL §2.7「`CODEBUDDY_INTL_ALLOWED_ENDPOINTS`
    默认只有国际站主机，填国内站启动时报错（跨站发送即凭证泄露）」。
    只校验「端点在白名单里」不够——把国内站主机**加进**国际站白名单后，
    端点校验会通过，Token 照样发往国内站（国际站账号的 Token 发到国内站
    网关 = 跨站泄露）。故白名单里出现国内站主机时一律拒绝。
    """
    allowed = settings.codebuddy_intl_allowed
    if any(_is_codebuddy_cn_host(entry) for entry in allowed):
        return False
    return endpoint.strip().rstrip("/") in allowed


def _is_codebuddy_cn_host(endpoint: str) -> bool:
    """该地址是不是国内站主机（`copilot.tencent.com`）。

    比主机名而非整串：配置里可能带路径或端口（`https://copilot.tencent.com:443/`），
    整串比较会漏判。国际站白名单里出现国内站主机 = 跨站发送，必须拒绝。
    """
    text = endpoint.strip()
    if text.startswith(("http://", "https://")):
        text = text.split("://", 1)[1]
    host = text.split("/", 1)[0].split(":", 1)[0].strip().lower()
    return host == _CODEBUDDY_CN.split("://", 1)[1].lower()


def validate_zen_endpoint_allowed(endpoint: str, settings: Settings) -> bool:
    """Zen 端点白名单校验（防误配；Zen 不携带真实 Token，风险面小于 CB）。"""
    return endpoint.strip().rstrip("/") in settings.zen_allowed


def validate_kilo_endpoint_allowed(endpoint: str, settings: Settings) -> bool:
    """Kilo 端点白名单校验（防误配；Kilo 不携带真实 Token，风险面小于 CB）。"""
    return endpoint.strip().rstrip("/") in settings.kilo_allowed


def validate_qoder_endpoint_allowed(endpoint: str, settings: Settings) -> bool:
    """Qoder 端点白名单校验（防把带 COSY 签名的请求发往未授权主机）。"""
    return endpoint.strip().rstrip("/") in settings.qoder_allowed


def validate_codearts_endpoint_allowed(endpoint: str, settings: Settings) -> bool:
    """CodeArts 端点白名单校验（防把 AK/SK 签名请求发往未授权主机）。"""
    return endpoint.strip().rstrip("/") in settings.codearts_allowed


def validate_officeace_endpoint_allowed(endpoint: str, settings: Settings) -> bool:
    """officeAce 控制面端点白名单校验（防把 AK/SK 签名请求发往未授权主机）。"""
    return endpoint.strip().rstrip("/") in settings.officeace_allowed


def live(value: Any) -> Callable[[], Any]:
    """把「标量或零参 callable」统一成取当前值的零参 callable。

    热更消费方（调度器 / 节流器 / 后台任务 / 执行器）持有的都是这个封装：
    生产装配传零参 lambda 实时读运行时覆盖层，测试仍可传标量。两条路径
    共用同一套逻辑，不必为「可热更」把每个构造点都改成传 callable。
    """
    if callable(value):
        return value
    return lambda: value
