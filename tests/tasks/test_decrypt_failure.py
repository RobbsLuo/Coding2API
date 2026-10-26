"""解不开的凭证（APP_SECRET 换过 / 密文损坏）**不得连累整轮任务**。

**背景（2026-10-26 14:48 实测）**：一条凭证的密文解不开时，`credential_data`
抛 `CredentialDecryptError` → 早先直接冒泡 → 整轮后台任务（额度探测、预刷新、
签到、成长中心、活跃上报、模型探活）挂掉，其余好凭证一条都没跑。

各任务的口径一致：**跳过这条凭证**（记 skipped + 一条 ERROR 日志），其余
照常。这组用例把六条路径一起钉住。
"""

from __future__ import annotations

from src.db.crypto import CredentialCipher, CredentialDecryptError
from src.db.repo import GrowthRepository, ModelProbeRepository
from src.tasks.activity import ActivityTask
from src.tasks.checkin import CheckinTask
from src.tasks.growth import GrowthTask
from src.tasks.model_probe import ModelProbeTask
from src.tasks.quota_probe import QuotaProbeTask
from src.tasks.refresh import RefreshTask
from tests.tasks._support import ProbeProvider


def _break_ciphertext(credentials, db, credential_id: str) -> None:
    """把该凭证的密文换成另一个密钥加密的（当前 APP_SECRET 解不开）。"""
    other = CredentialCipher("a-completely-different-secret")
    with db.transaction() as conn:
        conn.execute("UPDATE credentials SET data_enc = ? WHERE id = ?",
                     (other.encrypt(b'{"bearer_token": "t"}'), credential_id))


async def test_quota_probe_skips_undecryptable_credential(repo, caplog):
    credentials, db = repo
    good = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    bad = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t2"})
    _break_ciphertext(credentials, db, bad)

    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()})
    with caplog.at_level("ERROR", logger="src.tasks.quota_probe"):
        report = await task.run_once()
    # 坏的那条跳过，好的那条照常探测
    assert report.skipped == 1
    assert report.succeeded == 1
    assert credentials.candidates()[0].credential_id == good


async def test_refresh_skips_undecryptable_credential(repo, caplog):
    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})
    bad = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})
    _break_ciphertext(credentials, db, bad)

    task = RefreshTask(credentials, {"codebuddy": ProbeProvider(refresh_new="NEW")},
                       skew_seconds=3600, now=lambda: 1000)
    with caplog.at_level("ERROR", logger="src.tasks.refresh"):
        report = await task.run_once()
    assert report.skipped >= 1


async def test_checkin_skips_undecryptable_credential(repo, caplog):
    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    bad = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t2"})
    _break_ciphertext(credentials, db, bad)

    task = CheckinTask(credentials, {"codebuddy": ProbeProvider()})
    with caplog.at_level("ERROR", logger="src.tasks.checkin"):
        report = await task.run_once()
    assert report.skipped >= 1


async def test_growth_skips_undecryptable_credential(repo, caplog):
    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    bad = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t2"})
    _break_ciphertext(credentials, db, bad)

    task = GrowthTask(credentials, {"codebuddy": ProbeProvider()},
                      GrowthRepository(db))
    with caplog.at_level("ERROR", logger="src.tasks.growth"):
        report = await task.run_once()
    assert report.skipped >= 1


async def test_activity_skips_undecryptable_credential(repo, caplog):
    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    bad = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t2"})
    _break_ciphertext(credentials, db, bad)

    task = ActivityTask(credentials, {"codebuddy": ProbeProvider()})
    with caplog.at_level("ERROR", logger="src.tasks.activity"):
        report = await task.run_once()
    assert report.skipped >= 1


async def test_model_probe_skips_undecryptable_credential(repo, caplog):
    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    bad = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t2"})
    _break_ciphertext(credentials, db, bad)

    task = ModelProbeTask(credentials, {"codebuddy": ProbeProvider()},
                           ModelProbeRepository(db))
    with caplog.at_level("ERROR", logger="src.tasks.model_probe"):
        await task.run_once()
    # 不抛异常即达标：早先直接冒泡让整个探活任务挂掉


async def test_model_probe_uses_readable_credential_after_undecryptable_one(repo):
    """第一个候选解不开 → 继续找下一个可用的（不是整条渠道放弃）。"""
    credentials, db = repo
    bad = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    good = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t2"})
    _break_ciphertext(credentials, db, bad)

    task = ModelProbeTask(credentials, {"codebuddy": ProbeProvider()},
                           ModelProbeRepository(db))
    data = task._probe_data("codebuddy")
    assert data is not None
    assert data["bearer_token"] == "t2"
    assert good != bad


async def test_growth_skips_undecryptable_credential_before_data_read(repo, caplog):
    """成长中心：渠道无 growth 能力时先判能力、再读凭证（解不开的那条跳过）。"""
    credentials, db = repo
    bad = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    _break_ciphertext(credentials, db, bad)

    class NoGrowth:
        id = "codebuddy"

    task = GrowthTask(credentials, {"codebuddy": NoGrowth()},
                      GrowthRepository(db))
    with caplog.at_level("ERROR", logger="src.tasks.growth"):
        report = await task.run_once()
    # 无 growth 能力 → 跳过（读凭证前就返回，不会触发解密）
    assert report.skipped == 1


async def test_growth_skips_undecryptable_credential_with_growth_capable(repo, caplog):
    """渠道**有**成长中心能力时，解不开的凭证被跳过（整轮不挂）。"""
    from tests.provider.codebuddy._growth_support import StubGrowthProvider

    credentials, db = repo
    bad = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    _break_ciphertext(credentials, db, bad)

    task = GrowthTask(credentials, {"codebuddy": StubGrowthProvider()},
                      GrowthRepository(db))
    with caplog.at_level("ERROR", logger="src.tasks.growth"):
        report = await task.run_once()
    assert report.skipped == 1
    assert report.attempted == 0


async def test_refresh_skips_undecryptable_credential_after_lock(repo, caplog):
    """预刷新：持锁后重读时解不开 → 跳过（不冒泡打断整轮）。"""
    credentials, db = repo
    bad = credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})
    _break_ciphertext(credentials, db, bad)

    task = RefreshTask(credentials, {"codebuddy": ProbeProvider(refresh_new="NEW")},
                       skew_seconds=3600, now=lambda: 1000)
    with caplog.at_level("ERROR", logger="src.tasks.refresh"):
        report = await task.run_once()
    assert report.skipped >= 1


async def test_refresh_reread_under_lock_survives_decrypt_failure(repo, caplog):
    """**持锁后重读**那一刻解不开 → 跳过该凭证（早先直接冒泡）。

    锁内重读是「读 → await 上游 → 整份覆写」的第二读点：第一次读通过、
    等锁期间凭证被换成解不开的密文，第二次读就抛。
    """
    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1})

    task = RefreshTask(credentials, {"codebuddy": ProbeProvider(refresh_new="NEW")},
                       skew_seconds=3600, now=lambda: 1000)
    original = credentials.credential_data
    calls = {"n": 0}

    def _flaky(cid):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(cid)
        raise CredentialDecryptError("credential decryption failed")

    credentials.credential_data = _flaky  # type: ignore[method-assign]
    with caplog.at_level("ERROR", logger="src.tasks.refresh"):
        report = await task.run_once()
    assert report.skipped == 1
    assert report.failed == 0
