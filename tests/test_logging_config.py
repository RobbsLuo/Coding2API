"""日志配置测试。

来源：实测发现应用 logger 的 INFO 被静默丢弃——uvicorn 默认 LOGGING_CONFIG
只配置 `uvicorn` / `uvicorn.access`（propagate=false），**从不配置 root**，
而 root 默认 level=WARNING 且无 handler。后果是 PROPOSAL §8 要求的凭证审计
日志（增删改/pin/账号切换，共 7 处 logger.info）一条都没落盘。
"""

from __future__ import annotations

import logging

import pytest

from src.config import Settings
from src.main import build_app
from src.webapp.logging import DATE_FORMAT, LOG_FORMAT, _RootHandler, configure_logging
from tests.conftest import SECRET


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """每个用例前后把 root logger 复位，避免污染其他测试的日志断言。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def _root_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, _RootHandler)]


def test_configure_logging_enables_info_level():
    """配置后 root 必须是 INFO（此前是 WARNING，INFO 全被丢弃）。"""
    logging.getLogger().handlers = []
    configure_logging("INFO")
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_is_idempotent():
    """build_app 被测试反复调用：不能每次叠加 handler（否则日志重复 N 遍）。"""
    logging.getLogger().handlers = []
    configure_logging("INFO")
    configure_logging("INFO")
    configure_logging("DEBUG")
    assert len(_root_handlers()) == 1
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_normalizes_level_and_handles_blank():
    """level 大小写不敏感；空值退回 INFO（env 写错不应让日志消失）。"""
    logging.getLogger().handlers = []
    configure_logging("debug")
    assert logging.getLogger().level == logging.DEBUG
    configure_logging("")
    assert logging.getLogger().level == logging.INFO


def test_handler_has_timestamp_format():
    """launchd 重定向的文件没有外部时间戳，格式必须自立。"""
    logging.getLogger().handlers = []
    configure_logging("INFO")
    record = logging.LogRecord("src.api.admin_credentials", logging.INFO, __file__, 1,
                               "管理员 %s 新增凭证 %s", ("root", "cred_1"), None)
    text = _root_handlers()[0].formatter.format(record)
    assert "管理员 root 新增凭证 cred_1" in text
    assert "src.api.admin_credentials" in text
    assert "INFO" in text
    # 时间戳存在（不依赖具体时刻）
    assert text[:4].isdigit()
    assert LOG_FORMAT and DATE_FORMAT


def test_audit_logger_reaches_handler_after_build_app():
    """回归审计黑洞：build_app 之后应用 logger 的 INFO 必须真的写到 handler。

    不断言 caplog：caplog 靠往 root 加 handler 捕获，不能证明**我们的**
    handler 收到了记录（不配 root 时 caplog 也可能“通过”）。这里直接看
    真实 handler 的 emit。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR="/tmp/unused",
                        ADMIN_USERNAMES="root")
    logging.getLogger().handlers = []
    build_app(settings)

    handler = _root_handlers()[0]
    captured: list[str] = []
    handler.emit = lambda record: captured.append(handler.format(record))   # type: ignore[method-assign]
    logger = logging.getLogger("src.api.admin_credentials")   # 审计日志用的 logger
    logger.info("管理员 root 删除凭证 cred_9")
    assert any("管理员 root 删除凭证 cred_9" in line for line in captured)
    assert logger.getEffectiveLevel() == logging.INFO


def test_root_handler_streams_to_stderr():
    """应用只写 stderr，不自己开文件——文件与轮转交给平台（见模块 docstring）。"""
    import sys

    logging.getLogger().handlers = []
    configure_logging("INFO")
    assert _root_handlers()[0].stream is sys.stderr


def test_uvicorn_does_not_configure_root():
    """uvicorn 只配自己的 logger，不进 root——所以应用必须自己配（否则黑洞）。"""
    from uvicorn.config import LOGGING_CONFIG

    assert "root" not in LOGGING_CONFIG["loggers"]
    assert set(LOGGING_CONFIG["loggers"]) >= {"uvicorn", "uvicorn.access"}
    # 未经过 configure_logging 时 root 是空的且级别为 WARNING（黑洞成因）
    root = logging.getLogger()
    root.handlers = []
    root.setLevel(logging.WARNING)
    assert root.handlers == [] and root.level == logging.WARNING


def test_noisy_libraries_are_downgraded():
    """httpx/httpcore 的 INFO 会给每个上游请求打一条含完整 URL 的日志。

    既是噪音（量随请求数线性增长、把审计日志淡出视野），也与
    PROPOSAL §8「日志脱敏」冲突（URL 可能带 query 参数）。
    """
    logging.getLogger().handlers = []
    configure_logging("INFO")
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING


def test_noisy_libraries_reopened_at_debug():
    """排查上游问题时要能看到请求详情，所以不是永久静音。"""
    logging.getLogger().handlers = []
    configure_logging("DEBUG")
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.INFO


def test_audit_logger_not_downgraded():
    """降噪只针对第三方库，应用自己的 logger 必须保持 INFO。"""
    logging.getLogger().handlers = []
    configure_logging("INFO")
    assert logging.getLogger("src.api.admin_credentials").getEffectiveLevel() == logging.INFO
