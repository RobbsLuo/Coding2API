"""聊天出口候选收窄：目录/费率/冷却对 fan-out 的约束。"""

from __future__ import annotations

import time

import pytest

from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import ErrorOutcome, ModelCooldown, Scheduler
from src.provider.base import Model
from tests.api._support import RejectProvider as _RejectProvider
from tests.api._support import http_400 as _http_400
from tests.helpers.chat import chat_request as _request
from tests.helpers.events import GOOD


async def test_flat_exclusive_model_skips_other_upstream(dual_repo):
    """CodeBuddy 独有模型（目录可证归属）→ 只打 CB；TRAE 零调用、零统计记录。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    # pin TRAE：旧逻辑会先选中它并把请求真实打到 TRAE（留下无效请求记录）
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    records: list[dict] = []

    class _Stats:
        @staticmethod
        def record(**fields):
            records.append(fields)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="cb-only", stats=_Stats(),
        # "ghost" 未注册：顺带覆盖目录里混入未知上游的过滤分支
        model_aliases={"codebuddy": {"cb-only": "cb-only"},
                       "ghost": {"cb-only": "cb-only"}}))

    result = await executor.complete(_request("cb-only"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 0 and cb.calls == 1
    assert [(r["provider"], r["ok"]) for r in records] == [("codebuddy", True)]

async def test_free_tier_preferred_over_paid_when_catalog_has_rates(dual_repo):
    """候选中存在免费渠道（credit_rate=0）→ 免费渠道优先，不烧付费积分。

    实测故障（2026-10-08）：space-bunny 由 zen（`space-bunny-free`，x0）与
    CodeBuddy（`space-bunny`，x0.08）同时登记，合并成一条后调度按「到期
    积分多者先用」选中 CodeBuddy，免费 zen 被晾着——付费渠道的 36h 到期
    积分永远赢过免费渠道。现在 executor 把模型目录里的倍率注进候选，
    候选里出现免费渠道时它整体排前。
    """
    from src.provider.base import Model

    repo, _db = dual_repo
    # pin CB 且带足到期积分：旧排序必选 CB，证明免费档压过 pin 之外的
    # 一切既有指标（pin 本身仍是用户显式意图，最高优先）
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="zen", credential_data={})
    cb = _RejectProvider("codebuddy", [GOOD])
    zen = _RejectProvider("zen", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "zen": zen}, credentials=repo,
        scheduler=Scheduler(), default_model="space-bunny",
        model_aliases={"codebuddy": {"space-bunny": "space-bunny"},
                       "zen": {"space-bunny": "space-bunny-free"}},
        model_list_cache={
            "codebuddy": {"space-bunny": Model(id="space-bunny",
                                               credit_rate=0.08)},
            "zen": {"space-bunny-free": Model(id="space-bunny-free",
                                              credit_rate=0.0)}}))

    result = await executor.complete(_request("space-bunny"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert (cb.calls, zen.calls) == (0, 1)

async def test_free_tier_unknown_rate_keeps_legacy_order(dual_repo):
    """目录未就绪 / 倍率未知 → 不启用免费档，保持原排序（向后兼容）。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="zen", credential_data={})
    # zen pinned：旧排序 pin 优先。目录有别名但没有 zen 的倍率条目
    # （该渠道模型列表回退静态表丢掉该模型）→ zen 与付费同档
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'zen'")
    cb = _RejectProvider("codebuddy", [GOOD])
    zen = _RejectProvider("zen", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "zen": zen}, credentials=repo,
        scheduler=Scheduler(), default_model="space-bunny",
        model_aliases={"codebuddy": {"space-bunny": "space-bunny"},
                       "zen": {"space-bunny": "space-bunny-free"}},
        model_list_cache={"codebuddy": {"space-bunny": Model(
            id="space-bunny", credit_rate=0.08)}}))

    result = await executor.complete(_request("space-bunny"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert (cb.calls, zen.calls) == (0, 1)

async def test_free_tier_ignored_without_catalog(dual_repo):
    """ExecutorDeps.model_list_cache 缺省（None）→ 完全走原行为（老测试不破）。"""
    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="zen", credential_data={})
    cb = _RejectProvider("codebuddy", [GOOD])
    zen = _RejectProvider("zen", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "zen": zen}, credentials=repo,
        scheduler=Scheduler(), default_model="space-bunny",
        model_aliases={"codebuddy": {"space-bunny": "space-bunny"},
                       "zen": {"space-bunny": "space-bunny-free"}}))

    result = await executor.complete(_request("space-bunny"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls + zen.calls == 1

async def test_forced_provider_bypasses_catalog_narrowing(dual_repo):
    """@provider 强制指定不过滤：目录里只有 CB 登记也照样打 TRAE。"""
    from src.compat.openai.request import InvalidRequest

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="cb-only",
        model_aliases={"codebuddy": {"cb-only": "cb-only"}}))

    with pytest.raises(InvalidRequest):
        await executor.complete(_request("cb-only@trae"), username="u")
    assert trae.calls == 1 and cb.calls == 0

