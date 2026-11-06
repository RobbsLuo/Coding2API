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

import asyncio
from typing import Any

from ..provider.login_identity import login_identities_for
from .deps import Services

#: 登录落库的渠道锁：单进程内同一渠道的「查 → 合并 → 新增」串行化。
#:
#: 为什么凭证级 `edit_lock` 不够：它按 `credential_id` 分键，而「第一次登录」
#: 还没有 id 可键。两个并发登录（两个标签页 / 轮询超时后重试）都查到 None →
#: 都走 `add()`，池子里就是两条同账号凭证，正是本模块要消除的重复。
#:
#: 为什么按渠道而不是按身份分键：同一次登录的别名集合可能不同（身份补全接口
#: 偶发失败时只有兜底键），按集合分键会让「{uid}」与「{uid, domain|user}」
#: 两把锁互不排斥，等于没锁。登录是低频人工动作（一天几次），渠道级串行
#: 完全够用；选号/聊天热路径不经过这里。
_login_locks: dict[str, asyncio.Lock] = {}


def _login_lock(provider_id: str) -> asyncio.Lock:
    lock = _login_locks.get(provider_id)
    if lock is None:
        lock = asyncio.Lock()
        _login_locks[provider_id] = lock
    return lock


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

    **查 → 合并 → 新增整段持渠道登录锁**：`find_login_credential` 读库与
    `credentials.add` 写库之间若被并发登录插进来，两边都会查到「没有既有
    凭证」并各插一行。渠道级锁让同一渠道的并发登录串行化（登录是低频人工
    动作）；不同渠道互不阻塞。

    更新另走凭证级编辑锁：预刷新任务也是「解密 → await 上游 → 整份覆写」，
    不加锁时两边的快照会互相覆盖。
    """
    target = login_identities_for(provider_id, credential_data)
    if not target:
        # 没有稳定身份 = 不能安全合并，直接新增（不加锁也没人有东西可抢）。
        credential_id = services.credentials.add(
            provider=provider_id, credential_data=credential_data,
            nickname=nickname, added_by=added_by)
        return credential_id, False

    async with _login_lock(provider_id):
        existing_id = services.credentials.find_login_credential(
            provider_id, lambda data: login_identities_for(provider_id, data), target)
        if existing_id is not None:
            async with services.credentials.edit_lock(existing_id):
                if services.credentials.merge_login_credential(
                        existing_id, credential_data, nickname):
                    return existing_id, True
        credential_id = services.credentials.add(
            provider=provider_id, credential_data=credential_data,
            nickname=nickname, added_by=added_by)
        return credential_id, False
