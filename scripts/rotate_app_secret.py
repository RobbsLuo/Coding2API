#!/usr/bin/env python3
"""原地轮换 APP_SECRET：用旧密钥解密全部凭证、用新密钥重新加密。

`src/db/crypto.py` 从 APP_SECRET 派生 Fernet 密钥（无盐单次 SHA-256），
**换掉 APP_SECRET 等于换掉密钥**：不做这一步，已有凭证全部不可解，只能重录。
PROPOSAL §6 明确「不做密钥轮换」，所以这里刻意做成一次性运维脚本而不是启动
期的自动迁移——轮换是需要停机 + 备份 + 人工确认的动作。

用法（**先停机**，否则运行中的进程会继续用旧密钥写库）：

    python3 scripts/backup_db.py                 # 1. 先留一份一致性备份
    python3 scripts/rotate_app_secret.py \\
        --old-secret-file /path/to/old.env \\
        --new-secret-file /path/to/new.env       # 2. 重新加密
    # 3. 把 .env 的 APP_SECRET 换成新值，chmod 600 .env，重启服务

`--old-secret-file` / `--new-secret-file` 接受 `.env` 文件或只含密钥文本的文件
（自动识别 `APP_SECRET=` 前缀）。不要把密钥写在命令行参数里：会进 shell
history 与 `ps`。

脚本是**幂等安全**的：所有行先解密再加密，全部成功才在一个事务里提交；
任何一条解不开就整体回滚，库保持原样。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.crypto import CredentialCipher  # noqa: E402


def _read_secret(path: Path) -> str:
    """从 `.env` 或纯文本文件读密钥；拒绝空值与含换行的可疑内容。"""
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("APP_SECRET="):
            value = stripped.split("=", 1)[1].strip().strip("'\"")
            if not value:
                raise SystemExit(f"{path}: APP_SECRET 为空")
            return value
    # 没有 `APP_SECRET=` 前缀：把整个文件当密钥（单行）
    value = text.strip()
    if not value:
        raise SystemExit(f"{path}: 文件为空")
    if "\n" in value:
        raise SystemExit(f"{path}: 既不是 .env 也不是单行密钥")
    return value


def rotate(db_path: Path, old_secret: str, new_secret: str) -> int:
    """把 `credentials.data_enc` 全部用新密钥重写；返回处理行数。"""
    old = CredentialCipher(old_secret, allow_known_weak=True)
    new = CredentialCipher(new_secret)
    if old_secret == new_secret:
        raise SystemExit("新旧 APP_SECRET 相同，无需轮换")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, data_enc FROM credentials").fetchall()
        # 先在内存里把每一行都解出来：任何一条失败就在写库前中止，
        # 避免「改了一半」留下新旧密钥混杂、谁也用不了的库。
        prepared: list[tuple[bytes, str]] = []
        for row in rows:
            try:
                plaintext = old.decrypt(row["data_enc"])
            except Exception as error:  # noqa: BLE001 - 逐行报告，整体中止
                raise SystemExit(
                    f"凭证 {row['id']} 无法用旧密钥解密（{type(error).__name__}）："
                    "旧密钥不对，或该行已被别的密钥加密。未做任何修改。") from error
            # 校验解出来是合法 JSON：坏行在这里暴露，而不是等到运行时
            try:
                json.loads(plaintext.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SystemExit(
                    f"凭证 {row['id']} 解密后不是合法 JSON：{error}") from error
            prepared.append((new.encrypt(plaintext), row["id"]))

        with conn:                      # 单事务：全成或全不成
            for ciphertext, credential_id in prepared:
                conn.execute("UPDATE credentials SET data_enc = ? WHERE id = ?",
                             (ciphertext, credential_id))
        return len(prepared)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="轮换 APP_SECRET 并重加密全部凭证")
    parser.add_argument("--db", required=True, help="SQLite 主库路径")
    parser.add_argument("--old-secret-file", required=True,
                        help="含旧 APP_SECRET 的文件（.env 或单行文本）")
    parser.add_argument("--new-secret-file", required=True,
                        help="含新 APP_SECRET 的文件（.env 或单行文本）")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")
    count = rotate(db_path,
                   _read_secret(Path(args.old_secret_file)),
                   _read_secret(Path(args.new_secret_file)))
    print(f"已用新密钥重新加密 {count} 条凭证。")
    print("下一步：把 .env 的 APP_SECRET 换成新值、chmod 600 .env、重启服务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
