#!/usr/bin/env bash
# 安装 macOS launchd 服务：frpc（反向隧道客户端，把本机 8000 映射到 VPS）。
#
#     ./scripts/install-frpc.sh --server <VPS 公网地址> --token-stdin
#     FRP_TOKEN=<与 frps 一致的 token> ./scripts/install-frpc.sh --server <VPS 公网地址>
#     ./scripts/install-frpc.sh --server <VPS 公网地址> --token <token>   # 不推荐，见下
#     ./scripts/install-frpc.sh --uninstall
#
# token 的三种传法（优先级从高到低）：
#   --token-stdin   从 stdin 读一行（`read -r -s`，不回显、不进 ps / shell 历史）
#   FRP_TOKEN 环境变量
#   --token <值>    仅作兼容保留：argv 对所有本机用户可见（`ps -ef` / `ps auxww`），
#                   且会落进 shell history。脚本会往 stderr 打一条弃用警告。
#   token 只在首次安装必须给；之后重装（只改地址）沿用 secrets/frpc-token。
#
# 与 install-launchd.sh 同一套路：deploy/launchd/com.coding2api.frpc.plist 是
# **模板**（绝对路径写成 __PROJECT_ROOT__、服务器地址写成 __FRP_SERVER_ADDR__），
# 安装时渲染成本机 plist 写入 ~/Library/LaunchAgents/ 并加载。launchd 不展开
# $HOME / 环境变量，路径必须是绝对路径，所以只能安装时替换。
#
# token 写进 secrets/frpc-token（权限 600，secrets/ 已在 .gitignore 中），
# 不写进 plist：plist 会被 `launchctl print` 原样打印出来。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.coding2api.frpc"
TEMPLATE="$ROOT/deploy/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
TOKEN_FILE="$ROOT/secrets/frpc-token"
LOGS_DIR="$ROOT/logs"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$DEST"
    echo "已卸载 $LABEL 并移除 $DEST（token 文件保留：$TOKEN_FILE）"
    exit 0
fi

SERVER=""
TOKEN=""
TOKEN_FROM_STDIN=0
TOKEN_FROM_ARGV=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --server) SERVER="${2:-}"; shift 2 ;;
        # 仅兼容保留：argv 可被同机任意用户读到
        --token)  TOKEN="${2:-}";  TOKEN_FROM_ARGV=1; shift 2 ;;
        --token-stdin) TOKEN_FROM_STDIN=1; shift ;;
        *) echo "未知参数: $1（用法见脚本顶部）" >&2; exit 2 ;;
    esac
done

if [[ "$TOKEN_FROM_STDIN" -eq 1 ]]; then
    # -s 不回显；read 失败（stdin 被关）时按「没给 token」处理。
    IFS= read -r -s TOKEN || TOKEN=""
fi
if [[ -z "$TOKEN" && -n "${FRP_TOKEN:-}" ]]; then
    TOKEN="$FRP_TOKEN"
fi

# 弃用警告：只提示「这个渠道不安全」，绝不回显 token 本身。
if [[ "$TOKEN_FROM_ARGV" -eq 1 ]]; then
    echo "警告：--token 已弃用——token 写在 argv 里，同机任何用户都能用" >&2
    echo "      ps -ef 读到，也会留在 shell history。改用 --token-stdin" >&2
    echo "      （或设 FRP_TOKEN 环境变量）。本次仍会按传入值安装。" >&2
fi

if [[ ! -f "$TEMPLATE" ]]; then
    echo "找不到模板: $TEMPLATE" >&2
    exit 1
fi
if [[ -z "$SERVER" ]]; then
    echo "缺少 --server（VPS 公网地址）；用法见脚本顶部" >&2
    exit 2
fi

# token 只在显式传入时覆盖已有文件；重复安装（只改地址）不会把 token 抹掉。
if [[ -n "$TOKEN" ]]; then
    mkdir -p "$(dirname "$TOKEN_FILE")"
    (umask 077 && printf '%s' "$TOKEN" > "$TOKEN_FILE")
fi
if [[ ! -f "$TOKEN_FILE" ]]; then
    echo "缺少 token 文件 $TOKEN_FILE；用 --token-stdin 或 FRP_TOKEN 传入" >&2
    echo "（须与 VPS 的 /etc/frp/frps-token 一致）" >&2
    exit 2
fi

# 日志目录：frpc 的 stdout/stderr 落这里（见 plist 的 StandardOutPath），
# 含隧道连接细节与上游错误正文 → 0700，同机其它用户不可读。
mkdir -p "$LOGS_DIR"
chmod 700 "$LOGS_DIR"

mkdir -p "$(dirname "$DEST")"
# 用 | 作 sed 分隔符（路径 / 地址里通常没有 |）。
# LC_ALL=C：模板含中文注释，按字节替换占位符，避免 locale 改坏 UTF-8。
LC_ALL=C sed -e "s|__PROJECT_ROOT__|$ROOT|g" \
               -e "s|__FRP_SERVER_ADDR__|$SERVER|g" \
    "$TEMPLATE" > "$DEST"

if ! plutil -lint "$DEST" >/dev/null; then
    echo "渲染后的 plist 不合法，已保留现场: $DEST" >&2
    exit 1
fi

# 重装先 bootout 再 bootstrap；launchd 释放旧实例有延迟，重试几次。
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
