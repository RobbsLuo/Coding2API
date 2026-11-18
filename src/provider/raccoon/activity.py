"""小浣熊的「签到」与「活跃上报」：都是 GET/POST 幂等探测，没有领取动作。

## 签到（`checkin`）

上游**没有**「点击领取」接口（官方前端 points 客户端只有 `getBalance` /
`getBills`，整包搜不到「签到」）。每日 300 积分在**当天第一次网页登录**时
补发一笔 `biz_type="daily_grant"`；当天没登录就没有这一笔。

**2026-11-18 更正（旧结论有误，勿回退）**：旧版本这里写的是「服务端 00:00
自动发放、当天登录只是把有效期延长到 7 天」，据此把「今天没有新发放但还有
未过期旧积分」判成「已签（积分可用）」。真号复测证明那是假的：

* 三张号的 `daily_grant.created_at` 与上游 `sessions[].login_at` **秒级对齐**
  （09:41:38 登录 → 09:41:40 发放）；当天没有任何登录的号，11-16 ~ 11-18
  一条 `daily_grant` 都没有；
* 光用旧 token 打 `user_info` / `balance` / `tasks` / `model_catalog` / `refresh`
  / 免费模型 chat，连续 67 秒、几十次请求，流水**零变化**；
* 官方规则原文（前端 i18n）：`每日积分每天 00:00 发放，默认当日 24:00 清零，
  当天登录后有效期延长至 7 天` —— 未登录的那天连「当天 24:00 清零」那条
  发放记录都不会有，也就是当天 **0 积分**。

所以签到只有两种真实结论，绝不能拿「账户里还有昨天没花完的」冒充今天已签：

* 今天有 `daily_grant` → 已签（再按 `created_at` 是否在本次窗口内区分
  「本次触发」/「早些已发」）；
* 今天没有 `daily_grant` → **失败**，并明确告诉用户「需在网页重新登录」。
  重试时刻推到 `GRANT_GRACE`（01:30），给账单同步留出延迟窗口。

「只认流水、不看 HTTP 200」的纪律保持不变——余额接口 200 不等于有积分。

## 会话需要重登的信号

上游在 `GET /api/web/auth/v1/user_info` 的 `trusted_device` 里直说会话状态：

```json
{"device_bind_state": "suspicious", "trusted": false,
 "needs_relogin": true, "needs_bind": true, "reason": "device_session_mismatch"}
```

进入这个状态后设备心跳恒回 `400 200811 device_bind_required`，且**当天不会再
有 `daily_grant`**（绑定接口本身也拒绝自愈：实测 `devices_current_bind` 对
`device_session_mismatch` 一律 400）。这正是「网页上重新登录一次才有积分」
的机器可读版本，故 `checkin` / `activity` 都要读它并如实上报。

流水用**上游的服务端过滤** `type=income`（只留 `daily_grant` /
`reward_grant`）。用 `type=all` 时消费流水会挤占条数窗口：实测 2026-11-10
055920 的 11-09 那笔排在第 62 条，60 条窗口里一条 grant 都看不到，于是
误报「今日尚未发放」（账户里其实还有 521 分可用）。`income` 里每笔
`daily_grant` 自带 `expire_at`，到期判定只需看这个字段。

这一课也解释了为什么「重新登录才有积分」：未登录那天上游压根不写发放流水，
只有新会话才会触发补发。

## 活跃上报（`activity`）

`POST /api/web/auth/v1/devices_current_heartbeat` 是网页端真实的设备心跳
（前端每轮页面加载都发）。只缺 `x-client-*` 头族，补上即 200、幂等。

但**心跳只刷新设备 `last_seen_at`，不会补发积分、也不延长有效期**：实测连续
几天只发心跳，`daily_grant` 照样缺席。页面访问（`GET /api/web/auth/v1/user_info`）
同样**不触发补发**——2026-11-18 复测：拿旧 token 打 `user_info` / `balance` /
`tasks` / `model_catalog` / `refresh` / 免费模型 chat 共几十次、67 秒，流水零变化。
补发只发生在**新登录会话**（`created_at` 与 `sessions[].login_at` 秒级对齐）。

故活跃上报保留「先页面访问、再心跳」只为维持网页端真实行为与设备 `last_seen_at`
（以及读出 `trusted_device` 的会话状态），**不要指望它签出积分**——那是签到任务
报「需重新登录」的场景。

## 积分套餐明细（`quota_packages`）

`/api/web/points/v1/balance` 把额度分成四个池（每日 / 月度 / 奖励 / 充值），
每个池都是**一个数字**，没有逐笔到期信息。逐笔只在流水里：
`type=income` 的 `daily_grant` 行带 `expire_at`、`reward_grant` 带发放额。
故明细**按发放流水逐笔展开**（`quota_packages(balance, items)`），每日积分的
池内消耗按「到期早的先消耗」分摊到各笔——上游不提供逐笔剩余，这是推断；
奖励积分直接按发放额列（池值等于各笔之和）。流水拿不到时才退回四个池。
`expiry_ladder` 与明细同源：由明细里每笔的**剩余**（`total - used`）派生，
组头「窗口内到期」与明细、额度合计三者口径一致。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..base import CheckinResult

#: 上游业务时区（流水时间戳都是 +08:00，与运行机器时区无关）
CST = timezone(timedelta(hours=8))

#: 判定「本次摸出来的」时间窗：流水 created_at 落在 [调用开始-该窗, 现在] 内
CLAIMED_WINDOW_SECONDS = 90

#: 读多少条流水。日常一天几十条，60 足够覆盖当天那条 daily_grant。
#: **必须配合 `type=income` 使用**：`all` 里消费流水会挤占窗口（实测
#: 2026-11-10 活跃账号的每日发放排到第 62 条，60 条窗口内一条都不剩）。
#: 只算发放时一条/天，60 条覆盖约两个月。
BILL_LOOKBACK = 60

#: 每日发放的流水类型
DAILY_GRANT = "daily_grant"
#: 奖励发放（邀请 / 活动），无到期
REWARD_GRANT = "reward_grant"
#: 到期清零
DAILY_EXPIRE = "daily_expire"

#: 凌晨判定宽限：账单同步有几分钟延迟；00:00 刚过就判必然误报。
#: 实测 11-06~11-08 的 daily_grant 直到 11-09 01:11 才出现在 bills 里。
GRANT_GRACE_HOUR = 1
GRANT_GRACE_MINUTE = 30


def now_cst() -> datetime:
    """当前时间（+08:00）。单点封装便于测试注入，也让「今天」的判定只在一处。"""
    return datetime.now(CST)


def parse_iso(value: Any) -> datetime | None:
    """ISO8601（带 +08:00 偏移）→ aware datetime；坏值/缺失返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=CST)


