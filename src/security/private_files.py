"""私有文件/目录的权限收口：POSIX 走 chmod，Windows 走 ACL。

**为什么需要这一层**（issue #45）：`os.chmod` 在 Windows 上只影响只读位，
不改 DACL。于是 `SECURITY.md` 承诺的「`data/` 权限收到 `0700`」在 Windows 上
既不报错也不生效：实测 `DATA_DIR` 与 `.env` 都继承了上级目录的
`Everyone:(I)(F)`，同机任意低权限进程都能同时读到凭证密文与 `APP_SECRET`
（按 `SECURITY.md` 自己的口径，这等价于明文）。`/healthz` 与启动日志全程正常，
部署方没有任何信号。

因此收紧动作统一从这里走：

- POSIX：`chmod`，再**读回校验** `mode & 0o077 == 0`；
- Windows：`icacls /inheritance:r` 替换成「仅当前用户 + SYSTEM」的受保护
  DACL，再**读回校验**没有继承 ACE、没有多余主体；
- 任何一步失败都 `logger.warning` 一行，绝不静默（是否阻断启动由调用方定）。

只读校验口径与 `doctor` 共用：`check_private()` 只读文件系统/ACL，不写任何东西。
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess  # noqa: S404 - 只调用系统自带的 icacls，参数逐项传入不拼 shell
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

#: Windows ACL 里允许出现的主体（小写比较）。除当前用户外只放行：
#: SYSTEM（S-1-5-18）与 Administrators（S-1-5-32-544）——与 `private-files.mjs`
#: 的 `privateAcl` 同口径：出现其他身份即判权限过宽。
WINDOWS_ALLOWED_PRINCIPALS = frozenset({
    "system",
    "nt authority\\system",
    "administrators",
    "builtin\\administrators",
})


class PrivateAccessError(RuntimeError):
    """权限无法收紧或校验不通过（调用方决定是告警还是中止）。"""


def _is_windows() -> bool:
    """是否 Windows。抽成函数便于测试 monkeypatch（CI 跑在 Linux）。"""
    return sys.platform == "win32"


def _current_user() -> str:
    """Windows 当前用户名（`icacls` 的授权主体写法）。"""
    return os.environ.get("USERNAME", "").strip()


def _run_icacls(args: list[str]) -> subprocess.CompletedProcess[str]:
    """执行 `icacls` 并返回结果；非零退出换算成 `PrivateAccessError`。"""
    try:
        return subprocess.run(  # noqa: S603 - 参数为常量 + 用户自己的路径
            ["icacls", *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as error:
        raise PrivateAccessError("找不到 icacls（Windows 系统自带工具）") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip() or f"退出码 {error.returncode}"
        raise PrivateAccessError(f"icacls 执行失败: {detail}") from error


def _apply_posix(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def _apply_windows(path: Path, *, directory: bool) -> None:
    """把 DACL 替换成「当前用户 + SYSTEM」并切断继承。

    目录带 `(OI)(CI)`：新建的子项继续只对这两个主体开放；文件不需要继承标志。
    """
    user = _current_user()
    if not user:
        raise PrivateAccessError("无法确定当前 Windows 用户名，不能收紧 ACL")
    suffix = ":(OI)(CI)F" if directory else ":F"
    _run_icacls([
        str(path),
        "/inheritance:r",
        "/grant:r", f"{user}{suffix}",
        "/grant:r", f"SYSTEM{suffix}",
    ])


def _verify_posix(path: Path, mode: int) -> str | None:
    """读回 POSIX 权限；组/其他有任何权限位即返回问题描述。"""
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual & 0o077:
        return f"权限 {actual:04o} 对组/其他用户开放（期望 {mode:04o}）"
    return None


#: `icacls` 的 ACE 分隔符：`<路径> <主体>:(权限标记…)`。路径可能含空格与
#: 盘符冒号（`C:\data`），故取**最后一个** `:(` 作为分隔点。
_ACL_SEPARATOR = ":("


def acl_violations(text: str, user: str, path: str | Path | None = None) -> list[str]:
    """从 `icacls` 输出里找出违反「仅自己 + SYSTEM」口径的条目。

    `icacls` 的首条 ACE 行形如 `<路径> <主体>:(权限标记…)`，之后的行只有主体。
    传入 `path` 时先剥掉行首路径，主体才能与白名单正确比对（路径自身可能带
    空格与中文）。

    解析口径刻意宽松：只认「`主体:(权限)`」这一形态（含 `(I)` 继承标记），
    忽略本地化差异较大的汇总行与空行。出现继承标记、或主体不在白名单，
    都算违规——宁可多报一条，也不放过一条 `Everyone:(F)`。

    主体形如 `DOMAIN\\user` 时同时接受裸 `user`：`icacls` 实际输出带域/机器名
    前缀，只比完整串会把「就是当前用户」误判成多余主体。
    """
    me = user.strip().lower()
    allowed = WINDOWS_ALLOWED_PRINCIPALS | ({me} if me else set())
    prefix = str(path).lower() if path is not None else ""
    problems: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if prefix and line.lower().startswith(prefix):
            line = line[len(prefix):].strip()
        index = line.rfind(_ACL_SEPARATOR)
        if index < 0:
            continue
        principal = line[:index].strip().lower()
        flags = line[index:]
        if not principal:  # 形如 `:(F)` 的残行：没有主体，不作为 ACE 参与判定
            continue
        if "(i)" in flags.lower():
            problems.append(f"仍存在继承 ACE: {raw.strip()}")
        elif principal not in allowed and principal.rsplit("\\", 1)[-1] != me:
            problems.append(f"多余主体 {principal}: {raw.strip()}")
    return problems


def _has_ace(text: str) -> bool:
    """输出里是否至少有一条可解析的 ACE（否则 icacls 可能只回了报错/汇总行）。"""
    return any(_ACL_SEPARATOR in line for line in text.splitlines())


def _verify_windows(path: Path) -> str | None:
    """读回 Windows ACL；有问题返回描述，正常返回 None。"""
    try:
        result = _run_icacls([str(path)])
    except PrivateAccessError as error:
        return str(error)
    problems = acl_violations(result.stdout, _current_user(), path)
    if problems:
        return "; ".join(problems)
    if not _has_ace(result.stdout):
        # 解析不到任何 ACE（含 icacls 报错文案）：无法确认边界，按不通过处理
        return "icacls 输出里没有可解析的 ACE，无法确认权限"
    return None


def check_private(path: str | Path, *, directory: bool) -> str | None:
    """只读校验（doctor 与启动自检共用）；返回问题描述，合规返回 None。

    路径不存在时返回问题描述而不是抛错：调用方按「还没创建」自行降级。
    """
    target = Path(path)
    try:
        if not target.exists():
            return f"路径不存在: {target}"
        if _is_windows():
            return _verify_windows(target)
        return _verify_posix(target, 0o700 if directory else 0o600)
    except OSError as error:
        return f"无法读取权限: {error}"


def _secure(path: Path, *, mode: int, directory: bool, strict: bool) -> bool:
    """收紧权限并读回校验；失败记 warning，`strict=True` 时再抛 `PrivateAccessError`。

    是否阻断由调用方决定：`data/` 的信任边界是启动前必须成立的**确定**前提
    （issue #45 的口径），传 `strict=True`；备份/dump 这类旁路产物收紧失败
    不该让主流程失败，用默认的 `strict=False`。

    **先收紧再校验，而不是「收紧报错就判失败」**：共享挂载上 `chmod` 可能被拒，
    但目录本身若已经是 `0700`（属主设好的），信任边界仍然成立，不该拦启动。
    """
    failure: str | None = None
    try:
        if _is_windows():
            _apply_windows(path, directory=directory)
        else:
            _apply_posix(path, mode)
    except (OSError, PrivateAccessError) as error:
        failure = str(error)
    try:
        problem = _verify_windows(path) if _is_windows() else _verify_posix(path, mode)
    except OSError as error:  # 校验都读不了（路径刚被删、权限不足）
        problem = f"无法读取权限: {error}"
    if problem is None:
        return True
    detail = problem if failure is None else f"{problem}（收紧失败: {failure}）"
    message = f"无法收紧 {path} 的权限（当前平台/属主不允许），请手工核对文件权限：{detail}"
    if strict:
        raise PrivateAccessError(message)
    logger.warning("%s", message)
    return False


def secure_dir(path: str | Path, *, mode: int = 0o700, strict: bool = False) -> bool:
    """把目录收成「仅属主可访问」；返回是否确认收紧成功。"""
    return _secure(Path(path), mode=mode, directory=True, strict=strict)


def secure_file(path: str | Path, *, mode: int = 0o600, strict: bool = False) -> bool:
    """把文件收成「仅属主可读写」；返回是否确认收紧成功。"""
    return _secure(Path(path), mode=mode, directory=False, strict=strict)
