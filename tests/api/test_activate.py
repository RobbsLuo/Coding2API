"""账号激活端点：GET/POST 令牌校验、IP 级失败窗口、成功不计数。

激活端点无鉴权（令牌即凭证），因此这里守三条：
1. GET 与 POST 结果完全一致（前端可无痛切到 POST，令牌不进反代访问日志）。
2. 无效令牌累计到窗口上限 → 429（否则等于「无限次试令牌」，试出即接管账号）。
3. 成功不计数：合法用户不会被自己/他人的失败尝试连坐。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth.rbac import ROLE_ADMIN
from src.auth.throttle import LoginThrottle, ThrottleLimits
from src.auth.users import create_password_hash
from src.config import Settings
from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import UserRepository
from src.main import build_app
from tests.helpers.constants import SECRET


def _app(tmp_path, monkeypatch):
    """root 已存在、其余用户由用例经管理台创建（拿到 activate_token）。"""
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    repo = UserRepository(db)
    repo.create("root", create_password_hash("rootpw"), role=ROLE_ADMIN,
                enabled=True, now=1)
    db.close()
    return build_app(settings)


def _login(client) -> None:
    response = client.post("/api/auth/login",
                           json={"username": "root", "password": "rootpw"})
    assert response.status_code == 200


def _issue_token(client) -> str:
    _login(client)
    return client.post("/api/users", json={"username": "newbie",
                                           "role": "viewer"}).json()["activate_token"]


def _throttle(app, *, max_per_user: int = 2, max_per_ip: int = 100) -> None:
    app.state.services.login_throttle = LoginThrottle(
        ThrottleLimits(max_per_user=max_per_user, max_per_ip=max_per_ip,
                       max_global=100))


def test_describe_get_and_post_agree(tmp_path, monkeypatch):
    """GET（兼容旧前端）与 POST（令牌在 body）返回同一结果。"""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        token = _issue_token(client)
        get_body = client.get("/api/auth/activate", params={"token": token})
        post_body = client.post("/api/auth/activate/describe", json={"token": token})
    assert get_body.status_code == post_body.status_code == 200
    assert get_body.json() == post_body.json() == {"username": "newbie", "valid": True}


@pytest.mark.parametrize(("token", "message"), [
    ("", "activation token is required"),
    ("nope", "invalid or expired activation token"),
])
def test_describe_post_rejects_bad_token(tmp_path, monkeypatch, token, message):
    """POST 版与 GET 同口径：空/不存在/过期的令牌一律 400。"""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post("/api/auth/activate/describe", json={"token": token})
    assert response.status_code == 400
    assert response.json()["error"]["message"] == message


def test_describe_post_requires_token_key(tmp_path, monkeypatch):
    """body 里没有 token 键 → 与空令牌同口径 400（不是 500）。"""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post("/api/auth/activate/describe", json={})
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "activation token is required"


def test_invalid_token_is_throttled_per_user_key(tmp_path, monkeypatch):
    """同一枚无效令牌累计到窗口上限 → 429（与登录共用失败窗口）。"""
    app = _app(tmp_path, monkeypatch)
    _throttle(app)
    with TestClient(app) as client:
        for _ in range(2):
            assert client.post("/api/auth/activate/describe",
                               json={"token": "bogus-token"}).status_code == 400
        throttled = client.post("/api/auth/activate/describe",
                                json={"token": "bogus-token"})
        assert throttled.status_code == 429
        assert throttled.headers["retry-after"] == "60"


def test_invalid_token_get_is_throttled_too(tmp_path, monkeypatch):
    """GET 版同样受限流保护（不能靠换入口绕过窗口）。"""
    app = _app(tmp_path, monkeypatch)
    _throttle(app)
    with TestClient(app) as client:
        for _ in range(2):
            assert client.get("/api/auth/activate",
                              params={"token": "bogus-token"}).status_code == 400
        assert client.get("/api/auth/activate",
                          params={"token": "bogus-token"}).status_code == 429


def test_activate_with_invalid_token_is_throttled(tmp_path, monkeypatch):
    """改密端点（POST /api/auth/activate）同样计数：那是真正的接管入口。"""
    app = _app(tmp_path, monkeypatch)
    _throttle(app)
    payload = {"token": "bogus-token", "password": "longenough1"}
    with TestClient(app) as client:
        for _ in range(2):
            assert client.post("/api/auth/activate", json=payload).status_code == 400
        assert client.post("/api/auth/activate", json=payload).status_code == 429


def test_throttled_ip_window_blocks_other_tokens(tmp_path, monkeypatch):
    """IP 窗口跨令牌共享：换一枚无效令牌也挡得住（按令牌前缀分键挡不住换前缀）。"""
    app = _app(tmp_path, monkeypatch)
    _throttle(app, max_per_user=100, max_per_ip=2)
    with TestClient(app) as client:
        for index in range(2):
            assert client.post(
                "/api/auth/activate/describe",
                json={"token": f"bogus-{index}"}).status_code == 400
        assert client.post("/api/auth/activate/describe",
                           json={"token": "bogus-fresh"}).status_code == 429


def test_success_does_not_record_failure(tmp_path, monkeypatch):
    """成功校验不计数：连续成功任意多次都不触发 429。"""
    app = _app(tmp_path, monkeypatch)
    _throttle(app, max_per_user=1, max_per_ip=100)
    with TestClient(app) as client:
        token = _issue_token(client)
        for _ in range(3):
            assert client.get("/api/auth/activate",
                              params={"token": token}).status_code == 200
        # 窗口仍是干净的：同一 IP 的失败计数没有被成功的 record_success 之外的东西污染
        assert client.get("/api/auth/activate",
                          params={"token": token}).status_code == 200


def test_success_clears_its_own_failure_window(tmp_path, monkeypatch):
    """同令牌先失败后成功：成功清掉该令牌前缀的计数，下一次失败重新计。"""
    app = _app(tmp_path, monkeypatch)
    _throttle(app, max_per_user=2, max_per_ip=100)
    with TestClient(app) as client:
        token = _issue_token(client)
        # 先打满该令牌的失败窗口（token 不存在时同样按前缀计）
        assert client.post("/api/auth/activate/describe",
                           json={"token": token + "x"}).status_code == 400
        assert client.post("/api/auth/activate/describe",
                           json={"token": token + "x"}).status_code == 400
        assert client.post("/api/auth/activate/describe",
                           json={"token": token + "x"}).status_code == 429
        # 合法令牌（前缀相同）成功后窗口被清，可继续使用
        assert client.get("/api/auth/activate",
                          params={"token": token}).status_code == 200
        assert client.post("/api/auth/activate/describe",
                           json={"token": token + "y"}).status_code == 400


def test_activation_flow_still_works_with_throttle(tmp_path, monkeypatch):
    """限流不改变主流程：GET 校验 → POST 改密 → 一次性失效。"""
    app = _app(tmp_path, monkeypatch)
    _throttle(app)
    with TestClient(app) as client:
        token = _issue_token(client)
        assert client.get("/api/auth/activate",
                          params={"token": token}).json()["username"] == "newbie"
        done = client.post("/api/auth/activate",
                           json={"token": token, "password": "newbie-pw-1"})
        assert done.status_code == 200 and done.json()["username"] == "newbie"
        again = client.post("/api/auth/activate",
                            json={"token": token, "password": "another-pw-1"})
        assert again.status_code == 400