def pick_today_grant(items: list[Any], now: datetime) -> dict[str, Any] | None:
    """从流水里挑出「今天发放的每日积分」。

    判据：`biz_type=daily_grant` 且 `points>0` 且 `grant_at`（缺失时回落
    `created_at`）落在**今天**（+08:00）。同一天可能有多条（正常发放 +
    补发），取点数最大的一条。纯函数，便于单测。
    """
    today = now.astimezone(CST).date()
    best: dict[str, Any] | None = None
    for item in items:
        if not isinstance(item, dict) or item.get("biz_type") != DAILY_GRANT:
            continue
        points = item.get("points")
        if not isinstance(points, (int, float)) or isinstance(points, bool) or points <= 0:
            continue
        reference = parse_iso(item.get("grant_at")) or parse_iso(item.get("created_at"))
        if reference is None or reference.astimezone(CST).date() != today:
            continue
        if best is None or points > best.get("points", 0):
            best = item
    return best


def grant_grace_at(now: datetime) -> datetime:
    """当日判定宽限时刻（01:30 +08:00）。"""
    current = now.astimezone(CST)
    return current.replace(hour=GRANT_GRACE_HOUR, minute=GRANT_GRACE_MINUTE,
                           second=0, microsecond=0)


def session_needs_relogin(user_info: dict[str, Any] | None) -> str:
    """`user_info.data` → 「会话需重登」的可操作原因；不需要时返回空串。

    上游 `trusted_device` 是这块唯一可信的信号源（实测 2026-11-18）：

    * `needs_relogin=true` / `trusted=false` / `device_bind_state` ∈
      {`suspicious`, `unbound`} —— 会话与设备对不上，上游拒绝心跳
      （`400 200811 device_bind_required`），当天也不会补发 `daily_grant`；
    * 此时**没有**自愈通道：`devices_current_bind` 对 `device_session_mismatch`
      同样回 400，`refresh` 也只是原样换同一张 token（sid 不变）。只能让用户
      在网页重新登录后重新导入凭据。

    字段缺失（老账号 / 上游改版）时不猜，返回空串走原有判定。
    """
    if not isinstance(user_info, dict):
        return ""
    device = user_info.get("trusted_device")
    if not isinstance(device, dict):
        return ""
    reason = device.get("reason")
    state = device.get("device_bind_state")
    needs = bool(device.get("needs_relogin")) or device.get("trusted") is False \
        or state in ("suspicious", "unbound")
    if not needs:
        return ""
    detail = "、".join(str(item) for item in (state, reason) if item)
    suffix = f"（上游 {detail}）" if detail else ""
    return ("会话已不被上游信任，需在网页端重新登录后重新导入凭据" + suffix)


