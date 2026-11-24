"""`persistence/compress_buckets.py`：原子快照的读写与宽容降级。"""
import json
from pathlib import Path

from src.persistence.compress_buckets import (
    COUNTER_FIELDS,
    RETENTION_DAYS,
    VERSION,
    load_buckets,
    save_buckets,
    snapshot_path,
)


def _counters(**overrides):
    values = dict.fromkeys(COUNTER_FIELDS, 0)
    values.update(overrides)
    return values


def test_snapshot_path_is_inside_data_dir(tmp_path):
    assert snapshot_path(str(tmp_path)) == str(tmp_path / "compress_buckets.json")


def test_round_trip_restores_lifetime_and_hour_buckets(tmp_path):
    path = Path(snapshot_path(str(tmp_path)))
    payload = {
        "version": VERSION,
        "saved_at": 1_700_000_000.0,
        "lifetime": _counters(triggers=2, chars_saved=80),
        "buckets": {str(1_700_000_000 // 3600 * 3600): _counters(triggers=2, chars_saved=80)},
    }
    assert save_buckets(str(path), payload) is True

    buckets, lifetime = load_buckets(str(path), now=1_700_000_100.0)
    assert lifetime["triggers"] == 2
    assert lifetime["chars_saved"] == 80
    assert buckets == {1_700_000_000 // 3600 * 3600: _counters(triggers=2, chars_saved=80)}


def test_missing_file_degrades_to_empty_snapshot(tmp_path):
    assert load_buckets(snapshot_path(str(tmp_path))) == ({}, {})


def test_corrupt_file_degrades_to_empty_snapshot(tmp_path, caplog):
    path = Path(snapshot_path(str(tmp_path)))
    path.write_text("{not json", encoding="utf-8")
    assert load_buckets(str(path)) == ({}, {})
    assert any("压缩收益快照读取失败" in record.getMessage()
               for record in caplog.records)


def test_version_mismatch_is_ignored(tmp_path, caplog):
    path = Path(snapshot_path(str(tmp_path)))
    path.write_text(json.dumps({"version": VERSION + 1, "lifetime": {},
                                "buckets": {}}), encoding="utf-8")
    assert load_buckets(str(path)) == ({}, {})
    assert any("版本不匹配" in record.getMessage() for record in caplog.records)


def test_non_object_buckets_is_rejected(tmp_path):
    path = Path(snapshot_path(str(tmp_path)))
    path.write_text(json.dumps({"version": VERSION, "lifetime": {},
                                "buckets": []}), encoding="utf-8")
    assert load_buckets(str(path)) == ({}, {})


def test_non_object_lifetime_is_rejected(tmp_path):
    path = Path(snapshot_path(str(tmp_path)))
    path.write_text(json.dumps({"version": VERSION, "lifetime": [],
                                "buckets": {}}), encoding="utf-8")
    assert load_buckets(str(path)) == ({}, {})


def test_invalid_counter_values_are_rejected(tmp_path):
    path = Path(snapshot_path(str(tmp_path)))
    for bad in ({"triggers": True}, {"triggers": -1}, {"triggers": "3"}):
        path.write_text(json.dumps({"version": VERSION, "lifetime": bad,
                                    "buckets": {}}), encoding="utf-8")
        assert load_buckets(str(path)) == ({}, {})


def test_old_buckets_are_pruned_on_load_but_lifetime_kept(tmp_path):
    path = Path(snapshot_path(str(tmp_path)))
    now = 1_700_000_000.0
    stale_hour = int(now // 3600) * 3600 - (RETENTION_DAYS + 1) * 24 * 3600
    fresh_hour = int(now // 3600) * 3600
    path.write_text(json.dumps({
        "version": VERSION,
        "lifetime": _counters(triggers=9),
        "buckets": {str(stale_hour): _counters(triggers=5),
                    str(fresh_hour): _counters(triggers=4)},
    }), encoding="utf-8")
    buckets, lifetime = load_buckets(str(path), now=now)
    assert stale_hour not in buckets
    assert fresh_hour in buckets
    assert lifetime["triggers"] == 9


def test_save_failure_returns_false_and_logs(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert save_buckets(str(blocker / "snap.json"), {}) is False
    assert any("压缩收益快照落盘失败" in record.getMessage()
               for record in caplog.records)


def test_save_without_directory_component_uses_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert save_buckets("snap.json", {"version": VERSION}) is True
    assert (tmp_path / "snap.json").exists()