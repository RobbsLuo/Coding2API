"""上游 OAuth 登录结果的落库策略：同一账号再次登录时合并，而不是新增一行。

为什么单独抽一层：三条登录路径（轮询 / 粘贴回调 `/upstream/complete` /
浏览器回调 `/authorize`）原来各写一份 `credentials.add(...)`，重新登录一律
新增。CodeArts / OfficeAce 的凭证会因 refresh_token 一次性作废、STS 到期等
原因需要重新登录；按旧行为每登一次池子里就多一条同账号凭证，选号、额度、
统计都会被重复账号污染。

合并键由 `provider/login_identity.py` 统一给（只能比稳定身份——CodeArts
`uid`、OfficeAce `account_id`），不能比整份 JSON：重新登录后 token / DPoP
私钥必然全变。
"""

from __future__ import annotations

from typing import Any

from ..provider.login_identity import LOGIN_IDENTITIES, login_identity_for
from .deps import Services


def provider_supports_merge(provider_id: str) -> bool:
    """该渠道是否声明了稳定身份（决定「同账号重新登录」能否合并）。"""
    return provider_id in LOGIN_IDENTITIES


async def save_login_credential(
        services: Services, *,
        provider_id: str,
        credential_data: dict[str, Any],
        nickname: str,
        added_by: str,
) -> tuple[str, bool]:
    """登录结果落库，返回 `(credential_id, 是否为合并更新)`。

    - 有稳定身份且命中同渠道既有凭证 → 就地更新（解除硬禁用/冷却），保留
      `enabled` / `pinned` / `tags` / `added_by` / `created_at`。
    - 没有身份、没有命中、或命中行在加锁前被删除 → 新增。

    更新走凭证级编辑锁：预刷新任务也是「解密 → await 上游 → 整份覆写」，
    不加锁时两边的快照会互相覆盖。
    """
    def identity_of(data: dict[str, Any]) -> str:
        return login_identity_for(provider_id, data)

    target = login_identity_for(provider_id, credential_data)
    if target:
        existing_id = services.credentials.find_login_credential(
            provider_id, identity_of, target)
        if existing_id is not None:
            async with services.credentials.edit_lock(existing_id):
                if services.credentials.merge_login_credential(
                        existing_id, credential_data, nickname):
                    return existing_id, True
    credential_id = services.credentials.add(
        provider=provider_id, credential_data=credential_data,
        nickname=nickname, added_by=added_by)
    return credential_id, False
