#!/usr/bin/env bash
# 安装 macOS 日志轮转定时任务（launchd，无需 sudo）。
#
#     ./scripts/install-logrotate.sh              # 渲染 + 安装 + 加载
#     ./scripts/install-logrotate.sh --uninstall  # 停止并移除
#     ./scripts/install-logrotate.sh --now        # 安装后立即跑一次轮转
#
# 为什么不用 newsyslog：
#   macOS 的 newsyslog 只有 rename 式轮转（flags 无 copytruncate），而 launchd
#   重定向给服务的 fd 仍指向旧 inode → 轮转后进程继续写已归档的文件，新路径
#   的文件直到重启才出现，中间日志全丢（实测丢过 21.7h）。详见
#   scripts/rotate-logs.sh 顶部说明。
#
# 模板 deploy/launchd/com.coding2api.logrotate.plist 用 __PROJECT_ROOT__
# 占位符；launchd 不展开环境变量，路径只能在安装时渲染成绝对路径。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.coding2api.logrotate"
TEMPLATE="$ROOT/deploy/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$DEST"
    echo "已卸载 $LABEL 并移除 $DEST"
    exit 0
fi

if [[ ! -f "$TEMPLATE" ]]; then
    echo "找不到模板: $TEMPLATE" >&2
    exit 1
fi

mkdir -p "$(dirname "$DEST")"
# LC_ALL=C：模板含中文注释，按字节替换占位符，避免 locale 改坏 UTF-8。
LC_ALL=C sed "s|__PROJECT_ROOT__|$ROOT|g" "$TEMPLATE" > "$DEST"

if ! plutil -lint "$DEST" >/dev/null; then
    echo "渲染后的 plist 非法，已保留现场: $DEST" >&2
    exit 1
fi

# 先卸载旧的（未加载时 bootout 会失败，忽略），再加载新定义。
# bootout 是异步的：立即 bootstrap 会撞上「Input/output error」，小睡重试。
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
loaded=1
for _ in 1 2 3 4 5; do
    sleep 1
    if launchctl bootstrap "$DOMAIN" "$DEST" 2>/dev/null; then
        loaded=0
        break
    fi
done
if [[ "$loaded" -ne 0 ]]; then
    echo "bootstrap 失败：launchd 迟迟未释放旧实例，稍后手动重跑本脚本" >&2
    exit 1
fi
echo "已安装: $DEST（每 6 小时按阈值截断式轮转）"
echo "查看状态: launchctl print $DOMAIN/$LABEL | grep -E 'state|last exit'"
echo "手动跑一次: launchctl kickstart $DOMAIN/$LABEL"

if [[ "${1:-}" == "--now" ]]; then
    echo "== 立即执行一次 =="
    "$ROOT/scripts/rotate-logs.sh" --dry-run
    echo "（上面是 dry-run；真要轮转请直接跑 ./scripts/rotate-logs.sh）"
fi
