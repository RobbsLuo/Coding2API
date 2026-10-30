"""部署产物静态校验。

本机没有 docker（只有 Apple container，且构建需要 Rosetta），镜像无法本地构建，
因此把「构建必然会失败」的条件固化成静态断言，避免同类问题再次溜过去。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # noqa: PTH123 — 部署测试须自证仓库根，勿依赖 helpers
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


def test_web_stage_installs_corepack_before_enabling():
    """Node 25 起不再内置 corepack，必须先 npm 安装再 enable。

    裸 `corepack enable` 在 node:26-alpine 里是 exit 127（command not found），
    compose job 的镜像构建会直接失败。corepack 0.34.x 与 pnpm 11+ 不兼容，
    因此版本固定，不能退化成不带版本号的 `npm install -g corepack`。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    stage = text.split("FROM ", 1)[1].split("FROM ", 1)[0]
    # 只看 RUN 指令：注释里也会出现 corepack enable 字样
    runs = [line for line in stage.splitlines() if line.startswith("RUN ")]
    enable_runs = [line for line in runs if "corepack enable" in line]
    assert enable_runs, "web 阶段必须有启用 corepack 的 RUN"
    for line in enable_runs:
        assert "npm install -g corepack@" in line, "必须先 npm 安装 corepack 再 enable"
        assert line.index("npm install -g corepack@") < line.index("corepack enable"), \
            "安装必须在 enable 之前"


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


def test_compose_port_follows_through_mapping_and_healthcheck():
    """PORT 同时是「容器内监听端口」与「宿主机映射端口」，三处必须同源。

    曾踩过：`ports` 写成 `"${PORT:-8000}:8000"`（target 写死 8000）而
    `PORT` 又透传进容器当监听端口。设 `PORT=9000` 时应用监听 9000、映射却
    指向 8000、健康检查也打 8000 —— 容器永远不 healthy，且无任何报错线索。
    CI 只跑默认 PORT，所以这个 bug 对 CI 不可见，必须静态盯住。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    # 端口映射两侧都引用 PORT（target 不得写死）
    assert re.search(r"\$\{PORT:-8000\}:\$\{PORT:-8000\}", text), \
        "ports 的 target 必须跟随 PORT，否则非默认 PORT 的部署永远不 healthy"
    # 健康检查必须读容器内的 PORT，而不是写死 8000
    healthcheck = re.search(r"healthcheck:\n(?:.*\n)*?\s+retries: \d+", text)
    assert healthcheck, "compose 缺 healthcheck 段"
    assert "os.environ.get('PORT'" in healthcheck.group(0), \
        "healthcheck 端口必须跟随 PORT"


def test_dockerfile_healthcheck_follows_port_variable():
    """Dockerfile 的 HEALTHCHECK 同样不得写死 8000（`docker run` 路线）。

    compose 会覆盖 healthcheck，但直接用 `docker run` 时用的是镜像里这条；
    写死端口会让 `docker run -e PORT=9000` 的容器永远不 healthy。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    healthcheck = re.search(r"HEALTHCHECK[^\n]*\n\s+CMD (.+)", text)
    assert healthcheck, "Dockerfile 缺 HEALTHCHECK 的 CMD"
    command = healthcheck.group(1)
    assert "127.0.0.1:8000" not in command, "Dockerfile 健康检查写死了 8000"
    assert "os.environ.get('PORT'" in command, "Dockerfile 健康检查端口必须跟随 PORT"


def test_compose_forwards_tz():
    """TZ 必须在 compose 透传，否则 .env 里设了也被静默忽略。

    compose 的 .env 只用于 `${}` 插值、**不自动注入容器**；镜像里虽有
    `TZ=Asia/Shanghai` 兜底，但用户改成别的时区时不透传就等于没改。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"^\s+TZ:", text, re.MULTILINE), \
        ".env 里的 TZ 不会被注入容器（compose 未声明该 env）"


def test_compose_declares_host_gateway_for_host_side_proxy():
    """容器内 127.0.0.1 指容器自身，代理跑在宿主时必须能走 host.docker.internal。

    `PROVIDER_PROXIES` 只校验格式、不校验可达，地址写错不会启动失败，只会
    让该渠道全部请求连不上。compose 必须声明 extra_hosts 把宿主暴露进来。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert "extra_hosts:" in text, "compose 缺 extra_hosts"
    assert "host.docker.internal:host-gateway" in text, \
        "缺 host.docker.internal 映射，容器无法访问宿主代理"


def test_ci_workflow_paths_match_repository():
    """CI 里用到的路径必须存在，否则 workflow 必然失败。"""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for path in ("src", "tests", "scripts", "web/pnpm-lock.yaml"):
        assert path in workflow
        assert (ROOT / path).exists(), f"CI 引用 {path} 但它不存在"
    # 覆盖率门槛必须保留
    assert "--cov-fail-under=100" in workflow


