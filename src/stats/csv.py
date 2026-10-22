"""逐请求明细的 CSV 导出：列定义 + 公式注入防护 + 单行编码。

拆成独立纯函数模块（不碰 DB、不碰 HTTP）：CSV 的坑几乎全在「转义」与「Excel
把单元格当公式执行」两件事上，集中在这里才好逐条测。

**公式注入防护**：Excel / WPS 会把以 `=` `+` `-` `@`（以及制表符、回车）开头的
单元格当公式解析，`=cmd|'/c calc'!A0` 这类载荷能直接起进程。防护手段是给这类
**文本**单元格加一个前导单引号（Excel 视为「强制文本」）。只对文本生效：数值
（含负数）由 Python 渲染，`-1.5` 在 Excel 里就是数字，加引号反而毁掉可计算性。

**换行符**：用 `\r\n`（CSV RFC 4180），Excel 对 `\n` 的单文件兼容性更差。
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

__all__ = ["CSV_BOM", "EXPORT_HEADERS", "csv_line", "event_cells", "format_cell"]

# Excel 在 Windows 上默认按本地代码页读 `.csv`；带 BOM 才会认 UTF-8。
# 中文表头 + 中文文案（成功/失败）没有 BOM 会变乱码。
CSV_BOM = "\ufeff"

# (表头, 取值键)：顺序即导出列顺序，与前端明细表同口径。
EXPORT_HEADERS: tuple[str, ...] = (
    "时间", "用户", "渠道", "凭证", "模型", "状态", "错误类型",
    "输入 token", "输出 token", "命中缓存", "推理 token",
    "credit", "credit 为推算", "成本 USD", "成本 CNY",
    "首字延迟 ms", "耗时 ms",
    # 逐请求速度与页面明细表同口径（规则见 query._row_speed_sql）：只算生成
    # 窗口可测的成功请求，不可测的行留空（不是 0）。
    "正文速度 tok/s", "总产出速度 tok/s",
    "请求 ID",
)

# 文本单元格里会被表格软件当公式起点的前缀字符。
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def format_cell(value: Any) -> str:
    """单个单元格 → CSV 文本（含公式注入防护）。

    数值/布尔按 Python 原生渲染；文本若以公式前缀开头则加前导单引号。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _iso_utc(ts: Any) -> str:
    """epoch 秒 → ISO 8601（UTC）。

    导出不带时区后缀的时间会被下游按本地时区误读；统一标 `+00:00`，谁看谁换算。
    """
    if ts is None:
        return ""
    return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()


def event_cells(row: dict[str, Any]) -> list[str]:
    """一条明细 → 与 `EXPORT_HEADERS` 对齐的单元格列表。"""
    credential = row.get("credential_name") or row.get("credential_id") or ""
    return [
        _iso_utc(row.get("ts")),
        format_cell(row.get("username")),
        format_cell(row.get("provider")),
        format_cell(credential),
        format_cell(row.get("model")),
        "成功" if row.get("ok") else "失败",
        format_cell(row.get("error_type")),
        format_cell(row.get("input_tokens")),
        format_cell(row.get("output_tokens")),
        format_cell(row.get("cached_tokens")),
        format_cell(row.get("reasoning_tokens")),
        format_cell(row.get("credit")),
        "是" if row.get("credit_estimated") else "",
        format_cell(row.get("cost_usd")),
        format_cell(row.get("cost_cny")),
        format_cell(row.get("ttfb_ms")),
        format_cell(row.get("latency_ms")),
        format_cell(row.get("tps_visible")),
        format_cell(row.get("tps")),
        format_cell(row.get("request_id")),
    ]


def csv_line(cells: list[str]) -> str:
    """一行单元格 → CSV 行文本（含必要的引号转义与 CRLF 结尾）。"""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow(cells)
    return buffer.getvalue()
