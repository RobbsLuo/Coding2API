"""上游状态页聚合：四种协议解析、抓取降级、内存快照。

载荷是**结构忠实的最小样本**（字段名与嵌套层级照抄真实响应，值截短），
这样上游改字段时这些用例才会失败——如果只造 `{"status": "ok"}` 这样的
自编形状，解析器跟着改坏也测不出来。
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.upstream_status import (
    LEVEL_DEGRADED,
    LEVEL_MAINTENANCE,
    LEVEL_MAJOR,
    LEVEL_OPERATIONAL,
    LEVEL_PARTIAL,
    LEVEL_UNKNOWN,
    MAX_INCIDENTS,
    MAX_SOURCE_BODY,
    SOURCE_SPECS,
    Component,
    Incident,
    SourceSpec,
    SourceStatus,
    UpstreamStatusStore,
    aggregate_level,
    fetch_all,
    fetch_source,
    parse_atom,
    parse_betterstack,
    parse_opencode,
    parse_statuspage,
    spec_by_key,
)

NOW = 1_800_000_000

# --------------------------------------------------------------- 载荷样本

STATUSPAGE = {
    "page": {"id": "abc", "name": "Kilo", "url": "https://status.kilo.ai"},
    "status": {"indicator": "maintenance", "description": "Service Under Maintenance"},
    "components": [
        {"id": "1", "name": "Kilo AI Gateway", "status": "operational",
         "group": False, "showcase": True},
        # group=true 只是分组标题，没有自身状态
        {"id": "2", "name": "Kimi", "status": "operational", "group": True},
        {"id": "3", "name": "Fly.io Machines API", "status": "under_maintenance",
         "group": False},
        {"id": "4", "name": "Edge", "status": "degraded_performance", "group": False},
        # 名字缺失 / 空白的组件直接跳过
        {"id": "5", "status": "operational", "group": False},
        {"id": "6", "name": "   ", "status": "operational", "group": False},
        "不是 dict",
    ],
    "incidents": [
        {"name": "Elevated errors", "status": "monitoring", "impact": "major",
         "created_at": "2026-10-14T17:00:44Z", "updated_at": "2026-10-14T20:00:36Z",
         "shortlink": "https://stspg.io/abc",
         "incident_updates": [
             {"status": "monitoring", "body": "<p>We are <b>monitoring</b>.</p>"},
             {"status": "identified", "body": "older"},
         ]},
        {"name": "Cost data delayed", "status": "resolved", "impact": "minor",
         "created_at": "2026-10-13T00:00:00Z"},
        {"name": "Postmortem done", "status": "postmortem", "impact": "none"},
        {"name": "   ", "status": "resolved", "impact": "minor"},
        {"status": "resolved", "impact": "minor"},
        {"name": "未来字段全缺", "status": "investigating"},
        "不是 dict",
    ],
    "scheduled_maintenances": [],
}

OPENCODE = {
    "updatedAt": 1_792_034_894_728,
    "since": 1_792_028_534_694,
    "stale": False,
    "status": "degraded",
    "components": [
        {"service": "console", "status": "operational", "reason": "none",
         "since": 1, "models": [], "breakdown": [], "speed": {}},
        {"service": "go", "status": "degraded", "reason": "upstream", "since": 1,
         "models": [{"id": "deepseek-v4-pro", "reason": "latency"}],
         "breakdown": [
             {"id": "deepseek-v4-pro", "status": "degraded", "reason": "latency",
              "speed": {"ttft": 4041, "tps": 137}},
             {"id": "deepseek-v4.1-flash", "status": "operational",
              "reason": "none", "speed": {"ttft": 2286, "tps": 195}},
             {"id": "no-speed", "status": "operational", "reason": "none"},
             {"status": "operational"},
             "不是 dict",
         ],
         "speed": {"ttft": 2485, "tps": 188}},
        {"status": "operational", "reason": "none"},
        "不是 dict",
    ],
    "incidents": [
        {"id": "go-1", "event": "go-1", "service": "go",
         "startedAt": 1_792_028_534_694, "resolvedAt": None,
         "status": "degraded", "reason": "upstream",
         "models": [{"id": "deepseek-v4-pro", "reason": "latency"},
                    {"id": "mimo-v2.6-flash", "reason": "upstream"},
                    {"reason": "upstream"}, "不是 dict"]},
        {"id": "free-1", "event": "free-1", "service": "free",
         "startedAt": 1_792_028_534_694, "resolvedAt": 1_792_028_894_697,
         "status": "degraded", "reason": "latency",
         "models": [{"id": "mimo-free", "reason": "latency"}]},
        {"id": "x", "service": "payg", "startedAt": 1, "resolvedAt": None,
         "status": "operational", "reason": "none", "models": []},
        {"startedAt": 1, "resolvedAt": None, "status": "degraded", "reason": "error"},
        "不是 dict",
    ],
    "notes": [],
    "history": [],
}

BETTERSTACK = {
    "data": {
        "id": "197766",
        "type": "status_page",
        "attributes": {"company_name": "Together AI", "aggregate_state": "downtime"},
    },
    "included": [
        {"type": "status_page_section", "id": 231960,
         "attributes": {"name": "Inference - Chat", "position": 1}},
        {"type": "status_page_section", "id": 231964, "attributes": "坏形状"},
        {"type": "status_page_resource", "id": "r1",
         "attributes": {"status_page_section_id": 231960,
                        "public_name": "DeepSeek V4.1 Flash",
                        "status": "operational", "availability": 0.998453}},
        {"type": "status_page_resource", "id": "r2",
         "attributes": {"status_page_section_id": 231960,
                        "public_name": "DeepSeek V4 Flash 0731",
                        "status": "downtime", "availability": 0.998978}},
        # 无可用率、无分区
        {"type": "status_page_resource", "id": "r3",
         "attributes": {"public_name": "Website", "status": "operational"}},
        # 名字缺失
        {"type": "status_page_resource", "id": "r4",
         "attributes": {"status": "operational"}},
        # attributes 不是 dict
        {"type": "status_page_resource", "id": "r5", "attributes": None},
        "不是 dict",
    ],
}

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>DeepSeek</title>
  <updated>2026-10-15T09:08:41+08:00</updated>
  <entry>
    <title>DeepSeek 网页/API 部分中断</title>
    <updated>2026-10-15T09:08:41+08:00</updated>
    <id>urn:flashduty:change:1</id>
    <link href="https://status.deepseek.com/incidents/1" rel="alternate"></link>
    <summary type="html">&lt;p&gt;&lt;strong&gt;Status:&lt;/strong&gt; identified&lt;/p&gt;
      &lt;p&gt;&lt;strong&gt;Affected components:&lt;/strong&gt; 对话服务&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>DeepSeek 网页/API 性能下降</title>
    <updated>2026-10-12T14:57:54+08:00</updated>
    <link href="https://status.deepseek.com/incidents/2" rel="alternate"></link>
    <summary type="html">&lt;p&gt;&lt;strong&gt;Status:&lt;/strong&gt; resolved&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>服务不可用</title>
    <summary>没有 Status 字段</summary>
  </entry>
  <entry>
    <summary>没有标题</summary>
  </entry>
</feed>
"""