def pick_active_grants(items: list[Any], now: datetime) -> list[dict[str, Any]]:
    """所有**未过期、未被清零**的 `daily_grant`，按到期升序。

    只给**积分明细**（`quota_packages`）用：账户里还有多少张未到期的每日积分券。
    它**不代表今天签过到**——当天没登录就没有今天这笔（见模块文档），签到判定
    只认 `pick_today_grant`。

    已过期（`expire_at <= now`）、缺 `expire_at`、点数非正、以及已被同一天
    同额度的 `daily_expire` 配对的条目都不算。返回列表而非单条：上游允许多笔
    每日积分并存，明细要逐笔展示。
    """
    current = now.astimezone(CST)
    stamp = current.timestamp()
    expired = _expired_keys(items)
    active: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("biz_type") != DAILY_GRANT:
            continue
        points = item.get("points")
        if not isinstance(points, (int, float)) or isinstance(points, bool) or points <= 0:
            continue
        expire = parse_iso(item.get("expire_at"))
        if expire is None or expire.timestamp() <= stamp:
            continue
        if _grant_key(item) in expired:
            continue                        # 已被到期清零消费掉
        active.append(item)
    active.sort(key=lambda item: parse_iso(item.get("expire_at")).timestamp())
    return active


def checkin_result(items: list[Any], started: float, *,
                   now: datetime | None = None,
                   user_info: dict[str, Any] | None = None) -> CheckinResult:
    """流水 + 调用开始时刻 → 签到结论。

    三态必须能分清，否则用户无法判断「这次摸出来了」「今天早些时候已经发过」
    「今天压根没登录」：

    * 今日有发放且落账时间在本次调用窗口内 → `already_checked_in=False`
      （本次触发，`credit` 是这笔所得）
    * 今日有发放但落账更早 → `already_checked_in=True`
    * 今日没有发放 → **失败**。账户里昨天没花完的积分不算今天签到：上游未登录
      的那天不补发 `daily_grant`（见模块文档）。凌晨 00:00~01:30 之间把
      `retry_at` 推到 01:30，避免在账单同步前空锤；会话已不被信任时直接点名
      「需重新登录」，让人立刻知道该做什么。
    """
    current = now or now_cst()
    relogin = session_needs_relogin(user_info)
    row = pick_today_grant(items, current)
    if row is not None:
        points = float(row.get("points") or 0)
        created = parse_iso(row.get("created_at"))
        fresh = (created is not None
                 and created.timestamp() >= started - CLAIMED_WINDOW_SECONDS)
        expire = parse_iso(row.get("expire_at"))
        suffix = f"（{expire.astimezone(CST):%Y-%m-%d} 到期）" if expire else ""
        if fresh:
            return CheckinResult(ok=True, credit=points, already_checked_in=False,
                                 message=f"每日积分 +{points:g}{suffix}")
        return CheckinResult(ok=True, credit=points, already_checked_in=True,
                             message=f"今日已发放 {points:g}（不是本次触发）{suffix}")

    grace = grant_grace_at(current)
    retry_at = int(grace.timestamp()) if current < grace else None
    if relogin:
        return CheckinResult(ok=False, retry_at=retry_at,
                             message=f"{relogin}；今日每日积分尚未发放")
    return CheckinResult(ok=False, retry_at=retry_at,
                         message="上游今日每日积分尚未发放（需当天登录网页端；稍后重试）")


