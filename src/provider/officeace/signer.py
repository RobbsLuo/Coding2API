"""华为云请求签名：`V11-HMAC-SHA256`（模型网关）与 `SDK-HMAC-SHA256`（控制面）。

officeAce 同时打两套签名，用错一套的回包是 `APIG.0624 Unsupported signature
algorithm`（或 401 `verify ak sk signature fail`），不会告诉你哪一步错了：

| 面 | 端点 | 算法 |
|---|---|---|
| 模型网关 `/v1/subscription*`、`/v1/model-token-rates` | `modelgw-*.officeace.*` | **V11** |
| 控制面 `/v1/claw/*` | `agentarts.*.myhuaweicloud.com` | `SDK-HMAC`（V4） |

V11 与 V4 的差只有两处（其余逐字节相同，故共用 `codearts.signer` 的
canonical 原语，不另写一份）：

1. `StringToSign` 多一行 **CredentialScope** `YYYYMMDD/{region}/apic`；
2. 签名密钥不是 SK 本身，而要先派生：
   `Key = HMAC(HMAC(AK, SK), Scope + 0x01)[:32]`，再用它做最后一次 HMAC。

三处「必须原样」的细节（都实测过）：

* `CanonicalURI` **每段 percent-encode 且末尾补 `/`**；
* **query 必须参与签名**（`/v1/subscription/usage?sku_attr_code=…`）：漏签回
  401 `APIG.0301 verify ak sk signature fail`，实测对照过；
* `X-Project-ID` 有则必须计入 SignedHeaders（放进去了就必须签名，否则对不上）。

纯函数 + 显式 `x_sdk_date`：不含「当前时间」，固定向量可复现。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from ..codearts.signer import (
    HEADER_X_SDK_DATE,
    HEADER_X_SECURITY_TOKEN,
    canonical_headers,
    canonical_query,
    canonical_uri,
    sha256_hex,
)

ALGORITHM_V11 = "V11-HMAC-SHA256"
ALGORITHM_V4 = "SDK-HMAC-SHA256"
#: V11 的 CredentialScope 服务名（APIG 固定 `apic`）。
SCOPE_SERVICE = "apic"


@dataclass(frozen=True, slots=True)
class SignCredential:
    """华为云临时 AK/SK（+ STS security token、可选 project_id）。"""

    access_key_id: str
    secret_access_key: str
    security_token: str = ""
    project_id: str = ""


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_access_key: str, scope: str, access_key_id: str) -> str:
    """V11 签名密钥派生：`HMAC(HMAC(AK, SK), scope + 0x01)[:32]` 的**十六进制文本**。

    三处与直觉不符、写错任一处签名恒为 401 `verify ak sk signature fail`
    （逐项实测对照过）：

    1. 外层**密钥**是 AK、**消息**是 SK（华为特有的方向）；
    2. 截断到 32 字节后要转成 **hex 文本**再当下一轮的密钥（不是原始字节）；
    3. `scope + 0x01` 的单字节分隔符不能省。
    """
    step1 = _hmac(access_key_id.encode("utf-8"), secret_access_key)
    return _hmac(step1, scope + "\x01")[:32].hex()


def _canonical_request(method: str, url: str,
                       headers: Mapping[str, str], payload_hash: str,
                       ) -> tuple[str, str]:
    """返回 `(CanonicalRequest, SignedHeaders)`（V4/V11 共用）。"""
    split = urlsplit(url)
    text, signed = canonical_headers(headers)
    request = "\n".join([
        method.upper(),
        canonical_uri(split.path or "/"),
        canonical_query(split.query),
        text,
        signed,
        payload_hash,
    ])
    return request, signed


def build_headers(
    credential: SignCredential,
    *,
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    payload: bytes = b"",
    x_sdk_date: str,
    region: str = "cn-southwest-2",
    scope_service: str = SCOPE_SERVICE,
) -> dict[str, str]:
    """补齐 V11 签名头（返回**合并后**的完整头，不改动入参）。

    `url` 必须是含 query 的完整地址：query 参与 CanonicalRequest。
    """
    merged = {str(key): str(value) for key, value in (headers or {}).items()}
    merged[HEADER_X_SDK_DATE] = x_sdk_date
    if credential.security_token:
        merged[HEADER_X_SECURITY_TOKEN] = credential.security_token
    merged["host"] = urlsplit(url).netloc
    if credential.project_id:
        merged["X-Project-ID"] = credential.project_id
    # **不写 `X-Sdk-Content-Sha256`**：华为 APIG 的 V11 只按实际存在的头签名，
    # 多塞一个头会改变 SignedHeaders 与 CanonicalRequest，结果恒 401
    # `verify ak sk signature fail`（实测对照过）。
    payload_hash = sha256_hex(payload)

    request, signed = _canonical_request(method, url, merged, payload_hash)
    scope = f"{x_sdk_date[:8]}/{region}/{scope_service}"
    string_to_sign = "\n".join(
        [ALGORITHM_V11, x_sdk_date, scope, sha256_hex(request.encode("utf-8"))])
    key = signing_key(credential.secret_access_key, scope,
                      credential.access_key_id)
    signature = _hmac(key.encode("utf-8"), string_to_sign).hex()
    merged["Authorization"] = (
        f"{ALGORITHM_V11} Credential={credential.access_key_id}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}")
    return merged


def build_v4_headers(
    credential: SignCredential,
    *,
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    payload: bytes = b"",
    x_sdk_date: str,
) -> dict[str, str]:
    """补齐 `SDK-HMAC-SHA256`（V4）签名头：控制面 `/v1/claw/*` 用。

    V4 的签名密钥就是 SK 本身（不派生），`StringToSign` 没有 scope 行。
    """
    merged = {str(key): str(value) for key, value in (headers or {}).items()}
    merged[HEADER_X_SDK_DATE] = x_sdk_date
    if credential.security_token:
        merged[HEADER_X_SECURITY_TOKEN] = credential.security_token
    merged["host"] = urlsplit(url).netloc
    if credential.project_id:
        merged["X-Project-ID"] = credential.project_id
    payload_hash = sha256_hex(payload)

    request, signed = _canonical_request(method, url, merged, payload_hash)
    string_to_sign = "\n".join(
        [ALGORITHM_V4, x_sdk_date, sha256_hex(request.encode("utf-8"))])
    signature = _hmac(credential.secret_access_key.encode("utf-8"),
                      string_to_sign).hex()
    merged["Authorization"] = (
        f"{ALGORITHM_V4} Access={credential.access_key_id}, "
        f"SignedHeaders={signed}, Signature={signature}")
    return merged


def quote_segment(segment: str) -> str:
    """单段 percent-encode（测试与排障用；签名内部走 `canonical_uri`）。"""
    return quote(segment, safe="")
