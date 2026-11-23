"""凭证级模型白名单 + 编辑版本号（乐观并发）的仓储层行为。

两个功能的落点都在 `credentials` 行上：
- `allowed_models`：只在**候选生成**（`candidates(model=...)`）时过滤，不改
  库内状态、不影响不带 model 的后台任务；
- `updated_at`：人工编辑推进版本号，`apply_credential_edit` 用它做单条 UPDATE
  的原子比对（冲突返回 None，一行不改）。
"""

from __future__ import annotations


def _add(credentials, **kwargs):
    return credentials.add(provider="trae", credential_data={"accessToken": "a"}, **kwargs)


def test_candidates_filters_by_model_whitelist(repo):
    """命中白名单 → 参与本次选号；不命中 → 被摘掉。"""
    credentials, _db = repo
    hit = _add(credentials)
    miss = _add(credentials)
    assert credentials.apply_credential_edit(hit, allowed_models="glm-*") is not None
    assert credentials.apply_credential_edit(miss, allowed_models="kimi-*") is not None

    ids = {c.credential_id for c in credentials.candidates(model="glm-4.6")}
    assert hit in ids
    assert miss not in ids

    # 换一个模型：结论正好相反（不是硬禁用，库里状态没被动过）
    ids = {c.credential_id for c in credentials.candidates(model="kimi-k3")}
    assert miss in ids
    assert hit not in ids


def test_candidates_empty_whitelist_means_unrestricted(repo):
    """`''` = 不限制：任何模型都参与选号（老库补列后的默认）。"""
    credentials, _db = repo
    credential_id = _add(credentials)
    ids = {c.credential_id for c in credentials.candidates(model="anything")}
    assert credential_id in ids


def test_candidates_multiple_globs(repo):
    """多个逗号分隔的 glob 是「或」关系，匹配大小写不敏感。"""
    credentials, _db = repo
    credential_id = _add(credentials)
    credentials.apply_credential_edit(credential_id, allowed_models="GLM-*, kimi-k3")
    for model in ("glm-4.6", "kimi-k3"):
        assert {c.credential_id for c in credentials.candidates(model=model)} == {
            credential_id}
    assert credentials.candidates(model="gpt-5") == []


def test_candidates_without_model_ignores_whitelist(repo):
    """不传 model（签到/刷新/探活/额度探测）看到全量：白名单只管对话选号。"""
    credentials, _db = repo
    credential_id = _add(credentials)
    credentials.apply_credential_edit(credential_id, allowed_models="glm-*")
    assert {c.credential_id for c in credentials.candidates()} == {credential_id}
    assert {c.credential_id for c in credentials.candidates(selectable_only=True)} == {
        credential_id}


def test_candidates_unknown_or_auto_model_is_unattributable(repo):
    """请求没点名具体模型（空 / auto）时无法归因，放行而不是把池子摘空。"""
    credentials, _db = repo
    credential_id = _add(credentials)
    credentials.apply_credential_edit(credential_id, allowed_models="glm-*")
    for model in (None, "", "auto", "AUTO"):
        assert {c.credential_id for c in credentials.candidates(model=model)} == {
            credential_id}


def test_candidates_uses_resolved_name_without_provider_suffix(repo):
    """`model@provider` 的 `@` 后缀不参与匹配（渠道由 provider_binding 管）。"""
    credentials, _db = repo
    credential_id = _add(credentials)
    credentials.apply_credential_edit(credential_id, allowed_models="glm-*")
    assert {c.credential_id for c in credentials.candidates(model="glm-4@trae")} == {
        credential_id}


def test_apply_credential_edit_advances_monotonic_version(repo):
    """版本号严格单调：同一秒内的两次编辑也不会取到同一个值。"""
    credentials, _db = repo
    credential_id = _add(credentials)
    first = credentials.apply_credential_edit(
        credential_id, allowed_models="glm-*", now=1_000)
    second = credentials.apply_credential_edit(
        credential_id, allowed_models="glm-*", now=1_000)
    assert first == 1_000
    assert second == 1_001


def test_apply_credential_edit_conflict_changes_nothing(repo):
    """陈旧版本 → None 且不写任何字段（并发两次写只有一次成功）。"""
    credentials, _db = repo
    credential_id = _add(credentials)
    version = credentials.apply_credential_edit(
        credential_id, allowed_models="glm-*", now=1_000)
    assert credentials.apply_credential_edit(
        credential_id, allowed_models="kimi-*", expected_updated_at=0,
        now=1_001) is None
    snapshot = credentials.edit_snapshot(credential_id)
    assert snapshot["updated_at"] == version
    assert snapshot["allowed_models"] == "glm-*"


def test_apply_credential_edit_without_expected_version_is_legacy(repo):
    """不传版本 = 兼容口子：保持「最后写入者赢」，且照旧推进版本号。"""
    credentials, _db = repo
    credential_id = _add(credentials)
    assert credentials.apply_credential_edit(
        credential_id, allowed_models="glm-*", now=1_000) == 1_000
    assert credentials.apply_credential_edit(
        credential_id, allowed_models="", now=1_002) == 1_002
    assert credentials.edit_snapshot(credential_id)["allowed_models"] == ""


def test_apply_credential_edit_missing_credential_returns_none(repo):
    credentials, _db = repo
    assert credentials.apply_credential_edit("ghost", allowed_models="") is None


def test_edit_snapshot_missing_credential_returns_empty(repo):
    """并发删除后没有行可读：返回空 dict（调用方 `.get(..., 默认)` 即可）。"""
    credentials, _db = repo
    assert credentials.edit_snapshot("ghost") == {}


def test_manual_edits_advance_version_background_writes_do_not(repo):
    """人工编辑推进版本；后台自动写（冷却/额度/健康度）不推进。

    否则 30s 一次的轮询（save_error/save_quota）会把打开中的编辑页写成
    「已过期」，冲突检测退化成永远 409。
    """
    from src.engine.scheduler import ErrorOutcome

    credentials, _db = repo
    credential_id = _add(credentials)
    assert credentials.edit_snapshot(credential_id)["updated_at"] == 0

    credentials.set_tags(credential_id, ["a"], now=10)
    assert credentials.edit_snapshot(credential_id)["updated_at"] == 10

    credentials.set_routing_weight(credential_id, 3, now=11)
    assert credentials.edit_snapshot(credential_id)["updated_at"] == 11

    credentials.set_enabled(credential_id, False, now=12)
    assert credentials.edit_snapshot(credential_id)["updated_at"] == 12

    credentials.revive(credential_id, now=13)
    assert credentials.edit_snapshot(credential_id)["updated_at"] == 13

    # 后台写：不推进版本
    credentials.save_error(credential_id, ErrorOutcome(
        err_count=1, cooling_until=99, cooling_kind="soft"))
    assert credentials.edit_snapshot(credential_id)["updated_at"] == 13

    credentials.set_web_cookie_state(credential_id, "ok")
    assert credentials.edit_snapshot(credential_id)["updated_at"] == 13


def test_list_all_exposes_edit_version_and_whitelist(repo):
    """管理台列表带 updated_at / allowed_models（编辑对话框据此带版本号）。"""
    credentials, _db = repo
    credential_id = _add(credentials)
    credentials.apply_credential_edit(credential_id, allowed_models="glm-*", now=42)
    row = credentials.list_all()[0]
    assert row["updated_at"] == 42
    assert row["allowed_models"] == "glm-*"