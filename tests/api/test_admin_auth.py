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

def test_session_endpoint_requires_login(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/auth/session").status_code == 401

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
