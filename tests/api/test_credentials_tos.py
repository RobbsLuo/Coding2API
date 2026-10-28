"""渠道 ToS 风险登记：导入时返回评级、列表页透出全量、审计留痕。"""

from __future__ import annotations

import logging

from src.engine.model_resolver import KNOWN_PROVIDERS
from src.provider.tos import CHANNEL_TOS, tos_for


def test_every_known_provider_is_registered():
    """新增渠道必须同时登记风险等级（模块导入时已强校验，这里兜住重构）。"""
    assert set(KNOWN_PROVIDERS) == set(CHANNEL_TOS)


def test_unknown_provider_falls_back_to_gray():
    risk = tos_for("nope")
    assert risk.level == "gray"
    assert "未登记" in risk.basis


def test_known_levels():
    assert tos_for("zen").level == "green"
    assert tos_for("kilo").level == "green"
    for provider in ("codebuddy", "codebuddy_intl", "trae", "qoder",
                     "codearts", "officeace"):
        assert tos_for(provider).level == "gray"
        assert tos_for(provider).reason


def test_import_returns_tos_and_audits_level(admin_client, caplog):
    app, client = admin_client
    with caplog.at_level(logging.WARNING):
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
    assert created.status_code == 200
    tos = created.json()["tos"]
    assert tos["level"] == "gray"
    assert tos["reason"]
    # 灰区导入在日志里留一条（列表页走脚本批量导入时也能事后追溯）
    assert any("ToS 评级 gray" in r.getMessage() for r in caplog.records)

    audit = app.state.audit.query(limit=20)
    row = next(item for item in audit if item["action"] == "credential.import")
    assert "合规=gray" in row["detail"]


def test_green_import_does_not_warn(admin_client, caplog):
    """green 渠道照常导入，不产生灰区告警日志。"""
    app, client = admin_client
    with caplog.at_level(logging.WARNING):
        created = client.post("/api/credentials", json={
            "provider": "zen", "credential": {}})
    assert created.status_code == 200
    assert created.json()["tos"]["level"] == "green"
    assert not any("ToS 评级" in r.getMessage() for r in caplog.records)


def test_credentials_list_exposes_all_risks(admin_client):
    _app, client = admin_client
    payload = client.get("/api/credentials").json()
    risks = payload["tos_risks"]
    assert set(risks) == set(KNOWN_PROVIDERS)
    assert risks["zen"]["level"] == "green"
    assert risks["trae"]["level"] == "gray"
    assert all(item["reason"] for item in risks.values())