async def test_narrowing_disabled_without_catalog(dual_repo):
    """目录未就绪（model_aliases 缺省/为空）→ 保持双上游参与的原行为。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max"))

    result = await executor.complete(_request("qwen3.8-max"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and cb.calls == 1

async def test_narrowing_keeps_candidates_when_model_unknown_to_catalog(dual_repo):
    """目录就绪但没有已注册上游登记该模型 → 不过滤（黑名单滤掉仍可直连）。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max",
        model_aliases={"codebuddy": {"glm-5.2": "glm-5.2"},
                       "trae": {"GLM-5.2": "GLM-5.2"}}))

    result = await executor.complete(_request("qwen3.8-max"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and cb.calls == 1

async def test_narrowed_provider_without_usable_credential_does_not_fan_out(dual_repo):
    """收窄渠道无可用凭证 → 不扇出到未登记该模型的渠道（只信模型目录）。

    实测故障（2026-10-10 `longcat-2.5-preview`）：目录里只有 zen 登记它
    （原代号 `longcat-2.5-preview-free`），zen 匿名免费层 429 让虚拟凭证进
    60s 账号级冷却后，`_select` 旧逻辑放宽到全部渠道，CB/TRAE/kilo/CodeArts
    各回 11102/4001/401/404——客户端拿到误导性的 400 而非「该渠道暂时
    不可用」，还给 CB 写下 6 小时起步的 (凭证, 模型) 负缓存。现在直接出
    503，其余渠道零调用。此用例即该场景的合成版（唯一登记渠道被硬禁用）。
    """
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    # CodeBuddy 有凭证但被硬禁用：收窄到它之后没有任何可用候选
    db.connect().execute("UPDATE credentials SET disabled = 1 WHERE provider = 'codebuddy'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="deepseek-v4.1-flash",
        # 陈旧/降级目录：只登记了 CodeBuddy，TRAE 被漏掉
        model_aliases={"codebuddy": {"deepseek-v4.1-flash": "deepseek-v4.1-flash"}}))

    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request("deepseek-v4.1-flash"), username="u")
    assert (cb.calls, trae.calls) == (0, 0)

async def test_model_cooling_exhaustion_does_not_fan_out(dual_repo):
    """收窄渠道全部因**模型级冷却**不可选 → 不放宽重试（不扇出到不认的渠道）。

    实测故障（2026-10-08 15:53）：`qwen3.8-flash` 只有 Qoder 登记（内部原代号
    `qfmodel`），Qoder 节点故障（`[FAIL]node:oa_qwen-plus-main Execution failed`，
    按上游分类归 MODEL）让两条 Qoder 凭证进 600s 模型级冷却；此后每次请求收窄到
    `{qoder}` → 空 → 触发 broader 回退，把请求甩给不持有该模型的渠道——它们的
    别名表没有这个键、拿到的仍是原始名 `qwen3.8-flash`，必然被拒（CB 11102 /
    TRAE 4001 / zen·kilo 401），实测 6 次轮换全废、还给 CB 写下 6 小时起步的
    BLOCKED 负缓存。现在此路径不扇出，直接给模型级 503（换个模型即可用）。
    """
    repo, db = dual_repo
    qoder_a = repo.add(provider="qoder", credential_data={"accessToken": "a"})
    qoder_b = repo.add(provider="qoder", credential_data={"accessToken": "b"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    # 冷却登记用**上游原始名**（冷却表按各渠道自己调用的名字）
    cool = ErrorOutcome(model_cooldowns={"qfmodel": ModelCooldown(
        cooling_until=int(time.time()) + 600, hits=1, reason="model")})
    repo.save_error(qoder_a, cool)
    repo.save_error(qoder_b, cool)
    qoder = _RejectProvider("qoder", [GOOD])
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"qoder": qoder, "codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-flash",
        # 只有 Qoder 登记该模型（内部原代号 qfmodel）
        model_aliases={"qoder": {"qwen3.8-flash": "qfmodel"}},
        # 与 main.py 同款注入：模型冷却按各渠道实际调用的名字（qfmodel）对齐
        upstream_model_name=lambda pid, name: {
            "qoder": {"qwen3.8-flash": "qfmodel"}}.get(pid, {}).get(name.lower(), name),
        model_suggestions=lambda _name: ["qwen3.8-max"]))

    with pytest.raises(NoHealthyCredential) as exc_info:
        await executor.complete(_request("qwen3.8-flash"), username="u")
    assert "temporarily unavailable on upstream" in str(exc_info.value)
    assert "qwen3.8-max" in str(exc_info.value)
    assert (qoder.calls, cb.calls, trae.calls) == (0, 0, 0)
    # 负缓存零新增：没有渠道被写「该后端无此模型」
    assert db.connect().execute(
        "SELECT COUNT(*) FROM credential_model_cooldowns").fetchone()[0] == 2

