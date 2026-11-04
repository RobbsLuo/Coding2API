"""小浣熊的「签到」与「活跃上报」：都是 GET/POST 幂等探测，没有领取动作。

## 签到（`checkin`）

上游**没有**「点击领取」接口（官方前端 points 客户端只有 `getBalance` /
`getBills`，整包搜不到「签到」）。每日 300 积分由服务端**懒发放**：当天首次
触达积分服务时补写一条流水 `biz_type="daily_grant"`、`points=300`，
`grant_at` 是当天 00:00、`created_at` 才是真正落账时刻（实测 21:41:38，
等于当天第一次用它）。

所以「签到」= 摸一次积分服务（触发懒发放）→ 读流水核对当日是否真到账。
**只认 `daily_grant` 这一行，不看 HTTP 200**——这与 CodeArts 的
「claim 成功但 confirm 失败按失败处理」是同一条纪律。

## 活跃上报（`activity`）

`POST /api/web/auth/v1/devices_current_heartbeat` 是网页端真实的设备心跳
（前端每轮页面加载都发）。只缺 `x-client-*` 头族，补上即 200、幂等。
`user_info` 里的 `last_login_at` 也会随之刷新——这是本渠道唯一可用的
「保持账号活跃」手段。

## 积分套餐明细（`quota_packages`）

`/api/web/points/v1/balance` 把额度分成四个池（每日 / 月度 / 奖励 / 充值），
每个池都是**一个数字**，没有逐笔到期信息。逐笔到期只在流水里：
`daily_grant` 行带 `expire_at`。故明细按「池」展示（与 balance 一一对应），
到期阶梯另由 `expiry_ladder` 从流水推导——两者口径不同，不能混。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..base import CheckinResult

#: 上游业务时区（流水时间戳都是 +08:00，与运行机器时区无关）
CST = timezone(timedelta(hours=8))

#: 判定「本次摸出来的」时间窗：流水 created_at 落在 [调用开始-该窗, 现在] 内
CLAIMED_WINDOW_SECONDS = 90

#: 读多少条流水。日常一天几十条，60 足够覆盖当天那条 daily_grant
BILL_LOOKBACK = 60

#: 每日发放的流水类型
DAILY_GRANT = "daily_grant"
#: 到期清零
DAILY_EXPIRE = "daily_expire"


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


def checkin_result(items: list[Any], started: float, *,
                   now: datetime | None = None) -> CheckinResult:
    """流水 + 调用开始时刻 → 签到结论。

    三态必须能分清，否则用户无法判断「这次摸出来了」还是「今天早些时候
    已经发过」：

    * 今日有发放且落账时间在本次调用窗口内 → `already_checked_in=False`
      （本次触发的懒发放，`credit` 是本轮所得）
    * 今日有发放但落账更早 → `already_checked_in=True`
    * 今日没有发放 → `ok=False`，提示「稍后重试」（上游可能还没跑到补发逻辑）
    """
    current = now or now_cst()
    row = pick_today_grant(items, current)
    if row is None:
        return CheckinResult(
            ok=False, message="上游今日每日积分尚未发放（稍后重试）")
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


def quota_packages(balance: dict[str, Any]) -> list[dict[str, Any]]:
    """`balance.data` → 明细包（四个池各一条）。

    与 `Quota.packages` 的形状一致（`name` / `total` / `used` / `end`）。
    上游每个池只给「当前可用量」一个数字，没有「已用」，故 `used=0`；
    到期信息也不在这里（逐笔到期在流水里，见 `expiry_ladder_from_bills`）。
    数量为 0 的池不出条目（避免面板上挂一排 0）。
    """
    pools = (
        ("每日额度", "daily_points", True),
        ("奖励积分", "reward_points", True),
        ("月度积分", "monthly_points", True),
        # 充值池被冻结时「看得见用不了」：照常列出，但标记不可用
        ("充值积分", "topup_points", not balance.get("topup_frozen")),
    )
    packages: list[dict[str, Any]] = []
    for name, key, usable in pools:
        value = balance.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            continue
        packages.append({"name": name, "total": float(value),
                         "used": 0.0, "end": None, "usable": bool(usable)})
    return packages


def expiry_ladder_from_bills(items: list[Any], now: datetime | None = None,
                             ) -> list[tuple[int, float]] | None:
    """未到期的 `daily_grant` → `[(到期 epoch, 该笔剩余), …]`，按到期升序。

    为什么只认 `daily_grant`：其余流水（充值 / 邀请奖励）的 `expire_at`
    实测缺失或与每日额度混在同一池，混进来会让「快过期先用」的排序失真。
    同一笔的到期清零（`daily_expire`）已消费掉时不再计入——按 `biz_id` 前缀
    对齐（`daily_expire` 的 `biz_id` 形如 `20261103:free:300`，与 grant 的
    数字 id 对不上，故改按「同日 + 同额度」配对，见 `_expired_keys`）。

    没有任何未到期条目时返回 None（而不是空列表）：空列表会被读成「全都过期了」。
    """
    current = now or now_cst()
    stamp = current.timestamp()
    expired = _expired_keys(items)
    ladder: list[tuple[int, float]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("biz_type") != DAILY_GRANT:
            continue
        points = item.get("points")
        if not isinstance(points, (int, float)) or isinstance(points, bool) or points <= 0:
            continue
        expire = parse_iso(item.get("expire_at"))
        if expire is None:
            continue
        epoch = int(expire.timestamp())
        if epoch <= stamp:
            continue                        # 已过期，不算「即将到期」
        if _grant_key(item) in expired:
            continue                        # 已被到期清零消费掉
        ladder.append((epoch, float(points)))
    if not ladder:
        return None
    ladder.sort(key=lambda pair: pair[0])
    return ladder


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