def quota_packages(balance: dict[str, Any], items: list[Any] | None = None,
                   now: datetime | None = None) -> list[dict[str, Any]]:
    """`balance.data` + 发放流水 → 明细包。

    `items`（`type=income` 的流水）非 None 时**按逐笔发放展开**：每条
    `daily_grant` / `reward_grant` 一行，`end` 是该笔真实到期。上游只给池的
    当前可用量、不给逐笔剩余，故：

    * 每日积分按「到期早的先消耗」把池内消耗分摊到各笔（各笔剩余之和恒等于
      `daily_points`，与顶部额度合计一致）。这是推断而非上游明示——上游的
      流水只有整笔发放，没有单笔扣减记录。
    * 奖励积分实测是「每日额度用光后才扣」：池值等于未到期各笔之和，直接
      按发放额列。

    `items` 为 None（流水取不到）时退化为四个池各一行——老口径，`end` 只能是
    null，明细里看不到各笔到期。
    """
    if items is not None:
        packages = _grant_packages(balance, items, now or now_cst())
        if packages:
            return packages
        # 没有任何发放流水（新号/流水异常）时仍退回池口径，不让明细空白
    pools = (
        ("每日额度", "daily_points", True),
        ("奖励积分", "reward_points", True),
        ("月度积分", "monthly_points", True),
        # 充值池被冻结时「看得见用不了」：照常列出，但标记不可用
        ("充值积分", "topup_points", not balance.get("topup_frozen")),
    )
    packages = []
    for name, key, usable in pools:
        value = balance.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            continue
        packages.append({"name": name, "total": float(value),
                         "used": 0.0, "end": None, "usable": bool(usable)})
    return packages


