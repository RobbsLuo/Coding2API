#!/usr/bin/env bash
# 安装 macOS launchd 服务（把仓库模板渲染成本机 plist，无需 sudo）。
#
#     ./scripts/install-launchd.sh              # 渲染 + 安装 + 立即启动
#     ./scripts/install-launchd.sh --uninstall  # 停止并移除
#
# 为什么要有这一层，而不是直接 cp 模板：
#   deploy/launchd/com.coding2api.plist 是**模板**，绝对路径写成
#   __PROJECT_ROOT__ 占位符——否则仓库里会带上开发机的路径，克隆到别处
#   就不可用。launchd 不展开 $HOME / 环境变量，路径必须是绝对路径，
#   所以只能在安装时把占位符替换成实际项目根。
#
# 重装会先 bootout 再 bootstrap，改模板后重跑本脚本即可生效。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.coding2api"
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
# 用 | 作 sed 分隔符（路径里通常没有 |）；__PROJECT_ROOT__ 只出现在值里。
# LC_ALL=C：模板含中文注释，按字节替换占位符，避免 locale 改坏 UTF-8。
LC_ALL=C sed "s|__PROJECT_ROOT__|$ROOT|g" "$TEMPLATE" > "$DEST"

if ! plutil -lint "$DEST" >/dev/null; then
    echo "渲染后的 plist 非法，已保留现场: $DEST" >&2
    exit 1
fi

# 先卸载旧的（未加载时 bootout 会失败，忽略），再加载新定义。
# bootout 是异步的：立即 bootstrap 会撞上「Input/output error」（旧实例
# 状态还没清理完），小睡重试几次。
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
echo "已安装并启动: $DEST"
echo "查看状态: launchctl print $DOMAIN/$LABEL | grep -E 'state|pid'"
