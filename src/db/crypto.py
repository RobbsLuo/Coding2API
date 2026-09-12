"""凭证列加解密：Fernet（AES-128-CBC + HMAC），密钥从 APP_SECRET 派生。

APP_SECRET 丢失 = 已存凭证全部不可解，只能重录（PROPOSAL §8 已明示，不做密钥轮换）。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def derive_key(app_secret: str) -> bytes:
    """把任意长度的 APP_SECRET 归一化为 Fernet 需要的 32 字节 base64 key。"""
    digest = hashlib.sha256(app_secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


# 弱密钥直接拒绝：Fernet 的密钥完全由 APP_SECRET 派生，
# 而 .env 里的占位值（local-dev-secret-change-me 之类）会被用来加密
# 真实上游凭证。长度下限是启发式，但足以拦住占位与手滑。
MIN_SECRET_LENGTH = 16


class WeakSecretError(ValueError):
    """APP_SECRET 过短（占位值/手滑），拒绝启动。"""


class CredentialCipher:
    def __init__(self, app_secret: str) -> None:
        if not app_secret:
            raise ValueError("APP_SECRET must not be empty")
        if len(app_secret) < MIN_SECRET_LENGTH:
            raise WeakSecretError(
                f"APP_SECRET must be at least {MIN_SECRET_LENGTH} characters")
        self._fernet = Fernet(derive_key(app_secret))

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        try:
            return self._fernet.decrypt(ciphertext)
        except InvalidToken as error:
            raise CredentialDecryptError("credential decryption failed") from error


class CredentialDecryptError(Exception):
    """密钥不匹配或密文损坏。"""
