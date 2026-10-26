"""全仓静态审查修掉的后端缺陷的回归测试。

这一组守着六条「静默出错」的语义（每条都曾真实发生）：

1. DEAD 尝试已落明细 → 轮换耗尽不再落收尾行（一次请求一行，Key 配额不双扣）
2. 管理台降级不被重启撤销（`ADMIN_USERNAMES` 只在引导期提权）
3. JSON 溢出成 inf 的 `1e999` → 400 而不是 500
4. pin 端点：非字符串 id → 400；不存在的 id → 400 且不清掉别人的 pin
5. 国际站白名单含国内站主机 → 启动报错（跨站发送即凭证泄露）
6. 统计 `metric` 未知取值 → 400 而不是静默回落
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.auth.bootstrap import bootstrap_users
from src.auth.rbac import ROLE_ADMIN, ROLE_VIEWER
from src.auth.users import create_password_hash
from src.config import Settings, validate_codebuddy_intl_endpoint_allowed
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository, UserRepository
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import Scheduler
from src.main import build_app
from src.provider.base import ErrKind
from tests.helpers.constants import SECRET
from tests.helpers.providers import ScriptedProvider

# ---------------------------------------------------------------- 1. DEAD 双落


class _Dead(Exception):
    """上游报「session 失效」的替身异常（kind = DEAD）。"""

    def kind(self) -> ErrKind:
        return ErrKind.DEAD


class _Collector:
    """记录每次 `record` 调用，便于断言「落了几行明细」。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, **fields) -> None:
        self.events.append(fields)


