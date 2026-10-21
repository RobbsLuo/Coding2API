"""凭证运维端点：管理标记、到期积分、模型冷却与 token 到期透出。"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.scheduler import ErrorOutcome
from src.main import build_app
from tests.helpers.chat import jwt_token as _jwt
from tests.helpers.constants import SECRET


def test_credentials_endpoint_exposes_admin_flag(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "guest", "password": "guestpw"})
        body = client.get("/api/credentials").json()
        assert body["viewer"] == "guest" and body["is_admin"] is False
        assert body["is_operator"] is False

def test_credentials_endpoint_exposes_expiring_credits(tmp_path):
    """到期指标随列表下发（与调度排序同源）；无周期信息为 None，套餐阶梯同源下发。"""
    from src.db.repo import _ladder_text

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        repo = app.state.credentials
        repo.add(provider="codebuddy", credential_data={"bearer_token": "t"})
        repo.add(provider="trae", credential_data={"token": "x"})
        ids = {row["provider"]: row["id"] for row in repo.list_all()}
        now = int(time.time())
        ladder = _ladder_text([(now + 3600, 100.0), (now + 3 * 86400, 40.0),
                               (now + 999_999, 50.0)])
        conn = repo._db.connect()
        conn.execute(
            "UPDATE credentials SET quota_expiry_ladder = ?, quota_packages = ? WHERE id = ?",
            (ladder, json.dumps([{"name": "福利积分", "total": 2000.0, "used": 0.0,
                                  "end": now + 3600}]), ids["codebuddy"]))
        conn.commit()
        body = client.get("/api/credentials").json()

    rows = {row["provider"]: row for row in body["credentials"]}
    assert body["expiry_window_seconds"] == settings.quota_expiry_window_seconds
    assert body["expiry_secondary_window_seconds"] == \
        settings.quota_expiry_secondary_window_seconds
    assert rows["codebuddy"]["quota_expiring_credits"] == 100.0
    # 次窗口是 36h 窗口的超集：主窗口已含的包也计入（这里 100 + 40）
    assert rows["codebuddy"]["quota_expiring_credits_secondary"] == 140.0
    assert rows["codebuddy"]["quota_expiry_ladder"] == [
        [now + 3600, 100.0], [now + 3 * 86400, 40.0], [now + 999_999, 50.0]]
    # 福利链路下线后不再下发任何积分账派生字段
    for row in (rows["codebuddy"], rows["trae"]):
        for key in ("quota_credits_expiring", "quota_credits_expiring_secondary",
                    "quota_credits_health", "quota_credits_expiry_ladder"):
            assert key not in row
    assert rows["trae"]["quota_expiring_credits"] is None
    assert rows["trae"]["quota_expiring_credits_secondary"] is None
    assert rows["trae"]["quota_expiry_ladder"] is None
    # 额度包明细（展示用）同样随列表下发；无明细的渠道为 None
    assert rows["codebuddy"]["quota_packages"] == [
        {"name": "福利积分", "total": 2000.0, "used": 0.0, "end": now + 3600}]
    assert rows["trae"]["quota_packages"] is None
    # 窗口关闭 → 0 而不是 None，展示层据此隐藏该行（两级窗口各自独立）
    closed = {row["provider"]: row for row in repo.list_all(
        expiring_window=0, now=now)}
    assert closed["codebuddy"]["quota_expiring_credits"] == 0.0
    assert closed["codebuddy"]["quota_expiring_credits_secondary"] == 0.0
    secondary_closed = {row["provider"]: row for row in repo.list_all(
        expiring_window=settings.quota_expiry_window_seconds,
        expiring_secondary_window=0, now=now)}
    assert secondary_closed["codebuddy"]["quota_expiring_credits"] == 100.0
    assert secondary_closed["codebuddy"]["quota_expiring_credits_secondary"] == 0.0

def test_list_all_exposes_model_cooldowns(tmp_path):
    """管理台列表带出生效中的模型冷却（过期行不下发）。"""
    from src.engine.scheduler import ModelCooldown

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    now = int(time.time())
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "fresh": ModelCooldown(cooling_until=now + 600, hits=2, reason="blocked")}))
    credentials.save_error(cid, ErrorOutcome(model_cooldowns={
        "stale": ModelCooldown(cooling_until=now - 600, hits=1)}))
    row = credentials.list_all(now=now)[0]
    assert row["model_cooldowns"] == [
        {"model": "fresh", "cooling_until": now + 600, "hits": 2, "reason": "blocked"}]
    assert CredentialRepository(
        db, CredentialCipher(SECRET)).list_all(now=now)[0]["model_cooldowns"] == row[
            "model_cooldowns"]
    db.close()

def test_credentials_endpoint_exposes_model_cooldowns(tmp_path):
    """端到端：模型级冷却经 /api/credentials 下发（前端据此展示）。"""
    from src.engine.scheduler import ModelCooldown

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        repo = app.state.credentials
        cid = repo.add(provider="codebuddy", credential_data={"bearer_token": "t"})
        repo.save_error(cid, ErrorOutcome(model_cooldowns={
            "glm-5.2": ModelCooldown(cooling_until=int(time.time()) + 600,
                                     hits=1, reason="model")}))
        rows = client.get("/api/credentials").json()["credentials"]
    codebuddy = next(row for row in rows if row["provider"] == "codebuddy")
    assert [row["model"] for row in codebuddy["model_cooldowns"]] == ["glm-5.2"]

def test_credentials_endpoint_exposes_token_expiry(tmp_path):
    """端到端：/api/credentials 下发 token 签发/到期秒数与预警阈值。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", TOKEN_EXPIRY_WARNING_SECONDS=7200)
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        repo = app.state.credentials
        now = int(time.time())
        repo.add(provider="codebuddy",
                 credential_data={"bearer_token": _jwt(now + 600, iat=now)}, now=now)
        repo.add(provider="trae", credential_data={"accessToken": "plain"}, now=now)
        body = client.get("/api/credentials").json()
    assert body["token_expiry_warning_seconds"] == 7200
    rows = {row["provider"]: row for row in body["credentials"]}
    assert rows["codebuddy"]["token_expires_at"] > now
    assert rows["codebuddy"]["token_issued_at"] == now
    # 未知到期下发 0（展示层隐藏），不是 0 之外的哨兵值
    assert rows["trae"]["token_expires_at"] == 0
    assert rows["trae"]["token_issued_at"] == 0