def test_ci_compose_job_builds_fresh_image_with_admin_user():
    """compose job 必须真跑本次构建的镜像，且用户文件里要有 admin。

    两个坑都曾静默通过：
    * compose 同时写了 `build:` 与 `image:`，`up` 不带 `--build` 时会去
      registry 拉 `image:` 指向的已发布镜像 —— 测的不是本次代码；
    * 引导要求至少一个活跃 admin，而 compose 的 ADMIN_USERNAMES 默认
      `admin`，users.txt 建 `ci` 会直接启动失败。
    """
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "docker compose up -d --build" in workflow, "compose up 必须 --build"
    assert "printf 'admin:" in workflow, "users.txt 必须建 admin 用户"
    assert "printf 'ci:" not in workflow


# -------------------------------------------------─ macOS launchd 部署产物

LAUNCHD_TEMPLATE = ROOT / "deploy" / "launchd" / "com.coding2api.plist"
FRPC_PLIST_TEMPLATE = ROOT / "deploy" / "launchd" / "com.coding2api.frpc.plist"
# 日志路径来自任一个 launchd 模板（本体 / frpc 隧道），由轮转脚本覆盖
LAUNCHD_TEMPLATES = (LAUNCHD_TEMPLATE, FRPC_PLIST_TEMPLATE)
LAUNCHD_AGENTS = Path.home() / "Library" / "LaunchAgents"
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
    # 日志路径必须与轮转脚本覆盖的一致（改一处漏一处最难查）
    assert data["StandardOutPath"].endswith("logs/launchd.out.log")
    assert data["StandardErrorPath"].endswith("logs/launchd.err.log")


def test_launchd_template_uses_portable_placeholder_not_local_path():
    """部署模板不得写死开发机绝对路径，否则克隆到别处不可用。"""
    text = LAUNCHD_TEMPLATE.read_text(encoding="utf-8")
    assert PROJECT_ROOT_TOKEN in text, "plist 模板应用 __PROJECT_ROOT__ 占位符"
    home_marker = "/" + "Users" + "/"
    assert home_marker not in text, "plist 模板混入了开发机家目录路径"


def test_deploy_templates_contain_no_personal_absolute_paths():
    """全仓库不得混入开发机的个人家目录路径（macOS 的 Users、Linux 的 home）。

    这类路径只在开发机成立，进仓库后别人无法复现；需要绝对路径的场景
    （launchd 不展开环境变量）改用 __PROJECT_ROOT__ 占位符，
    由 scripts/install-*.sh 在安装时渲染。
    """
    import subprocess

    personal_markers = ("/" + "Users" + "/", "/" + "home" + "/")
    try:
        listed = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT, check=True, capture_output=True, text=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - 非 git 检出
        pytest.skip("非 git 检出，跳过")

    offenders = []
    for rel in listed:
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in personal_markers:
            if marker in text:
                offenders.append(rel)
                break
    assert not offenders, f"仓库混入开发机绝对路径: {offenders}"


def test_launchd_install_script_renders_placeholder():
    """安装脚本必须渲染占位符并写到 LaunchAgents。"""
    script = ROOT / "scripts" / "install-launchd.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111, "安装脚本没有执行位"
    body = script.read_text(encoding="utf-8")
    assert PROJECT_ROOT_TOKEN in body, "脚本必须替换 __PROJECT_ROOT__"
    assert "Library/LaunchAgents" in body
    assert "--uninstall" in body


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


def _shell_code_lines(path: Path) -> str:
    """剥掉 shell 注释行，只留可执行代码。

    断言「脚本里不得出现 uvicorn / --host」时不能连解释性注释一起算：
    注释恰恰要写清「为什么不再用 uvicorn CLI」，那是给后人看的。
    """
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#"))


def test_launchd_server_does_not_hardcode_listen_address():
    """脚本不得硬编码 --host/--port：HOST/PORT 是文档化配置项。

    CLI 参数优先级高于环境变量，写死 --host 127.0.0.1 会让 .env 里的
    HOST 静默失效——本地表现为「局域网 IP 访问不了、只有回环能通」，
    且 curl 本地永远成功，极难定位。走 src.main:run 才是唯一读 .env 的路径。
    """
    code = _shell_code_lines(LAUNCHD_SERVER)
    assert "--host" not in code, "监听地址必须由 src.main:run 从 .env 读取"
    assert "--port" not in code, "监听端口必须由 src.main:run 从 .env 读取"
    assert "python -m src.main" in code, "应走 src.main:run（唯一读 .env 的启动路径）"
    assert "uvicorn" not in code, "改走 src.main:run 后不应再出现 uvicorn CLI"


