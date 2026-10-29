"""模型可用性探活结果的落盘缓存：原子写、过期剪枝、坏文件降级。"""

from __future__ import annotations

import json

from src.provider.codebuddy.model_access_store import (
    MAX_ENTRIES,
    VERSION,
    ModelAccessStore,
    cache_path,
)


def _store(tmp_path, *, ttl=3600, now=1000.0) -> ModelAccessStore:
    return ModelAccessStore(str(tmp_path / "access.json"), ttl,
                            clock=lambda: now)


def test_cache_path_sits_in_data_dir(tmp_path):
    assert cache_path(str(tmp_path)) == str(tmp_path / "codebuddy-model-access.json")


def test_put_then_get_roundtrips_and_survives_reload(tmp_path):
    store = _store(tmp_path)
    store.put(("acct", "https://www.codebuddy.ai"), 1000.0, {"ok": True, "no": False})
    assert store.get(("acct", "https://www.codebuddy.ai")) == (
        1000.0, {"ok": True, "no": False})

    reloaded = _store(tmp_path)
    assert reloaded.get(("acct", "https://www.codebuddy.ai"))[1] == {
        "ok": True, "no": False}
    assert reloaded.get(("other", "https://www.codebuddy.ai")) is None


def test_expired_entry_is_dropped_on_get_and_load(tmp_path):
    store = _store(tmp_path, ttl=10)
    store.put(("acct", "ep"), 1000.0, {"ok": True})
    stale = _store(tmp_path, ttl=10, now=2000.0)
    assert stale.get(("acct", "ep")) is None        # 载入时按 cutoff 剪掉
    assert stale.get(("acct", "ep")) is None

    # 载入时未过期、之后过期：get 里主动删除（不再无限留在大字典里）
    now = [1000.0]
    live = ModelAccessStore(str(tmp_path / "live.json"), 10, clock=lambda: now[0])
    live.put(("acct", "ep"), 1000.0, {"ok": True})
    now[0] = 1030.0
    assert live.get(("acct", "ep")) is None
    assert live.get(("acct", "ep")) is None


def test_corrupt_or_foreign_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "access.json"
    for payload in ("{not json", json.dumps({"version": VERSION + 1, "entries": {}}),
                    json.dumps({"version": VERSION, "entries": []}),
                    json.dumps([1, 2])):
        path.write_text(payload, encoding="utf-8")
        assert _store(tmp_path).get(("acct", "ep")) is None

    path.unlink()
    assert _store(tmp_path).get(("acct", "ep")) is None       # 文件缺失


def test_malformed_entries_are_skipped(tmp_path):
    path = tmp_path / "access.json"
    payload = {
        "version": VERSION,
        "entries": {
            "no-separator": {"checked_at": 1000.0, "access": {}},
            "a\x1fb": "not a dict",
            "a\x1fc": {"checked_at": "yesterday", "access": {}},
            "a\x1fd": {"checked_at": 1000.0, "access": ["nope"]},
            # 过期条目
            "a\x1fe": {"checked_at": 1.0, "access": {}},
            # 合法条目里夹带脏字段：脏值被丢弃，合法值保留
            "a\x1ff": {"checked_at": 1000.0,
                       "access": {"ok": True, "bad": "yes"}},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = _store(tmp_path, ttl=500)
    assert store.get(("a", "b")) is None
    assert store.get(("a", "c")) is None
    assert store.get(("a", "d")) is None
    assert store.get(("a", "e")) is None
    assert store.get(("a", "f")) == (1000.0, {"ok": True})


def test_entry_count_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("src.provider.codebuddy.model_access_store.MAX_ENTRIES", 2)
    store = _store(tmp_path)
    store.put(("a", "1"), 1000.0, {"m": True})
    store.put(("a", "2"), 1001.0, {"m": True})
    store.put(("a", "3"), 1002.0, {"m": True})
    assert store.get(("a", "1")) is None            # 最旧的被淘汰
    assert store.get(("a", "3")) is not None
    assert len(json.loads((tmp_path / "access.json").read_text())["entries"]) == 2


def test_write_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    store = _store(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    store.put(("acct", "ep"), 1000.0, {"ok": True})
    assert store.get(("acct", "ep")) == (1000.0, {"ok": True})
    assert "模型可用性缓存写盘失败" in caplog.text


def test_store_without_directory_writes_relative_path(tmp_path, monkeypatch):
    """`path` 无目录段时跳过 makedirs（不能对空字符串 os.makedirs）。"""
    monkeypatch.chdir(tmp_path)
    store = ModelAccessStore("access.json", 3600, clock=lambda: 1000.0)
    store.put(("acct", "ep"), 1000.0, {"ok": True})
    assert (tmp_path / "access.json").exists()


def test_max_entries_constant_is_sane():
    assert MAX_ENTRIES >= 32
