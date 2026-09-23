#!/usr/bin/env python3
"""直接对 SQLite 建/删管理台用户（无 Web 场景与恢复路径）。

B5 起用户数据的唯一事实来源是 SQLite 的 `users` 表（`secrets/users.txt`
只在启动时做一次引导导入）。管理台已覆盖日常增删改，本脚本的存在是为了：

1. **首个 admin**：新库没有任何账号，能进管理台之前得先有一个人。
2. **恢复**：管理员全部被误操作停用、或忘记密码又无人可重置时。
3. **硬删**：管理台**故意不提供** DELETE（硬删会在 usage_events 里留下
   查不到用户名的孤儿统计），只在这里提供，且要求 `--force`。

用法：
    python3 scripts/create_user.py alice --role admin          # 交互输入密码
    python3 scripts/create_user.py alice --role viewer --password pw
    python3 scripts/create_user.py --list
    python3 scripts/create_user.py alice --delete --force      # 硬删（不可逆）
    python3 scripts/create_user.py alice --db data/x.sqlite3   # 指定库
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audit.actions import ACTION_USER_CREATE, ACTION_USER_DELETE  # noqa: E402
from src.auth.rbac import ROLE_ADMIN, ROLES  # noqa: E402
from src.auth.users import create_password_hash  # noqa: E402
from src.db.conn import Database  # noqa: E402
from src.db.migrate import apply_schema  # noqa: E402
from src.db.repo import AuditRepository, UserRepository  # noqa: E402


def resolve_db_path(explicit: str | None) -> Path:
    """--db 优先；否则走 Settings.db_path（即 DATA_DIR/coding2api.sqlite3）。"""
    if explicit:
        return Path(explicit)
    from src.config import load_settings

    return Path(load_settings().db_path)


def open_repos(db_path: Path) -> tuple[UserRepository, AuditRepository]:
    database = Database(db_path)
    apply_schema(database.connect())
    return UserRepository(database), AuditRepository(database)


def list_users(users: UserRepository) -> int:
    rows = users.list_all()
    if not rows:
        print("（无用户）")
        return 0
    print(f"{'用户名':<20} {'角色':<10} {'状态':<8} 创建者")
    for row in rows:
        state = "启用" if row["enabled"] else "已禁用"
        print(f"{row['username']:<20} {row['role']:<10} {state:<8} "
              f"{row['created_by'] or '引导导入'}")
    return 0


def create(users: UserRepository, audit: AuditRepository, *, username: str,
           role: str, password: str) -> int:
    if users.get(username) is not None:
        print(f"用户 {username!r} 已存在；改密码请用管理台，或先 --delete --force",
              file=sys.stderr)
        return 1
    users.create(username, create_password_hash(password), role=role, created_by="cli")
    # actor 用 "cli"：审计要能区分「谁做的」，无会话时不存在真实操作者
    audit.record(actor="cli", action=ACTION_USER_CREATE, target=username,
                 detail=f"角色 {role}")
    print(f"已创建用户 {username!r}（角色 {role}）。"
          f"首次登录后请在管理台修改密码。")
    return 0


def delete(users: UserRepository, audit: AuditRepository, *, username: str) -> int:
    row = users.get(username)
    if row is None:
        print(f"用户 {username!r} 不存在", file=sys.stderr)
        return 1
    # 与 Web 端同一道防锁死闸门：删掉最后一个活跃 admin 之后没人能管了
    if row["role"] == ROLE_ADMIN and row["enabled"] and users.count_active_admins() <= 1:
        print("拒绝：这是最后一个活跃管理员，删掉后将无人能管理此系统。\n"
              "请先用 --role admin 建另一个管理员，或改回禁用（管理台可做）。",
              file=sys.stderr)
        return 1
    users.delete(username)
    audit.record(actor="cli", action=ACTION_USER_DELETE, target=username,
                 detail="硬删（CLI）")
    print(f"已硬删用户 {username!r}。注意：统计里该用户的历史记录会变成无主数据"
          f"（用户名查不到账号）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="直接对 SQLite 建/删管理台用户")
    parser.add_argument("username", nargs="?", help="用户名（--list 时可省略）")
    parser.add_argument("--db", help="SQLite 路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--role", default="viewer", choices=sorted(ROLES))
    parser.add_argument("--password", help="非交互模式使用（默认交互输入，不回显）")
    parser.add_argument("--list", action="store_true", help="列出全部用户后退出")
    parser.add_argument("--delete", action="store_true", help="硬删用户（需 --force）")
    parser.add_argument("--force", action="store_true", help="确认硬删不可逆")
    args = parser.parse_args(argv)

    users, audit = open_repos(resolve_db_path(args.db))

    if args.list:
        return list_users(users)

    username = (args.username or "").strip()
    if not username:
        parser.error("缺少用户名（或用 --list）")
    if username != args.username:
        parser.error("用户名不能包含首尾空白")

    if args.delete:
        if not args.force:
            parser.error("硬删不可逆，需同时加 --force")
        return delete(users, audit, username=username)

    if args.password is None:
        password = getpass.getpass("请输入密码: ")
        if password != getpass.getpass("请再次输入密码: "):
            parser.error("两次输入的密码不一致")
    else:
        password = args.password
    if not password:
        parser.error("密码不能为空")

    return create(users, audit, username=username, role=args.role, password=password)


if __name__ == "__main__":
    raise SystemExit(main())
