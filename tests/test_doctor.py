"""`src.doctor`：启动前自检的每一项都要能区分 PASS / WARN / FAIL。

这些用例只跑纯函数：不启服务、不写库。凡涉及平台的（Windows 保留段提示、
ACL 校验）一律 monkeypatch 测全，不靠 pragma 掩盖（AGENTS.md 硬约束）。
"""

from __future__ import annotations

import errno
import socket
import sqlite3
import ssl
from pathlib import Path

import httpx
import pytest

from src import doctor
from src.config import Settings
from src.db.migrate import SCHEMA_VERSION, apply_schema
from tests.helpers.constants import SECRET


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {"APP_SECRET": SECRET, "DATA_DIR": str(tmp_path)}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _shape_settings(tmp_path: Path, **overrides) -> Settings:
    """绕过 field_validator 构造（模拟老 .env / 运行期改写出的非法值）。

    Settings 本身已拒绝非法 PUBLIC_BASE_URL；doctor 仍要对**已存在的坏配置**
    报 FAIL（升级前写入的 .env、直接构造的对象），所以这里用 model_construct。
    """
    values = {"app_secret": SECRET, "data_dir": str(tmp_path),
              "public_base_url": "http://127.0.0.1:8000", "host": "127.0.0.1",
              "allowed_hosts": ""}
    values.update({key.lower(): value for key, value in overrides.items()})
    return Settings.model_construct(**values)


# ------------------------------------------------------------------ Python


def test_check_python_accepts_312():
    assert doctor.check_python((3, 12, 1)).status == doctor.PASS


def test_check_python_rejects_older():
    check = doctor.check_python((3, 11, 9))
    assert check.status == doctor.FAIL
    assert "3.12" in check.detail


# ------------------------------------------------------------- 依赖可 import


def test_check_dependencies_passes_for_real_environment():
    assert doctor.check_dependencies().status == doctor.PASS


def test_check_dependencies_lists_every_missing_module():
    def importer(name: str):
        raise ImportError(f"no {name}")

    check = doctor.check_dependencies(importer)
    assert check.status == doctor.FAIL
    assert "fastapi" in check.detail and "httpx" in check.detail
    assert "uv sync" in check.detail


# --------------------------------------------------------------- APP_SECRET


def test_check_app_secret_missing_is_fail(tmp_path):
    check = doctor.check_app_secret(
        Settings(_env_file=None, APP_SECRET="", DATA_DIR=str(tmp_path)))
    assert check.status == doctor.FAIL
    assert "未配置" in check.detail


def test_check_app_secret_reuses_real_weak_rules(tmp_path):
    """弱密钥判定只有一份实现：doctor 必须与服务启动同口径。"""
    check = doctor.check_app_secret(_settings(tmp_path, APP_SECRET="short"))
    assert check.status == doctor.FAIL


def test_check_app_secret_ok(tmp_path):
    check = doctor.check_app_secret(_settings(tmp_path))
    assert check.status == doctor.PASS
    assert str(len(SECRET)) in check.detail


# ---------------------------------------------------------------- DATA_DIR


def test_check_data_dir_missing_is_warn(tmp_path):
    check = doctor.check_data_dir(_settings(tmp_path / "nope"))
    assert check.status == doctor.WARN
    assert "自动创建" in check.detail


def test_check_data_dir_not_a_directory(tmp_path):
    target = tmp_path / "data"
    target.write_text("x", encoding="utf-8")
    check = doctor.check_data_dir(_settings(target))
    assert check.status == doctor.FAIL


def test_check_data_dir_unwritable(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.os, "access", lambda *_a, **_k: False)
    check = doctor.check_data_dir(_settings(tmp_path))
    assert check.status == doctor.FAIL
    assert "不可写" in check.detail


def test_check_data_dir_ok(tmp_path):
    assert doctor.check_data_dir(_settings(tmp_path)).status == doctor.PASS


def test_check_data_permissions_missing_dir_is_warn(tmp_path):
    check = doctor.check_data_permissions(_settings(tmp_path / "nope"))
    assert check.status == doctor.WARN


def test_check_data_permissions_wide_mode_is_fail(tmp_path):
    """权限过宽 = 服务会拒绝启动，doctor 必须 FAIL 而不是 WARN（口径一致）。"""
    tmp_path.chmod(0o755)
    check = doctor.check_data_permissions(_settings(tmp_path))
    assert check.status == doctor.FAIL
    assert "0755" in check.detail


def test_check_data_permissions_ok(tmp_path):
    tmp_path.chmod(0o700)
    assert doctor.check_data_permissions(_settings(tmp_path)).status == doctor.PASS


# ------------------------------------------------ PUBLIC_BASE_URL / ALLOWED_HOSTS


def test_host_config_default_allows_public_base(tmp_path):
    check = doctor.check_host_config(
        _settings(tmp_path, PUBLIC_BASE_URL="http://127.0.0.1:8000"))
    assert check.status == doctor.PASS


def test_host_config_explicit_and_consistent(tmp_path):
    check = doctor.check_host_config(_settings(
        tmp_path, PUBLIC_BASE_URL="https://ai.example.com",
        ALLOWED_HOSTS="ai.example.com"))
    assert check.status == doctor.PASS


def test_host_config_strips_path_and_port(tmp_path):
    """PUBLIC_BASE_URL 带 path/端口时也应与 ALLOWED_HOSTS 一致。"""
    check = doctor.check_host_config(_settings(
        tmp_path, PUBLIC_BASE_URL="https://ai.example.com:8443/callback/x",
        ALLOWED_HOSTS="ai.example.com"))
    assert check.status == doctor.PASS


def test_host_config_explicit_but_missing_host_is_fail(tmp_path):
    """最容易踩的一处静默故障：ALLOWED_HOSTS 忘了加自己的域名。

    此时服务会真的拒掉该 Host（400），不是「能跑但该看一眼」，必须 FAIL。
    """
    check = doctor.check_host_config(_settings(
        tmp_path, PUBLIC_BASE_URL="https://ai.example.com",
        ALLOWED_HOSTS="other.example.com"))
    assert check.status == doctor.FAIL
    assert "打不开" in check.detail and "ai.example.com" in check.detail


def test_host_config_rejects_relative_public_base(tmp_path):
    """漏写 `://` → 回调变相对路径，登录静默失败，必须 FAIL 并给示例。"""
    check = doctor.check_host_config(_shape_settings(
        tmp_path, PUBLIC_BASE_URL="127.0.0.1:8000"))
    assert check.status == doctor.FAIL
    assert "https://ai.example.com" in check.detail


def test_host_config_rejects_empty_public_base(tmp_path):
    check = doctor.check_host_config(_shape_settings(tmp_path, PUBLIC_BASE_URL=""))
    assert check.status == doctor.FAIL


def test_host_config_rejects_public_base_without_host(tmp_path):
    """`http:///path` 有 scheme 但没有主机：同样解析不出可回调的地址。"""
    check = doctor.check_host_config(_shape_settings(
        tmp_path, PUBLIC_BASE_URL="https:///oops"))
    assert check.status == doctor.FAIL
    assert "缺少主机名" in check.detail


def test_host_config_rejects_unparsable_public_base(tmp_path):
    """`urlsplit` 抛 ValueError（未闭合 IPv6 括号）时不能把 doctor 打挂。"""
    check = doctor.check_host_config(_shape_settings(
        tmp_path, PUBLIC_BASE_URL="http://[::1"))
    assert check.status == doctor.FAIL
    assert "无法解析" in check.detail


def test_host_config_rejects_base_that_parses_to_no_host(tmp_path):
    """`_host_of` 对无主机地址返回空串（最终兜底分支有单测）。"""
    assert doctor._host_of("http://") == ""
    assert doctor._host_of("https://ai.example.com:8443/x") == "ai.example.com"
    assert doctor._host_of("http://[::1]:8000") == "::1"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
