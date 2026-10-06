"""Qoder COSY 请求编码：自定义 Base64 变体、设备指纹派生与 `cosy-*` 头。

纯函数优先（`qoder_encode` / `signature` / `derive_*`）便于固定向量单测；
`CosySession` 只做「把常量与凭证拼成一次请求的头」这一件事。

三个与常规协议不同的点（PROPOSAL §3.3 / TECHNICAL §3.16）：

1. **请求体不是 JSON**，而是自定义 Base64 变体：标准 base64 → 尾/中/首
   三段轮转 → 自定义字母表映射 → `=` 换成 `$`。服务端按同一变换还原。
2. **鉴权不是 bearer**，而是 `Authorization: Bearer COSY.<payload_b64>.<md5>`；
   签名串为 `md5(payload_b64 \\n cosy_key \\n date \\n body \\n path)`，其中
   `path` 是 URL path **去掉 `/algo` 前缀**（query 不参与签名，故国际版
   api1/api2/api3 之间切换主机不影响签名有效性）。
3. **会话身份靠两段密文传递**：`cosy_key = base64(RSA_PKCS1v15(tempKey))`，
   `info = base64(AES-128-CBC(json_sorted_compact(identity), key=iv=tempKey))`。
   密码学复用项目已有的 `cryptography`（不移植参考仓库的手写纯 Python 实现）。

`cosy-machineid` / `cosy-machinetoken` 按 `uid` 稳定派生：同一账号永远来自
同一台虚拟设备（随机机器码会触发上游风控）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .events import CLIENT_UA, DEFAULT_USER_TYPE

# ---------------------------------------------------------------------------
# 自定义 Base64 变体
# ---------------------------------------------------------------------------

QODER_STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
QODER_CUSTOM_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
QODER_PAD = "$"

_ENC_TABLE = str.maketrans(
    QODER_STD_ALPHABET + "=", QODER_CUSTOM_ALPHABET + QODER_PAD)
_DEC_TABLE = str.maketrans(
    QODER_CUSTOM_ALPHABET + QODER_PAD, QODER_STD_ALPHABET + "=")


def qoder_encode(plain: bytes) -> str:
    """明文 → Qoder 自定义 Base64 变体（出站请求体格式）。"""
    std = base64.b64encode(plain).decode("ascii")
    size = len(std)
    head = size // 3
    rearranged = std[size - head:] + std[head:size - head] + std[:head]
    return rearranged.translate(_ENC_TABLE)


def qoder_decode(encoded: str) -> bytes:
    """`qoder_encode` 的逆运算（测试/调试用）。

    正向 `R = S[n-a:] + S[a:n-a] + S[:a]`，逆向 `S = R3 + R2 + R1`。
    """
    std = encoded.translate(_DEC_TABLE)
    size = len(std)
    head = size // 3
    r1, r2, r3 = std[:head], std[head:size - head], std[size - head:]
    return base64.b64decode(r3 + r2 + r1)


def json_sorted_compact(mapping: dict[str, Any]) -> bytes:
    """键排序 + 无空白的紧凑 JSON（服务端签名字节与此强绑定）。

    官方实现的 `None` 归一为空串——`info` 里 `yx_uid` 等字段缺失时上游按空串
    解析，写 `null` 会让服务端解出的身份 JSON 与客户端不一致。
    """
    parts = []
    for key in sorted(mapping.keys()):
        value = mapping[key]
        parts.append(json.dumps(str(key), ensure_ascii=False) + ":"
                     + json.dumps("" if value is None else value, ensure_ascii=False))
    return ("{" + ",".join(parts) + "}").encode("utf-8")


# ---------------------------------------------------------------------------
# 稳定设备指纹（由 uid 单向派生；多账号天然隔离）
# ---------------------------------------------------------------------------


def derive_id(uid: str, salt: str) -> str:
    """由 `uid + salt` 稳定派生的 36 位十六进制设备/会话标识。

    `uid` 缺失时用 `anonymous` 占位：同一进程内稳定（不会每次请求换机器码），
    但多账号会算出同一指纹——上游会把它当成同一台设备。因此 `uid` 缺失是
    「可用但会被关联」的降级态，登录流程必须尽力补齐 uid。
    """
    seed = f"{salt}:{uid or 'anonymous'}"
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:36]


def derive_machine_type(uid: str) -> str:
    """稳定派生的 18 位去横线 machine_type（`cosy-machinetype` 头）。"""
    return derive_id(uid, "machinetype").replace("-", "")[:18]


def derive_machine_token(uid: str) -> str:
    """稳定派生 machine_token（base64url 外观，`cosy-machinetoken` 头）。"""
    raw = hashlib.sha512(f"machinetoken:{uid}".encode()).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:43]


def derive_request_id(uid: str) -> str:
    """openapi 请求的 `X-Request-ID`：稳定前缀 + 微秒后缀（防重放且可溯源）。"""
    return f"{derive_id(uid, 'req')}-{str(time.time_ns() % 1000000).zfill(6)}"


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------

COSY_VERSION = "1.1.64"
COSY_CLIENT_TYPE = "5"
COSY_CLIENT_IP = "169.254.198.161"      # 官方客户端的链路本地占位地址
LOGIN_VERSION = "v2"

# 服务端 RSA 公钥（逆向自官方客户端；只用于包裹会话 AES 密钥）。
SERVER_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""


def _load_server_public_key():
    return serialization.load_pem_public_key(SERVER_PUBLIC_KEY_PEM.encode("ascii"))


def rsa_wrap(value: bytes, public_key: rsa.RSAPublicKey | None = None) -> bytes:
    """RSA PKCS#1 v1.5 加密（包裹会话 AES 密钥）；默认用硬编码服务端公钥。"""
    key = public_key if public_key is not None else _load_server_public_key()
    return key.encrypt(value, padding.PKCS1v15())


