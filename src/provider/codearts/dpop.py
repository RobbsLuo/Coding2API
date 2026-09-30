"""ES256 DPoP 证明 JWT（RFC 9449），供 `POST {sts}/v1/oauth2/tokens` 使用。

用项目已有依赖 `cryptography`（`ec.SECP256R1` + `ECDSA(SHA256)`），不移植 Go、
不手写 ECDSA。两条与上游兼容性直接相关的纪律：

* **低 S 归一化**：华为 STS 的 DPoP 校验要求低 S（s ≤ n/2），而 `cryptography`
  的 `ECDSA(SHA256)` **不保证**低 S（实测 2000 次签名有 991 次 high-S），故
  本模块在编码 JWS 前显式归一：`s > n/2` 时取 `s = n - s`。高 S 签名在数学上
  同样可验，但会被 STS 判 `InvalidDPoPHeader`。
* **私钥必须随 refresh_token 一起持久化并原样复用**：华为 STS 把
  refresh_token 绑定在首次换取时的 DPoP 公钥上，重新生成密钥刷新会被拒
  （`STS5.1806 invalid refresh token: 'InvalidDPoPHeader'`）。
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
)

CURVE = "P-256"
ALG = "ES256"
HEADER_TYP = "dpop+jwt"
_COORD_BYTES = 32

# P-256 曲线阶 n（基点阶）。ECDSA 签名 (r, s) 中 s 与 n-s 都合法且验证等价，
# 华为 STS 的 DPoP 校验只接受低 S（s ≤ n/2），故编码前必须归一化。
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_HALF_ORDER = P256_ORDER // 2


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _int_to_bytes(value: int) -> bytes:
    return value.to_bytes(_COORD_BYTES, "big")


def new_private_jwk() -> dict[str, str]:
    """生成可持久化的 P-256 私钥 JWK（含公钥坐标，便于验证与调试）。"""
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_jwk_from_key(private_key)


def private_jwk_from_key(private_key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    numbers = private_key.private_numbers()
    public = numbers.public_numbers
    return {
        "kty": "EC",
        "crv": CURVE,
        "x": _b64url(_int_to_bytes(public.x)),
        "y": _b64url(_int_to_bytes(public.y)),
        "d": _b64url(_int_to_bytes(numbers.private_value)),
    }


def public_jwk(private_jwk: dict[str, str]) -> dict[str, str]:
    """从私钥 JWK 取公钥 JWK（DPoP 头里只放公钥）。"""
    return {"kty": "EC", "crv": CURVE,
            "x": str(private_jwk.get("x") or ""),
            "y": str(private_jwk.get("y") or "")}


def private_key_from_jwk(private_jwk: dict[str, str]) -> ec.EllipticCurvePrivateKey:
    """私钥 JWK → `cryptography` 私钥；结构非法时抛 ValueError。

    只做结构校验（曲线/字段可解码），不验公私钥是否匹配：不匹配的凭证交上去
    会被 STS 拒，本地重复校验没有收益。
    """
    if private_jwk.get("kty") != "EC" or private_jwk.get("crv") != CURVE:
        raise ValueError("invalid DPoP JWK curve")
    try:
        private_value = int.from_bytes(_b64url_decode(str(private_jwk["d"])), "big")
    except (KeyError, ValueError) as error:
        raise ValueError("invalid DPoP JWK d") from error
    if private_value <= 0:
        raise ValueError("invalid DPoP JWK d")
    return ec.derive_private_key(private_value, ec.SECP256R1())


def normalize_low_s(s: int) -> int:
    """ECDSA 的 s 归一化为低 S（s ≤ n/2）；已是低 S 则原样返回。

    `(r, s)` 与 `(r, n - s)` 验证等价，但华为 STS 的 DPoP 校验要求低 S。
    """
    return s if s <= _P256_HALF_ORDER else P256_ORDER - s


def sign_proof(private_jwk: dict[str, str], htu: str, *,
               htm: str = "POST", issued_at: int | None = None,
               jti: str | None = None) -> str:
    """生成 DPoP 证明 JWT：`header.payload.signature`（全 base64url 无填充）。

    `htu` 是**目标令牌端点**完整地址（不含查询串），`htm` 恒为 POST。
    `iat` / `jti` 可注入：固定向量测试需要可复现的 payload。
    """
    private_key = private_key_from_jwk(private_jwk)
    header = {"alg": ALG, "typ": HEADER_TYP, "jwk": public_jwk(private_jwk)}
    payload: dict[str, Any] = {
        "htm": htm,
        "htu": htu,
        "iat": int(issued_at if issued_at is not None else time.time()),
        "jti": jti or secrets.token_hex(16),
    }
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))}"
        f".{_b64url(json.dumps(payload, separators=(',', ':')).encode('utf-8'))}")
    # ECDSA(SHA256) 输出 DER；JWS 要求原始 R||S 定长拼接。
    # s 必须归一为低 S：cryptography 不保证，高 S 会被华为 STS 拒。
    der = private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    jose_signature = _int_to_bytes(r) + _int_to_bytes(normalize_low_s(s))
    return f"{signing_input}.{_b64url(jose_signature)}"


def verify_proof(proof: str, public: ec.EllipticCurvePublicKey) -> bool:
    """校验 DPoP JWS（仅测试与本地诊断用；上游才是权威校验方）。"""
    parts = proof.split(".")
    if len(parts) != 3:
        return False
    try:
        signature = _b64url_decode(parts[2])
    except ValueError:
        return False
    if len(signature) != _COORD_BYTES * 2:
        return False
    r = int.from_bytes(signature[:_COORD_BYTES], "big")
    s = int.from_bytes(signature[_COORD_BYTES:], "big")
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        public.verify(encode_dss_signature(r, s),
                      f"{parts[0]}.{parts[1]}".encode("ascii"),
                      ec.ECDSA(hashes.SHA256()))
    except Exception:  # noqa: BLE001 - 验签失败一律 False（InvalidSignature 等）
        return False
    return True


def decode_segment(segment: str) -> dict[str, Any]:
    """解一个 JWS 段（测试用：读 header / payload）。"""
    parsed = json.loads(_b64url_decode(segment).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("JWS segment is not an object")
    return parsed


def public_key_from_jwk(jwk: dict[str, str]) -> ec.EllipticCurvePublicKey:
    """公钥 JWK → `cryptography` 公钥（测试用）。"""
    numbers = ec.EllipticCurvePublicNumbers(
        int.from_bytes(_b64url_decode(str(jwk["x"])), "big"),
        int.from_bytes(_b64url_decode(str(jwk["y"])), "big"),
        ec.SECP256R1())
    return numbers.public_key()
