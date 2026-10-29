"""CodeBuddy 模型可用性探活结果的落盘缓存。

为什么需要：国际站的 `filter_available_models` 会为每个账号并发打 20 次
`max_tokens=1` 的探活请求，结果原本只存在**进程内**字典（TTL 6h）。进程一
重启缓存即空，于是每次重启都对每个账号重放一轮 20 连发——同 IP、秒级窗口、
并发 4，正是上游风控眼里的自动化特征（2026-10-27 当天 11 次重启 ≈ 1320 次）。

落盘后重启不再重探；文件损坏 / 版本不符一律当作空缓存（探活是可选优化，
绝不能因为它把模型列表带崩）。

只存「账号标识 + endpoint → 模型可用性」，不存 token 明文：key 是昵称或
token 的 SHA-256（与进程内缓存同一套 identity，见
`CodeBuddyClient._model_access_cache_key`）。
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

VERSION = 1
# 条目上限：账号数 × 渠道数的量级，超出按最旧淘汰（文件不能无界增长）
MAX_ENTRIES = 256


def cache_path(data_dir: str) -> str:
    """落盘位置：与模型目录快照同一个 data 目录。"""
    return os.path.join(data_dir, "codebuddy-model-access.json")


class ModelAccessStore:
    """`{(identity, endpoint): (checked_at, {model: bool|None})}` 的原子落盘缓存。"""

    def __init__(self, path: str, ttl_seconds: float,
                 *, clock=time.time) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[tuple[str, str], tuple[float, dict[str, bool | None]]] = {}
        self._loaded = False

    # ------------------------------------------------------------ 读

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            # 文件缺失 / 损坏 / 无权限：空缓存即可，探活照常发生
            return
        if not isinstance(payload, dict) or payload.get("version") != VERSION:
            return
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return
        cutoff = self._clock() - self.ttl_seconds
        for raw_key, raw_value in entries.items():
            parsed = _parse_entry(raw_key, raw_value, cutoff)
            if parsed is not None:
                self._entries[parsed[0]] = parsed[1]

    def get(self, key: tuple[str, str]) -> tuple[float, dict[str, bool | None]] | None:
        """命中未过期条目返回 `(checked_at, access)`；否则 None。"""
        self._load()
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry[0] >= self.ttl_seconds:
            del self._entries[key]
            return None
        return entry

    # ------------------------------------------------------------ 写

    def put(self, key: tuple[str, str], checked_at: float,
            access: dict[str, bool | None]) -> None:
        self._load()
        self._entries[key] = (checked_at, dict(access))
        if len(self._entries) > MAX_ENTRIES:
            oldest = sorted(self._entries, key=lambda item: self._entries[item][0])
            for stale in oldest[:len(self._entries) - MAX_ENTRIES]:
                del self._entries[stale]
        self._save()

    def _save(self) -> None:
        """原子写盘；失败只记日志（缓存不是正确性依赖）。"""
        payload = {
            "version": VERSION,
            "entries": {
                _dump_key(key): {"checked_at": entry[0], "access": entry[1]}
                for key, entry in self._entries.items()
            },
        }
        tmp = f"{self.path}.tmp"
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            logger.warning("模型可用性缓存写盘失败：%s", self.path, exc_info=True)


def _dump_key(key: tuple[str, str]) -> str:
    return f"{key[0]}\x1f{key[1]}"


def _parse_entry(raw_key: object, raw_value: object, cutoff: float):
    """落盘条目 → (key, (checked_at, access))；形状不对/过期返回 None。"""
    if not isinstance(raw_key, str) or "\x1f" not in raw_key:
        return None
    if not isinstance(raw_value, dict):
        return None
    checked_at = raw_value.get("checked_at")
    access = raw_value.get("access")
    if not isinstance(checked_at, (int, float)) or not isinstance(access, dict):
        return None
    if float(checked_at) < cutoff:
        return None
    clean = {model: value for model, value in access.items()
             if isinstance(model, str) and (value is None or isinstance(value, bool))}
    identity, _, endpoint = raw_key.partition("\x1f")
    return (identity, endpoint), (float(checked_at), clean)
