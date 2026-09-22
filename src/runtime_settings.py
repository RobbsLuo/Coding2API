"""运行时配置覆盖层（B3.2）。

分层原则（这是本模块存在的理由，不是实现细节）：

- **启动期不可变项**（`APP_SECRET` / `DATA_DIR` / `HOST` / `PORT` /
  `USERS_FILE` / 上游端点白名单 …）留在 `config.Settings`，改了必须重启，
  且**不允许**从管理台改——它们决定进程如何启动（监听地址、加密密钥、
  上游白名单）。运行期变更只会让「当前进程」与「磁盘配置」静默分叉，而
  分叉后的行为无法从任一处推断。
- **运行时可覆盖项**（下方 `HOT_SETTINGS` 白名单）经 `RuntimeSettings`
  读取：DB 里有值就用 DB，否则回落 env。**env 是默认值来源，不是被替代
  的权威**；管理台改的是「覆盖意图」，可以在界面上「恢复默认」清掉。

读取语义：`RuntimeSettings` 对非热更属性透明委托给 `Settings`，对热更
属性返回 DB 覆盖值。调用方仍写 `settings.default_model`，无需感知覆盖层
——这样才不会为了热更把每个读配置的地方都改成「先问覆盖层」。

存储：`runtime_settings(key, value, updated_at)`，value 统一为文本，类型
与范围由白名单校验。表里的坏行（白名单外 / 类型非法）在读取时被忽略并记
警告日志：一行坏数据不能让整个服务起不来。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HotSetting:
    """一条可热更配置：类型、取值范围与界面文案。"""

    key: str                            # Settings 属性名，也是 env 变量名（大写）
    kind: type                          # int | float | bool | str
    label: str
    description: str
    minimum: float | None = None
    maximum: float | None = None
    task: str | None = None             # 所属后台任务 key（见 tasks/status.py）；None=网关/调度配置

    @property
    def env_name(self) -> str:
        return self.key.upper()


# 首批可热更清单。刻意不含上游端点、端口、密钥这类启动期项（见模块 docstring）。
HOT_SETTINGS: tuple[HotSetting, ...] = (
    HotSetting("default_model", str, "默认模型",
               "请求未指定 model（或为 auto/空）时使用的模型名。"),
    HotSetting("model_blocklist", str, "模型黑名单",
               "fnmatch glob，逗号分隔；只影响 /v1/models 与 Playground 列表，"
               "直连指定不受影响。留空表示不过滤。"),
    HotSetting("quota_expiry_window_seconds", int, "到期积分主窗口（秒）",
               "把「距到期 ≤ 该秒数」的积分加总作为选号第一排序指标；"
               "≤0 关闭整套到期排序（次窗口一并失效）。", minimum=0),
    # 次窗口是主窗口的配对项：选号按 (主, 次) 字典序比较，只热更主窗口而
    # 次窗口留在 env，会让管理台展示与调度器用两套数字。一并对齐。
    HotSetting("quota_expiry_secondary_window_seconds", int, "到期积分次窗口（秒）",
               "主窗口打平（含都为 0）时才参与比较；主窗口 ≤0 时本项自动失效。",
               minimum=0),
    HotSetting("conversation_sticky_seconds", int, "会话粘性 TTL（秒）",
               "同一对话的多轮请求固定用同一凭证；≤0 关闭粘性。"),
    HotSetting("growth_irreversible_actions", bool, "成长中心不可逆动作",
               "是否允许抽奖 / 连登兑换 / 开盲盒 / 消耗补登卡。关闭后仍会领取"
               "旅行礼物与任务奖励。", task="growth"),
    HotSetting("growth_interval_minutes", int, "成长中心周期（分钟）",
               "成长中心后台任务的一轮间隔；下限 5 分钟（更密只会撞上游风控）。",
               minimum=0, task="growth"),
    HotSetting("quota_probe_minutes", int, "额度探测周期（分钟）",
               "后台额度探测的一轮间隔；下限 1 分钟。", minimum=0,
               task="quota_probe"),
    HotSetting("codebuddy_chat_min_interval", float, "CodeBuddy 聊天最小间隔（秒）",
               "与 TRAE 共享的最小请求间隔（0 关闭）；改小会显著提高风控概率。",
               minimum=0.0),
    HotSetting("pacer_min_seconds", float, "后台任务节流下限（秒）",
               "后台任务相邻上游请求的最小间隔；0 关闭节流。", minimum=0.0),
    HotSetting("pacer_max_seconds", float, "后台任务节流上限（秒）",
               "后台任务相邻上游请求的最大间隔，必须不小于下限。", minimum=0.0),
    HotSetting("activity_report_enabled", bool, "活跃上报",
               "是否为 CodeBuddy 账号补发对话事件以续连连登天数。官方条款禁止"
               "脚本篡改活动数据，开启前请自行评估账号风险。", task="activity"),
    # 与开关配对：开着但时点不对等于没开，只热更开关会让用户以为改坏了。
    HotSetting("activity_report_hour", int, "活跃上报时点（0-23）",
               "本地（北京）时间整点窗口；该小时内每 10 分钟检查一次，每号每天"
               "最多补发一条。", minimum=0, maximum=23, task="activity"),
)

HOT_BY_KEY: dict[str, HotSetting] = {item.key: item for item in HOT_SETTINGS}


class InvalidSetting(ValueError):
    """配置值非法（未知 key / 类型错误 / 越界 / 组合非法）。"""


def parse_value(key: str, raw: str) -> Any:
    """文本 → 有类型的值；非法时抛 InvalidSetting（消息可直接回给用户）。"""
    spec = HOT_BY_KEY.get(key)
    if spec is None:
        raise InvalidSetting(f"unknown setting {key!r}")
    return _coerce(spec, raw)


def _coerce(spec: HotSetting, raw: Any) -> Any:
    """按白名单类型解析并做范围校验（bool 必须先于 int 判断）。"""
    text = raw if isinstance(raw, str) else str(raw)
    try:
        if spec.kind is bool:
            value: Any = _parse_bool(text)
        elif spec.kind is int:
            value = int(text)
        elif spec.kind is float:
            value = float(text)
        else:
            value = text
    except (TypeError, ValueError) as error:
        raise InvalidSetting(
            f"{spec.key} 期望 {spec.kind.__name__}，收到 {raw!r}") from error
    if spec.kind is str and not value.strip() and spec.key == "default_model":
        raise InvalidSetting("default_model 不能为空")
    if spec.minimum is not None and value < spec.minimum:
        raise InvalidSetting(f"{spec.key} 不能小于 {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise InvalidSetting(f"{spec.key} 不能大于 {spec.maximum}")
    return value


def _parse_bool(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise InvalidSetting(f"布尔值应为 true/false，收到 {text!r}")


def _is_reset(raw: Any) -> bool:
    """null / 空串 = 「恢复默认」（删除覆盖行），不是「设为空值」。"""
    if raw is None:
        return True
    return isinstance(raw, str) and not raw.strip()


def format_value(value: Any) -> str:
    """有类型的值 → 存储文本（与 parse_value 互逆）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def validate_group(values: dict[str, Any], current: Callable[[str], Any] | None = None) -> None:
    """跨字段校验：单字段各自合法、组合起来仍可能非法。

    目前只有节流上下限一处（min ≤ max），但校验必须发生在**写入前**——
    持久化一个下限大于上限的组合会让下一个读它的请求抛异常，而那时已无从
    追溯是谁写坏的。

    `current` 用于补全「本次没提交、但已在生效」的对端值：只改下限时也要
    与现存上限比较，否则单字段提交就能绕过校验。调用方（`set_many`）传入
    的是「这次提交落地之后」的对端值，因此对端被恢复默认时比较的是 env
    默认值，而不是马上要被删掉的旧覆盖值。
    """
    parsed: dict[str, Any] = {}
    for key, raw in values.items():
        spec = HOT_BY_KEY.get(key)
        if spec is None:
            raise InvalidSetting(f"unknown setting {key!r}")
        if _is_reset(raw):
            continue                      # 恢复默认：值由 current 给出
        parsed[key] = _coerce(spec, raw)
    pairs = (("pacer_min_seconds", "pacer_max_seconds"),)
    for low_key, high_key in pairs:
        if low_key not in parsed and high_key not in parsed:
            continue
        low = parsed.get(low_key, current(low_key) if current else None)
        high = parsed.get(high_key, current(high_key) if current else None)
        if low is None or high is None:
            continue
        if low > high:
            raise InvalidSetting(f"{low_key} 不能大于 {high_key}")