def test_launchd_server_uses_exec_so_keepalive_tracks_the_real_process():
    """服务进程必须用 exec 替换脚本进程。

    否则 launchd 监管的是 bash，kickstart -k 的 SIGTERM 打到 bash，
    真正的服务进程会变成孤儿继续占着 8000 端口。
    """
    text = LAUNCHD_SERVER.read_text(encoding="utf-8")
    assert re.search(r"^exec ", text, re.MULTILINE), "缺少 exec，信号无法直达服务进程"


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
    """探测失败原因必须可操作，不能是 Python 类名。

    按基类分派，故 CB / TRAE / Qoder 的 `UpstreamHTTPError` 走同一分支
    （Qoder 此前未被覆盖，401/403 会被误报成 unknown_error）。
    """
    from src.api.admin_credentials import describe_probe_failure
    from src.provider.codebuddy.client import UpstreamHTTPError as CBHTTP
    from src.provider.qoder.client import UpstreamHTTPError as QoderHTTP
    from src.provider.trae.client import UpstreamHTTPError as TraeHTTP

    assert describe_probe_failure(CBHTTP(401, b"")) == "credential_rejected"
    assert describe_probe_failure(CBHTTP(403, b"")) == "credential_rejected"
    assert describe_probe_failure(TraeHTTP(429, b"")) == "rate_limited"
    assert describe_probe_failure(TraeHTTP(503, b"")) == "upstream_unavailable"
    assert describe_probe_failure(TraeHTTP(400, b"")) == "upstream_rejected"
    assert describe_probe_failure(QoderHTTP(401, b"")) == "credential_rejected"
    assert describe_probe_failure(QoderHTTP(400, b"")) == "upstream_rejected"


def test_describe_probe_failure_maps_protocol_violation():
    from src.api.admin_credentials import describe_probe_failure
    from src.provider.base import UpstreamProtocolViolation
    from src.provider.codebuddy.events import (
        UpstreamProtocolViolation as CBViolation,
    )
    from src.provider.qoder.events import (
        UpstreamProtocolViolation as QoderViolation,
    )
    from src.provider.trae.events import UpstreamProtocolViolation as TraeViolation

    assert describe_probe_failure(CBViolation("x")) == "upstream_response_invalid"
    assert describe_probe_failure(TraeViolation("x")) == "upstream_response_invalid"
    assert describe_probe_failure(QoderViolation("x")) == "upstream_response_invalid"
    # 全渠道共用同一基类：新增渠道自动被覆盖
    assert isinstance(QoderViolation("x"), UpstreamProtocolViolation)


def test_describe_probe_failure_maps_transport_errors_to_network_unreachable():
    """连不上渠道（DNS / 连接被拒 / 建连超时）必须区别于「响应超时」。"""
    import httpx

    from src.api.admin_credentials import describe_probe_failure
    from src.provider.base import UpstreamTransportError

    assert describe_probe_failure(httpx.ConnectError("boom")) == "network_unreachable"
    assert describe_probe_failure(httpx.ConnectTimeout("boom")) == "network_unreachable"
    assert describe_probe_failure(UpstreamTransportError("boom")) == "network_unreachable"
    # 已连上但读超时 → 超时（不是「网络不可达」）
    assert describe_probe_failure(httpx.ReadTimeout("boom")) == "upstream_timeout"


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
# macOS 截断式轮转：newsyslog 只做 rename，会丢日志（见 scripts/rotate-logs.sh）
ROTATE_SCRIPT = ROOT / "scripts" / "rotate-logs.sh"
INSTALL_ROTATE = ROOT / "scripts" / "install-logrotate.sh"
ROTATE_PLIST = ROOT / "deploy" / "launchd" / "com.coding2api.logrotate.plist"

# 部署模板里的占位符（安装脚本渲染成实际值，见 scripts/install-*.sh）。
# 模板不得写死开发机路径，否则克隆到别处就不可用。
PROJECT_ROOT_TOKEN = "__PROJECT_ROOT__"
LOG_OWNER_TOKEN = "__LOG_OWNER__"


def _render_template(text: str) -> str:
    """按安装脚本的口径替换占位符，供测试还原「安装后」的文件内容。"""
    import grp
    import os
    import pwd

    owner = f"{pwd.getpwuid(os.getuid()).pw_name}:{grp.getgrgid(os.getgid()).gr_name}"
    return text.replace(PROJECT_ROOT_TOKEN, str(ROOT)).replace(LOG_OWNER_TOKEN, owner)



def test_compose_limits_docker_log_growth():
    """json-file 驱动默认无上限，会把宿主磁盘写满——必须显式限额。"""
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"^\s+logging:", text, re.M), "compose 缺 logging 段"
    assert "max-size" in text and "max-file" in text
    assert "json-file" in text


def _plist_label(template: Path) -> str:
    """读模板里的 Label（渲染前后不变，无需替换占位符）。"""
    import plistlib

    with template.open("rb") as handle:
        return str(plistlib.load(handle)["Label"])


def test_rotate_script_covers_every_launchd_log_path():
    """轮转脚本必须覆盖 launchd 实际写的每个日志，否则那个文件会无限涨。

    口径：从 launchd 模板取 StandardOutPath / StandardErrorPath，逐个断言
    scripts/rotate-logs.sh 里有对应的文件名。改名一处漏一处是最难查的坑。
    """
    import plistlib

    script = ROTATE_SCRIPT.read_text(encoding="utf-8")
    covered = 0
    for template in LAUNCHD_TEMPLATES:
        with template.open("rb") as handle:
            data = plistlib.load(handle)
        for key in ("StandardOutPath", "StandardErrorPath"):
            path = data.get(key)
            if not path:
                continue
            name = path.rsplit("/", 1)[-1]
            assert name in script, f"rotate-logs.sh 未覆盖 {name}（来自 {template.name}）"
            covered += 1
    assert covered >= 2, "launchd 模板里没有找到日志路径"


