"""重新登录合并键：渠道 → 稳定上游身份。

登录落库（`api/credential_login.py`）与运维侧的历史清理共用这一套判据。

这里刻意**不 import 任何渠道子包**（`codearts` / `officeace` 的门面要反向
import 本模块，否则成环），只按落盘 JSON 的字段名取值——字段名是落盘契约，
与 `credential.py` 的 `to_dict()` 一致，改动时三处（`to_dict` / `from_dict` /
本模块）必须同步。

为什么不能比对整份凭证 JSON：重新登录后 access token / refresh_token /
DPoP 私钥必然全变，整份比对永远不相等，结果是每次登录都新增一条。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _text(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def codearts_login_identity(credential_data: dict[str, Any]) -> str:
    """CodeArts：`uid`（caller-identity 的 principal_id）；缺则 `domain_id|user_name`。"""
    uid = _text(credential_data, "uid", "user_id", "userId")
    if uid:
        return uid
    domain_id = _text(credential_data, "domain_id", "domainId")
    user_name = _text(credential_data, "user_name", "userName")
    return f"{domain_id}|{user_name}" if domain_id and user_name else ""


def officeace_login_identity(credential_data: dict[str, Any]) -> str:
    """OfficeAce：`account_id`；缺则 `principal_id`。"""
    return _text(credential_data, "account_id", "accountId",
                 "principal_id", "principalId")


#: 只有声明了身份的渠道才参与重新登录合并 / 历史重复清理。
LOGIN_IDENTITIES: dict[str, Callable[[dict[str, Any]], str]] = {
    "codearts": codearts_login_identity,
    "officeace": officeace_login_identity,
}


def login_identity_for(provider_id: str, credential_data: dict[str, Any]) -> str:
    """取该渠道的稳定身份；渠道未声明 / 解析失败 / 空值一律返回空串。

    返回空串 = 无法安全合并，调用方退回「新增」或「跳过」，绝不猜。
    """
    getter = LOGIN_IDENTITIES.get(provider_id)
    if getter is None:
        return ""
    try:
        value = getter(credential_data)
    except Exception:  # noqa: BLE001 - 畸形凭证不影响登录本身
        return ""
    return value if isinstance(value, str) else ""
