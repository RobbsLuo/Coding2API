# 多阶段构建：前端产物 + Python 运行时
FROM node:26-alpine AS web
WORKDIR /web
# pnpm-workspace.yaml 必须一起 COPY：pnpm 从这里读 allowBuilds（放行 esbuild
# 的 postinstall），缺失会以 ERR_PNPM_IGNORED_BUILDS 退出。
# .npmrc 同理必须带上：里面有 auto-install-peers=false，与 lockfile 的
# settings.autoInstallPeers 必须一致，否则 --frozen-lockfile 直接报
# ERR_PNPM_LOCKFILE_CONFIG_MISMATCH。
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml web/.npmrc ./
# Node 25 起不再内置 corepack，裸 `corepack enable` 会 exit 127（command not found）；
# 先用 npm 装一份再 enable。0.34.x 与 pnpm 11+ 不兼容，固定到 0.36.0。
#
# `corepack prepare` 要现去 registry.npmjs.org 取 pnpm 本体，该端点偶发 503
# （compose job 因此连续红过两次，同期 registry 拉普通包正常），而 corepack
# 自己不重试，所以这里显式重试。prepare 成功后 pnpm 已落在 corepack 缓存里，
# 后面的 install 不再需要联网取 pnpm 本体。
RUN npm install -g corepack@0.36.0 && corepack enable \
    && ( for i in 1 2 3 4 5; do \
           if corepack prepare pnpm@10.18.0 --activate; then exit 0; fi; \
           echo "corepack prepare 第 $i 次失败（registry 抖动？），10s 后重试"; \
           sleep 10; \
         done; exit 1 ) \
    && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm exec vite build

FROM python:3.14-slim AS runtime

# tzdata 是 CHECKIN_HOUR / 额度周期正确性的前提：任务用 time.localtime() 判断
# "今天 9 点"，基础镜像不带 zoneinfo 时 TZ 会被静默忽略、退回 UTC。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# uv：从官方镜像复制，避免联网安装脚本
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY scripts/ ./scripts/
# 从构建阶段取前端产物：web/dist 是 gitignored，CI 检出后不存在
COPY --from=web /web/dist ./web/dist/
RUN uv sync --frozen --no-dev

# 运行数据与用户文件由挂载提供；非 root 运行
RUN useradd --create-home --uid 1001 appuser \
    && mkdir -p /app/data /app/secrets \
    && chown -R appuser:appuser /app/data /app/secrets
USER appuser

ENV DATA_DIR=/app/data \
    USERS_FILE=/app/secrets/users.txt \
    TZ=Asia/Shanghai \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000
# 端口跟随上面的 PORT（默认 8000）：写死 8000 会让非默认 PORT 的
# `docker run` 部署永远不 healthy。用 python 读 os.environ 而非 shell 展开，
# 因为 HEALTHCHECK 的 exec 形式不做变量替换。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/health\")"

# CMD 走 src.main:run（而非硬编码 --host/--port）：HOST/PORT 是文档化配置项
# （config.py + README），硬编码会让它们只对本地启动生效、在容器里静默失效。
CMD ["python", "-m", "src.main"]
