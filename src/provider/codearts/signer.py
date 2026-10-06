"""华为云 `SDK-HMAC-SHA256` 请求签名（逐行对照官方 AKSKSigner / 逆向记录 signer.go）。

签名串构造（每一步都踩过坑，勿「优化」）：

1. `CanonicalURI` **每段 percent-encode 且末尾补 `/`**——`/v1/model/builtin` →
   `/v1/model/builtin/`。漏掉尾斜杠是 APIG.0301 的常见原因。
2. `SignedHeaders` 是请求里**全部**头（小写、字典序），值只去首尾空白。
   `Authorization` 自身在签名前尚未写入，自然不在集合里。
3. payload hash 取 `X-Sdk-Content-Sha256` 头的值（本模块由 `sign_headers`
   先算好写入），不是再算一遍 body——两者必须一致，否则签名对不上。
4. `StringToSign` = 算法 + `X-Sdk-Date` + sha256(CanonicalRequest)，全十六进制小写。

纯函数 + 显式入参：不含「当前时间」，固定向量可复现。本模块不依赖
httpx / 任何凭证类型，便于单独测试与复用。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

SIGNING_ALGORITHM = "SDK-HMAC-SHA256"
HEADER_X_SDK_DATE = "X-Sdk-Date"
HEADER_X_SECURITY_TOKEN = "X-Security-Token"
HEADER_X_SDK_CONTENT_SHA256 = "X-Sdk-Content-Sha256"


@dataclass(frozen=True, slots=True)
class SignCredential:
    """AK/SK 临时凭证（+ 可选 STS security token）。"""

    access_key_id: str
    secret_access_key: str
    security_token: str = ""


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def hmac_sha256_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def canonical_uri(path: str) -> str:
    """每段 percent-encode 且末尾补 `/`（对齐 JS SDK 的 CanonicalURI）。

    先按 `/` 拆段再逐段 quote：对整串 quote 会把分隔符也编码掉。
    `quote(..., safe="")` 保留 RFC3986 未保留字符集，与 `encodeURIComponent`
    的差别只在 `!'()*`，华为云路径里不会出现，接受。
    """
    segments = [quote(segment, safe="") for segment in path.split("/")]
    joined = "/".join(segments)
    return joined if joined.endswith("/") else f"{joined}/"


def canonical_query(raw_query: str) -> str:
    """规范查询串：key 与 value 都 quote，key 字典序、同 key 的 value 也排序。"""
    if not raw_query:
        return ""
    pairs: list[tuple[str, str]] = []
    for part in raw_query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        pairs.append((quote(key, safe=""), quote(value, safe="")))
    pairs.sort()
    return "&".join(f"{key}={value}" for key, value in pairs)


def canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """全部头 → (canonicalHeaders 文本, signedHeaders 分号串)。

    头值先去首尾空白并**剥离 CR/LF**（L5 纵深防御）：签名串以换行分隔字段，
    值里混入换行会改变 canonical request 的结构；实际头值均来自本服务构造，
    这里只作最后一道防线。
    """
    lowered = {str(key).lower(): _clean_header_value(value)
               for key, value in headers.items()}
    keys = sorted(lowered)
    canonical = "".join(f"{key}:{lowered[key]}\n" for key in keys)
    return canonical, ";".join(keys)


def _clean_header_value(value: object) -> str:
    return str(value).replace("\r", "").replace("\n", "").strip()


def canonical_request(method: str, path: str, raw_query: str,
                      headers: Mapping[str, str], payload_hash: str) -> tuple[str, str]:
    """返回 (CanonicalRequest, SignedHeaders)。"""
    canonical_header_text, signed_headers = canonical_headers(headers)
    request = "\n".join([
        method.upper(),
        canonical_uri(path),
        canonical_query(raw_query),
        canonical_header_text,
        signed_headers,
        payload_hash,
    ])
    return request, signed_headers


def string_to_sign(canonical_request_text: str, x_sdk_date: str) -> str:
    return "\n".join([SIGNING_ALGORITHM, x_sdk_date,
                      sha256_hex(canonical_request_text.encode("utf-8"))])


def sign_headers(
    credential: SignCredential,
    *,
    method: str,
    path: str,
    raw_query: str = "",
    headers: Mapping[str, str],
    payload: bytes = b"",
    x_sdk_date: str,
    trace_id: str | None = None,
) -> dict[str, str]:
    """给一次请求补齐签名头（返回**合并后**的完整头，不改动入参）。

    流程与 signer.go 的 `signRequest` 一致：先落 `X-Sdk-Date` /
    `X-Security-Token` / `X-Sdk-Content-Sha256`，再用**全部头**算签名，
    最后写 `Authorization`。trace id 是可选的 `x-snap-traceid`，它同样
    计入 SignedHeaders（在签名前写入）。
    """
    merged = {str(key): str(value) for key, value in headers.items()}
    merged[HEADER_X_SDK_DATE] = x_sdk_date
    if credential.security_token:
        merged[HEADER_X_SECURITY_TOKEN] = credential.security_token
    payload_hash = sha256_hex(payload)
    merged[HEADER_X_SDK_CONTENT_SHA256] = payload_hash
    if trace_id:
        merged["x-snap-traceid"] = trace_id

    request, signed_headers = canonical_request(
        method, path, raw_query, merged, payload_hash)
    signature = hmac_sha256_hex(
        credential.secret_access_key, string_to_sign(request, x_sdk_date))
    merged["Authorization"] = (
        f"{SIGNING_ALGORITHM} Access={credential.access_key_id}, "
        f"SignedHeaders={signed_headers}, Signature={signature}")
    return merged
