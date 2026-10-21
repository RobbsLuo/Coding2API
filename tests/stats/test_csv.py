"""CSV 导出纯函数：单元格渲染、公式注入防护、行编码。"""

from __future__ import annotations

from src.stats.csv import CSV_BOM, EXPORT_HEADERS, csv_line, event_cells, format_cell


def test_format_cell_renders_scalars():
    assert format_cell(None) == ""
    assert format_cell(12) == "12"
    assert format_cell(-3.5) == "-3.5"          # 负数按数值渲染，不加引号
    assert format_cell(True) == "true"
    assert format_cell(False) == "false"
    assert format_cell("普通文本") == "普通文本"


def test_format_cell_guards_formula_injection():
    """以公式前缀开头的文本加前导单引号；数值型负数不受影响。"""
    assert format_cell("=cmd|'/c calc'!A0") == "'=cmd|'/c calc'!A0"
    assert format_cell("+1+2") == "'+1+2"
    assert format_cell("-2+3") == "'-2+3"       # 文本形态的负号也拦
    assert format_cell("@SUM(A1)") == "'@SUM(A1)"
    assert format_cell("\tlead") == "'\tlead"
    assert format_cell("\rcr") == "'\rcr"
    # 前缀不在集合内：原样
    assert format_cell("safe=1") == "safe=1"


def test_event_cells_aligns_with_headers():
    row = {
        "ts": 1_700_000_000, "username": "alice", "provider": "trae",
        "credential_id": "cred_1234567890", "credential_name": "主号",
        "model": "glm-5.2", "ok": 1, "error_type": None,
        "input_tokens": 100, "output_tokens": 50, "cached_tokens": None,
        "reasoning_tokens": 7, "credit": 1.5, "credit_estimated": 0,
        "cost_usd": 0.001, "cost_cny": 0.0067, "ttfb_ms": 120, "latency_ms": 800,
        "request_id": "req-1",
    }
    cells = event_cells(row)
    assert len(cells) == len(EXPORT_HEADERS)
    assert cells[0].startswith("2023-11-14T")     # ISO 8601 UTC
    assert cells[1:5] == ["alice", "trae", "主号", "glm-5.2"]
    assert cells[5] == "成功"
    assert cells[7:11] == ["100", "50", "", "7"]  # 缺失缓存留空
    assert cells[11] == "1.5" and cells[12] == ""  # 非推算
    assert cells[19] == "req-1"


def test_event_cells_include_per_request_speed():
    """速度两列与页面明细表同口径：有值按数值渲染，不可测留空（不是 0）。"""
    cells = event_cells({"ok": 1, "tps_visible": 51.2, "tps": 82.8})
    assert cells[17:19] == ["51.2", "82.8"]
    # 整段输出都是思考 → 正文列空、总产出有值
    cells = event_cells({"ok": 1, "tps_visible": None, "tps": 200.0})
    assert cells[17:19] == ["", "200.0"]
    # 失败 / 无生成窗口 → 两列都空
    cells = event_cells({"ok": 0})
    assert cells[17:19] == ["", ""]


def test_event_cells_falls_back_to_credential_id_and_marks_failure():
    cells = event_cells({"ok": 0, "credential_id": "cred_x", "credit_estimated": 1})
    assert cells[3] == "cred_x"                   # 无昵称 → 回落 ID
    assert cells[5] == "失败"
    assert cells[12] == "是"                      # credit 为推算


def test_event_cells_credential_blank_when_absent():
    cells = event_cells({"ok": 1})
    assert cells[3] == "" and cells[0] == ""      # 无凭证、无时间戳


def test_csv_line_quotes_and_crlf():
    line = csv_line(["a,b", 'has "quote"', "plain"])
    assert line == '"a,b","has ""quote""",plain\r\n'
    assert csv_line([]) == "\r\n"


def test_csv_bom_is_utf8_bom():
    assert CSV_BOM == "\ufeff"
