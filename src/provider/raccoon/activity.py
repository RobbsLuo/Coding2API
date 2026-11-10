"""小浣熊的「签到」与「活跃上报」：都是 GET/POST 幂等探测，没有领取动作。

## 签到（`checkin`）

上游**没有**「点击领取」接口（官方前端 points 客户端只有 `getBalance` /
`getBills`，整包搜不到「签到」）。每日 300 积分由服务端在 00:00 自动发放，
**当天登录过**才会把有效期延长到 7 天；没登录则当日 24:00 清零（官方规则
与实测一致）。

关键：**账单同步有几分钟延迟**。实测 11-06~11-08 的 `daily_grant` 直到
11-09 01:11 才出现在 `bills` 里，凌晨 00:02 核对必然误报。故 `checkin`
不再要求「当天必须出现新 `daily_grant`」：

* 今天有发放 → 按 `created_at` 是否在本次窗口内区分「本次触发」/「早些已发」；
* 今天没有新发放，但存在**未过期**的 `daily_grant` → 视为已签到（积分可用，
  只是今天这笔还没落账/还没延期）；
* 两者都没有 → 失败，但把最早重试时刻推到 `GRANT_GRACE`（01:30），
  不让任务从 00:02 起每 10 分钟空锤。

「只认流水、不看 HTTP 200」的纪律保持不变——余额接口 200 不等于有积分。

流水用**上游的服务端过滤** `type=income`（只留 `daily_grant` /
`reward_grant`）。用 `type=all` 时消费流水会挤占条数窗口：实测 2026-11-10
055920 的 11-09 那笔排在第 62 条，60 条窗口里一条 grant 都看不到，于是
误报「今日尚未发放」（账户里其实还有 521 分可用）。`income` 里每笔
`daily_grant` 自带 `expire_at`，到期判定只需看这个字段。

另外一个实测事实：**换新 token（登录）会立刻补发当天这笔**——`created_at`
是登录后几秒，`grant_at` 回填 00:00。只拿旧 token 访问 `user_info` /
`home` / 心跳不会触发（实测 75s 内余额不动）。故「没发」的号要人工重新
登录拿 token，本服务不代登录。

## 活跃上报（`activity`）

`POST /api/web/auth/v1/devices_current_heartbeat` 是网页端真实的设备心跳
（前端每轮页面加载都发）。只缺 `x-client-*` 头族，补上即 200、幂等。

但**心跳只刷新设备 `last_seen_at`，不算「当天登录」**：实测连续几天只发
心跳，`daily_grant` 仍然在当日 24:00 清零。真正能延期的是网页会话级接口
（`GET /api/web/auth/v1/user_info` 等），所以活跃上报现在先做一次页面访问、
再发心跳。`checkin` 也会先访问一次（best-effort），保证即使活跃任务关闭
也有一次「登录」。

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


def pick_active_grants(items: list[Any], now: datetime) -> list[dict[str, Any]]:
    """所有**未过期、未被清零**的 `daily_grant`，按到期升序。

    已过期（`expire_at <= now`）、缺 `expire_at`、点数非正、以及已被同一天
    同额度的 `daily_expire` 配对的条目都不算。返回列表而非单条：上游允许
    多笔每日积分并存（页面登录延期后跨日保留），判断「今天有没有可用积分」
    要看它们的合计。
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
                   now: datetime | None = None) -> CheckinResult:
    """流水 + 调用开始时刻 → 签到结论。

    四态必须能分清，否则用户无法判断「这次摸出来了」还是「今天早些时候
    已经发过」：

    * 今日有发放且落账时间在本次调用窗口内 → `already_checked_in=False`
      （本次触发/本次首次看到，`credit` 是这笔所得）
    * 今日有发放但落账更早 → `already_checked_in=True`
    * 今日没有新发放，但账户里还有**未过期**的每日积分 → `ok=True` +
      `already_checked_in=True`（积分可用；只是今天这笔还没落账/还没延期）。
      凌晨 00:00~01:30 之间把 `seal_until` 设到 01:30，让任务在账单同步后
      再确认一次今天这笔。
    * 今日没有发放且没有未过期积分 → `ok=False`；凌晨同样把 `retry_at`
      推到 01:30，避免 00:02 起每 10 分钟空锤。
    """
    current = now or now_cst()
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

    active = pick_active_grants(items, current)
    if active:
        total = sum(float(item.get("points") or 0) for item in active)
        nearest = parse_iso(active[0].get("expire_at"))
        suffix = (f"（最早 {nearest.astimezone(CST):%Y-%m-%d} 到期）"
                  if nearest else "")
        grace = grant_grace_at(current)
        seal = int(grace.timestamp()) if current < grace else None
        return CheckinResult(ok=True, credit=total, already_checked_in=True,
                             seal_until=seal,
                             message=f"今日额度可用 {total:g}{suffix}（本次未产生新发放）")

    grace = grant_grace_at(current)
    retry_at = int(grace.timestamp()) if current < grace else None
    return CheckinResult(ok=False, retry_at=retry_at,
                         message="上游今日每日积分尚未发放（稍后重试）")


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