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


def test_compose_forwards_every_settings_field():
    """compose 必须透传 config.py 的每个可配置字段。

    compose 的 .env 只用于 ${VAR} 插值，**不会**注入容器：没写进
    environment 的变量在 .env 里设了也无效，而且完全无报错（静默失效）。
    所以这里把两边对一遍，漏一个就抦住。
    """
    config_text = (ROOT / "src" / "config.py").read_text(encoding="utf-8")
    fields = set(re.findall(r"^    ([a-z_]+):[^=\n]*=", config_text, re.MULTILINE))
    # pydantic-settings 默认大小写不敏感，惯例上 env 全大写
    expected = {name.upper() for name in fields}
    compose_text = COMPOSE.read_text(encoding="utf-8")
    forwarded = set(re.findall(r"^\s+([A-Z_]+):", compose_text, re.MULTILINE))
    missing = expected - forwarded
    assert not missing, f"compose 未透传（.env 里设了也不会生效）: {sorted(missing)}"


def test_compose_forwards_port_used_by_entrypoint():
    """HOST/PORT 必须既透传又在入口生效（CMD 不得硬编码地址）。"""
    compose_text = COMPOSE.read_text(encoding="utf-8")
    assert "${PORT" in compose_text
    cmd = re.search(r"^CMD (.+)$", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert cmd is not None
    # 硬编码 --host/--port 会让 config.py 里的 HOST/PORT 在容器里静默失效
    assert "--host" not in cmd.group(1) and "--port" not in cmd.group(1)


def test_ci_workflow_paths_match_repository():
    """CI 里用到的路径必须存在，否则 workflow 必然失败。"""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for path in ("src", "tests", "scripts", "web/pnpm-lock.yaml"):
        assert path in workflow
        assert (ROOT / path).exists(), f"CI 引用 {path} 但它不存在"
    # 覆盖率门槛必须保留
    assert "--cov-fail-under=100" in workflow


# -------------------------------------------------─ macOS launchd 部署产物

LAUNCHD_TEMPLATE = ROOT / "deploy" / "launchd" / "com.coding2api.plist"
BUILD_WEB = ROOT / "scripts" / "build-web.sh"
LAUNCHD_SERVER = ROOT / "scripts" / "launchd-server.sh"


def test_launchd_template_exists_and_is_valid_plist():
    """仓库必须留一份 plist 模板：只放在 ~/Library/LaunchAgents 里不可复现。"""
    import plistlib

    assert LAUNCHD_TEMPLATE.is_file(), "缺少 deploy/launchd 下的 plist 模板"
    with LAUNCHD_TEMPLATE.open("rb") as handle:
        data = plistlib.load(handle)
    assert data["Label"] == "com.coding2api"
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    # 日志路径必须与本机 plist / newsyslog 规则一致（改一处漏一处最难查）
    assert data["StandardOutPath"].endswith("logs/launchd.out.log")
    assert data["StandardErrorPath"].endswith("logs/launchd.err.log")


def test_launchd_template_starts_via_wrapper_not_bare_uvicorn():
    """入口必须是 wrapper：它负责先构建前端产物。

    后端在 / 直接服务 web/dist，裸 uvicorn 会让首次部署拿到 503
    「管理台前端尚未构建」。
    """
    import plistlib

    with LAUNCHD_TEMPLATE.open("rb") as handle:
        args = plistlib.load(handle)["ProgramArguments"]
    assert args[-1].endswith("scripts/launchd-server.sh"), args
    assert "uvicorn" not in args, "uvicorn 应写在 wrapper 里，便于构建失败时不阻断"


def test_launchd_server_uses_exec_so_keepalive_tracks_the_real_process():
    """uvicorn 必须用 exec 替换脚本进程。

    否则 launchd 监管的是 bash，kickstart -k 的 SIGTERM 打到 bash，
    真正的 uvicorn 会变成孤儿继续占着 8000 端口。
    """
    text = LAUNCHD_SERVER.read_text(encoding="utf-8")
    assert re.search(r"^exec ", text, re.MULTILINE), "缺少 exec，信号无法直达 uvicorn"


def test_launchd_server_does_not_abort_when_build_fails():
    """构建失败不得阻断启动。

    plist 是 KeepAlive + ThrottleInterval=10：若脚本在构建失败时退出，
    launchd 会每 10 秒重拉一次 → 无限循环刷日志、烧 CPU。这里锁定
    「build 结果被判断后继续，而不是 set -e 直接退出」。
    """
    text = LAUNCHD_SERVER.read_text(encoding="utf-8")
    # 不能用 set -e：会让 build-web 的非零退出码直接终止脚本
    assert "set -e" not in text, "wrapper 不能开 set -e，构建失败会中断启动"
    assert "if ! " in text and "build-web.sh" in text


def test_build_web_resolves_pnpm_outside_login_shell():
    """build-web 必须自己解析 node/pnpm 路径。

    launchd 不读 shell rc，PATH 只有 plist 里的系统目录；本机 pnpm 装在
    nvm 下，直接调 `pnpm` 会 command not found。
    """
    text = BUILD_WEB.read_text(encoding="utf-8")
    assert ".nvm" in text, "未处理 nvm：launchd 环境下会找不到 pnpm"
    assert "--frozen-lockfile" in text, "依赖安装必须按 lockfile，与 CI 一致"


def test_build_web_is_executable():
    """两个脚本都必须有执行位，否则 plist 启动会 permission denied。"""
    for script in (BUILD_WEB, LAUNCHD_SERVER):
        assert script.is_file()
        assert script.stat().st_mode & 0o111, f"{script.name} 没有执行位"


# ------------------------------------------------- 探测失败原因的分类

def test_describe_probe_failure_maps_http_status_to_actionable_reason():
    """探测失败原因必须可操作，不能是 Python 类名。"""
    from src.api.admin_credentials import describe_probe_failure
    from src.provider.codebuddy.client import UpstreamHTTPError as CBHTTP
    from src.provider.trae.client import UpstreamHTTPError as TraeHTTP

    assert describe_probe_failure(CBHTTP(401, b"")) == "credential_rejected"
    assert describe_probe_failure(CBHTTP(403, b"")) == "credential_rejected"
    assert describe_probe_failure(TraeHTTP(429, b"")) == "rate_limited"
    assert describe_probe_failure(TraeHTTP(503, b"")) == "upstream_unavailable"
    assert describe_probe_failure(TraeHTTP(400, b"")) == "upstream_rejected"


def test_describe_probe_failure_maps_protocol_violation():
    from src.api.admin_credentials import describe_probe_failure
    from src.provider.codebuddy.events import (
        UpstreamProtocolViolation as CBViolation,
    )
    from src.provider.trae.events import UpstreamProtocolViolation as TraeViolation

    assert describe_probe_failure(CBViolation("x")) == "upstream_response_invalid"
    assert describe_probe_failure(TraeViolation("x")) == "upstream_response_invalid"


def test_describe_probe_failure_handles_timeout_and_unknown():
    from src.api.admin_credentials import describe_probe_failure

    assert describe_probe_failure(TimeoutError()) == "upstream_timeout"
    assert describe_probe_failure(RuntimeError("boom")) == "unknown_error"
    # 任何情况下都不得把类名当 reason
    for error in (RuntimeError("x"), ValueError("y"), KeyError("z")):
        reason = describe_probe_failure(error)
        assert type(error).__name__ not in reason


# ------------------------------------------------------------ 日志轮转

NEWSYSLOG = ROOT / "deploy" / "newsyslog" / "coding2api.conf"
LOGROTATE = ROOT / "deploy" / "logrotate" / "coding2api"
SYSTEMD_UNIT = ROOT / "deploy" / "systemd" / "coding2api.service"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.coding2api.plist"


def test_compose_limits_docker_log_growth():
    """json-file 驱动默认无上限，会把宿主磁盘写满——必须显式限额。"""
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"^\s+logging:", text, re.M), "compose 缺 logging 段"
    assert "max-size" in text and "max-file" in text
    assert "json-file" in text


def test_newsyslog_rule_matches_launchd_plist_paths():
    """轮转路径必须与 launchd 实际写的文件一致，否则规则永远不生效。"""
    text = NEWSYSLOG.read_text(encoding="utf-8")
    paths = [line.split()[0] for line in text.splitlines()
             if line.strip() and not line.startswith("#")]
    assert paths, "newsyslog 规则里没有有效条目"
    if LAUNCHD_PLIST.is_file():
        plist = LAUNCHD_PLIST.read_text(encoding="utf-8")
        for path in paths:
            assert path in plist, f"newsyslog 规则里的 {path} 不在 launchd plist 中"
    # 每条规则必须有 8 个字段（路径 属主 权限 份数 大小 时间 标志）
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            assert len(line.split()) == 7, f"newsyslog 字段数不对（需 7 列）: {line}"


def test_logrotate_rule_uses_copytruncate():
    """进程持有 fd（>> 重定向）时 rename 不会让它换文件，必须 copytruncate。"""
    text = LOGROTATE.read_text(encoding="utf-8")
    assert "copytruncate" in text
    assert "rotate " in text and "maxsize" in text
    assert "compress" in text


def test_systemd_unit_sends_logs_to_journal():
    """systemd 路线靠 journald 自带轮转，不需要额外 logrotate 配置。"""
    text = SYSTEMD_UNIT.read_text(encoding="utf-8")
    assert "StandardOutput=journal" in text
    assert "StandardError=journal" in text
    assert "ExecStart=" in text and "build_app" in text
    # 密钥不得写进 unit（systemctl cat 会暴露给所有用户）
    assert "APP_SECRET=" not in text


