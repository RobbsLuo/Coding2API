# 多阶段构建：前端产物 + Python 运行时
FROM node:24-alpine AS web
WORKDIR /web
# pnpm-workspace.yaml 必须一起 COPY：pnpm 从这里读 allowBuilds（放行 esbuild
# 的 postinstall），缺失会以 ERR_PNPM_IGNORED_BUILDS 退出。
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm exec vite build

FROM python:3.12-slim AS runtime

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
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"

# CMD 走 src.main:run（而非硬编码 --host/--port）：HOST/PORT 是文档化配置项
# （config.py + README），硬编码会让它们只对本地启动生效、在容器里静默失效。
CMD ["python", "-m", "src.main"]
