"""启动前自检（doctor）的纯函数实现。

**为什么需要**（issue #47）：能回答「起没起来」的只有 `/healthz`，而真正把部署
卡死的场景全都发生在服务起来之前，报错点离病根很远：

- 端口落在系统保留段 → uvicorn 绑定失败，launchd / 计划任务把它变成刷屏循环
  （见 `src/instance_lock.py` 记的 2377 次 `[Errno 48]`）；
- `APP_SECRET` 缺失或被换过 → 凭证解密失败，对外表现是「所有凭证都不可用」；
- `web/dist` 未构建或落后一个 release → 管理台白屏，而 `/healthz` 仍是 200；
- 库的 `user_version` 落后 `SCHEMA_VERSION` → 迁移在 lifespan 里跑，起不来时
  看不出卡在哪一列。

本模块只做**只读**检查：不启服务、不迁移、不写 `runtime_settings`、不落凭证
明文。`scripts/doctor.py` 负责排版与退出码，这里保持可被 lifespan 复用的纯函数
形态（每项返回 `Check`，不抛异常）。
"""

from __future__ import annotations

import errno
import os
import re
import socket
import sqlite3
import ssl
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .db.crypto import CredentialCipher
from .db.migrate import (
    _MIGRATION_COLUMN_DROPS,
    _MIGRATION_COLUMNS,
    _MIGRATION_DROPS,
    _MIGRATION_INDEXES,
    SCHEMA_VERSION,
)
from .security.private_files import check_private
from .webapp.security import host_allowed

#: 结果状态。PASS/WARN/FAIL 三态：WARN 是「能跑但该看一眼」，FAIL 才该拦门禁。
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

#: 前端入口文件里需要校验存在的资源后缀（Vite 产物只有 js/css 走 hash 命名）。
_ASSET_SUFFIXES = (".js", ".css")

#: 上游连通性探测的超时（秒）。部署自检不该因为一条慢上游卡住整体。
PROBE_TIMEOUT = 5.0


@dataclass(frozen=True)
class Check:
    """单项检查结果。`name` 只用于展示，`detail` 说清「怎么了 + 怎么修」。"""

    name: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """是否没有 FAIL（WARN 不影响门禁）。"""
        return self.status != FAIL


def check_python(version: tuple[int, int, int]) -> Check:
    """Python >= 3.12（`pyproject` 的 requires-python 与 CI 一致）。"""
    if version >= (3, 12):
        return Check("Python 版本", PASS, f"{version[0]}.{version[1]}.{version[2]}")
    return Check("Python 版本", FAIL,
                 f"{version[0]}.{version[1]}.{version[2]} 过低，需要 >= 3.12")


def check_dependencies(
    importer: Callable[[str], object] | None = None,
) -> Check:
    """关键依赖可 import（fastapi / httpx / cryptography）。

    只 import 不初始化：不建连接、不读配置，保证「依赖坏了」与「配置坏了」
    两个问题不会被混成一条。
    """
    import importlib

    load = importer or importlib.import_module
    missing: list[str] = []
    for name in ("fastapi", "httpx", "cryptography"):
        try:
            load(name)
        except Exception:  # noqa: BLE001 - 任何导入失败都算依赖不可用
            missing.append(name)
    if missing:
        return Check("关键依赖", FAIL,
                     f"导入失败: {', '.join(missing)}（重新执行 `uv sync`）")
    return Check("关键依赖", PASS, "fastapi / httpx / cryptography 可用")


def check_app_secret(settings: Settings) -> Check:
    """`APP_SECRET` 存在且能通过真实的弱密钥口径（复用 `CredentialCipher`）。

    不复制判定逻辑：长度 / 低熵 / 已知占位值的规则只有一份，doctor 报「过」
    必须等于服务启动时也「过」，否则自检会给出虚假的安全感。
    """
    secret = settings.app_secret
    if not secret:
        return Check("APP_SECRET", FAIL,
                     "未配置；凭证加密与会话签名都依赖它（生成："
                     "python -c \"import secrets; print(secrets.token_urlsafe(32))\"）")
    try:
        CredentialCipher(secret)
    except ValueError as error:
        return Check("APP_SECRET", FAIL, str(error))
    return Check("APP_SECRET", PASS, f"已配置（{len(secret)} 字符）")


def check_data_dir(settings: Settings) -> Check:
    """`DATA_DIR` 存在且可写。

    不存在不算失败（首次启动会自己建），但要说清「会自动创建」而不是让人以为
    忽略了配置写错；存在但不可写才是 FAIL——那种情况建库必然失败。
    """
    target = Path(settings.data_dir)
    if not target.exists():
        return Check("DATA_DIR", WARN, f"{target} 不存在（首次启动会自动创建）")
    if not target.is_dir():
        return Check("DATA_DIR", FAIL, f"{target} 不是目录")
    if not os.access(target, os.W_OK):
        return Check("DATA_DIR", FAIL, f"{target} 不可写（检查属主与挂载权限）")
    return Check("DATA_DIR", PASS, str(target))


