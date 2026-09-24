"""运行时配置热更（B3.2）：覆盖层语义、仓储、管理台端点。

这一组测试守着三条容易退化的语义：
1. 生效值 = DB 覆盖 > env 默认；「恢复默认」必须真的把行删掉而不是写空串。
2. 非法值/未知 key 一律拒绝，且拒绝发生在**落库之前**（写坏组合无法追溯）。
3. 读取路径不吃库：覆盖值在内存缓存里，热更才有意义。
"""

from __future__ import annotations

import pytest

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import RuntimeSettingsRepository
from src.runtime_settings import (
    HOT_BY_KEY,
    HOT_SETTINGS,
    InvalidSetting,
    RuntimeSettings,
    format_value,
    load_runtime_settings,
    now_seconds,
    parse_value,
    validate_group,
)
from tests.conftest import SECRET


class MemoryStore:
    """最小 SettingsStore：只实现读写三动作，避免测试依赖数据库。"""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.rows: dict[str, str] = dict(initial or {})
        self.set_calls: list[tuple[str, str, int | None]] = []

    def load(self) -> dict[str, str]:
        return dict(self.rows)

    def set(self, key: str, value: str, now: int | None = None) -> None:
        self.rows[key] = value
        self.set_calls.append((key, value, now))

    def delete(self, key: str) -> None:
        self.rows.pop(key, None)


def _base(**overrides) -> Settings:
    defaults = {"_env_file": None, "APP_SECRET": SECRET, "DATA_DIR": "./data"}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def runtime() -> RuntimeSettings:
    return RuntimeSettings(_base(), MemoryStore())


# ------------------------------------------------------------------ 白名单

def test_hot_settings_keys_unique_and_env_names_derived():
    keys = [item.key for item in HOT_SETTINGS]
    assert len(keys) == len(set(keys))
    for item in HOT_SETTINGS:
        assert item.env_name == item.key.upper()
        assert item.label and item.description


# ------------------------------------------------------------ 值解析与校验

def test_parse_value_parses_each_supported_kind():
    assert parse_value("quota_probe_minutes", "15") == 15
    assert parse_value("pacer_min_seconds", "2.5") == 2.5


def test_floor_clamps_env_value_and_stale_override_is_dropped():
    """生效下限（floor）与写入下限（minimum）双保险：

    - env 值低于 floor（如旧部署 env=1）→ 读路径钳到 floor，UI 回显与
      实际运行是同一套数字；
    - 旧版（minimum 更宽时）写入的越界覆盖行 → reload 按非法行丢弃回落
      env 默认，并记警告日志；
    - 新写入越下限 → 直接拒绝。
    """
    runtime = RuntimeSettings(_base(growth_interval_minutes=1), MemoryStore())
    assert runtime.get("growth_interval_minutes") == 5
    assert runtime.growth_interval_minutes == 5
    stale = RuntimeSettings(_base(), MemoryStore({"growth_interval_minutes": "1"}))
    assert stale.get("growth_interval_minutes") == 60
    with pytest.raises(InvalidSetting):
        runtime.set("growth_interval_minutes", 1)
    with pytest.raises(InvalidSetting):
        runtime.set("quota_probe_minutes", 0)
    assert parse_value("activity_report_enabled", "yes") is True
    assert parse_value("activity_report_enabled", "off") is False
    assert parse_value("default_model", "glm-5.2") == "glm-5.2"


def test_parse_value_rejects_unknown_key():
    with pytest.raises(InvalidSetting, match="unknown setting"):
        parse_value("app_secret", "x")


@pytest.mark.parametrize("raw", ["maybe", "", "2"])
def test_parse_value_rejects_non_boolean(raw):
    with pytest.raises(InvalidSetting):
        parse_value("activity_report_enabled", raw)


def test_parse_value_rejects_non_numeric_and_out_of_range():
    with pytest.raises(InvalidSetting, match="期望 int"):
        parse_value("quota_probe_minutes", "abc")
    with pytest.raises(InvalidSetting, match="不能小于"):
        parse_value("quota_probe_minutes", "-1")
    with pytest.raises(InvalidSetting, match="不能大于"):
        parse_value("activity_report_hour", "24")


def test_parse_value_rejects_blank_default_model():
    with pytest.raises(InvalidSetting, match="不能为空"):
        parse_value("default_model", "   ")


def test_format_value_is_inverse_of_parse_value():
    assert format_value(True) == "true"
    assert format_value(False) == "false"
    assert format_value(7) == "7"
    assert format_value(1.5) == "1.5"
    assert parse_value("activity_report_enabled", format_value(True)) is True


