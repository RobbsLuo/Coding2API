"""角色判定：ADMIN_USERNAMES env 决定 admin（Q18=A）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    username: str
    is_admin: bool


class ForbiddenError(Exception):
    """非 admin 尝试写操作。"""


class UnauthorizedError(Exception):
    """未认证。"""


def require_admin(principal: Principal) -> Principal:
    if not principal.is_admin:
        raise ForbiddenError("admin only")
    return principal
