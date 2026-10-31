"""凭证加密与密钥派生。"""

from __future__ import annotations

import base64

import pytest

from src.db.crypto import CredentialCipher, CredentialDecryptError, derive_key


def test_cipher_roundtrip():
    cipher = CredentialCipher("secret-a-0123456789")
    token = cipher.encrypt(b'{"uid": "1"}')
    assert cipher.decrypt(token) == b'{"uid": "1"}'

def test_cipher_wrong_secret_raises():
    token = CredentialCipher("secret-a-0123456789").encrypt(b"x")
    with pytest.raises(CredentialDecryptError):
        CredentialCipher("secret-b-0123456789").decrypt(token)

def test_cipher_rejects_empty_secret():
    with pytest.raises(ValueError):
        CredentialCipher("")

def test_derive_key_is_stable_urlsafe_base64():
    key = derive_key("abc")
    assert key == derive_key("abc")
    assert key != derive_key("abd")
    assert len(base64.urlsafe_b64decode(key)) == 32

def test_cipher_rejects_low_entropy_secret():
    """够长但无熵的密钥同样拒绝（重复/周期/字符集过小）。"""
    from src.db.crypto import WeakSecretError

    for weak in ("a" * 20, "ab" * 10, "1234567890" * 2, "abcdefgh" * 3):
        with pytest.raises(WeakSecretError, match="low-entropy"):
            CredentialCipher(weak)

def test_cipher_accepts_high_entropy_secret():
    """字符集足够大的长密钥正常通过。"""
    assert CredentialCipher("x9Q2mZ7pL4vT8wR1sK6n").encrypt(b"ok")


def test_cipher_rejects_known_placeholder_secret():
    """已知占位值必须直接拒启动，即使长度与熵都过关。

    `local-dev-secret-change-me` 是 26 字符 / 15 种字符，`_looks_low_entropy`
    放行；实测用它可解开真实库里的全部凭证（详见 `KNOWN_WEAK_SECRETS`）。
    """
    from src.db.crypto import KNOWN_WEAK_SECRETS, WeakSecretError

    assert "local-dev-secret-change-me" in KNOWN_WEAK_SECRETS
    for weak in KNOWN_WEAK_SECRETS:
        with pytest.raises(WeakSecretError, match="placeholder"):
            CredentialCipher(weak)


def test_cipher_placeholder_check_ignores_case_and_whitespace():
    """`.env` 里带引号/首尾空白也算命中，不能靠大小写绕过。"""
    from src.db.crypto import WeakSecretError

    for variant in ("  local-dev-secret-change-me  ",
                    "LOCAL-DEV-SECRET-CHANGE-ME"):
        with pytest.raises(WeakSecretError, match="placeholder"):
            CredentialCipher(variant)