def test_rotate_script_uses_truncate_not_rename():
    """macOS 只能截断式轮转：rename 会让进程继续写旧 inode，日志直接丢。

    实测（2026-10-30）：mv app.log app.log.old 后进程仍写 app.log.old，
    新路径文件不存在；newsyslog 的 flags 只有 B C D G J N U Z，没有
    copytruncate。所以本项目自建脚本，必须看到「拷贝 → 压缩 → 截断」的顺序。
    """
    script = ROTATE_SCRIPT.read_text(encoding="utf-8")
    code = _shell_code_lines(ROTATE_SCRIPT)
    # 必须截断当前文件（: > file），不能只 mv/rename
    assert ": > " in code, "缺少截断步骤（rename 会丢日志）"
    assert "cp -p" in code, "缺少拷贝步骤（先归档再截断）"
    assert "bzip2" in code, "缺少压缩步骤"
    # 截断必须晚于压缩成功：否则压缩失败就把未归档的日志清掉了
    assert code.index("bzip2 -f") < code.index(": > "), "截断早于压缩成功"
    # 不得出现 rename 式轮转
    assert "mv " not in code, "不得使用 mv/rename 轮转"
    # 轮转目标目录可覆盖：否则测试会把真实 logs/ 轮转掉
    assert "ROTATE_LOGS_DIR" in script, "缺少 ROTATE_LOGS_DIR 覆盖（测试隔离用）"


def test_rotate_script_avoids_platform_specific_stat():
    """不能依赖 `stat -f %z`：GNU 的 `stat -f` 会**成功**返回文件系统信息。

    CI 在 ubuntu 上真跑本脚本（上面的行为用例），BSD/GNU 差异会直接红。
    这条与 bash 3.2 兼容性同理：脚本本体只服务 macOS，测试却跨平台。
    """
    code = _shell_code_lines(ROTATE_SCRIPT)
    assert "stat -f" not in code, "BSD 专用的 stat -f 在 GNU 上不报错但结果错"
    assert "wc -c" in code, "文件大小用 wc -c（两边行为一致）"


def test_rotate_script_is_bash32_compatible():
    """launchd 用 /bin/bash（macOS 自带 3.2），不能用 mapfile / readarray。"""
    code = _shell_code_lines(ROTATE_SCRIPT)
    assert "mapfile" not in code, "bash 3.2 没有 mapfile"
    assert "readarray" not in code, "bash 3.2 没有 readarray"
    assert "${archives[@]}" not in code, "数组切片在 3.2 上行为不同，用 while read"


def test_rotate_plist_schedules_without_keepalive():
    """定时任务必须跑完即退：KeepAlive 会让它被立刻反复重拉。"""
    import plistlib

    with ROTATE_PLIST.open("rb") as handle:
        data = plistlib.load(handle)
    assert data["Label"] == "com.coding2api.logrotate"
    assert not data.get("KeepAlive"), "轮转是一次性任务，不能 KeepAlive"
    assert data.get("RunAtLoad") is False, "登录时不必立刻轮转（阈值未到）"
    intervals = data.get("StartCalendarInterval")
    assert intervals, "缺少定时触发"
    assert len(intervals) >= 2, "至少两个时点，避免只在一个时段轮转"
    # 走 /bin/bash 而不是 exec 脚本本身：脚本 shebang 用 bash，但显式声明更稳
    assert data["ProgramArguments"][0] == "/bin/bash"
    assert data["ProgramArguments"][-1].endswith("scripts/rotate-logs.sh")


def test_rotate_install_script_renders_placeholder():
    """安装脚本必须渲染 __PROJECT_ROOT__ 并写进 LaunchAgents。"""
    assert INSTALL_ROTATE.is_file()
    assert INSTALL_ROTATE.stat().st_mode & 0o111, "安装脚本没有执行位"
    body = INSTALL_ROTATE.read_text(encoding="utf-8")
    assert PROJECT_ROOT_TOKEN in body
    assert "Library/LaunchAgents" in body
    assert "--uninstall" in body
    assert "launchctl bootstrap" in body and "launchctl bootout" in body


def test_newsyslog_rules_removed():
    """macOS 不再用 newsyslog：它只做 rename，会让服务继续写旧 inode。

    回归护栏——如果有人把 newsyslog 规则加回来，说明又踩回丢日志的坑。
    """
    assert not NEWSYSLOG.exists(), "macOS 侧已改用 scripts/rotate-logs.sh，不应再有 newsyslog 规则"
    assert not (ROOT / "scripts" / "install-newsyslog.sh").exists()


