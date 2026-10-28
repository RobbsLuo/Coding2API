"""`CredentialRepository.credential_data`：解密失败的口径。

**背景（2026-10-26 14:48 实测）**：一条凭证的密文解不开（APP_SECRET 换过
或密文损坏）时，`credential_data` 抛 `CredentialDecryptError` → 一条坏凭证
就能让**整个聊天请求**回 500（栈顶「凭证解密失败：APP_SECRET 是否被更换
过？」），其余好凭证白白连坐。

两套口径并存，各有用途：

* `credential_data` **照旧抛** —— 单条探测端点要把它如实报给管理员
  （可行动的 500，见 `webapp.handlers` 与 `tests/regression/test_hardening.py`）；
* 批处理路径（选号、模型列表、后台任务）**捕获它并跳过该凭证**，
  见 `executor._safe_credential_data` 与 `api.models`。
"""

from __future__ import annotations

import pytest

from src.db.crypto import CredentialCipher, CredentialDecryptError


def test_credential_data_raises_for_undecryptable_credential(repo):
    """`credential_data` 照旧抛：单条探测端点要如实报给管理员。"""
    credentials, db = repo
    credential_id = credentials.add(provider="trae",
                                    credential_data={"accessToken": "a"})
    # 换成另一个密钥加密 → 密文与当前 APP_SECRET 不匹配
    other = CredentialCipher("a-completely-different-secret")
    with db.transaction() as conn:
        conn.execute("UPDATE credentials SET data_enc = ? WHERE id = ?",
                     (other.encrypt(b'{"accessToken": "a"}'), credential_id))

    with pytest.raises(CredentialDecryptError):
        credentials.credential_data(credential_id)


def test_credential_data_round_trip(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="trae",
                                    credential_data={"accessToken": "a"})
    assert credentials.credential_data(credential_id) == {"accessToken": "a"}


def test_credential_data_returns_none_for_missing_credential(repo):
    credentials, _db = repo
    assert credentials.credential_data("cred_missing") is None


def test_has_same_credential_matches_provider_and_content(repo):
    """批量导入去重：同渠道 + 解密后内容完全一致才命中。"""
    credentials, _db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="qoder", credential_data={"accessToken": "a"})

    assert credentials.has_same_credential("trae", {"accessToken": "a"}) is True
    assert credentials.has_same_credential("trae", {"accessToken": "b"}) is False
    # 同内容不同渠道不算重复
    assert credentials.has_same_credential("codebuddy", {"accessToken": "a"}) is False


def test_has_same_credential_skips_undecryptable_rows(repo):
    """坏密文行跳过，不阻断整批导入（继续比对后面的好行）。"""
    credentials, db = repo
    other = CredentialCipher("a-completely-different-secret")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO credentials (id, provider, nickname, data_enc, created_at, "
            "added_by, tags) VALUES ('cred_bad', 'trae', '', ?, 1, '', '')",
            (other.encrypt(b'{"accessToken": "broken"}'),),
        )
    credentials.add(provider="trae", credential_data={"accessToken": "good"})

    assert credentials.has_same_credential("trae", {"accessToken": "good"}) is True
    assert credentials.has_same_credential("trae", {"accessToken": "nope"}) is False