def _spec(key: str) -> SourceSpec:
    found = spec_by_key(key)
    assert found is not None
    return found


# ------------------------------------------------------------------ 档位聚合

def test_aggregate_level_takes_worst():
    assert aggregate_level([]) == LEVEL_UNKNOWN
    assert aggregate_level([LEVEL_OPERATIONAL, LEVEL_DEGRADED]) == LEVEL_DEGRADED
    assert aggregate_level([LEVEL_DEGRADED, LEVEL_MAJOR, LEVEL_PARTIAL]) == LEVEL_MAJOR
    # unknown 不压过真实故障：一个源连不上，不该盖住另一个源的中断
    assert aggregate_level([LEVEL_UNKNOWN, LEVEL_PARTIAL]) == LEVEL_PARTIAL
    assert aggregate_level([LEVEL_UNKNOWN, LEVEL_OPERATIONAL]) == LEVEL_OPERATIONAL
    assert aggregate_level([LEVEL_UNKNOWN]) == LEVEL_UNKNOWN
    # 完全不认识的档位（上游新增枚举）按最轻处理
    assert aggregate_level(["who-knows", LEVEL_MAINTENANCE]) == LEVEL_MAINTENANCE


def test_source_specs_are_unique_and_well_formed():
    keys = [spec.key for spec in SOURCE_SPECS]
    assert len(set(keys)) == len(keys)
    for spec in SOURCE_SPECS:
        assert spec.label and spec.api_url.startswith("https://")
        assert spec.page_url.startswith("https://")
        assert spec.scope in ("channel", "model")
        if spec.scope == "channel":
            assert spec.related_providers, spec.key


