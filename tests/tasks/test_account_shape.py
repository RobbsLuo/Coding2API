"""账号形态判定（企业版不跑个人版任务）。"""

from __future__ import annotations

from src.tasks.account_shape import is_enterprise_account


def test_enterprise_quota_mode_detected():
    assert is_enterprise_account({"quota_probe_mode": "enterprise"}) is True


def test_personal_or_missing_mode_is_not_enterprise():
    assert is_enterprise_account({"quota_probe_mode": "personal"}) is False
    assert is_enterprise_account({"bearer_token": "t"}) is False
    assert is_enterprise_account({}) is False


def test_non_dict_credential_is_not_enterprise():
    """凭证解不开 / 形状异常时按「不是企业号」处理：宁可多跑一轮，不静默漏签。"""
    assert is_enterprise_account(None) is False
    assert is_enterprise_account("nope") is False  # type: ignore[arg-type]