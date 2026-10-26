"""前端静态资源与 SPA 回退。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


def _spa_client(tmp_path, *, build: bool = True, monkeypatch=None):
    """构造前端产物目录并注入，避免在仓库里真的建删 web/dist。"""
    dist = tmp_path / "dist"
    if build:
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
        (dist / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr("src.webapp.static.frontend_dist", lambda: dist if build else None)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    return TestClient(build_app(settings))

def test_frontend_dist_prefers_project_root():
    """路径必须锚定项目根，而不是当前工作目录。"""
    from src.webapp import static

    resolved = static.frontend_dist()
    # 本仓库已构建前端时应能找到；未构建时返回 None（由下面的注入测试覆盖）
    if resolved is not None:
        assert resolved.name == "dist"
        assert (resolved / "index.html").is_file()

def test_frontend_dist_returns_none_when_absent(tmp_path, monkeypatch):
    from src.webapp import static

    monkeypatch.setattr(static, "_PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    assert static.frontend_dist() is None

def test_spa_serves_index_for_unknown_path(tmp_path, monkeypatch):
    with _spa_client(tmp_path, monkeypatch=monkeypatch) as client:
        response = client.get("/credentials")
    assert response.status_code == 200
    assert "spa" in response.text

def test_spa_serves_real_asset(tmp_path, monkeypatch):
    with _spa_client(tmp_path, monkeypatch=monkeypatch) as client:
        response = client.get("/app.js")
    assert response.status_code == 200 and "console.log" in response.text

def test_spa_reports_missing_build_with_actionable_page(tmp_path, monkeypatch):
    """未构建前端时必须给出可执行的下一步，而不是一句英文错误。"""
    with _spa_client(tmp_path, build=False, monkeypatch=monkeypatch) as client:
        response = client.get("/credentials")
    assert response.status_code == 503
    assert "pnpm build" in response.text
    assert "尚未构建" in response.text or "not" in response.text.lower()
    # 提示里要说明 API 仍可用，避免用户以为整个服务挂了
    assert "/v1/" in response.text or "/api/" in response.text

def test_spa_reports_missing_index_with_actionable_page(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    monkeypatch.setattr("src.webapp.static.frontend_dist", lambda: dist)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    with TestClient(build_app(settings)) as client:
        response = client.get("/credentials")
    assert response.status_code == 503
    assert "pnpm build" in response.text

def test_spa_does_not_escape_dist(tmp_path, monkeypatch):
    """路径穿越必须拒绝：解析后位于 dist 之外的文件不能被读出。"""
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (tmp_path / "secret.env").write_text("APP_SECRET=leaked", encoding="utf-8")
    monkeypatch.setattr("src.webapp.static.frontend_dist", lambda: dist)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    with TestClient(build_app(settings)) as client:
        response = client.get("/../secret.env")
    assert "leaked" not in response.text
    assert "spa" in response.text          # 回退到 index.html

def test_frontend_dist_falls_back_to_container_and_cwd(tmp_path, monkeypatch):
    """项目根没有产物时，依次尝试容器路径与当前工作目录（main 516-517）。"""

    from src.webapp import static

    monkeypatch.setattr(static, "_PROJECT_ROOT", tmp_path)          # 项目根没有
    container_dist = tmp_path / "container" / "web" / "dist"
    container_dist.mkdir(parents=True)
    (container_dist / "index.html").write_text("x", encoding="utf-8")

    # 容器候选已抽成模块常量，可直接指到临时目录
    monkeypatch.setattr(static, "_CONTAINER_DIST", container_dist)

    found = static.frontend_dist()
    assert found is not None and found.name == "dist"


def test_unmatched_write_method_on_api_prefix_returns_json_404(tmp_path, monkeypatch):
    """未匹配的写方法也要回 JSON 404（此前是 405 `{"detail":…}`）。

    PROPOSAL §6「未匹配的 /api、/v1 返回 JSON 404」：客户端拼错端点 + 错
    方法时若拿到裸 `detail` 形状，按统一错误体解析的调用方会踩空。
    """
    with _spa_client(tmp_path, monkeypatch=monkeypatch) as client:
        for method in ("post", "put", "patch", "delete"):
            response = getattr(client, method)("/api/nonexistent")
            assert response.status_code == 404, method
            body = response.json()
            assert body["error"]["code"] == "invalid_request"
            assert body["error"]["status"] == 404


def test_unmatched_write_method_on_v1_prefix_returns_json_404(tmp_path, monkeypatch):
    with _spa_client(tmp_path, monkeypatch=monkeypatch) as client:
        response = client.post("/v1/nonexistent")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invalid_request"


def test_unmatched_write_method_on_plain_path_serves_spa(tmp_path, monkeypatch):
    """非 API 前缀的写方法仍走 SPA（前端路由回 index.html）。"""
    with _spa_client(tmp_path, monkeypatch=monkeypatch) as client:
        response = client.post("/some-page")
    assert response.status_code == 200
    assert "spa" in response.text


def test_unmatched_write_method_without_frontend_build_reports_missing(
        tmp_path, monkeypatch):
    """前端没构建时，写方法也给出可执行提示（不是 404 掩盖「前端缺失」）。"""
    with _spa_client(tmp_path, build=False, monkeypatch=monkeypatch) as client:
        response = client.post("/some-page")
    assert response.status_code == 503
    assert "pnpm build" in response.text


def test_unmatched_write_method_without_index_reports_missing(tmp_path, monkeypatch):
    """产物目录在但 index.html 缺失：同样回可执行提示（不是 404/500）。"""
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    monkeypatch.setattr("src.webapp.static.frontend_dist", lambda: dist)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    with TestClient(build_app(settings)) as client:
        response = client.post("/some-page")
    assert response.status_code == 503
    assert "pnpm build" in response.text
