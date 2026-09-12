"""CodeBuddy 上游技术常量与请求头构造。

请求头规则来自 codebuddy2api 的逆向实现（AGENTS.md 明确约束，勿改）：
- X-Domain 与 Host 必须由同一个当前 API endpoint 派生
- X-Department-Info 的部门名必须 UTF-8 百分号编码（HTTP 头只接受 ASCII）
- 手动 bearer-only 凭证不要求 account_uid / 过期时间 / refresh token
"""

from __future__ import annotations

import platform
import secrets
import uuid
from urllib.parse import quote, urlsplit

CN_ENDPOINT = "https://copilot.tencent.com"
INTL_ENDPOINT = "https://www.codebuddy.ai"

CLI_VERSION = "2.107.0"
OPENAI_JS_PACKAGE_VERSION = "6.25.0"
NODE_RUNTIME_VERSION = "v24.11.1"
EP_CHAT = "/v2/chat/completions"
EP_AUTH_STATE = "/v2/plugin/auth/state"
EP_AUTH_TOKEN = "/v2/plugin/auth/token"
EP_LOGIN_ACCOUNT = "/v2/plugin/login/account"
EP_ACCOUNTS = "/v2/plugin/accounts"
EP_SWITCH_ENTERPRISE = "/v2/plugin/login/enterprise"
EP_TOKEN_REFRESH = "/v2/plugin/auth/token/refresh"
EP_USER_RESOURCE = "/v2/billing/meter/get-user-resource"
EP_ENTERPRISE_USAGE = "/v2/billing/meter/get-enterprise-user-usage"
EP_DAILY_CHECKIN = "/billing/meter/daily-checkin"

QUOTA_PRODUCT_CODE = "codebuddy"
CODEBUDDY_IDE_VERSION = "1.42.0"


QUOTA_RANGE_END = "2099-12-31 23:59:59"


def host_of(endpoint: str) -> str:
    return urlsplit(endpoint).netloc


def encode_department(name: str) -> str:
    """部门名按 UTF-8 百分号编码（不能把中文直接塞进 HTTP 头）。"""
    return quote(name, safe="")


def _stainless_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x64"
    return machine or "x64"


def _stainless_os() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "MacOS"
    if system == "linux":
        return "Linux"
    if system == "windows":
        return "Windows"
    return platform.system() or "Linux"


def generate_headers(
    *,
    endpoint: str,
    bearer_token: str,
    user_id: str | None = None,
    account_uid: str | None = None,
    domain: str | None = None,
    enterprise_id: str | None = None,
    department_full_name: str | None = None,
    quota_only: bool = False,
) -> dict[str, str]:
    """构造上游请求头。

    quota_only=True 用于手动凭证的「仅供额度探测」场景：只切换额度接口，
    绝不构造或发送企业上下文头（AGENTS.md 约束）。
    """
    host = domain or host_of(endpoint)
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        # OpenAI JS SDK 指纹（官方 CLI 用 openai@js 发请求，腾讯校验这些头）
        "x-stainless-arch": _stainless_arch(),
        "x-stainless-lang": "js",
        "x-stainless-os": _stainless_os(),
        "x-stainless-package-version": OPENAI_JS_PACKAGE_VERSION,
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": NODE_RUNTIME_VERSION,
        # 会话链路 ID：原实现每次请求随机生成，缺失会触发 11128 渠道风控
        "X-Conversation-ID": str(uuid.uuid4()),
        "X-Conversation-Request-ID": secrets.token_hex(16),
        "X-Conversation-Message-ID": uuid.uuid4().hex,
        "X-Request-ID": uuid.uuid4().hex,
        "X-Agent-Intent": "craft",
        "X-Agent-Purpose": "conversation",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "CLI",
        "X-IDE-Version": CLI_VERSION,
        "User-Agent": f"CLI/{CLI_VERSION} CodeBuddy/{CLI_VERSION}",
        "X-Private-Data": "false",
        "X-CodeBuddy-Request": "1",
        "X-Product": "SaaS",
        "X-Domain": host,
        "Host": host,
    }
    if quota_only:
        return headers
    # OAuth 凭证优先用 account_uid，否则回退 user_id
    effective_user = account_uid or user_id
    if effective_user:
        headers["X-User-Id"] = effective_user
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    if department_full_name:
        headers["X-Department-Info"] = encode_department(department_full_name)
    return headers


def auth_start_headers(host: str) -> dict[str, str]:
    """OAuth state 请求头（无 Authorization，走匿名通道）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": f"CLI/{CLI_VERSION} CodeBuddy/{CLI_VERSION}",
        "X-Product": "SaaS",
        "X-Domain": host,
        "X-B3-Sampled": "1",
        "X-No-Authorization": "true",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
    }