def test_spec_by_key():
    assert _spec("opencode").label == "OpenCode Zen"
    assert spec_by_key("never") is None


# ------------------------------------------------------------ 内部取值助手

def test_as_int_covers_every_input_shape():
    from src.upstream_status import _as_int

    assert _as_int(True) is None              # bool 不是数字
    assert _as_int(7) == 7
    assert _as_int(7.9) == 7
    assert _as_int(" 42 ") == 42
    assert _as_int("abc") is None             # 非数字字符串
    assert _as_int("") is None
    assert _as_int(None) is None
    assert _as_int({"a": 1}) is None


def test_as_ms_epoch_normalises_units():
    from src.upstream_status import _as_ms_epoch

    # 毫秒（13 位）→ 秒
    assert _as_ms_epoch(1_792_034_894_728) == 1_792_034_894
    # 已经是秒（10 位）就原样返回
    assert _as_ms_epoch(1_792_034_894) == 1_792_034_894
    # 0 / 负数 / 取不到 → None
    assert _as_ms_epoch(0) is None
    assert _as_ms_epoch(-1) is None
    assert _as_ms_epoch("abc") is None


def test_as_iso_epoch_rejects_unparsable():
    from src.upstream_status import _as_iso_epoch

    assert _as_iso_epoch("2026-10-15T09:08:41+08:00") == 1_792_026_521
    assert _as_iso_epoch("not a date") is None
    assert _as_iso_epoch("   ") is None
    assert _as_iso_epoch(None) is None


def test_text_truncates_and_rejects_non_string():
    from src.upstream_status import _text

    assert _text(None) is None
    assert _text("   ") is None
    assert _text("<p>a</p>") == "a"
    long = _text("x" * 500)
    assert long is not None and long.endswith("…") and len(long) == 301


def test_speed_detail_handles_missing_pieces():
    from src.upstream_status import _speed_detail

    # 无前缀 + 只有 ttft
    assert _speed_detail({"ttft": 1200}) == "TTFT 1.2s"
    # 无前缀 + 只有 tps
    assert _speed_detail({"tps": 90}) == "90 tok/s"
    # 0 / 负数视为缺项
    assert _speed_detail({"ttft": 0, "tps": 0}) is None
    # speed 不是 dict 时只剩前缀
    assert _speed_detail(None, "go") == "go"
    assert _speed_detail(None) is None


def test_latest_update_body_without_updates():
    from src.upstream_status import _latest_update_body

    assert _latest_update_body(None) is None
    assert _latest_update_body([]) is None
    assert _latest_update_body([{"body": "<p>hi</p>"}]) == "hi"


def test_atom_link_skips_entries_without_href():
    from src.upstream_status import parse_atom

    feed = ('<feed><entry><title>性能下降</title>'
            '<link rel="alternate"></link>'
            '<link href="https://x/1"></link></entry></feed>')
    status = parse_atom(_spec("deepseek"), feed, now=NOW)
    assert status.incidents[0].url == "https://x/1"


# ------------------------------------------------------------ Statuspage v2

def test_parse_statuspage_full():
    status = parse_statuspage(_spec("kilo"), STATUSPAGE, now=NOW)
    assert status.level == LEVEL_MAINTENANCE       # indicator=maintenance
    assert status.fetched_at == NOW and status.error is None
    names = [item.name for item in status.components]
    # group 标题与无名组件被跳过
    assert names == ["Kilo AI Gateway", "Fly.io Machines API", "Edge"]
    assert [item.level for item in status.components] == [
        LEVEL_OPERATIONAL, LEVEL_MAINTENANCE, LEVEL_DEGRADED]

    first, resolved, postmortem, bare = status.incidents
    assert first.title == "Elevated errors"
    assert first.level == LEVEL_MAJOR and first.ongoing is True
    assert first.url == "https://stspg.io/abc"
    # 正文里的 HTML 被剥成一行纯文本
    assert first.detail == "We are monitoring."
    assert first.updated_at is not None
    assert resolved.ongoing is False and resolved.level == LEVEL_DEGRADED
    # postmortem 也算已结束
    assert postmortem.ongoing is False and postmortem.level == LEVEL_DEGRADED
    # 未来字段全缺时不崩，时间回落 None；impact 缺失 → unknown
    assert bare.title == "未来字段全缺" and bare.ongoing is True
    assert bare.updated_at is None and bare.level == LEVEL_UNKNOWN
    # 全空白标题 / 无标题 / 非 dict 的事故被跳过
    assert len(status.incidents) == 4


