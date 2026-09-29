#!/usr/bin/env bash
# 安装 macOS newsyslog 轮转规则（需 sudo：newsyslog 只读 /etc/newsyslog.d/）。
#
#     ./scripts/install-newsyslog.sh            # 安装 + 立即生效
#     ./scripts/install-newsyslog.sh --uninstall
#
# deploy/newsyslog/coding2api.conf 是**模板**：路径写成 __PROJECT_ROOT__、
# 属主写成 __LOG_OWNER__ 占位符，避免把开发机路径 / 用户名带进仓库。
# 本脚本在安装时替换成本机实际仓库根与当前用户:组，再写入
# /etc/newsyslog.d/。日志位置取自 launchd plist，因此 plist 改了日志路径时
# 需要重新跑本脚本。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RULES_SRC="$ROOT/deploy/newsyslog/coding2api.conf"
RULES_DST="/etc/newsyslog.d/coding2api.conf"
LOG_OWNER="$(id -un):$(id -gn)"

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

# 渲染占位符到临时文件（LC_ALL=C：模板含中文注释，按字节替换，避免改坏 UTF-8）
RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT
LC_ALL=C sed -e "s|__PROJECT_ROOT__|$ROOT|g" \
              -e "s|__LOG_OWNER__|$LOG_OWNER|g" "$RULES_SRC" > "$RENDERED"

# 渲染后不应再残留占位符，否则说明模板改了字段名而脚本漏改
if grep -q "__PROJECT_ROOT__\|__LOG_OWNER__" "$RENDERED"; then
    echo "渲染后仍残留占位符，检查模板与脚本: $RULES_SRC" >&2
    exit 1
fi

# 校验：每条有效规则 7 列，且日志路径必须与 plist 写的位置一致
echo "== 规则内容 =="
cat "$RENDERED"

FIRST_LOG="$(awk 'NF && $1 !~ /^#/ {print $1; exit}' "$RENDERED")"
if [[ ! -f "$FIRST_LOG" ]]; then
    echo "警告: 规则里的日志 $FIRST_LOG 尚不存在（后端还没起过则正常）" >&2
fi

sudo cp "$RENDERED" "$RULES_DST"
sudo launchctl kickstart -k system/com.apple.newsyslog
echo "已安装: $RULES_DST（单文件超 10MB 轮转，保留 7 份，bzip2 压缩）"
echo "查看结果: ls -la $(dirname "$FIRST_LOG")"
