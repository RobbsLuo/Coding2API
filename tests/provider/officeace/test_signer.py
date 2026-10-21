"""officeAce 签名测试：V11（模型网关）与 SDK-HMAC-SHA256（控制面）。

两个算法都有**参考实现的固定向量**（从 OfficeAce 自己的 JS 实现逐行对照出来
并已用真实上游验证过 200），故这里断的是「签名串逐字节相等」，不是「能跑」。
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import quote, urlsplit

import pytest

from src.provider.officeace import signer

AK = "AKTEST"
SK = "SKTEST"
TOK = "TOK"
NOW = "20261021T013730Z"
URL = "https://gw.example.com/v1/subscription"


def _v11_reference(url: str, *, body: bytes, extra: dict[str, str],
                   method: str = "GET") -> str:
    """参考实现（OfficeAce JS 的 signV11 逐行翻译，独立于被测代码）。"""
    split = urlsplit(url)
    headers = {"x-sdk-date": NOW, "x-security-token": TOK,
               "host": split.netloc, **extra}
    names = sorted(headers)
    head = "".join(f"{name}:{headers[name].strip()}\n" for name in names)
    segments = [quote(x, safe="") for x in split.path.split("/")]
    path = "/".join(segments)
    if not path.endswith("/"):
        path += "/"
    pairs = []
    for part in split.query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        pairs.append((quote(key, safe=""), quote(value, safe="")))
    pairs.sort()
    query = "&".join(f"{k}={v}" for k, v in pairs)
    canonical = "\n".join([method, path, query, head, ";".join(names),
                           hashlib.sha256(body).hexdigest()])
    scope = f"{NOW[:8]}/cn-southwest-2/apic"
    string_to_sign = "\n".join(["V11-HMAC-SHA256", NOW, scope,
                                hashlib.sha256(canonical.encode()).hexdigest()])
    step1 = hmac.new(AK.encode(), SK.encode(), hashlib.sha256).digest()
    key = hmac.new(step1, scope.encode() + b"\x01", hashlib.sha256).digest()[:32].hex()
    signature = hmac.new(key.encode(), string_to_sign.encode(),
                         hashlib.sha256).hexdigest()
    return (f"V11-HMAC-SHA256 Credential={AK}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}")


@pytest.mark.parametrize("body", [b"", b""])
def test_v11_matches_reference(body):
    """GET 空体与 POST `` 两种签名串都要逐字节对齐参考实现。"""
    extra = {"x-subscription-type": "v2"}
    method = "POST" if body else "GET"
    signed = signer.build_headers(
        signer.SignCredential(AK, SK, TOK), method=method, url=URL,
        headers=extra, payload=body, x_sdk_date=NOW)
    assert signed["Authorization"] == _v11_reference(
        URL, body=body, extra=extra, method=method)


def test_v11_query_joins_signature():
    """query 必须参与签名：漏签实测回 401 `verify ak sk signature fail`。"""
    url = URL + "/usage?limit=5&sku_attr_code=officeace_points"
    extra = {"x-subscription-type": "v2"}
    signed = signer.build_headers(
        signer.SignCredential(AK, SK, TOK), method="GET", url=url,
        headers=extra, payload=b"", x_sdk_date=NOW)
    assert signed["Authorization"] == _v11_reference(url, body=b"", extra=extra)


def test_v11_signature_differs_when_query_dropped():
    """对照实验：同一路径带不带 query 的签名必须不同（否则是漏签）。"""
    extra = {"x-subscription-type": "v2"}
    with_query = signer.build_headers(
        signer.SignCredential(AK, SK, TOK), method="GET",
        url=URL + "?a=1", headers=extra, payload=b"", x_sdk_date=NOW)
    without = signer.build_headers(
        signer.SignCredential(AK, SK, TOK), method="GET", url=URL,
        headers=extra, payload=b"", x_sdk_date=NOW)
    assert with_query["Authorization"] != without["Authorization"]


def test_v11_project_id_is_signed():
    signed = signer.build_headers(
        signer.SignCredential(AK, SK, TOK, project_id="pid"), method="GET", url=URL,
        headers={"x-subscription-type": "v2"}, payload=b"", x_sdk_date=NOW)
    assert signed["X-Project-ID"] == "pid"
    assert "x-project-id" in signed["Authorization"]


def test_v11_does_not_inject_content_sha256_header():
    """多塞 `X-Sdk-Content-Sha256` 会改 SignedHeaders → 恒 401（实测）。"""
    signed = signer.build_headers(
        signer.SignCredential(AK, SK, TOK), method="GET", url=URL,
        headers={}, payload=b"", x_sdk_date=NOW)
    assert "X-Sdk-Content-Sha256" not in signed
    assert "x-sdk-content-sha256" not in signed["Authorization"]


def test_v11_scope_service_override():
    signed = signer.build_headers(
        signer.SignCredential(AK, SK, TOK), method="GET", url=URL, headers={},
        payload=b"", x_sdk_date=NOW, scope_service="other")
    assert "/other," in signed["Authorization"]


def test_v11_no_security_token_omits_header():
    signed = signer.build_headers(
        signer.SignCredential(AK, SK), method="GET", url=URL, headers={},
        payload=b"", x_sdk_date=NOW)
    assert "X-Security-Token" not in signed
    assert "x-security-token" not in signed["Authorization"]


def test_v4_reference_vector():
    """SDK-HMAC-SHA256：签名密钥就是 SK，StringToSign 没有 scope 行。"""
    url = "https://cloud.example.com/v1/claw/client-permission-validate"
    headers = {"host": "cloud.example.com", "x-sdk-date": NOW,
               "x-security-token": TOK, "content-type": "application/json;charset=utf8"}
    names = sorted(headers)
    head = "".join(f"{name}:{headers[name]}\n" for name in names)
    canonical = "\n".join([
        "GET", "/v1/claw/client-permission-validate/", "", head,
        ";".join(names), hashlib.sha256(b"").hexdigest()])
    string_to_sign = "\n".join(["SDK-HMAC-SHA256", NOW,
                                hashlib.sha256(canonical.encode()).hexdigest()])
    signature = hmac.new(SK.encode(), string_to_sign.encode(),
                         hashlib.sha256).hexdigest()
    want = (f"SDK-HMAC-SHA256 Access={AK}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}")

    signed = signer.build_v4_headers(
        signer.SignCredential(AK, SK, TOK), method="GET", url=url,
        headers={"Content-Type": "application/json;charset=utf8"},
        payload=b"", x_sdk_date=NOW)
    assert signed["Authorization"] == want


def test_v4_project_id_is_signed():
    signed = signer.build_v4_headers(
        signer.SignCredential(AK, SK, TOK, project_id="pid"), method="GET",
        url="https://cloud.example.com/x", headers={}, payload=b"",
        x_sdk_date=NOW)
    assert signed["X-Project-ID"] == "pid"
    assert "x-project-id" in signed["Authorization"]


def test_v4_without_security_token_omits_header():
    """永久 AK/SK（无 STS token）时不得带空的 X-Security-Token。"""
    signed = signer.build_v4_headers(
        signer.SignCredential(AK, SK), method="GET",
        url="https://cloud.example.com/x", headers={}, payload=b"",
        x_sdk_date=NOW)
    assert "X-Security-Token" not in signed
    assert "x-security-token" not in signed["Authorization"]
    assert "X-Project-ID" not in signed


def test_build_headers_does_not_mutate_input():
    headers = {"x-subscription-type": "v2"}
    signer.build_headers(signer.SignCredential(AK, SK, TOK), method="GET",
                         url=URL, headers=headers, payload=b"", x_sdk_date=NOW)
    assert headers == {"x-subscription-type": "v2"}


def test_signing_key_derivation_is_stable():
    first = signer.signing_key(SK, "20261021/cn-southwest-2/apic", AK)
    second = signer.signing_key(SK, "20261021/cn-southwest-2/apic", AK)
    assert first == second
    assert len(first) == 64
    assert first != signer.signing_key(SK, "20261022/cn-southwest-2/apic", AK)


def test_quote_segment_encodes_reserved_chars():
    assert signer.quote_segment("a/b c") == "a%2Fb%20c"
