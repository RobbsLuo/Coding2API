"""sk- API Key：生成、摘要存储、常量时间校验。"""

from __future__ import annotations

import hashlib
import secrets
import time

API_KEY_PREFIX = "sk-"
API_KEY_RANDOM_BYTES = 32


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_RANDOM_BYTES)


def digest_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def preview_api_key(api_key: str) -> str:
    """仅保留前缀与末 4 位，其余打码。"""
    tail = api_key[-4:] if len(api_key) > 4 else ""
    return f"{API_KEY_PREFIX}…{tail}"


def key_expired(expires_at: int | None, *, now: int | None = None) -> bool:
    """Key 是否已过期（P0-3）。`None`/`0` = 永不过期。

    `now` 可注入便于测试；到期时刻本身算「已过期」（`>=`），与「有效期至
    某时刻」的直觉一致。
    """
    if not expires_at:
        return False
    moment = int(time.time()) if now is None else int(now)
    return moment >= expires_at