def test_rotate_script_actually_rotates_in_isolation(tmp_path):
    """真跑一次：内容归档、当前文件被截断、超额归档被清理。

    **必须用 ROTATE_LOGS_DIR 指到临时目录**：脚本按自身位置解析仓库根，
    在别的 cwd 里跑也照样作用于真实 logs/（这条坑是 2026-10-30 实际踩过的，
    当场轮转掉本机日志并误删了 4 份历史归档）。
    """
    import subprocess

    logs = tmp_path / "logs"
    logs.mkdir()
    payload = "x" * 4096
    (logs / "launchd.err.log").write_text(payload, encoding="utf-8")
    env = {
        **__import__("os").environ,
        "ROTATE_LOGS_DIR": str(logs),
        "ROTATE_MAX_KB": "1",
        "ROTATE_KEEP": "2",
    }
    result = subprocess.run(["bash", str(ROTATE_SCRIPT)], env=env,
                            capture_output=True, text=True, check=True)
    assert "已轮转" in result.stdout
    # 当前文件被截断为空，归档里有完整内容
    assert (logs / "launchd.err.log").read_text(encoding="utf-8") == ""
    archives = list(logs.glob("launchd.err.log.*.bz2"))
    assert len(archives) == 1
    import bz2

    assert bz2.decompress(archives[0].read_bytes()).decode() == payload


def test_rotate_script_dry_run_leaves_files_untouched(tmp_path):
    """--dry-run 只报告，不归档也不截断。"""
    import subprocess

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "launchd.err.log").write_text("y" * 4096, encoding="utf-8")
    env = {
        **__import__("os").environ,
        "ROTATE_LOGS_DIR": str(logs),
        "ROTATE_MAX_KB": "1",
    }
    result = subprocess.run(["bash", str(ROTATE_SCRIPT), "--dry-run"], env=env,
                            capture_output=True, text=True, check=True)
    assert "[dry-run]" in result.stdout
    assert (logs / "launchd.err.log").read_text(encoding="utf-8") == "y" * 4096
    assert not list(logs.glob("*.bz2"))


def test_rotate_script_skips_empty_and_small_files(tmp_path):
    """空文件与未超阈值的文件都不轮转（notifempty / size 语义）。"""
    import subprocess

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "launchd.err.log").write_text("", encoding="utf-8")
    (logs / "launchd.out.log").write_text("z" * 512, encoding="utf-8")
    env = {
        **__import__("os").environ,
        "ROTATE_LOGS_DIR": str(logs),
        "ROTATE_MAX_KB": "10240",
    }
    subprocess.run(["bash", str(ROTATE_SCRIPT)], env=env,
                   capture_output=True, text=True, check=True)
    assert not list(logs.glob("*.bz2"))


def test_rotate_script_prunes_old_archives(tmp_path):
    """超过 KEEP 的归档按 mtime 倒序清理，只留最近 KEEP 份。"""
    import subprocess
    import time

    logs = tmp_path / "logs"
    logs.mkdir()
    # 先造 3 份旧归档，再触发一次轮转（KEEP=2 → 最后应剩 2 份）
    for index in range(3):
        archive = logs / f"launchd.err.log.2026010{index + 1}-000000.bz2"
        archive.write_bytes(b"old")
        time.sleep(0.01)
    (logs / "launchd.err.log").write_text("w" * 4096, encoding="utf-8")
    env = {
        **__import__("os").environ,
        "ROTATE_LOGS_DIR": str(logs),
        "ROTATE_MAX_KB": "1",
        "ROTATE_KEEP": "2",
    }
    subprocess.run(["bash", str(ROTATE_SCRIPT)], env=env,
                   capture_output=True, text=True, check=True)
    assert len(list(logs.glob("launchd.err.log.*.bz2"))) == 2


