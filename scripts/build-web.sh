#!/usr/bin/env bash
# 构建前端产物（web/dist），供后端在 http://host:port/ 直接服务。
#
#     ./scripts/build-web.sh            # 仅在产物过期时构建
#     ./scripts/build-web.sh --force    # 无条件重建
#
# 为什么需要这个脚本而不是直接 `cd web && pnpm build`：
#
#   1. launchd 不读 shell 的 rc 文件，PATH 只有 plist 里写的系统目录。
#      本机 pnpm 装在 nvm 下（~/.nvm/versions/node/vX/bin），Homebrew 的
#      node 既没有 pnpm 也没有 corepack——直接写 `pnpm build` 会
#      `command not found`。这里从 ~/.nvm/alias/default 解析出实际路径。
#
#   2. node_modules 不存在时（全新检出）先按 lockfile 装依赖，否则
#      vite/tsc 都找不到。
#
# 退出码：构建失败返回非 0。调用方（launchd wrapper）应据此决定是否
# 阻断启动——见 scripts/launchd-server.sh 的说明。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB="$ROOT/web"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

# ---- 1. 定位 node/pnpm：优先 nvm（本机 pnpm 的实际来源）--------------------
NODE_BIN=""
if [[ -s "$HOME/.nvm/alias/default" ]]; then
    # alias/default 可能是 "24" 这类主版本号，也可能是 "v24.16.0"
    alias_ver="$(tr -d '[:space:]' < "$HOME/.nvm/alias/default")"
    alias_ver="${alias_ver#v}"
    for candidate in "$HOME"/.nvm/versions/node/v"$alias_ver"*/bin; do
        if [[ -d "$candidate" ]]; then
            NODE_BIN="$candidate"
            break
        fi
    done
fi
if [[ -z "$NODE_BIN" ]]; then
    # 回落到 PATH 里已有的 node（Homebrew / 系统）
    if command -v node >/dev/null 2>&1; then
        NODE_BIN="$(dirname "$(command -v node)")"
    fi
fi
if [[ -n "$NODE_BIN" ]]; then
    export PATH="$NODE_BIN:$PATH"
fi

if ! command -v pnpm >/dev/null 2>&1; then
    echo "build-web: 找不到 pnpm（已尝试 nvm 与 PATH: $NODE_BIN）" >&2
    echo "build-web: 请确认 pnpm 已安装，或设置 NVM_DIR" >&2
    exit 1
fi

cd "$WEB"

# ---- 2. 依赖：node_modules 缺失才装（--frozen-lockfile 保证与 CI 同版本）--
if [[ ! -d node_modules ]]; then
    echo "build-web: node_modules 缺失，安装依赖…"
    pnpm install --frozen-lockfile
fi

# ---- 3. 增量判断：源码/配置比产物新才重建 ----------------------------------
# 崩溃重启时避免无谓的重复构建（KeepAlive 会反复触发本脚本）。
# web/dist/ 在 .gitignore 里，全新检出必然不存在，此处会走构建。
if [[ "$FORCE" -eq 0 && -f dist/index.html ]]; then
    newer="$(find src index.html package.json pnpm-lock.yaml \
                  vite.config.ts tsconfig.json tsconfig.app.json tsconfig.node.json \
                  -newer dist/index.html -print -quit 2>/dev/null || true)"
    if [[ -z "$newer" ]]; then
        echo "build-web: 产物已是最新，跳过（--force 可强制重建）"
        exit 0
    fi
fi

echo "build-web: 开始构建（node $(node -v), pnpm $(pnpm -v)）"
pnpm build
echo "build-web: 完成 → $WEB/dist"