async def test_model_cooled_registered_peer_takes_over(dual_repo):
    """收窄渠道模型级冷却、但另一条**已登记**渠道有可用凭证 → 走登记渠道。

    非扇出路径：目录同时登记了 qoder 与 codebuddy，qoder 被模型级冷却后
    收窄候选池里 codebuddy 本身就是可用候选，无需放宽到任何未登记渠道。
    """
    repo, db = dual_repo
    qoder_ok = repo.add(provider="qoder", credential_data={"accessToken": "a"})
    dead = repo.add(provider="qoder", credential_data={"accessToken": "b"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    # 一条账号级硬禁用 + 一条模型级冷却：混有成因也不放宽（登记渠道已够用）
    db.connect().execute("UPDATE credentials SET disabled = 1 WHERE id = ?", (dead,))
    repo.save_error(qoder_ok, ErrorOutcome(model_cooldowns={"qfmodel": ModelCooldown(
        cooling_until=int(time.time()) + 600, hits=1, reason="model")}))
    qoder = _RejectProvider("qoder", [GOOD])
    cb = _RejectProvider("codebuddy", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"qoder": qoder, "codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-flash",
        # 目录：qoder 与 codebuddy 都登记了该模型，qoder 又不可用 → 回落 CB
        model_aliases={"qoder": {"qwen3.8-flash": "qfmodel"},
                       "codebuddy": {"qwen3.8-flash": "qwen3.8-flash"}},
        upstream_model_name=lambda pid, name: {
            "qoder": {"qwen3.8-flash": "qfmodel"}}.get(pid, {}).get(name.lower(), name)))

    result = await executor.complete(_request("qwen3.8-flash"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert (qoder.calls, cb.calls) == (0, 1)

async def test_narrowed_channel_without_credentials_does_not_broaden(dual_repo):
    """收窄渠道**一条凭证都没有** → 不放宽到未登记渠道（无候选 ≠ 该试别的）。

    目录说该模型只在 codearts，而 codearts 一条凭证都没有：直接出 503，
    不去打没登记它的 codebuddy——那只会拿 11102/4001 换一条误导性错误。
    """
    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    cb = _RejectProvider("codebuddy", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "codearts": _RejectProvider("codearts", [GOOD])},
        credentials=repo, scheduler=Scheduler(), default_model="m-only",
        # 只有 codearts 登记，但该渠道无凭证 → 不放宽
        model_aliases={"codearts": {"m-only": "m-only"}}))

    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request("m-only"), username="u")
    assert cb.calls == 0

async def test_owner_channel_account_cooled_does_not_fan_out(dual_repo):
    """持有渠道账号级冷却（429）→ 不扇出到未登记渠道（2026-10-10 longcat 实测）。

    `longcat-2.5-preview` 在模型目录里只有 zen 登记（原代号
    `longcat-2.5-preview-free`）。zen 匿名免费层 429（kind=SOFT）让虚拟凭证
    进 60s 账号级冷却后，旧逻辑每次请求都放宽打 CB/TRAE/kilo/CodeArts——各回
    11102/4001/401/404，客户端拿到 400 `invalid_request` 而非「zen 暂时
    不可用」，还给 CB 写下 6 小时起步的负缓存（线上实测 usage_events 里同一
    模型同时出现 kilo/codearts/codebuddy/trae 的失败记录）。现在只信目录：
    zen 冷却中则零上游调用，直接出 503。
    """
    repo, db = dual_repo
    zen_id = repo.add(provider="zen", credential_data={})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    repo.add(provider="kilo", credential_data={})
    # zen 被 429 打冷却（账号级 SOFT）：目录里唯一登记该模型的渠道不可用
    repo.save_error(zen_id, ErrorOutcome(cooling_until=int(time.time()) + 60,
                                         err_count=0))
    zen = _RejectProvider("zen", [GOOD])
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    kilo = _RejectProvider("kilo", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"zen": zen, "codebuddy": cb, "trae": trae, "kilo": kilo},
        credentials=repo, scheduler=Scheduler(), default_model="longcat-2.5-preview",
        model_aliases={"zen": {"longcat-2.5-preview": "longcat-2.5-preview-free"}},
        upstream_model_name=lambda pid, name: {
            "zen": {"longcat-2.5-preview": "longcat-2.5-preview-free"}}
        .get(pid, {}).get(name.lower(), name)))

    with pytest.raises(NoHealthyCredential) as exc_info:
        await executor.complete(_request("longcat-2.5-preview"), username="u")
    assert "all credentials unavailable" in str(exc_info.value)
    assert (zen.calls, cb.calls, trae.calls, kilo.calls) == (0, 0, 0, 0)
    # 零负缓存：没有任何渠道被写「该后端无此模型」
    assert db.connect().execute(
        "SELECT COUNT(*) FROM credential_model_cooldowns").fetchone()[0] == 0