def _points(value: Any) -> float:
    """非负数值字段 → float；坏值按 0（与 `quota_packages` 的宽容口径一致）。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return 0.0


def _grant_name(prefix: str, item: dict[str, Any]) -> str:
    """`每日积分 11-09` / `奖励积分 11-04 10:02`：同一天可能多笔，时间能把它们区分开。

    每日积分有 `grant_at`（回填到当天 00:00），只取日期；奖励积分没有
    `grant_at`，用 `created_at` 的「日期 + 时分」。
    """
    granted = parse_iso(item.get("grant_at"))
    if granted is not None:
        return f"{prefix} {granted.astimezone(CST):%m-%d}"
    created = parse_iso(item.get("created_at"))
    return (f"{prefix} {created.astimezone(CST):%m-%d %H:%M}"
            if created else prefix)


def _grant_packages(balance: dict[str, Any], items: list[Any],
                    current: datetime) -> list[dict[str, Any]]:
    """逐笔发放 → 明细包；每日积分的消耗按到期先后分摊。"""
    grants = pick_active_grants(items, current)
    rewards = [item for item in items
               if isinstance(item, dict)
               and item.get("biz_type") == REWARD_GRANT
               and _points(item.get("points")) > 0]
    if not grants and not rewards:
        # 一条发放流水都没有（新号 / 流水异常）：交给调用方退回池口径，
        # 否则会渲染出一堆「流水未覆盖」，读起来像上游在欠账。
        return []
    packages: list[dict[str, Any]] = []
    pool_left = _points(balance.get("daily_points"))
    granted = sum(_points(grant.get("points")) for grant in grants)
    remaining_pool = pool_left if pool_left else granted
    consume = max(0.0, granted - remaining_pool)
    for grant in grants:                       # 已按到期升序
        points = _points(grant.get("points"))
        used = min(consume, points)
        consume -= used
        expire = parse_iso(grant.get("expire_at"))
        packages.append({
            "name": _grant_name("每日积分", grant),
            "total": points,
            "used": used,
            "end": int(expire.timestamp()) if expire else None,
            "usable": True,
        })
    leftover = remaining_pool - granted
    if leftover > 0:
        # 池里的可用量比流水能解释的还多（流水同步延迟）：补一行无到期余额，
        # 宁可少一条到期信息，也不要让明细合计与额度合计对不上。
        packages.append({"name": "每日积分（流水未覆盖）", "total": leftover,
                         "used": 0.0, "end": None, "usable": True})
    for item in rewards:
        points = _points(item.get("points"))
        expire = parse_iso(item.get("expire_at"))
        packages.append({
            "name": _grant_name("奖励积分", item),
            "total": points,
            "used": 0.0,
            "end": int(expire.timestamp()) if expire else None,
            "usable": True,
        })
    leftover = _points(balance.get("reward_points")) - sum(
        _points(item.get("points")) for item in rewards)
    if leftover > 0:
        packages.append({"name": "奖励积分（流水未覆盖）", "total": leftover,
                         "used": 0.0, "end": None, "usable": True})
    # 月度 / 充值池没有逐笔流水，仍按池列（数量为 0 不出条目）
    for name, key, usable in (("月度积分", "monthly_points", True),
                              ("充值积分", "topup_points",
                               not balance.get("topup_frozen"))):
        value = _points(balance.get(key))
        if value > 0:
            packages.append({"name": name, "total": value, "used": 0.0,
                             "end": None, "usable": bool(usable)})
    return packages


def ladder_from_packages(packages: list[dict[str, Any]],
                         ) -> list[tuple[int, float]] | None:
    """明细包 → `[(到期 epoch, 剩余), …]`，按到期升序。

    **与明细同源**：剩余取 `total - used`（每日池的消耗已在明细里按到期先后
    分摊过），否则阶梯会比明细和额度合计还大——组头「窗口内到期」正是拿它算的。

    无到期信息的包（奖励 / 月度 / 充值）跳过；没有可用的返回 None
    （而不是空列表：空列表会被读成「全都过期了」）。
    """
    ladder: list[tuple[int, float]] = []
    for item in packages:
        end = item.get("end")
        left = float(item.get("total") or 0) - float(item.get("used") or 0)
        if not isinstance(end, int) or isinstance(end, bool) or left <= 0:
            continue
        ladder.append((end, left))
    ladder.sort(key=lambda pair: pair[0])
    return ladder or None


def _grant_key(item: dict[str, Any]) -> tuple[str, float]:
    """发放条目的配对键：`grant_at` 的日期 + 点数。

    `daily_expire` 的 `biz_id`（`20261103:free:300`）与 `daily_grant` 的
    `biz_id`（纯数字）不是同一套 id，故只能按「哪天的多少点」配对。
    """
    grant = parse_iso(item.get("grant_at")) or parse_iso(item.get("created_at"))
    day = grant.astimezone(CST).date().isoformat() if grant else ""
    return (day, float(item.get("points") or 0))


def _expired_keys(items: list[Any]) -> set[tuple[str, float]]:
    keys: set[tuple[str, float]] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("biz_type") != DAILY_EXPIRE:
            continue
        points = item.get("points")
        if not isinstance(points, (int, float)) or isinstance(points, bool):
            continue
        keys.add(_grant_key({**item, "points": abs(points)}))
    return keys


#: 会话不被信任时给用户的一步动作。上游没有自愈通道（绑定接口同样 400、
#: refresh 只换同一张 token），所以文案必须指向「重新登录 + 重新导入」。
RELOGIN_HINT = "需在网页端重新登录后重新导入凭据"

#: 心跳被拒时的业务码：`{"code":200811,"message":"device_bind_required"}`。
#: 实测只在 `trusted_device.device_bind_state=suspicious`（如
#: `device_session_mismatch`）时出现，与 token 是否过期无关。
DEVICE_BIND_REQUIRED_CODE = "200811"
_DEVICE_BIND_MARKERS = ("device_bind_required", "trusted device binding")


def heartbeat_needs_relogin(body: bytes | str | None) -> bool:
    """心跳失败响应体 → 是否属于「会话需重登」。"""
    if not body:
        return False
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    lowered = text.lower()
    return DEVICE_BIND_REQUIRED_CODE in text or any(
        marker in lowered for marker in _DEVICE_BIND_MARKERS)


def heartbeat_headers(device_id: str) -> dict[str, str]:
    """网页端心跳的头族。

    `x-client-device-id` 必填（缺了回 `100002 params_invalid_error` /
    `param client headers required invalid`）。`device_id` 由凭证派生
    （见 `credential.derive_device_id`），保证同一账号永远是同一台「设备」。
    """
    return {
        "x-client-device-id": device_id,
        "x-client-platform": "web",
        "x-client-version": "156.0.0.0",
        "x-raccoon-language": "zh",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }