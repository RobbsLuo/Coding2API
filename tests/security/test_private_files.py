"""`src.security.private_files`：POSIX chmod 与 Windows ACL 两种收口路径。

Windows 分支在本机（macOS）与 CI（Linux）上都跑不到，因此一律 monkeypatch
`_is_windows` / `subprocess.run` 把两条路径测全——不用 pragma 掩盖未测分支。
"""

from __future__ import annotations

import stat
import subprocess

import pytest

from src.security import private_files as pf


def _write(tmp_path, name: str = "secret.txt"):
    path = tmp_path / name
    path.write_text("x", encoding="utf-8")
    return path


# --------------------------------------------------------------- POSIX 路径


def test_secure_file_tightens_posix_mode(tmp_path):
    """POSIX 收紧后读回校验通过，返回 True。"""
    path = _write(tmp_path)
    path.chmod(0o644)
    assert pf.secure_file(path) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_secure_dir_tightens_posix_mode(tmp_path):
    target = tmp_path / "data"
    target.mkdir()
    target.chmod(0o755)
    assert pf.secure_dir(target) is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_secure_warns_when_chmod_denied(tmp_path, monkeypatch, caplog):
    """chmod 被拒（共享目录 / 他人属主）时必须告警而不是静默吞掉。"""
    path = _write(tmp_path)

    def boom(_path, _mode):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(pf.os, "chmod", boom)
    with caplog.at_level("WARNING"):
        assert pf.secure_file(path) is False
    assert "无法收紧" in caplog.text
    assert "operation not permitted" in caplog.text


def test_secure_strict_raises_when_not_private(tmp_path, monkeypatch):
    """`strict=True` 用于 `data/`：启动前必须成立的信任边界，不成立就阻断。"""
    path = _write(tmp_path)

    def boom(_path, _mode):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(pf.os, "chmod", boom)
    with pytest.raises(pf.PrivateAccessError) as excinfo:
        pf.secure_file(path, strict=True)
    assert "无法收紧" in str(excinfo.value)


def test_secure_strict_passes_when_already_private(tmp_path, monkeypatch):
    """收紧失败但目录**本来**就是私有：信任边界成立，不该误拦启动。"""
    path = _write(tmp_path)
    path.chmod(0o600)

    def boom(_path, _mode):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(pf.os, "chmod", boom)
    assert pf.secure_file(path, strict=True) is True


def test_secure_reports_unreadable_after_tightening(tmp_path, monkeypatch, caplog):
    """收紧后 stat 也失败（路径刚被删/权限不足）时给描述而不是崩。"""
    path = _write(tmp_path)

    def boom(_self):
        raise OSError("stat failed")

    monkeypatch.setattr(pf.Path, "stat", boom)
    with caplog.at_level("WARNING"):
        assert pf.secure_file(path) is False
    assert "stat failed" in caplog.text


def test_check_private_reports_world_readable(tmp_path):
    path = _write(tmp_path)
    path.chmod(0o644)
    problem = pf.check_private(path, directory=False)
    assert problem is not None
    assert "0644" in problem


def test_check_private_accepts_owner_only(tmp_path):
    path = _write(tmp_path)
    path.chmod(0o600)
    assert pf.check_private(path, directory=False) is None


def test_check_private_missing_path_returns_message(tmp_path):
    """路径不存在返回描述而不是抛错：doctor 要能继续往下跑。"""
    problem = pf.check_private(tmp_path / "nope", directory=True)
    assert problem is not None and "不存在" in problem


def test_check_private_wraps_oserror(tmp_path, monkeypatch):
    """stat 失败（权限不足）折算成描述，不让 doctor 崩。"""
    path = _write(tmp_path)

    def boom(_self):
        raise OSError("stat failed")

    monkeypatch.setattr(pf.Path, "exists", lambda _self: True)
    monkeypatch.setattr(pf.Path, "stat", boom)
    problem = pf.check_private(path, directory=True)
    assert problem is not None and "stat failed" in problem