def test_rotate_script_rejects_unknown_flag():
    """未知参数直接退 2，避免拼错的 --dryrun 被当成真跑。"""
    import subprocess

    result = subprocess.run(["bash", str(ROTATE_SCRIPT), "--nope"],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "未知参数" in result.stderr


def test_logrotate_rule_uses_copytruncate():
    """Linux 侧：进程持有 fd（>> 重定向）时 rename 不会让它换文件，必须 copytruncate。"""
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


def test_graceful_shutdown_timeout_consistent_across_entrypoints():
    """停机排空时长必须处处一致，且容器/systemd 窗口 >= 应用侧排空窗口。

    三条启动路径（Docker CMD / launchd → src.main:run、systemd → uvicorn CLI）
    各写一遍同一个数字，最容易漂移；漂移的后果是「本地排空 30s、生产无限等」
    或「应用还在排空就被 SIGKILL、长流统计丢失」。这里把它们对一遍。
    """
    import re as _re

    from src.main import GRACEFUL_SHUTDOWN_SECONDS

    assert GRACEFUL_SHUTDOWN_SECONDS > 0
    # launchd 与 Docker 都走 src.main:run，flag 由 run() 内部传入，脚本里没有
    # 这个数字可对；systemd 仍直接跑 uvicorn CLI，必须显式带同一个 flag。
    text = SYSTEMD_UNIT.read_text(encoding="utf-8")
    found = _re.search(r"--timeout-graceful-shutdown (\d+)", text)
    assert found, f"{SYSTEMD_UNIT.name} 缺 --timeout-graceful-shutdown"
    assert int(found.group(1)) == GRACEFUL_SHUTDOWN_SECONDS, SYSTEMD_UNIT.name
    # launchd 走 run() 的前提：脚本代码里不得再出现 uvicorn CLI（否则数字又分裂）
    launchd = _shell_code_lines(ROOT / "scripts" / "launchd-server.sh")
    assert "uvicorn" not in launchd, "launchd 改走 src.main:run 后不应再出现 uvicorn CLI"
    # Docker：stop_grace_period 是容器给进程的停机窗口，必须 >= 应用排空窗口
    compose = COMPOSE.read_text(encoding="utf-8")
    grace = _re.search(r"stop_grace_period: (\d+)s", compose)
    assert grace, "compose 缺 stop_grace_period"
    assert int(grace.group(1)) >= GRACEFUL_SHUTDOWN_SECONDS
    # systemd 的 TimeoutStopSec 同理（>= 排空窗口，否则提前 SIGKILL）
    unit = SYSTEMD_UNIT.read_text(encoding="utf-8")
    stop = _re.search(r"TimeoutStopSec=(\d+)", unit)
    assert stop and int(stop.group(1)) >= GRACEFUL_SHUTDOWN_SECONDS
    # launchd 的 ExitTimeOut 同理：默认约 20s，短于排空窗口就会 SIGKILL 长流
    plist = LAUNCHD_TEMPLATE.read_text(encoding="utf-8")
    exit_timeout = _re.search(r"<key>ExitTimeOut</key>\s*<integer>(\d+)</integer>", plist)
    assert exit_timeout, f"{LAUNCHD_TEMPLATE.name} 缺 ExitTimeOut"
    assert int(exit_timeout.group(1)) >= GRACEFUL_SHUTDOWN_SECONDS


def test_rotate_script_preserves_file_owner():
    """截断式轮转用 cp -p：属主/权限跟着归档走，不因轮转变成 root 或丢权限。"""
    code = _shell_code_lines(ROTATE_SCRIPT)
    assert "cp -p" in code, "必须保留属主与权限（cp -p），否则归档属主错乱"


def test_webapp_logging_module_does_not_open_files():
    """应用只写 stderr：不得引入 RotatingFileHandler / FileHandler。

    文件与轮转交给平台（launchd+rotate-logs.sh / systemd+journald / docker json-file）。
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

    root = Path(__file__).resolve().parents[2]
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


# ------------------------------------------------- 公网入口（VPS + frp 反向隧道）
#
# 家宽在 CGNAT 后没有可入站地址，公网入口只能由内网侧主动连出建隧道。
# 这组产物把「客户端 --443/TLS--> VPS(nginx) --frp 隧道--> Mac:8000」固化下来，
# 关键不变量在这里锁死：frp 映射端口不得对公网暴露、token 不得内联进配置、
# nginx 必须透传 Host 与 X-Forwarded-Proto、SSE 必须关缓冲。

FRP_DIR = ROOT / "deploy" / "frp"
FRPS_TOML = FRP_DIR / "frps.toml"
FRPC_TOML = FRP_DIR / "frpc.toml"
NGINX_CONF = ROOT / "deploy" / "nginx" / "coding2api.conf"
FRPS_UNIT = ROOT / "deploy" / "systemd" / "frps.service"
FRPC_RUN = ROOT / "scripts" / "frpc-run.sh"
INSTALL_FRPC = ROOT / "scripts" / "install-frpc.sh"

# nginx 的 proxy_read_timeout 必须盖过应用侧非流式聚合上限（config.py 默认 600s），
# 否则长对话会被 nginx 先掐断，表现为客户端 504 而不是应用的超时错误。
MIN_PROXY_READ_TIMEOUT_SECONDS = 600


def _toml_int(text: str, key: str) -> int:
    """取出形如 `key = 123` 的整数值（frp 配置是 TOML，值可能带下划线）。"""
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(\d+)\s*$", text, re.M)
    assert match, f"找不到整数配置项 {key}"
    return int(match.group(1))


def test_frps_binds_mapped_port_to_loopback():
    """映射端口必须只监听回环：公网入口是 nginx，明文 8000 不能直接开在公网。

    不设 proxyBindAddr 时 frps 按 bindAddr(0.0.0.0) 绑定，等于绕开 nginx 的
    TLS 与应用自身的 Host 校验，把回源流量摊成明文。
    """
    text = FRPS_TOML.read_text(encoding="utf-8")
    assert re.search(r'^\s*proxyBindAddr\s*=\s*"127\.0\.0\.1"', text, re.M), \
        "frps 的 proxyBindAddr 必须锁到回环"


def test_frps_and_frpc_read_token_from_file_not_inline():
    """token 必须走独立文件：配置文件会被 cp / 备份 / 贴进工单，内联等于随处分发。"""
    for path in (FRPS_TOML, FRPC_TOML):
        text = path.read_text(encoding="utf-8")
        assert re.search(r'^\s*auth\.tokenSource\.type\s*=\s*"file"', text, re.M), \
            f"{path.name} 的 token 必须从文件读"
        assert re.search(r"^\s*auth\.tokenSource\.file\.path\s*=", text, re.M), \
            f"{path.name} 缺少 tokenSource 文件路径"
        # 内联形式 `auth.token = "..."` 一律禁止（tokenSource 不会被误伤：
        # 等号紧跟 token 才是内联）
        assert not re.search(r'^\s*auth\.token\s*=', text, re.M), \
            f"{path.name} 内联了 token"


def test_frps_forces_tls_and_restricts_allowed_remote_ports():
    """隧道跨公网：必须强制 TLS；且只放行约定的那一个远程端口。"""
    text = FRPS_TOML.read_text(encoding="utf-8")
    assert re.search(r"^\s*transport\.tls\.force\s*=\s*true", text, re.M), \
        "frps 必须强制客户端启用 TLS"
    assert re.search(r"^\s*allowPorts\s*=\s*\[", text, re.M), "frps 必须限制可申请的远程端口"
    assert "single = 8000" in text, "allowPorts 应只放行 8000"


def test_frps_dashboard_stays_on_loopback():
    """dashboard 不对公网暴露，也就不需要把密码写进配置文件。"""
    text = FRPS_TOML.read_text(encoding="utf-8")
    assert re.search(r'^\s*webServer\.addr\s*=\s*"127\.0\.0\.1"', text, re.M), \
        "frps dashboard 必须只听回环"
    assert "password" not in text, "dashboard 只听回环时不该在配置里放密码"


def test_frpc_has_no_machine_specific_values():
    """客户端配置必须可直接克隆使用：地址走环境变量模板，token 走文件。"""
    text = FRPC_TOML.read_text(encoding="utf-8")
    assert '{{ .Envs.FRP_SERVER_ADDR }}' in text, "serverAddr 必须用环境变量模板"
    assert '{{ .Envs.FRP_TOKEN_FILE }}' in text, "token 路径必须用环境变量模板"
    # 不得出现写死的地址 / 家目录（拼接以免本文件自身被
    # test_deploy_templates_contain_no_personal_absolute_paths 误伤）
    assert ("/" + "Users" + "/") not in text
    assert ("/" + "home" + "/") not in text


def test_frpc_maps_local_8000_to_remote_8000():
    """回源目标就是本机后端（127.0.0.1:8000），映射端口与 nginx 反代目标一致。"""
    text = FRPC_TOML.read_text(encoding="utf-8")
    assert _toml_int(text, "localPort") == 8000
    assert _toml_int(text, "remotePort") == 8000
    assert re.search(r'^\s*localIP\s*=\s*"127\.0\.0\.1"', text, re.M), \
        "回源必须走回环，不能是 0.0.0.0"
    assert re.search(r'^\s*type\s*=\s*"tcp"', text, re.M), "应使用 tcp 代理"
    # 健康检查打本机存活探针，服务没起来时不在 frps 上挂死映射
    assert "healthCheck" in text and 'path = "/health"' in text


def test_nginx_proxy_target_matches_frp_remote_port():
    """nginx 反代目标必须与 frp 映射端口一致，否则整条链路断在第一跳。"""
    nginx = NGINX_CONF.read_text(encoding="utf-8")
    remote_port = _toml_int(FRPC_TOML.read_text(encoding="utf-8"), "remotePort")
    assert f"proxy_pass http://127.0.0.1:{remote_port};" in nginx, \
        f"nginx 应反代到 127.0.0.1:{remote_port}"


def test_nginx_passes_host_and_forwarded_proto():
    """Host 原样透传（ALLOWED_HOSTS 校验）；XFP 决定会话 Cookie 的 Secure。"""
    text = NGINX_CONF.read_text(encoding="utf-8")
    assert "proxy_set_header Host $host;" in text, "Host 必须原样透传"
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in text, \
        "缺 X-Forwarded-Proto 会让 https 入口下发不带 Secure 的会话 Cookie"
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in text


def test_nginx_disables_buffering_for_sse():
    """聊天是 SSE 长流：不关缓冲会把 token 攒起来一次性吐出。"""
    text = NGINX_CONF.read_text(encoding="utf-8")
    assert "proxy_buffering off;" in text, "SSE 必须关 proxy_buffering"
    assert "proxy_cache off;" in text


def test_nginx_read_timeout_covers_longest_conversation():
    """读超时要盖过应用侧非流式聚合上限，否则长对话被 nginx 先掐断。"""
    text = NGINX_CONF.read_text(encoding="utf-8")
    match = re.search(r"^\s*proxy_read_timeout\s+(\d+)s;", text, re.M)
    assert match, "缺少 proxy_read_timeout"
    assert int(match.group(1)) >= MIN_PROXY_READ_TIMEOUT_SECONDS, \
        f"proxy_read_timeout 需 >= {MIN_PROXY_READ_TIMEOUT_SECONDS}s"


def test_nginx_body_limit_aligns_with_app_middleware():
    """两侧同值才能让超限统一由应用裁决（否则 nginx 先返回 HTML 形状的 413）。"""
    from src.webapp.limits import DEFAULT_BODY_LIMIT

    text = NGINX_CONF.read_text(encoding="utf-8")
    expected = f"client_max_body_size {DEFAULT_BODY_LIMIT // (1024 * 1024)}m;"
    assert expected in text, f"nginx 上传上限应与应用对齐（{expected}）"


def test_nginx_uses_domain_placeholder_not_hardcoded_host():
    """域名是部署期值：模板用占位符，安装时替换，仓库里不留具体域名。"""
    text = NGINX_CONF.read_text(encoding="utf-8")
    assert "__TUNNEL_DOMAIN__" in text, "缺少域名占位符"
    assert text.count("server_name __TUNNEL_DOMAIN__;") == 2, "两个 server 块都要用占位符"


def test_frps_unit_runs_unprivileged_and_hardened():
    """frps 不需要 root（端口都 > 1024）：专用账号 + 只读文件系统。"""
    text = FRPS_UNIT.read_text(encoding="utf-8")
    assert re.search(r"^User=frps$", text, re.M), "frps 应以专用非 root 账号运行"
    assert re.search(r"^ExecStart=/usr/local/bin/frps -c /etc/frp/frps\.toml$", text, re.M)
    assert re.search(r"^NoNewPrivileges=true$", text, re.M)
    assert re.search(r"^ProtectSystem=strict$", text, re.M)
    assert re.search(r"^StandardOutput=journal$", text, re.M), "日志交给 journald"
    # unit 里不放任何密钥：token 走 /etc/frp/frps-token（600，属主 frps），
    # 否则 `systemctl cat frps` 会把密钥暴露给任何用户
    assert not re.search(r"^Environment", text, re.M), "unit 里不得内联环境变量/密钥"


def test_frpc_plist_template_is_valid_and_renders():
    """plist 模板必须合法，且占位符渲染后路径落到项目根内。"""
    import plistlib

    text = FRPC_PLIST_TEMPLATE.read_text(encoding="utf-8")
    assert PROJECT_ROOT_TOKEN in text, "plist 模板必须用 __PROJECT_ROOT__ 占位符"
    assert "__FRP_SERVER_ADDR__" in text, "服务器地址应是占位符"
    assert ("/" + "Users" + "/") not in text, "模板不得写死开发机路径"

    rendered = _render_template(text).replace("__FRP_SERVER_ADDR__", "vps.example.com")
    data = plistlib.loads(rendered.encode("utf-8"))
    assert data["Label"] == "com.coding2api.frpc"
    assert data["EnvironmentVariables"]["FRP_SERVER_ADDR"] == "vps.example.com"
    # token 文件落在项目根的 secrets/ 下（该目录已在 .gitignore 中）
    assert data["EnvironmentVariables"]["FRP_TOKEN_FILE"] == f"{ROOT}/secrets/frpc-token"
    # plist 里只出现 token 的**文件路径**，不出现 token 值本身：launchctl print
    # 会把 plist 原样打印出来，任何内联密钥都等于对同机用户公开。
    env_keys = set(data["EnvironmentVariables"])
    assert not [k for k in env_keys if "TOKEN" in k and k != "FRP_TOKEN_FILE"], \
        "plist 只应注入 token 文件路径，不得注入 token 值"


def test_frpc_plist_writes_logs_covered_by_rotate_script():
    """launchd 写的 frpc 日志必须被轮转脚本覆盖，否则日志无限增长。"""
    plist_text = _render_template(FRPC_PLIST_TEMPLATE.read_text(encoding="utf-8"))
    rotate = ROTATE_SCRIPT.read_text(encoding="utf-8")
    for suffix in ("frpc.out.log", "frpc.err.log"):
        assert f"logs/{suffix}" in plist_text, f"plist 未把日志写到 logs/{suffix}"
        assert suffix in rotate, f"rotate-logs.sh 缺少 logs/{suffix} 的轮转"


def test_frpc_run_resolves_binary_and_execs():
    """wrapper 解析 frpc 位置后 exec（PID 即 frpc），并指向仓库内配置。"""
    text = FRPC_RUN.read_text(encoding="utf-8")
    assert "command -v frpc" in text, "应按 PATH 解析 frpc"
    assert "/opt/homebrew/bin/frpc" in text, "应兜底 Homebrew 前缀"
    assert 'exec "$FRPC_BIN" -c "$CONFIG"' in text, "必须 exec 替换进程"
    assert 'CONFIG="$ROOT/deploy/frp/frpc.toml"' in text
    assert FRPC_RUN.stat().st_mode & 0o111, "wrapper 必须可执行"


def test_install_frpc_renders_placeholders_and_installs_agent():
    """安装脚本：渲染两个占位符 → 写 LaunchAgents → 加载；token 落在 secrets/。"""
    text = INSTALL_FRPC.read_text(encoding="utf-8")
    assert 'LABEL="com.coding2api.frpc"' in text
    assert "__PROJECT_ROOT__" in text and "__FRP_SERVER_ADDR__" in text
    assert 'DEST="$HOME/Library/LaunchAgents/$LABEL.plist"' in text
    assert "launchctl bootstrap" in text and "launchctl bootout" in text
    assert "--uninstall" in text
    assert 'TOKEN_FILE="$ROOT/secrets/frpc-token"' in text
    assert "umask 077" in text, "token 文件必须按 600 落盘"
    assert INSTALL_FRPC.stat().st_mode & 0o111, "安装脚本必须可执行"
