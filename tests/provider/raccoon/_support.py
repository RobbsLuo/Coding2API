"""小浣熊渠道测试共享装配。"""

from __future__ import annotations

from src.provider.raccoon.client import RaccoonClient
from tests.helpers.http import provider_client

__all__ = ["client"]


def client(handler, **kw) -> RaccoonClient:
    kw.setdefault("host", "https://raccoon.test")
    return provider_client(RaccoonClient, handler, **kw)