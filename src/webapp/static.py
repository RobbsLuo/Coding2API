"""前端静态资源：产物定位 + SPA catch-all 路由。

模块级状态（_PROJECT_ROOT / _CONTAINER_DIST / _frontend_dist）被测试直接
patch，因此这里保持为普通模块属性，不做成类或闭包。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ..compat.openai.errors import error_payload

# 每个路径前缀对应的 API 错误形状：/v1 走 OpenAI 兼容体，其余走管理台形状
_API_PREFIXES = ("api/", "v1/")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONTAINER_DIST = Path("/app/web/dist")

_FRONTEND_MISSING_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Coding2API</title>
<style>body{font-family:ui-sans-serif,system-ui,sans-serif;max-width:44rem;margin:4rem auto;
padding:0 1.5rem;line-height:1.7;color:#1f2937}
code{background:#f3f4f6;padding:.15rem .4rem;border-radius:.25rem;font-size:.9em}
pre{background:#f3f4f6;padding:1rem;border-radius:.5rem;overflow-x:auto}
h1{font-size:1.25rem}</style></head>
<body><h1>管理台前端尚未构建</h1>
<p>后端已经在运行，但找不到前端产物，因此无法显示管理界面。</p>
<p>在项目根目录执行：</p>
<pre>cd web &amp;&amp; pnpm install &amp;&amp; pnpm build</pre>
<p>构建完成后刷新本页即可。API 端点（<code>/v1/*</code>、<code>/api/*</code>）
不受影响，现在就可以用。</p>
</body></html>"""


def api_not_found(path: str) -> JSONResponse:
    """未匹配的 /api、/v1 路径返回 JSON 404。

    静态资源是 catch-all 路由（返回 index.html），不排除 API 前缀的话，
    客户端拼错端点会拿到 200 + HTML，看起来“调用成功”，极难排查。
    """
    return JSONResponse(status_code=404,
                        content=error_payload(f"no such endpoint: /{path}",
                                              "invalid_request", 404))


def frontend_dist() -> Path | None:
    """定位前端产物目录；不存在时返回 None。"""
    candidates = (
        _PROJECT_ROOT / "web" / "dist",       # 源码运行
        _CONTAINER_DIST,                       # 容器内固定路径
        Path.cwd() / "web" / "dist",           # 兜底：从仓库根启动
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def register_spa_routes(app: FastAPI) -> None:
    """注册 SPA catch-all。

    必须在所有 API 路由之后注册——它是通配路由，先注册会吞掉一切。
    路径锚定到项目根（而不是当前工作目录），否则从其他目录启动服务时
    会找不到前端产物。找不到时给出可执行的下一步，而不是一句
    「frontend build not found」。
    """

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        """生产模式服务前端产物；开发模式由 Vite 代理，不经过此路由。"""
        if path.startswith(_API_PREFIXES) or path in ("api", "v1"):
            return api_not_found(path)
        dist = frontend_dist()
        if dist is None:
            return HTMLResponse(_FRONTEND_MISSING_HTML, status_code=503)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        index = dist / "index.html"
        if not index.is_file():
            return HTMLResponse(_FRONTEND_MISSING_HTML, status_code=503)
        return FileResponse(index)
