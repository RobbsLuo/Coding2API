"""CodeBuddy 内置检索工具：web search 与 webfetch。

来源：官方 CLI 2.185.0 解包（`dist/codebuddy-headless.js`）—— 这两个端点由
客户端**真实调用**，不是第三方转述：

    POST {endpoint}/agenttool/v1/search
    POST {endpoint}/agenttool/v1/webfetch

请求头与聊天同一套（`build_headers`）：Bearer + IDE 版本/名称 + 会话与请求 id +
部门信息。搜索超时 20s；抓取默认 30s，只接受文本页。

契约（逐字段取自包内 `buildRequestBody` / `fetchContentViaApi`）：

* 搜索请求 `{query, type:"text2text", max_results:5, allowed_domains?, freshness?}`；
  `blocked_domains` 客户端是拼成 ` -site:xxx` 后塞进 `query` 的，本模块照做。
* 搜索响应 `{provider, results:[{title,url,snippet,site?,favicon?}],
  total_results, response_time_ms}`。
* 抓取请求 `{url, format:"markdown", prompt, timeout:<秒>, max_length:<字符>}`。
* 抓取响应 `{url, content, content_type, context:{postprocess_context}}`。

两个端点都用**私有信封**：HTTP 200 里 `code != 0` 表示业务失败（`msg` 是文案）。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ...provider import base
from .credential import CodeBuddyCredential

EP_SEARCH = "/agenttool/v1/search"
EP_WEBFETCH = "/agenttool/v1/webfetch"

# 与官方客户端一致：搜索 20s；抓取 30s（正文抓取要等目标站点）。
SEARCH_TIMEOUT_SECONDS = 20.0
FETCH_TIMEOUT_SECONDS = 30.0
# 官方 `max_results` 固定 5；MCP 风格客户端常传 10，这里允许到 20（上游仍可能截断）。
MAX_RESULTS_LIMIT = 20
# 抓取正文上限（字符）：官方 `dm` 常量未在包内读到具体值，取 200_000 作为安全上界。
MAX_CONTENT_CHARS = 200_000


class ToolRejected(Exception):
    """上游明确拒绝（信封 `code != 0` 或 HTTP 非 2xx）。

    `status` 是**建议对外状态码**（由业务码映射而来），`message` 已脱敏
    （只带上游给的 `msg`，不经 body 摘要，避免把用户 query 回显给客户端）。
    """

    def __init__(self, status: int, message: str, *, code: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def _classify_code(code: Any) -> int:
    """业务码 → 对外状态码（只映射已确认的几类，其余 502）。

    与聊天错误码同源：`11128` 渠道风控、`11101` 参数坏、`11135` 坏图都是
    **请求级**（400，换号无用）；`14018` 积分耗尽、`1005` 权益耗尽 → 429；
    `11140` 账号被风控 → 403（当前号不可用，换号可试）。
    """
    try:
        value = int(code)
    except (TypeError, ValueError):
        return 502
    if value in (11101, 11128, 11135):
        return 400
    if value in (14018, 1005):
        return 429
    if value == 11140:
        return 403
    return 502


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """解私有信封：`code == 0` 时返回 `data`（无 data 时返回原对象）。"""
    code = payload.get("code")
    if code in (0, None):
        data = payload.get("data")
        return data if isinstance(data, dict) else payload
    message = payload.get("msg") or payload.get("message") or "upstream rejected"
    raise ToolRejected(_classify_code(code), str(message), code=code)


class CodeBuddyTools:
    """检索工具客户端（挂在 `CodeBuddyClient` 上，共用连接池与头族）。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def _headers(self, credential: CodeBuddyCredential,
                 model: str | None) -> dict[str, str]:
        """复用 client 的头族构造（含 intl 指纹）。

        不直接 import `build_headers`：它定义在 `client.py`，而 client 要 import
        本模块 → 循环导入。走实例方法既避开环，也保证工具请求与聊天请求的
        指纹**逐字节同源**（上游按头族判客户端一致性）。
        """
        headers = self._client._headers(credential, model=model)
        headers.setdefault("Accept", "application/json")
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
        return headers

    async def search(self, credential: CodeBuddyCredential, *,
                     query: str, max_results: int = 5,
                     allowed_domains: list[str] | None = None,
                     blocked_domains: list[str] | None = None,
                     freshness: str | None = None) -> dict[str, Any]:
        """`POST /agenttool/v1/search` → 归一后的搜索结果。"""
        text = query
        if blocked_domains:
            text += "".join(f" -site:{domain}" for domain in blocked_domains)
        body: dict[str, Any] = {
            "query": text,
            "type": "text2text",
            "max_results": max(1, min(int(max_results), MAX_RESULTS_LIMIT)),
        }
        if allowed_domains:
            body["allowed_domains"] = list(allowed_domains)
        if freshness:
            body["freshness"] = freshness
        payload = await self._post(EP_SEARCH, body, credential,
                                   timeout=SEARCH_TIMEOUT_SECONDS)
        data = _envelope(payload)
        results = data.get("results")
        return {
            "provider": data.get("provider") or "",
            "results": [item for item in (results or []) if isinstance(item, dict)],
            "total_results": data.get("total_results") or 0,
            "response_time_ms": data.get("response_time_ms") or 0,
        }

    async def fetch(self, credential: CodeBuddyCredential, *, url: str,
                    prompt: str = "", max_length: int | None = None,
                    timeout: float | None = None,
                    model: str | None = None) -> dict[str, Any]:
        """`POST /agenttool/v1/webfetch` → `{url, content, content_type}`。"""
        body: dict[str, Any] = {
            "url": url,
            "format": "markdown",
            "prompt": prompt,
            "timeout": int(timeout if timeout is not None else FETCH_TIMEOUT_SECONDS),
            "max_length": max(1, min(int(max_length or MAX_CONTENT_CHARS),
                                     MAX_CONTENT_CHARS)),
        }
        if model:
            body["model_name"] = model
        payload = await self._post(EP_WEBFETCH, body, credential,
                                   timeout=FETCH_TIMEOUT_SECONDS + 5)
        data = _envelope(payload)
        content = data.get("content")
        return {
            "url": data.get("url") or url,
            "content": content if isinstance(content, str) else "",
            "content_type": data.get("content_type") or "text/plain",
        }

    async def _post(self, path: str, body: dict[str, Any],
                    credential: CodeBuddyCredential, *, timeout: float) -> dict[str, Any]:
        """发请求并返回 JSON；非 2xx 抛 `ToolRejected`（带建议状态码）。"""
        url = f"{self._client.endpoint}{path}"
        started = time.monotonic()
        try:
            response = await self._client._short.post(
                url, json=body, headers=self._headers(credential, None),
                timeout=httpx.Timeout(timeout))
        except httpx.HTTPError as error:
            raise base.UpstreamTransportError(str(error)) from error
        if response.status_code >= 400:
            status = response.status_code
            raw = await base.read_body_bounded(response)
            # 与聊天共用分类：401/403 → 凭据问题（403 交给上层换号）；
            # 429 → 限流；其余按「上游拒绝」
            if status in (401, 403):
                raise ToolRejected(403, "upstream rejected credential")
            if status == 429:
                raise ToolRejected(429, "upstream rate limited")
            raise ToolRejected(502, f"upstream http {status}: "
                                    f"{base.body_hint(raw)[:120]}")
        try:
            payload = response.json()
        except ValueError as error:
            raise base.UpstreamProtocolViolation(
                "non-JSON response from search/fetch") from error
        if not isinstance(payload, dict):
            raise base.UpstreamProtocolViolation(
                "search/fetch response must be an object")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        payload.setdefault("response_time_ms", elapsed_ms)
        return payload
