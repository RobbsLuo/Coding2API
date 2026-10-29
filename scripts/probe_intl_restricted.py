#!/usr/bin/env python3
"""一次性验证：codebuddy_intl 单个凭证在当前出口下是否仍被 11140 限制。

只发极少量真实请求（默认 3 次、间隔 10s），只挑请求量最少的凭证。
用法：
    uv run python scripts/probe_intl_restricted.py --requests 3 --interval 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db.conn import Database  # noqa: E402
from src.db.crypto import CredentialCipher  # noqa: E402
from src.provider.base import EventKind  # noqa: E402
from src.provider.codebuddy.client import CodeBuddyClient  # noqa: E402
from src.provider.codebuddy.credential import CodeBuddyCredential  # noqa: E402
from src.provider.codebuddy.headers import INTL_ENDPOINT  # noqa: E402
from src.provider.proxy import parse_provider_proxies  # noqa: E402

MODEL = "deepseek-v4.1-flash"


def pick_credential(db_path: str, secret: str) -> tuple[str, dict]:
    """挑 30 天内请求量最少、且当前未被限制的 intl 凭证。"""
    db = Database(db_path)
    cipher = CredentialCipher(secret)
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, data_enc FROM credentials WHERE provider = 'codebuddy_intl' "
        "AND enabled = 1 AND disabled = 0").fetchall()
    counts = {row["credential_id"]: row["n"] for row in conn.execute(
        "SELECT credential_id, count(*) n FROM usage_events "
        "WHERE provider = 'codebuddy_intl' GROUP BY 1")}
    if not rows:
        raise SystemExit("没有可用的 codebuddy_intl 凭证")
    best = min(rows, key=lambda row: counts.get(row["id"], 0))
    data = json.loads(cipher.decrypt(best["data_enc"]).decode("utf-8"))
    return best["id"], data


def all_credentials(db_path: str, secret: str) -> list[tuple[str, dict]]:
    db = Database(db_path)
    cipher = CredentialCipher(secret)
    rows = db.connect().execute(
        "SELECT id, data_enc FROM credentials WHERE provider = 'codebuddy_intl' "
        "AND enabled = 1 AND disabled = 0 ORDER BY created_at").fetchall()
    return [(row["id"], json.loads(cipher.decrypt(row["data_enc"]).decode("utf-8")))
            for row in rows]


def pick_by_id(db_path: str, secret: str, credential_id: str) -> tuple[str, dict]:
    for cid, data in all_credentials(db_path, secret):
        if cid == credential_id:
            return cid, data
    raise SystemExit(f"凭证 {credential_id} 不存在或不可用")


async def attempt(client: CodeBuddyClient, data: dict) -> tuple[str, str]:
    payload = {
        "stream": True,
        "max_tokens": 16,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Reply with the single word: pong"},
        ],
    }
    text = ""
    try:
        # 直接调用客户端（绕过 pacer）：探活只发 3 次，不需要排队节流
        iterator = client.stream_chat(
            CodeBuddyCredential.from_dict(data), payload, MODEL)
        async for event in iterator:
            if event.kind is EventKind.ERROR:
                return "ERROR", event.error_message or "-"
            if event.kind is EventKind.CONTENT and event.content:
                text += event.content
        return ("OK", text[:60]) if text else ("EMPTY", "-")
    except Exception as error:  # noqa: BLE001
        return type(error).__name__, str(error)[:300]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=3)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--credential", default="")
    parser.add_argument("--all", action="store_true", help="每个 intl 凭证各测一次")
    args = parser.parse_args()

    config = load_settings()
    # 代理走配置里的 PROVIDER_PROXIES（与其他 probe 脚本一致），不写死本机端口：
    # codebuddy_intl 通常必须走代理，但端口因机器而异。
    proxies = parse_provider_proxies(config.provider_proxies, (
        "trae", "codebuddy", "codebuddy_intl", "zen", "kilo", "qoder", "codearts"))
    client = CodeBuddyClient(endpoint=INTL_ENDPOINT, require_leading_system=True,
                             proxy=proxies.get("codebuddy_intl"))
    if args.all:
        for credential_id, data in all_credentials(config.db_path, config.app_secret):
            status, detail = await attempt(client, data)
            print(f"{credential_id}: {status} {detail[:120]}")
            await asyncio.sleep(args.interval)
        return

    credential_id, data = (pick_by_id(config.db_path, config.app_secret, args.credential)
                           if args.credential else
                           pick_credential(config.db_path, config.app_secret))
    print(f"凭证 {credential_id} · 模型 {MODEL} · 出口 {config.provider_proxies}")
    for i in range(args.requests):
        status, detail = await attempt(client, data)
        print(f"  第 {i + 1} 次: {status} {detail}")
        if status != "OK":
            break
        if i + 1 < args.requests:
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