async def test_forced_provider_without_usable_credential_keeps_narrowing(dual_repo):
    """@provider 强制指定：该渠道无可用凭证也不回退（强制语义不被兜底削弱）。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    db.connect().execute("UPDATE credentials SET disabled = 1 WHERE provider = 'codebuddy'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="deepseek-v4.1-flash",
        model_aliases={"codebuddy": {"deepseek-v4.1-flash": "deepseek-v4.1-flash"},
                       "trae": {"deepseek-v4.1-flash": "deepseek-v4.1-flash"}}))

    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request("deepseek-v4.1-flash@codebuddy"), username="u")
    assert (cb.calls, trae.calls) == (0, 0)


async def test_probe_failed_model_still_narrows_to_its_owner(dual_repo):
    """探活判死的模型仍收窄到持有它的渠道 → 不扇出到无此模型的渠道。

    实测故障（2026-10-26 `deepseek-v4.1-flash-1017`）：officeAce 的
    `/v2/models` 挂着它但推理端点直接 404 `ModelArts.81009`（上游目录撒谎）。
    人工探活判死后，若别名登记跟着被摘掉，执行侧 `_narrow_providers` 因
    「没有任何渠道登记该模型」回退成**全部渠道** → 请求扇出到 codebuddy /
    codearts / codebuddy_intl，三家各回「无此模型」（11102 / not registered），
    并被写上 (凭证, 模型) 负缓存——污染的是从没有该模型的渠道。

    修好后：别名登记保留 → 收窄到 officeAce → 负缓存让该渠道的凭证不可选
    → 快速 503「该模型暂时不可用」，其余渠道零调用、零负缓存。
    """
    repo, db = dual_repo
    officeace_id = repo.add(provider="officeace", credential_data={"t": "x"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="codearts", credential_data={"t": "x"})
    # 负缓存（判死的前提）：officeAce 的这个模型被负缓存锁住
    repo.save_error(officeace_id, ErrorOutcome(model_cooldowns={
        "deepseek-v4.1-flash-1017": ModelCooldown(
            cooling_until=int(time.time()) + 3600, hits=1, reason="blocked")}))
    officeace = _RejectProvider("officeace", [GOOD])
    cb = _RejectProvider("codebuddy", [GOOD])
    codearts = _RejectProvider("codearts", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"officeace": officeace, "codebuddy": cb, "codearts": codearts},
        credentials=repo, scheduler=Scheduler(),
        default_model="deepseek-v4.1-flash-1017",
        # 别名表保留判死渠道的归属（`api.models._hidden_aliases` 的产出）
        model_aliases={"officeace": {"deepseek-v4.1-flash-1017":
                                     "deepseek-v4.1-flash-1017"}},
        upstream_model_name=lambda pid, name: {
            "officeace": {"deepseek-v4.1-flash-1017": "deepseek-v4.1-flash-1017"}
        }.get(pid, {}).get(name.lower(), name)))

    with pytest.raises(NoHealthyCredential) as exc_info:
        await executor.complete(_request("deepseek-v4.1-flash-1017"), username="u")
    assert "temporarily unavailable on upstream" in str(exc_info.value)
    # 只有持有该模型的渠道被考虑过；无此模型的渠道零调用
    assert (officeace.calls, cb.calls, codearts.calls) == (0, 0, 0)
    # 负缓存零新增：没给任何渠道写「该后端无此模型」
    assert db.connect().execute(
        "SELECT COUNT(*) FROM credential_model_cooldowns").fetchone()[0] == 1