# ---------------------------------------------------------------- 跨字段校验

def test_validate_group_rejects_reversed_pacer_bounds():
    with pytest.raises(InvalidSetting, match="不能大于"):
        validate_group({"pacer_min_seconds": 30, "pacer_max_seconds": 5})


def test_validate_group_checks_single_sided_update_against_current():
    """只提交下限时，必须与「当前生效上限」比较，否则单字段就能绕过校验。"""
    with pytest.raises(InvalidSetting, match="不能大于"):
        validate_group({"pacer_min_seconds": 30}, current=lambda key: 5)


def test_validate_group_allows_unrelated_and_unknown_only_fields():
    validate_group({"pacer_min_seconds": 1})
    validate_group({"default_model": "x"}, current=lambda key: 0)
    with pytest.raises(InvalidSetting, match="unknown setting"):
        validate_group({"nope": 1})


# ------------------------------------------------------------ 覆盖层读取语义

def test_override_wins_over_env_and_reset_restores(runtime):
    base_default = runtime.get("quota_probe_minutes")
    runtime.set("quota_probe_minutes", 15)
    assert runtime.quota_probe_minutes == 15
    assert runtime.is_overridden("quota_probe_minutes") is True

    runtime.reset("quota_probe_minutes")
    assert runtime.quota_probe_minutes == base_default
    assert runtime.is_overridden("quota_probe_minutes") is False


def test_set_none_means_restore_default(runtime):
    runtime.set("pacer_min_seconds", 9)
    runtime.set("pacer_min_seconds", None)
    assert runtime.is_overridden("pacer_min_seconds") is False


def test_set_blank_string_also_means_restore_default(runtime):
    runtime.set("default_model", "glm-5.2")
    runtime.set("default_model", "   ")
    assert runtime.is_overridden("default_model") is False


def test_set_many_validates_before_writing_anything(runtime):
    with pytest.raises(InvalidSetting):
        runtime.set_many({"pacer_min_seconds": 30, "pacer_max_seconds": 5})
    # 校验失败时一条都不该落库：半新半旧的组合比拒绝更糟
    assert runtime.is_overridden("pacer_min_seconds") is False
    assert runtime.is_overridden("pacer_max_seconds") is False


def test_set_many_applies_group_atomically(runtime):
    runtime.set_many({"pacer_min_seconds": 1, "pacer_max_seconds": 3})
    assert runtime.pacer_min_seconds == 1
    assert runtime.pacer_max_seconds == 3


def test_set_many_group_validates_against_post_commit_effective_values(runtime):
    """同批提交「重置 + 改对端」时，比较的必须是重置后的 env 默认值。"""
    # env 默认 pacer_min=5, max=20；先把 max 覆盖成 100
    runtime.set("pacer_max_seconds", 100)
    # 同一批：max 恢复默认（20），min 提到 50 → 50 > 20，必须拒绝
    with pytest.raises(InvalidSetting, match="不能大于"):
        runtime.set_many({"pacer_max_seconds": None, "pacer_min_seconds": 50})
    # 拒绝后旧覆盖仍在，没有半写状态
    assert runtime.pacer_max_seconds == 100
    # 反过来：max 恢复默认、min 保持不变则合法
    runtime.set_many({"pacer_max_seconds": None, "pacer_min_seconds": 1})
    assert runtime.pacer_max_seconds == 20


def test_effective_after_falls_back_to_base_for_untouched_keys(runtime):
    """未提交的对端回落 env：env 默认 max=20，提交 min=30 必须被拒。"""
    with pytest.raises(InvalidSetting, match="不能大于"):
        runtime.set_many({"pacer_min_seconds": 30})


def test_set_and_reset_reject_unknown_key(runtime):
    with pytest.raises(InvalidSetting):
        runtime.set("nope", 1)
    with pytest.raises(InvalidSetting):
        runtime.reset("nope")


def test_every_hot_key_exposes_a_matching_property(runtime):
    """每个白名单 key 必须有同名热更属性；否则消费方读到的还是 env 旧值。"""
    for item in HOT_SETTINGS:
        assert isinstance(getattr(runtime, item.key), (int, float, bool, str))


def test_blocklist_patterns_recomputed_from_effective_value(runtime):
    runtime.set("model_blocklist", "a*, b ,,c")
    assert runtime.blocklist_patterns == ("a*", "b", "c")
    runtime.reset("model_blocklist")
    assert runtime.blocklist_patterns == _base().blocklist_patterns


