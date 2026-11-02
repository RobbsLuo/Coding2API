"""自定义模型别名（`MODEL_ALIASES`）的端到端行为。

守着四件事：

1. 别名出现在 `/v1/models` 且 `GET /v1/models/{别名}` 命中同一份条目
   （`custom_alias` 标记供前端打徽章）；
2. 聊天用别名能路由到目标渠道（`@渠道` 钉渠道 + 不钉渠道按目录收窄）；
3. 撞车（真实对外 id / 已有自动别名）与坏段（目标不存在 / 渠道不对）都
   **跳过该段并记错误**，不静默覆盖、不拖垮整个列表；
4. 热更：改 `runtime_settings.model_aliases` 后下一次请求即生效。
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from src.config import Settings
from src.engine.executor import Executor, ExecutorDeps
from src.engine.scheduler import Scheduler
from src.main import build_app
from src.provider.base import Model
from tests.api._support import RejectProvider
from tests.helpers.chat import chat_request
from tests.helpers.constants import SECRET
from tests.helpers.events import GOOD


class _Stub:
    """最小渠道替身：`list_models` 返回固定表。"""

    def __init__(self, pid: str, models: list[tuple[str, str]]) -> None:
        self.id = pid
        self._models = models

    async def list_models(self, _data):
        return [Model(id=model_id, name=name, max_input_tokens=128000)
                for model_id, name in self._models]

    def import_credential(self, raw):  # pragma: no cover - 未使用
        return raw


def _app(tmp_path, aliases: str = ""):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", MODEL_ALIASES=aliases)
    app = build_app(settings, providers={
        "trae": _Stub("trae", [("glm-5.3", "GLM 5.3"),
                               ("kimi-k3.1-pro", "Kimi-K3.1-Pro")]),
        "kilo": _Stub("kilo", [("recursive/flint-v1.0:free",
                                "Recursive: Flint (free)")]),
    })
    for provider in ("trae", "kilo"):
        app.state.credentials.add(provider=provider,
                                  credential_data={"accessToken": "a", "token": "t"})
    return app


def _key(app) -> dict[str, str]:
    created = app.state.api_keys.create("root")
    return {"Authorization": f"Bearer {created['api_key']}"}


def _ids(client, app) -> dict[str, dict]:
    data = client.get("/v1/models", headers=_key(app)).json()["data"]
    return {item["id"]: item for item in data}


# ------------------------------------------------------------- 列表 / 单查

def test_alias_appears_in_list_and_single_lookup(tmp_path):
    app = _app(tmp_path, "team-fast=glm-5.3@trae")
    with TestClient(app) as client:
        items = _ids(client, app)
        assert "team-fast" in items
        entry = items["team-fast"]
        assert entry["custom_alias"] is True
        assert entry["alias_target"] == "glm-5.3"
        assert entry["providers"] == ["trae"]
        assert entry["name"] == "team-fast"
        # 别名条目必须带目标条目的能力元数据（前端选择器要用）
        assert entry["max_input_tokens"] == items["glm-5.3"].get("max_input_tokens")

        single = client.get("/v1/models/team-fast", headers=_key(app))
        assert single.status_code == 200
        assert single.json()["id"] == "team-fast"
        # 大小写不敏感，与真实条目同口径
        assert client.get("/v1/models/TEAM-FAST",
                          headers=_key(app)).json()["id"] == "team-fast"


def test_real_entries_do_not_carry_custom_alias_flag(tmp_path):
    app = _app(tmp_path, "team-fast=glm-5.3")
    with TestClient(app) as client:
        items = _ids(client, app)
    assert "custom_alias" not in items["glm-5.3"]
    assert "alias_target" not in items["glm-5.3"]


def test_alias_without_provider_keeps_all_target_channels(tmp_path):
    """不钉渠道：目标模型在哪些渠道，别名就在哪些渠道（列表显示并集）。"""
    app = _app(tmp_path, "team-flint=flint")
    with TestClient(app) as client:
        items = _ids(client, app)
    assert items["team-flint"]["providers"] == ["kilo"]
    assert items["team-flint"]["custom_alias"] is True


def test_alias_target_may_be_raw_upstream_id_or_display_name(tmp_path):
    """目标写成任一渠道原 id / 展示名都能命中（与 `_match_entry` 同口径）。"""
    app = _app(tmp_path,
               "by-raw=recursive/flint-v1.0:free;by-name=Kimi K3.1 Pro")
    with TestClient(app) as client:
        items = _ids(client, app)
    assert items["by-raw"]["alias_target"] == "flint"
    assert items["by-name"]["alias_target"] == "kimi-k3.1-pro"


# ---------------------------------------------------------------- 执行侧

def test_alias_routes_chat_to_pinned_provider(tmp_path):
    """聊天用别名 → 只打目标渠道，且上游收到的是该渠道的原 id。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    trae = RejectProvider("trae", [GOOD])
    kilo = RejectProvider("kilo", [GOOD])
    app = build_app(settings, providers={"trae": trae, "kilo": kilo})
    app.state.runtime_settings.set("model_aliases",
                                   "team-flint=flint-v1.0@kilo")
    services = app.state.services
    services.model_list_cache["trae"] = {"glm-5.3": Model(id="glm-5.3")}
    services.model_list_cache["kilo"] = {
        "recursive/flint-v1.0:free": Model(id="recursive/flint-v1.0:free")}
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.credentials.add(provider="kilo", credential_data={"token": "t"})
    key = app.state.api_keys.create("root")["api_key"]

    with TestClient(app) as client:
        # 先拉一次列表把别名表 publish 出来（真实流程也是这样：客户端先列模型）
        listed = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        assert "team-flint" in {item["id"] for item in listed.json()["data"]}
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"model": "team-flint",
                                     "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok"
    assert (trae.calls, kilo.calls) == (0, 1)