def test_parse_statuspage_caps_incidents():
    """事故条数封顶：OpenAI 这类源条目很多，不封顶会把卡片撑到几百行。"""
    payload = {
        "status": {"indicator": "none"},
        "components": [],
        "incidents": [{"name": f"事件 {i}", "status": "resolved", "impact": "minor"}
                      for i in range(MAX_INCIDENTS + 3)],
    }
    status = parse_statuspage(_spec("openai"), payload, now=NOW)
    assert len(status.incidents) == MAX_INCIDENTS


def test_parse_statuspage_unknown_indicator_falls_back():
    status = parse_statuspage(_spec("kilo"),
                              {"status": {"indicator": "brand-new"},
                               "components": [], "incidents": []}, now=NOW)
    assert status.level == LEVEL_UNKNOWN
    assert status.components == () and status.incidents == ()


def test_parse_statuspage_rejects_non_object():
    with pytest.raises(ValueError):
        parse_statuspage(_spec("kilo"), [], now=NOW)


def test_parse_statuspage_tolerates_wrong_shapes():
    """status 不是 dict / components 不是 list / 事故缺字段，都不该崩。"""
    status = parse_statuspage(_spec("kilo"),
                              {"status": "operational", "components": {"a": 1},
                               "incidents": [{"name": "无 impact"}]}, now=NOW)
    assert status.level == LEVEL_UNKNOWN
    assert status.components == ()
    assert status.incidents[0].level == LEVEL_UNKNOWN


# ------------------------------------------------------------ OpenCode Zen

def test_parse_opencode_expands_breakdown():
    status = parse_opencode(_spec("opencode"), OPENCODE, now=NOW)
    assert status.level == LEVEL_DEGRADED and status.source_stale is False
    by_name = {item.name: item for item in status.components}
    # 服务级一行（无 breakdown）
    assert by_name["console"].level == LEVEL_OPERATIONAL
    # 有 breakdown 的服务展开成逐模型行，服务级行不再出现
    assert "go" not in by_name
    assert by_name["deepseek-v4-pro"].level == LEVEL_DEGRADED
    assert by_name["deepseek-v4-pro"].detail == "go · TTFT 4.0s · 137 tok/s"
    assert by_name["no-speed"].detail == "go"
    # service 缺失回落 "?"
    assert by_name["?"].level == LEVEL_OPERATIONAL

    ongoing = [item for item in status.incidents if item.ongoing]
    assert [item.title for item in ongoing] == [
        "go · deepseek-v4-pro, mimo-v2.6-flash（上游）",
        "payg",
        "?（报错）",
    ]
    assert ongoing[1].level == LEVEL_OPERATIONAL
    done = [item for item in status.incidents if not item.ongoing]
    assert done[0].title == "free · mimo-free（延迟）"
    assert done[0].updated_at is not None


def test_parse_opencode_caps_incidents():
    payload = {
        "status": "operational",
        "components": [],
        "incidents": [{"service": f"s{i}", "status": "operational", "reason": "none",
                       "startedAt": 1, "resolvedAt": None, "models": []}
                      for i in range(MAX_INCIDENTS + 3)],
    }
    status = parse_opencode(_spec("opencode"), payload, now=NOW)
    assert len(status.incidents) == MAX_INCIDENTS


def test_parse_opencode_rejects_non_object():
    with pytest.raises(ValueError):
        parse_opencode(_spec("opencode"), "nope", now=NOW)


def test_parse_opencode_marks_stale():
    status = parse_opencode(_spec("opencode"),
                            {"status": "operational", "stale": True,
                             "components": [], "incidents": []}, now=NOW)
    assert status.source_stale is True and status.level == LEVEL_OPERATIONAL


# --------------------------------------------------------------- Better Stack

def test_parse_betterstack_full():
    status = parse_betterstack(_spec("together"), BETTERSTACK, now=NOW)
    assert status.level == LEVEL_MAJOR        # aggregate_state=downtime
    assert status.incidents == ()             # 事故只在 RSS 里，不在 index.json
    names = [item.name for item in status.components]
    assert names == ["DeepSeek V4.1 Flash", "DeepSeek V4 Flash 0731", "Website"]
    assert status.components[0].detail == "可用率 99.85% · Inference - Chat"
    assert status.components[1].level == LEVEL_MAJOR
    # 无分区时 detail 只剩可用率；无可用率时 detail 为 None
    assert status.components[2].detail is None