@pytest.mark.parametrize("base", [
    "http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"])
def test_host_config_wildcard_bind_with_loopback_base_is_fail(tmp_path, host, base):
    """绑通配却把基址留在回环：远端管理台与回调都打不开，doctor 不能报 PASS。"""
    check = doctor.check_host_config(_settings(
        tmp_path, HOST=host, PUBLIC_BASE_URL=base))
    assert check.status == doctor.FAIL
    assert "打不开" in check.detail


def test_host_config_wildcard_bind_with_public_base_is_pass(tmp_path):
    """通配监听 + 真实地址是正确部署（远端可达），不应误报。"""
    check = doctor.check_host_config(_settings(
        tmp_path, HOST="0.0.0.0", PUBLIC_BASE_URL="https://ai.example.com"))
    assert check.status == doctor.PASS


def test_host_config_loopback_bind_with_loopback_base_is_pass(tmp_path):
    """只监听回环 + 回环基址是本地开发形态，不应误报。"""
    check = doctor.check_host_config(_settings(
        tmp_path, HOST="127.0.0.1", PUBLIC_BASE_URL="http://127.0.0.1:8000"))
    assert check.status == doctor.PASS


@pytest.mark.parametrize("host", ["myhost", "myhost.lan", "192.168.1.10"])
def test_is_wildcard_host_rejects_non_unspecified(tmp_path, host):
    """`HOST` 是普通主机名 / 具体网卡地址时都不是通配，不该判成远端打不开。

    覆盖 `ipaddress.ip_address` 抛 `ValueError` 的分支（主机名不是 IP 字面量）。
    """
    assert doctor._is_wildcard_host(host) is False
    check = doctor.check_host_config(_settings(
        tmp_path, HOST=host, PUBLIC_BASE_URL="http://127.0.0.1:8000"))
    assert check.status == doctor.PASS


def test_host_config_ipv6_base_matches_bracketed_allowlist(tmp_path):
    """IPv6 基址（`http://[::1]:8000`）与 `ALLOWED_HOSTS=[::1]` 应判一致。"""
    check = doctor.check_host_config(_settings(
        tmp_path, PUBLIC_BASE_URL="http://[::1]:8000", ALLOWED_HOSTS="[::1]"))
    assert check.status == doctor.PASS


# ------------------------------------------------ TRUST_PROXY / TRUSTED_PROXIES


def test_trusted_proxies_default_is_pass(tmp_path):
    """默认配置（不信 XFF、名单空、未强制）→ PASS，不该给存量部署亮黄灯。"""
    check = doctor.check_trusted_proxies(_settings(tmp_path))
    assert check.status == doctor.PASS
    assert "TRUST_PROXY=false" in check.detail
    assert "TRUSTED_PROXIES=空" in check.detail


def test_trusted_proxies_empty_but_unenforced_is_warn(tmp_path):
    """TRUST_PROXY=true 且名单为空、但未强制 → WARN：遗留风险主动提示不拦门禁。"""
    check = doctor.check_trusted_proxies(_settings(tmp_path, TRUST_PROXY=True))
    assert check.status == doctor.WARN
    assert "allowed_ips" in check.detail
    assert "REQUIRE_TRUSTED_PROXIES=true" in check.detail


def test_trusted_proxies_enforced_empty_is_fail(tmp_path):
    """强制开启且名单为空 → 服务会拒绝启动，doctor 必须 FAIL。"""
    check = doctor.check_trusted_proxies(_settings(
        tmp_path, TRUST_PROXY=True, REQUIRE_TRUSTED_PROXIES=True))
    assert check.status == doctor.FAIL
    assert "拒绝启动" in check.detail


