#!/usr/bin/env python3
"""临时诊断：上游（Qoder / TRAE）响应里到底有没有「真实模型名」。

背景：`Event.model` 观测在 Qoder 上只看到 `auto`、TRAE 上只有空串，等于盲区。
本脚本发一次最小对话，把**原始** SSE / 信封载荷的字段名与 model 类字段打印出来，
判断是「上游根本不给」还是「给在别的键里」。

只读：不改库、不改 src/（只在进程内包一层解析函数）。用法：
    uv run python scripts/probe_model_field.py --provider qoder --model deepseek-v4.1-flash
    uv run python scripts/probe_model_field.py --provider trae --model glm-5.2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db.conn import Database  # noqa: E402
from src.db.crypto import CredentialCipher  # noqa: E402
from src.provider.qoder.client import QoderClient  # noqa: E402
from src.provider.qoder.events import parse_inner_chunk as qoder_parse  # noqa: E402
from src.provider.trae.client import TraeClient  # noqa: E402
from src.provider.trae.events import SSEFrame  # noqa: E402
from src.provider.trae.events import parse_frame as trae_parse  # noqa: E402

SEEN: set[str] = set()


def dump_fields(label: str, obj: Any) -> None:
    """打印一帧里所有「可能是模型名」的字段（每类只打一次，避免刷屏）。"""
    if not isinstance(obj, dict) or label in SEEN:
        return
    keys = [key for key in obj if isinstance(key, str)
            and ("model" in key.lower() or "route" in key.lower()
                 or "deploy" in key.lower() or "engine" in key.lower())]
    if not keys:
        return
    SEEN.add(label)
    print(f"  [{label}] {json.dumps({k: obj.get(k) for k in keys}, ensure_ascii=False)[:400]}")


def load_credentials(db_path: str, secret: str, provider: str) -> list[tuple[str, dict]]:
    db = Database(db_path)
    cipher = CredentialCipher(secret)
    rows = db.connect().execute(
        "SELECT id, data_enc FROM credentials WHERE provider = ? "
        "AND enabled = 1 AND disabled = 0 ORDER BY created_at", (provider,)).fetchall()
    return [(row["id"], json.loads(cipher.decrypt(row["data_enc"]).decode("utf-8")))
            for row in rows]


def patch_qoder() -> None:
    def wrapped(body: str):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return qoder_parse(body)
        dump_fields("qoder inner chunk", payload)
        choice = (payload.get("choices") or [{}])[0]
        if isinstance(choice, dict):
            dump_fields("qoder choice", choice)
        return qoder_parse(body)

    import src.provider.qoder.client as qoder_client
    import src.provider.qoder.events as qoder_events
    qoder_events.parse_inner_chunk = wrapped
    qoder_client.qoder_events.parse_inner_chunk = wrapped


def patch_trae() -> None:
    def wrapped(frame: SSEFrame):
        if frame.data:
            try:
                payload = json.loads(frame.data)
            except json.JSONDecodeError:
                payload = None
            dump_fields(f"trae {frame.event}", payload)
        return trae_parse(frame)

    import src.provider.trae.client as trae_client
    import src.provider.trae.events as trae_events
    trae_events.parse_frame = wrapped
    trae_client.trae_events.parse_frame = wrapped


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True, choices=("qoder", "trae"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "data/coding2api.sqlite3"))
    args = parser.parse_args()
    settings = load_settings()

    creds = load_credentials(args.db, settings.app_secret, args.provider)
    if not creds:
        raise SystemExit(f"没有可用的 {args.provider} 凭证")
    credential_id, data = creds[0]
    print(f"=== {args.provider} / {args.model} / cred {credential_id} ===")

    payload = {
        "model": args.model,
        "stream": True,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }

    if args.provider == "qoder":
        from src.provider.qoder.credential import QoderCredential
        patch_qoder()
        client = QoderClient()
        credential = QoderCredential.from_dict(data)
        stream = client.stream_chat(credential, payload, args.model)
    else:
        from src.provider.trae.credential import TraeCredential
        patch_trae()
        client = TraeClient()
        credential = TraeCredential.from_dict(data)
        stream = client.stream_chat(credential, payload, args.model)

    seen_events = 0
    try:
        async for event in stream:
            seen_events += 1
            if event.model:
                print(f"  [Event.model] {event.model!r} kind={event.kind}")
            if seen_events > 400:
                break
    finally:
        await client.aclose()
    print(f"事件数 {seen_events}；model 类字段见上（无输出 = 上游不回显）")


if __name__ == "__main__":
    asyncio.run(main())