def check_data_permissions(settings: Settings) -> Check:
    """`DATA_DIR` 权限口径：POSIX 看 `mode & 0o077`，Windows 看 ACL。

    与运行时收紧共用 `check_private`：**只读**判定，不做任何 chmod/icacls。
    Windows 上出现继承 ACE 或多余主体即 WARN——运行时已会尝试收紧，但运维
    仍需知道这条信任边界此前是否成立（issue #45）。
    """
    target = Path(settings.data_dir)
    if not target.exists():
        return Check("DATA_DIR 权限", WARN, "目录不存在，跳过权限检查")
    problem = check_private(target, directory=True)
    if problem is None:
        return Check("DATA_DIR 权限", PASS, "仅属主可访问")
    # 与启动口径一致：服务会因为这个状态**拒绝启动**，所以这里必须是 FAIL，
    # 不能降成 WARN——否则 doctor 说「警告」而服务直接退出，门禁就自相矛盾。
    return Check("DATA_DIR 权限", FAIL, f"{problem}（服务会拒绝启动，先修权限）")


def check_host_config(settings: Settings) -> Check:
    """`PUBLIC_BASE_URL` 与 `ALLOWED_HOSTS` 一致（`SECURITY.md` 的部署须知项）。

    不一致的后果不是报错而是**静默**：管理台打不开（Host 校验拒掉）、TRAE
    回调地址指向一个不被放行的主机。所以能在启动前报出来就报出来。
    """
    public_host = settings.public_base_url
    if not settings.allowed_hosts:
        # 空 = 本地回环 + PUBLIC_BASE_URL 主机，天然一致
        return Check("PUBLIC_BASE_URL / ALLOWED_HOSTS", PASS,
                     f"{public_host}（ALLOWED_HOSTS 留空，按默认口径放行）")
    # 只留主机名：`host_allowed` 比的是纯主机（剥 scheme / 端口），
    # 但 PUBLIC_BASE_URL 可能带 path（`https://host/callback`），不剥会误判。
    host = public_host.split("://", 1)[-1].split("/", 1)[0]
    if host_allowed(host, settings):
        return Check("PUBLIC_BASE_URL / ALLOWED_HOSTS", PASS, public_host)
    return Check("PUBLIC_BASE_URL / ALLOWED_HOSTS", WARN,
                 f"{public_host} 不在 ALLOWED_HOSTS 里，管理台与回调会打不开")


def pending_migration_count(conn: sqlite3.Connection) -> int:
    """统计**尚未应用**的迁移条目数（只读，绝不执行）。

    口径与 `apply_schema` 一致：缺列、缺索引、该删的遗留表/列各算一条。
    只用于展示「差多少」，具体怎么补由服务启动时的 `apply_schema` 负责。
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    count = 0
    for table, column in _MIGRATION_COLUMNS:
        if table not in tables:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column.split()[0] not in columns:
            count += 1
    indexes = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    count += sum(1 for name, _table, _column in _MIGRATION_INDEXES if name not in indexes)
    count += sum(1 for table in _MIGRATION_DROPS if table in tables)
    for table, column in _MIGRATION_COLUMN_DROPS:
        if table not in tables:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in columns:
            count += 1
    return count


def check_database(settings: Settings) -> Check:
    """SQLite 只读检查：`integrity_check` + `user_version` 与 `SCHEMA_VERSION` 的差值。

    以 `mode=ro` 打开：doctor 绝不能顺手 checkpoint / 建表 / 补列，否则它自己
    就变成第二个故障源。库不存在时 WARN（首次启动会建）。
    """
    path = Path(settings.db_path)
    if not path.is_file():
        return Check("数据库", WARN, f"{path} 不存在（首次启动会创建并建表）")
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity = row[0] if row else None
            if integrity != "ok":
                # 完整性都不过就不要再读版本号：坏库的 PRAGMA 结果不可信
                return Check("数据库", FAIL, f"integrity_check 未通过: {integrity!r}")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            pending = pending_migration_count(conn)
    except (sqlite3.Error, ValueError) as error:
        return Check("数据库", FAIL, f"无法读取 {path}: {error}")
    if version > SCHEMA_VERSION:
        return Check("数据库", FAIL,
                     f"schema 版本 {version} 高于本程序支持的 {SCHEMA_VERSION}；"
                     f"请升级程序，否则启动会拒绝")
    if pending:
        return Check("数据库", WARN,
                     f"版本 {version}/{SCHEMA_VERSION}，有 {pending} 条迁移待启动时执行")
    return Check("数据库", PASS, f"完整性 ok，版本 {version}/{SCHEMA_VERSION}")


def check_port(host: str, port: int,
               binder: Callable[..., object] | None = None) -> Check:
    """端口可用性：`bind` 一次并释放。

    只探测不监听：探测与真正 uvicorn 绑定之间仍有 TOCTOU 窗口，这里不假装
    能消除它（`src/instance_lock.py` 已说明为何不靠端口探测做单实例保护）。
    价值在于把「保留端口段 / 无权限绑定低端口」这类**确定性**失败提前说出来。
    """
    maker = binder or socket.socket
    try:
        sock = maker(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        finally:
            sock.close()
    except OSError as error:
        return Check("端口", FAIL,
                     f"{host}:{port} 无法绑定: {error}{_port_hint(error)}")
    return Check("端口", PASS, f"{host}:{port} 可绑定")


def _port_hint(error: OSError) -> str:
    """按错误码/平台补一句排查提示：占用、无权限、保留段是三件不同的事。"""
    if error.errno == errno.EADDRINUSE:
        return "（已有进程占用；若确信没在跑，检查残留进程或另一个实例）"
    if error.errno in (errno.EACCES, errno.EPERM):
        if os.name == "nt":
            return ("（Windows 上常见原因是端口落在系统保留段，检查："
                    "netsh interface ipv4 show excludedportrange protocol=tcp）")
        return "（Linux 上绑定 1024 以下端口需要 CAP_NET_BIND_SERVICE）"
    if os.name == "nt":
        return ("（Windows 上常见原因是端口落在系统保留段，检查："
                "netsh interface ipv4 show excludedportrange protocol=tcp）")
    return ""


def check_frontend(dist: str | Path | None) -> Check:
    """`web/dist` 存在，且 `index.html` 引用的 js/css 真的在磁盘上。

    专治「落后一个 release 的白屏」：只检查目录存在是不够的——换过构建产物后
    旧 `index.html` 仍可能引用已被删掉的 hash 文件名，后端 `/healthz` 照样 200。
    """
    if dist is None:
        return Check("前端产物", FAIL, "找不到 web/dist（执行 ./scripts/build-web.sh）")
    root = Path(dist)
    index = root / "index.html"
    if not index.is_file():
        return Check("前端产物", FAIL, f"{index} 不存在（执行 ./scripts/build-web.sh）")
    missing: list[str] = []
    for reference in _index_assets(index.read_text(encoding="utf-8")):
        if not (root / reference).is_file():
            missing.append(reference)
    if missing:
        return Check("前端产物", FAIL,
                     f"index.html 引用了不存在的资源: {', '.join(missing)}"
                     f"（重新构建：./scripts/build-web.sh --force）")
    return Check("前端产物", PASS, str(root))


def _index_assets(html: str) -> list[str]:
    """从 index.html 抽出本地 js/css 引用（跳过绝对 URL 与内联 data:）。

    用正则而非 HTML 解析：只需要 `<script src>` / `<link href>` 两类，引入
    解析器会让「构建产物格式正常」这个前提变得不值钱。
    """
    pattern = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""")
    found: list[str] = []
    for reference in pattern.findall(html):
        if reference.startswith(("http://", "https://", "//", "data:")):
            continue
        if not reference.endswith(_ASSET_SUFFIXES):
            continue
        found.append(reference.lstrip("/"))
    return found


