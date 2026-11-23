"""原子 JSON 快照的公共读写。

两套目录快照（`api/model_catalog.py` 的渠道模型表、`benchmarks.py` 的
OpenRouter 三表）原先各写一份「tmp + `os.replace` 原子写 + 宽容读取」的
同构代码，口径一旦漂移就会出现「一边容忍坏快照、另一边启动即崩」的分叉。
公共口径收敛到这里，各模块只留自己的文件名、版本号与 payload 构造。

**宽容读取**：文件缺失 / 损坏 / 版本不符 / 超龄一律退化成调用方给的
`fallback`，只丢缓存不丢服务——快照只是加速与兜底手段，任何解析失败都必须
安静降级。整份文件一个 `try`，不做逐字段防御：原子写已排除半截写，坏文件
只可能来自手改或旧版本。

**新鲜度由 validator 判**：两套快照的粒度不同——模型目录是**逐渠道**
`saved_at`（每渠道各自折算 TTL），OpenRouter 是**整份**一个 `saved_at`。
故 `load_snapshot` 只把 `now` / `max_age` 交给 validator，由它决定超龄的
作用域，公共层不替它挑一种语义。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# (raw payload, now, max_age) → 调用方自己的结果；抛异常即视为坏快照。
type SnapshotValidator[T] = Callable[[Any, float, float], T]


def save_snapshot(path: str, payload: dict[str, Any]) -> None:
    """原子写盘（tmp + `os.replace`）；目录自动建，失败只记日志。

    原子替换是必须的：读到写了一半的 JSON 会让下一次启动直接丢掉整份缓存。
    写失败只记日志——快照是加速手段，不能让它把聊天带崩。
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as error:
        logger.warning("模型目录落盘失败 %s: %s", path, error)


def load_snapshot[T](
    path: str,
    *,
    version: int,
    max_age: float,
    validator: SnapshotValidator[T],
    fallback: T,
    now: float | None = None,
) -> T:
    """读回快照并交给 `validator` 还原；任何异常都退化成 `fallback`。

    `version` 不符直接忽略（不猜旧/新格式）；`max_age` 与 `now` 只转交
    validator，超龄判定的粒度由各模块自己定（见模块 docstring）。
    """
    moment = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if raw.get("version") != version:
            logger.warning("模型目录版本不匹配 %s，忽略", path)
            return fallback
        return validator(raw, moment, max_age)
    except FileNotFoundError:
        return fallback
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        logger.warning("模型目录读取失败 %s: %s", path, error)
        return fallback