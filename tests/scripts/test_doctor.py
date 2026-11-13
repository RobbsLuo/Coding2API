"""`scripts/doctor.py` 的编排层：端点收集、排版、退出码。

只测脚本自己的逻辑（不起服务、不发外网）：`src.doctor` 的每一项已由
`tests/test_doctor.py` 覆盖，这里验证「怎么把它们拼起来」。
"""

from __future__ import annotations

import json

import pytest

from src import doctor
from src.config import Settings
from tests.helpers.constants import SECRET
from tests.helpers.paths import load_script

DOCTOR = load_script("doctor")


def _settings(tmp_path, **overrides) -> Settings:
    values = {"APP_SECRET": SECRET, "DATA_DIR": str(tmp_path)}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_upstream_endpoints_dedupes_and_keeps_order(tmp_path):
    """只取当前生效端点；重复（如两站同一主机）只探测一次。"""
    settings = _settings(
        tmp_path,
        CODEBUDDY_API_ENDPOINT="https://a.example.com",
        CODEBUDDY_INTL_API_ENDPOINT="https://a.example.com",
        ZEN_API_ENDPOINT="https://b.example.com",
        KILO_API_ENDPOINT="",
        QODER_API_ENDPOINT="",
        CODEARTS_API_ENDPOINT="",
        OFFICEACE_API_ENDPOINT="",
        RACCOON_API_ENDPOINT="",
        LINGXI_API_ENDPOINT="",
    )
    assert DOCTOR.upstream_endpoints(settings) == [
        ("codebuddy", "https://a.example.com"),
        ("zen", "https://b.example.com"),
    ]


def test_upstream_endpoints_skips_blank(tmp_path):
    settings = _settings(
        tmp_path,
        CODEBUDDY_API_ENDPOINT="",
        CODEBUDDY_INTL_API_ENDPOINT="",
        ZEN_API_ENDPOINT="  ",
        KILO_API_ENDPOINT="",
        QODER_API_ENDPOINT="",
        CODEARTS_API_ENDPOINT="",
        OFFICEACE_API_ENDPOINT="",
        RACCOON_API_ENDPOINT="",
        LINGXI_API_ENDPOINT="",
    )
    assert DOCTOR.upstream_endpoints(settings) == []


def test_collect_runs_all_checks_without_upstream(tmp_path):
    checks = DOCTOR.collect(_settings(tmp_path), port=0, skip_upstream=True)
    names = [c.name for c in checks]
    for expected in ("Python 版本", "关键依赖", "APP_SECRET", "DATA_DIR",
                     "DATA_DIR 权限", "PUBLIC_BASE_URL / ALLOWED_HOSTS",
                     "数据库", "端口", "前端产物"):
        assert expected in names
    assert not any(name.startswith("上游") for name in names)


def test_collect_includes_upstream_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "check_upstreams",
                        lambda endpoints: [doctor.Check("上游 x", doctor.PASS, "ok")])
    checks = DOCTOR.collect(_settings(tmp_path), port=0)
    assert any(c.name == "上游 x" for c in checks)


def test_render_text_aligns_and_marks_status():
    checks = [doctor.Check("短", doctor.PASS, "ok"),
              doctor.Check("一个很长的检查名", doctor.WARN, "注意")]
    text = DOCTOR.render(checks)
    lines = text.splitlines()
    assert lines[0].startswith("[PASS] 短 ")
    assert lines[1].startswith("[WARN] 一个很长的检查名")
    assert "注意" in lines[1]


def test_render_json_is_machine_readable():
    checks = [doctor.Check("x", doctor.FAIL, "坏了")]
    payload = json.loads(DOCTOR.render(checks, as_json=True))
    assert payload == [{"name": "x", "status": "FAIL", "detail": "坏了"}]


def test_render_empty_list_does_not_crash():
    assert DOCTOR.render([]) == ""


def test_summarize_counts_each_status():
    checks = [doctor.Check("a", doctor.PASS), doctor.Check("b", doctor.PASS),
              doctor.Check("c", doctor.WARN), doctor.Check("d", doctor.FAIL)]
    assert DOCTOR.summarize(checks) == "2 项通过，1 项警告，1 项失败"


def _stub_collect(monkeypatch, statuses):
    checks = [doctor.Check(f"case{index}", status) for index, status in enumerate(statuses)]
    monkeypatch.setattr(DOCTOR, "collect", lambda *_a, **_k: checks)


def test_main_exit_zero_when_only_pass_and_warn(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _stub_collect(monkeypatch, [doctor.PASS, doctor.WARN])
    assert DOCTOR.main([]) == 0
    out = capsys.readouterr().out
    assert "1 项通过，1 项警告，0 项失败" in out


def test_main_exit_one_on_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _stub_collect(monkeypatch, [doctor.PASS, doctor.FAIL])
    assert DOCTOR.main([]) == 1


def test_main_json_mode_prints_only_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _stub_collect(monkeypatch, [doctor.PASS])
    assert DOCTOR.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == [{"name": "case0", "status": "PASS", "detail": ""}]


def test_main_passes_port_and_skip_upstream(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    captured: dict = {}

    def fake_collect(settings, *, port=None, skip_upstream=False):
        captured.update(port=port, skip=skip_upstream)
        return [doctor.Check("x", doctor.PASS)]

    monkeypatch.setattr(DOCTOR, "collect", fake_collect)
    assert DOCTOR.main(["--port", "9123", "--skip-upstream"]) == 0
    assert captured == {"port": 9123, "skip": True}


def test_main_default_port_is_none(monkeypatch, tmp_path):
    """不传 `--port` 时交给 `src.doctor` 读 Settings.PORT（不能写死 8000）。"""
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    captured: dict = {}

    def fake_collect(_settings, *, port=None, skip_upstream=False):
        captured["port"] = port
        return [doctor.Check("x", doctor.PASS)]

    monkeypatch.setattr(DOCTOR, "collect", fake_collect)
    DOCTOR.main([])
    assert captured["port"] is None


def test_load_settings_tolerant_returns_settings_normally(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(DOCTOR, "load_settings", lambda: settings)
    loaded, problem = DOCTOR.load_settings_tolerant()
    assert (loaded, problem) == (settings, None)


def test_load_settings_tolerant_degrades_when_secret_missing(monkeypatch):
    """APP_SECRET 缺失时不能让 pydantic 的 ValidationError 挡住整个自检。"""
    from pydantic import ValidationError

    error = ValidationError.from_exception_data("Settings", [
        {"type": "missing", "loc": ("app_secret",), "input": {},
         "ctx": {"missing": True}}])
    monkeypatch.setattr(DOCTOR, "load_settings",
                        lambda: (_ for _ in ()).throw(error))
    settings, problem = DOCTOR.load_settings_tolerant()
    assert problem == "APP_SECRET 未配置"
    assert settings.app_secret == ""


@pytest.mark.parametrize("status", [doctor.PASS, doctor.WARN, doctor.FAIL])
def test_check_ok_matrix(status):
    assert doctor.Check("x", status).ok is (status != doctor.FAIL)
