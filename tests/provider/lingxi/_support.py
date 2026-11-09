"""灵犀客户端测试共享装配。"""

from __future__ import annotations

import json
from typing import Any

from src.provider.lingxi.client import LingxiClient
from src.provider.lingxi.credential import LingxiCredential
from tests.helpers.http import provider_client

CREDENTIAL = LingxiCredential("sid-abc", "42")


def client(handler, **kw: Any) -> LingxiClient:
    kw.setdefault("host", "https://lingxi.test")
    kw.setdefault("fallback_host", "https://kdocs.test")
    return provider_client(LingxiClient, handler, **kw)


def frame(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload) + "\n"


def lines(*payloads: dict[str, Any]) -> str:
    return "".join(frame(payload) for payload in payloads)
