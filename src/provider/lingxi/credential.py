"""WPS 灵犀凭证：只有一份 `wps_sid` Cookie，**没有刷新接口**。

上游登录走 WPS 账号 SSO（网页版 / 桌面客户端同一套 cookie），拿不到
设备码 / 扫码换票的公开协议，程序侧换不出新票。因此：

* 凭证只保留 `wps_sid` 与可选的伴生 `uid` / `copilot_server_branch`；
* 不写 `expires`：插件实测 cookie 约一年有效（无官方承诺），真正的判据是
  上游 `401`。按时长写死过期会让还能用的凭证被提前摘掉、还会让预刷新任务
  每小时白跑一次；
* 过期只能重新粘贴 —— `refresh()` 的语义因此降级为「重新校验 + 刷余额」
  （见 `provider.refresh`）。

输入形态宽松（用户会从 DevTools 整段复制）：裸 sid、`wps_sid=…`、
`Cookie: wps_sid=…; uid=…`、带引号的粘贴、以及 `{"cookie": "…"}` 都归一成
同一个 `LingxiCredential`。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ..base import UpstreamProtocolViolation

#: Cookie 名（上游字段名与 Cookie 名一致，见 UPSTREAM-API 取证）
SID_KEY = "wps_sid"
#: 可选的伴生 cookie：账号 uid 与集群分支（带上更接近真实客户端）
UID_KEY = "uid"
BRANCH_KEY = "copilot_server_branch"
#: 归一后保留的键（其余 cookie 一律丢弃：它们与业务请求无关，留着只会外泄）
KEPT_KEYS = (SID_KEY, UID_KEY, BRANCH_KEY)


def _strip_prefix(text: str) -> str:
    """去掉首尾引号与 `Cookie:` 前缀（用户从 DevTools 复制时常带）。

    顺序必须「先去引号、再去前缀、再去引号」：粘贴常见形态是
    `"Cookie: wps_sid=…"`（整段被引号包住），先判断前缀会漏掉它。
    """
    value = text.strip().strip("\"'").strip()
    if value.lower().startswith("cookie:"):
        value = value[len("cookie:"):].strip()
    return value.strip().strip("\"'")


def parse_cookie_input(raw: str) -> dict[str, str]:
    """整段 Cookie / 裸 sid → `{键: 值}`（只保留需要的三个键）。

    裸 sid（不含 `=`）直接当作 `wps_sid`；含 `=` 的串按 `;` 分段解析。
    空段、缺 `=` 的段、空值一律跳过 —— 那不是可用凭证。
    """
    text = _strip_prefix(raw)
    if not text:
        return {}
    if "=" not in text:
        return {SID_KEY: text}
    values: dict[str, str] = {}
    for segment in text.split(";"):
        key, sep, value = segment.partition("=")
        if not sep:
            continue
        name = key.strip()
        item = value.strip()
        if name in KEPT_KEYS and item:
            values[name] = item
    return values


def _pick(raw: dict[str, Any], *names: str) -> str:
    """按顺序取第一个非空字符串值。"""
    for name in names:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass(slots=True)
class LingxiCredential:
    """归一后的灵犀凭证。"""

    wps_sid: str
    uid: str = ""
    branch: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> LingxiCredential:
        """落库 JSON → 凭证；缺 sid 返回空凭证（调用方据此报错 / 探活失败）。"""
        if not isinstance(raw, dict):
            return cls(wps_sid="")
        # `wps_sid` / `cookie` 都可能装整段 Cookie；`raw` 兼容旧导入字段
        source = _pick(raw, SID_KEY, "cookie", "raw")
        pairs = parse_cookie_input(source)
        sid = pairs.get(SID_KEY, "")
        uid = _pick(raw, UID_KEY) or pairs.get(UID_KEY, "")
        branch = _pick(raw, BRANCH_KEY) or pairs.get(BRANCH_KEY, "")
        return cls(wps_sid=sid, uid=uid, branch=branch)

    @classmethod
    def from_import(cls, raw: Any) -> LingxiCredential:
        """导入入口：形态非法（不是对象 / 没有 sid）直接拒绝，不落空凭证。"""
        if not isinstance(raw, dict):
            raise UpstreamProtocolViolation(
                "credential is not an object: paste {\"cookie\": \"wps_sid=…\"} "
                "or the whole Cookie header")
        credential = cls.from_dict(raw)
        if not credential.wps_sid:
            raise UpstreamProtocolViolation(
                "missing wps_sid: paste the Cookie header from "
                "lingxi.kdocs.cn (contains wps_sid=…) or {\"wps_sid\": \"…\"}")
        return credential

    def to_dict(self) -> dict[str, str]:
        """落库形态：只留非空字段（空串回写会让界面显示一个空 uid）。"""
        out = {SID_KEY: self.wps_sid}
        if self.uid:
            out[UID_KEY] = self.uid
        if self.branch:
            out[BRANCH_KEY] = self.branch
        return out

    def cookie_header(self) -> str:
        """`Cookie:` 头的值（顺序固定，便于测试与日志比对）。"""
        parts = [f"{SID_KEY}={self.wps_sid}"]
        if self.uid:
            parts.append(f"{UID_KEY}={self.uid}")
        if self.branch:
            parts.append(f"{BRANCH_KEY}={self.branch}")
        return "; ".join(parts)

    def identity(self) -> str:
        """稳定身份：`uid` 优先，缺省用 sid 的 sha256 前 16 位。

        **不落 sid 原文**：这个值会进运行缓存键与登录合并键，日志里出现
        就等于泄露登录态。sid 本身对同一账号在重新登录后会变，故 uid 缺失时
        只用于「本次进程内」的隔离，不承诺跨会话合并。
        """
        if self.uid:
            return self.uid
        if not self.wps_sid:
            return ""
        digest = hashlib.sha256(self.wps_sid.encode("utf-8")).hexdigest()
        return f"sid-{digest[:16]}"


#: 桌面客户端伪装用的稳定设备号（固定值，随机值会被当成新设备）。
DEVICE_ID = hashlib.sha256(b"coding2api-lingxi-device").hexdigest()
#: 桌面客户端版本（取证来源：安装包 1.3.31；上游按客户端类型放行）
APP_NAME = "win-lingxi-desktop"
APP_VERSION = "1.3.31"