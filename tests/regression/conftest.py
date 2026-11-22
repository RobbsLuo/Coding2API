"""回归测试目录的共享夹具。

`settings` 与 `client` 在 `test_hardening.py` / `test_hardening_admin.py` 里
各写一份逐字节等价的实现，收敛到这里。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET
from tests.helpers.startup import wait_for_startup


@pytest.fixture()
def settings(tmp_path):
    """带 admin 引导配置的隔离 Settings。"""
    return Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                    ADMIN_USERNAMES="root")


@pytest.fixture()
def client(settings):
    """带 admin 会话 Cookie 的 TestClient。"""
    app = build_app(settings)
    with TestClient(app) as c:
        c.cookies.set("coding2api_session", create_session_token("root", SECRET))
        # 等 runner.start() 的首轮任务收尾：否则 retention 会与用例并发，
        # 把「早已过期时间戳」的数据随机删掉（见 tests/helpers/startup.py）。
        wait_for_startup(app, c)
        yield c
