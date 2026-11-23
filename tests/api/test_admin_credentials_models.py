"""凭证编辑的乐观并发（409）与账号级模型白名单（PUT .../limits）。

协议契约（P2-19 / v39）：
- 请求可带 `expected_updated_at`（来自列表每行的 `updated_at`）；
- 不一致 → 409 + `current` 快照（当前版本、当前白名单、当前护栏），一行不改；
- 不传 = 旧行为（兼容脚本），但管理台必须传。

模型白名单（P2-20）：与限额同一整表替换端点，`''` = 不限制，非法 glob 400；
只收窄**对话选号**的候选集，不硬禁用、不写冷却、不影响后台任务。
"""

from __future__ import annotations


def _add(app, provider="codebuddy"):
    return app.state.credentials.add(provider=provider,
                                     credential_data={"bearer_token": "t"})


def _row(client, credential_id):
    rows = client.get("/api/credentials").json()["credentials"]
    return next(row for row in rows if row["id"] == credential_id)


def test_set_limits_returns_edit_version(admin_client):
    """写入响应带新版本号，列表每行也能读到（前端据此带 expected_updated_at）。"""
    app, client = admin_client
    credential_id = _add(app)
    response = client.put(f"/api/credentials/{credential_id}/limits",
                          json={"daily_tokens": 1000})
    assert response.status_code == 200
    version = response.json()["updated_at"]
    assert version > 0
    row = _row(client, credential_id)
    assert row["updated_at"] == version
    # 人工编辑（标签）也会推进版本——两个编辑入口共享同一个版本号
    client.put(f"/api/credentials/{credential_id}/tags", json={"tags": ["a"]})
    assert _row(client, credential_id)["updated_at"] > version


def test_set_limits_matching_version_succeeds(admin_client):
    app, client = admin_client
    credential_id = _add(app)
    version = client.put(f"/api/credentials/{credential_id}/limits",
                         json={"daily_tokens": 1000}).json()["updated_at"]

    response = client.put(f"/api/credentials/{credential_id}/limits",
                          json={"daily_tokens": 2000,
                                "expected_updated_at": version})
    assert response.status_code == 200
    assert response.json()["limits"]["daily_tokens"] == 2000
    assert response.json()["updated_at"] > version


def test_stale_version_returns_409_with_current_snapshot(admin_client):
    """陈旧版本：409 + 当前快照，且库里一行都没被覆盖（只有一次写成功）。

    这就是「两人同编后保存者静默赢」的反面：后到的那次被拒绝并拿到现状，
    不会把先到者的白名单/护栏冲掉。
    """
    app, client = admin_client
    credential_id = _add(app)
    first = client.put(
        f"/api/credentials/{credential_id}/limits",
        json={"daily_tokens": 1000, "allowed_models": "glm-*",
              "expected_updated_at": 0})
    assert first.status_code == 200
    version = first.json()["updated_at"]

    stale = client.put(
        f"/api/credentials/{credential_id}/limits",
        json={"daily_tokens": 5, "allowed_models": "kimi-*",
              "expected_updated_at": 0})
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"]["code"] == "credential_conflict"
    assert "已被他人修改" in body["error"]["message"]
    assert body["current"] == {
        "id": credential_id,
        "updated_at": version,
        "allowed_models": "glm-*",
        "limits": body["current"]["limits"],
    }
    assert body["current"]["limits"]["daily_tokens"] == 1000

    # 库里仍是先到者的版本：后到的那次一行没写
    row = _row(client, credential_id)
    assert row["allowed_models"] == "glm-*"
    assert row["limits"]["daily_tokens"] == 1000


def test_concurrent_writes_only_one_succeeds(admin_client):
    """同一版本的两个写请求：只有一个 200。"""
    app, client = admin_client
    credential_id = _add(app)
    version = _row(client, credential_id)["updated_at"]
    statuses = [
        client.put(f"/api/credentials/{credential_id}/limits",
                   json={"daily_tokens": tokens, "expected_updated_at": version}
                   ).status_code
        for tokens in (10, 20)
    ]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 1


def test_other_edit_endpoint_advances_version_and_wins_the_race(admin_client):
    """别的编辑端点（标签）先保存 → 手上的限额版本变陈旧 → 409。

    这是「两人同编」最常见的形态：A 打开限额对话框，B 改了标签/权重，A 再
    保存时不能把 B 的改动当成不存在。
    """
    app, client = admin_client
    credential_id = _add(app)
    opened_at = _row(client, credential_id)["updated_at"]

    client.put(f"/api/credentials/{credential_id}/tags", json={"tags": ["b"]})

    response = client.put(f"/api/credentials/{credential_id}/limits",
                          json={"daily_tokens": 1, "expected_updated_at": opened_at})
    assert response.status_code == 409
    assert response.json()["current"]["updated_at"] > opened_at
    assert response.json()["current"]["limits"] is None


def test_limits_without_expected_version_keeps_legacy_behavior(admin_client):
    """不传版本 = 兼容口子：既有脚本照常整体覆盖（最后写入者赢）。"""
    app, client = admin_client
    credential_id = _add(app)
    first = client.put(f"/api/credentials/{credential_id}/limits",
                       json={"daily_tokens": 1000})
    second = client.put(f"/api/credentials/{credential_id}/limits",
                        json={"daily_tokens": 2000})
    assert first.status_code == second.status_code == 200
    assert second.json()["limits"]["daily_tokens"] == 2000


def test_invalid_expected_version_is_400(admin_client):
    app, client = admin_client
    credential_id = _add(app)
    for value in ("1", True, -1, 1.5):
        response = client.put(f"/api/credentials/{credential_id}/limits",
                              json={"daily_tokens": 1, "expected_updated_at": value})
        assert response.status_code == 400, value
        assert "expected_updated_at" in response.json()["error"]["message"]


def test_model_whitelist_round_trip_and_audit(admin_client):
    """白名单落库 + 回显 + 审计（明细只记 glob，不含凭证明文）。"""
    app, client = admin_client
    credential_id = _add(app)
    response = client.put(
        f"/api/credentials/{credential_id}/limits",
        json={"daily_tokens": 1000, "allowed_models": " GLM-* , kimi-k3 "})
    assert response.status_code == 200
    assert response.json()["allowed_models"] == "GLM-*,kimi-k3"
    assert _row(client, credential_id)["allowed_models"] == "GLM-*,kimi-k3"

    events = app.state.audit.query(action="credential.limits")
    assert len(events) == 1
    assert "模型白名单 GLM-*,kimi-k3" in events[0]["detail"]
    # 审计与响应都不得出现凭证明文（导入时用的 bearer_token 值）
    assert "bearer_token" not in events[0]["detail"]
    assert "t" not in {part.strip() for part in events[0]["detail"].split("；")}


def test_empty_model_whitelist_clears_restriction(admin_client):
    app, client = admin_client
    credential_id = _add(app)
    client.put(f"/api/credentials/{credential_id}/limits",
               json={"allowed_models": "glm-*"})
    client.put(f"/api/credentials/{credential_id}/limits",
               json={"allowed_models": ""})
    assert _row(client, credential_id)["allowed_models"] == ""


def test_missing_allowed_models_keeps_existing_whitelist(admin_client):
    """只改限额的老调用方不发该字段：不得把别人设的白名单静默清成「不限制」。"""
    app, client = admin_client
    credential_id = _add(app)
    client.put(f"/api/credentials/{credential_id}/limits",
               json={"allowed_models": "glm-*"})
    response = client.put(f"/api/credentials/{credential_id}/limits",
                          json={"daily_tokens": 100})
    assert response.status_code == 200
    assert response.json()["allowed_models"] == "glm-*"
    assert _row(client, credential_id)["allowed_models"] == "glm-*"
    # 审计只写限额（没动白名单就不声称动过）：两次写里只有第一次提到白名单。
    # 不依赖查询顺序（同一秒内的两行排序不保证）——按「哪几次提到」计数。
    details = [event["detail"] for event in app.state.audit.query(
        action="credential.limits")]
    assert len(details) == 2
    assert sum("模型白名单 glm-*" in detail for detail in details) == 1
    assert sum("模型白名单" not in detail for detail in details) == 1


