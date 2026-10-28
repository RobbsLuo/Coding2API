"""运行时配置覆盖层。

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

import ipaddress
import logging
import math
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HotSetting:
    """一条可热更配置：类型、取值范围与界面文案。"""

    key: str                            # Settings 属性名，也是 env 变量名（大写）
    kind: type                          # int | float | bool | str
    label: str
    description: str
    minimum: float | None = None        # 写入校验下限（管理台/env 写入时拦截）
    maximum: float | None = None
    task: str | None = None             # 所属后台任务 key（见 tasks/status.py）；None=网关/调度配置
    group: str | None = None            # task 为 None 时的分组 key（见 GATEWAY_GROUPS）
    # 生效下限（读时钳制）：存量覆盖值可能低于现行下限（旧版 minimum 更宽时
    # 写入），读路径直接钳到 floor 自愈——否则 UI 回显写入值、实际跑钳制值，
    # 两者静默分叉。floor ≥ minimum 恒成立。
    floor: float | None = None

    @property
    def env_name(self) -> str:
        return self.key.upper()


# 无任务归属的配置项（`task is None`）在管理台里的分组：key → 界面上的一级 tab
# 名。顺序即前端展示顺序；分组归属与标签由后端定义，前端不硬编码 key 列表，
# 后端加项/改归属不用动前端。留空的分组不渲染。
GATEWAY_GROUPS: tuple[tuple[str, str], ...] = (
    ("routing", "模型路由"),
    ("affinity", "选号与会话"),
    ("channels", "渠道节流"),
    ("pacer", "后台任务节流"),
    ("observability", "可观测性"),
)


# 首批可热更清单。刻意不含上游端点、端口、密钥这类启动期项（见模块 docstring）。
HOT_SETTINGS: tuple[HotSetting, ...] = (
    HotSetting("default_model", str, "默认模型",
               "请求未指定 model（或为 auto/空）时使用的模型名。",
               group="routing"),
    HotSetting("model_blocklist", str, "模型黑名单",
               "fnmatch glob，逗号分隔；只影响 /v1/models 与 Playground 列表，"
               "直连指定不受影响。留空表示不过滤。",
               group="routing"),
    HotSetting("context_compress_enabled", bool, "上下文压缩",
               "按模型目录里的输入上限裁剪过长对话，避免撞上游硬限制"
               "（CodeBuddy 11115 prompt is too long）。目录里查不到上限的模型"
               "不受影响。", group="routing"),
    HotSetting("context_compress_reserve_tokens", int, "上下文压缩：预留输出 token",
               "压缩预算里为模型回复预留的输出 token 数。", minimum=0,
               group="routing"),
    HotSetting("context_compress_min_keep_messages", int, "上下文压缩：至少保留消息数",
               "无论多长都保留的最近消息条数（保证当前这轮对话完整）。",
               minimum=0, group="routing"),
    HotSetting("context_compress_safety_ratio", float, "上下文压缩：安全系数",
               "按模型上限的百分比计算压缩预算，给 token 估算误差留余量。",
               minimum=0.0, maximum=1.0, group="routing"),
    HotSetting("context_compress_sticky_enabled", bool, "上下文压缩：粘性裁剪",
               "开启后一次裁到预算的 60% 并记住保留窗口的起点，跨轮前缀不变，"
               "上游前缀缓存才能命中（客户端每轮重发完整历史时，不粘性就只有 "
               "system 段可复用，命中率会掉到个位数）。关闭 = 每轮按当轮预算"
               "重算，只有前缀本来就单调增长时才划算。",
               group="routing"),
    HotSetting("model_fallback_groups", str, "跨渠道 fallback 兼容组",
               "请求模型在主渠道全部不可用时，按兼容组依次回退到组内其他模型。"
               "格式 `组名=成员1,成员2;组名2=成员3`；成员可带 `@渠道` 固定回退渠道。"
               "留空 = 不启用。回退会用尽该组（×3 轮换预算）后正常 503。",
               group="routing"),
    HotSetting("model_aliases", str, "自定义模型别名",
               "给客户端一个稳定的对外模型名（上游改 id 时只改这里）。格式 "
               "`对外名=模型@渠道;对外名2=模型`；列表与请求解析用同一份。"
               "与真实模型 id 或自动别名撞车视为配置错误，不静默覆盖。"
               "留空 = 不启用。", group="routing"),
    HotSetting("stream_first_event_timeout_seconds", float, "流式首事件超时（秒）",
               "上游接了连接却长时间不吐第一个数据帧时，在**出帧前**换号重试"
               "（此时切换安全）。注释帧/心跳不重置计时。0 = 关闭；实测慢上下文"
               "首字可达 40–71s，内置默认必须保持关闭，用时按部署实际填（推荐 60）。",
               minimum=0.0, group="routing"),
    HotSetting("stream_idle_timeout_seconds", float, "流式空闲超时（秒）",
               "**已出帧**后上游长时间不再吐数据时断流并以错误帧收尾——半截输出"
               "不可回滚，换号会把两个账号的内容拼给客户端。0 = 关闭（推荐 300）。",
               minimum=0.0, group="routing"),
    HotSetting("short_cooldown_wait_seconds", float, "短冷却等待上限（秒）",
               "全池都在短冷却、没有任何立即可用候选时，最短剩余冷却不超过该值"
               "则等待后续上（而非直接 503）。等待期间不占在途租约。0 = 关闭"
               "（快速失败，保持旧行为）。", minimum=0.0, group="affinity"),
    HotSetting("nonstream_keepalive_seconds", float, "非流式保活（秒）",
               "`stream=false` 的请求在聚合完成前不发任何字节，CDN / 反代的空闲"
               "超时会切连接。设为 N>0 时，N 秒内没完成就转 chunked，先吐空白"
               "再补 JSON 正文——空白是合法 JSON 前导（RFC 8259），解析器照常"
               "工作。0 = 关闭（本部署 nginx 3600s + frp TCP 无此问题）；"
               "只在 CDN 后面需要，推荐 15。", minimum=0.0, group="routing"),
    HotSetting("quota_expiry_window_seconds", int, "到期积分主窗口（秒）",
               "把「距到期 ≤ 该秒数」的积分加总作为选号第一排序指标；"
               "≤0 关闭整套到期排序（次窗口一并失效）。", minimum=0,
               group="affinity"),
    # 次窗口是主窗口的配对项：选号按 (主, 次) 字典序比较，只热更主窗口而
    # 次窗口留在 env，会让管理台展示与调度器用两套数字。一并对齐。
    HotSetting("quota_expiry_secondary_window_seconds", int, "到期积分次窗口（秒）",
               "主窗口打平（含都为 0）时才参与比较；主窗口 ≤0 时本项自动失效。",
               minimum=0, group="affinity"),
    HotSetting("conversation_sticky_seconds", int, "会话粘性 TTL（秒）",
               "同一对话的多轮请求固定用同一凭证；≤0 关闭粘性。",
               group="affinity"),
    HotSetting("max_inflight_per_credential", int, "单账号在途上限",
               "同一凭证的并发请求达到该值后不再参与选号，避免把单账号打进上游"
               "限流/封号（跨渠道全局生效）。0 = 不限。租约在请求结束后释放，"
               "另有 600s TTL 兜底漏释放。在途数同时是选号的负载键：并发请求"
               "按「在途最少优先」摊到空闲号（0 = 不限时也照常计数）。",
               minimum=0, group="affinity"),
    HotSetting("growth_irreversible_actions", bool, "成长中心不可逆动作",
               "是否允许抽奖 / 连登兑换 / 开盲盒 / 消耗补登卡。关闭后仍会领取"
               "旅行礼物与任务奖励。", task="growth"),
    HotSetting("growth_interval_minutes", int, "成长中心周期（分钟）",
               "成长中心后台任务的一轮间隔；下限 5 分钟（更密只会撞上游风控）。",
               minimum=5, task="growth", floor=5),
    HotSetting("refresh_interval_minutes", int, "token 预刷新周期（分钟）",
               "后台 token 预刷新任务的一轮间隔；下限 5 分钟。短寿命渠道"
               "（CodeArts STS 2h）会把预刷新窗口封顶，此时该周期必须窄于窗口，"
               "否则窗口整轮漏过、凭证拖到到期才刷（上游 401 + 管理台标红）。",
               minimum=5, task="refresh", floor=5),
    HotSetting("quota_probe_minutes", int, "额度探测周期（分钟）",
               "后台额度探测的一轮间隔；下限 1 分钟。", minimum=1,
               task="quota_probe", floor=1),
    HotSetting("model_catalog_minutes", int, "渠道模型列表刷新周期（分钟）",
               "后台兜底刷新各渠道模型表的间隔；没有它，模型列表只在有人调 "
               "/v1/models 或 Playground 时才刷新，纯 API 用法的部署会让"
               "「模型→渠道」归属表与落盘快照一起变陈旧。下限 5 分钟。",
               minimum=5, task="model_catalog", floor=5),
    HotSetting("model_probe_minutes", int, "模型探活周期（分钟）",
               "对仍被负缓存（上游「无此模型」）与已判死的 (渠道, 模型) 发一次"
               "最小对话的间隔：探活成功立即恢复展示并解除避让，额度/限流类"
               "失败只记「不确定」不禁用。判死即从模型列表摘掉且无到期时间，"
               "复探是唯一的恢复入口。"
               "0 = 关闭（默认）：探活会真发上游请求、消耗额度，是否开启按余量"
               "决定；负缓存本身 6 小时起步，开启后建议 ≥60 分钟。",
               minimum=0, task="model_probe"),
    HotSetting("openrouter_catalog_minutes", int, "模型目录刷新周期（分钟）",
               "后台拉取 OpenRouter 公开模型列表的间隔：一次抓取代刊例价（成本"
               "估算）、明细元数据（「模型列表」页）与 Artificial Analysis 能力"
               "指数（Playground / /v1/models 的 benchmarks）三份数据。上游变动"
               "很少，默认每日一次；拉取失败只是成本与分数显示 —。下限 60 分钟。",
               minimum=60, task="openrouter_catalog", floor=60),
    HotSetting("usd_cny_rate", float, "美元兑人民币汇率",
               "成本估算里把 OpenRouter 的美元刊例价折成人民币的汇率"
               "（1 USD = 该值 CNY）；改后只影响之后写入的请求，历史成本不重算。",
               minimum=0.000001, group="routing"),
    HotSetting("codebuddy_chat_min_interval", float, "CodeBuddy 聊天最小间隔（秒）",
               "同渠道同凭证顺序连发的最小请求间隔（0 关闭）；按凭证分桶、桶内"
               "允许并发，故同渠道同模型的并发请求不会被串行化。",
               minimum=0.0, group="channels"),
    HotSetting("codebuddy_intl_chat_min_interval", float,
               "CodeBuddy 国际聊天最小间隔（秒）",
               "国际站（www.codebuddy.ai）专用节流（默认 5s，0 关闭）。与国内站"
               "各自独立、不共享——共用会让两站请求排进同一条队列互相空等。",
               minimum=0.0, group="channels"),
    HotSetting("zen_chat_min_interval", float, "Zen 聊天最小间隔（秒）",
               "zen 匿名免费层专用节流（0 关闭，默认）。与 CodeBuddy/TRAE 各自"
               "独立、不共享——共享会让 zen 请求排在 CB/TRAE 之后空等最小间隔。",
               minimum=0.0, group="channels"),
    HotSetting("kilo_chat_min_interval", float, "Kilo 聊天最小间隔（秒）",
               "Kilo 匿名免费层专用节流（0 关闭，默认）。与 zen / CodeBuddy / TRAE"
               "各自独立，不共享。", minimum=0.0, group="channels"),
    HotSetting("qoder_chat_min_interval", float, "Qoder 聊天最小间隔（秒）",
               "Qoder 真实账号渠道专用节流（默认 5s，0 关闭）。与其余渠道各自"
               "独立，不共享。", minimum=0.0, group="channels"),
    HotSetting("codearts_chat_min_interval", float, "CodeArts 聊天最小间隔（秒）",
               "CodeArts 真实账号渠道专用节流（默认 5s，0 关闭）。与其余渠道各自"
               "独立，不共享。", minimum=0.0, group="channels"),
    HotSetting("officeace_chat_min_interval", float,
               "OfficeAce 聊天最小间隔（秒）",
               "OfficeAce 真实账号渠道专用节流（默认 0 关闭）。上游没有 CodeArts "
               "那种硬并发限，被限流时走伪 200 的模型级短冷却；与其余渠道各自"
               "独立，不共享。", minimum=0.0, group="channels"),
    HotSetting("codearts_max_concurrency", int, "CodeArts 每账号并发上限",
               "CodeArts 上游硬限每账号并发会话数 3，超限请求直接 400 "
               "TM.00001041（实测 77% 失败率主因）。默认 3 对齐上游；0 关闭上限"
               "（回到「有在途即放行」，会再次击穿）。", minimum=0,
               group="channels"),
    HotSetting("codearts_request_window_seconds", float, "CodeArts 账号节流窗口（秒）",
               "与「每账号并发上限」组合成滑动窗口：最近这么多秒内最多启动 N 次。"
               "上游口径是「每账号每约 60s 最多 3 个会话」（会话在流结束后仍滞留"
               "数十秒），纯在途上限挡不住突发，故默认 60s；0 关闭窗口口径。",
               minimum=0.0, group="channels"),
    HotSetting("pacer_min_seconds", float, "后台任务节流下限（秒）",
               "后台任务相邻上游请求的最小间隔；0 关闭节流。", minimum=0.0,
               group="pacer"),
    HotSetting("pacer_max_seconds", float, "后台任务节流上限（秒）",
               "后台任务相邻上游请求的最大间隔，必须不小于下限。", minimum=0.0,
               group="pacer"),
    HotSetting("activity_report_enabled", bool, "活跃上报",
               "是否为 CodeBuddy（含国际）账号补发对话事件以续连连登天数。官方"
               "条款禁止脚本篡改活动数据，开启前请自行评估账号风险。", task="activity"),
    # 与开关配对：开着但时点不对等于没开，只热更开关会让用户以为改坏了。
    HotSetting("activity_report_hour", int, "活跃上报时点（0-23）",
               "本地（北京）时间整点窗口；该小时内每 10 分钟检查一次，每号每天"
               "最多补发一条。", minimum=0, maximum=23, task="activity"),
    HotSetting("checkin_times", str, "签到时点",
               "本地时间 HH:MM，逗号分隔（如 `09:37,21:10`）。命中时点才跑一轮"
               "签到；留空 = 全天每 10 分钟检查一次（旧行为）。上游签到幂等，"
               "多设几个时点只是多几次重试机会。", task="checkin"),
    HotSetting("checkin_jitter_minutes", int, "签到抖动（分钟）",
               "在时点**之前**提前一个 0..N 分钟的随机偏移执行，避免多部署"
               "同一分钟齐打上游。按「日期 + 时点」去重，抖动不会让同一时点"
               "跑两次。0 = 不抖。", minimum=0, maximum=1440, task="checkin"),
    HotSetting("log_buffer_lines", int, "日志页保留行数",
               "「任务与配置」→ 日志页的进程内环形缓冲行数（只收本进程日志，"
               "不读文件）。0 = 关闭该页（端点返回 404）。日志行发出前经脱敏"
               "白名单过滤，含 token / secret / authorization / cookie 的行整行丢弃。",
               minimum=0, group="observability"),
    # 运维告警：全部归到「运维告警」任务卡片下——改阈值时人就在看
    # 这个任务最近报了什么。webhook 留空 = 只留站内记录、不外推。
    HotSetting("alert_enabled", bool, "运维告警开关",
               "是否启用后台告警评估。关闭后不落站内记录也不推送 webhook。",
               task="alert"),
    HotSetting("alert_webhook_url", str, "告警 Webhook 地址",
               "命中告警时 POST JSON 的地址，多个用逗号分隔；留空 = 只在管理台"
               "留站内记录、不外推。", task="alert"),
    HotSetting("alert_interval_minutes", int, "运维告警周期（分钟）",
               "后台评估四类风险的一轮间隔；下限 1 分钟。", minimum=1,
               task="alert", floor=1),
    HotSetting("alert_silence_minutes", int, "告警静默窗（分钟）",
               "同一条告警（规则 + 对象）在该窗口内只落库/推送一次，避免持续"
               "状态每轮刷屏；0 关闭静默（每轮都报）。", minimum=0, task="alert"),
    HotSetting("alert_for_ticks", int, "告警进入迟滞（连续命中轮数）",
               "同一条告警连续命中这么多轮才真正告警（滤掉单轮毛刺）；"
               "1 = 不迟滞、首轮即报。", minimum=1, task="alert", floor=1),
    HotSetting("alert_clear_ticks", int, "告警恢复迟滞（连续未命中轮数）",
               "告警中的条目连续这么多轮不再命中才确认恢复（写 resolved_at "
               "并外推恢复通知），避免单轮抖动误撤。", minimum=1,
               task="alert", floor=1),
    HotSetting("alert_pool_ready_min", int, "池耗尽阈值（可用数）",
               "凭证池可用数少于该值时告警（服务活着但用不了）；0 关闭该规则。",
               minimum=0, task="alert"),
    HotSetting("alert_task_failures", int, "任务连续失败阈值",
               "某后台任务连续失败达到该次数时告警（成功一轮清零）；0 关闭该规则。",
               minimum=0, task="alert"),
    HotSetting("alert_token_expiry_hours", int, "token 到期预警窗口（小时）",
               "凭证 token 剩余时间少于该小时数时告警；0 关闭该规则。",
               minimum=0, task="alert"),
    HotSetting("alert_error_rate_threshold", float, "上游错误率阈值",
               "统计窗内失败占比达到该值时告警；0 关闭该规则。", minimum=0.0,
               maximum=1.0, task="alert"),
    HotSetting("alert_error_rate_min_requests", int, "错误率最小样本数",
               "统计窗内请求数少于该值时不判错误率（样本太小无意义）。",
               minimum=1, task="alert"),
    HotSetting("alert_error_rate_window_minutes", int, "错误率统计窗（分钟）",
               "计算上游错误率的时间窗；0 表示只看当前瞬间（无样本，不触发）。",
               minimum=0, task="alert"),
    HotSetting("alert_key_burst_tokens", int, "单 Key 用量暴涨阈值（token）",
               "统计窗内某把 API Key 的 token 总量达到该值即告警（Key 泄露/"
               "多人共用的信号）；0 关闭该规则。", minimum=0, task="alert"),
    HotSetting("alert_key_burst_window_minutes", int, "单 Key 用量统计窗（分钟）",
               "按 Key 聚合 token 的时间窗；0 关闭该规则。", minimum=0, task="alert"),
    HotSetting("alert_off_hours_start", int, "非工作时段起点（时）",
               "与「终点」一起定义工作时间 `[start, end)`（本地时区，可跨零点，"
               "如 9 → 19）。起点与终点相等 = 关闭该规则。", minimum=0,
               maximum=23, task="alert"),
    HotSetting("alert_off_hours_end", int, "非工作时段终点（时）",
               "工作时间区间的右端（不含）。与起点相等 = 关闭该规则。",
               minimum=0, maximum=23, task="alert"),
    HotSetting("alert_off_hours_min_requests", int, "非工作时段请求数阈值",
               "非工作时段内请求数达到该值才告警（避免个别重试误报）；0 关闭。",
               minimum=0, task="alert"),
    HotSetting("alert_budget_cny", float, "月度成本预算（元）",
               "按本地自然月累计可定价成本，达到分层阈值时告警；0 关闭该规则。"
               "只统计匹配到价表与汇率的明细，未定价模型不计入。", minimum=0.0,
               task="alert"),
    HotSetting("alert_budget_levels", str, "预算分层（%）",
               "逗号分隔的百分比档位，默认 50,80,95,100；只报当前达到的最高档，"
               "100% 为 critical。留空 = 不分层（规则不生效）。", task="alert"),
    # SQLite 备份：改周期时人就在看这个任务最近备份了什么。
    HotSetting("backup_interval_minutes", int, "备份周期（分钟）",
               "后台把主库一致性快照（VACUUM INTO）写进 DATA_DIR/backups/ 的"
               "一轮间隔；默认每日一次。库不大，更密只是白占磁盘。下限 60 分钟。",
               minimum=60, task="backup", floor=60),
    HotSetting("backup_keep", int, "备份保留份数",
               "只保留最近这么多份备份，更旧的按时间删除；0 表示不清理（只备份）。",
               minimum=0, task="backup"),
    # 上游状态页聚合：改周期时人就在看这个任务最近抓到了什么。
    HotSetting("upstream_status_enabled", bool, "上游状态页抓取",
               "是否抓取各模型 / 网关提供方的官方状态接口并在「上游状态」页展示。"
               "关闭后页面保留上一次快照并标注已关闭。", task="upstream_status"),
    HotSetting("upstream_status_minutes", int, "上游状态页周期（分钟）",
               "并发抓取全部状态源的一轮间隔；官方状态页本身是分钟级更新，抓得"
               "更密只是白打别人的接口。下限 1 分钟。", minimum=1,
               task="upstream_status", floor=1),
    HotSetting("prefix_debug_enabled", bool, "前缀缓存诊断",
               "每次请求往日志写一行载荷指纹：消息数、system/tools 摘要、与同凭证"
               "同模型上一条请求的最长公共前缀消息序号、分叉处消息摘要（全部是"
               "sha256 前 16 位，不含正文）。用来定位命中率异常时前缀在哪条消息"
               "分叉、压缩是否每轮都在裁剪。默认关。",
               group="observability"),
    HotSetting("gateway_paused", bool, "网关暂停（kill switch）",
               "开启后所有 API Key 鉴权的 `/v1` 端点（chat / messages / responses /"
               " models / usage / balance）立即返 503（`type=service_paused`），"
               "管理台与登录照常可用——异常消耗或上游事故时一键止血。暂停原因"
               "写进日志与审计，不回显给调用方。默认关。",
               group="routing"),
    HotSetting("gateway_pause_reason", str, "暂停原因",
               "配合「网关暂停」使用：写清为什么停（事故编号 / 上游状态页链接），"
               "只进服务端日志与审计。留空按「未填原因」记录。",
               group="routing"),
    HotSetting("dump_request_bodies", bool, "请求体诊断落盘",
               "把每次 `/v1/chat/completions` 的**原始请求体**（含完整对话内容）"
               "写到 `DATA_DIR/dumps/`，目录 0700、文件 0600、保留 200 个且不超过 "
               "24 小时。只用于排查客户端差异，**排查完立刻关**：这是本服务唯一"
               "把用户对话落盘的开关。改动会进审计。默认关。",
               group="observability"),
    HotSetting("metrics_enabled", bool, "Prometheus 指标出口",
               "是否暴露 `GET /metrics`（Prometheus text 格式：请求/token/凭证池/"
               "失败计数）。**不鉴权**，与 /healthz 同口径；指标已禁用高基数标签"
               "（不含凭证 ID / 用户名）。关闭后端点返回 404。",
               group="observability"),
    HotSetting("request_log_enabled", bool, "请求级结构化日志",
               "每个业务请求（`/api/*`、`/v1/*`、`/authorize`）写一行 logfmt："
               "`event=request request_id=… method=… route=… status=… duration_ms=…`。"
               "路由用模板（`/api/credentials/{credential_id}`）而不是实际 ID；"
               "**不写 query string、请求体、用户名**。5xx 记 WARNING、其余记 INFO。"
               "关闭后只剩 uvicorn 的裸 access log。",
               group="observability"),
    # reasoning 控制：只给机制、不给策略（不内置默认矩阵——未核实就是编造）
    HotSetting("force_disable_thinking", bool, "剥离思考内容",
               "开启后出站响应不再包含模型的思考内容（chat 的 reasoning_content / "
               "Anthropic 的 thinking 块 / Responses 的 reasoning item 一并过滤）。"
               "**只过滤响应、不改写请求体**：上游仍照常思考，只是不交给客户端。",
               group="routing"),
    HotSetting("reasoning_effort_map", str, "档位映射（出站）",
               "把客户端显式给的 `reasoning_effort` 改写成渠道期望的档位。格式 "
               "`渠道=from:to,from:to;*=from:to`（渠道取 KNOWN_PROVIDERS，`*` 兜底）；"
               "目标写 `none` 表示删除该键、走渠道默认。留空 = 原样透传。只改写"
               "客户端显式值，渠道自身补的默认档不受影响。",
               group="routing"),
    HotSetting("anthropic_thinking_efforts", str, "Anthropic thinking→档位",
               "把 Anthropic 入站的 `thinking.budget_tokens` 翻成上游 "
               "`reasoning_effort`。格式 `预算:档位,预算:档位,disabled:档位`："
               "`disabled` 匹配 `thinking.type==\"disabled\"`，其余取「阈值 ≤ "
               "budget_tokens 中最大的那一档」，低于最小阈值则不映射。留空 = "
               "完全不映射（`thinking` 照旧忽略）。Anthropic 未给档位对应表，"
               "阈值由使用者自行决定。",
               group="routing"),
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


# 字符串类热更项的字符上限（L7）：模型名/黑名单/webhook 地址都是短文本。
# 取 64 KiB：远超任何合法配置，只拦粘贴事故级别的畸形写入。
MAX_STRING_SETTING_LENGTH = 64 * 1024


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
    # NaN 与任何数比较都为 False，会同时绕过下面的 minimum / maximum；±inf 对
    # 未设 maximum 的 float 项（各 *_chat_min_interval、pacer_min/max_seconds、
    # codearts_request_window_seconds）也直接通过。非有限值一律拒——例如
    # pacer_min_seconds=inf 会让 Pacer 持锁永久挂起、后台任务全卡死，
    # *_chat_min_interval=nan 会静默关闭节流（见 tasks/pacer.py）。
    if spec.kind is float and not math.isfinite(value):
        raise InvalidSetting(f"{spec.key} 必须是有限数值，收到 {raw!r}")
    if spec.kind is str and not value.strip() and spec.key == "default_model":
        raise InvalidSetting("default_model 不能为空")
    # 字符串类热更项设长度上限（L7）：模型名/黑名单/地址都是短文本，无上限时
    # 一个粘贴事故就能把 MB 级文本写进 DB 并每请求读取。非字符串输入经 str()
    # 强转（如 dict → "{'a': 1}"），上限同时也拦住这类畸形写入。
    if spec.kind is str and len(value) > MAX_STRING_SETTING_LENGTH:
        raise InvalidSetting(
            f"{spec.key} 不能超过 {MAX_STRING_SETTING_LENGTH} 字符")
    if spec.minimum is not None and value < spec.minimum:
        raise InvalidSetting(f"{spec.key} 不能小于 {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise InvalidSetting(f"{spec.key} 不能大于 {spec.maximum}")
    if spec.key == "alert_webhook_url":
        _validate_webhook_urls(value)
    return value


# 告警 webhook 只允许 http/https：地址由 admin 配置（受信边界），但仍不能是
# `file:` / `javascript:` 等 scheme——它会被后台任务直接 httpx POST。
WEBHOOK_SCHEMES = ("http", "https")
MAX_WEBHOOK_ENTRIES = 16


def _validate_webhook_urls(raw: str) -> None:
    entries = [item.strip() for item in raw.split(",") if item.strip()]
    if len(entries) > MAX_WEBHOOK_ENTRIES:
        raise InvalidSetting(
            f"alert_webhook_url 最多 {MAX_WEBHOOK_ENTRIES} 个地址")
    for entry in entries:
        parsed = urlparse(entry)
        scheme = parsed.scheme.lower()
        if scheme not in WEBHOOK_SCHEMES:
            raise InvalidSetting(
                f"alert_webhook_url 只支持 http/https，收到 {entry!r}")
        # SSRF 防护：地址由 admin 配置（受信边界），但热更成一个内网/回环/
        # 链路本地（云元数据 169.254.169.254）地址后，后台任务会带 JSON
        # POST 打内网。解析不出主机名或落在私网/保留段一律拒绝。
        host = (parsed.hostname or "").strip().lower()
        if not host:
            raise InvalidSetting(f"alert_webhook_url 缺少主机名: {entry!r}")
        if _is_blocked_webhook_host(host):
            raise InvalidSetting(
                f"alert_webhook_url 不允许指向内网/回环/保留地址: {entry!r}")


def _is_blocked_webhook_host(host: str) -> bool:
    """主机名是否指向内网/回环/链路本地/保留地址（SSRF 黑名单）。"""
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False                    # 域名：写入期解析不了，交给投递前复核
    return (address.is_private or address.is_loopback
            or address.is_link_local or address.is_reserved
            or address.is_multicast or address.is_unspecified)


def blocked_webhook_host(url: str) -> bool:
    """webhook URL 在**投递前**是否指向内网/保留地址（含 DNS 解析）。

    写入期 `_is_blocked_webhook_host` 只能判字面量 IP（域名当时解析不了）。
    这里补上解析：`getaddrinfo` 的每个结果都必须过 IP 黑名单，堵住「域名指向
    127.0.0.1 / 169.254.169.254（云元数据）/ 内网 DNS 名」的 SSRF。解析失败
    按阻断处理——宁可漏报一条告警，也不把池子健康详情 POST 进内网。
    """
    host = (urlparse(url).hostname or "").strip().lower()
    if not host or _is_blocked_webhook_host(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    # IPv6 链路本地地址会带 `%en0` 作用域后缀，先剥掉再判
    return any(_is_blocked_webhook_host(info[4][0].split("%", 1)[0])
               for info in infos)


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
        # env 提供的 webhook 地址同样要过校验：否则 .env 里的
        # `ALERT_WEBHOOK_URL=file:///etc/passwd`（或内网地址）会绕过
        # `_coerce`/`_validate_webhook_urls`，只靠 httpx 运行时失败兜底。
        _validate_webhook_urls(str(getattr(base, "alert_webhook_url", "") or ""))

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
        """生效值：DB 覆盖 > env 默认；低于生效下限的存量值钳到 floor。"""
        raw = self._overrides.get(key)
        value = parse_value(key, raw) if raw is not None else getattr(self._base, key)
        spec = HOT_BY_KEY.get(key)
        if spec is not None and spec.floor is not None and value < spec.floor:
            return spec.floor
        return value

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
                # task 为 None 时的分组 key（见 GATEWAY_GROUPS）；前端据此分一级 tab
                "group": spec.group,
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
    def model_fallback_groups(self) -> str:
        return str(self.get("model_fallback_groups"))

    @property
    def model_aliases(self) -> str:
        return str(self.get("model_aliases"))

    @property
    def stream_first_event_timeout_seconds(self) -> float:
        return float(self.get("stream_first_event_timeout_seconds"))

    @property
    def stream_idle_timeout_seconds(self) -> float:
        return float(self.get("stream_idle_timeout_seconds"))

    @property
    def short_cooldown_wait_seconds(self) -> float:
        return float(self.get("short_cooldown_wait_seconds"))

    @property
    def nonstream_keepalive_seconds(self) -> float:
        return float(self.get("nonstream_keepalive_seconds"))

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
    def context_compress_enabled(self) -> bool:
        return bool(self.get("context_compress_enabled"))

    @property
    def prefix_debug_enabled(self) -> bool:
        return bool(self.get("prefix_debug_enabled"))

    @property
    def dump_request_bodies(self) -> bool:
        return bool(self.get("dump_request_bodies"))

    @property
    def gateway_paused(self) -> bool:
        return bool(self.get("gateway_paused"))

    @property
    def gateway_pause_reason(self) -> str:
        return str(self.get("gateway_pause_reason") or "")

    @property
    def context_compress_reserve_tokens(self) -> int:
        return int(self.get("context_compress_reserve_tokens"))

    @property
    def context_compress_min_keep_messages(self) -> int:
        return int(self.get("context_compress_min_keep_messages"))

    @property
    def context_compress_safety_ratio(self) -> float:
        return float(self.get("context_compress_safety_ratio"))

    @property
    def context_compress_sticky_enabled(self) -> bool:
        return bool(self.get("context_compress_sticky_enabled"))

    @property
    def conversation_sticky_seconds(self) -> int:
        return int(self.get("conversation_sticky_seconds"))

    @property
    def max_inflight_per_credential(self) -> int:
        return int(self.get("max_inflight_per_credential"))

    @property
    def growth_irreversible_actions(self) -> bool:
        return bool(self.get("growth_irreversible_actions"))

    @property
    def growth_interval_minutes(self) -> int:
        return int(self.get("growth_interval_minutes"))

    @property
    def refresh_interval_minutes(self) -> int:
        return int(self.get("refresh_interval_minutes"))

    @property
    def quota_probe_minutes(self) -> int:
        return int(self.get("quota_probe_minutes"))

    @property
    def model_catalog_minutes(self) -> int:
        return int(self.get("model_catalog_minutes"))

    @property
    def model_probe_minutes(self) -> int:
        return int(self.get("model_probe_minutes"))

    @property
    def openrouter_catalog_minutes(self) -> int:
        return int(self.get("openrouter_catalog_minutes"))

    @property
    def usd_cny_rate(self) -> float:
        return float(self.get("usd_cny_rate"))

    @property
    def codebuddy_chat_min_interval(self) -> float:
        return float(self.get("codebuddy_chat_min_interval"))

    @property
    def codebuddy_intl_chat_min_interval(self) -> float:
        return float(self.get("codebuddy_intl_chat_min_interval"))

    @property
    def zen_chat_min_interval(self) -> float:
        return float(self.get("zen_chat_min_interval"))

    @property
    def kilo_chat_min_interval(self) -> float:
        return float(self.get("kilo_chat_min_interval"))

    @property
    def qoder_chat_min_interval(self) -> float:
        return float(self.get("qoder_chat_min_interval"))

    @property
    def officeace_chat_min_interval(self) -> float:
        return float(self.get("officeace_chat_min_interval"))

    @property
    def codearts_chat_min_interval(self) -> float:
        return float(self.get("codearts_chat_min_interval"))

    @property
    def codearts_max_concurrency(self) -> int:
        return int(self.get("codearts_max_concurrency"))

    @property
    def codearts_request_window_seconds(self) -> float:
        return float(self.get("codearts_request_window_seconds"))

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

    @property
    def checkin_times(self) -> str:
        return str(self.get("checkin_times"))

    @property
    def checkin_jitter_minutes(self) -> int:
        return int(self.get("checkin_jitter_minutes"))

    @property
    def log_buffer_lines(self) -> int:
        return int(self.get("log_buffer_lines"))

    # ---------------------------------------------------------- 运维告警

    @property
    def alert_enabled(self) -> bool:
        return bool(self.get("alert_enabled"))

    @property
    def alert_webhook_url(self) -> str:
        return str(self.get("alert_webhook_url"))

    @property
    def alert_interval_minutes(self) -> int:
        return int(self.get("alert_interval_minutes"))

    @property
    def alert_silence_minutes(self) -> int:
        return int(self.get("alert_silence_minutes"))

    @property
    def alert_for_ticks(self) -> int:
        return int(self.get("alert_for_ticks"))

    @property
    def alert_clear_ticks(self) -> int:
        return int(self.get("alert_clear_ticks"))

    @property
    def alert_pool_ready_min(self) -> int:
        return int(self.get("alert_pool_ready_min"))

    @property
    def alert_task_failures(self) -> int:
        return int(self.get("alert_task_failures"))

    @property
    def alert_token_expiry_hours(self) -> int:
        return int(self.get("alert_token_expiry_hours"))

    @property
    def alert_error_rate_threshold(self) -> float:
        return float(self.get("alert_error_rate_threshold"))

    @property
    def alert_error_rate_min_requests(self) -> int:
        return int(self.get("alert_error_rate_min_requests"))

    @property
    def alert_error_rate_window_minutes(self) -> int:
        return int(self.get("alert_error_rate_window_minutes"))

    @property
    def alert_key_burst_tokens(self) -> int:
        return int(self.get("alert_key_burst_tokens"))

    @property
    def alert_key_burst_window_minutes(self) -> int:
        return int(self.get("alert_key_burst_window_minutes"))

    @property
    def alert_off_hours_start(self) -> int:
        return int(self.get("alert_off_hours_start"))

    @property
    def alert_off_hours_end(self) -> int:
        return int(self.get("alert_off_hours_end"))

    @property
    def alert_off_hours_min_requests(self) -> int:
        return int(self.get("alert_off_hours_min_requests"))

    @property
    def alert_budget_cny(self) -> float:
        return float(self.get("alert_budget_cny"))

    @property
    def alert_budget_levels(self) -> str:
        return str(self.get("alert_budget_levels"))

    # ---------------------------------------------------------- SQLite 备份

    @property
    def backup_interval_minutes(self) -> int:
        return int(self.get("backup_interval_minutes"))

    @property
    def backup_keep(self) -> int:
        return int(self.get("backup_keep"))

    # ------------------------------------------------------ 上游状态页聚合

    @property
    def upstream_status_enabled(self) -> bool:
        return bool(self.get("upstream_status_enabled"))

    @property
    def upstream_status_minutes(self) -> int:
        return int(self.get("upstream_status_minutes"))

    @property
    def metrics_enabled(self) -> bool:
        return bool(self.get("metrics_enabled"))

    @property
    def request_log_enabled(self) -> bool:
        return bool(self.get("request_log_enabled"))

    # ------------------------------------------------------ reasoning 控制

    @property
    def force_disable_thinking(self) -> bool:
        return bool(self.get("force_disable_thinking"))

    @property
    def reasoning_effort_map(self) -> str:
        return str(self.get("reasoning_effort_map"))

    @property
    def anthropic_thinking_efforts(self) -> str:
        return str(self.get("anthropic_thinking_efforts"))

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
