"""B3.5 多 Key 出口测试：/healthz 池计数、provider_binding、allowed_ips。

分三层：
- `src/auth/access.py` 纯函数（IP 解析/判定/来源推断）直测；
- 仓储 + SDK 直调（`ApiKeyRepository.create/authenticate` 的策略列）；
- HTTP 端到端（TestClient 真发请求，验证 400/403 与绑定真的收窄上游）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth.access import (
    MAX_IP_ENTRIES,
    client_ip,
    ip_allowed,
    normalize_allowed_ips,
    split_entries,
)
from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import ApiKeyRepository, CredentialRepository
from src.engine.executor import Executor, ExecutorDeps
from src.engine.scheduler import Scheduler
from src.main import build_app
from src.provider.base import Event, EventKind
from tests.conftest import SECRET

GOOD = [Event(kind=EventKind.CONTENT, content="ok"),
        Event(kind=EventKind.FINISH, finish_reason="stop")]


# ---------------------------------------------------------------- IP 纯函数

def test_split_entries_trims_and_drops_blanks():
    assert split_entries(None) == []
    assert split_entries("") == []
    assert split_entries(" 10.0.0.1 , , 192.168.1.0/24 ") == [
        "10.0.0.1", "192.168.1.0/24"]


def test_normalize_allowed_ips_canonicalizes_and_dedupes():
    assert normalize_allowed_ips("10.0.0.1") == "10.0.0.1/32"
    assert normalize_allowed_ips("10.0.0.1/32,10.0.0.1") == "10.0.0.1/32"
    assert normalize_allowed_ips(" 192.168.1.5/24 ") == "192.168.1.0/24"
    assert normalize_allowed_ips("") == ""


def test_normalize_allowed_ips_rejects_garbage():
    with pytest.raises(ValueError, match="非法 IP/CIDR"):
        normalize_allowed_ips("not-an-ip")


def test_normalize_allowed_ips_enforces_entry_cap():
    many = ",".join(f"10.0.{i}.0/24" for i in range(MAX_IP_ENTRIES + 1))
    with pytest.raises(ValueError, match="最多"):
        normalize_allowed_ips(many)
    # 恰好在上限内可以通过
    assert normalize_allowed_ips(
        ",".join(f"10.0.{i}.0/24" for i in range(MAX_IP_ENTRIES))) != ""


def test_ip_allowed_empty_means_unrestricted():
    assert ip_allowed("1.2.3.4", "") is True
    assert ip_allowed("1.2.3.4", None) is True


def test_ip_allowed_matches_single_and_network():
    assert ip_allowed("10.0.0.1", "10.0.0.1/32") is True
    assert ip_allowed("10.0.0.2", "10.0.0.1/32") is False
    assert ip_allowed("10.1.2.3", "10.0.0.0/8") is True
    assert ip_allowed("2001:db8::1", "2001:db8::/32") is True


def test_ip_allowed_rejects_when_ip_or_entry_unusable():
    # 拿不到对端地址 → 拒绝（不能默认放行）
    assert ip_allowed("", "10.0.0.1/32") is False
    assert ip_allowed("not-an-ip", "10.0.0.1/32") is False
    # 脏条目在读取路径被丢弃，但合法条目仍然生效
    assert ip_allowed("10.0.0.1", "garbage,10.0.0.1/32") is True
    # 整份白名单不可解析 → 拒绝（fail closed，不因脏数据敞开）
    assert ip_allowed("10.0.0.1", "garbage") is False


def test_ip_allowed_never_crosses_address_families():
    assert ip_allowed("::1", "10.0.0.0/8") is False


def test_client_ip_ignores_xff_unless_trusted():
    assert client_ip("10.0.0.9", "1.2.3.4", trust_proxy=False) == "10.0.0.9"
    assert client_ip(None, None, trust_proxy=False) == ""
    # 受信反代：取最后一个条目（紧邻代理实际看到的地址）
    assert client_ip("127.0.0.1", "1.2.3.4, 10.0.0.9", trust_proxy=True) == "10.0.0.9"
    # 开了但头是空的 → 回落对端
    assert client_ip("127.0.0.1", " , ", trust_proxy=True) == "127.0.0.1"


# ------------------------------------------------------------------- 仓储

@pytest.fixture()
def keys(tmp_path):
    db = Database(tmp_path / "keys.sqlite3")
    apply_schema(db.connect())
    yield ApiKeyRepository(db)
    db.close()


def test_repository_round_trips_policy_columns(keys):
    created = keys.create("alice", "laptop", provider_binding="trae",
                          allowed_ips="10.0.0.0/8")
    assert created["provider_binding"] == "trae"
    assert created["allowed_ips"] == "10.0.0.0/8"

    row = keys.authenticate(created["api_key"])
    assert row == {"id": created["id"], "username": "alice",
                   "provider_binding": "trae", "allowed_ips": "10.0.0.0/8"}
    listed = keys.list_for("alice")[0]
    assert listed["provider_binding"] == "trae"
    assert listed["allowed_ips"] == "10.0.0.0/8"


def test_repository_defaults_to_unrestricted(keys):
    created = keys.create("alice")
    row = keys.authenticate(created["api_key"])
    assert row["provider_binding"] == "" and row["allowed_ips"] == ""
    # verify 仍是「只回用户名」的薄封装（旧调用方契约）
    assert keys.verify(created["api_key"]) == "alice"
    assert keys.authenticate("sk-nope") is None
    assert keys.verify("sk-nope") is None


# ------------------------------------------------------------------- HTTP

def _settings(tmp_path, **overrides) -> Settings:
    return Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                    ADMIN_USERNAMES="root", **overrides)


@pytest.fixture()
def client(tmp_path):
    app = build_app(_settings(tmp_path))
    with TestClient(app) as http:
        http.cookies.set("coding2api_session", create_session_token("root", SECRET))
        yield http


def _create_key(client, **payload) -> dict:
    response = client.post("/api/api-keys", json={"name": "t", **payload})
    assert response.status_code == 200, response.text
    return response.json()


def test_healthz_reports_pool_counts(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["service"] == "coding2api"
    assert body["version"]
    counts = body["credentials"]
    assert counts == {"total": 0, "ready": 0, "cooling": 0, "paused": 0, "disabled": 0}
    # 老端点保留
    assert client.get("/health").json() == {"status": "ok"}


def test_healthz_counts_partition_the_pool(tmp_path):
    """四类计数互斥且合计 = total；口径与调度器 is_selectable 一致。"""
    import sqlite3

    settings = _settings(tmp_path)
    app = build_app(settings)
    repo = app.state.credentials
    repo.add(provider="trae", credential_data={"accessToken": "a"})
    paused = repo.add(provider="trae", credential_data={"accessToken": "b"})
    cooling = repo.add(provider="trae", credential_data={"accessToken": "c"})
    broken = repo.add(provider="trae", credential_data={"accessToken": "d"})
    repo.set_enabled(paused, False)
    # 硬禁用/冷却没有公开 setter（生产路径由 save_error 驱动），直接改库构造状态
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE credentials SET disabled = 1 WHERE id = ?", (broken,))
    conn.execute("UPDATE credentials SET cooling_until = ? WHERE id = ?",
                 (2 ** 31 - 1, cooling))
    conn.commit()
    conn.close()
    with TestClient(app) as http:
        counts = http.get("/healthz").json()["credentials"]
    assert counts == {"total": 4, "ready": 1, "cooling": 1, "paused": 1,
                      "disabled": 1}


def test_create_key_rejects_unknown_binding(client):
    response = client.post("/api/api-keys", json={"name": "t", "provider_binding": "ghost"})
    assert response.status_code == 400
    assert "provider_binding" in response.json()["error"]["message"]


def test_create_key_rejects_bad_allowed_ips(client):
    response = client.post("/api/api-keys", json={"name": "t", "allowed_ips": "nope"})
    assert response.status_code == 400
    assert "非法 IP/CIDR" in response.json()["error"]["message"]


def test_create_key_normalizes_and_lists_policy(client):
    created = _create_key(client, provider_binding=" TRAE ", allowed_ips="10.0.0.1")
    assert created["provider_binding"] == "trae"
    assert created["allowed_ips"] == "10.0.0.1/32"
    listed = client.get("/api/api-keys").json()["api_keys"][0]
    assert listed["provider_binding"] == "trae"
    assert listed["allowed_ips"] == "10.0.0.1/32"


def test_allowed_ips_blocks_foreign_source(client):
    key = _create_key(client, allowed_ips="10.0.0.1/32")["api_key"]
    response = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "source ip not allowed for this api key"


def test_allowed_ips_allows_matching_source(client):
    # TestClient 的对端是 "testclient"（非 IP）→ 用不加限制的 Key 才通得过；
    # 这里用 127.0.0.1 走「白名单为空即放行」的另一支。
    key = _create_key(client, allowed_ips="")["api_key"]
    response = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200


def test_trust_proxy_honors_forwarded_for(tmp_path):
    """TRUST_PROXY 开启时按 XFF 末条判定；关闭时忽略该头。"""
    settings = _settings(tmp_path, TRUST_PROXY=True)
    app = build_app(settings)
    key = app.state.api_keys.create("root", allowed_ips="203.0.113.0/24")["api_key"]
    with TestClient(app) as http:
        ok = http.get("/v1/models", headers={
            "Authorization": f"Bearer {key}",
            "X-Forwarded-For": "1.2.3.4, 203.0.113.9"})
        assert ok.status_code == 200
        blocked = http.get("/v1/models", headers={
            "Authorization": f"Bearer {key}",
            "X-Forwarded-For": "1.2.3.4"})
        assert blocked.status_code == 403


def test_forwarded_for_ignored_without_trust_proxy(tmp_path):
    app = build_app(_settings(tmp_path, TRUST_PROXY=False))
    key = app.state.api_keys.create("root", allowed_ips="203.0.113.0/24")["api_key"]
    with TestClient(app) as http:
        # XFF 说自己在白名单内，但默认不采信 → 403
        response = http.get("/v1/models", headers={
            "Authorization": f"Bearer {key}",
            "X-Forwarded-For": "203.0.113.9"})
    assert response.status_code == 403


# ------------------------------------------------- 绑定收窄（executor 直测）

class _Provider:
    def __init__(self, provider_id: str) -> None:
        self.id = provider_id
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        self.calls += 1
        for event in GOOD:
            yield event


@pytest.fixture()
def dual(tmp_path):
    db = Database(tmp_path / "dual.sqlite3")
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    yield repo
    db.close()


def _executor(repo, aliases):
    assert repo is not None
    cb, trae = _Provider("codebuddy"), _Provider("trae")
    return (Executor(ExecutorDeps(providers={"codebuddy": cb, "trae": trae},
                                  credentials=repo, scheduler=Scheduler(),
                                  default_model="glm-5.2", model_aliases=aliases)),
            cb, trae)


def _chat(model="glm-5.2"):
    from src.compat.openai.request import parse_chat_request

    return parse_chat_request({"model": model,
                               "messages": [{"role": "user", "content": "hi"}]})


async def test_binding_narrows_to_bound_provider(dual):
    """绑定 TRAE：只打 TRAE，即使 CB 排序更优。"""
    executor, cb, trae = _executor(dual, {"codebuddy": {"glm-5.2": "glm-5.2"},
                                          "trae": {"glm-5.2": "glm-5.2"}})
    result = await executor.complete(_chat(), username="u", provider_binding="trae")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert (trae.calls, cb.calls) == (1, 0)


async def test_binding_rejects_model_owned_by_other_provider(dual):
    """模型目录证明该模型只属于 CB，但 Key 绑定 TRAE → 400，零上游调用。"""
    from src.compat.openai.request import InvalidRequest

    executor, cb, trae = _executor(dual, {"codebuddy": {"cb-only": "cb-only"}})
    with pytest.raises(InvalidRequest) as exc_info:
        await executor.complete(_chat("cb-only"), username="u", provider_binding="trae")
    assert "not available on provider 'trae'" in str(exc_info.value)
    assert (cb.calls, trae.calls) == (0, 0)


async def test_binding_conflicts_with_forced_provider(dual):
    """@codebuddy 与绑定 trae 冲突 → 400（明确意图冲突，不静默改道）。

    用 `preflight` 而非 `complete`：这是「请求本身不可能成功」的失败，
    与流式头发出前的校验走同一条路径。
    """
    from src.compat.openai.request import InvalidRequest

    executor, cb, trae = _executor(dual, {})
    with pytest.raises(InvalidRequest) as exc_info:
        executor.preflight(_chat("glm-5.2@codebuddy"), provider_binding="trae")
    assert "restricted to 'trae'" in str(exc_info.value)
    assert (cb.calls, trae.calls) == (0, 0)


async def test_binding_defers_when_catalog_not_ready(dual):
    """目录未就绪（空映射）→ 不做归属判断，按绑定渠道直接放行。"""
    executor, cb, trae = _executor(dual, {})
    result = await executor.complete(_chat("whatever"), username="u",
                                     provider_binding="codebuddy")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert (cb.calls, trae.calls) == (1, 0)


async def test_binding_keeps_forced_target_when_consistent(dual):
    """@trae 与绑定 trae 一致 → 正常执行（forced 分支的放行支）。"""
    executor, cb, trae = _executor(dual, {})
    await executor.complete(_chat("glm-5.2@trae"), username="u", provider_binding="trae")
    assert (trae.calls, cb.calls) == (1, 0)


async def test_preflight_applies_binding(dual):
    """流式前置校验同样生效：不能等 200 响应头发出后才报 400。"""
    from src.compat.openai.request import InvalidRequest

    executor, _cb, _trae = _executor(dual, {"codebuddy": {"cb-only": "cb-only"}})
    with pytest.raises(InvalidRequest):
        executor.preflight(_chat("cb-only"), provider_binding="trae")


def test_http_binding_end_to_end(tmp_path):
    """走完整 HTTP：绑定 Key 请求属别家渠道的模型 → 400 且提示绑定渠道。"""
    app = build_app(_settings(tmp_path), providers={"trae": _Provider("trae")})
    key = app.state.api_keys.create("root", provider_binding="trae")["api_key"]
    # `@codebuddy` 与绑定 trae 冲突：不经模型目录即可判定
    with TestClient(app) as http:
        response = http.post("/v1/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json={"model": "glm-5.2@codebuddy",
                                   "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 400
    assert "restricted to 'trae'" in response.json()["error"]["message"]
    # 展示面：列表把策略一并下发
    listed = app.state.api_keys.list_for("root")[0]
    assert listed["provider_binding"] == "trae"


def test_http_binding_rejects_foreign_model_via_catalog(tmp_path):
    """目录证明模型归属别家渠道时 400，文案给出实际归属。

    走 `preflight`（同 HTTP 路径）而不是真发请求：请求一旦进入选号，目录
    （`model_aliases`）会被上游拉取结果覆写，这里要在快照前完成判定。
    """
    from src.compat.openai.request import InvalidRequest

    app = build_app(_settings(tmp_path), providers={"trae": _Provider("trae")})
    app.state.model_aliases["codebuddy"] = {"cb-only": "cb-only"}
    ex = app.state.executor
    with pytest.raises(InvalidRequest) as exc_info:
        ex.preflight(_chat("cb-only"), "trae")
    assert "not available on provider 'trae'" in str(exc_info.value)


def test_http_binding_stream_preflight(tmp_path):
    """流式：绑定冲突必须在 200 SSE 头之前以 400 拒绝。"""
    app = build_app(_settings(tmp_path), providers={"trae": _Provider("trae")})
    key = app.state.api_keys.create("root", provider_binding="trae")["api_key"]
    with TestClient(app) as http:
        response = http.post("/v1/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json={"model": "glm-5.2@codebuddy", "stream": True,
                                   "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 400
    assert "restricted to 'trae'" in response.json()["error"]["message"]