def test_newsyslog_install_script_is_executable():
    """安装脚本必须可执行，否则 README 里的用法会失败。"""
    script = ROOT / "scripts" / "install-newsyslog.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111, "安装脚本没有执行位"
    body = script.read_text(encoding="utf-8")
    assert "/etc/newsyslog.d/" in body      # 装到 newsyslog 会读的目录
    assert "--uninstall" in body


def test_newsyslog_owner_matches_actual_log_owner():
    """owned:group 必须与文件实际属主一致，否则 newsyslog 会跳过该条目。

    本机日志由 launchd 以当前用户身份写入，规则写成 root:wheel 就永远不会
    轮转（newsyslog 静默跳过），是很容易埋下的坑。
    """
    text = NEWSYSLOG.read_text(encoding="utf-8")
    entries = [line.split() for line in text.splitlines()
               if line.strip() and not line.startswith("#")]
    assert entries
    for path, owner in ((e[0], e[1]) for e in entries):
        target = Path(path)
        if not target.exists():
            continue                      # 未部署到本机的路径跳过
        import grp
        import pwd

        stat = target.stat()
        actual = f"{pwd.getpwuid(stat.st_uid).pw_name}:{grp.getgrgid(stat.st_gid).gr_name}"
        assert owner == actual, f"{path} 规则写 {owner}，实际是 {actual}"


def test_webapp_logging_module_does_not_open_files():
    """应用只写 stderr：不得引入 RotatingFileHandler / FileHandler。

    文件与轮转交给平台（launchd+newsyslog / systemd+journald / docker json-file）。
    应用自己写文件会与平台轮转争抢同一文件，容器里还会写进镜像层重启即丢。
    """
    src = (ROOT / "src" / "webapp" / "logging.py").read_text(encoding="utf-8")
    assert "RotatingFileHandler" not in src
    assert "FileHandler" not in src
    assert "StreamHandler" in src


def test_version_is_single_sourced_everywhere():
    """版本号必须处处一致：pyproject（真源）/ 前端 / 回落常量 / README / publish 示例。

    此前这些位置各写一遍，升版必漏——`/openapi.json` 会报出与镜像 tag 不同的
    版本，排查问题时误导。这条测试就是防止再漂移。
    """
    import json
    import re
    import tomllib
    from pathlib import Path

    from src.version import FALLBACK_VERSION, app_version

    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as handle:
        canonical = tomllib.load(handle)["project"]["version"]
    assert app_version() == canonical

    package_json = json.loads((root / "web" / "package.json").read_text(encoding="utf-8"))
    assert package_json["version"] == canonical, "web/package.json 版本未同步"

    assert canonical == FALLBACK_VERSION, "src/version.py 回落值未同步"

    readme = (root / "README.md").read_text(encoding="utf-8")
    pulls = re.findall(r"coding2api:v(\d+\.\d+\.\d+)", readme)
    assert pulls, "README 里找不到带版本的 docker pull 示例"
    assert set(pulls) == {canonical}, "README 的 docker pull 示例版本未同步"

    publish = (root / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    examples = re.findall(r"如 v(\d+\.\d+\.\d+)", publish)
    assert set(examples) == {canonical}, "publish.yml 的示例版本未同步"

    # 源码里不得再出现硬编码的版本号（除了单一版本源自己的回落常量）
    for path in (root / "src").rglob("*.py"):
        if path.name == "version.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert f'version="{canonical}"' not in text, f"{path} 硬编码了版本号"
