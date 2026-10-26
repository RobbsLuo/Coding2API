"""CodeBuddy 国际版渠道（`codebuddy_intl`）接线与能力边界。

国际版与国内站**同协议**（同一 Keycloak realm、同一组 `/v2/*` `/v3/*` `/billing/*`
路径），所以本文件只测**接线与差异**，不重复测协议解析（那些在本目录其余
`test_client*.py` / `test_events.py` 里）：

- Settings 新字段与**独立**白名单（混发两站 Token 即泄露，必须测到）
- 端点解析的白名单拒绝路径
- 独立 pacer 与独立 OAuth 设备码流程
- **首条 system 强制注入**：国际站网关硬性要求 `messages[0]` 是 system prompt，
  否则整单 400 / 11128（2026-10-12 真实国际站凭证实测：无 system → 400，
  前置中性 system → 200）。国内站无此要求，出站 body 必须一个字节不变。
- 能力边界：`growth` 为 None（未验证且含不可逆动作），`checkin` /
  `checkin_scope` / `checkin_status` 为 None（国际站在渠道侧就没有签到活动，
  `active: false` 且活动字段全空）；**活跃上报启用**（`/v2/report` 实测存在，
  与成长中心不是同一路径）
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.config import (
    Settings,
    validate_codebuddy_intl_endpoint_allowed,
    validate_endpoint_allowed,
)
from src.engine.model_resolver import KNOWN_PROVIDERS
from src.provider.codebuddy.client import (
    DEFAULT_LEADING_SYSTEM,
    CodeBuddyClient,
    CodeBuddyProvider,
    ensure_leading_system,
)
from src.provider.codebuddy.credential import CodeBuddyCredential
from src.provider.codebuddy.intl import CodeBuddyIntlProvider
from src.runtime_settings import HOT_BY_KEY, RuntimeSettings
from tests.helpers.auth import settings_with as _settings
from tests.helpers.constants import SECRET
from tests.helpers.stores import MemoryStore as _MemoryStore

# --------------------------------------------------------------- Settings 字段

def test_intl_defaults_and_allowed_parsing():
    settings = _settings()
    assert settings.codebuddy_intl_api_endpoint == "https://www.codebuddy.ai"
    assert settings.codebuddy_intl_allowed == ("https://www.codebuddy.ai",)


def test_intl_allowed_parsing_ignores_blank_and_trailing_slash():
    settings = _settings(CODEBUDDY_INTL_ALLOWED_ENDPOINTS=(
        " https://www.codebuddy.ai/ , , "))
    assert settings.codebuddy_intl_allowed == ("https://www.codebuddy.ai",)
    assert _settings(CODEBUDDY_INTL_ALLOWED_ENDPOINTS="").codebuddy_intl_allowed == ()


def test_intl_whitelist_excludes_cn_site():
    """国际站白名单**不含**国内站主机；反之国内站白名单也不负责国际站。

    两站 Token 由不同网关签发，跨站发送就是凭证泄露——这是本渠道最要紧的
    一条不变量，所以显式测双向拒绝。
    """
    settings = _settings()
    cn = "https://copilot.tencent.com"
    intl = "https://www.codebuddy.ai"
    assert not validate_codebuddy_intl_endpoint_allowed(cn, settings)
    assert validate_codebuddy_intl_endpoint_allowed(intl, settings)
    # 国内站那条白名单里虽然列了国际站（历史遗留，写在那里供人查阅），
    # 但国际站的解析走的是自己那条校验，不会误用。
    assert validate_endpoint_allowed(intl, settings)


def test_intl_whitelist_rejects_cn_host_even_if_listed():
    """白名单里**混入**国内站主机 → 整个白名单作废（端点校验直接拒绝）。

    只校验「端点在白名单里」不够：把国内站主机加进国际站白名单后，
    端点校验会通过，国际站账号的 Token 照样发往国内站网关（跨站泄露）。
    故白名单含国内站主机时一律拒绝。
    """
    settings = _settings(
        CODEBUDDY_INTL_ALLOWED_ENDPOINTS=(
            "https://www.codebuddy.ai,https://copilot.tencent.com"))
    intl = "https://www.codebuddy.ai"
    assert not validate_codebuddy_intl_endpoint_allowed(intl, settings)
    # 带路径 / 端口 / 大小写 / 无 scheme 的国内站主机同样要识别出来
    for variant in ("https://copilot.tencent.com:443/",
                    "http://copilot.tencent.com",
                    "https://Copilot.Tencent.COM",
                    "copilot.tencent.com",
                    "copilot.tencent.com:443/path"):
        poisoned = _settings(
            CODEBUDDY_INTL_ALLOWED_ENDPOINTS=f"https://www.codebuddy.ai,{variant}")
        assert not validate_codebuddy_intl_endpoint_allowed(intl, poisoned), variant


# --------------------------------------------------------------- 端点解析

def test_resolve_intl_endpoint_returns_configured_host():
    import src.main as main

    assert main._codebuddy_intl_endpoint(_settings()) == "https://www.codebuddy.ai"


def test_resolve_intl_endpoint_rejects_host_outside_whitelist():
    import src.main as main

    settings = _settings(CODEBUDDY_INTL_API_ENDPOINT="https://evil.example.com")
    with pytest.raises(ValueError, match="CODEBUDDY_INTL_API_ENDPOINT"):
        main._codebuddy_intl_endpoint(settings)


def test_resolve_intl_endpoint_rejects_cn_site_even_though_cn_whitelist_has_it():
    """把国内站主机填进国际站端点必须被拒——否则等于把国际站凭证发到国内站。"""
    import src.main as main

    settings = _settings(
        CODEBUDDY_INTL_API_ENDPOINT="https://copilot.tencent.com")
    with pytest.raises(ValueError, match="CODEBUDDY_INTL_API_ENDPOINT"):
        main._codebuddy_intl_endpoint(settings)


# --------------------------------------------------------------- 渠道注册

def test_intl_is_known_provider():
    assert "codebuddy_intl" in KNOWN_PROVIDERS


def test_build_app_registers_intl_channel(tmp_path):
    import src.main as main

    app = main.build_app(_settings(data_dir=str(tmp_path)))
    provider = app.state.services.registry["codebuddy_intl"]
    assert isinstance(provider, CodeBuddyIntlProvider)
    assert provider.id == "codebuddy_intl"
    assert provider.client.endpoint == "https://www.codebuddy.ai"
    assert provider.host() == "www.codebuddy.ai"


def test_build_app_keeps_cn_channel_on_cn_site(tmp_path):
    """两渠道并存：国内站渠道不能被国际站端点污染（旧凭证全靠这条）。"""
    import src.main as main

    app = main.build_app(_settings(data_dir=str(tmp_path)))
    registry = app.state.services.registry
    assert registry["codebuddy"].client.endpoint == "https://copilot.tencent.com"
    assert registry["codebuddy_intl"].client.endpoint == "https://www.codebuddy.ai"


def test_intl_channel_has_its_own_pacer(tmp_path):
    """独立节流器：共用会让两站请求排进同一条队列互相空等最小间隔。"""
    import src.main as main

    app = main.build_app(_settings(data_dir=str(tmp_path)))
    registry = app.state.services.registry
    assert registry["codebuddy_intl"].pacer is not registry["codebuddy"].pacer


def test_intl_requires_leading_system_but_cn_does_not(tmp_path):
    """国际站网关硬性要求首条 system（实测 400/11128），国内站无此要求。

    这条守卫挡的是「为了让国际站通而给 client 加全局注入」：那会让存量国内站
    请求凭空多一条 system，客户端自带的 system 也可能被顶掉。
    """
    import src.main as main

    registry = main.build_app(_settings(data_dir=str(tmp_path))).state.services.registry
    assert registry["codebuddy_intl"].client.require_leading_system is True
    assert registry["codebuddy"].client.require_leading_system is False


def test_upstream_auth_registers_both_codebuddy_sites():
    """两站各一套设备码流程：登录域名不同，endpoint 不同。"""
    import src.main as main

    registry = {
        "codebuddy": CodeBuddyProvider(client=CodeBuddyClient()),
        "codebuddy_intl": CodeBuddyIntlProvider(client=CodeBuddyClient()),
    }
    flows = main._upstream_auth(registry, _settings())
    assert set(flows) == {"codebuddy", "codebuddy_intl"}
    assert flows["codebuddy"] is not flows["codebuddy_intl"]


@pytest.fixture()
def admin_client(tmp_path):
    """真实 registry 的 admin 客户端（不注入替身 OAuth）。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        yield app, client


