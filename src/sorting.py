"""列表排序：所有 GET 列表端点共用的 sort / order 白名单解析（中性层）。

放在顶层而非 `src/api/`：仓储（`src/db/repo.py`）与统计查询（`src/stats/`）都要
用它，db → api 的依赖方向会把分层搞反。

对外契约（列表端点一致）：

- `sort`：排序键，必须是该端点声明的**白名单键**之一；未知 / 缺省回落到该端点
  的默认键——不报错，避免前端缓存里的旧键把页面打挂。
- `order`：`asc` / `desc`（大小写敏感，与前端约定一致）；缺省 / 非法回落到该
  端点的默认方向。

安全：白名单把「API 键 → SQL 列名」写死，SQL 片段只由受控常量拼出，用户输入
永不直接进 SQL——注入面为零。`sort_rows` 给「组装后再排」的场景（凭证 / 模型
目录等含派生字段的列表），`None` 值统一排最后。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

ASC = "asc"
DESC = "desc"


def parse_sort_order(
    sort: str | None,
    order: str | None,
    allowed: Mapping[str, Any],
    *,
    default_key: str,
    default_desc: bool,
) -> tuple[str, bool]:
    """解析出 (选中的白名单键, 是否降序)。`sort` 未命中 / `order` 非法即回落默认。"""
    key = sort if sort in allowed else default_key
    if order == ASC:
        desc = False
    elif order == DESC:
        desc = True
    else:
        desc = default_desc
    return key, desc


def sql_order(
    sort: str | None,
    order: str | None,
    allowed: Mapping[str, str],
    *,
    default_key: str,
    default_desc: bool,
    tiebreak: str | None = None,
) -> str:
    """生成 `ORDER BY` 片段（不含 `ORDER BY` 关键字，便于拼在 WHERE 之后）。

    `tiebreak` 是稳定次序的第二列（如 `id`）：主排序列并列时用它兜底，翻页 /
    轮询才不会因 SQLite 的次序抖动而跳行。与主列同名时自动省略。
    """
    key, desc = parse_sort_order(sort, order, allowed,
                                 default_key=default_key, default_desc=default_desc)
    direction = "DESC" if desc else "ASC"
    clause = f"{allowed[key]} {direction}"
    if tiebreak and tiebreak != allowed[key]:
        clause += f", {tiebreak} {direction}"
    return clause


def sort_rows(
    rows: list[dict[str, Any]],
    sort: str | None,
    order: str | None,
    allowed: Mapping[str, Callable[[dict[str, Any]], Any]],
    *,
    default_key: str,
    default_desc: bool,
) -> list[dict[str, Any]]:
    """对已组装的 dict 列表排序（供含派生字段的列表复用）。

    `None` 值无论升降序都排在末尾——「缺失」不是「最小」，把未探测 / 未到期的
    行顶到最前会误导。排序稳定，并列值保持出厂顺序。
    """
    key, desc = parse_sort_order(sort, order, allowed,
                                 default_key=default_key, default_desc=default_desc)
    key_fn = allowed[key]
    present = [row for row in rows if key_fn(row) is not None]
    missing = [row for row in rows if key_fn(row) is None]
    present.sort(key=key_fn, reverse=desc)
    return present + missing