def test_parse_betterstack_rejects_non_object():
    with pytest.raises(ValueError):
        parse_betterstack(_spec("together"), 3, now=NOW)


def test_parse_betterstack_tolerates_missing_data():
    status = parse_betterstack(_spec("together"), {"included": []}, now=NOW)
    assert status.level == LEVEL_UNKNOWN and status.components == ()


def test_parse_betterstack_section_only_detail():
    """可用率缺失但有分区名时，detail 只留分区名（不产生空段）。"""
    payload = {
        "data": {"attributes": {"aggregate_state": "operational"}},
        "included": [
            {"type": "status_page_section", "id": 7, "attributes": {"name": "Voice"}},
            {"type": "status_page_resource",
             "attributes": {"status_page_section_id": 7, "public_name": "Whisper",
                            "status": "operational"}},
        ],
    }
    status = parse_betterstack(_spec("together"), payload, now=NOW)
    assert status.components[0].detail == "Voice"


# -------------------------------------------------------------- DeepSeek Atom

def test_parse_atom_full():
    status = parse_atom(_spec("deepseek"), ATOM, now=NOW)
    # 源级别 = 最严重的未恢复事故（「服务不可用」→ major 压过「部分中断」）
    assert status.level == LEVEL_MAJOR
    assert status.components == ()            # 该源不提供当前组件态
    first, resolved, no_status = status.incidents
    assert first.title == "DeepSeek 网页/API 部分中断"
    assert first.ongoing is True and first.level == LEVEL_PARTIAL
    assert first.url == "https://status.deepseek.com/incidents/1"
    assert "Affected components" in (first.detail or "")
    assert first.updated_at is not None
    # 正文里 Status: resolved → 已恢复
    assert resolved.ongoing is False
    # 没有 Status 字段 → 视为进行中，标题命中「不可用」→ major
    assert no_status.ongoing is True and no_status.level == LEVEL_MAJOR
    # 无标题的条目被跳过
    assert len(status.incidents) == 3

def test_parse_atom_all_resolved_is_operational():
    feed = ("<feed><entry><title>性能下降</title>"
            "<summary>Status: resolved</summary></entry></feed>")
    status = parse_atom(_spec("deepseek"), feed, now=NOW)
    assert status.level == LEVEL_OPERATIONAL
    assert status.incidents[0].level == LEVEL_DEGRADED


def test_parse_atom_no_incidents_is_operational():
    status = parse_atom(_spec("deepseek"), "<feed></feed>", now=NOW)
    assert status.level == LEVEL_OPERATIONAL and status.incidents == ()


def test_parse_atom_unknown_title_keyword_defaults_degraded():
    feed = "<feed><entry><title>Something odd</title></entry></feed>"
    status = parse_atom(_spec("deepseek"), feed, now=NOW)
    assert status.incidents[0].level == LEVEL_DEGRADED


def test_parse_atom_rejects_bad_input():
    with pytest.raises(ValueError):
        parse_atom(_spec("deepseek"), None, now=NOW)
    with pytest.raises(ValueError):
        parse_atom(_spec("deepseek"), "<feed><entry>", now=NOW)


def test_parse_atom_rejects_xxe_and_entity_expansion():
    """外部实体 / 十亿笑声：走 defusedxml，绝不能让外部站点读本地文件或炸内存。

    stdlib `ElementTree.fromstring` 对这两类载荷不设防（bandit B314），故解析
    入口用 defusedxml——命中即抛 `ValueError`，由 `fetch_source` 兜成 unknown。
    """
    xxe = ('<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY xxe SYSTEM '
           '"file:///etc/passwd">]><feed><entry><title>&xxe;</title></entry></feed>')
    with pytest.raises(ValueError):
        parse_atom(_spec("deepseek"), xxe, now=NOW)
    billion_laughs = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
                      '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>'
                      '<feed><entry><title>&lol2;</title></entry></feed>')
    with pytest.raises(ValueError):
        parse_atom(_spec("deepseek"), billion_laughs, now=NOW)


