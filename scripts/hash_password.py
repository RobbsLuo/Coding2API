#!/usr/bin/env python3
"""添加或更新管理台用户。

用法：
    python3 scripts/hash_password.py <用户名> [--output secrets/users.txt]

重复使用同一用户名会替换旧记录（原子写回）。用户文件格式为
`用户名:pbkdf2_sha256$迭代数$盐$摘要`，是系统的唯一用户源。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth.users import PBKDF2_ITERATIONS, create_password_hash  # noqa: E402

DEFAULT_OUTPUT = "secrets/users.txt"


def load_records(path: Path) -> list[str]:
    if not path.is_file():
        return []
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def upsert(records: list[str], username: str, password_hash: str) -> list[str]:
    prefix = f"{username}:"
    kept = [line for line in records if not line.startswith(prefix)]
    kept.append(f"{username}:{password_hash}")
    return kept


def write_atomic(path: Path, records: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同目录临时文件 + rename，避免写入中断损坏已有用户
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".users-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("\n".join(records) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="添加或更新管理台用户")
    parser.add_argument("username")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--iterations", type=int, default=PBKDF2_ITERATIONS)
    parser.add_argument("--password", help="非交互模式使用（默认交互输入，不回显）")
    args = parser.parse_args(argv)

    if not args.username.strip():
        parser.error("用户名不能为空")
    if args.username != args.username.strip():
        parser.error("用户名不能包含首尾空白")

    # 区分「未提供 --password」（交互输入）与「提供了空值」（直接拒绝）
    if args.password is None:
        password = getpass.getpass("请输入密码: ")
        if not password:
            parser.error("密码不能为空")
        if password != getpass.getpass("请再次输入密码: "):
            parser.error("两次输入的密码不一致")
    else:
        password = args.password
        if not password:
            parser.error("密码不能为空")

    path = Path(args.output)
    password_hash = create_password_hash(password, args.iterations)
    records = upsert(load_records(path), args.username, password_hash)
    write_atomic(path, records)
    print(f"已写入 {path}（用户 {args.username}，共 {len(records)} 条记录）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
