"""HTTP 边缘层：请求体上限、Host 白名单、异常处理器、前端静态资源。

这些模块都是「注册到 app 上」的横切关注点，与业务装配无关：
- limits.py   请求体上限（纯 ASGI 中间件，登录 8KB / 其余 16MB）
- security.py Host 白名单 + 安全响应头
- handlers.py 异常 → HTTP 响应映射（稳定错误码，见 TECHNICAL §6.5）
- static.py   前端产物定位与 SPA catch-all 路由

装配顺序有约束：见 src/main.py 的 build_app 注释（BodySizeLimitMiddleware
必须最后 add，才会落在最外层）。
"""