def test_trusted_proxies_enforced_but_trust_proxy_off_is_pass(tmp_path):
    """强制开启但不采信 XFF：没有风险面，不该拦。"""
    check = doctor.check_trusted_proxies(_settings(
        tmp_path, TRUST_PROXY=False, REQUIRE_TRUSTED_PROXIES=True))
    assert check.status == doctor.PASS


def test_trusted_proxies_enforced_unparsable_is_fail(tmp_path):
    """名单非空但写坏（`not-an-ip`）：运行期 fail closed，配置层却会拦启动。"""
    check = doctor.check_trusted_proxies(_settings(
        tmp_path, TRUST_PROXY=True, TRUSTED_PROXIES="not-an-ip",
        REQUIRE_TRUSTED_PROXIES=True))
    assert check.status == doctor.FAIL
    assert "not-an-ip" in check.detail


def test_trusted_proxies_enforced_valid_is_pass(tmp_path):
    check = doctor.check_trusted_proxies(_settings(
        tmp_path, TRUST_PROXY=True, TRUSTED_PROXIES="172.31.250.0/24",
        REQUIRE_TRUSTED_PROXIES=True))
    assert check.status == doctor.PASS
    assert "172.31.250.0/24" in check.detail
    assert "REQUIRE_TRUSTED_PROXIES=true" in check.detail


# ------------------------------------------------------------- 迁移条目计数