def test_parse_atom_tolerates_unrelated_entries_and_missing_link():
    """无 link 的条目 url 为 None；summary 为空文本时 detail 为 None。"""
    feed = ("<feed><entry><title>性能下降</title><summary>  </summary></entry>"
            "</feed>")
    status = parse_atom(_spec("deepseek"), feed, now=NOW)
    assert status.incidents[0].url is None
    assert status.incidents[0].detail is None


# ------------------------------------------------------------------ fetch

def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_source_parses_json():
    async with _client(lambda request: httpx.Response(200, json=STATUSPAGE)) as client:
        status = await fetch_source(_spec("kilo"), client=client)
    assert status.level == LEVEL_MAINTENANCE and status.error is None


async def test_fetch_source_parses_atom_as_text():
    async with _client(lambda request: httpx.Response(
            200, text=ATOM, headers={"content-type": "application/xml"})) as client:
        status = await fetch_source(_spec("deepseek"), client=client)
    assert status.level == LEVEL_MAJOR


async def test_fetch_source_decodes_atom_with_declared_charset():
    """content-type 声明了非 UTF-8 charset 时按它解码（与 `response.text` 同口径）。

    流式读取后不能读 `response.encoding`（body 未缓存会抛 `ResponseNotRead`），
    故走 `charset_encoding`。
    """
    feed = "<feed><entry><title>性能下降</title></entry></feed>".encode("gbk")

    async with _client(lambda request: httpx.Response(
            200, content=feed,
            headers={"content-type": "application/xml; charset=gbk"})) as client:
        status = await fetch_source(_spec("deepseek"), client=client)
    assert status.level == LEVEL_DEGRADED
    assert status.incidents[0].title == "性能下降"


async def test_fetch_source_sends_browser_headers():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"status": {"indicator": "none"}})

    async with _client(handler) as client:
        await fetch_source(_spec("kilo"), client=client)
    assert "Mozilla/5.0" in seen["user-agent"]


async def test_fetch_source_degrades_on_http_error():
    async with _client(lambda request: httpx.Response(503, text="busy")) as client:
        status = await fetch_source(_spec("kilo"), client=client)
    assert status.level == LEVEL_UNKNOWN and status.abnormal is True
    assert status.error is not None and "503" in status.error


async def test_fetch_source_degrades_on_bad_json():
    async with _client(lambda request: httpx.Response(200, text="not json")) as client:
        status = await fetch_source(_spec("kilo"), client=client)
    assert status.level == LEVEL_UNKNOWN
    assert status.error is not None and status.error.startswith("JSONDecodeError")


async def test_fetch_source_degrades_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    async with _client(handler) as client:
        status = await fetch_source(_spec("kilo"), client=client)
    assert status.level == LEVEL_UNKNOWN
    assert status.error == "ConnectError: no route to host"


async def test_fetch_source_error_without_message():
    """异常的 str() 为空时回落异常类名，不能给出空错误文本。"""
    class Quiet(Exception):
        def __str__(self) -> str:
            return "   "

    def handler(request: httpx.Request) -> httpx.Response:
        raise Quiet()

    async with _client(handler) as client:
        status = await fetch_source(_spec("kilo"), client=client)
    assert status.error == "Quiet"


async def test_fetch_source_rejects_oversized_json_body():
    """成功路径有界：超大 JSON 不解析，按 unknown + error 降级。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_SOURCE_BODY + 1))

    async with _client(handler) as client:
        status = await fetch_source(_spec("kilo"), client=client)
    assert status.level == LEVEL_UNKNOWN
    assert status.error is not None and "OversizedBody" in status.error


async def test_fetch_source_rejects_oversized_atom_body():
    """atom 文本路径同样有界（原先 `response.text` 无上限）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_SOURCE_BODY + 1),
                              headers={"content-type": "application/xml"})

    async with _client(handler) as client:
        status = await fetch_source(_spec("deepseek"), client=client)
    assert status.level == LEVEL_UNKNOWN
    assert status.error is not None and "OversizedBody" in status.error


async def test_fetch_source_accepts_body_at_limit():
    """恰好等于上限是完整响应，不能被误判超限（边界不能差一）。"""
    body = json.dumps(STATUSPAGE).encode()
    assert len(body) < MAX_SOURCE_BODY

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _client(handler) as client:
        status = await fetch_source(_spec("kilo"), client=client)
    assert status.level == LEVEL_MAINTENANCE