def test_base_settings_blocklist_patterns_still_available():
    """覆盖层自己算黑名单，但 Settings 的同名字段仍是公开契约（不能被删）。"""
    assert _base(MODEL_BLOCKLIST="x*, y").blocklist_patterns == ("x*", "y")


def test_reload_skips_unknown_and_malformed_rows():
    store = MemoryStore({"quota_probe_minutes": "abc",
                         "app_secret": "should-not-load",
                         "pacer_min_seconds": "2.5"})
    runtime = RuntimeSettings(_base(), store)
    assert runtime.pacer_min_seconds == 2.5
    assert runtime.is_overridden("quota_probe_minutes") is False
    assert runtime.is_overridden("app_secret") is False


def test_env_value_reports_default_even_when_overridden(runtime):
    runtime.set("quota_probe_minutes", 15)
    assert runtime.env_value("quota_probe_minutes") == _base().quota_probe_minutes


def test_snapshot_shape(runtime):
    runtime.set("quota_probe_minutes", 15)
    rows = runtime.snapshot()
    assert [row["key"] for row in rows] == [item.key for item in HOT_SETTINGS]
    row = next(item for item in rows if item["key"] == "quota_probe_minutes")
    assert row["value"] == 15 and row["default"] == _base().quota_probe_minutes
    assert row["overridden"] is True and row["env_name"] == "QUOTA_PROBE_MINUTES"
    assert row["kind"] == "int" and row["label"] and row["description"]


def test_unknown_attributes_delegate_to_base(runtime):
    assert runtime.app_secret == SECRET
    assert runtime.db_path == _base().db_path


def test_dunder_attributes_do_not_delegate(runtime):
    """内部名必须直接 AttributeError，否则 reload/copy 会踩到无限递归。"""
    with pytest.raises(AttributeError):
        _ = runtime.__deepcopy__  # noqa: B018


def test_current_exposes_effective_value_for_group_validation(runtime):
    runtime.set("pacer_max_seconds", 7)
    assert runtime.current("pacer_max_seconds") == 7


def test_load_runtime_settings_and_now_seconds():
    store = MemoryStore({"quota_probe_minutes": "9"})
    runtime = load_runtime_settings(_base(), store)
    assert isinstance(runtime, RuntimeSettings)
    assert runtime.quota_probe_minutes == 9
    assert now_seconds() > 0


# ---------------------------------------------------------------- 仓储层

def test_repository_round_trip(tmp_path):
    db = Database(tmp_path / "settings.sqlite3")
    apply_schema(db.connect())
    repo = RuntimeSettingsRepository(db)

    assert repo.load() == {}
    repo.set("quota_probe_minutes", "15", now=100)
    assert repo.load() == {"quota_probe_minutes": "15"}
    assert repo.updated_at()["quota_probe_minutes"] == 100

    # 覆盖同一 key：upsert 而不是插入第二行
    repo.set("quota_probe_minutes", "30", now=200)
    assert repo.load() == {"quota_probe_minutes": "30"}
    assert repo.updated_at()["quota_probe_minutes"] == 200

    repo.set("default_model", "glm-5.2")
    assert repo.updated_at()["default_model"] > 0      # now 省略时取当前时间

    repo.delete("quota_probe_minutes")
    assert repo.load() == {"default_model": "glm-5.2"}
    db.close()


# ------------------------------------------------------------ 管理台端点

@pytest.fixture()
def admin_app(tmp_path):
    from fastapi.testclient import TestClient

    from src.main import build_app

    settings = _base(DATA_DIR=str(tmp_path), ADMIN_USERNAMES="root",
                     QUOTA_PROBE_MINUTES="10")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        yield app, client


def test_list_settings_requires_admin(admin_app):
    _app, client = admin_app
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    assert client.get("/api/settings").status_code == 403


def test_get_settings_returns_snapshot_with_defaults(admin_app):
    _app, client = admin_app
    body = client.get("/api/settings").json()
    assert body["overridden"] == 0
    row = next(item for item in body["settings"] if item["key"] == "quota_probe_minutes")
    assert row["value"] == 10 and row["default"] == 10 and row["overridden"] is False


def test_put_settings_persists_override_and_reports_it(admin_app):
    app, client = admin_app
    response = client.put("/api/settings", json={"values": {"quota_probe_minutes": 25}})
    assert response.status_code == 200
    body = response.json()
    row = next(item for item in body["settings"] if item["key"] == "quota_probe_minutes")
    assert row["value"] == 25 and row["overridden"] is True

    # 覆盖必须落到 DB（重启后仍生效）并且立刻反映在 Services 上
    assert app.state.runtime_settings.quota_probe_minutes == 25
    assert RuntimeSettingsRepository(Database(app.state.settings.db_path)).load() == {
        "quota_probe_minutes": "25"}


