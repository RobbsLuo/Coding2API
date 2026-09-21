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

    # 上游（白名单内的地址才可接收真实 Token）
    codebuddy_api_endpoint: str = _CODEBUDDY_CN
    codebuddy_allowed_endpoints: str = f"{_CODEBUDDY_CN},{_CODEBUDDY_INTL}"

    # 路由与调度
    default_model: str = "glm-5.2"
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

    # 后台任务
    quota_probe_minutes: int = 60
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
    pacer_min_seconds: float = 5
    pacer_max_seconds: float = 20
    # CodeBuddy 聊天最小间隔：与 TRAE 共享的最小请求间隔，避开两渠道各自的
    # 频率风控；0 关闭节流。（注意 11128 主因是内容指纹风控，见
    # codebuddy_sanitize_channel_markers 与 TECHNICAL.md §3.2，调间隔救不了）
    codebuddy_chat_min_interval: float = 5
    # 内容风控自愈（11128）：出站 system/assistant 正文命中「伪装其他厂商
    # 官方客户端」指纹串时替换为占位符（客户端会话历史不受影响）。该拦截
    # 与凭证无关、换号无效，会话一旦带入指纹将持续 11128；false 关闭
    codebuddy_sanitize_channel_markers: bool = True
    # 模型列表黑名单（fnmatch glob，逗号分隔）：滤掉非用户模型与老模型，
    # 只影响 /v1/models 与 playground 列表，直连指定不受影响。
    # 覆盖此值时为完全替换（含默认噪音规则），增删请重写全量。
    # B1.6 实测（2026-09-21，两边上游真实清单）补入的内部/不可用模型：
    #   custom_model_* / *sub*agent* / summary / browser_use_* / file_search_agent
    #   为上游内部或代理模型（chat 报 3003 / 非用户模型）
    #   default 实测 HTTP 200 但零内容（不可用于 chat）
    #   hunyuan-image-* 实测 HTTP 400 11103「backend is not supported」
    # 刻意不加的：*-volc（deepseek-v3-2-volc 实测正常 chat）、aquila/sagitta/
    #   seed-code-pro-0430（TRAE 实测均正常 chat）
    model_blocklist: str = (
        "custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,"
        "default,hunyuan-image-*"
    )
    # 诊断：把 /v1 入口的原始请求体落到 data/dumps/（排查客户端差异用）
    dump_request_bodies: bool = False
    # 截断续写（B1.4）：上游以 finish_reason=length 截断时，同凭证自动续写，
    # 最多该次数后收尾；0 关闭。仅 length 触发（其余"空正文/代码块未闭合"
    # 判据经实测无支撑，未实现，见 TECHNICAL.md §3.4）
    auto_continue_max: int = 10

    @cached_property
    def admin_set(self) -> frozenset[str]:
        return frozenset(u.strip() for u in self.admin_usernames.split(",") if u.strip())

    @cached_property
    def allowed_endpoints(self) -> tuple[str, ...]:
        return tuple(e.strip() for e in self.codebuddy_allowed_endpoints.split(",") if e.strip())

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


def live(value: Any) -> Callable[[], Any]:
    """把「标量或零参 callable」统一成取当前值的零参 callable（B3.2）。

    热更消费方（调度器 / 节流器 / 后台任务 / 执行器）持有的都是这个封装：
    生产装配传零参 lambda 实时读运行时覆盖层，测试仍可传标量。两条路径
    共用同一套逻辑，不必为「可热更」把每个构造点都改成传 callable。
    """
    if callable(value):
        return value
    return lambda: value
