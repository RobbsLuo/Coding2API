"""启动配置：pydantic-settings 绑定 env（T-Q3）。

PROPOSAL §8 的 14 项配置在此定型，缺必填项在进程启动阶段失败。
"""

from __future__ import annotations

from functools import cached_property

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
    log_level: str = "INFO"

    # 上游（白名单内的地址才可接收真实 Token）
    codebuddy_api_endpoint: str = _CODEBUDDY_CN
    codebuddy_allowed_endpoints: str = f"{_CODEBUDDY_CN},{_CODEBUDDY_INTL}"

    # 路由与调度
    default_model: str = "glm-5.2"
    refresh_skew_hours: int = 24

    # 后台任务
    checkin_hour: int = 9
    quota_probe_minutes: int = 60
    pacer_min_seconds: float = 5
    pacer_max_seconds: float = 20
    # CodeBuddy 聊天最小间隔：腾讯频率风控（11128）在连续快速请求时触发，
    # 实测 ≥5s 间隔稳定避开；0 关闭节流
    codebuddy_chat_min_interval: float = 5
    # 诊断：把 /v1 入口的原始请求体落到 data/dumps/（排查客户端差异用）
    dump_request_bodies: bool = False

    @cached_property
    def admin_set(self) -> frozenset[str]:
        return frozenset(u.strip() for u in self.admin_usernames.split(",") if u.strip())

    @cached_property
    def allowed_endpoints(self) -> tuple[str, ...]:
        return tuple(e.strip() for e in self.codebuddy_allowed_endpoints.split(",") if e.strip())

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
