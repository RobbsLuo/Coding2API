#!/usr/bin/env python3
"""清理「该渠道从来没有这个模型」的负缓存行（`reason='blocked'`）。

背景：负缓存是 (凭证, 模型) 粒度的「该后端无此模型」，由上游明确拒绝触发
（CodeBuddy 11102 / officeAce 81004·81009 / CodeArts InferHub.002002009）。
它本该只落在**真正提供过该模型**的渠道上。

但 2026-10-26 实测到一批被污染的负缓存：`deepseek-v4.1-flash-1017` 只有
officeAce 的 `/v2/models` 挂着（26 个模型之一），探活判死后别名表丢了归属，
请求扇出到 codebuddy / codearts / codebuddy_intl，三家各回「无此模型」，
于是 8 条负缓存写在了**从没有该模型**的渠道上——它们锁的是「上游没有这个
模型」，可这些上游本来就没有，无从谈起，纯粹是噪声。

判定（保守）：一条 blocked 负缓存行，若该**渠道的模型目录里根本没有这个
模型**（按小写原名比对），则是扇出留下的噪声 → 可删。

为什么这条判据是安全的：
  * 负缓存的语义是「该后端无此模型」，前提是**该后端曾被问过这个模型**。
    目录里没有 → 从没被问过（或问的是别的名字）→ 这条缓存拦不到任何东西。
  * 反过来，目录里**有**这个模型的行一律保留：那是上游真的把它下架了
    （officeAce 的 81009），负缓存正是应有的避让。
  * 目录是**缓存快照**（`data/model_catalog.json`），可能过期。只用它做
    「该渠道确实不认识这个名字」的判据，不做「上游已恢复」的判据。

用法：
    python3 scripts/cleanup_stale_blocked.py              # 预览，不写库
    python3 scripts/cleanup_stale_blocked.py --apply      # 备份后删除
    python3 scripts/cleanup_stale_blocked.py --db PATH    # 指定数据库
    python3 scripts/cleanup_stale_blocked.py --all        # 连「目录里有」的
                                                          # 也删（不推荐）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._common import backup  # noqa: E402

# 落盘目录快照里的字段名（见 `src/api/model_catalog.py` 的格式说明）。
_CATALOG_PROVIDERS = "providers"
_CATALOG_MODELS = "models"


def _catalog_models(data_dir: Path) -> dict[str, set[str]]:
    """落盘目录快照 → {渠道: {小写原名}}。

    快照损坏 / 不存在 → 空 dict（本脚本据此**什么也不删**：没有目录就没有
    「该渠道不认识这个名字」的判据）。读取失败绝不静默成「目录为空」——
    那会把所有负缓存都当成噪声删掉。
    """
    path = data_dir / "model_catalog.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    providers = payload.get(_CATALOG_PROVIDERS)
    if not isinstance(providers, dict):
        return {}
    tables: dict[str, set[str]] = {}
    for provider_id, record in providers.items():
        models = record.get(_CATALOG_MODELS) if isinstance(record, dict) else None
        if not isinstance(models, list):
            continue
        tables[provider_id] = {
            str(item.get("id", "")).lower()
            for item in models if isinstance(item, dict) and item.get("id")
        }
    return tables


def stale_rows(conn, tables: dict[str, set[str]],
               *, include_known: bool = False) -> list[tuple[str, str, str, int]]:
    """待删的负缓存行：(渠道, 模型, 凭证 id, 到期时刻)。

    `include_known=False`（默认）只返回「该渠道的目录里没有这个模型」的行。
    `include_known=True` 时连「目录里有」的也返回——那是管理员确认上游已恢复
    后的强制清理。
    """
    rows = conn.execute(
        "SELECT cred.provider AS provider, c.model AS model, "
        "       c.credential_id AS credential_id, c.cooling_until AS cooling_until "
        "FROM credential_model_cooldowns c "
        "JOIN credentials cred ON cred.id = c.credential_id "
        "WHERE c.reason = 'blocked' AND c.cooling_until > ? "
        "ORDER BY cred.provider, c.model, c.credential_id",
        (int(time.time()),),
    ).fetchall()
    stale: list[tuple[str, str, str, int]] = []
    for row in rows:
        provider, model, credential_id, cooling_until = row
        known = tables.get(provider)
        if known is None:
            # 该渠道的目录快照没有它 → 无从判断，保守保留
            continue
        if model.lower() in known and not include_known:
            continue
        stale.append((provider, model, credential_id, int(cooling_until)))
    return stale


def delete_rows(conn, stale: list[tuple[str, str, str, int]]) -> int:
    """删除待清理的负缓存行，返回删除条数。"""
    total = 0
    for _provider, model, credential_id, _until in stale:
        cursor = conn.execute(
            "DELETE FROM credential_model_cooldowns "
            "WHERE credential_id = ? AND model = ? AND reason = 'blocked'",
            (credential_id, model))
        total += cursor.rowcount
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="清理「该渠道从来没有这个模型」的负缓存行")
    parser.add_argument("--db", help="SQLite 路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--data-dir", help="数据目录（默认取数据库所在目录）")
    parser.add_argument("--apply", action="store_true",
                        help="实际删除（默认只预览）")
    parser.add_argument("--all", action="store_true",
                        help="连「目录里登记过」的负缓存也删（上游已恢复时用）")
    args = parser.parse_args(argv)

    from src.config import load_settings
    from src.db.conn import Database

    db_path = Path(args.db) if args.db else Path(load_settings().db_path)
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")
    data_dir = Path(args.data_dir) if args.data_dir else db_path.parent

    tables = _catalog_models(data_dir)
    if not tables:
        print("没有可用的模型目录快照（缺 model_catalog.json 或读取失败）："
              "无判据，不删任何行")
        return 0

    database = Database(db_path)
    conn = database.connect()
    stale = stale_rows(conn, tables, include_known=args.all)
    if not stale:
        print("没有需要清理的负缓存")
        return 0
    for provider, model, credential_id, until in stale:
        print(f"  {provider:<14} {model:<28} {credential_id} "
              f"到期 {until}")
    total = len(stale)
    scope = "全部" if args.all else "该渠道目录里没有的"
    print(f"共 {total} 条负缓存（{scope}）")

    if not args.apply:
        print("预览模式，未修改数据库；确认无误后加 --apply 执行")
        return 0

    target = backup(db_path)
    deleted = delete_rows(conn, stale)
    conn.commit()
    print(f"已删除 {deleted} 条负缓存（备份: {target}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