def _pkcs7_pad(plain: bytes) -> bytes:
    pad = 16 - len(plain) % 16
    return plain + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise ValueError("aes: ciphertext not block aligned")
    pad = data[-1]
    if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("aes: bad PKCS7 padding")
    return data[:-pad]


def aes_cbc_encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 加密（PKCS7 填充）；Qoder 的 `info` 使用 key == iv。"""
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(_pkcs7_pad(plain)) + encryptor.finalize()


def aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 解密（严格 PKCS7）；供单测验证 `info` 可还原身份 JSON。"""
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return _pkcs7_unpad(decryptor.update(data) + decryptor.finalize())


def sign_path(raw_url: str) -> str:
    """签名路径：URL path，去掉 `/algo` 前缀（query 不参与签名）。"""
    path = urlparse(raw_url).path or "/"
    if path.startswith("/algo"):
        path = path[len("/algo"):]
    return path


def cosy_signature(*, payload_b64: str, cosy_key: str, date: str, body: str,
                   path: str) -> str:
    """`md5(payload_b64 \\n cosy_key \\n date \\n body \\n path)` 十六进制。"""
    raw = "\n".join([payload_b64, cosy_key, date, body, path])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def cosy_payload(session: _SessionMaterial, request_id: str) -> str:
    """COSY payload（键排序紧凑 JSON）的 base64；`requestId` 每请求新生成。

    `ideVersion` 恒为空串（官方 SDK 的内置常量），`version` 为 `v1`。
    """
    payload = {
        "cosyVersion": COSY_VERSION,
        "ideVersion": "",
        "info": session.info,
        "requestId": request_id,
        "version": "v1",
    }
    return base64.b64encode(json_sorted_compact(payload)).decode("ascii")


@dataclass(frozen=True, slots=True)
class _SessionMaterial:
    """一次会话里**不随请求变化**的密文材料（token 轮换后整体重建）。"""

    info: str
    cosy_key: str


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """一次签名结果：`payload_b64` / `date` / 完整 `Authorization` 头值。"""

    payload_b64: str
    date: str
    bearer: str