async def test_executor_maps_alias_to_target_raw_id(dual_repo):
    """执行侧别名表：`upstream_model_name` 把别名换回目标渠道原 id。"""
    repo, _db = dual_repo
    repo.add(provider="kilo", credential_data={"token": "t"})
    kilo = RejectProvider("kilo", [GOOD])
    seen: list[str] = []

    class _Recording(RejectProvider):
        async def stream_chat(self, credential_data, payload, model):
            seen.append(model)
            async for event in super().stream_chat(credential_data, payload, model):
                yield event

    kilo = _Recording("kilo", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"kilo": kilo}, credentials=repo, scheduler=Scheduler(),
        default_model="team-flint",
        model_aliases={"kilo": {"team-flint": "recursive/flint-v1.0:free"}},
        upstream_model_name=lambda provider_id, model: (
            {"kilo": {"team-flint": "recursive/flint-v1.0:free"}}
            .get(provider_id, {}).get(model.lower(), model))))

    result = await executor.complete(chat_request("team-flint"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert seen == ["recursive/flint-v1.0:free"]


# ------------------------------------------------------------------ 撞车

def test_alias_colliding_with_real_id_is_skipped(tmp_path, caplog):
    """与真实对外 id 撞车：跳过该段、记错误，**不覆盖**真实条目。"""
    app = _app(tmp_path, "glm-5.3=kimi-k3.1-pro;good=glm-5.3@trae")
    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        items = _ids(client, app)
    assert "custom_alias" not in items["glm-5.3"]     # 真实条目未被别名覆盖
    assert items["glm-5.3"]["providers"] == ["trae"]
    assert "good" in items                              # 其它段照常生效
    assert any("撞车" in record.getMessage() for record in caplog.records)


def test_alias_colliding_with_auto_alias_is_skipped(tmp_path, caplog):
    """与已存在的自动别名撞车（这里是展示名键）同样跳过并记错误。"""
    app = _app(tmp_path, "GLM 5.3=kimi-k3.1-pro;good=glm-5.3@trae")
    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        items = _ids(client, app)
    assert "GLM 5.3" not in items
    assert "good" in items
    assert any("自动别名" in record.getMessage() for record in caplog.records)


def test_alias_name_with_at_is_rejected(tmp_path, caplog):
    """对外名含 `@` 与 `@渠道` 语法冲突，无法从请求里无歧义地还原。"""
    app = _app(tmp_path, "weird@name=glm-5.3;good=glm-5.3@trae")
    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        items = _ids(client, app)
    assert "weird@name" not in items
    assert "good" in items
    assert any("不能含 `@`" in record.getMessage() for record in caplog.records)


def test_alias_with_unknown_target_is_skipped(tmp_path, caplog):
    app = _app(tmp_path, "ghost=no-such-model;good=glm-5.3@trae")
    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        items = _ids(client, app)
    assert "ghost" not in items
    assert "good" in items
    assert any("不存在" in record.getMessage() for record in caplog.records)


def test_alias_pinned_to_wrong_provider_is_skipped(tmp_path, caplog):
    """目标模型不在该渠道上（钉错渠道）：跳过而不是让请求打到不认它的上游。"""
    app = _app(tmp_path, "wrong=glm-5.3@kilo;good=glm-5.3@trae")
    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        items = _ids(client, app)
    assert "wrong" not in items
    assert "good" in items
    assert any("不在渠道" in record.getMessage() for record in caplog.records)


def test_alias_cannot_chain_to_another_alias(tmp_path, caplog):
    """别名目标只认真实条目：A→B 时若 B 本身是别名，A 被跳过（不形成链）。"""
    app = _app(tmp_path, "first=glm-5.3@trae;second=first")
    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        items = _ids(client, app)
    assert "first" in items
    assert "second" not in items
    assert any("不存在" in record.getMessage() for record in caplog.records)


def test_broken_segment_does_not_break_other_aliases(tmp_path, caplog):
    """坏配置段（缺 `=`、未知渠道）不影响其它段——这是「宽容解析」的意义。"""
    app = _app(tmp_path, "garbage;bad=glm-5.3@nope;good=glm-5.3@trae")
    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        items = _ids(client, app)
    assert "good" in items
    assert "bad" not in items
    # 整个列表仍然完整（真实模型一条不少）
    assert {"glm-5.3", "kimi-k3.1-pro", "flint"} <= set(items)


# ------------------------------------------------------------------ 热更

def test_alias_config_is_hot_reloadable(tmp_path):
    """改运行时覆盖层后下一次请求即生效（不需要重启或等 TTL）。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert "later" not in _ids(client, app)
        app.state.runtime_settings.set("model_aliases", "later=kimi-k3.1-pro@trae")
        assert "later" in _ids(client, app)
        assert client.get("/v1/models/later", headers=_key(app)).status_code == 200
        # 清空覆盖 → 别名消失，真实模型仍在
        app.state.runtime_settings.set("model_aliases", "")
        items = _ids(client, app)
        assert "later" not in items and "kimi-k3.1-pro" in items


def test_find_target_entry_blank_target_is_none():
    """空白目标不参与匹配（配置层已拦，这里守住「直接调用」也不乱命中）。"""
    from src.api.model_merge import _find_target_entry

    entries = [{"id": "glm-5.3", "meta": {"name": "GLM 5.3"}, "raw_ids": {}}]
    assert _find_target_entry(entries, "   ") is None
    assert _find_target_entry(entries, "glm-5.3") is entries[0]


def test_alias_listed_for_key_allowlist(tmp_path):
    """Key 白名单按对外名过滤：别名与真实条目同一条规则（别名不绕过白名单）。"""
    app = _app(tmp_path, "team-fast=glm-5.3@trae")
    with TestClient(app) as client:
        allowed = app.state.api_keys.create("root", allowed_models="team-fast")
        headers = {"Authorization": f"Bearer {allowed['api_key']}"}
        ids = {item["id"] for item in client.get("/v1/models", headers=headers).json()["data"]}
        assert ids == {"team-fast"}
        # 白名单外的真实模型单查 404（不能拿单查探测池子）
        assert client.get("/v1/models/kimi-k3.1-pro", headers=headers).status_code == 404
        # 用目标渠道原 id 单查：命中的是白名单内的别名条目（与列表同一份）
        by_raw = client.get("/v1/models/glm-5.3", headers=headers)
        assert by_raw.status_code == 200 and by_raw.json()["id"] == "team-fast"
