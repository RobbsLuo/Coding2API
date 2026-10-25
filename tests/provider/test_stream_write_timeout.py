"""各渠道流式客户端超时常量回归测试。

写超时覆盖的是上传请求体阶段（大上下文可达数 MB），不是首字等待；
首字等待由 read=None + stream_first_event_timeout_seconds 负责。
"""

from __future__ import annotations

import importlib

import httpx
import pytest

# 7 个渠道客户端模块，均导出 STREAM_TIMEOUT / SHORT_TIMEOUT（同一份常量，见 proxy.py）
CLIENT_MODULES = [
    "src.provider.codebuddy.client",
    "src.provider.codearts.client",
    "src.provider.kilo.client",
    "src.provider.officeace.client",
    "src.provider.qoder.client",
    "src.provider.trae.client",
    "src.provider.zen.client",
]


@pytest.mark.parametrize("module_name", CLIENT_MODULES)
def test_stream_timeout_allows_slow_body_upload(module_name: str) -> None:
    module = importlib.import_module(module_name)
    timeout = module.STREAM_TIMEOUT

    # 上传数 MB 请求体在代理链路上需要足够余量
    assert timeout.write == 60.0
    assert timeout.connect == 10.0
    # 首字等待不设上限，由业务层 stream_first_event_timeout_seconds 控制
    assert timeout.read is None


@pytest.mark.parametrize("module_name", CLIENT_MODULES)
def test_short_timeout_unchanged(module_name: str) -> None:
    module = importlib.import_module(module_name)

    # 非流式短请求保持 30s，防止顺手改坏
    expected = httpx.Timeout(30.0)
    assert expected == module.SHORT_TIMEOUT
