#!/usr/bin/env python3
"""从备份恢复 SQLite 主库（校验 + 安全副本 + 原子替换）。

**先停服再恢复**：本脚本不做「服务是否在跑」的探测——那种探测在容器 / 其它
主机 / 权限不足时都可能误判，宁可由你确认。服务在跑时替换主库，进程里既有的
连接仍指向旧文件，且后续 WAL 写入可能落回被替换掉的文件。

行为：
  - **默认预览**：只校验备份完整性、打印将要执行的动作，不改动主库；
  - `--apply`：校验备份 → 给现有主库做一份 `.pre-restore-<时间戳>` 安全副本
    （走 `VACUUM INTO`，不会漏掉 WAL 里未 checkpoint 的事务）→ 原子替换 →
    删除 `-wal`/`-shm` 残留（它们属于旧库，留着会损坏新库）。

恢复后记得**重启服务**。

用法：
    python3 scripts/restore_db.py data/backups/coding2api-20261014-030000.sqlite3
    python3 scripts/restore_db.py <备份> --apply        # 实际执行
    python3 scripts/restore_db.py <备份> --db PATH      # 指定主库
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db.backup import restore_database, verify_app_database  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从备份恢复 SQLite 主库")
    parser.add_argument("backup", help="备份文件路径")
    parser.add_argument("--db", help="主库路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--apply", action="store_true", help="实际替换（默认只预览）")
    args = parser.parse_args(argv)

    source = Path(args.backup)
    if not source.is_file():
        parser.error(f"备份文件不存在: {source}")
    db_path = Path(args.db) if args.db else Path(load_settings().db_path)

    # 预览也要先校验：坏备份越早发现越好（别等到 --apply 才失败）。
    # 用 `verify_app_database` 而不是只查 integrity_check：任意完好 sqlite 都能
    # 通过完整性校验，但缺核心表/版本更高的库换进生产会让服务启动即挂。
    verify_app_database(source)
    print(f"备份校验通过: {source}")
    print(f"主库: {db_path}")
    if not args.apply:
        print("预览模式，未修改主库；确认服务已停止后加 --apply 执行")
        return 0

    safety = restore_database(db_path, source)
    print(f"已从备份恢复: {source} → {db_path}")
    if safety is not None:
        print(f"恢复前主库已留安全副本: {safety}")
    print("请重启服务使新库生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
