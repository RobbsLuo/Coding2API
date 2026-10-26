"""启动引导：把身份从 users.txt + env 交接给 SQLite。

四步，顺序有依赖：

1. 库为空 → 从 users.txt 导入（一律 viewer，不猜角色）。
2. 库里有**活跃 admin** 时跳过 env 提权；没有活跃 admin（引导期）才按
   `ADMIN_USERNAMES` 把命中者提权为 admin（幂等，已是 admin 不动）。
3. 库为空 → 报错。消息必须同时给出「放回 users.txt 重启」与「跑 CLI 建号」
   两条恢复路径——用户这时候已经起不来了，只报「文件不存在」等于没说怎么办。
4. 无活跃 admin → 兑回 env 指名者；仍无 → 报错。

第 4 步是防锁死的最后一层：只要有 active admin 就能在界面里处理其余情况，
所以启动不拦「admin 被降级」这类操作，只兜住「一个都没有」的死局。

**第 2 步为什么只在引导期提权**：角色真源是 DB，不是 env。每次启动都按
`ADMIN_USERNAMES` 改写角色，等于让管理台的降级在下一次重启后被撤销
（「角色不进 Cookie，降级立即生效」只对当次会话有效，重启即失效）。
判定用「有没有活跃 admin」而不是「库里有没有用户」：库里有用户但全是
viewer/operator 时仍是引导期（老部署从 users.txt 升级的中间态），此时
env 提权是唯一的出路；库里有活跃 admin 后 env 就不再插手。

写入幂等性来自 `upsert_imported`（已存在不覆盖）与 `update_role`（同值无害），
因此反复重启不会把在管理台改过的密码/角色冲回文件值。

第 4 步刻意**不**自动启用被禁用的账号：那等于绕过一次有意的禁用。若唯一候选
被禁用，就让它去走 `_RECOVERY_HINT` 里的恢复路径。
"""

from __future__ import annotations

from .rbac import ROLE_ADMIN
from .users import UsersFileError

# 恢复指引：两条路都写清楚，避免用户只知道「启动失败了」。
_RECOVERY_HINT = (
    "请任选其一：(1) 把有效的 users.txt 放回 USERS_FILE 指向的位置后重启；"
    "(2) 运行 `python -m scripts.create_user <用户名> --role admin` 直接建号。"
)


def bootstrap_users(config, user_repo, *, file_store=None, log=None) -> None:
    """见模块 docstring。`file_store` 为 None 时不尝试导入（纯 DB 部署）。

    导入来源为 None 表示这台机没有 users.txt，此时库必须有用户，否则启动失败。
    """
    imported = 0
    if file_store is not None:
        imported = file_store.import_into(user_repo)

    # 2. env 指名的引导期 admin
    #
    # **只在引导期提权**：库里有活跃 admin 时，env 不再改写任何人的角色。
    # 之前每次启动都无条件提权，等于让 ADMIN_USERNAMES 成为角色真源——
    # 管理台刚降级的用户重启后又变回 admin，「降级立即生效」被重启撤销。
    # 引导期 = 库里还没有活跃 admin（首次部署、老部署从 users.txt 升级）。
    admin_names = getattr(config, "admin_set", frozenset())
    promoted: list[str] = []
    if user_repo.count_active_admins() > 0:
        # 已有活跃 admin：角色只来自 DB，env 提权全部跳过（含被降级的）。
        if log is not None and admin_names:
            log("账号引导：已有活跃 admin，跳过 ADMIN_USERNAMES 提权（角色只来自 DB）")
    else:
        for username in sorted(admin_names):
            row = user_repo.get(username)
            if row is None:
                # env 指了个库里没有的用户：不报错（可能只是残留配置），但也不算
                # 「有 admin」——第 4 步会兜住真正无 admin 的情况。
                continue
            if row["role"] != ROLE_ADMIN:
                user_repo.update_role(username, ROLE_ADMIN, bump_epoch=False)
                promoted.append(username)

    if log is not None:
        if imported:
            log("账号引导：从用户文件导入 %d 个用户", imported)
        if promoted:
            log("账号引导：按 ADMIN_USERNAMES 提权为 admin：%s", ", ".join(promoted))

    # 3. 库必须非空
    if not user_repo.list_usernames():
        raise UsersFileError(f"no authentication users available; {_RECOVERY_HINT}")

    # 4. 必须至少一个活跃 admin。
    #
    # 第 2 步已在「无活跃 admin」时把 env 指名的**全部**用户提为 admin
    # （含被禁用的那些），所以走到这里「活跃 admin 为 0」只可能是它们全被
    # 禁用——此时 env 帮不上忙，只能走恢复路径。刻意不自动启用：那会绕过一次
    # 有意的禁用。
    if user_repo.count_active_admins() == 0:
        raise UsersFileError(f"no active admin account; {_RECOVERY_HINT}")