# ------------------------------------------------------------- Windows 路径


@pytest.fixture()
def windows(monkeypatch):
    """把模块切到 Windows 语义，并给出可控的 `icacls` 替身。"""
    monkeypatch.setattr(pf, "_is_windows", lambda: True)
    # `C:/data` 在 macOS/Linux 上不存在：这些用例只测 ACL 解析与调用形态，
    # 路径存在性不在被测范围内。
    monkeypatch.setattr(pf.Path, "exists", lambda _self: True)
    monkeypatch.setenv("USERNAME", "robbs")

    class FakeIcacls:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            # 默认给一份「收紧成功」后的读回输出：仅当前用户 + SYSTEM
            self.stdout = (
                "C:\\data ROBBS-PC\\robbs:(OI)(CI)(F)\r\n"
                "        NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\r\n"
            )
            self.error: Exception | None = None

        def __call__(self, args):
            self.calls.append(args)
            if self.error is not None:
                raise self.error
            return subprocess.CompletedProcess(args, 0, self.stdout, "")

    fake = FakeIcacls()
    monkeypatch.setattr(pf, "_run_icacls", fake)
    return fake


def test_acl_violations_flags_inherited_and_foreign():
    """继承 ACE 与多余主体都要报出来（issue #45 的实测输出形态）。"""
    text = (
        "\r\n"
        "C:\\data\\coding2api.sqlite3 Everyone:(F)\r\n"
        "                                NT AUTHORITY\\Authenticated Users:(I)(M)\r\n"
        "                                BUILTIN\\Users:(I)(RX)\r\n"
        "                                ROBBS-PC\\robbs:(F)\r\n"
        "                                NT AUTHORITY\\SYSTEM:(F)\r\n"
        "\r\n"
        "成功处理 1 个文件；处理 0 个文件时失败\r\n"
    )
    problems = pf.acl_violations(text, "robbs", r"C:\data\coding2api.sqlite3")
    # 继承来的两条（Authenticated Users / Users）都算违规
    assert sum("继承 ACE" in item for item in problems) == 2
    # 显式的 Everyone 是「多余主体」（非继承）
    assert any("多余主体 everyone" in item for item in problems)
    # 自身（带机器名前缀）与 SYSTEM 不该被报
    assert not any("robbs:(f)" in item for item in problems)
    assert not any("system:(f)" in item for item in problems)


def test_acl_violations_ignores_localized_summary_lines():
    """不含 ACE 形态的汇总行（含本地化文案）不能被误判成主体。"""
    problems = pf.acl_violations(
        "C:\\data\\a.txt ROBBS-PC\\robbs:(F)\n已成功处理 1 个文件\n", "robbs")
    assert problems == []


def test_acl_violations_handles_principal_less_residue():
    """形如 `:(F)` 的残行没有主体，跳过而不是报一个空主体。"""
    assert pf.acl_violations(":(F)\n", "robbs") == []


def test_secure_dir_windows_grants_only_self_and_system(windows):
    assert pf.secure_dir("C:/data") is True
    args = windows.calls[0]
    assert args[0] == "C:/data"
    assert "/inheritance:r" in args
    assert "robbs:(OI)(CI)F" in args
    assert "SYSTEM:(OI)(CI)F" in args


def test_secure_file_windows_omits_inherit_flags(windows):
    """文件不需要 (OI)(CI)，只给自己与 SYSTEM 完全控制。"""
    assert pf.secure_file("C:/data/a.txt") is True
    args = windows.calls[0]
    assert "robbs:F" in args
    assert "SYSTEM:F" in args


def test_secure_windows_warns_when_acl_still_wide(windows, caplog):
    """收紧后读回仍见继承 ACE：告警（不静默）。"""
    windows.stdout = "C:\\data Everyone:(I)(F)\n"
    with caplog.at_level("WARNING"):
        assert pf.secure_dir("C:/data") is False
    assert "多余主体" in caplog.text or "继承 ACE" in caplog.text


