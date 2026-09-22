#!/usr/bin/env bash
# launchd 服务入口：先尽量构建前端，再启动后端（前台，交给 launchd 监管）。
#
# 为什么要有这一层，而不是把 build 与 uvicorn 直接写进 plist：
#
#   plist 是 KeepAlive=true + ThrottleInterval=10。若把 `pnpm build` 串在
#   `uvicorn` 前面而不容错，一旦构建失败（缺依赖 / 类型错 / 断网），进程退出，
#   launchd 每 10 秒拉一次 → 无限循环，日志被刷爆、CPU 白烧。
#
#   本脚本的取舍：**构建失败不阻断启动**。后端服务的是 web/dist 里的既有产物
#   （src/webapp/static.py 用 FileResponse，每次请求现读磁盘），所以：
#     - 构建成功 → 立即生效，无需重启后端；
#     - 构建失败 → 后端照常起，继续服务上一份产物（没有产物时页面会给出
#       「管理台前端尚未构建」的可执行提示），失败原因留在 stderr。
#   uvicorn 用 exec 替换本进程，确保 launchd 拿到的 PID 就是服务本身，
#   信号（kickstart -k 的 SIGTERM）能直达，不会留孤儿进程。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "launchd-server: 构建前端（失败不阻断启动）…"
if ! "$ROOT/scripts/build-web.sh"; then
    echo "launchd-server: 警告：前端构建失败，将使用 web/dist 里的既有产物启动" >&2
fi

echo "launchd-server: 启动后端…"
exec /opt/homebrew/bin/uv run python -m uvicorn src.main:build_app \
    --factory --host 127.0.0.1 --port 8000