def _dead_executor(tmp_path, *, max_rotate: int = 1):
    db = Database(tmp_path / "dead.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    for index in range(3):
        credentials.add(provider="trae", credential_data={"accessToken": f"a{index}"})
    collector = _Collector()
    executor = Executor(ExecutorDeps(
        providers={"trae": ScriptedProvider([[_Dead()]])}, credentials=credentials,
        scheduler=Scheduler(max_rotate=max_rotate), default_model="glm-5.2",
        stats=collector))
    return executor, collector, db


async def test_dead_exhaustion_records_single_row(tmp_path):
    """DEAD 尝试行已落 → 轮换耗尽不再落收尾行（一次请求一行明细）。"""
    from tests.helpers.chat import chat_request

    executor, collector, db = _dead_executor(tmp_path)
    try:
        with pytest.raises(NoHealthyCredential):
            await executor.complete(chat_request())
    finally:
        db.close()
    assert [event["error_type"] for event in collector.events] == [
        "credential_unavailable"]


async def test_dead_exhaustion_does_not_double_count_requests(tmp_path):
    """Key 配额按明细行数计：双落会被扣两次日请求额度。"""
    from tests.helpers.chat import chat_request

    executor, collector, db = _dead_executor(tmp_path)
    try:
        with pytest.raises(NoHealthyCredential):
            await executor.complete(chat_request())
    finally:
        db.close()
    assert len(collector.events) == 1


async def test_stream_dead_exhaustion_records_single_row(tmp_path):
    """流式路径同口径：DEAD 已落尝试行 → 选号耗尽不再落收尾行。"""
    from tests.helpers.chat import chat_request

    db = Database(tmp_path / "dead-stream.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    collector = _Collector()
    executor = Executor(ExecutorDeps(
        providers={"trae": ScriptedProvider([[_Dead()]])}, credentials=credentials,
        scheduler=Scheduler(max_rotate=1), default_model="glm-5.2", stats=collector))
    try:
        chunks = [chunk async for chunk in executor.stream(chat_request())]
    finally:
        db.close()
    assert chunks                                    # 以错误帧收尾，不是静默断开
    assert [event["error_type"] for event in collector.events] == [
        "credential_unavailable"]


# ------------------------------------------------------- 2. 重启撤销管理台降级


class _Config:
    """bootstrap 只用到 `admin_set` 一个属性的最小桩。"""

    def __init__(self, admins=()) -> None:
        self.admin_set = frozenset(admins)


def test_bootstrap_does_not_repromote_when_active_admin_exists(tmp_path):
    """库里有活跃 admin → env 不再改写任何人的角色（降级不被重启撤销）。"""
    db = Database(tmp_path / "boot.sqlite3")
    apply_schema(db.connect())
    repo = UserRepository(db)
    repo.create("root", create_password_hash("a"), role=ROLE_ADMIN, now=1)
    repo.create("boss", create_password_hash("b"), role=ROLE_ADMIN, now=1)
    repo.update_role("root", ROLE_VIEWER, bump_epoch=True)

    bootstrap_users(_Config(["root"]), repo)

    assert repo.get("root")["role"] == ROLE_VIEWER
    db.close()


def test_bootstrap_promotes_during_bootstrap_period(tmp_path):
    """引导期（库里没有活跃 admin）仍按 env 提权：老部署升级的出路。"""
    db = Database(tmp_path / "boot2.sqlite3")
    apply_schema(db.connect())
    repo = UserRepository(db)
    repo.create("root", create_password_hash("a"), role=ROLE_VIEWER, now=1)

    bootstrap_users(_Config(["root"]), repo)

    assert repo.get("root")["role"] == ROLE_ADMIN
    db.close()


def test_restart_keeps_console_downgrade(admin_client, tmp_path):
    """端到端：管理台降级 → 重启 → 角色仍是降级后的值。"""
    app, _client = admin_client
    app.state.user_repo.create("boss", create_password_hash("bosspw"),
                               role=ROLE_ADMIN, now=1)
    app.state.user_repo.update_role("root", ROLE_VIEWER, bump_epoch=True)

    restarted = build_app(app.state.settings)

    assert restarted.state.user_repo.get("root")["role"] == ROLE_VIEWER


# ------------------------------------------------------------- 3. 1e999 → 400


def _app_with_key(admin_client):
    app, _client = admin_client
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    return app, app.state.api_keys.create("root")["api_key"]


def _chat_app(tmp_path):
    """装配一个带 trae provider 的 app + API Key（聊天请求要有真上游才 200）。"""
    from tests.helpers.events import TRAE_GOOD

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": ScriptedProvider([TRAE_GOOD])})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    return app, app.state.api_keys.create("root")["api_key"]


def _chat_with_key(app, key, body: str):
    """发一次聊天请求（用装配好的 trae provider，返回响应）。"""
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post("/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           content=body)


def test_json_overflow_number_is_400_not_500(tmp_path, monkeypatch):
    """`1e999` 解析成 inf：应 400，不能走到 httpx 序列化炸成 500。"""
    app, key = _chat_app(tmp_path)
    response = _chat_with_key(app, key,
                              ('{"messages":[{"role":"user","content":"hi"}],'
                               '"model":"glm-5.2","extra_num":1e999}'))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_json_overflow_in_nested_array_is_400(tmp_path, monkeypatch):
    """嵌套数组里的 inf 同样拦（逐字段校验覆盖不到的位置）。"""
    app, key = _chat_app(tmp_path)
    response = _chat_with_key(app, key,
                              ('{"messages":[{"role":"user","content":"hi"}],'
                               '"model":"glm-5.2","extra":[1,2e999]}'))
    assert response.status_code == 400


def test_finite_exponent_number_still_accepted(tmp_path, monkeypatch):
    """正常的浮点指数（1e3）不受影响：拦的是溢出，不是小数。"""
    app, key = _chat_app(tmp_path)
    response = _chat_with_key(app, key, json.dumps(
        {"messages": [{"role": "user", "content": "hi"}],
         "model": "glm-5.2", "extra": 1e3}))
    assert response.status_code == 200


# --------------------------------------------------------------- 4. pin 端点


def test_pin_rejects_non_string_credential_id(admin_client):
    """dict / list 形态的 id → 400（此前走到 sqlite 参数绑定 → 500）。"""
    app, client = admin_client
    for bad in ({"a": 1}, ["a"], 1, True):
        response = client.post("/api/credentials/pin", json={"credential_id": bad})
        assert response.status_code == 400, bad
        assert response.json()["error"]["code"] == "invalid_request"


def test_pin_unknown_credential_is_400_and_keeps_existing_pin(admin_client):
    """不存在的 id → 400，且**不清掉**别的凭证已有的 pin。"""
    app, client = admin_client
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    real = app.state.credentials.candidates()[0].credential_id
    assert client.post("/api/credentials/pin",
                       json={"credential_id": real}).status_code == 200

    response = client.post("/api/credentials/pin",
                           json={"credential_id": "no-such-id"})

    assert response.status_code == 400
    pinned = [c.credential_id for c in app.state.credentials.candidates() if c.pinned]
    assert pinned == [real]


def test_pin_none_clears_pin(admin_client):
    """`credential_id: null` = 取消指定，仍然合法。"""
    app, client = admin_client
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    real = app.state.credentials.candidates()[0].credential_id
    client.post("/api/credentials/pin", json={"credential_id": real})
    assert client.post("/api/credentials/pin",
                       json={"credential_id": None}).status_code == 200
    assert [c for c in app.state.credentials.candidates() if c.pinned] == []


def test_set_pinned_returns_false_for_unknown_id(tmp_path):
    """仓储层：目标不存在时返回 False 且不动既有 pin。"""
    db = Database(tmp_path / "pin.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    real = credentials.candidates()[0].credential_id
    assert credentials.set_pinned(real) is True
    assert credentials.set_pinned("nope") is False
    assert [c.credential_id for c in credentials.candidates() if c.pinned] == [real]
    db.close()


# ------------------------------------------------------ 5. 国际站白名单跨站校验


def test_intl_whitelist_rejects_cn_host():
    """国际站白名单里含国内站主机 → 一律拒绝（跨站发送即凭证泄露）。"""
    settings = Settings(
        _env_file=None, APP_SECRET=SECRET,
        CODEBUDDY_INTL_ALLOWED_ENDPOINTS="https://copilot.tencent.com")
    assert validate_codebuddy_intl_endpoint_allowed(
        "https://copilot.tencent.com", settings) is False


def test_intl_whitelist_rejects_cn_host_with_port_and_path():
    """带端口/路径的国内站地址同样要拦住（比主机名而非整串）。"""
    settings = Settings(
        _env_file=None, APP_SECRET=SECRET,
        CODEBUDDY_INTL_ALLOWED_ENDPOINTS="https://copilot.tencent.com:443/api")
    assert validate_codebuddy_intl_endpoint_allowed(
        "https://copilot.tencent.com:443/api", settings) is False


def test_intl_whitelist_allows_intl_host():
    """正常配置不受影响。"""
    settings = Settings(
        _env_file=None, APP_SECRET=SECRET,
        CODEBUDDY_INTL_ALLOWED_ENDPOINTS="https://www.codebuddy.ai")
    assert validate_codebuddy_intl_endpoint_allowed(
        "https://www.codebuddy.ai", settings) is True


def test_intl_whitelist_with_cn_host_fails_startup(tmp_path, monkeypatch):
    """白名单填国内站 → 启动报错（不静默放行）。

    先建好用户再触发启动：端点解析发生在用户引导之前，报的是配置错
    （`ValueError`）而不是「没有可用用户」（`UsersFileError`）。
    """
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
        ADMIN_USERNAMES="root",
        CODEBUDDY_INTL_ALLOWED_ENDPOINTS="https://copilot.tencent.com")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    UserRepository(db).create("root", create_password_hash("pw"),
                               role=ROLE_ADMIN, now=1)
    db.close()

    with pytest.raises(ValueError, match="CODEBUDDY_INTL_API_ENDPOINT"):
        build_app(settings)


# ------------------------------------------------------------ 6. metric 取值域


def test_stats_timeline_rejects_unknown_metric(admin_client):
    """未知 metric → 400（此前静默回落 requests，错误被吞掉）。"""
    _app, client = admin_client
    response = client.get("/api/stats/timeline", params={"metric": "bogus"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_stats_model_timeline_rejects_unknown_metric(admin_client):
    _app, client = admin_client
    response = client.get("/api/stats/model-timeline", params={"metric": "bogus"})
    assert response.status_code == 400


def test_stats_percentile_timeline_rejects_non_latency_metric(admin_client):
    """分位时序只认 latency / ttfb：其它 metric → 400。"""
    _app, client = admin_client
    response = client.get("/api/stats/percentile-timeline",
                          params={"metric": "requests"})
    assert response.status_code == 400
    ok = client.get("/api/stats/percentile-timeline", params={"metric": "ttfb"})
    assert ok.status_code == 200


def test_stats_timeline_accepts_all_known_metrics(admin_client):
    """取值域内的 metric 全部放行。"""
    _app, client = admin_client
    for metric in ("requests", "tokens", "cost", "latency", "ttfb",
                   "success_rate", "cache_rate", "cost_per_request"):
        response = client.get("/api/stats/timeline", params={"metric": metric})
        assert response.status_code == 200, metric