async def test_fetch_all_keeps_order_and_isolates_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "status.kilo.ai":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=STATUSPAGE)

    async with _client(handler) as client:
        statuses = await fetch_all(
            (_spec("kilo"), _spec("moonshot"), _spec("minimax")), client=client)
    assert [item.spec.key for item in statuses] == ["kilo", "moonshot", "minimax"]
    assert [item.level for item in statuses] == [
        LEVEL_UNKNOWN, LEVEL_MAINTENANCE, LEVEL_MAINTENANCE]
    assert statuses[0].error is not None
    assert statuses[1].error is None


async def test_fetch_all_owns_client_when_not_injected(monkeypatch):
    """未注入 client 时自建一个，且 `trust_env=False`（不吃宿主/系统代理）。

    回归：本机开全局代理时 httpx 默认 trust_env=True 会把状态页请求交给代理，
    代理握手失败 → 八个源全部 ConnectError（实测「上游状态全部抓取失败」）。
    """
    calls: list[object] = []
    seen_kwargs: list[dict] = []
    real_client = httpx.AsyncClient

    class _RecordingClient:
        def __init__(self, **kwargs):
            seen_kwargs.append(kwargs)
            self._inner = real_client(**kwargs)

        async def __aenter__(self):
            return await self._inner.__aenter__()

        async def __aexit__(self, *exc):
            return await self._inner.__aexit__(*exc)

    async def fake_fetch(spec, *, client, timeout):
        calls.append(client)
        return SourceStatus(spec=spec, level=LEVEL_OPERATIONAL, fetched_at=NOW)

    monkeypatch.setattr("src.upstream_status.fetch_source", fake_fetch)
    monkeypatch.setattr("src.upstream_status.httpx.AsyncClient", _RecordingClient)
    statuses = await fetch_all((_spec("kilo"), _spec("moonshot")))
    assert len(statuses) == 2
    assert all(isinstance(client, real_client) for client in calls)
    assert seen_kwargs == [{"trust_env": False}]


# ------------------------------------------------------------------ 存储

def _status(key: str, level: str, *, error: str | None = None) -> SourceStatus:
    return SourceStatus(spec=_spec(key), level=level, fetched_at=NOW, error=error,
                        components=(Component(name="a", level=level, detail="d"),),
                        incidents=(Incident(title="t", level=level, ongoing=True),))


def test_store_starts_empty():
    store = UpstreamStatusStore()
    assert store.snapshot() == ()
    assert store.fetched_at is None
    payload = store.payload()
    assert payload["summary"] == {"level": LEVEL_UNKNOWN, "operational": 0,
                                  "abnormal": 0, "unknown": 0, "total": 0}
    assert payload["sources"] == [] and payload["fetched_at"] is None


def test_store_replaces_snapshot_atomically():
    store = UpstreamStatusStore()
    store.update([_status("kilo", LEVEL_MAJOR)], fetched_at=100.0)
    assert store.fetched_at == 100.0
    store.update([_status("moonshot", LEVEL_OPERATIONAL)])
    assert [item.spec.key for item in store.snapshot()] == ["moonshot"]
    assert store.fetched_at is not None and store.fetched_at != 100.0


def test_store_payload_summary_counts():
    store = UpstreamStatusStore()
    store.update([
        _status("kilo", LEVEL_OPERATIONAL),
        _status("moonshot", LEVEL_DEGRADED),
        _status("openai", LEVEL_MAJOR),
        _status("minimax", LEVEL_UNKNOWN, error="boom"),
        _status("anthropic", LEVEL_MAINTENANCE),
    ])
    payload = store.payload()
    assert payload["summary"] == {"level": LEVEL_MAJOR, "operational": 1,
                                  "abnormal": 3, "unknown": 1, "total": 5}
    first = payload["sources"][0]
    assert first["key"] == "kilo" and first["scope"] == "channel"
    assert first["related_providers"] == ["kilo"]
    assert first["components"] == [{"name": "a", "level": LEVEL_OPERATIONAL,
                                    "detail": "d"}]
    assert first["incidents"][0]["ongoing"] is True
    assert first["source_stale"] is False


def test_source_status_abnormal_flag():
    assert _status("kilo", LEVEL_OPERATIONAL).abnormal is False
    assert _status("kilo", LEVEL_UNKNOWN, error="x").abnormal is True


def test_as_dict_round_trips_through_json():
    payload = _status("kilo", LEVEL_DEGRADED).as_dict()
    assert json.loads(json.dumps(payload))["level"] == LEVEL_DEGRADED
