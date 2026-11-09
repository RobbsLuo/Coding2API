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

# 残留的 newsyslog 规则会静默吞日志：它只做 rename，launchd 给进程的 fd 仍
# 指向旧 inode → 轮转后到下次重启之间的日志全部丢失（本机实测丢过 19h，见
# scripts/rotate-logs.sh 顶部）。仓库侧已删干净并有测试守着，但 /etc 下的
# 副本属于本机状态、仓库管不到，只能在启动时吼一声，否则又要靠翻日志才发现。
NEWSYSLOG_RULE="/etc/newsyslog.d/coding2api.conf"
if [[ -f "$NEWSYSLOG_RULE" ]]; then
    echo "launchd-server: 警告：检测到残留的 newsyslog 规则 $NEWSYSLOG_RULE" >&2
    echo "launchd-server: 警告：它会 rename 轮转导致日志丢失，请执行：" >&2
    echo "launchd-server:   sudo rm $NEWSYSLOG_RULE && sudo launchctl kickstart -k system/com.apple.newsyslog" >&2
fi

echo "launchd-server: 构建前端（失败不阻断启动）…"
if ! "$ROOT/scripts/build-web.sh"; then
    echo "launchd-server: 警告：前端构建失败，将使用 web/dist 里的既有产物启动" >&2
fi

echo "launchd-server: 启动后端…"
# 走 src.main:run（而非 uvicorn CLI）：HOST/PORT 是 config.py 的文档化配置项，
# 写死 --host/--port 会让 .env 里的设置静默失效——launchd 不读 .env，
# 只有进程自己（pydantic-settings）读，所以监听地址必须由 run() 决定。
# 与 Docker CMD 同一条路径，GRACEFUL_SHUTDOWN_SECONDS 由 run() 内部传给 uvicorn，
# 三条启动路径的停机窗口自动同源，不再各写一个数字（写死过 30，见 git 历史）。
exec /opt/homebrew/bin/uv run python -m src.main
