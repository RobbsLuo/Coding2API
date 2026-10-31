"""凭证列加解密：Fernet（AES-128-CBC + HMAC），密钥从 APP_SECRET 派生。

APP_SECRET 丢失 = 已存凭证全部不可解，只能重录（PROPOSAL §6 已明示，不做密钥轮换）。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def derive_key(app_secret: str) -> bytes:
    """把任意长度的 APP_SECRET 归一化为 Fernet 需要的 32 字节 base64 key。

    刻意保持「无盐单次 SHA-256」：换 HKDF/PBKDF2 + 随机盐会让**已入库的
    全部凭证不可解**（等价于密钥轮换，PROPOSAL §6 明确不做），属于需要
    运维先解密再重加密的数据迁移，不随本批安全加固一起改。作为补偿，
    下面按熵拒掉弱密钥（`WeakSecretError`）。
    """
    digest = hashlib.sha256(app_secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


# 弱密钥直接拒绝：Fernet 的密钥完全由 APP_SECRET 派生，
# 而 .env 里的占位值（local-dev-secret-change-me 之类）会被用来加密
# 真实上游凭证。长度下限是启发式，但足以拦住占位与手滑。
MIN_SECRET_LENGTH = 16
# 不同字符数下限：`aaaaaaaaaaaaaaaa` / `1234567890123456` 这类「够长但
# 无熵」的密钥同样能被秒破，单看长度拦不住。
MIN_SECRET_DISTINCT_CHARS = 8

# 已知占位值 / 示例值 / 常见弱口令：`.env.example` 与文档里出现过，或
# 「够长且字符够杂」刚好骗过上面两条启发式。实测 `local-dev-secret-change-me`
# （26 字符 / 15 种字符）能通过长度与熵检查，然后用它解开真实库的全部凭证。
# 长度与熵是启发式，这里是**确定性的**兜底：命中即拒绝启动。
KNOWN_WEAK_SECRETS = frozenset({
    "local-dev-secret-change-me",
    "change-me-please-change-me",
    "please-change-me-change-me",
    "changeme-change-me-1234",
    "password123456789",
    "secretsecretsecret",
    "your-secret-key-here",
    "replace-with-a-real-secret",
    "development-secret-key",
})


def _looks_low_entropy(app_secret: str) -> bool:
    """长度够但熵极低：重复字符、整串是短周期重复、或字符集过小。"""
    if len(set(app_secret)) < MIN_SECRET_DISTINCT_CHARS:
        return True
    # 整串由某个短周期重复而成（如 "abcdabcdabcdabcd"）：周期越长熵越低，
    # 但下限设为「串长的一半」——任何真随机的长密钥都不会命中。
    for period in range(1, len(app_secret) // 2 + 1):
        repeats = len(app_secret) // period
        if len(app_secret) % period == 0 and app_secret == app_secret[:period] * repeats:
            return True
    return False


def _is_known_weak(app_secret: str) -> bool:
    """已知占位值 / 示例值（大小写与首尾空白不敏感）。

    与 `_looks_low_entropy` 分开：那条是**统计特征**，这条是**已知名单**。
    名单拦截的是「够长、字符够杂、但公开可查」的占位值——它们在任何熵指标上
    都像真密钥，只有枚举才能识别。
    """
    return app_secret.strip().lower() in KNOWN_WEAK_SECRETS


class WeakSecretError(ValueError):
    """APP_SECRET 过短（占位值/手滑），拒绝启动。"""


class CredentialCipher:
    def __init__(self, app_secret: str, *, allow_known_weak: bool = False) -> None:
        """`allow_known_weak=True` 只给**轮换脚本**用：它必须能用旧（可能已被
        列入黑名单的）密钥解密存量数据，否则永远修不了。业务路径一律用默认值。
        """
        if not app_secret:
            raise ValueError("APP_SECRET must not be empty")
        if len(app_secret) < MIN_SECRET_LENGTH:
            raise WeakSecretError(
                f"APP_SECRET must be at least {MIN_SECRET_LENGTH} characters")
        if not allow_known_weak and _is_known_weak(app_secret):
            # 已知占位值优先判：它比「低熵」更具体，文案能直接指向怎么修。
            # 与 `.env.example` / 文档里的占位值同名：长度与熵都够，但公开可查。
            # 用它加密等于把凭证交给任何读过本仓库的人。
            raise WeakSecretError(
                "APP_SECRET is a known placeholder/example value; generate a "
                "real one (python -c \"import secrets;print(secrets.token_urlsafe(48))\")")
        if not allow_known_weak and _looks_low_entropy(app_secret):
            raise WeakSecretError(
                "APP_SECRET looks low-entropy (repeated/cyclic or too few "
                f"distinct characters, need >= {MIN_SECRET_DISTINCT_CHARS})")
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
