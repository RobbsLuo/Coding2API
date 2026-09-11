"""users.txt：PBKDF2-SHA256 用户存储（唯一用户源，PROPOSAL §5）。

格式（沿用 codebuddy2api 规范）：
    用户名:pbkdf2_sha256$<iterations>$<salt_b64>$<digest_b64>
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from pathlib import Path

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000
PBKDF2_MIN_ITERATIONS = 600_000
PBKDF2_MAX_ITERATIONS = 1_000_000
PBKDF2_SALT_BYTES = 16
PBKDF2_DIGEST_BYTES = 32


class UsersFileError(RuntimeError):
    """用户文件缺失、格式非法或无有效用户。"""


@dataclass(frozen=True)
class UserRecord:
    username: str
    password_hash: str


def create_password_hash(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    if isinstance(iterations, bool) or not isinstance(iterations, int):
        raise ValueError("PBKDF2 iterations are outside the supported range")
    if not PBKDF2_MIN_ITERATIONS <= iterations <= PBKDF2_MAX_ITERATIONS:
        raise ValueError("PBKDF2 iterations are outside the supported range")
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations,
                                dklen=PBKDF2_DIGEST_BYTES)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{PBKDF2_ALGORITHM}${iterations}${salt_b64}${digest_b64}"


def _parse_hash(password_hash: str) -> tuple[int, bytes, bytes]:
    algorithm, iterations_text, salt_b64, digest_b64 = password_hash.split("$")
    if algorithm != PBKDF2_ALGORITHM:
        raise ValueError("unsupported algorithm")
    if not isinstance(iterations_text, str) or not iterations_text.isdigit():
        raise ValueError("invalid iterations")
    iterations = int(iterations_text)
    if not PBKDF2_MIN_ITERATIONS <= iterations <= PBKDF2_MAX_ITERATIONS:
        raise ValueError("iterations out of range")
    salt = base64.urlsafe_b64decode(_pad(salt_b64))
    digest = base64.urlsafe_b64decode(_pad(digest_b64))
    return iterations, salt, digest


def _pad(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        iterations, salt, expected = _parse_hash(password_hash)
    except (AttributeError, TypeError, UnicodeError, ValueError, binascii.Error):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations,
                                 dklen=PBKDF2_DIGEST_BYTES)
    return hmac.compare_digest(actual, expected)


class UsersFileStore:
    """读取并缓存 users.txt；文件变更后按 mtime 重载。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._cache: dict[str, UserRecord] = {}
        self._mtime: float | None = None

    def _load_if_needed(self) -> None:
        if not self._path.is_file():
            raise UsersFileError(f"authentication users file not found: {self._path}")
        mtime = self._path.stat().st_mtime
        if self._mtime == mtime and self._cache:
            return
        records: dict[str, UserRecord] = {}
        for lineno, raw in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                raise UsersFileError(f"invalid user record at line {lineno}")
            username, _, password_hash = line.partition(":")
            username = username.strip()
            if not username:
                raise UsersFileError(f"invalid user record at line {lineno}")
            records[username] = UserRecord(username=username, password_hash=password_hash.strip())
        if not records:
            raise UsersFileError("no authentication users configured")
        self._cache = records
        self._mtime = mtime

    def verify(self, username: str, password: str) -> bool:
        self._load_if_needed()
        record = self._cache.get(username)
        if record is None:
            return False
        return verify_password(password, record.password_hash)

    def has(self, username: str) -> bool:
        self._load_if_needed()
        return username in self._cache

    def list_usernames(self) -> tuple[str, ...]:
        self._load_if_needed()
        return tuple(self._cache)

    def validate(self) -> None:
        """启动时调用：文件必须存在且至少一个有效用户。"""
        self._load_if_needed()