def test_login_start_endpoint_accepts_intl(admin_client, monkeypatch):
    """端到端：管理台「登录 CodeBuddy 国际」必须走 poll 轨道。

    这条测的是**真实装配**的 registry，不注入替身 OAuth——只测
    `_upstream_auth` 的返回值会漏掉「注册表里没有该渠道」这类断线，
    表现为登录按钮点了就报 `does not support login`。
    """
    from src.auth.session import create_session_token  # noqa: F401
    from src.provider.base import AuthSession

    app, client = admin_client
    assert "codebuddy_intl" in app.state.upstream_auth

    async def fake_start(self, username):
        return AuthSession(flow="poll", state=f"state-{username}",
                           auth_url=f"{self.endpoint}/v2/plugin/auth/state",
                           interval=5)

    monkeypatch.setattr(
        "src.provider.codebuddy.oauth.CodeBuddyOAuth.start", fake_start)
    response = client.post("/api/auth/upstream/start",
                           json={"provider": "codebuddy_intl"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["flow"] == "poll"
    assert body["auth_url"].startswith("https://www.codebuddy.ai/")
    # 登录域名必须是国际站，不能是国内站
    assert "copilot.tencent.com" not in body["auth_url"]


def test_login_start_endpoint_accepts_cn(admin_client):
    """对照：国内站渠道未被国际站的接线带坏。"""
    app, _client = admin_client
    assert "codebuddy" in app.state.upstream_auth


# --------------------------------------------------------------- 能力边界

def test_intl_has_no_growth_or_activity():
    """成长中心在国际站**未验证**，一律关闭。

    成长中心含抽奖 / 连登兑换 / 盲盒等不可逆动作；调用方一律用
    `getattr(provider, "growth", None)` 探测，置 None 即自动跳过。
    """
    provider = CodeBuddyIntlProvider(client=CodeBuddyClient())
    assert getattr(provider, "growth", None) is None


def test_cn_provider_keeps_growth_and_activity():
    """反向守卫：上一条的对照，国内站能力不能被误关。"""
    provider = CodeBuddyProvider(client=CodeBuddyClient())
    assert callable(provider.growth)
    assert callable(provider.activity)
    assert callable(provider.activity_scope)


def test_intl_keeps_activity_reporting():
    """国际站**启用**活跃上报——端点实测存在，与成长中心不是同一路径。

    2026-10-12 真实国际站凭证实测：`/v2/report` 带凭证回 `200 {"code":0}`，
    同批探针里 `/v2/report-nope` 回应用层 `404 Route Not Found`（两者可区分，
    故端点真实存在并接受上报）。早先「国际站无活跃上报」的判断只探过
    `/v2/activity/*` 前缀、没探过 `/v2/report`，属探针覆盖不全，这里纠正。

    与国内站同一套协议（`CodeBuddyActivity` 共用），`activity_scope` 带
    endpoint 故两站不会互相占位。
    """
    provider = CodeBuddyIntlProvider(client=CodeBuddyClient())
    assert callable(provider.activity)
    assert callable(provider.activity_scope)


def test_intl_activity_scope_is_scoped_by_endpoint():
    """两站活跃上报隔离键必须含 endpoint：同账号多凭证也要各站各报一次。"""
    import base64
    import json as _json

    from src.provider.codebuddy.client import CodeBuddyClient as _C

    def jwt_with_sub(sub: str) -> str:
        payload = base64.urlsafe_b64encode(
            _json.dumps({"sub": sub}).encode()).decode().rstrip("=")
        return f"header.{payload}.sig"

    credential = {"bearer_token": jwt_with_sub("user-1")}
    cn = CodeBuddyProvider(client=_C(endpoint="https://copilot.tencent.com"))
    intl = CodeBuddyIntlProvider(client=_C(endpoint="https://www.codebuddy.ai"))
    cn_scope = cn.activity_scope(credential)
    intl_scope = intl.activity_scope(credential)
    assert cn_scope == "https://copilot.tencent.com|user-1"
    assert intl_scope == "https://www.codebuddy.ai|user-1"
    # 同一渠道同一凭证稳定，否则同账号多凭证无法共桶（同日只报一次）
    assert cn.activity_scope(credential) == cn_scope
    # 身份取不到时返回空串由调用方回落到 credential_id，绝不编造 id 去上报
    assert intl.activity_scope({"bearer_token": "not-a-jwt"}) == ""


def test_intl_has_no_checkin():
    """国际站渠道侧就没有签到活动，三项能力一并关闭。

    实测（2026-10-12 真实国际站凭证）：`/billing/meter/checkin-activity-status`
    回 `code=0` 但 `active: false`，且 `season` / `theme_name` / `start_time` /
    `end_time` / `activity_name` **全为空**（国内站同接口是 `season: 10` /
    「Buddy加油站」/ 有起止时间），CLAIM 回 `{"code":10001,"msg":"签到活动未开启
    或已过期"}`——是渠道级缺席而非账号级未开启。保留会让后台任务每 10 分钟失败
    一次并触发 `task_failed` 噪音告警。

    三个探测点都要关：后台任务探 `checkin_scope`（tasks/checkin.py），
    管理台探 `checkin` / `checkin_status`（api/admin_credentials.py）。
    """
    provider = CodeBuddyIntlProvider(client=CodeBuddyClient())
    for name in ("checkin", "checkin_scope", "checkin_status"):
        assert getattr(provider, name, None) is None, name


def test_cn_provider_keeps_checkin():
    """反向守卫：上一条的对照，国内站签到不能被误关。"""
    provider = CodeBuddyProvider(client=CodeBuddyClient())
    for name in ("checkin", "checkin_scope", "checkin_status"):
        assert callable(getattr(provider, name)), name


def test_intl_keeps_chat_quota_models_refresh():
    """已验证存在的能力全部保留：与国内站同一套路径与协议。"""
    provider = CodeBuddyIntlProvider(client=CodeBuddyClient())
    for name in ("import_credential", "probe_quota", "list_models", "stream_chat",
                 "refresh", "credential_from", "classify", "aclose", "host",
                 "activity", "activity_scope"):
        assert callable(getattr(provider, name)), name


def test_intl_pacer_key_is_scoped_by_channel():
    """节流桶键带渠道 id：两站即使共用账号标识也不会互相占位。"""
    from src.provider.codebuddy.client import _pacer_key
    from src.provider.codebuddy.credential import CodeBuddyCredential

    credential = CodeBuddyCredential.from_dict({"bearer_token": "tok"})
    cn = _pacer_key("codebuddy", credential)
    intl = _pacer_key("codebuddy_intl", credential)
    assert cn.startswith("codebuddy:")
    assert intl.startswith("codebuddy_intl:")
    assert cn != intl
    # 同一渠道同一凭证稳定（否则同账号多凭证无法共桶）
    assert _pacer_key("codebuddy", credential) == cn


# --------------------------------------------------------------- 热更间隔

def test_intl_min_interval_is_hot_updatable():
    assert "codebuddy_intl_chat_min_interval" in HOT_BY_KEY
    runtime = RuntimeSettings(_settings(), _MemoryStore())
    assert runtime.codebuddy_intl_chat_min_interval == 5
    runtime.set("codebuddy_intl_chat_min_interval", 0)
    assert runtime.codebuddy_intl_chat_min_interval == 0
    # 与国内站各自独立：改一个不影响另一个
    assert runtime.codebuddy_chat_min_interval == 5


# --------------------------------------------------- 首条 system 强制注入

def _stream_client(handler, **kwargs) -> CodeBuddyClient:
    """只接流式请求的 client（额度等短请求用不到，这里不构造）。"""
    transport = httpx.MockTransport(handler)
    return CodeBuddyClient(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None),
        **kwargs)


@pytest.mark.parametrize("messages, expect_first_role", [
    # 需要补：首条不是 system
    ([{"role": "user", "content": "hi"}], "system"),
    ([{"role": "user", "content": "a"},
      {"role": "assistant", "content": "b"}], "system"),
    # 不补：客户端自带 system（含多条、含 developer 归一后的情形）
    ([{"role": "system", "content": "mine"}], "system"),
    ([{"role": "system", "content": "mine"},
      {"role": "user", "content": "hi"}], "system"),
])
async def test_stream_chat_prepends_system_when_absent(messages, expect_first_role):
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, text="")

    async for _ in _stream_client(handler, require_leading_system=True).stream_chat(
            CodeBuddyCredential(bearer_token="t"), {"messages": messages}, "glm-5.2"):
        pass
    out = sent["messages"]
    assert out[0]["role"] == expect_first_role
    assert len(out) == len(messages) + (0 if messages[0]["role"] == "system" else 1)


