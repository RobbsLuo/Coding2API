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

from src.auth.users import (  # noqa: E402
    FORBIDDEN_USERNAME_CHARS,
    PBKDF2_ITERATIONS,
    create_password_hash,
)

DEFAULT_OUTPUT = "secrets/users.txt"

# 用户名会写进 `用户名:hash` 行：冒号会截断字段、换行/回车会注入额外记录，
# 控制字符一律拒绝。规则本体在 `src/auth/users.py`（与 CLI 建号脚本同源）。
_FORBIDDEN_USERNAME_CHARS = FORBIDDEN_USERNAME_CHARS


def read_password(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """取密码：`--password-stdin` 优先，其次 `--password`，否则交互输入。

    `--password` 会让密码进 argv（`ps` / shell 历史可见），因此提供
    `--password-stdin` 作为非交互场景的推荐方式（CI / 部署脚本）。
    """
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            parser.error("从 stdin 读到空密码")
        return password
    if args.password is not None:
        if not args.password:
            parser.error("密码不能为空")
        return args.password
    password = getpass.getpass("请输入密码: ")
    if not password:
        parser.error("密码不能为空")
    if password != getpass.getpass("请再次输入密码: "):
        parser.error("两次输入的密码不一致")
    return password


def validate_username(parser: argparse.ArgumentParser, username: str) -> str:
    """校验用户名：非空、无首尾空白、不含 `:` 或控制字符。"""
    if not username.strip():
        parser.error("用户名不能为空")
    if username != username.strip():
        parser.error("用户名不能包含首尾空白")
    for char in _FORBIDDEN_USERNAME_CHARS:
        if char in username:
            parser.error(f"用户名不能包含 {char!r}")
    return username


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
    parser.add_argument("--password", help="非交互模式使用（会进 argv，优先用 --password-stdin）")
    parser.add_argument("--password-stdin", action="store_true",
                        help="从 stdin 读一行作为密码（避免进 argv / shell 历史）")
    args = parser.parse_args(argv)

    username = validate_username(parser, args.username)
    password = read_password(parser, args)

    path = Path(args.output)
    password_hash = create_password_hash(password, args.iterations)
    records = upsert(load_records(path), username, password_hash)
    write_atomic(path, records)
    print(f"已写入 {path}（用户 {username}，共 {len(records)} 条记录）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