def test_secure_windows_warns_when_icacls_fails(windows, caplog):
    windows.error = pf.PrivateAccessError("找不到 icacls（Windows 系统自带工具）")
    with caplog.at_level("WARNING"):
        assert pf.secure_dir("C:/data") is False
    assert "icacls" in caplog.text


def test_secure_windows_reports_unparsable_acl(windows):
    """`icacls` 只回了报错/汇总行（没有可解析 ACE）时按不通过处理。"""
    windows.stdout = "icacls 失败：拒绝访问\n"
    assert pf.secure_file("C:/data/a.txt") is False


def test_secure_windows_strict_raises(windows):
    """`strict=True` 时 Windows 收紧失败直接抛（`data/` 的启动门禁）。"""
    windows.error = pf.PrivateAccessError("拒绝访问")
    with pytest.raises(pf.PrivateAccessError):
        pf.secure_dir("C:/data", strict=True)


def test_secure_windows_requires_username(windows, monkeypatch, caplog):
    """USERNAME 缺失时不能瞎授权；若目录本就不私有，必须判失败。"""
    monkeypatch.delenv("USERNAME", raising=False)
    windows.stdout = "C:\\data Everyone:(I)(F)\n"
    with caplog.at_level("WARNING"):
        assert pf.secure_file("C:/data/a.txt") is False
    assert "用户名" in caplog.text
    # 未授权任何主体：第一次调用是读回校验，不该有 /grant:r
    assert all("/grant:r" not in args for args in windows.calls)


def test_run_icacls_raises_private_error_on_failure(monkeypatch):
    """非零退出换算成 PrivateAccessError，带上 stderr 摘要。"""

    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(5, args, output="", stderr="拒绝访问")

    monkeypatch.setattr(pf.subprocess, "run", fake_run)
    with pytest.raises(pf.PrivateAccessError) as excinfo:
        pf._run_icacls(["C:/data"])
    assert "拒绝访问" in str(excinfo.value)


def test_run_icacls_falls_back_to_exit_code(monkeypatch):
    """stderr 与 stdout 都空时至少给出退出码。"""

    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(5, args, output="", stderr="")

    monkeypatch.setattr(pf.subprocess, "run", fake_run)
    with pytest.raises(pf.PrivateAccessError) as excinfo:
        pf._run_icacls(["C:/data"])
    assert "退出码 5" in str(excinfo.value)


def test_run_icacls_reports_missing_binary(monkeypatch):
    def fake_run(_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(pf.subprocess, "run", fake_run)
    with pytest.raises(pf.PrivateAccessError) as excinfo:
        pf._run_icacls(["C:/data"])
    assert "icacls" in str(excinfo.value)


def test_run_icacls_invokes_system_tool(monkeypatch):
    """真实调用形态：参数逐项传入（不拼 shell），UTF-8 容错解码。"""
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(pf.subprocess, "run", fake_run)
    result = pf._run_icacls(["C:/data"])
    assert result.stdout == "ok"
    assert captured["args"] == ["icacls", "C:/data"]
    assert captured["check"] is True
    assert captured["errors"] == "replace"


def test_check_private_windows_delegates_to_icacls(windows):
    windows.stdout = "C:\\data ROBBS-PC\\robbs:(OI)(CI)(F)\n"
    assert pf.check_private("C:/data", directory=True) is None


def test_check_private_windows_reports_icacls_failure(windows):
    windows.error = pf.PrivateAccessError("icacls 执行失败: 拒绝访问")
    problem = pf.check_private("C:/data", directory=True)
    assert problem is not None and "拒绝访问" in problem


def test_check_private_windows_reports_violations(windows):
    windows.stdout = "C:\\data Everyone:(I)(F)\n"
    problem = pf.check_private("C:/data", directory=True)
    assert problem is not None and "Everyone" in problem
