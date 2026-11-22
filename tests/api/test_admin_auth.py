"""管理台登录 / 登出 / 会话与 build_app 启动前置。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


def test_login_success_sets_httponly_cookie(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        response = client.post("/api/auth/login",
                               json={"username": "root", "password": "rootpw"})
        assert response.status_code == 200
        assert response.json() == {"username": "root", "is_admin": True,
                                   "is_operator": True,
                                   "role": "admin", "must_change_password": False}
        cookie = response.headers["set-cookie"]
        assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
        assert client.get("/api/auth/session").json()["username"] == "root"

def test_login_sets_secure_cookie_only_on_https_entrypoint(tmp_path):
    """同一实例双入口：隧道(https)下发 Secure，LAN(http)不下发。

    Secure Cookie 会被浏览器在 http 上丢弃，所以不能写死；`Secure` 由请求
    的实际协议（X-Forwarded-Proto / Host）决定，两条入口才能都登录。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root",
                        ALLOWED_HOSTS="192.168.124.35,ai-api.robbs.work",
                        PUBLIC_BASE_URL="https://ai-api.robbs.work")
    app = build_app(settings)
    with TestClient(app) as client:
        tunnel = client.post(
            "/api/auth/login", json={"username": "root", "password": "rootpw"},
            headers={"Host": "ai-api.robbs.work", "X-Forwarded-Proto": "https"})
        assert tunnel.status_code == 200
        assert "secure" in tunnel.headers["set-cookie"].lower()

        lan = client.post(
            "/api/auth/login", json={"username": "root", "password": "rootpw"},
            headers={"Host": "192.168.124.35:8000"})
        assert lan.status_code == 200
        assert "secure" not in lan.headers["set-cookie"].lower()


def test_login_rejects_cross_origin_request(tmp_path):
    """login CSRF：跨站 Origin 的登录请求必须 403（无 Cookie 也校验）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        response = client.post("/api/auth/login",
                               json={"username": "root", "password": "rootpw"},
                               headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        # 同源 Origin 放行
        ok = client.post("/api/auth/login",
                         json={"username": "root", "password": "rootpw"},
                         headers={"Origin": "http://testserver"})
        assert ok.status_code == 200


def test_login_rejects_bad_password_and_unknown_user(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        for payload in ({"username": "root", "password": "wrong"},
                        {"username": "ghost", "password": "rootpw"},
                        {}):
            assert client.post("/api/auth/login", json=payload).status_code == 401

def test_logout_clears_session(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        assert client.post("/api/auth/logout").json() == {"ok": True}
        assert client.get("/api/auth/session").status_code == 401


def test_logout_bumps_epoch_so_stolen_cookie_dies(tmp_path):
    """登出必须 bump epoch：Cookie 是无状态 HMAC，只删浏览器那份的话，
    被复制走的 Cookie 仍能全效到 max_age（12h）到期。"""
    from src.auth.session import create_session_token

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        stolen = client.cookies.get("coding2api_session")
        epoch_before = app.state.services.users.session_epoch("root")

        assert client.post("/api/auth/logout").json() == {"ok": True}
        assert app.state.services.users.session_epoch("root") == epoch_before + 1

        # 攻击者手里仍握着旧 Cookie：epoch 失配 → 401
        client.cookies.set("coding2api_session", stolen)
        assert client.get("/api/auth/session").status_code == 401
        # 缺 ep 的老 Cookie 也被同一 bump 作废（旧值 0 ≠ 新 epoch）
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        assert client.get("/api/auth/session").status_code == 401


def test_logout_without_valid_session_still_succeeds(tmp_path):
    """会话已过期 / 坏签名时登出也必须成功，且不能炸。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        assert client.post("/api/auth/logout").json() == {"ok": True}
        client.cookies.set("coding2api_session", "garbage.signature")
        response = client.post("/api/auth/logout")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


def test_logout_bumps_only_existing_user(tmp_path):
    """签名有效但用户已被删除：跳过 bump，仍返回成功。"""
    from src.auth.session import create_session_token

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        app.state.services.user_repo.delete("guest")
        client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
        assert client.post("/api/auth/logout").json() == {"ok": True}