def test_invalid_model_whitelist_is_400(admin_client):
    app, client = admin_client
    credential_id = _add(app)
    too_long = "m" * 129
    too_many = ",".join(f"m{i}" for i in range(129))
    for value in (too_long, too_many):
        response = client.put(f"/api/credentials/{credential_id}/limits",
                              json={"allowed_models": value})
        assert response.status_code == 400
        assert "allowed_models" in response.json()["error"]["message"]
    # 400 的请求不得落库、也不得推进版本
    assert _row(client, credential_id)["updated_at"] == 0


def test_model_whitelist_narrows_candidates_without_touching_state(admin_client):
    """白名单只摘候选：不硬禁用、不写冷却，别的模型/后台任务照常看到它。"""
    app, client = admin_client
    credential_id = _add(app)
    client.put(f"/api/credentials/{credential_id}/limits",
               json={"allowed_models": "glm-*"})

    row = _row(client, credential_id)
    assert row["enabled"] == 1
    assert row["disabled"] == 0
    assert row["cooling_until"] is None
    assert row["model_cooldowns"] == []

    credentials = app.state.credentials
    assert [c.credential_id for c in credentials.candidates(
        ["codebuddy"], model="glm-4.6")] == [credential_id]
    assert credentials.candidates(["codebuddy"], model="kimi-k3") == []
    # 后台任务（签到/刷新/探活/额度探测）不带 model：仍是全量
    assert [c.credential_id for c in credentials.candidates(["codebuddy"])] == [
        credential_id]


async def test_chat_path_falls_back_to_whitelisted_credential(admin_client):
    """对话选号按白名单换号（P2-20 的落点：候选生成层）。

    两个 codebuddy 号：A 只允许 glm-*，B 不限。请求 kimi-k3 时 A 不参与，
    请求落到 B——**且 A 没有因此被停用/冷却**（不是「模型被封就整号停用」）。
    随后请求 glm-4.6 时 A 又能被选中。
    """
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler
    from tests.api._support import RejectProvider
    from tests.helpers.chat import chat_request
    from tests.helpers.events import GOOD

    app, client = admin_client
    restricted = _add(app)
    open_one = _add(app)
    client.put(f"/api/credentials/{restricted}/limits", json={"allowed_models": "glm-*"})
    # 健康度拉开：不限白名单时 A 一定先被选中（证明是白名单在起作用）
    db = app.state.credentials._db
    db.connect().execute("UPDATE credentials SET health = 100 WHERE id = ?", (restricted,))
    db.connect().execute("UPDATE credentials SET health = 10 WHERE id = ?", (open_one,))
    db.connect().commit()

    provider = RejectProvider("codebuddy", [GOOD])
    served: list[str] = []
    original = provider.stream_chat

    async def tracking(credential_data, payload, model):
        served.append(str(credential_data))
        async for event in original(credential_data, payload, model):
            yield event

    provider.stream_chat = tracking
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=app.state.credentials,
        scheduler=Scheduler(), default_model="kimi-k3"))

    await executor.complete(chat_request("kimi-k3"), username="u")
    # 请求真打到了不限白名单的 B（A 连一次都没被调用）
    assert len(served) == 1
    # A 没被白名单改动状态
    row = _row(client, restricted)
    assert (row["enabled"], row["disabled"], row["cooling_until"]) == (1, 0, None)

    # 换成 A 允许的模型：健康度 100 的 A 立刻回到候选首位
    await executor.complete(chat_request("glm-4.6"), username="u")
    assert app.state.credentials.candidates(
        ["codebuddy"], model="glm-4.6")[0].credential_id == restricted
    assert [c.credential_id for c in app.state.credentials.candidates(
        ["codebuddy"], model="kimi-k3")] == [open_one]