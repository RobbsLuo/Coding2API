#!/usr/bin/env bash
# macOS 日志轮转：**截断式**（truncate in place），由 launchd 定时拉起。
#
# 为什么不用 newsyslog：
#   macOS 的 newsyslog 只支持 rename 式轮转（flags 只有 B C D G J N U Z，
#   没有 copytruncate——那是 Linux logrotate 的选项）。rename 之后
#   launchd 重定向给进程的 fd 仍指向**旧 inode**，进程继续往里写，而
#   新路径下的文件直到下次重启才被创建。实测（2026-10-30）：
#     mv app.log app.log.old → 进程继续写 app.log.old，app.log 不存在
#   后果就是「轮转后到下次重启之间的日志全丢」——本机实测丢过 21.7h /
#   19.6h / 2.0h / 1.2h，每次都止于一次重启，是排障盲区而不是噪声问题。
#
# 截断为什么安全：
#   launchd 以 O_APPEND 打开 StandardOutPath（实测：truncate 后进程从
#   offset 0 继续写，不留空洞），所以「先拷贝再清空」不会让后续写入
#   写到旧偏移上。这也是 Linux 侧 deploy/logrotate/coding2api 用
#   copytruncate 的同一理由，两边口径一致。
#
# **只用 bash 3.2 可用的语法**：macOS 自带 /bin/bash 是 3.2，没有
# mapfile / readarray。launchd 拉起本脚本时不走登录 shell，拿到的就是它。
#
# 用法：
#     ./scripts/rotate-logs.sh              # 按阈值轮转
#     ./scripts/rotate-logs.sh --dry-run    # 只报告会做什么
#     ./scripts/rotate-logs.sh --force      # 忽略阈值，立即轮转
#
# 由 deploy/launchd/com.coding2api.logrotate.plist 定时调用
# （scripts/install-logrotate.sh 安装）。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#: 日志目录可用环境变量覆盖：**测试必须覆盖**，否则会把真实 logs/ 轮转掉
#: （本脚本按自身位置解析 ROOT，在别的目录 cd 进去也改不了目标）。
LOGS="${ROTATE_LOGS_DIR:-$ROOT/logs}"

#: 单文件超这个大小（KB）就轮转，与旧 newsyslog 规则的 10240 一致。
MAX_KB="${ROTATE_MAX_KB:-10240}"
#: 每类日志保留几份归档（不含当前文件），与旧规则 count=7 一致。
KEEP="${ROTATE_KEEP:-7}"

DRY_RUN=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --force)   FORCE=1 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

STAMP="$(date +%Y%m%d-%H%M%S)"
rotated=0

# 固定四个日志（本体两路 + frpc 两路），不 glob：glob 在无匹配时会让
# 后续 ls 把当前目录当成参数，从而误删。
LOGS_LIST="launchd.out.log launchd.err.log frpc.out.log frpc.err.log"

for name in $LOGS_LIST; do
    file="$LOGS/$name"
    [[ -f "$file" ]] || continue

    bytes=$(stat -f %z "$file")
    # 空文件不值得归档（notifempty 的等价物）
    (( bytes == 0 )) && continue

    size_kb=$(( bytes / 1024 ))
    if (( ! FORCE && size_kb < MAX_KB )); then
        continue
    fi

    archive="$file.$STAMP"
    if (( DRY_RUN )); then
        echo "[dry-run] $file (${size_kb}KB) → $(basename "$archive").bz2，然后截断"
        rotated=$((rotated + 1))
        continue
    fi

    # 顺序即安全性：先拷出内容、压缩成功、再截断。
    # 任何一步失败都不截断——宁可继续涨，也不能丢还没归档的日志。
    if ! cp -p "$file" "$archive"; then
        echo "轮转失败（拷贝）：$file" >&2
        rm -f "$archive"
        continue
    fi
    if ! bzip2 -f "$archive"; then
        echo "轮转失败（压缩）：$file，保留原文件不截断" >&2
        rm -f "$archive"
        continue
    fi
    if ! : > "$file"; then
        echo "轮转失败（截断）：$file" >&2
        continue
    fi
    rotated=$((rotated + 1))
    echo "已轮转 $file → $(basename "$archive").bz2（${size_kb}KB）"
done

# 清理超额归档：按 mtime 倒序保留最近 KEEP 份。
# 用 while read 而不是 mapfile（bash 3.2 没有）；ls 前先确认有匹配，
# 否则无匹配时 ls 会列出当前目录。
if (( KEEP > 0 )); then
    for name in $LOGS_LIST; do
        # shellcheck disable=SC2086  # 需要 glob 展开
        set -- "$LOGS/$name".*.bz2
        [[ -f "$1" ]] || continue          # 无匹配时 glob 原样保留，-f 判否
        ls -1t "$@" 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r stale; do
            if (( DRY_RUN )); then
                echo "[dry-run] 删除旧归档 $(basename "$stale")"
            else
                rm -f "$stale"
            fi
        done
    done
fi

exit 0
