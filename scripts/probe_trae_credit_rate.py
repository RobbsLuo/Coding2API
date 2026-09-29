#!/usr/bin/env python3
"""临时诊断：一次真实 TRAE 对话，推算 token 与实际消耗积分的比例。

做法（同凭证、串行）：
    1. 探额度（ide_user_ent_usage）→ 记 remaining / used 基线
    2. 发一次最小对话 → 从 token_usage 帧取 prompt/completion/total tokens
    3. 再探额度 → 取 used 增量 = 本次真实消耗积分
    4. 算 credit/token，并与模型声明倍率（consumption_rate.data.rate）对照

注意：额度探测与真实对话都会打上游；期间若有其它流量在跑，差值会掺入别人的消耗。

只读：不改库、不改 src/。用法：
    uv run python scripts/probe_trae_credit_rate.py --model glm-5.2
    uv run python scripts/probe_trae_credit_rate.py --model glm-5.2 --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db.conn import Database  # noqa: E402
from src.db.crypto import CredentialCipher  # noqa: E402
from src.provider.trae.client import TraeClient, prepare_body, solo_headers  # noqa: E402
from src.provider.trae.credential import TraeCredential  # noqa: E402


def load_credential(db_path: str, secret: str, credential_id: str | None):
    db = Database(db_path)
    cipher = CredentialCipher(secret)
    sql = "SELECT id, nickname, data_enc FROM credentials WHERE provider='trae'"
    params: list[str] = []
    if credential_id:
        sql += " AND id = ?"
        params.append(credential_id)
    sql += " ORDER BY created_at"
    for row in db.connect().execute(sql, params).fetchall():
        data = json.loads(cipher.decrypt(row["data_enc"]).decode("utf-8"))
        cred = TraeCredential.from_dict(data)
        if cred.access_token and cred.uid:
            return row["id"], row["nickname"], cred
    raise SystemExit("没有可用的 trae 凭证（缺 access_token / uid）")


async def snapshot(client: TraeClient, credential: TraeCredential) -> dict:
    """探一次额度，返回汇总与逐包明细。"""
    quota = await client.fetch_quota(credential)
    packs = quota.packages or []
    return {
        "remaining": quota.remaining,
        "total": quota.total,
        "used": (quota.total or 0.0) - (quota.remaining or 0.0),
        "packs": [
            {"name": p["name"], "total": p["total"], "used": p["used"]} for p in packs
        ],
    }


async def chat_once(client: TraeClient, credential: TraeCredential, model: str,
                    prompt: str, max_tokens: int = 64) -> dict:
    """发一次最小对话，返回 token_usage 与输出摘要。"""
    body = prepare_body({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }, model)
    usage: dict = {}
    content = ""
    async with client._stream().stream(
        "POST", f"{client.agent_host}/api/agent/v3/llm_utils_chat",
        json=body, headers=solo_headers(credential),
    ) as response:
        if response.status_code >= 400:
            raise SystemExit(
                f"上游 {response.status_code}: "
                f"{(await response.aread()).decode('utf-8', 'replace')[:400]}")
        async for frame in iter_trae_frames(response):
            name = frame[0]
            if name == "token_usage":
                usage = json.loads(frame[1])
            elif name == "output":
                payload = json.loads(frame[1])
                content += payload.get("response") or ""
    return {"usage": usage, "content": content}


async def iter_trae_frames(response):
    from src.engine.sse import iter_frames

    async for frame in iter_frames(response.aiter_bytes()):
        yield frame.event.strip(), frame.data


async def declared_rate(client: TraeClient, credential: TraeCredential, model: str):
    """模型声明倍率（列表里的 consumption_rate.data.rate）。"""
    try:
        for item in await client.fetch_models(credential):
            if item.id.lower() == model.lower():
                return item.credit_rate
    except Exception as error:  # noqa: BLE001
        print(f"（取模型倍率失败：{error}）")
    return None


def _gauss(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """高斯消元解 n×n 线性方程组。"""
    n = len(rhs)
    a = [row[:] for row in matrix]
    b = rhs[:]
    for i in range(n):
        pivot = max(range(i, n), key=lambda k: abs(a[k][i]))
        a[i], a[pivot] = a[pivot], a[i]
        b[i], b[pivot] = b[pivot], b[i]
        for k in range(i + 1, n):
            factor = a[k][i] / a[i][i]
            for j in range(i, n):
                a[k][j] -= factor * a[i][j]
            b[k] -= factor * b[i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (b[i] - sum(a[i][j] * x[j] for j in range(i + 1, n))) / a[i][i]
    return x


def _fit(cols: list[list[float]]) -> dict:
    """cols 每行 = [特征..., 目标]，最小二乘返回系数与 R²。"""
    width = len(cols[0]) - 1
    matrix = [
        [sum(r[i] * r[j] for r in cols) for j in range(width)] for i in range(width)
    ]
    rhs = [sum(r[i] * r[width] for r in cols) for i in range(width)]
    coef = _gauss(matrix, rhs)
    total = sum(r[width] ** 2 for r in cols)
    resid = sum(
        (sum(coef[i] * r[i] for i in range(width)) - r[width]) ** 2 for r in cols
    )
    return {"coef": coef, "r2": 1 - resid / total if total else None}


def least_squares(rows: list[tuple[float, float, float]]) -> dict:
    """两种拟合对照：无截距（纯单价）与带截距（吸收每窗口的并发消耗）。

    并发噪声：账号若同时在跑别的请求，探额度前后差会掺入那部分消耗；
    由于每个取样窗口时长相近，该噪声近似常数偏移 → 带截距模型可把它吸收。
    """
    no_int = _fit([[p, c, y] for p, c, y in rows])
    with_int = _fit([[p, c, 1.0, y] for p, c, y in rows])
    return {
        "a": no_int["coef"][0],
        "b": no_int["coef"][1],
        "r2": no_int["r2"],
        "a2": with_int["coef"][0],
        "b2": with_int["coef"][1],
        "c2": with_int["coef"][2],
        "r2_2": with_int["r2"],
    }


async def noise_floor(client: TraeClient, credential: TraeCredential, rounds: int) -> None:
    """空转对照：不发对话，只做「探额度 → 探额度」，测并发噪声底。"""
    print(f"=== 噪声底（空转 {rounds} 轮，每轮两次探额度）===")
    for index in range(1, rounds + 1):
        first = await snapshot(client, credential)
        second = await snapshot(client, credential)
        print(f"  第 {index} 轮 Δused = {round(second['used'] - first['used'], 6)}")
    print()


async def run(model: str, credential: TraeCredential, runs: int, prompt: str,
              null_rounds: int = 0, quick: bool = False) -> None:
    client = TraeClient()
    base_text = "下面是一段用于测量的填充文本，请忽略内容本身。" * 200
    # 每个用例的填充文本都唯一：避免命中上一轮/上一用例的上游前缀缓存，
    # 否则「输入单价」会被缓存折扣压低，测不出冷输入真实价格。
    def filler(tag: str, length: int = 200) -> str:
        return f"[{tag}-{uuid.uuid4().hex}] " + base_text[: length * 16]

    # 多点取样：输入/输出各自跨越两个量级，便于最小二乘分离单价
    cases = [
        ("base", prompt, 64),
        ("input-heavy", f"{filler('heavy')}\n\n只回复两个字：收到", 16),
        ("output-heavy", "用大约 200 字介绍杭州。", 512),
        ("input-mid", f"{filler('mid', 38)}\n\n只回复两个字：收到", 16),
        ("output-mid", "用大约 50 字介绍杭州。", 128),
        ("both", f"{filler('both', 38)}\n\n用大约 50 字介绍杭州。", 128),
    ]
    if runs != 1:                       # 保留 --runs 语义：>1 时重复 base
        cases = [("base", prompt, 64)] * runs
    if quick:                           # 三点快速模式：省额度，仍能分离 a/b
        cases = cases[:3]
    samples: list[tuple[float, float, float]] = []
    try:
        if null_rounds:
            await noise_floor(client, credential, null_rounds)
        rate = await declared_rate(client, credential, model)
        print(f"模型 {model} 声明倍率 credit_rate = {rate}\n")
        for index, (label, case_prompt, cap) in enumerate(cases, 1):
            before = await snapshot(client, credential)
            result = await chat_once(client, credential, model, case_prompt, cap)
            after = await snapshot(client, credential)

            usage = result["usage"]
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            reasoning_tokens = usage.get("reasoning_tokens")
            total_tokens = usage.get("total_tokens")
            cache_read = usage.get("cache_read_input_tokens")
            cache_create = usage.get("cache_creation_input_tokens")
            credit = round(after["used"] - before["used"], 6)

            print(f"=== {label}（第 {index} 点）===")
            print(f"tokens: prompt={prompt_tokens} completion={completion_tokens} "
                  f"reasoning={reasoning_tokens} total={total_tokens} "
                  f"cache_read={cache_read} cache_create={cache_create}")
            print(f"额度: used {before['used']} → {after['used']}  Δcredit={credit}")
            if total_tokens:
                print(f"credit/total_token = {credit / total_tokens:.6f}")
            if completion_tokens:
                print(f"credit/completion_token = {credit / completion_tokens:.6f}")
            print(f"输出: {result['content'][:40]!r}")
            # 逐包差异：定位是哪一类权益包被扣
            diff = [
                (p["name"], round(n["used"] - p["used"], 6))
                for p, n in zip(before["packs"], after["packs"], strict=False)
                if round(n["used"] - p["used"], 6) != 0
            ]
            print(f"变动包: {diff if diff else '（无）'}\n")
            if prompt_tokens is not None and completion_tokens is not None:
                samples.append((float(prompt_tokens), float(completion_tokens), credit))

        fit = least_squares(samples)
        print("=== 拟合 credit = a·prompt_tokens + b·completion_tokens ===")
        if fit["a"] is None:
            print("样本退化，无法拟合")
        else:
            print(f"无截距: a={fit['a']:.8f}  b={fit['b']:.8f}  "
                  f"b/a={fit['b'] / fit['a']:.3f}  R²={fit['r2']:.5f}")
            print(f"带截距: a={fit['a2']:.8f}  b={fit['b2']:.8f}  "
                  f"c={fit['c2']:+.6f}  b/a={fit['b2'] / fit['a2']:.3f}  "
                  f"R²={fit['r2_2']:.5f}")
            print(f"声明倍率 {rate} ｜ 无截距每百万 token: "
                  f"in={fit['a'] * 1e6:.1f} out={fit['b'] * 1e6:.1f} ｜ "
                  f"带截距每百万 token: in={fit['a2'] * 1e6:.1f} "
                  f"out={fit['b2'] * 1e6:.1f}")
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="glm-5.2")
    parser.add_argument("--credential", default=None)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--null-rounds", type=int, default=0,
                        help="空转轮数：不发对话只探额度，测并发噪声底")
    parser.add_argument("--quick", action="store_true",
                        help="三点快速模式（base/input-heavy/output-heavy）")
    parser.add_argument("--prompt", default="只回复两个字：收到")
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "data/coding2api.sqlite3"))
    args = parser.parse_args()

    settings = load_settings()
    credential_id, nickname, credential = load_credential(
        args.db, settings.app_secret, args.credential)
    print(f"凭证 {credential_id}（{nickname or '无昵称'}）uid={credential.uid}\n")
    asyncio.run(run(args.model, credential, args.runs, args.prompt, args.null_rounds,
                    args.quick))


if __name__ == "__main__":
    main()