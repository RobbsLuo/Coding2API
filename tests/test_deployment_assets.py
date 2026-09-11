"""部署产物静态校验。

本机没有 docker（只有 Apple container，且构建需要 Rosetta），镜像无法本地构建，
因此把「构建必然会失败」的条件固化成静态断言，避免同类问题再次溜过去。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"


def _copy_sources(stage: str) -> list[str]:
    """取出某个构建阶段里所有 COPY 的宿主机侧路径。"""
    sources: list[str] = []
    for line in stage.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        parts = [p for p in stripped.split()[1:] if not p.startswith("--")]
        if "--from=" in stripped or len(parts) < 2:
            continue
        sources.extend(parts[:-1])
    return sources


def test_dockerfile_is_multi_stage_with_frontend_build():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count("FROM ") >= 2, "前端需要独立构建阶段"
    assert "pnpm exec vite build" in text
    # 前端产物必须从构建阶段取，不能从构建上下文取：
    # web/dist 在 .gitignore 与 .dockerignore 里，CI 全新检出后不存在。
    assert "COPY --from=web /web/dist ./web/dist/" in text
    assert "COPY web/dist/" not in text, "从上下文复制 web/dist 在 CI 必然失败"


@pytest.mark.parametrize("source", [
    "web/package.json", "web/pnpm-lock.yaml", "web/", "pyproject.toml",
    "uv.lock", "README.md", "src/", "scripts/",
])
def test_all_copy_sources_exist_in_repository(source):
    """Dockerfile 的 COPY 源必须真的在仓库里（不是在 .gitignore 里）。"""
    assert (ROOT / source).exists(), f"{source} 不在仓库中，CI 构建会失败"


def test_dockerfile_copy_sources_match_repository():
    """解析 Dockerfile 实际声明的 COPY 源，逐个校验存在性。"""
    runtime = DOCKERFILE.read_text(encoding="utf-8").split("AS runtime", 1)[1]
    for source in _copy_sources(runtime):
        if source.startswith("/"):
            continue
        assert (ROOT / source).exists(), f"COPY {source} 找不到"


def test_dockerignore_keeps_runtime_venv_and_node_modules_out():
    """.venv 与 node_modules 必须排除，否则宿主机产物会覆盖容器内安装。"""
    rules = [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert ".venv/" in rules
    assert "web/node_modules/" in rules
    assert "web/dist/" in rules
    assert "secrets/" in rules and "data/" in rules


def test_dockerignore_keeps_readme_for_uv_sync():
    """pyproject 的 readme 指向 README.md，被排除会让 uv sync 失败。"""
    rules = [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert "*.md" in rules
    assert "!README.md" in rules
    # 排除（negation）必须在通配之后才生效
    assert rules.index("!README.md") > rules.index("*.md")


def test_dockerfile_runs_as_non_root():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^USER (?!root)\S+", text, re.MULTILINE), "必须以非 root 用户运行"


def test_dockerfile_declares_healthcheck():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "HEALTHCHECK" in text and "/health" in text


def test_compose_requires_app_secret():
    text = COMPOSE.read_text(encoding="utf-8")
    # 缺 APP_SECRET 时必须直接失败，而不是用默认值静默启动
    assert "${APP_SECRET:?" in text


def test_compose_mounts_secrets_read_only():
    text = COMPOSE.read_text(encoding="utf-8")
    assert "./secrets:/app/secrets:ro" in text


def test_compose_publishes_same_port_as_dockerfile():
    dockerfile_port = re.search(r"^EXPOSE (\d+)", DOCKERFILE.read_text(encoding="utf-8"),
                                re.MULTILINE)
    assert dockerfile_port is not None
    assert f":{dockerfile_port.group(1)}" in COMPOSE.read_text(encoding="utf-8")


def test_ci_workflow_paths_match_repository():
    """CI 里用到的路径必须存在，否则 workflow 必然失败。"""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for path in ("src", "tests", "scripts", "web/pnpm-lock.yaml"):
        assert path in workflow
        assert (ROOT / path).exists(), f"CI 引用 {path} 但它不存在"
    # 覆盖率门槛必须保留
    assert "--cov-fail-under=100" in workflow


# ------------------------------------------------- 探测失败原因的分类

def test_describe_probe_failure_maps_http_status_to_actionable_reason():
    """探测失败原因必须可操作，不能是 Python 类名。"""
    from src.main import describe_probe_failure
    from src.provider.codebuddy.client import UpstreamHTTPError as CBHTTP
    from src.provider.trae.client import UpstreamHTTPError as TraeHTTP

    assert describe_probe_failure(CBHTTP(401, b"")) == "credential_rejected"
    assert describe_probe_failure(CBHTTP(403, b"")) == "credential_rejected"
    assert describe_probe_failure(TraeHTTP(429, b"")) == "rate_limited"
    assert describe_probe_failure(TraeHTTP(503, b"")) == "upstream_unavailable"
    assert describe_probe_failure(TraeHTTP(400, b"")) == "upstream_rejected"


def test_describe_probe_failure_maps_protocol_violation():
    from src.main import describe_probe_failure
    from src.provider.codebuddy.events import (
        UpstreamProtocolViolation as CBViolation,
    )
    from src.provider.trae.events import UpstreamProtocolViolation as TraeViolation

    assert describe_probe_failure(CBViolation("x")) == "upstream_response_invalid"
    assert describe_probe_failure(TraeViolation("x")) == "upstream_response_invalid"


def test_describe_probe_failure_handles_timeout_and_unknown():
    from src.main import describe_probe_failure

    assert describe_probe_failure(TimeoutError()) == "upstream_timeout"
    assert describe_probe_failure(RuntimeError("boom")) == "unknown_error"
    # 任何情况下都不得把类名当 reason
    for error in (RuntimeError("x"), ValueError("y"), KeyError("z")):
        reason = describe_probe_failure(error)
        assert type(error).__name__ not in reason
