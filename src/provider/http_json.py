"""上游短请求的统一错误路径：非 2xx → 有界读 body 上抛；2xx → 解析 JSON。

十个渠道客户端里反复出现同一段样板（以 generic 为例）：

```python
if response.status_code >= 400:
    raise UpstreamHTTPError(response.status_code, await base.read_body_bounded(response))
try:
    data = response.json()
except ValueError as error:
    raise UpstreamProtocolViolation("non-JSON models response") from error
```

本模块把它收成一处，让三条纪律只写一份：

* 错误体走 `read_body_bounded`（上限就是 `BOUNDED_BODY_LIMIT`，不另设）；
* 非 JSON 保留 `from error` 异常链，`public` 原样透传；
* 成功路径的读取口径由 `limit` 决定——**默认与 `response.json()` 等价**
  （不设上限），只有本来就走 `read_json_bounded` 的调用点传 `limit`
  （lingxi / trae），以免去重顺手改掉「大 body 允不允许过」的既有行为。

`label` 是诊断标签（端点 URL / 路径 / 资源名），进异常文本；`message` 用来
保留各渠道**历史文案**——异常文本会进服务端日志、`public_message` 会回显
客户端，统一文案等于改可观测契约，故不做归一。
"""

from __future__ import annotations

import json
from typing import Any

from . import base


async def json_or_raise(
    response: Any,
    error_cls: type[base.UpstreamHTTPError],
    label: str,
    *,
    message: str | None = None,
    public: str | None = None,
    with_retry_hints: bool = False,
    violation_cls: type[base.UpstreamProtocolViolation] = base.UpstreamProtocolViolation,
    limit: int | None = None,
) -> Any:
    """非 2xx 抛 `error_cls`；2xx 返回解析后的 JSON。

    与逐点手写的样板等价：

    * 非 2xx → `read_body_bounded` 取前缀后 `raise error_cls(status, body)`；
      仅原本解析了 `Retry-After` 的调用点置 `with_retry_hints=True`（不传时
      `retry_at` / `retry_after_seconds` 保持 `None`，行为不变）；
    * 2xx 但非 JSON → `violation_cls(message, public=public)`，`message` 缺省为
      `non-JSON response from {label}`，并保留 `from error`。渠道有自己子类时
      （如 raccoon）由 `violation_cls` 指定，保证调用点 `except` 的捕获口径不变；
    * `limit=None`（默认）→ 与 `response.json()` 逐字节一致（不设上限）；
      `limit=N` → 有界读取，超过 N 字节按协议违规早失败（`public` 固定为
      `upstream returned an oversized response`，即 `base.read_json_bounded` 的文案）。

    返回值是解析后的原样结果（可能是数组/标量）：形状校验仍由调用点自己做，
    本函数不替各渠道决定「必须是什么形状」。
    """
    if response.status_code >= 400:
        body = await base.read_body_bounded(response)
        hints = base.retry_hints(response.headers) if with_retry_hints else {}
        raise error_cls(response.status_code, body, **hints)
    if limit is None:
        try:
            return response.json()
        except ValueError as error:
            raise violation_cls(_non_json_message(label, message), public=public) from error
    # 多读 1 字节用于判「是否超限」：刚好等于上限的完整 body 不该被误杀。
    raw = await base.read_body_bounded(response, limit + 1)
    if len(raw) > limit:
        raise violation_cls(
            f"response body from {label} exceeds {limit} bytes",
            public="upstream returned an oversized response")
    try:
        return json.loads(raw)
    except ValueError as error:
        raise violation_cls(_non_json_message(label, message), public=public) from error


def _non_json_message(label: str, message: str | None) -> str:
    """非 JSON 的诊断文案：调用点给了历史文案就用它，否则按 label 拼。"""
    return f"non-JSON response from {label}" if message is None else message
