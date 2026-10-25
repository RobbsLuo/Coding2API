"""各渠道流式客户端超时常量回归测试。

写超时覆盖的是上传请求体阶段（大上下文可达数 MB），不是首字等待；
首字等待由 read=None + stream_first_event_timeout_seconds 负责。
"""

from __future__ import annotations

import importlib

import httpx

# 超时常量只有一份（proxy.py），所有渠道经 LazyClients 共用
MODULE = "src.provider.proxy"


def test_stream_timeout_allows_slow_body_upload() -> None:
    module = importlib.import_module(MODULE)
    timeout = module.STREAM_TIMEOUT

    # 上传数 MB 请求体在代理链路上需要足够余量
    assert timeout.write == 60.0
    assert timeout.connect == 10.0
    # 首字等待不设上限，由业务层 stream_first_event_timeout_seconds 控制
    assert timeout.read is None


def test_short_timeout_unchanged() -> None:
    module = importlib.import_module(MODULE)

    # 非流式短请求保持 30s，防止顺手改坏
    expected = httpx.Timeout(30.0)
    assert expected == module.SHORT_TIMEOUT
