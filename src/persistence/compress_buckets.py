"""压缩收益小时桶的落盘快照。

为什么落盘：`/api/stats/compression` 的收益按小时桶汇总（见
`api/context.py` 的 `CompressStats`）。只存内存的话，进程重启会把「7d 与
24h 的差异」一并清空——面板切窗口又变成一排相同数字。落盘只做**重启续接**，
不是账单：启动读回、写入节流（默认 60s）、退出时补写一次。

宽容读取：文件缺失 / 损坏 / 版本不符 / 字段非法一律退化成空快照，只丢历史
不丢服务。写失败只记日志——压缩本身不能因为存不下收益而失败。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

#: 快照格式版本：字段变化即 +1，旧格式直接忽略（不猜、不迁移）。
VERSION = 1
#: 小时桶的秒数；键与 `usage_hourly.hour_utc` 同一口径（整点起点）。
HOUR_SECONDS = 3600
#: 小时桶保留天数：与 `usage_events` 的保留期一致。
RETENTION_DAYS = 90
#: 桶/终身累计里被落盘的字段（`tokens_saved` 是派生值，不存）。
COUNTER_FIELDS = ("triggers", "tool_triggers", "context_trims",
                  "chars_saved", "unchanged")
#: 两次落盘的最小间隔（秒）：长会话里压缩每轮都触发，逐次写盘不划算。
PERSIST_INTERVAL_SECONDS = 60.0


def snapshot_path(data_dir: str) -> str:
    """快照文件路径（`DATA_DIR/compress_buckets.json`）。"""
    return os.path.join(data_dir, "compress_buckets.json")


def _counters(value: Any) -> dict[str, int]:
    """校验一组计数字段：缺省按 0，非负整数以外的值视为坏快照。"""
    if not isinstance(value, dict):
        raise ValueError("counters 不是对象")
    counters: dict[str, int] = {}
    for name in COUNTER_FIELDS:
        raw = value.get(name, 0)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise ValueError(f"计数字段非法: {name}={raw!r}")
        counters[name] = raw
    return counters


def load_buckets(path: str, *, now: float | None = None) -> tuple[
        dict[int, dict[str, int]], dict[str, int]]:
    """读回 `(小时桶, 终身累计)`；任何异常都退化为空快照。

    加载时顺手丢掉超过保留期的小时桶（进程停机很久再启动也能自愈）；终身
    累计不裁剪——`/metrics` 的 counter 不能因为旧桶被丢而倒退。
    """
    empty: tuple[dict[int, dict[str, int]], dict[str, int]] = ({}, {})
    moment = time.time() if now is None else now
    cutoff = moment - RETENTION_DAYS * 24 * HOUR_SECONDS
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if raw.get("version") != VERSION:
            logger.warning("压缩收益快照版本不匹配 %s，忽略", path)
            return empty
        buckets_raw = raw.get("buckets", {})
        if not isinstance(buckets_raw, dict):
            raise ValueError("buckets 不是对象")
        buckets: dict[int, dict[str, int]] = {}
        for key, value in buckets_raw.items():
            hour = int(key)
            if hour < cutoff:
                continue
            buckets[hour] = _counters(value)
        return buckets, _counters(raw.get("lifetime"))
    except FileNotFoundError:
        return empty
    except (OSError, ValueError, TypeError, AttributeError) as error:
        logger.warning("压缩收益快照读取失败 %s: %s", path, error)
        return empty


def save_buckets(path: str, payload: dict[str, Any]) -> bool:
    """原子写盘（tmp + `os.replace`）；成功返回 True，失败只记日志。"""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError as error:
        logger.warning("压缩收益快照落盘失败 %s: %s", path, error)
        return False