def test_logout_deletes_cookie_with_issue_session_attributes(tmp_path):
    """https 入口：删除 Cookie 必须带 Secure，否则浏览器留下一条同名明文
    记录（cookie tossing），看起来登出没生效。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root",
                        ALLOWED_HOSTS="ai-api.robbs.work",
                        PUBLIC_BASE_URL="https://ai-api.robbs.work")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        response = client.post("/api/auth/logout",
                               headers={"Host": "ai-api.robbs.work",
                                        "X-Forwarded-Proto": "https"})
        cookie = response.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie

def test_session_endpoint_requires_login(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/auth/session").status_code == 401


def test_login_loopback_peer_with_forwarded_header_is_throttled(tmp_path):
    """反代回源（peer=127.0.0.1 + XFF）必须按 IP 窗口计数并 429。

    docker-compose 端口绑 127.0.0.1、TRUST_PROXY 默认 false 时 peer 恒为回环，
    若无条件豁免回环，IP 窗口全废、只剩用户名窗口（可跨用户名撞库）。
    """
    from src.auth.throttle import LoginThrottle, ThrottleLimits

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    app.state.services.login_throttle = LoginThrottle(
        ThrottleLimits(max_per_ip=2, max_per_user=100, max_global=100))
    # 对端固定回环：模拟反代回源
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        proxied = {"X-Forwarded-For": "203.0.113.9"}
        for name in ("root", "guest"):
            assert client.post("/api/auth/login",
                               json={"username": name, "password": "bad"},
                               headers=proxied).status_code == 401
        # 第三次：换用户名也照样 429（IP 窗口而非用户名窗口命中）
        assert client.post("/api/auth/login",
                           json={"username": "alice", "password": "bad"},
                           headers=proxied).status_code == 429
        # 无 XFF 的本机直连仍豁免：IP 窗口已满，但直连请求照常返回 401
        with TestClient(app, client=("127.0.0.1", 50001)) as direct:
            for _ in range(5):
                assert direct.post(
                    "/api/auth/login",
                    json={"username": "ghost", "password": "bad"}).status_code == 401

def test_session_from_previous_database_rejected(tmp_path):
    """服务器重置 / 换机后整库重建：同 APP_SECRET 下旧 Cookie 不得再进后台。

    issue #37：新库 session_epoch 从 0 重来，旧 Cookie 的 ep=0 会重新匹配；
    账号名字不变、只是密码变了的情况下，浏览器刷新就直接进了后台。
    签发时间早于当前账号创建 → 必然是上一个库签的，按未认证处理。
    """
    from src.auth.session import create_session_token

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        stale = create_session_token("root", SECRET, issued_at=1)
        client.cookies.set("coding2api_session", stale)
        assert client.get("/api/auth/session").status_code == 401

def test_build_app_fails_without_users_file(tmp_path, monkeypatch):
    """文件缺失**且**库为空才是致命错误（库非空时文件缺失可正常启动）。"""
    from src.auth.users import UsersFileError

    monkeypatch.setenv("USERS_FILE", str(tmp_path / "missing.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    with pytest.raises(UsersFileError):
        build_app(settings)

def test_build_app_starts_without_users_file_when_db_has_users(tmp_path, monkeypatch):
    """库已有用户时，users.txt 缺失不再是启动障碍（部署忘挂文件不再等于不可用）。"""
    from src.auth.users import create_password_hash
    from src.db.conn import Database
    from src.db.migrate import apply_schema
    from src.db.repo import UserRepository

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    UserRepository(db).create("root", create_password_hash("rootpw"),
                              role="admin", now=1)
    db.close()
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "missing.txt"))

    app = build_app(settings)
    with TestClient(app) as client:
        assert client.post("/api/auth/login",
                           json={"username": "root", "password": "rootpw"}).status_code == 200

def test_build_app_with_invalid_users_file_falls_back_to_db(tmp_path, monkeypatch):
    """文件存在但格式非法：当作没有文件，库里的用户说了算，而不是启动失败。

    users_file 必须显式传给 Settings：`Settings(_env_file=None)` 在构造时就读 OS env，
    若先构造再 monkeypatch.setenv，读到的仍是 conftest 的合法文件，这条测试就空了。
    """
    broken = tmp_path / "broken.txt"
    broken.write_text("this-line-has-no-colon\n", encoding="utf-8")
    monkeypatch.setenv("USERS_FILE", str(broken))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", USERS_FILE=str(broken))

    from src.auth.users import create_password_hash
    from src.db.conn import Database
    from src.db.migrate import apply_schema
    from src.db.repo import UserRepository

    db = Database(settings.db_path)
    apply_schema(db.connect())
    UserRepository(db).create("root", create_password_hash("rootpw"),
                              role="admin", now=1)
    db.close()

    app = build_app(settings)
    with TestClient(app) as client:
        logged = client.post("/api/auth/login",
                             json={"username": "root", "password": "rootpw"})
        assert logged.status_code == 200
        assert client.get("/api/auth/session").json()["role"] == "admin"
