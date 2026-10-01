"""CodeArts 历史数据从 **token** 口径折算成「积分」口径。

背景：CodeArts 上游余额接口返回的是每日免费 token 池（千万量级），本服务原先
直接把 token 落库、也把福利模型的单请求扣池按 token 记进 `usage_events.credit`。
改为统一「积分」（1 积分 = 10000 token，见 `units`）后，**新**数据会在
`parse_balance` / `_fill_estimated_credit` 里先折算，但**改动前**已落库的历史
数据仍是 token 口径，必须一次性折算到同一单位，否则额度排序与统计口径分叉。

处理范围（仅 provider='codearts'）：
- `credentials`：`quota_remaining` / `quota_total` / `quota_expiry_ladder`
  （JSON 金额）/ `quota_packages`（JSON 的 total、used）；
- `usage_events`：`credit`（非空的行）；
- `usage_hourly`：`credit_sum`（非空的行）——直接除，不重算，避免丢掉
  「明细已过保留期、只剩汇总」的历史小时；
- `credit_events`：属于 codearts 凭证的 `before` / `after` / `delta`。

**仅执行一次**：折算即除以 `TOKENS_PER_CREDIT`，重复执行会把已折算的值再除一遍。
`scripts/convert_codearts_credit_unit.py` 默认只预览、需 `--apply` 才写库，并在
写库前备份。

模块还提供 `backfill_estimated_credit`：补齐**福利模型**历史明细里为空的推算积分
（`scripts/backfill_codearts_credit.py` 调用），与上面的单位折算是两件事、互不影响。
"""

from __future__ import annotations

import json
from typing import Any

from .client import BENEFIT_SEED
from .units import TOKENS_PER_CREDIT, tokens_to_credits


