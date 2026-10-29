"""CodeBuddy 上游技术常量与请求头构造。

请求头规则来自 codebuddy2api 的逆向实现（AGENTS.md 明确约束，勿改）：
- X-Domain 与 Host 必须由同一个当前 API endpoint 派生
- X-Department-Info 的部门名必须 UTF-8 百分号编码（HTTP 头只接受 ASCII）
- 手动 bearer-only 凭证不要求 account_uid / 过期时间 / refresh token
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import secrets
import time
import uuid
from urllib.parse import quote, urlsplit

CN_ENDPOINT = "https://copilot.tencent.com"
INTL_ENDPOINT = "https://www.codebuddy.ai"

CLI_VERSION = "2.107.0"
OPENAI_JS_PACKAGE_VERSION = "6.25.0"
NODE_RUNTIME_VERSION = "v24.11.1"

# 国际站指纹：国内站这套常量（2.107.0）已稳定工作两年，不动；国际站自
# 2026-10-15 加渠道校验后，上游会拿「宣称的 CLI 版本 / SDK / Node」与随包
# 发布的版本比对——长期钉死一个旧版本本身就是自动化特征（第三方实现
# PanRouter 的 intl executor 原文：a handful of static headers, one IDE/version
# string forever, huge repeated payloads）。这里取官方 2.17x 实测值，只作用于
# 国际站，出问题可单独回退。
INTL_CLI_VERSION = "2.178.0"
INTL_OPENAI_JS_PACKAGE_VERSION = "7.39.0"
INTL_NODE_RUNTIME_VERSION = "v26.7.0"
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
EP_CHECKIN_STATUS = "/billing/meter/checkin-activity-status"

CODEBUDDY_IDE_VERSION = "1.42.0"


QUOTA_RANGE_END = "2099-12-31 23:59:59"


def host_of(endpoint: str) -> str:
    return urlsplit(endpoint).netloc


def encode_department(name: str) -> str:
    """部门名按 UTF-8 百分号编码（不能把中文直接塞进 HTTP 头）。"""
    return quote(name, safe="")


# UUID v1 的 node 段：官方 CLI 用同一个固定值（PanRouter 原文 “constant node
# id, as the CLI uses”）。v1 的价值是「带创建时间 + 机器节点」；v4 两者都没有，
# 一眼可判为伪造。
_UUID_V1_NODE = "426614174000"

# 会话 → 首次出现时刻。官方 CLI 的 conversation id 在对话创建时固定、之后不再
# 变化；逐请求随机等于「每问一句就开一个新对话」。进程内记忆即可：重启换一批
# id 只影响一次，不影响正确性。
_CONVERSATION_CREATED_AT: dict[str, float] = {}
_CONVERSATION_CACHE_MAX = 1024


def conversation_uuid(seed: str, *, now: float | None = None) -> str:
    """会话种子 → 稳定 UUID v1：同一 seed 在进程内每次得到同一个 id。"""
    created = _CONVERSATION_CREATED_AT.get(seed)
    if created is None:
        created = time.time() if now is None else now
        if len(_CONVERSATION_CREATED_AT) >= _CONVERSATION_CACHE_MAX:
            # 无界增长会长期占内存；会话 id 只是指纹，清空只让仍在进行的
            # 对话换一次 id（不影响正确性，宁可清空也不做 LRU 记账）。
            _CONVERSATION_CREATED_AT.clear()
        _CONVERSATION_CREATED_AT[seed] = created
    ticks = int(created * 10_000_000) + 0x01B21DD213814000
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    clock_seq = (int.from_bytes(digest[:2], "big") & 0x3FFF) | 0x8000
    return (
        f"{ticks & 0xFFFFFFFF:08x}-"
        f"{(ticks >> 32) & 0xFFFF:04x}-"
        f"{((ticks >> 48) & 0x0FFF) | 0x1000:04x}-"
        f"{clock_seq:04x}-{_UUID_V1_NODE}")


def uid_from_token(token: str) -> str:
    """JWT `sub` → 账号 uid；不是 JWT / 解不出 / 无 sub 返回空串。

    只解 payload 不验签：验签是上游的事，这里只把 token 自带的账号标识带给
    上游。官方 CLI 每个请求都发 `X-User-Id`（第三方逆向一致结论），缺它时
    请求无法被关联到账号主体，是国际站风控的一个自动化信号。
    """
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    payload = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(claims, dict):
        return ""
    sub = claims.get("sub")
    return sub if isinstance(sub, str) else ""


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
    intl: bool = False,
    conversation_seed: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    """构造上游请求头。

    quota_only=True 用于手动凭证的「仅供额度探测」场景：只切换额度接口，
    绝不构造或发送企业上下文头（AGENTS.md 约束）。

    intl=True 切到国际站指纹：版本常量换成 INTL_*、会话 id 按
    `conversation_seed` 稳定化（UUID v1）、补官方 CLI 会发的会话/模型头。
    国内站传 False，逐字节保持历史形态。
    """
    host = domain or host_of(endpoint)
    cli_version = INTL_CLI_VERSION if intl else CLI_VERSION
    sdk_version = (INTL_OPENAI_JS_PACKAGE_VERSION if intl
                   else OPENAI_JS_PACKAGE_VERSION)
    node_version = INTL_NODE_RUNTIME_VERSION if intl else NODE_RUNTIME_VERSION
    request_id = uuid.uuid4().hex
    # 国际站：同一会话稳定（官方 CLI 用 v1），国内站保持逐请求随机
    conversation_id = (conversation_uuid(conversation_seed) if intl and conversation_seed
                       else str(uuid.uuid4()))
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        # OpenAI JS SDK 指纹（官方 CLI 用 openai@js 发请求，腾讯校验这些头）
        "x-stainless-arch": _stainless_arch(),
        "x-stainless-lang": "js",
        "x-stainless-os": _stainless_os(),
        "x-stainless-package-version": sdk_version,
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": node_version,
        # 会话链路 ID：缺失会触发 11128 渠道风控
        "X-Conversation-ID": conversation_id,
        "X-Conversation-Request-ID": secrets.token_hex(16),
        "X-Conversation-Message-ID": uuid.uuid4().hex,
        "X-Request-ID": request_id,
        "X-Agent-Intent": "craft",
        "X-Agent-Purpose": "conversation",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "CLI",
        "X-IDE-Version": cli_version,
        "User-Agent": f"CLI/{cli_version} CodeBuddy/{cli_version}",
        "X-Private-Data": "false",
        "X-CodeBuddy-Request": "1",
        "X-Product": "SaaS",
        "X-Domain": host,
        "Host": host,
    }
    if intl:
        # 官方 CLI 还会带的头（ccLoad 从官方安装包抽出的协议常量表）。
        # 只补语义确定的：会话 id / 链路 id / 产品版本 / 当前模型。值不明的
        # （X-Agent-Type / X-Data-Tag / X-Root-Request-ID）宁可不发——发一个
        # 官方从不发的头本身就是信号。
        headers["X-Product-Version"] = cli_version
        headers["X-Trace-ID"] = request_id
        headers["X-Session-ID"] = conversation_id
        if model:
            headers["X-Model-ID"] = model
    if quota_only:
        return headers
    # OAuth 凭证优先用 account_uid，否则回退 user_id；国际站再回退到 JWT `sub`
    # （本地 6 个 intl 凭证的 user_id / account_uid 都是空串，官方 CLI 却每个
    # 请求都发 X-User-Id）。
    effective_user = account_uid or user_id
    if not effective_user and intl:
        effective_user = uid_from_token(bearer_token)
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
