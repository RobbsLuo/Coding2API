"""DeepSeek DSML 工具调用泄漏的回读。

DeepSeek V3.2/V4 的推理引擎正常情况下会把 `DSML` 标记解析成结构化
`tool_calls`，但 `tool_choice=auto` + 流式、或参数值含换行时**偶发**解析失败，
原始标记直接混进 assistant 正文：

```text
<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="shell">
<｜｜DSML｜｜parameter name="command" string="true">Get-Content a.csv</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>
```

对客户端来说这是纯文本：没有 tool_call、回合静默结束。本模块把这些块还原成
结构化调用、并从可见正文里剥掉标记。

**方言**（2026-10 多来源实测）：

- 真实标记用**双**全角竖线 `｜`（U+FF5C）包住 `DSML`，也见过单竖线形态；
  两种都接受（匹配单/双竖线，不是「纠正」成某一种）。
- V4 Flash 会把 `invoke` 写成**自闭合**并携带 `string` 属性值：
  `<｜｜DSML｜｜invoke name="cmd" string="echo alpha" />`。参数按**形状**读，
  不按标签名猜：自闭合的没有 body，值只能在属性里。
- 可能缺 `<...tool_calls>` 外层包装，只剩裸 `invoke` 块（V4 长上下文实测）。
  裸块同样回读。

**fail-closed**：回读出来的参数由本模块 `json.dumps` 生成，天然合法；而
`aggregate` 对上游原始 `arguments` 做一次校验——空串归一成 `{}`，其余必须是
合法 JSON **对象**，否则抛 `InvalidToolArguments`（协议级错误：不冷却凭证、
不落账号级惩罚，由 executor 换号重试/按 400 结束）。宁可明确报错，也不把
坏参数递给客户端去执行。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# U+FF5C 全角竖线；真实标记是双写，历史上也出现过单写
_BAR = "\uff5c"
_OPEN = rf"<{_BAR}{{1,2}}DSML{_BAR}{{1,2}}"
_CLOSE = rf"</{_BAR}{{1,2}}DSML{_BAR}{{1,2}}"

_TC_START = re.compile(_OPEN + r"tool_calls>")
_TC_BLOCK = re.compile(_OPEN + r"tool_calls>(.*?)" + _CLOSE + r"tool_calls>", re.S)
_INVOKE = re.compile(
    _OPEN + r'invoke\b([^>]*?)(?:/>|>(.*?)' + _CLOSE + r"invoke>)", re.S)
_PARAM = re.compile(
    _OPEN + r'parameter\b([^>]*?)(?:/>|>(.*?)' + _CLOSE + r"parameter>)", re.S)
_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
# 尾部「可能是标记前缀」的窗口：`<` + 竖线 / DSML 字母的任意前缀
_PARTIAL = re.compile(rf"<{_BAR}{{0,2}}[DSML]{{0,4}}{_BAR}{{0,2}}$")


def looks_like_marker(text: str) -> bool:
    """文本是否含 DSML 标记（或结尾停在标记前缀上）。"""
    if "DSML" in text and _BAR in text:
        return True
    return bool(_PARTIAL.search(text))


def _open_marker_tail(text: str) -> str | None:
    """最后一个**未闭合**开标记之后的文本；没有未闭合标记返回 None。

    标记可能被切成多片（`<｜｜DS` + `ML｜｜invoke ...`），此时整段都要留在
    缓冲里，否则半截标记会当正文发给客户端。
    """
    last = None
    for match in re.finditer(_OPEN, text):
        last = match.start()
    if last is None:
        return None
    tail = text[last:]
    if "/>" in tail or re.search(_CLOSE, tail):
        return None
    return tail


def _attrs(raw: str) -> dict[str, str]:
    return {key: value for key, value in _ATTR.findall(raw)}


def _decode_value(value: str, is_string: bool) -> Any:
    """参数值归一：`string="true"` 原样保留；否则尝试 JSON（数字/对象/布尔）。"""
    if is_string:
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _params(body: str) -> dict[str, Any]:
    """解析一个 invoke 的 body：`parameter` 标签 + 自闭合 `invoke`（V4 方言）。"""
    params: dict[str, Any] = {}
    if not body:
        return params
    for param in _PARAM.finditer(body):
        attrs = _attrs(param.group(1) or "")
        key = attrs.get("name")
        if not key:
            continue
        raw = param.group(2)
        if raw is None:
            # 自闭合参数：值在 string 属性里，语义上是字符串
            params[key] = attrs.get("string", "")
            continue
        params[key] = _decode_value(raw, attrs.get("string") == "true")
    for nested in _INVOKE.finditer(body):
        if nested.group(2) is not None:
            # 不可达（正则结构决定）：`_INVOKE` 的非贪婪 body 在**第一个**
            # `</invoke>` 前截断，有闭合标签的嵌套项会连同闭合一起被外层吃掉，
            # body 里不可能出现完整的嵌套对。保留判断是防御 DSML 方言演进
            # （若将来上游改用不同闭合形态，这里能挡住把嵌套调用误当参数）。
            continue  # pragma: no cover
        attrs = _attrs(nested.group(1) or "")
        key = attrs.get("name")
        if key:
            params[key] = attrs.get("string", "")
    return params


def _parse_invokes(block: str) -> list[dict[str, Any]]:
    """顺序扫描一个块里的顶层 invoke（嵌套自闭合项按参数处理，不重复计数）。"""
    calls: list[dict[str, Any]] = []
    position = 0
    while True:
        match = _INVOKE.search(block, position)
        if match is None:
            break
        position = match.end()
        attrs = _attrs(match.group(1) or "")
        name = attrs.get("name")
        if not name:
            continue
        body = match.group(2)
        if body is None:
            # 顶层自闭合 invoke：参数值在 string 属性里（没有参数名）
            params = {"value": attrs["string"]} if "string" in attrs else {}
        else:
            params = _params(body)
        calls.append({
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(params, ensure_ascii=False, sort_keys=True),
            },
        })
    return calls


def extract(text: str) -> tuple[list[dict[str, Any]], str]:
    """从正文里提取 DSML 工具调用，返回 `(calls, 可见文本)`。

    完整 `tool_calls` 块优先；剩下的裸 `invoke` 块同样回读。无法配对的
    `<...DSML...` 标记原样保留在文本里（宁可显示出来，也不吞掉用户正文）。
    调用按出现顺序编号。
    """
    if not text or "DSML" not in text:
        return [], text
    calls: list[dict[str, Any]] = []
    visible = text
    for match in _TC_BLOCK.finditer(text):
        calls.extend(_parse_invokes(match.group(1)))
    if calls:
        visible = _TC_BLOCK.sub("", text)
    # 裸 invoke 块（缺外层 tool_calls 包装）：必须在剥完整块之后扫，
    # 否则外层块里的 invoke 会被重复计入。
    while True:
        match = _INVOKE.search(visible)
        if match is None:
            break
        found = _parse_invokes(match.group(0))
        if found:
            calls.extend(found)
        visible = visible[:match.start()] + visible[match.end():]
    if not calls:
        return [], text
    for index, call in enumerate(calls):
        call["index"] = index
        call.setdefault("id", f"call_dsml_{index}")
    return calls, visible


class DsmlStreamFilter:
    """流式 content 的增量过滤器。

    普通文本立刻放行（不缓冲、不延迟）；只有出现标记（或结尾停在标记前缀上）
    才开始缓冲，等到完整块解析完再从缓冲里取可见文本。流结束时仍不完整的
    标记原样放行（`flush`），绝不丢用户正文。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self.recovered = False

    def feed(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        """喂一个 content 分片，返回 `(可放行文本, 回读的调用)`。"""
        if not text:
            return "", []
        if not self._buffer and not looks_like_marker(text):
            return text, []
        self._buffer += text
        calls, visible = extract(self._buffer)
        if calls:
            self.recovered = True
            self._buffer = visible
            return visible, calls
        # 还没有完整块：只有「结尾看不出是标记前缀」时才把前面的正文放行，
        # 否则留到下一片（标记可能跨 chunk 断开）
        if _open_marker_tail(self._buffer) is not None:
            return "", []
        if _PARTIAL.search(self._buffer):
            return "", []
        pending, self._buffer = self._buffer, ""
        return pending, []

    def flush(self) -> str:
        """流结束：把缓冲里剩下的内容原样交出（不完整的标记也不吞）。"""
        pending, self._buffer = self._buffer, ""
        return pending


def is_valid_arguments(arguments: Any) -> bool:
    """工具参数是否为合法 JSON 对象（`fail-closed` 的唯一判据）。

    空串 / `None` 视为无参数（无参函数的常见形态）。非对象（数组、裸标量）
    也算非法：OpenAI 契约要求 arguments 是 JSON **对象**。合法值原样保留——
    重新序列化会改变字节（空格/键序），对按字节比对或签名校验的客户端不友好。
    """
    if arguments is None or arguments == "":
        return True
    if not isinstance(arguments, str):
        return False
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return False
    return isinstance(parsed, dict)
