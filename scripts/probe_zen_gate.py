#!/usr/bin/env python3
"""临时诊断：Zen / Console 免费层门禁形态 A/B 实测。

只读：不改库、不改 src/、不写配置。每个组合只发一次最小流式请求
（`max_tokens=8`），用于回答：

1. `legacy`（1.x）与 `v2`（2.x）两套头/消息形态，哪个能过门禁；
2. `/zen/v1`、`/inference/v1`、`/inference/openai/v1` 三条对话根是否都活着；
3. 429 是否为匿名额度耗尽（看 `Retry-After`），而不是形态被拒。

用法：
    uv run python scripts/probe_zen_gate.py
    uv run python scripts/probe_zen_gate.py --model deepseek-v4.1-flash-free
    uv run python scripts/probe_zen_gate.py --profile v2 --root /inference/openai/v1

判断口径：
- `200`：形态可用；
- `403/426`：形态被门禁拒绝；
- `429` + `Retry-After`：匿名免费额度耗尽，**该轮无法判定形态**；
- `429` 无 `Retry-After`：可能是缺 `<env>` 块（官方 2.x 抓包口径）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.provider.zen.client import (  # noqa: E402
    CLIENT_PROFILES,
    gate_headers,
    prepare_body,
)

HOST = "https://opencode.ai"
ROOTS = ("/zen/v1", "/inference/v1", "/inference/openai/v1")


async def probe(client: httpx.AsyncClient, profile: str, root: str,
                model: str) -> dict[str, object]:
    """发一次最小请求，返回状态码 / Retry-After / 正文摘要。"""
    body, _ = prepare_body(
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        model, ("bash", "read"), profile=profile)
    headers = gate_headers(profile=profile)
    response = await client.post(f"{HOST}{root}/chat/completions",
                                 json=body, headers=headers)
    raw = response.text[:240].replace("\n", " ")
    return {
        "status": response.status_code,
        "retry_after": response.headers.get("Retry-After"),
        "body": raw,
    }


def verdict(status: int, retry_after: str | None) -> str:
    if status == 200:
        return "可用"
    if status in (403, 426):
        return "形态被拒"
    if status == 429 and retry_after:
        return "额度耗尽，无法判定形态"
    if status == 429:
        return "疑似缺 <env> 块或额度信号异常"
    return "其它"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="big-pickle", help="免费模型 id")
    parser.add_argument("--profile", choices=CLIENT_PROFILES, action="append",
                        help="只测指定档位（可重复；默认两档都测）")
    parser.add_argument("--root", action="append",
                        help="只测指定对话根（可重复；默认三条都测）")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    profiles = args.profile or list(CLIENT_PROFILES)
    roots = args.root or list(ROOTS)
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        for profile in profiles:
            for root in roots:
                try:
                    result = await probe(client, profile, root, args.model)
                except httpx.HTTPError as error:
                    print(f"{profile:<6} {root:<22} 连接失败: {error}")
                    continue
                status = int(result["status"])
                retry = result["retry_after"]
                print(f"{profile:<6} {root:<22} {status:<3} "
                      f"{verdict(status, retry):<20} "
                      f"retry_after={retry or '-'}")
                print(f"       {result['body']}")


if __name__ == "__main__":
    asyncio.run(main())