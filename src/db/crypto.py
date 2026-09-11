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


class CredentialCipher:
    def __init__(self, app_secret: str) -> None:
        if not app_secret:
            raise ValueError("APP_SECRET must not be empty")
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
