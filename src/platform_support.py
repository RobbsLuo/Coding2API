"""平台支持范围（显式声明，而不是靠 `ModuleNotFoundError: No module named 'fcntl'`）。

**为什么需要**（issue #46）：单实例锁依赖 `flock`，是 POSIX 专有原语。v0.13.1 起
`src/instance_lock.py` 无条件 `import fcntl`，而 `src/main.py` 在模块级 import 它，
于是 Windows 上启动直接在 import 阶段炸掉，日志里连一行上下文都没有
（`build_app --factory` 被 `ModuleNotFoundError` 打断）；CI 四个 job 全是
ubuntu-latest，也没有任何文档声明平台范围。

这里把「支持哪些平台」变成一处显式常量与一条可读的退出信息，入口在 import
`instance_lock` **之前**调用 `require_supported_platform()`。
"""

from __future__ import annotations

import sys

#: 支持的系统（`sys.platform` 前缀）。Linux 与 macOS 均提供 `flock`。
SUPPORTED_PLATFORMS = ("linux", "darwin")


def is_supported_platform(platform: str | None = None) -> bool:
    """当前（或传入的）`sys.platform` 是否在支持范围内。"""
    name = sys.platform if platform is None else platform
    return name.split("-", 1)[0].startswith(SUPPORTED_PLATFORMS)


def unsupported_platform_message(platform: str | None = None) -> str:
    """非支持平台的退出文案：说清原因、替代路径与文档位置。"""
    name = sys.platform if platform is None else platform
    return (
        f"本平台暂不支持：{name}。单实例锁依赖 flock（仅 Linux / macOS），"
        f"缺失时会让同一 DATA_DIR 的双写保护静默失效。"
        f"Windows 请用 WSL2 或 Docker 部署（见 README「部署注意」）。"
    )


def require_supported_platform(platform: str | None = None) -> None:
    """非支持平台直接 `SystemExit`；支持平台静默返回。

    用 `SystemExit` 而不是 `ImportError`：这是**环境不支持**，不是程序缺陷，
    运维需要看到一句能照做的中文，而不是一段来自第三方模块的 traceback。
    """
    if not is_supported_platform(platform):
        raise SystemExit(unsupported_platform_message(platform))
