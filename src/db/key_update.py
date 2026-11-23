"""按字段增量更新 `api_keys` 的可编辑列（管理台「编辑 Key」）。

为什么不放进 `repo.py` 的 `ApiKeyRepository`：该文件正被并行改动占用。这里
只做一条参数化 `UPDATE`，列名过白名单、值一律走占位符——与 `set_quota` 同一
写路径，只是列集合更大。

**不可编辑列**：`key_digest` / `preview`（Key 凭据本身，换 Key 必须重新签发）、
`id`（审计归属）、`username`（越权边界）、`created_at` / `last_used_at`
（时间线由系统写）。传入白名单外的列直接抛 `ValueError`，而不是静默忽略。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .repo import ApiKeyRepository

#: 管理台可改的列。顺序即审计 detail 的字段顺序（稳定可读）。
EDITABLE_COLUMNS: tuple[str, ...] = (
    "name", "provider_binding", "allowed_ips", "allowed_models", "expires_at",
)

_EDITABLE = frozenset(EDITABLE_COLUMNS)


def update_api_key_fields(repo: ApiKeyRepository, key_id: str, username: str,
                          changes: Mapping[str, Any]) -> bool:
    """按 `changes` 增量更新一把 Key 的可编辑列；返回 False = 不存在或不属于该用户。

    带上 `username` 条件：管理台只能改自己名下的 Key，否则任何登录用户都能改
    别人的白名单（越权写）。「不存在」与「不是你的」刻意不区分，避免按 id
    枚举别人的 Key。

    空 `changes` 不写库（返回 True）：调用方已先查过行存在性，且「没有任何
    字段变化」不需要发一次空写事务。
    """
    unknown = set(changes) - _EDITABLE
    if unknown:
        raise ValueError(f"不可编辑的列: {', '.join(sorted(unknown))}")
    if not changes:
        return True
    # 列名来自上面的白名单（不是用户输入），值全部参数化。
    assignments = ", ".join(f"{column} = ?" for column in changes)
    with repo._db.transaction() as conn:
        cursor = conn.execute(
            f"UPDATE api_keys SET {assignments} WHERE id = ? AND username = ?",
            (*changes.values(), key_id, username))
    return cursor.rowcount > 0
