"""角色判定（B5）：三角色由 DB 的 users.role 决定，env 只在引导期参与。

角色从 DB **每请求现读**，不进签名 Cookie——改角色/禁用必须立刻生效，
否则降级一个 admin 后其旧 Cookie 在过期前仍是 admin（权限撤销漏洞）。

`Principal.is_admin` 保持同名同位置（第 2 个字段）：全库 32 处
`Depends(principal_from_request)` 与前端 `is_admin` 都不用改；三角色下
它退化为 `role == "admin"` 的派生属性。
"""

from __future__ import annotations

from dataclasses import dataclass

# 角色枚举：DB 里存的就是这三个字面量。
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)


@dataclass(frozen=True)
class Principal:
    username: str
    is_admin: bool = False
    # 三角色（B5）。默认空串 = 老构造（`Principal("u", True)`）不传角色；
    # 此时 is_admin 仍由显式字段决定，保证既有测试与调用点语义不变。
    role: str = ""

    @property
    def is_operator(self) -> bool:
        """凭证写操作的门槛：admin 与 operator 都算，viewer 不算。"""
        return self.is_admin or self.role == ROLE_OPERATOR


class ForbiddenError(Exception):
    """权限不足（非 admin 写配置/管用户，或非 operator 写凭证）。"""


class UnauthorizedError(Exception):
    """未认证。"""


class LastAdminError(Exception):
    """会移除最后一个活跃 admin 的操作（降级/禁用/删除）。"""


class SelfTargetError(Exception):
    """admin 对自己做会自锁的操作（降级/禁用）。"""


class PasswordChangeRequiredError(Exception):
    """首登未改密：除放行清单外的端点一律拒绝（B5）。

    定义在 rbac 而不是 api.deps：webapp.handlers 需要捕获它，而 deps 依赖
    handlers 的上层链路，放 rbac 可避免循环导入。
    """


def require_admin(principal: Principal) -> Principal:
    if not principal.is_admin:
        raise ForbiddenError("admin only")
    return principal


def require_operator(principal: Principal) -> Principal:
    """凭证写操作门槛（admin 或 operator）。"""
    if not principal.is_operator:
        raise ForbiddenError("operator only")
    return principal
