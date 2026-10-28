"""本轮隐患修复回归（HTTP 层）——模型/探测/会话/404/凭证/限流/审计。"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import _api_not_found, build_app
from src.provider.base import Model
from tests.helpers.constants import SECRET


def _drain_model_refresh(client, services):
    """等 stale-while-revalidate 的后台刷新跑完（否则调用计数断言会与事件循环赛跑）。"""
    tasks = [t for t in services.pending_model_refreshes if not t.done()]
    if not tasks:
        return

    async def _wait():
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    client.portal.call(_wait)

@pytest.fixture()
def app_client_and_settings(settings):
    app = build_app(settings)
    with TestClient(app) as client:
        yield app, client

# --------------------------------------------------- #8/#9 模型列表 TTL


def test_models_endpoint_skips_disabled_credentials(settings):
    """模型列表只应使用可调度凭证，disabled 凭证不再被挑中。"""
    calls = []

    class Provider:
        id = "trae"

        async def list_models(self, credential_data):
            calls.append(credential_data)
            return [Model(id="glm-5.3")]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={"trae": Provider()})
    credentials = app.state.credentials
    enabled = credentials.add(provider="trae", credential_data={"accessToken": "good"})
    disabled = credentials.add(provider="trae", credential_data={"accessToken": "bad"})
    credentials.set_enabled(disabled, False)
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert calls == [{"accessToken": "good"}]
    assert enabled  # 保留引用，避免被 lint 判为未使用

def test_models_endpoint_reuses_cache_within_ttl(settings):
    calls = []

    class Provider:
        id = "trae"

        async def list_models(self, credential_data):
            calls.append(1)
            return [Model(id="glm-5.3")]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={"trae": Provider()})
    # 没凭证的渠道不参与模型列表，故先接入一条凭证
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        for _ in range(3):
            c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert len(calls) == 1          # 后续请求命中 TTL 缓存

# --------------------------------------------------------- #10 探测清理


def test_pending_probes_drops_finished_tasks(client):
    """探测任务完成后从列表摘除，避免长期运行内存累积。"""
    from src.main import _forget_task

    pending = client.app.state.pending_probes
    pending.clear()
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(asyncio.sleep(0))
        pending.append(task)
        task.add_done_callback(lambda done: _forget_task(done, pending))
        loop.run_until_complete(task)
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    assert pending == []

# ------------------------------------------------- #12 会话/Key 用户校验


def test_session_rejected_after_user_removed(settings, tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    token = create_session_token("ghost", SECRET)     # users.txt 里没有 ghost
    with TestClient(app) as c:
        c.cookies.set("coding2api_session", token)
        assert c.get("/api/auth/session").status_code == 401

def test_api_key_rejected_after_user_removed(settings):
    app = build_app(settings)
    key = app.state.api_keys.create("ghost")["api_key"]
    with TestClient(app) as c:
        response = c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 401

def test_logout_requires_csrf(client):
    response = client.post("/api/auth/logout", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403

def test_logout_allows_same_origin(client):
    response = client.post("/api/auth/logout",
                           headers={"Origin": "http://testserver",
                                    "X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200

# -------------------------------------------------------- #14 API 404


def test_unknown_api_path_returns_json_404(client):
    for path in ("/api/nope", "/v1/nope"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "invalid_request"

def test_frontend_routes_are_not_treated_as_api_404(client, tmp_path, monkeypatch):
    """前端路由（非 /api、/v1）不进 API 404 分支，走 SPA 兜底。

    dist 产物是 gitignored 的（CI 上不存在），因此这里把 dist 指到临时目录，
    同时断言「服务了 index.html」而不是「依赖本地构建产物」。
    """
    from src.webapp import static

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    monkeypatch.setattr(static, "frontend_dist", lambda: dist)

    response = client.get("/credentials")
    assert response.status_code == 200
    assert "spa" in response.text

def test_api_not_found_helper_shape():
    assert _api_not_found("api/x").status_code == 404

def test_static_cache_headers_keep_entry_fresh_and_assets_immutable(
        client, tmp_path, monkeypatch):
    """index.html 走 no-cache，hash 化的 assets 永久缓存。

    重新构建后 assets 文件名会变，但浏览器若缓存了旧 index.html 就会加载上一版
    bundle（页面能打开、功能却是旧的）。入口页必须每次回源校验。
    """
    from src.webapp import static

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(static, "frontend_dist", lambda: dist)

    index = client.get("/credentials")
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-cache"

    asset = client.get("/assets/index-abc123.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]

    # 直接请求 /index.html（不是 catch-all 回落）也必须 no-cache：
    # 它是入口文档而非 hash 化资源，immutable 会让浏览器缓存旧入口。
    direct = client.get("/index.html")
    assert direct.status_code == 200
    assert direct.headers["cache-control"] == "no-cache"

    # `web/public/` 原样拷贝、文件名不带 hash 的文件（theme-init.js）同样
    # 不能 immutable，否则改了主题脚本浏览器还按旧版缓存。
    (dist / "theme-init.js").write_text("console.log(1)", encoding="utf-8")
    theme = client.get("/theme-init.js")
    assert theme.status_code == 200
    assert theme.headers["cache-control"] == "no-cache"

# ---------------------------------------------------- #15 凭证恢复入口


def test_revive_credential_endpoint(client):
    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "t"}}).json()["id"]
    db = client.app.state.credentials._db
    db.connect().execute("UPDATE credentials SET disabled = 1, disabled_reason = 'x' "
                         "WHERE id = ?", (created,))
    db.connect().commit()
    assert client.post(f"/api/credentials/{created}/revive").status_code == 200
    row = db.connect().execute("SELECT disabled, disabled_reason FROM credentials "
                               "WHERE id = ?", (created,)).fetchone()
    assert row["disabled"] == 0 and row["disabled_reason"] is None

def test_revive_unknown_credential_is_400(client):
    assert client.post("/api/credentials/cred_missing/revive").status_code == 400

# ------------------------------------------------------------- #24 429


def test_throttled_response_has_retry_after(settings):
    from src.auth.throttle import LoginThrottle, ThrottleLimits

    app = build_app(settings)
    app.state.services.login_throttle = LoginThrottle(ThrottleLimits(max_per_user=1))
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "root", "password": "bad"})
        response = c.post("/api/auth/login", json={"username": "root", "password": "bad"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"

# ------------------------------------------------------- #27 审计日志


def test_credential_mutations_are_logged(client, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="src.api.admin_credentials"):
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "t"}}).json()["id"]
        client.post("/api/credentials/pin", json={"credential_id": created})
        client.delete(f"/api/credentials/{created}")
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "新增凭证" in messages and "固定凭证" in messages and "删除凭证" in messages

def test_transport_error_maps_to_502(client, monkeypatch):
    key = client.post("/api/api-keys", json={"name": "t"}).json()["api_key"]
    executor = client.app.state.executor

    async def boom(request, *, username="unknown", provider_binding=None):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(executor, "complete", boom)
    response = client.post("/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": "glm-5.3",
                                 "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"

# ------------------------------------------------- 覆盖率补口（真实分支）


def test_models_endpoint_falls_back_to_cache(settings):
    """动态拉取失败且已有缓存时，用缓存兜底保持列表完整。"""
    class Flaky:
        id = "trae"
        fail = False

        async def list_models(self, credential_data):
            if self.fail:
                raise RuntimeError("upstream down")
            return [Model(id="glm-5.3")]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    provider = Flaky()
    app = build_app(settings, providers={"trae": provider})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    services = app.state.services
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        provider.fail = True
        second = c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert second.status_code == 200
    assert [m["id"] for m in second.json()["data"]] == ["glm-5.3"]
    assert services.model_list_cache["trae"]      # 缓存保留

def test_models_endpoint_logs_when_no_cache(settings):
    """首次拉取就失败且无缓存 → 跳过该上游但仍返回列表。"""

    class Broken:
        id = "trae"

        async def list_models(self, credential_data):
            raise RuntimeError("no cache available")

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={"trae": Broken()})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        response = c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200 and response.json()["data"] == []

def test_models_cache_fallback_when_ttl_expired(settings):
    """TTL 过期后重新拉取失败 → 用缓存兜底（cached 分支）。"""
    class Flaky:
        id = "trae"
        fail = False

        async def list_models(self, credential_data):
            if self.fail:
                raise RuntimeError("upstream down")
            return [Model(id="glm-5.3")]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    provider = Flaky()
    app = build_app(settings, providers={"trae": provider})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        provider.fail = True
        app.state.services.model_list_fetched_at.clear()      # 模拟 TTL 过期
        response = c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert [m["id"] for m in response.json()["data"]] == ["glm-5.3"]

def test_models_failure_is_negatively_cached(settings):
    """拉取失败也刷 TTL 时间戳：TTL 内不再重试（有缓存用缓存，无缓存跳过）。"""

    class Flaky:
        id = "trae"
        attempts = 0

        async def list_models(self, credential_data):
            type(self).attempts += 1
            raise RuntimeError("upstream down")

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={"trae": Flaky()})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    services = app.state.services
    key = app.state.api_keys.create("root")["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as c:
        first = c.get("/v1/models", headers=auth)
        second = c.get("/v1/models", headers=auth)
    assert first.status_code == 200 and first.json()["data"] == []
    assert second.json()["data"] == []
    assert Flaky.attempts == 1                       # 第二次命中负缓存，没重试
    assert services.model_list_fetched_at["trae"] is not None

    # 清掉尝试时间戳（模拟 TTL 过期）→ 重新尝试
    services.model_list_fetched_at.clear()
    with TestClient(app) as c:
        c.get("/v1/models", headers=auth)
    assert Flaky.attempts == 2

def test_models_failure_keeps_cache_for_ttl(settings):
    """失败后 TTL 内用缓存兜底且不重试；TTL 过期才再次尝试。"""

    class Flaky:
        id = "trae"
        fail = False
        attempts = 0

        async def list_models(self, credential_data):
            type(self).attempts += 1
            if self.fail:
                raise RuntimeError("upstream down")
            return [Model(id="glm-5.3")]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    provider = Flaky()
    app = build_app(settings, providers={"trae": provider})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    services = app.state.services
    key = app.state.api_keys.create("root")["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as c:
        first = c.get("/v1/models", headers=auth)
        assert [m["id"] for m in first.json()["data"]] == ["glm-5.3"]
        assert Flaky.attempts == 1

        provider.fail = True
        services.model_list_fetched_at.clear()       # 模拟 TTL 过期 → 触发重试并失败
        second = c.get("/v1/models", headers=auth)
        assert [m["id"] for m in second.json()["data"]] == ["glm-5.3"]
        _drain_model_refresh(c, services)            # SWR：重试在后台，等它跑完
        assert Flaky.attempts == 2

        third = c.get("/v1/models", headers=auth)    # 失败后的 TTL 内：不再重试
        assert [m["id"] for m in third.json()["data"]] == ["glm-5.3"]
        assert Flaky.attempts == 2

# --------------------------------------------------- #25 dump 有界保留


def test_dump_request_body_trims_to_keep_limit(tmp_path):
    """超过保留上限后旧文件被淘汰（文件名同毫秒会覆盖，故只断言上界）。"""
    from src.api import chat

    for _ in range(chat.DUMP_KEEP_FILES + 5):
        chat.dump_request_body(tmp_path, {"messages": []})
    remaining = list((tmp_path / "dumps").glob("*.json"))
    assert 0 < len(remaining) <= chat.DUMP_KEEP_FILES

def test_dump_request_body_survives_unwritable_dir(tmp_path):
    """诊断落盘失败不影响聊天请求（路径不可用 → OSError 被吞掉）。

    `DATA_DIR/dumps` 已被占成普通文件时 `mkdir` 抛 `FileExistsError`：诊断失败
    绝不能把请求带崩，也不能向外抛任何异常。
    """
    from src.api import chat

    (tmp_path / "dumps").write_text("not a directory", encoding="utf-8")
    chat.dump_request_body(tmp_path, {"a": 1})      # 不应抛异常
    assert (tmp_path / "dumps").is_file()


def test_dump_files_are_owner_only_and_expire_after_a_day(tmp_path):
    """请求体含完整对话：目录 0700、文件 0600；条数没超也要按 24h 过期清理。"""
    import os
    import time as _time

    from src.api import chat

    chat.dump_request_body(tmp_path, {"messages": [{"role": "user", "content": "hi"}]})
    dump_dir = tmp_path / "dumps"
    written = list(dump_dir.glob("*.json"))
    assert len(written) == 1
    assert dump_dir.stat().st_mode & 0o777 == 0o700
    assert written[0].stat().st_mode & 0o777 == 0o600

    # 老版本建过 0755 的目录：下一次落盘必须把它收紧（mkdir 的 mode 只影响新建）
    dump_dir.chmod(0o755)
    chat.dump_request_body(tmp_path, {"a": 1})
    assert dump_dir.stat().st_mode & 0o777 == 0o700

    # 条数没超，但超过 24h 的文件也要被清掉（按 mtime 判）
    fresh = sorted(dump_dir.glob("*.json"))[-1]
    old = dump_dir / "0000000000001.json"
    old.write_text("old", encoding="utf-8")
    _time.sleep(0)
    os.utime(old, (0, 0))                    # mtime 归零 = 远超 24h
    chat.dump_request_body(tmp_path, {"b": 2})
    remaining = {item.name for item in dump_dir.glob("*.json")}
    assert old.name not in remaining
    assert fresh.name in remaining

def test_dump_request_body_trims_ignoring_unlink_errors(tmp_path, monkeypatch):
    """淘汰旧文件时遇到错误不抛出（suppress 分支）。"""
    from pathlib import Path as _Path

    from src.api import chat

    # 先造出比上限多的旧文件（文件名直接给定，避免同毫秒覆盖）
    dump_dir = tmp_path / "dumps"
    dump_dir.mkdir()
    for index in range(3):
        (dump_dir / f"{index:013d}.json").write_text("", encoding="utf-8")

    def boom(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(_Path, "unlink", boom)
    monkeypatch.setattr(chat, "DUMP_KEEP_FILES", 1)
    chat.dump_request_body(tmp_path, {"a": 1})      # 不应抛异常

# ------------------------------------------------- #28 限流前置（防 CPU 放大）


def test_transport_windows_checked_before_hashing(app_client_and_settings):
    """全局/IP 窗口超限时不再执行 PBKDF2（哈希前拦截）。"""
    from src.auth.throttle import LoginThrottle, ThrottleLimits

    app, client = app_client_and_settings
    hashes = []
    original = app.state.users.verify

    def counting_verify(username, password):
        hashes.append(username)
        return original(username, password)

    app.state.users.verify = counting_verify
    app.state.services.login_throttle = LoginThrottle(
        ThrottleLimits(max_per_ip=1, max_per_user=99, max_global=99))

    assert client.post("/api/auth/login",
                       json={"username": "root", "password": "bad"}).status_code == 401
    assert len(hashes) == 1
    # IP 窗口已满 → 第二个请求在哈希前被拒
    assert client.post("/api/auth/login",
                       json={"username": "root", "password": "rootpw"}).status_code == 429
    assert len(hashes) == 1

def test_correct_password_not_blocked_by_username_window(app_client_and_settings):
    """用户名窗口在哈希后判定：密码正确时不因他人刷错用户名而被拒。"""
    from src.auth.throttle import LoginThrottle, ThrottleLimits

    app, client = app_client_and_settings
    app.state.services.login_throttle = LoginThrottle(
        ThrottleLimits(max_per_user=1, max_per_ip=100, max_global=100))

    # 先烧掉该用户名的窗口（模拟他人用同用户名输错密码）
    app.state.services.login_throttle.record_failure(ip="9.9.9.9", username="root")
    response = client.post("/api/auth/login",
                           json={"username": "root", "password": "rootpw"})
    assert response.status_code == 200

def test_transport_windows_rejects_when_global_cap_reached():
    """全局窗口满时哈希前即拒（全局分支）。"""
    from src.auth.throttle import LoginThrottle, ThrottledError, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_global=1, max_per_ip=99, max_per_user=99))
    throttle.record_failure(ip="9.9.9.9", username="ghost")
    with pytest.raises(ThrottledError):
        throttle.check_transport_windows(ip="1.2.3.4")

def test_transport_windows_pass_within_limits():
    from src.auth.throttle import LoginThrottle, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_global=5, max_per_ip=5, max_per_user=5))
    throttle.check_transport_windows(ip="1.2.3.4")      # 不抛

def test_transport_windows_rejects_when_ip_cap_reached():
    from src.auth.throttle import LoginThrottle, ThrottledError, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_global=99, max_per_ip=1, max_per_user=99))
    throttle.record_failure(ip="1.2.3.4", username="ghost")
    with pytest.raises(ThrottledError):
        throttle.check_transport_windows(ip="1.2.3.4")

def test_check_covers_ip_window_directly():
    """check() 的 IP 分支（HTTP 流程已被前置检查挡掉，这里直接调用）。"""
    from src.auth.throttle import LoginThrottle, ThrottledError, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_global=99, max_per_ip=1, max_per_user=99))
    throttle.record_failure(ip="1.2.3.4", username="")
    with pytest.raises(ThrottledError):
        throttle.check(ip="1.2.3.4", username="")