class SettingsStore(Protocol):
    """只依赖读写四个动作，避免从本模块反向 import 仓储层（会成环）。"""

    def load(self) -> dict[str, str]: ...
    def set(self, key: str, value: str, now: int | None = None) -> None: ...
    def delete(self, key: str) -> None: ...


class RuntimeSettings:
    """`Settings` 的热更覆盖层：热更键读覆盖值，其余透明委托。

    覆盖值在内存里缓一份，写入后由本类负责刷新；读取路径不走数据库，
    否则每次选号/建请求都要查一次库，热更省下的重启根本不值得这个开销。
    """

    def __init__(self, base: Settings, store: SettingsStore) -> None:
        self._base = base
        self._store = store
        self._overrides: dict[str, str] = {}
        self.reload()

    # ------------------------------------------------------------- 覆盖层

    def reload(self) -> None:
        """从 DB 重读覆盖值；非法/未知行跳过并记日志（不让坏行拖垮服务）。"""
        loaded: dict[str, str] = {}
        for key, raw in self._store.load().items():
            try:
                parse_value(key, raw)
            except InvalidSetting as error:
                logger.warning("忽略非法的运行时配置 %s=%r: %s", key, raw, error)
                continue
            loaded[key] = raw
        self._overrides = loaded

    def get(self, key: str) -> Any:
        """生效值：DB 覆盖 > env 默认。未知 key 直接回落 Settings 属性。"""
        raw = self._overrides.get(key)
        if raw is not None:
            return parse_value(key, raw)
        return getattr(self._base, key)

    def is_overridden(self, key: str) -> bool:
        return key in self._overrides

    def env_value(self, key: str) -> Any:
        """env/默认值（不论是否被覆盖），供界面展示「默认是什么」。"""
        return getattr(self._base, key)

    def current(self, key: str) -> Any:
        """当前生效值，供跨字段校验补全「本次没提交」的对端。"""
        return self.get(key)

    def set_many(self, values: dict[str, Any], now: int | None = None) -> None:
        """批量写入（先整组校验再落库）。

        整组校验的意义：一条条写、写到一半失败，库里就留下「一半新一半旧」
        的组合，而校验恰好看的是组合。先把这一批全部校验过，再逐条写。

        组合校验按「这一批写完之后的样子」做：同批里被重置的对端，比较的
        是它回落到的 env 默认值，而不是马上要被删掉的旧覆盖值。
        """
        wanted = list(values.items())
        prospective = dict(self._overrides)
        for key, raw in wanted:
            if HOT_BY_KEY.get(key) is None:
                raise InvalidSetting(f"unknown setting {key!r}")
            if _is_reset(raw):
                prospective.pop(key, None)
            else:
                prospective[key] = format_value(_coerce(HOT_BY_KEY[key], raw))

        def effective_after(key: str):
            if key in prospective:
                return parse_value(key, prospective[key])
            return getattr(self._base, key)

        validate_group(values, current=effective_after)
        for key, raw in wanted:
            self.set(key, raw, now)

    def set(self, key: str, raw: Any, now: int | None = None) -> None:
        """写入覆盖值（已校验）；null / 空文本表示「恢复默认」= 删除该行。"""
        if HOT_BY_KEY.get(key) is None:
            raise InvalidSetting(f"unknown setting {key!r}")
        if _is_reset(raw):
            self.reset(key)
            return
        text = format_value(_coerce(HOT_BY_KEY[key], raw))
        self._store.set(key, text, now)
        self.reload()

    def reset(self, key: str, now: int | None = None) -> None:
        if HOT_BY_KEY.get(key) is None:
            raise InvalidSetting(f"unknown setting {key!r}")
        self._store.delete(key)
        self.reload()

    def snapshot(self) -> list[dict[str, Any]]:
        """管理台列表：每条含生效值、默认值、来源与文案。"""
        return [
            {
                "key": spec.key,
                "env_name": spec.env_name,
                "label": spec.label,
                "description": spec.description,
                "kind": spec.kind.__name__,
                "value": self.get(spec.key),
                "default": self.env_value(spec.key),
                "overridden": self.is_overridden(spec.key),
                # 所属后台任务（管理台据此把配置归到任务卡片下）；None = 网关/调度项
                "task": spec.task,
            }
            for spec in HOT_SETTINGS
        ]

    # ------------------------------------------------------ 热更属性入口

    @property
    def default_model(self) -> str:
        return str(self.get("default_model"))

    @property
    def model_blocklist(self) -> str:
        return str(self.get("model_blocklist"))

    @property
    def blocklist_patterns(self) -> tuple[str, ...]:
        """与 `Settings.blocklist_patterns` 同口径，但基于生效值重算。

        不能直接委托给 Settings 的同名 `cached_property`：那会把 env 值永久
        缓存，覆盖层改了黑名单，模型列表仍是旧规则。
        """
        return tuple(p.strip() for p in self.model_blocklist.split(",") if p.strip())

    @property
    def quota_expiry_window_seconds(self) -> int:
        return int(self.get("quota_expiry_window_seconds"))

    @property
    def quota_expiry_secondary_window_seconds(self) -> int:
        return int(self.get("quota_expiry_secondary_window_seconds"))

    @property
    def conversation_sticky_seconds(self) -> int:
        return int(self.get("conversation_sticky_seconds"))

    @property
    def growth_irreversible_actions(self) -> bool:
        return bool(self.get("growth_irreversible_actions"))

    @property
    def growth_interval_minutes(self) -> int:
        return int(self.get("growth_interval_minutes"))

    @property
    def quota_probe_minutes(self) -> int:
        return int(self.get("quota_probe_minutes"))

    @property
    def codebuddy_chat_min_interval(self) -> float:
        return float(self.get("codebuddy_chat_min_interval"))

    @property
    def pacer_min_seconds(self) -> float:
        return float(self.get("pacer_min_seconds"))

    @property
    def pacer_max_seconds(self) -> float:
        return float(self.get("pacer_max_seconds"))

    @property
    def activity_report_enabled(self) -> bool:
        return bool(self.get("activity_report_enabled"))

    @property
    def activity_report_hour(self) -> int:
        return int(self.get("activity_report_hour"))

    # ---------------------------------------------------------- 透明委托

    def __getattr__(self, name: str) -> Any:
        """非热更属性（app_secret / is_admin / db_path …）全部回落 Settings。

        只处理「正常查找失败」的属性，热更属性有显式 property，优先命中；
        下划线开头的内部名直接抛 AttributeError，避免 reload 期间被
        copy/pickle 之类触发无限递归。
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._base, name)


def load_runtime_settings(base: Settings, store: SettingsStore) -> RuntimeSettings:
    """构造并加载覆盖层（进程内单例，挂在 app.state / Services 上）。"""
    return RuntimeSettings(base, store)


def now_seconds() -> int:
    return int(time.time())