def _number_to_credits(value: Any) -> Any:
    """数值 → 积分；非数值原样返回（历史脏数据不因折算而丢形状）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return value / TOKENS_PER_CREDIT


def _ladder_to_credits(text: str | None) -> str | None:
    """到期阶梯 JSON `[[epoch, 剩余], ...]` → 金额折算成积分；脏数据原样保留。"""
    if not text:
        return text
    try:
        items = json.loads(text)
    except ValueError:
        return text
    if not isinstance(items, list):
        return text
    out = []
    for item in items:
        if isinstance(item, list) and len(item) == 2:
            out.append([item[0], _number_to_credits(item[1])])
        else:
            out.append(item)
    return json.dumps(out)


def _packages_to_credits(text: str | None) -> str | None:
    """额度包明细 JSON → `total` / `used` 折算成积分；脏数据原样保留。"""
    if not text:
        return text
    try:
        items = json.loads(text)
    except ValueError:
        return text
    if not isinstance(items, list):
        return text
    out = []
    for item in items:
        if isinstance(item, dict):
            item = dict(item)
            for key in ("total", "used"):
                if key in item:
                    item[key] = _number_to_credits(item[key])
            out.append(item)
        else:
            out.append(item)
    return json.dumps(out, ensure_ascii=False)


def pending(conn) -> dict[str, int]:
    """待折算的行数（预览用）：按表分批统计，不写库。"""
    return {
        "credentials": conn.execute(
            "SELECT COUNT(*) FROM credentials WHERE provider = 'codearts'"
        ).fetchone()[0],
        "usage_events": conn.execute(
            "SELECT COUNT(*) FROM usage_events "
            "WHERE provider = 'codearts' AND credit IS NOT NULL"
        ).fetchone()[0],
        "usage_hourly": conn.execute(
            "SELECT COUNT(*) FROM usage_hourly "
            "WHERE provider = 'codearts' AND credit_sum IS NOT NULL"
        ).fetchone()[0],
        "credit_events": conn.execute(
            "SELECT COUNT(*) FROM credit_events WHERE credential_id IN "
            "(SELECT id FROM credentials WHERE provider = 'codearts')"
        ).fetchone()[0],
    }


def convert_codearts_credit_unit(db) -> dict[str, int]:
    """把 codearts 历史数据由 token 折算为积分，返回各表更新/处理条数。

    `db` 只用 `transaction()`（鸭子类型），便于测试与脚本注入裸连接适配器。
    幂等性**不由本函数保证**（除法不可逆），调用方负责只执行一次。
    """
    with db.transaction() as conn:
        creds = conn.execute(
            "SELECT id, quota_remaining, quota_total, quota_expiry_ladder, quota_packages "
            "FROM credentials WHERE provider = 'codearts'").fetchall()
        for row in creds:
            conn.execute(
                "UPDATE credentials SET quota_remaining = ?, quota_total = ?,"
                " quota_expiry_ladder = ?, quota_packages = ? WHERE id = ?",
                (_number_to_credits(row["quota_remaining"]),
                 _number_to_credits(row["quota_total"]),
                 _ladder_to_credits(row["quota_expiry_ladder"]),
                 _packages_to_credits(row["quota_packages"]),
                 row["id"]))
        events = conn.execute(
            "UPDATE usage_events SET credit = credit / ? "
            "WHERE provider = 'codearts' AND credit IS NOT NULL",
            (TOKENS_PER_CREDIT,)).rowcount
        hourly = conn.execute(
            "UPDATE usage_hourly SET credit_sum = credit_sum / ? "
            "WHERE provider = 'codearts' AND credit_sum IS NOT NULL",
            (TOKENS_PER_CREDIT,)).rowcount
        credit_events = conn.execute(
            "UPDATE credit_events SET before = before / ?, after = after / ?,"
            " delta = delta / ? WHERE credential_id IN "
            "(SELECT id FROM credentials WHERE provider = 'codearts')",
            (TOKENS_PER_CREDIT, TOKENS_PER_CREDIT, TOKENS_PER_CREDIT)).rowcount
    return {
        "credentials": len(creds),
        "usage_events": events,
        "usage_hourly": hourly,
        "credit_events": credit_events,
    }


def benefit_models(conn) -> set[str]:
    """可按每日 token 池推算积分的模型名（小写）。

    判定用**本服务留下的证据**而不是上游目录：目录是按账号授予且只在内存里
    （`CodeArtsClient._benefit_models`），回填时拿不到、也不该发请求去问。而
    「这个模型曾经被记过推算积分」（`credit_estimated=1`）就说明它当时确实走了
    福利路由、并真的扣了那条每日池——正是回填要复原的口径。

    内置模型（如 `openpangu-2.0-flash`）走 `credit[]` 的付费倍率、**不**扣每日
    池，没有任何证据会落进这个集合，故它们的空 `credit` 保持空（展示层 `—`），
    不会被错算成池消耗。
    """
    proven = {row[0] for row in conn.execute(
        "SELECT DISTINCT model FROM usage_events WHERE provider = 'codearts' "
        "AND credit_estimated = 1 AND credit IS NOT NULL")}
    return {model.lower() for model in proven} | set(BENEFIT_SEED)


def backfill_estimated_credit(db) -> int:
    """补齐 codearts **福利模型**历史明细里为空的推算积分，返回更新条数。

    口径与 `client._fill_estimated_credit` 完全一致：输入 + 输出 token 按
    1:1 折成池消耗（`units.tokens_to_credits`），并标 `credit_estimated=1`。
    新请求已在落库前补好（见 executor 取末尾 usage 帧），这里只处理历史明细。

    跳过：
    - `credit IS NOT NULL`：上游真值或已推算过，不覆盖；
    - `ok = 0`：请求没成功，正文都没拿到，池消耗无从谈起（上游未给 usage）；
    - 两个 token 都为空：不猜；
    - 不在 `benefit_models` 里的模型：多半是内置模型，按 1:1 折算会造出
      「池消耗」的假账。

    **幂等**：只动 `credit IS NULL` 的行，重复执行第二次返回 0。
    `db` 只用到 `Database` 的 `transaction()` 形态（鸭子类型），便于测试注入
    裸连接适配器。调用方在返回值 > 0 时应重算小时汇总
    （`StatsCollector.rollup_hourly`），本函数只管明细，不碰汇总。
    """
    with db.transaction() as conn:
        known = benefit_models(conn)
        rows = conn.execute(
            "SELECT rowid, model, input_tokens, output_tokens FROM usage_events "
            "WHERE provider = 'codearts' AND credit IS NULL AND ok = 1"
        ).fetchall()
        updated = 0
        for row in rows:
            model = (row["model"] or "").lower()
            if model not in known:
                continue
            if row["input_tokens"] is None and row["output_tokens"] is None:
                continue
            credit = tokens_to_credits(
                float((row["input_tokens"] or 0) + (row["output_tokens"] or 0)))
            conn.execute(
                "UPDATE usage_events SET credit = ?, credit_estimated = 1 WHERE rowid = ?",
                (credit, row["rowid"]))
            updated += 1
    return updated