def test_pending_migration_count_counts_each_kind(tmp_path, monkeypatch):
    """缺列 / 缺索引 / 该删的表 / 该删的列各算一条，缺表的整条跳过。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.execute("CREATE TABLE dead (x TEXT)")
    monkeypatch.setattr(doctor, "_MIGRATION_COLUMNS",
                        (("t", "b INTEGER"), ("missing_table", "x INTEGER")))
    monkeypatch.setattr(doctor, "_MIGRATION_INDEXES", (("idx_t", "t", "a"),))
    monkeypatch.setattr(doctor, "_MIGRATION_DROPS", ("dead", "never_existed"))
    monkeypatch.setattr(doctor, "_MIGRATION_COLUMN_DROPS",
                        (("t", "a"), ("missing_table", "x")))
    try:
        # 1 缺列 + 1 缺索引 + 1 待删表 + 1 待删列
        assert doctor.pending_migration_count(conn) == 4
    finally:
        conn.close()


def test_pending_migration_count_is_zero_for_fresh_schema(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    try:
        assert doctor.pending_migration_count(conn) == 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 数据库检查


def test_check_database_missing_is_warn(tmp_path):
    check = doctor.check_database(_settings(tmp_path))
    assert check.status == doctor.WARN


def test_check_database_unreadable_is_fail(tmp_path, monkeypatch):
    db = tmp_path / "coding2api.sqlite3"
    db.write_bytes(b"not a database")

    def boom(*_args, **_kwargs):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(doctor.sqlite3, "connect", boom)
    check = doctor.check_database(_settings(tmp_path))
    assert check.status == doctor.FAIL
    assert "无法读取" in check.detail


def test_check_database_integrity_failure_is_fail(tmp_path, monkeypatch):
    db = tmp_path / "coding2api.sqlite3"
    db.write_bytes(b"x")

    class FakeConn:
        def __init__(self) -> None:
            self._calls = 0

        def execute(self, sql: str):
            if "integrity_check" in sql:
                return self
            return self

        def fetchone(self):
            return ("*** in database main ***",)

        def close(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    monkeypatch.setattr(doctor.sqlite3, "connect", lambda *_a, **_k: FakeConn())
    check = doctor.check_database(_settings(tmp_path))
    assert check.status == doctor.FAIL
    assert "integrity_check" in check.detail


def test_check_database_newer_schema_is_fail(tmp_path):
    db = tmp_path / "coding2api.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    check = doctor.check_database(_settings(tmp_path))
    assert check.status == doctor.FAIL
    assert "高于本程序" in check.detail


def test_check_database_old_schema_is_warn(tmp_path):
    """老库（user_version=0）能读，但要说清「有迁移待执行」。"""
    db = tmp_path / "coding2api.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE users (id TEXT)")
    conn.commit()
    conn.close()
    check = doctor.check_database(_settings(tmp_path))
    assert check.status == doctor.WARN
    assert "迁移" in check.detail


def test_check_database_current_schema_is_pass(tmp_path):
    db = tmp_path / "coding2api.sqlite3"
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    conn.close()
    check = doctor.check_database(_settings(tmp_path))
    assert check.status == doctor.PASS
    assert f"{SCHEMA_VERSION}/{SCHEMA_VERSION}" in check.detail


# ------------------------------------------------------------------ 端口检查


def test_check_port_reports_in_use(tmp_path):
    """真实占用一个端口再探测：报 FAIL 并指向「已有进程占用」。"""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        check = doctor.check_port("127.0.0.1", port)
    finally:
        holder.close()
    assert check.status == doctor.FAIL
    assert "无法绑定" in check.detail


def _binder_raising(err: int):
    def binder(*_args, **_kwargs):
        raise OSError(err, "boom")

    return binder


def test_check_port_hint_for_address_in_use():
    check = doctor.check_port("127.0.0.1", 8000, _binder_raising(errno.EADDRINUSE))
    assert check.status == doctor.FAIL
    assert "已有进程占用" in check.detail


def test_check_port_hint_for_permission_denied_posix(monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "posix")
    check = doctor.check_port("127.0.0.1", 80, _binder_raising(errno.EACCES))
    assert "CAP_NET_BIND_SERVICE" in check.detail


def test_check_port_hint_for_permission_denied_windows(monkeypatch):
    """Windows 上多半是端口落在系统保留段，提示应当指向 netsh。"""
    monkeypatch.setattr(doctor.os, "name", "nt")
    check = doctor.check_port("127.0.0.1", 80, _binder_raising(errno.EACCES))
    assert "excludedportrange" in check.detail


def test_check_port_hint_for_other_error_on_windows(monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "nt")
    check = doctor.check_port("127.0.0.1", 8000, _binder_raising(errno.EADDRNOTAVAIL))
    assert "excludedportrange" in check.detail


def test_check_port_other_error_on_posix_has_no_hint(monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "posix")
    check = doctor.check_port("127.0.0.1", 8000, _binder_raising(errno.EADDRNOTAVAIL))
    assert "（" not in check.detail


def test_check_port_passes_when_bindable(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert doctor.check_port("127.0.0.1", port).status == doctor.PASS


# ---------------------------------------------------------------- 前端产物


def _dist_with(tmp_path: Path, html: str, files: tuple[str, ...] = ()) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(html, encoding="utf-8")
    for name in files:
        target = dist / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    return dist


def test_check_frontend_missing_dir_is_fail():
    assert doctor.check_frontend(None).status == doctor.FAIL


def test_check_frontend_missing_index_is_fail(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    check = doctor.check_frontend(dist)
    assert check.status == doctor.FAIL
    assert "build-web.sh" in check.detail


def test_check_frontend_detects_stale_index(tmp_path):
    """「落后一个 release 的白屏」：index.html 引用的 hash 文件已不在磁盘上。"""
    dist = _dist_with(
        tmp_path,
        '<script type="module" src="/assets/index-OLD.js"></script>',
        files=("assets/index-NEW.js",))
    check = doctor.check_frontend(dist)
    assert check.status == doctor.FAIL
    assert "index-OLD.js" in check.detail


def test_check_frontend_ok(tmp_path):
    dist = _dist_with(
        tmp_path,
        '<script src="/assets/index-abc.js"></script>'
        '<link rel="stylesheet" href="/assets/index-abc.css">',
        files=("assets/index-abc.js", "assets/index-abc.css"))
    assert doctor.check_frontend(dist).status == doctor.PASS


def test_index_assets_skips_external_and_inline():
    html = (
        '<script src="https://cdn.example.com/x.js"></script>'
        '<script src="//cdn.example.com/y.js"></script>'
        '<link href="data:text/css,h1">'
        '<link rel="icon" href="/favicon.svg">'
        '<script src="assets/app.js"></script>'
    )
    assert doctor._index_assets(html) == ["assets/app.js"]


# ------------------------------------------------------------ 上游连通性


def test_classify_upstream_error_timeout():
    assert doctor.classify_upstream_error(httpx.ConnectTimeout("t")) == "超时"
    assert doctor.classify_upstream_error(httpx.ReadTimeout("t")) == "超时"


def test_classify_upstream_error_tls_and_dns_direct():
    assert doctor.classify_upstream_error(ssl.SSLError("bad")) == "TLS 失败"
    assert doctor.classify_upstream_error(socket.gaierror("no")) == "DNS 失败"


def test_classify_upstream_error_connect_wrapped_causes():
    tls = httpx.ConnectError("boom")
    tls.__cause__ = ssl.SSLError("handshake")
    assert doctor.classify_upstream_error(tls) == "TLS 失败"

    dns = httpx.ConnectError("boom")
    dns.__cause__ = socket.gaierror("no")
    assert doctor.classify_upstream_error(dns) == "DNS 失败"

    other = httpx.ConnectError("boom")
    other.__cause__ = OSError("refused")
    assert doctor.classify_upstream_error(other) == "连接失败"


def test_classify_upstream_error_unknown():
    assert doctor.classify_upstream_error(ValueError("?")) == "未知错误"


class _FakeClient:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.urls: list[str] = []

    def head(self, url: str):
        self.urls.append(url)
        return httpx.Response(self.status, request=httpx.Request("HEAD", url))


def test_probe_upstream_ok_and_auth_required():
    status, detail = doctor.probe_upstream("https://a.example.com", _FakeClient(200))
    assert (status, detail) == (doctor.PASS, "HTTP 200")
    # 不带凭证拿到 401 说明链路是通的，不能当失败
    status, detail = doctor.probe_upstream("https://a.example.com", _FakeClient(401))
    assert status == doctor.PASS and "鉴权" in detail


def test_probe_upstream_server_error_is_warn():
    status, detail = doctor.probe_upstream("https://a.example.com", _FakeClient(503))
    assert status == doctor.WARN
    assert "503" in detail


def test_probe_upstream_uses_real_client_when_not_injected(monkeypatch):
    """不注入 client 时自己开一个短超时客户端并关掉它（不泄漏连接池）。"""
    closed: list[bool] = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            closed.append(True)
            return None

        def head(self, url: str):
            return httpx.Response(200, request=httpx.Request("HEAD", url))

    monkeypatch.setattr(doctor.httpx, "Client", FakeClient)
    status, _detail = doctor.probe_upstream("https://a.example.com")
    assert status == doctor.PASS
    assert closed == [True]


def test_probe_upstream_reports_failure_reason(monkeypatch):
    class Boom:
        def head(self, _url: str):
            raise httpx.ConnectError("refused")

    status, detail = doctor.probe_upstream("https://a.example.com", Boom())
    assert status == doctor.FAIL
    assert detail == "连接失败"


def test_check_upstreams_returns_one_check_per_endpoint():
    seen: list[str] = []

    def prober(url: str) -> tuple[str, str]:
        seen.append(url)
        return doctor.PASS, "HTTP 200"

    checks = doctor.check_upstreams(
        [("a", "https://a.example.com"), ("b", "https://b.example.com")], prober)
    assert [c.name for c in checks] == ["上游 a", "上游 b"]
    assert seen == ["https://a.example.com", "https://b.example.com"]


def test_check_ok_property_ignores_warn_but_not_fail():
    assert doctor.Check("x", doctor.PASS).ok is True
    assert doctor.Check("x", doctor.WARN).ok is True
    assert doctor.Check("x", doctor.FAIL).ok is False
