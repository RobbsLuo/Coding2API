"""备份管理端点：列表 / 立即备份 / 下载 / 删除 / 上传恢复与启动应用。"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.backup import backup_dir, pending_restore_path, stage_restore
from src.main import build_app
from tests.helpers.constants import SECRET


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                    ADMIN_USERNAMES="root")


@pytest.fixture()
def clean_backups(admin_client):
    """`(app, client)`，并清掉启动备份任务刚落下的那份。

    `TaskRunner` 启动时会立即跑一轮备份（避免短命进程永远不备份），于是
    「备份目录初始为空」不再是前提；这些用例断言的是列表/删除语义本身。
    """
    app, client = admin_client
    shutil.rmtree(backup_dir(app.state.settings.db_path), ignore_errors=True)
    return app, client


def test_backups_require_admin(tmp_path):
    app = build_app(_settings(tmp_path))
    with TestClient(app) as guest:
        guest.cookies.set("coding2api_session", create_session_token("guest", SECRET))
        assert guest.get("/api/backups").status_code == 403
        assert guest.post("/api/backups").status_code == 403


def test_list_create_download_delete(clean_backups):
    app, client = clean_backups
    directory = backup_dir(app.state.settings.db_path)

    assert client.get("/api/backups").json() == {"backups": [], "pending": None}

    created = client.post("/api/backups")
    assert created.status_code == 200
    name = created.json()["backup"]["name"]
    assert name.endswith(".sqlite3") and (directory / name).is_file()

    listed = client.get("/api/backups").json()["backups"]
    assert [item["name"] for item in listed] == [name]
    assert listed[0]["size"] > 0

    download = client.get(f"/api/backups/{name}")
    assert download.status_code == 200
    assert download.content.startswith(b"SQLite format 3")

    assert client.delete(f"/api/backups/{name}").json() == {"ok": True}
    assert client.get("/api/backups").json()["backups"] == []
    # 再删同一条：已不存在 → 400
    assert client.delete(f"/api/backups/{name}").status_code == 400


def test_download_and_delete_reject_illegal_names(admin_client):
    _app, client = admin_client
    # 路径分隔符要编码后才会进入 handler（未编码的 `..` 会被路由归一成 404）
    for bad in ("notes.txt", "missing.sqlite3", "%2E%2E", "..%5Cx.sqlite3"):
        assert client.get(f"/api/backups/{bad}").status_code == 400
        assert client.delete(f"/api/backups/{bad}").status_code == 400


def test_restore_upload_validates_and_stages(admin_client):
    app, client = admin_client
    created = client.post("/api/backups").json()["backup"]["name"]
    content = client.get(f"/api/backups/{created}").content

    response = client.post(
        "/api/backups/restore?filename=my-backup.sqlite3",
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["staged"] is True and body["restart_required"] is True
    assert body["filename"] == "my-backup.sqlite3"
    pending = pending_restore_path(app.state.settings.db_path)
    assert pending.is_file()
    assert client.get("/api/backups").json()["pending"]["filename"] == "my-backup.sqlite3"


def test_restore_upload_rejects_bad_payloads(admin_client):
    app, client = admin_client
    assert client.post("/api/backups/restore", content=b"",
                       headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert client.post("/api/backups/restore", content=b"not a database",
                       headers={"Content-Type": "application/octet-stream"}).status_code == 400
    # 能打开、缺本服务核心表的 sqlite 也不能恢复
    foreign = app.state.settings.db_path + ".foreign"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.commit()
    conn.close()
    assert client.post("/api/backups/restore", content=Path(foreign).read_bytes(),
                       headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert pending_restore_path(app.state.settings.db_path).exists() is False


def test_pending_restore_applies_on_next_startup(tmp_path):
    settings = _settings(tmp_path)
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        client.put("/api/settings", json={"values": {"alert_interval_minutes": 9}})
        backup = client.post("/api/backups").json()["backup"]["name"]
        content = client.get(f"/api/backups/{backup}").content
        client.put("/api/settings", json={"values": {"alert_interval_minutes": 3}})

    # 模拟「上传备份 → 重启」：直接把备份放进待恢复位置
    stage_restore(settings.db_path, content, filename=backup)
    restarted = build_app(settings)
    with TestClient(restarted) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        values = {item["key"]: item["value"]
                  for item in client.get("/api/settings").json()["settings"]}
        assert values["alert_interval_minutes"] == 9
        assert pending_restore_path(settings.db_path).exists() is False


def test_list_when_backup_dir_is_gone(clean_backups):
    """备份目录被人工删掉：列表返回空数组，不报 500。"""
    app, client = clean_backups
    assert client.get("/api/backups").json()["backups"] == []
    shutil.rmtree(backup_dir(app.state.settings.db_path), ignore_errors=True)
    assert client.get("/api/backups").json()["backups"] == []


def test_create_backup_failure_is_reported(admin_client, monkeypatch):
    """写盘失败（磁盘满等）→ 400 + 原始原因，不是 500。"""
    from src.api import admin_backups

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(admin_backups, "backup_database", boom)
    _app, client = admin_client
    response = client.post("/api/backups")
    assert response.status_code == 400
    assert "disk full" in response.json()["error"]["message"]


def test_restore_upload_rejects_oversized_body(admin_client, monkeypatch):
    from src.api import admin_backups

    monkeypatch.setattr(admin_backups, "MAX_UPLOAD_BYTES", 1)
    _app, client = admin_client
    response = client.post("/api/backups/restore", content=b"ab",
                           headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 400
    assert "不能超过" in response.json()["error"]["message"]


def test_restore_upload_streams_and_stops_on_overflow(tmp_path, monkeypatch):
    """分块上传时边读边写临时文件：超限立即中断，剩余 chunk 不再读。"""
    import asyncio

    from src.api import admin_backups
    from src.compat.openai.request import InvalidRequest

    consumed: list[bytes] = []
    target = tmp_path / "upload.bin"

    class FakeRequest:
        async def stream(self):
            for chunk in (b"aa", b"bb", b"cc"):
                consumed.append(chunk)
                yield chunk

    with pytest.raises(InvalidRequest) as excinfo:
        asyncio.run(admin_backups._stream_to_file(FakeRequest(), target, 3))
    assert "不能超过" in str(excinfo.value)
    assert consumed == [b"aa", b"bb"]       # 第二段超限即停
    # 超限的那一块不落盘（先判长度再写），文件里只有已接受的段
    assert target.read_bytes() == b"aa"


def test_stream_to_file_writes_private_temp_file(tmp_path):
    """正常路径：逐块落盘、累计字节数正确、文件权限 0600。"""
    import asyncio
    import stat as _stat

    from src.api import admin_backups

    target = tmp_path / "upload.bin"

    class FakeRequest:
        async def stream(self):
            for chunk in (b"ab", b"cd"):
                yield chunk

    assert asyncio.run(admin_backups._stream_to_file(FakeRequest(), target, 100)) == 4
    assert target.read_bytes() == b"abcd"
    assert _stat.S_IMODE(target.stat().st_mode) == 0o600


def test_restore_upload_overflow_leaves_no_residue(admin_client, monkeypatch):
    """超限被拒：不留 pending，也不留半截 `.uploading` 临时文件。"""
    from src.api import admin_backups

    monkeypatch.setattr(admin_backups, "MAX_UPLOAD_BYTES", 8)
    app, client = admin_client
    response = client.post("/api/backups/restore", content=b"x" * 9,
                           headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 400
    assert "不能超过" in response.json()["error"]["message"]
    restore_dir = pending_restore_path(app.state.settings.db_path).parent
    assert pending_restore_path(app.state.settings.db_path).exists() is False
    assert list(restore_dir.glob("*.uploading")) == []


def test_restore_upload_empty_body_leaves_no_residue(admin_client):
    """空 body 与旧口径一致（400 + 同一文案），且不留临时文件。"""
    app, client = admin_client
    response = client.post("/api/backups/restore", content=b"",
                           headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 400
    assert "上传内容为空" in response.json()["error"]["message"]
    restore_dir = pending_restore_path(app.state.settings.db_path).parent
    assert list(restore_dir.glob("*.uploading")) == []


def test_restore_upload_validation_failure_leaves_no_residue(admin_client):
    """校验失败（不是本服务的库）：400 + 无 pending、无临时文件。"""
    app, client = admin_client
    response = client.post("/api/backups/restore", content=b"not a database",
                           headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 400
    restore_dir = pending_restore_path(app.state.settings.db_path).parent
    assert pending_restore_path(app.state.settings.db_path).exists() is False
    assert list(restore_dir.glob("*.uploading")) == []
    assert (restore_dir / "pending.json").exists() is False


def test_restore_upload_meta_matches_stage_restore_shape(admin_client, tmp_path):
    """落盘结果与旧 `stage_restore` 路径同形：meta 字段与 pending 内容一致。"""
    import json

    app, client = admin_client
    created = client.post("/api/backups").json()["backup"]["name"]
    content = client.get(f"/api/backups/{created}").content

    response = client.post("/api/backups/restore?filename=my-backup.sqlite3",
                           content=content,
                           headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 200

    pending = pending_restore_path(app.state.settings.db_path)
    meta = json.loads((pending.parent / "pending.json").read_text(encoding="utf-8"))
    assert set(meta) == {"filename", "size", "uploaded_at"}
    assert meta["filename"] == "my-backup.sqlite3"
    assert meta["size"] == len(content) == pending.stat().st_size
    assert isinstance(meta["uploaded_at"], int) and meta["uploaded_at"] > 0
    # 响应里的 meta 与落盘 meta 同值（前端提示与重启后展示口径一致）
    body = response.json()
    assert {key: body[key] for key in meta} == meta

    # 与 `stage_restore` 同形：字段名、类型、pending 文件名都对得上
    stage_restore(app.state.settings.db_path, content, filename="other.sqlite3")
    staged = json.loads((pending.parent / "pending.json").read_text(encoding="utf-8"))
    assert set(staged) == set(meta)
    assert pending.name == "pending.sqlite3"


def test_restore_upload_endpoint_uses_streamed_bytes(admin_client, monkeypatch):
    """端点改走流式读取：上限判断用的是累计长度，不再 `request.body()` 全量。"""
    from src.api import admin_backups

    monkeypatch.setattr(admin_backups, "MAX_UPLOAD_BYTES", 4)
    _app, client = admin_client
    response = client.post("/api/backups/restore", content=b"abcdef",
                           headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 400
    assert "不能超过" in response.json()["error"]["message"]


def test_restore_upload_accepts_chunked_within_limit(admin_client):
    """分块读取后仍按原逻辑暂存（内容校验不受影响）。"""
    app, client = admin_client
    created = client.post("/api/backups").json()["backup"]["name"]
    content = client.get(f"/api/backups/{created}").content

    def body():
        for start in range(0, len(content), 4096):
            yield content[start:start + 4096]

    response = client.post("/api/backups/restore", content=body(),
                           headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 200
    assert pending_restore_path(app.state.settings.db_path).is_file()


def test_list_skips_non_files_and_symlinks(clean_backups, tmp_path):
    """备份目录里混入子目录 / 符号链接时不展示（也不跟随链接）。"""
    app, client = clean_backups
    directory = backup_dir(app.state.settings.db_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sub.sqlite3").mkdir()
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"x")
    (directory / "link.sqlite3").symlink_to(outside)

    assert client.get("/api/backups").json()["backups"] == []
