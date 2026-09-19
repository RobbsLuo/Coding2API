#!/usr/bin/env bash
# 安装 macOS newsyslog 轮转规则（需 sudo：newsyslog 只读 /etc/newsyslog.d/）。
#
#     ./scripts/install-newsyslog.sh            # 安装 + 立即生效
#     ./scripts/install-newsyslog.sh --uninstall
#
# 日志路径取自 ~/Library/LaunchAgents/com.coding2api.plist，因此 plist 改了
# 日志位置时需要重新跑本脚本。
set -euo pipefail

RULES_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/newsyslog/coding2api.conf"
RULES_DST="/etc/newsyslog.d/coding2api.conf"

if [[ "${1:-}" == "--uninstall" ]]; then
    sudo rm -f "$RULES_DST"
    sudo launchctl kickstart -k system/com.apple.newsyslog
    echo "已移除 $RULES_DST"
    exit 0
fi

if [[ ! -f "$RULES_SRC" ]]; then
    echo "找不到规则文件: $RULES_SRC" >&2
    exit 1
fi

# 用规则里第一条路径做一次 dry-run 校验，避免装进坏规则导致 newsyslog 整体失败
echo "== 规则内容 =="
cat "$RULES_SRC"

sudo cp "$RULES_SRC" "$RULES_DST"
sudo launchctl kickstart -k system/com.apple.newsyslog
echo "已安装: $RULES_DST（单文件超 10MB 轮转，保留 7 份，bzip2 压缩）"
echo "查看结果: ls -la $(dirname "$(awk 'NF && $1 !~ /^#/ {print $1; exit}' "$RULES_SRC")")"