def classify_upstream_error(error: BaseException) -> str:
    """把上游探测异常归成「TLS / DNS / 其它」——只需要能指向哪一层出问题。

    不输出任何地址细节以外的信息：doctor 的结果可能贴到 issue 里。
    """
    if isinstance(error, httpx.ConnectTimeout | httpx.ReadTimeout):
        return "超时"
    if isinstance(error, ssl.SSLError):
        return "TLS 失败"
    if isinstance(error, socket.gaierror):
        return "DNS 失败"
    if isinstance(error, httpx.ConnectError):
        cause = error.__cause__
        if isinstance(cause, ssl.SSLError):
            return "TLS 失败"
        if isinstance(cause, socket.gaierror):
            return "DNS 失败"
        return "连接失败"
    return "未知错误"


def probe_upstream(url: str, client: object | None = None) -> tuple[str, str]:
    """对某个渠道端点做一次最小请求，返回 `(状态, 说明)`。

    只报「可达 / TLS 失败 / DNS 失败 / 4xx」这四个结论：不带凭证的探测拿到
    401/403 恰恰说明**链路是通的**，把它当成失败会让每一次真实部署都亮红灯。
    """
    try:
        if client is None:
            with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=True) as owned:
                response = owned.head(url)
        else:
            response = client.head(url)  # type: ignore[attr-defined]
    except Exception as error:  # noqa: BLE001 - 探测失败只影响这一条结论
        return FAIL, classify_upstream_error(error)
    if response.status_code >= 500:
        return WARN, f"HTTP {response.status_code}（上游异常，与本机部署无关）"
    if response.status_code >= 400:
        return PASS, f"HTTP {response.status_code}（链路可达，鉴权由服务承担）"
    return PASS, f"HTTP {response.status_code}"


def check_upstreams(
    endpoints: list[tuple[str, str]],
    prober: Callable[[str], tuple[str, str]] = probe_upstream,
) -> list[Check]:
    """逐个渠道探测，一个渠道一条结果。

    `endpoints` 是 `(渠道名, 端点 URL)`；`prober` 可注入，便于测试与
    「离线部署也跑 doctor」的场景（传 `lambda _: (PASS, "已跳过")`）。
    """
    checks: list[Check] = []
    for name, url in endpoints:
        status, detail = prober(url)
        checks.append(Check(f"上游 {name}", status, f"{url} {detail}"))
    return checks
