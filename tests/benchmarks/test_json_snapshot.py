"""`persistence/json_snapshot.py`：两套目录快照共用的原子读写口径。

重点覆盖公共层自己负责的三件事：原子写（tmp + replace）的字节形态、
宽容读取的四条退化路径（缺失 / 损坏 / 版本不符 / validator 抛错）、
以及 `now` / `max_age` 原样转交 validator。
"""

from __future__ import annotations

import json
import logging
import os
import time

import pytest

from src.persistence.json_snapshot import load_snapshot, save_snapshot


def _payload() -> dict:
    return {"version": 3, "saved_at": 1_700_000_000.0,
            "items": {"太空兔": {"id": "m"}}}


def _never(_raw, _now, _max_age):        # pragma: no cover - 退化路径不该调它
    raise AssertionError("退化路径不应调用 validator")


def test_save_writes_exact_bytes_and_replaces_tmp(tmp_path):
    """落盘内容 = `json.dumps(..., ensure_ascii=False)`（中文不转义），tmp 不残留。"""
    path = str(tmp_path / "nested" / "snap.json")
    save_snapshot(path, _payload())

    raw = (tmp_path / "nested" / "snap.json").read_text(encoding="utf-8")
    assert raw == json.dumps(_payload(), ensure_ascii=False)
    assert "太空兔" in raw                       # ensure_ascii=False 生效
    assert not os.path.exists(f"{path}.tmp")     # tmp 已被 replace 掉


def test_save_into_cwd_when_path_has_no_dir(tmp_path, monkeypatch):
    """只有文件名时建到当前目录（dirname 为空串的兜底分支）。"""
    monkeypatch.chdir(tmp_path)
    save_snapshot("snap.json", _payload())
    assert json.loads((tmp_path / "snap.json").read_text(encoding="utf-8")) == _payload()


def test_save_failure_only_logs(tmp_path, caplog):
    """写盘失败（data_dir 指向一个已存在的文件）只记日志，不抛。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        save_snapshot(str(blocker / "snap.json"), _payload())
    assert any("模型目录落盘失败" in r.getMessage() for r in caplog.records)


def test_load_missing_file_returns_fallback(tmp_path):
    assert load_snapshot(str(tmp_path / "none.json"), version=3, max_age=10.0,
                         validator=_never, fallback=("empty",)) == ("empty",)


def test_load_broken_json_returns_fallback(tmp_path, caplog):
    path = tmp_path / "snap.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_snapshot(str(path), version=3, max_age=10.0,
                             validator=_never, fallback=None) is None
    assert any("模型目录读取失败" in r.getMessage() for r in caplog.records)


def test_load_version_mismatch_returns_fallback(tmp_path, caplog):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"version": 4}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_snapshot(str(path), version=3, max_age=10.0,
                             validator=_never, fallback={}) == {}
    assert any("模型目录版本不匹配" in r.getMessage() for r in caplog.records)


def test_load_validator_exception_returns_fallback(tmp_path, caplog):
    """validator 撞上坏 payload（缺键）也算坏快照：降级而不是抛出。"""
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"version": 3}), encoding="utf-8")

    def validator(raw, _now, _max_age):
        return raw["items"]                   # KeyError → 公共层兜住

    with caplog.at_level(logging.WARNING):
        assert load_snapshot(str(path), version=3, max_age=10.0,
                             validator=validator, fallback={}) == {}
    assert any("模型目录读取失败" in r.getMessage() for r in caplog.records)


def test_load_passes_now_and_max_age_to_validator(tmp_path):
    """新鲜度语义由 validator 定：公共层只转交 `now` / `max_age`。"""
    path = str(tmp_path / "snap.json")
    save_snapshot(path, _payload())
    seen: dict = {}

    def validator(raw, now, max_age):
        seen.update(raw=raw, now=now, max_age=max_age)
        return "ok"

    before = time.time()
    assert load_snapshot(path, version=3, max_age=99.0,
                         validator=validator, fallback=None) == "ok"
    assert seen["raw"] == _payload()
    assert seen["max_age"] == 99.0
    assert before <= seen["now"] <= time.time()   # now 缺省 = time.time()

    assert load_snapshot(path, version=3, max_age=99.0, validator=validator,
                         fallback=None, now=123.0) == "ok"
    assert seen["now"] == 123.0                   # 显式 now 原样透传


def test_fallback_identity_is_returned_on_every_degrated_path(tmp_path):
    """`fallback` 原样返回（含可变对象），调用方不必再包一层默认值。"""
    marker = {"k": "v"}
    for path in (tmp_path / "missing.json",):
        assert load_snapshot(str(path), version=1, max_age=1.0,
                             validator=_never, fallback=marker) is marker
    for content in ("{bad", json.dumps({"version": 9})):
        path = tmp_path / "snap.json"
        path.write_text(content, encoding="utf-8")
        assert load_snapshot(str(path), version=1, max_age=1.0,
                             validator=_never, fallback=marker) is marker


@pytest.mark.parametrize("now", [None, 0.0])
def test_load_snapshot_covers_both_now_branches(tmp_path, now):
    path = str(tmp_path / "snap.json")
    save_snapshot(path, _payload())
    assert load_snapshot(path, version=3, max_age=1.0, now=now,
                         validator=lambda raw, _n, _a: raw["version"],
                         fallback=-1) == 3