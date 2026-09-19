"""日志配置：应用只写 stderr，轮转交给平台。

**为什么应用不自己写文件、不自己轮转**
三种部署形态的日志采集方式完全不同，且都靠「进程的 stdout/stderr」这个
统一接口对接：

- macOS launchd：`StandardOutPath` / `StandardErrorPath` 重定向到文件，
  轮转交给系统自带的 `newsyslog`（规则见 `deploy/newsyslog/`）
- Linux systemd：journald 接管并自带轮转（`deploy/systemd/`）
- Docker：json-file 驱动捕获，按 `logging.options` 限制大小
  （`docker-compose.yml` 已配，三平台统一）

应用自己写文件会把日志切成两份、与平台轮转争抢同一个文件，而且容器里
写进镜像层重启即丢、`docker logs` 也看不到。所以这里只挂一个 stderr
handler，文件与轮转全交给平台。

**为什么必须显式配置 root**
uvicorn 的默认 `LOGGING_CONFIG` 只配置 `uvicorn` 与 `uvicorn.access`
两个 logger（且 `propagate=false`），**从不配置 root**，而 root 默认
`level=WARNING` 且无 handler。后果是应用里 `logging.getLogger(__name__)`
的 INFO 被静默丢弃——`PROPOSAL §8` 要求的凭证审计日志（增删改/pin/
账号切换）首当其冲，一条都不会落盘。
"""

from __future__ import annotations

import logging
import sys

# 带时间戳：launchd/newsyslog 重定向的文件里只有进程自己写的内容，
# 没有 journald/docker 那种外部时间戳，格式必须自立
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 第三方库降噪：root 升到 INFO 后 httpx/httpcore 会给每个上游请求打一条
# INFO，带完整 URL（可能含 query 参数，与 PROPOSAL §8「日志脱敏」冲突），
# 且量级与请求数线性增长、把审计日志淡出视野。只保留它们的 WARNING+。
_NOISY_LOGGERS = ("httpx", "httpcore")


class _RootHandler(logging.StreamHandler):
    """标记类：用于识别本模块已配置过的 handler，保证 configure_logging 幂等。

    build_app 会被测试反复调用（每个用例一次），重复 addHandler 会让每条
    日志打印多遍，因此必须有可靠的「已配置」判定。
    """


def configure_logging(level: str = "INFO") -> None:
    """配置 root logger：应用日志走 stderr（平台采集/轮转）。

    幂等：重复调用只更新级别，不重复添加 handler。
    uvicorn 的 logger 不在此处配置——它自带的配置已经够用，且
    `disable_existing_loggers=false` 不会覆盖这里设置的 root。
    """
    normalized = (level or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(normalized)

    handler = next((h for h in root.handlers if isinstance(h, _RootHandler)), None)
    if handler is None:
        handler = _RootHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        root.addHandler(handler)
    handler.setLevel(normalized)

    # 降噪而非禁用：默认（INFO 及以上）把这两个库压到 WARNING；
    # root 调到 DEBUG 时放开到 INFO，排查上游问题需要看请求详情
    noisy_level = logging.INFO if root.level <= logging.DEBUG else logging.WARNING
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)