def test_put_settings_null_restores_default(admin_app):
    _app, client = admin_app
    client.put("/api/settings", json={"values": {"quota_probe_minutes": 25}})
    body = client.put("/api/settings", json={"values": {"quota_probe_minutes": None}}).json()
    row = next(item for item in body["settings"] if item["key"] == "quota_probe_minutes")
    assert row["overridden"] is False and row["value"] == 10


def test_put_settings_requires_admin(admin_app):
    _app, client = admin_app
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    assert client.put("/api/settings",
                      json={"values": {"quota_probe_minutes": 1}}).status_code == 403


def test_put_settings_requires_csrf_header(admin_app):
    _app, client = admin_app
    response = client.put("/api/settings", json={"values": {"quota_probe_minutes": 1}},
                          headers={"Origin": "http://evil.example.com"})
    assert response.status_code == 403


def test_put_settings_rejects_non_object_values(admin_app):
    _app, client = admin_app
    assert client.put("/api/settings", json={"values": []}).status_code == 400
    assert client.put("/api/settings", json={}).status_code == 400


def test_put_settings_rejects_invalid_value_with_actionable_message(admin_app):
    _app, client = admin_app
    response = client.put("/api/settings",
                          json={"values": {"activity_report_hour": 99}})
    assert response.status_code == 400
    assert "不能大于" in response.json()["error"]["message"]


def test_put_settings_rejects_unknown_key(admin_app):
    _app, client = admin_app
    assert client.put("/api/settings",
                      json={"values": {"app_secret": "x"}}).status_code == 400


def test_hot_settings_are_wired_into_running_services(admin_app):
    """热更的意义在于「改完立刻生效」：调度窗口与后台周期必须读覆盖层。"""
    app, client = admin_app
    client.put("/api/settings", json={"values": {
        "quota_expiry_window_seconds": 0,
        "quota_expiry_secondary_window_seconds": 123,
        "growth_interval_minutes": 7,
    }})
    runtime = app.state.runtime_settings
    scheduler = app.state.services.executor._deps.scheduler  # noqa: SLF001
    assert scheduler.expiry_window == 0
    assert scheduler.secondary_expiry_window == 0        # 主窗口 ≤0 时次窗口一并失效
    assert runtime.growth_interval_minutes == 7
    assert HOT_BY_KEY["growth_interval_minutes"].kind is int


def test_every_hot_setting_is_read_lazily_not_baked_at_startup(admin_app):
    """回归：`live(config.x)` 会当场求值再包成常量，等于没热更。

    装配必须传 `lambda: runtime.x`。这里对每个「已接线的热更点」在运行时
    改值，断言消费方拿到的是**新值**——只断言 snapshot 会漏掉烘焙 bug。
    """
    app, client = admin_app
    runtime = app.state.runtime_settings
    deps = app.state.services.executor._deps  # noqa: SLF001
    runner = app.state.task_runner

    client.put("/api/settings", json={"values": {
        "default_model": "kimi-k3",
        "codebuddy_chat_min_interval": 12.5,
        "pacer_min_seconds": 3,
        "pacer_max_seconds": 8,
        "quota_probe_minutes": 33,
        "growth_interval_minutes": 9,
        "activity_report_enabled": True,
        "activity_report_hour": 5,
        "conversation_sticky_seconds": 99,
        "model_blocklist": "foo*",
    }})

    # Executor 侧：默认模型每次请求现读
    assert runtime.default_model == "kimi-k3"
    assert deps.default_model() == "kimi-k3"
    # 调度器与粘性 TTL
    assert deps.affinity.ttl_seconds == 99
    # 聊天节流器（provider 共享同一个 Pacer）
    assert app.state.services.registry["codebuddy"].pacer.min_seconds == 12.5
    assert app.state.services.registry["codebuddy"].pacer.max_seconds == 12.5
    # 后台任务周期与开关
    assert runner._quota_interval == 33 * 60        # noqa: SLF001
    assert runner._growth_interval == 9 * 60        # noqa: SLF001
    assert runner._activity_enabled() is True       # noqa: SLF001
    assert runner._activity._hour == 5              # noqa: SLF001
    # 模型黑名单基于生效值重算（不是 Settings 的 cached_property）
    assert runtime.blocklist_patterns == ("foo*",)
    assert app.state.services.settings.blocklist_patterns == ("foo*",)
