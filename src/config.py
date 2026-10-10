"""启动配置：pydantic-settings 绑定 env（T-Q3）。

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
    # 必须含 snap 引擎（推理/令牌）、STS（refresh）、福利网关（模型/领取/余额）
    # 与门户（PKCE 授权页）四个主机。
    codearts_api_endpoint: str = "https://snap-access.cn-north-4.myhuaweicloud.com"
    codearts_allowed_endpoints: str = (
        "https://snap-access.cn-north-4.myhuaweicloud.com,"
        "https://sts.cn-north-4.myhuaweicloud.com,"
        "https://opengw.developer.huaweicloud.com,"
        "https://codearts.huaweicloud.com"
    )
    # 按渠道出站代理（P1-6，启动期项，见 provider/proxy.py）：格式
    # `渠道=代理URL;渠道2=代理URL2`，协议 http/https/socks5/socks5h；留空 = 直连。
    # 代理作用于连接池，运行中改值需重启，故不做热更。
    provider_proxies: str = ""

    # 路由与调度
    default_model: str = "glm-5.2"
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
    # 成长中心（仅 CodeBuddy 有）：一轮领取的周期，以及是否允许不可逆动作
    # （抽奖/连登兑换/开 Buddy 盲盒/消耗补登卡）。这些动作无法撤销，
    # 需要保守部署时可关闭：关闭后仍会领取旅行礼物与任务奖励。
    growth_interval_minutes: int = 60
    growth_irreversible_actions: bool = True
    # 活跃上报（B1.7，仅 CodeBuddy）：默认关闭。给账号补发一条对话事件，续上
    # 成长中心连登天数/活跃地图，与积分、调度无关。官方条款禁止脚本篡改活动
    # 数据（处罚为取消资格并追回礼品），开启前请评估账号风险；上游改版即失效。
    activity_report_enabled: bool = False
    activity_report_hour: int = 10          # 本地（北京）时间整点窗口内执行一次
    # 运维告警（P1-7）：周期评估四类风险，命中即落 alert_events 并可选推送
    # webhook。webhook 留空 = 只留站内记录、不外推。
    alert_enabled: bool = True
    alert_webhook_url: str = ""
    alert_interval_minutes: int = 5         # 评估周期（分钟），下限 1
    alert_silence_minutes: int = 30         # 同一 (规则, 对象) 的静默窗（分钟）
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
    pacer_min_seconds: float = 5
    pacer_max_seconds: float = 20
    # CodeBuddy 聊天最小间隔：同渠道同凭证的「顺序连发」之间的最小请求间隔，
    # 避开频率风控；0 关闭节流。按凭证分桶、桶内允许并发：同渠道同模型的
    # 并发请求不再被逐级串行化（旧实现把它们推到 +5s、+10s）；只有上一请求
    # 已结束、紧接着又来一个时才补足间隔。（注意 11128 主因是内容指纹风控，
    # 见 codebuddy_sanitize_channel_markers 与 TECHNICAL.md §3.2，调间隔救不了）
    codebuddy_chat_min_interval: float = 5
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
    # CodeArts 每账号在途并发上限：上游硬限「并发会话数 3」，超限的请求直接
    # 400 TM.00001041 并发超限（实测 77% 失败率的主因）。默认 3 对齐上游；
    # 0 关闭上限（回到「有在途就放行」，会再次击穿）。可热更。
    codearts_max_concurrency: int = 3
    # CodeArts 账号滑动窗口（秒，默认 60）：与上面的上限组合成「窗口内最多
    # 启动 N 次」。实测上游限制的不是「同时在途」而是「每账号每约 60s 最多
    # 3 个会话」——会话在 HTTP 流结束后仍滞留数十秒（打满 3 并发后，单请求
    # 直到约 68s 才恢复）。0 关闭窗口口径，退回纯在途上限。可热更。
    codearts_request_window_seconds: float = 60
    # 成本估算（models.dev 刊例价，USD/百万 token）：人民币汇率（1 USD = 该值 CNY）
    # 与模型列表刷新周期（分钟）。汇率是运行时热更项（见 HOT_SETTINGS），来源
    # 是公开只读端点，无需密钥。
    usd_cny_rate: float = 6.70
    price_catalog_minutes: int = 1440      # 模型列表刷新周期（分钟）：默认每日一次
    models_dev_url: str = "https://models.dev/api.json"
    # 能力排行（Artificial Analysis 指数，经 OpenRouter 公开接口分发，无需密钥）：
    # 拉取周期（分钟）与数据源 URL。指数变动很慢，默认每日一次；拉取失败只是
    # 没有分数徽章，不影响模型列表。URL 可覆盖以便测试/内网镜像。
    benchmark_catalog_minutes: int = 1440
    openrouter_models_url: str = "https://openrouter.ai/api/v1/models"
    # 内容风控自愈（11128）：出站 system/assistant 正文命中「伪装其他厂商
    # 官方客户端」指纹串时替换为占位符（客户端会话历史不受影响）。该拦截
    # 与凭证无关、换号无效，会话一旦带入指纹将持续 11128；false 关闭
    codebuddy_sanitize_channel_markers: bool = True
    # 模型列表黑名单（fnmatch glob，逗号分隔）：滤掉非用户模型与老模型，
    # 只影响 /v1/models 与 playground 列表，直连指定不受影响。
    # 模式按**归一后的对外写法**匹配（见 api/models.py::_block_names）：原代号、
    # 原代号的归一键、展示名、展示名的归一键四种写法任一命中即滤——用户照列表
    # 里看到的 `kimi-k3` / `Kimi K3` 写就能生效，老规则按原代号写也仍生效。
    # 覆盖此值时为完全替换（含默认噪音规则），增删请重写全量。
    # B1.6 实测（2026-09-21，两边上游真实清单）补入的内部/不可用模型：
    #   custom_model_* / *sub*agent* / summary / browser_use_* / file_search_agent
    #   为上游内部或代理模型（chat 报 3003 / 非用户模型）
    #   default 实测 HTTP 200 但零内容（不可用于 chat）
    #   hunyuan-image-* 实测 HTTP 400 11103「backend is not supported」
    # 2026-10-10 复测六渠道全量清单后补入三条「上游已下架」：
    #   hy4-preview-x（CB 400 11102 service info not found；同名的 hy4-preview
    #     仍可用，故写全名不用 glob）
    #   qwen3.8-flash（Qoder qfmodel 400「Execution failed: null」；CB/TRAE 的
    #     qwen3.8-max 正常，不受影响）
    #   glyph-cluster（Kilo stealth/glyph-cluster 两次 121s 后 408；Kilo 其余正常）
    # 刻意不加的：*-volc（deepseek-v3-2-volc 实测正常 chat）、aquila/sagitta/
    #   seed-code-pro-0430（TRAE 实测均正常 chat）、glm-5.0-turbo/glm-5v-turbo/
    #   hunyuan-chat/hy3/auto（2026-10-10 复测均正常 chat）
    model_blocklist: str = (
        "custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,"
        "default,hunyuan-image-*,hy4-preview-x,qwen3.8-flash,glyph-cluster"
    )
    # 诊断：把 /v1 入口的原始请求体落到 data/dumps/（排查客户端差异用）
    dump_request_bodies: bool = False
    # 截断续写（B1.4）：上游以 finish_reason=length 截断时，同凭证自动续写，
    # 最多该次数后收尾；0 关闭。仅 length 触发（其余"空正文/代码块未闭合"
    # 判据经实测无支撑，未实现，见 TECHNICAL.md §3.4）
    auto_continue_max: int = 10
    # 非流式聚合整体超时（秒）：上游只支持流式，非流式由引擎聚合；上游连接
    # 半开停滞会让请求无限悬挂并占住凭证。超时按瞬态错误换号重试；≤0 关闭
    upstream_complete_timeout_seconds: int = 600
    # 上下文压缩（P0-2）：按模型目录里的输入上限裁剪过长对话，避免撞上游硬
    # 限制（CodeBuddy 11115 prompt is too long）。enabled=false 完全关闭；
    # 目录里查不到上限的模型不受影响（宁可不裁剪也不猜一个数字去砍上下文）。
    # reserve=为模型回复预留的输出 token；safety_ratio=按上限的百分比留估算
    # 余量；min_keep=无论多长都保留的最近消息条数。
    context_compress_enabled: bool = True
    context_compress_reserve_tokens: int = 4096
    context_compress_min_keep_messages: int = 4
    context_compress_safety_ratio: float = 0.95
    # 跨渠道 fallback 链（P1-5）：请求模型在主渠道全部不可用时，按「兼容组」
    # 依次回退到组内其他模型（组内成员自行带上 @渠道 或依赖目录收窄）。
    # 格式 `组名=成员1,成员2;组名2=成员3`；留空 = 不启用（保持原行为）。
    model_fallback_groups: str = ""

    @cached_property
    def admin_set(self) -> frozenset[str]:
        return frozenset(u.strip() for u in self.admin_usernames.split(",") if u.strip())

    @cached_property
    def allowed_endpoints(self) -> tuple[str, ...]:
        return tuple(e.strip() for e in self.codebuddy_allowed_endpoints.split(",") if e.strip())

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
    """上游地址必须落在白名单内，否则拒绝发出真实 Token（PROPOSAL §8）。"""
    return endpoint.strip() in settings.allowed_endpoints


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


def live(value: Any) -> Callable[[], Any]:
    """把「标量或零参 callable」统一成取当前值的零参 callable（B3.2）。

    热更消费方（调度器 / 节流器 / 后台任务 / 执行器）持有的都是这个封装：
    生产装配传零参 lambda 实时读运行时覆盖层，测试仍可传标量。两条路径
    共用同一套逻辑，不必为「可热更」把每个构造点都改成传 callable。
    """
    if callable(value):
        return value
    return lambda: value