class CosySession:
    """单账号的 COSY 签名会话（进程内按 uid+token 缓存，见 `CosySessionCache`）。

    随机材料与时间源可注入，便于固定向量测试：
    `temp_key`（16 字符 AES 密钥）、`request_id`（payload 幂等性）、
    `now`（签名时间戳）、`public_key`（替换服务端 RSA 公钥）。
    """

    def __init__(
        self,
        *,
        uid: str,
        access_token: str = "",
        refresh_token: str = "",
        nickname: str = "",
        user_type: str = DEFAULT_USER_TYPE,
        org_id: str = "",
        org_name: str = "",
        temp_key: str | None = None,
        public_key: rsa.RSAPublicKey | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("cosy: empty access token")
        self.uid = uid or ""
        self.access_token = access_token
        self.machine_id = derive_id(self.uid, "machine")
        self.machine_token = derive_machine_token(self.uid)
        self.machine_type = derive_machine_type(self.uid)
        key = temp_key if temp_key is not None else uuid.uuid4().hex[:16]
        if len(key) != 16:
            raise ValueError("cosy: temp key must be 16 bytes")
        key_bytes = key.encode("utf-8")
        identity = {
            "name": nickname or "",
            "aid": self.uid,
            "uid": self.uid,
            "yx_uid": "",
            "organization_id": org_id or "",
            "organization_name": org_name or "",
            "user_type": user_type or DEFAULT_USER_TYPE,
            "security_oauth_token": access_token,
            "refresh_token": refresh_token or "",
        }
        self._material = _SessionMaterial(
            info=base64.b64encode(
                aes_cbc_encrypt(json_sorted_compact(identity), key_bytes, key_bytes)
            ).decode("ascii"),
            cosy_key=base64.b64encode(rsa_wrap(key_bytes, public_key)).decode("ascii"),
        )

    @property
    def info(self) -> str:
        return self._material.info

    @property
    def cosy_key(self) -> str:
        return self._material.cosy_key

    def new_request_id(self) -> str:
        return str(uuid.uuid4())

    def sign(self, *, body: str, raw_url: str, date: str | None = None,
             request_id: str | None = None) -> SignedRequest:
        """对一次请求（body + URL）签名；`date` 缺省用当前时间戳。"""
        payload_b64 = cosy_payload(self._material, request_id or self.new_request_id())
        stamp = date if date is not None else str(int(time.time()))
        digest = cosy_signature(payload_b64=payload_b64, cosy_key=self.cosy_key,
                                date=stamp, body=body, path=sign_path(raw_url))
        return SignedRequest(payload_b64=payload_b64, date=stamp,
                             bearer=f"Bearer COSY.{payload_b64}.{digest}")

    def headers(self, *, body: str, raw_url: str, model_key: str = "",
                sse: bool = True, accept: str = "text/event-stream",
                date: str | None = None, request_id: str | None = None,
                ) -> dict[str, str]:
        """一次推理/模型接口的完整 COSY 头。

        `x-model-key` 决定上游模型路由；`x-model-source: system` 标记这不是
        用户自带模型（官方客户端同款）。
        """
        signed = self.sign(body=body, raw_url=raw_url, date=date,
                           request_id=request_id)
        headers = {
            "cosy-data-policy": "AGREE",
            "content-type": "application/json",
            "cosy-machinetype": self.machine_type,
            "cosy-clienttype": COSY_CLIENT_TYPE,
            "cosy-date": signed.date,
            "cosy-user": self.uid,
            "cosy-key": self.cosy_key,
            "accept": accept,
            "cosy-clientip": COSY_CLIENT_IP,
            "authorization": signed.bearer,
            "accept-encoding": "identity",
            "cosy-version": COSY_VERSION,
            "cosy-machineid": self.machine_id,
            "cosy-machinetoken": self.machine_token,
            "login-version": LOGIN_VERSION,
            "user-agent": CLIENT_UA,
        }
        if sse:
            headers["cache-control"] = "no-cache"
        if model_key:
            headers["x-model-key"] = model_key
            headers["x-model-source"] = "system"
        return headers


class CosySessionCache:
    """`(uid, access_token)` → `CosySession` 的进程内缓存。

    键含 token：access token 轮换后旧会话携带旧身份密文，必须重建
    （否则刷新完的请求仍用旧 token 签名，上游按旧身份计权）。
    """

    def __init__(self, *, public_key: rsa.RSAPublicKey | None = None,
                 temp_key_factory: Callable[[], str] | None = None) -> None:
        self._entries: dict[str, tuple[str, CosySession]] = {}
        self._public_key = public_key
        self._temp_key_factory = temp_key_factory

    def get(self, credential: Any) -> CosySession:
        """按凭证取/建会话；凭证对象需带 `uid`/`access_token`/`nickname` 等字段。"""
        token = str(getattr(credential, "access_token", "") or "")
        if not token:
            raise ValueError("cosy: empty access token")
        uid = str(getattr(credential, "uid", "") or "")
        key = uid or token[:16]
        hit = self._entries.get(key)
        # 常量时间比较，避免缓存命中日志/时序侧信道（L4）。token 非协议秘密，
        # 这里只是防御纵深；编码成 bytes 以免非 ASCII token 触发 compare_digest 的
        # str 限制。
        if hit is not None and hmac.compare_digest(hit[0].encode("utf-8"),
                                                   token.encode("utf-8")):
            return hit[1]
        session = CosySession(
            uid=uid, access_token=token,
            refresh_token=str(getattr(credential, "refresh_token", "") or ""),
            nickname=str(getattr(credential, "nickname", "") or ""),
            user_type=str(getattr(credential, "user_type", "") or DEFAULT_USER_TYPE),
            org_id=str(getattr(credential, "organization_id", "") or ""),
            org_name=str(getattr(credential, "organization_name", "") or ""),
            temp_key=(self._temp_key_factory() if self._temp_key_factory else None),
            public_key=self._public_key,
        )
        self._entries[key] = (token, session)
        return session

    def invalidate(self, uid: str) -> None:
        self._entries.pop(uid, None)

    def clear(self) -> None:
        self._entries.clear()
