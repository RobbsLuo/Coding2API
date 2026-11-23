"""`benchmarks.save_catalog` 的落盘字节形态与老快照兼容性。

公共读写口径已收敛到 `persistence/json_snapshot.py`（其自身用例见
`tests/benchmarks/test_json_snapshot.py`）；这里只锁 OpenRouter 快照自己的
两件事：文件内容仍是原来那份 JSON（中文不转义、无额外空白），以及手写的
老格式（多未知键 / 缺可选段）照样读得回来。
"""

from __future__ import annotations

import json
import time

from src.benchmarks import (
    CATALOG_FILENAME,
    CATALOG_VERSION,
    load_catalog_snapshot,
    save_catalog,
)


def test_save_catalog_bytes_are_stable_and_unescaped(tmp_path):
    """落盘 = `json.dumps(payload, ensure_ascii=False)`：中文不转义、格式不变。"""
    save_catalog(
        str(tmp_path),
        {"glm-5.3": (1.0, 2.0, 0.5)},
        {"glm-5.3": {"source": "openrouter", "intelligence_index": 1.0}},
        {"glm-5.3": {"id": "z-ai/glm-5.3", "name": "太空兔 GLM"}},
    )
    path = tmp_path / CATALOG_FILENAME
    raw = path.read_text(encoding="utf-8")
    assert raw == json.dumps(json.loads(raw), ensure_ascii=False)
    assert "太空兔" in raw
    assert not (tmp_path / f"{CATALOG_FILENAME}.tmp").exists()


def test_load_reads_hand_written_legacy_snapshot(tmp_path):
    """老版本手写快照（带当时没有的未知键）读回不报错。"""
    payload = {
        "version": CATALOG_VERSION,
        "saved_at": time.time(),
        "prices": {"glm-5.3": [1.0, 2.0, 0.5]},
        "benchmarks": {"glm-5.3": {"intelligence_index": 2.0,
                                   "unknown_section": "x"}},
        "models": {"glm-5.3": {"id": "glm-5.3", "legacy_column": 7}},
    }
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    prices, benchmarks, catalog, saved_at = load_catalog_snapshot(str(tmp_path))
    assert prices == {"glm-5.3": (1.0, 2.0, 0.5)}
    assert benchmarks == {"glm-5.3": {"intelligence_index": 2.0,
                                      "unknown_section": "x"}}
    assert catalog == {"glm-5.3": {"id": "glm-5.3", "legacy_column": 7}}
    assert saved_at == payload["saved_at"]