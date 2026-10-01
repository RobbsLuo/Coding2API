"""模型目录的落盘快照（跨重启）：provider → {小写 id: Model}。

**为什么落盘**：进程内缓存重启即丢，代价有两处，都是实测过的真故障：

1. **启动窗口扇出**：别名表（`services.model_aliases`）要等启动预热跑完
   才有内容，而预热里 zen 的 `fetch_models` 会逐个免费模型真发探活请求
   （十几秒）。这段时间里扁平名请求无法把候选收窄到真正持有该模型的渠道，
   于是 CodeBuddy 对 kilo 的免费模型回 11102、TRAE 回 4001 —— 对侧留请求
   记录、本地多几条 `invalid_request`，纯属白打。
2. **兜底消失**：某渠道拉取失败时靠进程内缓存兜底（`list_models`），重启
   后缓存没了，那条渠道的模型会从 `/v1/models` 整体消失。

所以这里把每次成功拉取到的**未过滤原始表**写到 `DATA_DIR/model_catalog.json`，
启动时同步读回并立即 publish 别名表（零上游请求），预热退化为纯后台刷新。

**格式**（version 1）：

    {"version": 1,
     "providers": {"kilo": {"saved_at": 1790827000.0,
                            "models": [{"id": "kilo-auto/free", ...}]}}}

存原始表而非过滤后的结果：`MODEL_BLOCKLIST` 是热更项，过滤在出口现做
（见 `api/models.py::_visible`），落盘时就滤掉会让改完黑名单要等下一次刷新。

**宽容读取**：文件损坏 / 版本不符一律只丢缓存，不丢服务——这个文件只是加速
手段，任何解析失败都必须安静降级到「没有缓存」（整份文件一个 try，不做逐字段
防御：原子写已排除半截写，坏文件只可能来自手改或旧版本）。写入同样如此：
落盘失败只记日志，绝不影响聊天。

快照超过 `MAX_AGE_SECONDS` 直接丢弃：停机很久的部署不该拿陈旧目录发请求
（启动后几秒内就会被后台刷新覆盖，这个上限只是兜住「刷新一直失败」的场合）。
`saved_at` 随表一起交回调用方，用于把快照年龄折进 TTL——快照不只是数据，
还带新鲜度。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import fields
from typing import Any

from ..provider.base import Model

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "model_catalog.json"
CATALOG_VERSION = 1
# 快照最长可信时长（秒）：7 天。启动后后台刷新会立刻覆盖它，这个上限只用来
# 兜住「停机很久 + 上游一直拉不通」的组合——那时宁可没有目录（退化成老行为：
# 保守地按全部渠道扇出），也不拿几天前的模型归属去收窄候选。
MAX_AGE_SECONDS = 7 * 24 * 3600

# 参与序列化的 Model 字段：dataclasses.fields 取当前定义，新增字段自动带上，
# 读取时只取认识的键 —— 老快照缺字段用 dataclass 默认值，新快照多出的键忽略。
_MODEL_FIELDS: tuple[str, ...] = tuple(field.name for field in fields(Model))


def catalog_path(data_dir: str) -> str:
    return os.path.join(data_dir, CATALOG_FILENAME)


def save_catalog(data_dir: str,
                 tables: dict[str, dict[str, Model]]) -> None:
    """把各渠道的模型表原子写盘（tmp + replace）。

    原子替换是必须的：读到写了一半的 JSON 会让下一次启动直接丢掉整份缓存。
    写失败只记日志——缓存是加速手段，不能让它把聊天带崩。
    """
    payload = {
        "version": CATALOG_VERSION,
        "providers": {
            provider_id: {"saved_at": time.time(),
                          "models": [_model_to_dict(model)
                                     for model in table.values()]}
            for provider_id, table in tables.items() if table
        },
    }
    path = catalog_path(data_dir)
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as error:
        logger.warning("模型目录落盘失败 %s: %s", path, error)


def load_catalog(
        data_dir: str, *, now: float | None = None,
) -> dict[str, tuple[float, dict[str, Model]]]:
    """读回 `provider → (saved_at, {小写 id: Model})`；任何异常都退化成空目录。

    整份文件一个 try：原子写（tmp + replace）已经排除了半截写，坏文件只可能
    来自手改或旧版本，逐字段的防御分支不值得写——解析不了就当没有缓存。
    `saved_at` 要带回给调用方：恢复时据此推算 TTL（`restore_model_catalog`），
    否则三分钟前的快照与三天前的一样新，启动预热会立刻把它全量重拉一遍。
    """
    path = catalog_path(data_dir)
    moment = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if raw.get("version") != CATALOG_VERSION:
            logger.warning("模型目录版本不匹配 %s，忽略", path)
            return {}
        return {provider_id: entry
                for provider_id, record in raw["providers"].items()
                if (entry := _restore_entry(record, moment))}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        logger.warning("模型目录读取失败 %s: %s", path, error)
        return {}


def _restore_entry(record: Any, now: float) -> tuple[float, dict[str, Model]] | None:
    """单条渠道快照 → `(saved_at, {小写 id: Model})`；已过期返回 None。"""
    saved_at = record["saved_at"]
    if now - saved_at > MAX_AGE_SECONDS:
        return None
    table = {model.id.lower(): model for model in map(_model_from_dict, record["models"])
             if model is not None}
    return (saved_at, table) if table else None


def _model_to_dict(model: Model) -> dict[str, Any]:
    return {name: getattr(model, name) for name in _MODEL_FIELDS}


def _model_from_dict(raw: Any) -> Model | None:
    """快照条目 → Model；非 dict / id 非空串一律跳过（坏条目不拖垮整表）。"""
    if not isinstance(raw, dict):
        return None
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    fields_in = {name: raw[name] for name in _MODEL_FIELDS
                 if name != "id" and name in raw}
    return Model(id=model_id, **fields_in)