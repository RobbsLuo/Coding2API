# 多阶段构建：前端产物 + Python 运行时
FROM node:24-alpine AS web
WORKDIR /web
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm exec vite build

FROM python:3.12-slim AS runtime

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
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["python", "-m", "uvicorn", "src.main:build_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
