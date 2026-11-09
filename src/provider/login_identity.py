"""重新登录合并键：渠道 → 稳定上游身份。

登录落库（`api/credential_login.py`）与运维侧的历史清理共用这一套判据。

这里刻意**不 import 任何渠道子包**（`codearts` / `officeace` 的门面要反向
import 本模块，否则成环），只按落盘 JSON 的字段名取值——字段名是落盘契约，
与 `credential.py` 的 `to_dict()` 一致，改动时三处（`to_dict` / `from_dict` /
本模块）必须同步。

为什么不能比对整份凭证 JSON：重新登录后 access token / refresh_token /
DPoP 私钥必然全变，整份比对永远不相等，结果是每次登录都新增一条。

**为什么是身份集合而不是单个键**：CodeArts 的 `uid` 优先、`domain_id|user_name`
兜底。若只认首选键，一次身份补全接口失败（`oauth._fill_identity` 尽力而为，
网络错误只记日志）的登录拿不到 `uid`，落库键退化成 `domain_id|user_name`，
与老行的 `uid` 键**永远不相等** → 又新增一条重复凭证，正是本功能要消除的
回归。故两侧都给出**全部可推导别名**，按交集判定同一账号。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from typing import Any

#: 同一账号的别名集合；空集 = 无法安全合并。
IdentitySet = frozenset[str]


def _text(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _domain_name(raw: dict[str, Any]) -> str:
    domain_id = _text(raw, "domain_id", "domainId")
    user_name = _text(raw, "user_name", "userName")
    return f"{domain_id}|{user_name}" if domain_id and user_name else ""


def codearts_login_identity(credential_data: dict[str, Any]) -> str:
    """CodeArts 首选身份：`uid`（caller-identity 的 principal_id）；缺则 `domain_id|user_name`。"""
    uid = _text(credential_data, "uid", "user_id", "userId")
    return uid or _domain_name(credential_data)


def codearts_login_identities(credential_data: dict[str, Any]) -> IdentitySet:
    """CodeArts 全部别名：`uid` 与 `domain_id|user_name` 都给出（有才给）。

    两者不互斥：老行可能只有 `uid`，新登录可能只有 `domain|user`，交集非空
    即同一账号。
    """
    aliases = {
        _text(credential_data, "uid", "user_id", "userId"),
        _domain_name(credential_data),
    }
    return frozenset(alias for alias in aliases if alias)


def officeace_login_identity(credential_data: dict[str, Any]) -> str:
    """OfficeAce 首选身份：`account_id`；缺则 `principal_id`。"""
    return _text(credential_data, "account_id", "accountId",
                 "principal_id", "principalId")


def officeace_login_identities(credential_data: dict[str, Any]) -> IdentitySet:
    """OfficeAce 全部别名：`account_id` 与 `principal_id` 都给出（有才给）。"""
    return frozenset(
        alias for alias in (
            _text(credential_data, "account_id", "accountId"),
            _text(credential_data, "principal_id", "principalId"),
        ) if alias)


def _lingxi_sid_identity(raw: dict[str, Any]) -> str:
    """sid 哈希身份：`sid-<sha256 前 16 位>`（与 `lingxi/credential.identity` 同口径）。

    刻意不 import `lingxi` 子包（门面反向 import 本模块，会成环），故在这里
    复制同一段纯函数；两处改口径必须同步（有测试钉住）。
    """
    sid = _text(raw, "wps_sid")
    if not sid:
        return ""
    return "sid-" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]


def lingxi_login_identity(credential_data: dict[str, Any]) -> str:
    """灵犀首选身份：`uid`；缺则 sid 哈希（重新登录后 sid 会变，仅兜底）。"""
    return _text(credential_data, "uid") or _lingxi_sid_identity(credential_data)


def lingxi_login_identities(credential_data: dict[str, Any]) -> IdentitySet:
    """灵犀全部别名：`uid` 与 sid 哈希（老行只有 sid 时靠它合并）。"""
    aliases = {_text(credential_data, "uid"),
               _lingxi_sid_identity(credential_data)}
    return frozenset(alias for alias in aliases if alias)


#: 首选身份（展示 / 门面转发用）；只有声明了身份的渠道才参与重新登录合并。
LOGIN_IDENTITIES: dict[str, Callable[[dict[str, Any]], str]] = {
    "codearts": codearts_login_identity,
    "officeace": officeace_login_identity,
    "lingxi": lingxi_login_identity,
}

#: 别名集合（合并匹配用）：与 `LOGIN_IDENTITIES` 必须覆盖同一批渠道。
LOGIN_IDENTITY_SETS: dict[str, Callable[[dict[str, Any]], IdentitySet]] = {
    "codearts": codearts_login_identities,
    "officeace": officeace_login_identities,
    "lingxi": lingxi_login_identities,
}


def _safe_call(getter: Callable[[dict[str, Any]], Any],
               credential_data: dict[str, Any]) -> Any:
    """畸形凭证不抛：登录路径不能因为一条脏数据整体失败。"""
    try:
        return getter(credential_data)
    except Exception:  # noqa: BLE001 - 畸形凭证不影响登录本身
        return None


def login_identity_for(provider_id: str, credential_data: dict[str, Any]) -> str:
    """取该渠道的首选身份；渠道未声明 / 解析失败 / 空值一律返回空串。

    返回空串 = 无法安全合并，调用方退回「新增」或「跳过」，绝不猜。
    """
    getter = LOGIN_IDENTITIES.get(provider_id)
    if getter is None:
        return ""
    value = _safe_call(getter, credential_data)
    return value if isinstance(value, str) else ""


def login_identities_for(provider_id: str,
                         credential_data: dict[str, Any]) -> IdentitySet:
    """取该渠道的全部身份别名；渠道未声明 / 解析失败 / 全部为空 → 空集。"""
    getter = LOGIN_IDENTITY_SETS.get(provider_id)
    if getter is None:
        return frozenset()
    value = _safe_call(getter, credential_data)
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return frozenset()
    return frozenset(alias for alias in value
                     if isinstance(alias, str) and alias)
