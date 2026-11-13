"""平台范围门禁：非 POSIX 平台给一句可读的中文，而不是 `ModuleNotFoundError`。"""

from __future__ import annotations

import pytest

from src import platform_support
from src.config import Settings
from tests.helpers.constants import SECRET


@pytest.mark.parametrize("platform", ["linux", "linux2", "darwin"])
def test_supported_platforms(platform):
    assert platform_support.is_supported_platform(platform) is True
    platform_support.require_supported_platform(platform)   # 不抛即通过


@pytest.mark.parametrize("platform", ["win32", "cygwin", "freebsd13", ""])
def test_unsupported_platforms_exit_with_actionable_message(platform, capsys):
    assert platform_support.is_supported_platform(platform) is False
    with pytest.raises(SystemExit) as excinfo:
        platform_support.require_supported_platform(platform)
    text = str(excinfo.value.code)
    # 必须说清「原因 + 替代路径 + 去哪看文档」，而不是只回一个模块名
    assert "仅 Linux / macOS" in text
    assert "WSL2" in text or "Docker" in text
    assert "README" in text


def test_default_uses_real_platform(monkeypatch):
    """不传参数时读真实 `sys.platform`（部署路径走这一支）。"""
    monkeypatch.setattr(platform_support.sys, "platform", "win32")
    assert platform_support.is_supported_platform() is False
    monkeypatch.setattr(platform_support.sys, "platform", "linux")
    assert platform_support.is_supported_platform() is True


def test_build_app_refuses_on_unsupported_platform(monkeypatch, tmp_path):
    """`uvicorn src.main:build_app --factory` 不经过 run()，门禁必须在 build_app 里。

    否则 Windows 上会绕过单实例锁直接起服务（issue #46 的「保护静默失效」）。
    """
    import sys

    from src.main import build_app

    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SystemExit) as excinfo:
        build_app(Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path)))
    assert "仅 Linux / macOS" in str(excinfo.value.code)