async def test_injected_system_keeps_original_messages_order():
    """补的 system 只能落在最前：原消息顺序 / 正文一个字都不能动。"""
    sent: dict = {}
    messages = [{"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"}]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, text="")

    async for _ in _stream_client(handler, require_leading_system=True).stream_chat(
            CodeBuddyCredential(bearer_token="t"), {"messages": messages}, "glm-5.2"):
        pass
    assert sent["messages"][0] == {"role": "system", "content": DEFAULT_LEADING_SYSTEM}
    assert sent["messages"][1:] == messages


async def test_cn_stream_chat_leaves_user_first_payload_untouched():
    """反向守卫：国内站开关关闭时不出站 body 一个字节都不变。"""
    sent: dict = {}
    payload = {"messages": [{"role": "user", "content": "u1"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, text="")

    async for _ in _stream_client(handler).stream_chat(
            CodeBuddyCredential(bearer_token="t"), payload, "glm-5.2"):
        pass
    assert sent["messages"] == [{"role": "user", "content": "u1"}]


async def test_developer_first_is_normalized_not_duplicated():
    """PI 等客户端把 system 转成 developer：归一后已满足首条要求，不能再补一条。"""
    sent: dict = {}
    messages = [{"role": "developer", "content": "mine"}, {"role": "user", "content": "hi"}]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, text="")

    async for _ in _stream_client(handler, require_leading_system=True).stream_chat(
            CodeBuddyCredential(bearer_token="t"), {"messages": messages}, "glm-5.2"):
        pass
    assert sent["messages"] == [{"role": "system", "content": "mine"},
                                {"role": "user", "content": "hi"}]


async def test_injection_does_not_leak_back_into_request_payload():
    """出站改写只作用于副本：注入不能穿透回 request.raw（会话粘性按原文算指纹）。"""
    sent: dict = {}
    messages = [{"role": "user", "content": "u1"}]
    payload = {"messages": messages}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, text="")

    async for _ in _stream_client(handler, require_leading_system=True).stream_chat(
            CodeBuddyCredential(bearer_token="t"), payload, "glm-5.2"):
        pass
    assert len(sent["messages"]) == 2
    assert payload["messages"] == [{"role": "user", "content": "u1"}]


@pytest.mark.parametrize("body", [
    {},                                            # 无 messages 键
    {"messages": []},                              # 空列表
    {"messages": "not-a-list"},                    # 非法类型
    {"messages": ["bare-string"]},                 # 首条不是 dict
])
def test_ensure_leading_system_skips_unusable_message_lists(body):
    """不可用形态一律不动：宁可让上游原样报错，也不在坏结构上乱插。"""
    snapshot = dict(body)
    assert ensure_leading_system(body) is False
    assert body == snapshot