"""CodeBuddy 成长中心编排：把 7 类领取串成一轮，并决定「什么算失败」。

分层原因（勿合并回 growth.py）：growth.py 只认协议（请求 + 解析 + 业务码）；
「名额用完不算失败」「不可逆动作要不要做」「整体成功还是失败」是产品决策，
放在这里，任务层只负责调度与落库。

三条硬纪律（来自原脚本实测教训，勿省）：
1. 每个子步骤独立 try：一段失败不影响其余领取
2. 401/403 立即升级为 session_dead 并停止后续请求（再打只会一路 401）
3. 4xx 业务规则（名额用完、未解锁、任务没做完）不算失败：算了会让定时任务天天报红，
   真正的故障被淹没
"""

from __future__ import annotations

import contextlib
import math
from typing import Any

from ..base import GrowthResult, GrowthStep, StepStatus
from .credential import CodeBuddyCredential
from .events import UpstreamProtocolViolation
from .growth import (
    CodeBuddyGrowth,
    GrowthRejected,
    GrowthTaskItem,
    dig,
    is_tier_locked,
    is_unknown_tier,
    parse_reward,
)

# 连登兑换三档：(档位标识, /redeem/summary 的状态字段前缀, 展示名, 天数)
# tier 传**档位标识** "7d"/"14d"/"28d"；天数只用于兜底重试时退回旧写法。
REDEEM_TIERS: tuple[tuple[str, str, str, int], ...] = (
    ("7d", "starter", "入门", 7),
    ("14d", "advanced", "进阶", 14),
    ("28d", "legendary", "巅峰", 28))

# 接单一次提交多少个 task_code：上游收数组，分批只是为了别把 body 撑大
ACCEPT_BATCH_SIZE = 20

# 每轮最多用掉几张补登卡。卡稀缺（上限 4 张）且该写路径未被真实响应验证过，
# 一轮只花一张：猜错形状也只错一次。
MAKEUP_MAX_PER_RUN = 1


def _is_session_dead(error: Exception) -> bool:
    return isinstance(error, GrowthRejected) and error.status in (401, 403)


def _describe(error: Exception) -> str:
    """把异常变成一句人话（用户看得到，不能是 Python 类名）。"""
    if isinstance(error, GrowthRejected):
        return (f"{error.message}（HTTP {error.status}）" if error.message
                else f"HTTP {error.status}")
    if isinstance(error, UpstreamProtocolViolation):
        return f"响应结构异常（{error}）"
    return f"{type(error).__name__}: {error}"


def _eta(arrive_at: Any, server_now: Any) -> str:
    """把服务端时间戳换算成「还有多久回来」。纯展示：任何异常都返回空串。"""
    try:
        left = float(arrive_at) - float(server_now)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(left):
        return ""
    if left <= 0:
        return "，已到达待领取"
    minutes = int(round(left / 60.0))
    if minutes < 60:
        return f"，约 {max(1, minutes)} 分钟后回"
    return f"，约 {left / 3600.0:.1f} 小时后回"


class GrowthRunner:
    """一轮成长中心。持有不可逆动作开关（抽奖/兑换/开盲盒/补登）。"""

    def __init__(self, client: CodeBuddyGrowth, *, allow_irreversible: bool = True) -> None:
        self._client = client
        self._allow_irreversible = allow_irreversible

    async def run(self, credential: CodeBuddyCredential) -> GrowthResult:
        result = GrowthResult()
        # session_dead 一旦置位就收手：后面每个请求都只会再返回一次 401
        if not await self._travel(credential, result):
            return result
        if not await self._tasks(credential, result):
            return result
        await self._makeup(credential, result)
        await self._redeem(credential, result)
        await self._lottery(credential, result)
        await self._buddy_box(credential, result)
        await self._tail(credential, result)
        # 整体结论：只有「确有需要关注的失败、且一件都没成」才算失败。
        # 部分成功仍是成功——上游某个接口抖动不该让「今天领到 300 积分」变成一张红牌，
        # 那会让定时任务天天报红，真正的故障被淹没（原脚本同此口径）。
        if result.steps and result.failed and not result.gained and not result.session_dead:
            result.ok = False
        result.report = _report(result)
        return result

    # ------------------------------------------------------------ 各子步骤

    async def _travel(self, credential: CodeBuddyCredential,
                      result: GrowthResult) -> bool:
        """1. 领旅行礼物 + 派 Buddy 出发。返回 False 表示必须停止后续步骤。"""
        try:
            status = await self._client.travel_status(credential)
        except Exception as error:  # noqa: BLE001 - 单步失败不中断其余领取
            return self._note_error(result, "查旅行状态", error)
        state = status.state
        claimed = False
        if state == "arrived":
            try:
                credit, _energy = await self._client.claim_travel(credential, status.record_id)
            except Exception as error:  # noqa: BLE001
                return self._note_error(result, "领旅行礼物", error, extra_fail=True)
            got = credit if credit is not None else (status.reward_credit or 0.0)
            self._add_credit(result, got)
            result.steps.append(GrowthStep(
                "领旅行礼物", StepStatus.DONE,
                f"{status.location_name} 带回 {_fmt(got)} 积分", credit=got))
            state = "idle"          # 只有领取成功后才允许派出
            claimed = True
        if state == "idle":
            if status.daily_limit_reached:
                # 服务端明确说名额用完：读它而不是等 depart 报错——后者每轮都白撞一次墙
                result.steps.append(GrowthStep("派 Buddy", StepStatus.IDLE, "今日旅行名额已用完"))
            else:
                await self._depart(credential, result)
        elif state == "traveling":
            result.steps.append(GrowthStep(
                "Buddy 旅行中", StepStatus.IDLE,
                f"{status.location_name}{_eta(status.arrive_at, status.server_now)}"))
        elif state == "arrived" and not claimed:      # pragma: no cover - 领取失败已提前返回
            result.steps.append(GrowthStep("领旅行礼物", StepStatus.FAILED, "未领取"))
        return True

    async def _depart(self, credential: CodeBuddyCredential, result: GrowthResult) -> None:
        try:
            locations = await self._client.travel_locations(credential)
        except Exception as error:  # noqa: BLE001
            self._note_error(result, "查旅行地点", error)
            return
        if not locations:
            result.steps.append(GrowthStep("派 Buddy", StepStatus.IDLE, "无可选目的地"))
            return
        location = locations[0]
        try:
            data = await self._client.depart(credential, location.id)
        except GrowthRejected as error:
            if _is_no_buddy(error):
                # 还没有 Buddy 时派出必然失败——这是账号状态而非故障，而且与其他
                # 17 个任务的 first_buddy 是同一个根因，说一遍就够（不刷屏、不算失败）
                result.steps.append(GrowthStep(
                    "派 Buddy", StepStatus.IDLE, "尚未领取 Buddy（在客户端新建任务并发起对话）",
                    reportable=True))
                return
            self._note_error(result, "派 Buddy", error)
            return
        except Exception as error:  # noqa: BLE001
            self._note_error(result, "派 Buddy", error)
            return
        inner = dig(data, "location")
        inner = inner if isinstance(inner, dict) else {}
        # 时长在 depart 响应里嵌在 location 内层（顶层也兜一下，接口改版方向未知）
        hours = dig(data, "duration_hours") or inner.get("duration_hours")
        # 时长拿不到就不编造数字：宁可只说「已出发」，也不要显示「? 小时后回」
        when = f"（{hours} 小时后回）" if hours else ""
        result.steps.append(GrowthStep(
            "派 Buddy", StepStatus.DONE,
            f"去{inner.get('name') or location.name}{when}"))

    async def _tasks(self, credential: CodeBuddyCredential,
                     result: GrowthResult) -> bool:
        """2. 接单（复数数组）+ 领奖（独立端点）。放在抽奖前：任务送的抽奖机会马上能用上。

        契约（2026-09 桌面端 H5 growthSpace，勿按直觉改）：
        - accept_status 五态：not_accepted | accepted | in_progress | completed | claimed
        - 接单 POST /tasks/accept {"task_codes": [...]}（单数形式一律 400）
        - 领奖 POST /tasks/{code}/claim（不再走 accept）
        """
        try:
            tasks = await self._client.tasks(credential)
        except Exception as error:  # noqa: BLE001
            return self._note_error(result, "查任务列表", error)
        if not await self._accept_pending(credential, result, tasks):
            return False
        return await self._claim_completed(credential, result, tasks)

    async def _accept_pending(self, credential: CodeBuddyCredential, result: GrowthResult,
                              tasks: list[GrowthTaskItem]) -> bool:
        """接单：一批提交多个 task_code，逐条读 results。

        失败要分三类对待，**不是**一律记 FAILED：
        - `prerequisite not met: <code>`：前置任务未完成。这是常态（新账号全部任务都
          被 first_buddy 门住），而且**众多任务共享同一个前置条件**——逐条报会让报告
          变成十几行同样的噪音，掩盖「其实只需做一件事」。按原因归并成一条汇总。
        - `task does not require acceptance`：上游说这个任务不需要接单，是**正常应答**
          而非失败（此前当失败报，误导用户以为出了问题）。
        - 其余：真正需要人看的失败，逐条报出。
        """
        pending = [task.task_code for task in tasks if task.needs_accept and task.task_code]
        titles = {task.task_code: task.title for task in tasks}
        accepted = 0
        blocked: dict[str, list[str]] = {}          # 前置条件 → 受影响的标题
        others: list[tuple[str, str]] = []          # (标题, 上游原文)
        for start in range(0, len(pending), ACCEPT_BATCH_SIZE):
            batch = pending[start:start + ACCEPT_BATCH_SIZE]
            try:
                results = await self._client.accept_tasks(credential, batch)
            except Exception as error:  # noqa: BLE001
                return self._note_error(result, "接单", error)
            for item in results:
                code = item.get("task_code")
                title = titles.get(code, code)
                if item.get("status") != "error":
                    accepted += 1
                    result.steps.append(GrowthStep(
                        "领取任务", StepStatus.DONE, f"「{title}」（进度开始计）"))
                    continue
                message = str(item.get("message") or "未说明原因")
                if _prerequisite_of(message):
                    blocked.setdefault(_prerequisite_of(message) or "", []).append(title)
                elif "does not require acceptance" in message:
                    # 正常应答：该任务不需要接单，不用管它（下一步会照常尝试领奖）
                    continue
                else:
                    others.append((title, message))
        if accepted:
            result.steps.append(GrowthStep(
                "接单完成", StepStatus.DONE, f"共 {accepted} 个任务开始计进度"))
        for reason, titles_blocked in blocked.items():
            # reportable=True：这是用户需要知道并去处理的事，不能被摘要过滤掉
            result.steps.append(GrowthStep(
                "接单受阻", StepStatus.IDLE,
                f"{len(titles_blocked)} 个任务需先完成「{_prerequisite_label(reason)}」"
                f"（在官方客户端操作后自动解除）", reportable=True))
        for title, message in others:
            result.steps.append(GrowthStep(
                "领取任务", StepStatus.FAILED, f"「{title}」失败：{message}"))
        return True

    async def _claim_completed(self, credential: CodeBuddyCredential, result: GrowthResult,
                               tasks: list[GrowthTaskItem]) -> bool:
        """领奖：只有 accept_status == completed 才发，走独立端点。"""
        for task in tasks:
            if not task.needs_claim:
                continue
            try:
                body = await self._client.claim_task(credential, task.task_code)
            except Exception as error:  # noqa: BLE001
                if not self._note_error(result, "领任务奖", error,
                                        detail=f"「{task.title}」"):
                    return False
                continue
            if dig(body, "already_claimed"):
                # 重复领奖不算错，但绝不能重复计分
                result.steps.append(GrowthStep(
                    "领任务奖", StepStatus.IDLE, f"「{task.title}」已领过"))
                continue
            credit, energy = parse_reward(body)
            got = credit if credit is not None else (task.reward_credit or 0.0)
            self._add_credit(result, got)
            extra = f" +{_fmt(energy)} 能量" if energy else ""
            result.steps.append(GrowthStep(
                "领任务奖", StepStatus.DONE,
                f"「{task.title}」+{_fmt(got)} 积分{extra}", credit=got))
        return True

    async def _makeup(self, credential: CodeBuddyCredential,
                      result: GrowthResult) -> None:
        """3. 补登卡：断登自动补一天，保住连登。放在连登兑换之前（补登改变连登天数）。

        查 /streak 是纯只读操作，与「要不要消耗补登卡」无关：开关关闭时只跳过消耗，
        连签天数仍要拿回来。把两者写在一起会让关闭不可逆动作的部署静默丢掉连签展示。
        """
        try:
            streak = await self._client.streak(credential)
        except Exception as error:  # noqa: BLE001
            self._note_error(result, "查连登状态", error)
            return
        result.streak_days = streak.days
        if not self._allow_irreversible:
            result.steps.append(GrowthStep("补登", StepStatus.SKIPPED, "不可逆动作已关闭"))
            return
        if streak.makeup_cards <= 0 or not streak.makeup_dates:
            return
        for target in streak.makeup_dates[:min(streak.makeup_cards, MAKEUP_MAX_PER_RUN)]:
            try:
                left = await self._client.use_makeup_card(credential, target)
            except Exception as error:  # noqa: BLE001
                self._note_error(result, "补登", error)
                return
            result.steps.append(GrowthStep(
                "补登", StepStatus.DONE,
                f"{target}（剩 {left if left is not None else streak.makeup_cards - 1} 张卡）"))
        remaining = len(streak.makeup_dates) - MAKEUP_MAX_PER_RUN
        if remaining > 0 and streak.makeup_cards > MAKEUP_MAX_PER_RUN:
            result.steps.append(GrowthStep(
                "补登", StepStatus.SKIPPED, f"另有 {remaining} 天可补，下轮继续"))

    async def _redeem(self, credential: CodeBuddyCredential,
                      result: GrowthResult) -> None:
        """4. 连登奖励兑换（入门 7 天 / 进阶 14 天 / 巅峰 28 天解锁）。"""
        if not self._allow_irreversible:
            result.steps.append(GrowthStep("连登兑换", StepStatus.SKIPPED, "不可逆动作已关闭"))
            return
        try:
            summary = await self._client.redeem_summary(credential)
        except Exception as error:  # noqa: BLE001
            self._note_error(result, "查连登兑换", error)
            return
        for tier, status_key, label, days in REDEEM_TIERS:
            status = summary.get(status_key)
            # 字段缺失或已领/未解锁一律跳过：接口改版时不该对三档无脑 POST
            if not status or status in ("claimed", "locked"):
                continue
            try:
                credit, energy = await self._client.redeem(credential, tier)
            except GrowthRejected as error:
                # 403「连登天数不足」= 档位未解锁，是常态而非故障；必须先于 session
                # 判定处理：401/403 一律当登录失效会让未解锁档把整轮成长中心误报成
                # 「登录态已失效」并中止。
                if is_tier_locked(error):
                    result.steps.append(GrowthStep(
                        "连登兑换", StepStatus.IDLE, f"「{label}」未解锁（连登天数不足）"))
                    continue
                if is_unknown_tier(error):
                    # 参数校验阶段的 400：服务端没兑换任何东西，退回天数再试是安全的
                    try:
                        credit, energy = await self._client.redeem(credential, days)
                    except Exception as retry_error:  # noqa: BLE001
                        if not self._note_error(result, "连登兑换", retry_error,
                                                detail=f"「{label}」"):
                            return
                        continue
                elif not self._note_error(result, "连登兑换", error, detail=f"「{label}」"):
                    return
                else:
                    continue
            except Exception as error:  # noqa: BLE001
                # 走到这里不可能是 session 失效（GrowthRejected 已被上面的分支接住），
                # 所以记为失败后继续下一档即可
                self._note_error(result, "连登兑换", error, detail=f"「{label}」")
                continue
            self._add_credit(result, credit)
            result.steps.append(GrowthStep(
                "连登兑换", StepStatus.DONE,
                f"「{label}」+{_fmt(credit)} 积分"
                + (f" +{_fmt(energy)} 能量" if energy else ""), credit=credit))

    async def _lottery(self, credential: CodeBuddyCredential,
                       result: GrowthResult) -> None:
        """5. 开盲盒抽奖。一轮只开一次：这条写路径不可逆，剩下的机会留给下一轮。"""
        if not self._allow_irreversible:
            result.steps.append(GrowthStep("开盲盒", StepStatus.SKIPPED, "不可逆动作已关闭"))
            return
        try:
            chances = await self._client.lottery_chances(credential)
        except Exception as error:  # noqa: BLE001
            self._note_error(result, "查抽奖机会", error)
            return
        if chances <= 0:
            return
        try:
            data = await self._client.draw_lottery(credential)
        except Exception as error:  # noqa: BLE001
            self._note_error(result, "开盲盒", error)
            return
        prize = dig(data, "prize_name") or dig(data, "prize")
        # prize 可能是对象/数字：直接拼接会抛 TypeError，把一次已中的奖变成"模块异常"
        text = prize if isinstance(prize, str) else str(prize if prize is not None else "未知")
        if dig(data, "need_address") or dig(data, "require_address"):
            # 实物奖必须由用户自己填收件信息——脚本代填不了也不该代填，但必须提醒
            text += "（实物奖，需到成长中心填写收件信息）"
        result.steps.append(GrowthStep("开盲盒", StepStatus.DONE, text))
        if chances > 1:
            result.steps.append(GrowthStep(
                "开盲盒", StepStatus.SKIPPED, f"还剩 {chances - 1} 次机会，下轮继续"))

    async def _buddy_box(self, credential: CodeBuddyCredential,
                         result: GrowthResult) -> None:
        """6. 能量开 Buddy 盲盒（能量没有其它消耗出口）。"""
        if not self._allow_irreversible:
            result.steps.append(GrowthStep("Buddy 盲盒", StepStatus.SKIPPED, "不可逆动作已关闭"))
            return
        try:
            affordable, _cost, max_open = await self._client.buddy_quota(credential)
        except Exception as error:  # noqa: BLE001
            self._note_error(result, "Buddy 盲盒", error)
            return
        if affordable <= 0:
            return
        count = min(affordable, max_open)
        try:
            data = await self._client.open_buddy(credential, count)
        except Exception as error:  # noqa: BLE001
            self._note_error(result, "Buddy 盲盒", error)
            return
        name = dig(data, "buddy") or dig(data, "name") or dig(data, "buddies")
        result.steps.append(GrowthStep(
            "Buddy 盲盒", StepStatus.DONE,
            f"×{count}（{name if isinstance(name, str) else '新 Buddy'}）"))

    async def _tail(self, credential: CodeBuddyCredential,
                    result: GrowthResult) -> None:
        """7. 能量余额（纯展示；失败不计入失败，不影响结论）。"""
        with contextlib.suppress(Exception):    # 展示字段，失败不能推翻已完成的领取
            result.energy = await self._client.energy(credential)

    # ------------------------------------------------------------ 小工具

    def _add_credit(self, result: GrowthResult, credit: Any) -> None:
        value = credit if isinstance(credit, (int, float)) and not isinstance(credit, bool) else 0
        if value:
            result.credit = (result.credit or 0.0) + float(value)

    def _note_error(self, result: GrowthResult, label: str, error: Exception,
                    *, extra_fail: bool = False, detail: str = "") -> bool:
        """记录一步失败；返回 False 表示必须停止（session 失效）。

        只有 5xx / 协议违规算「需要人关注」的失败。4xx 业务规则（名额用完、未解锁、
        抽奖没次数）是每天的正常状态，计入会让定时任务天天报红。
        """
        if _is_session_dead(error):
            result.session_dead = True
            result.ok = False
            result.report = "登录态已失效，请重新登录"
            result.steps.append(GrowthStep(
                label, StepStatus.FAILED, f"{detail}登录态已失效" if detail else "登录态已失效"))
            return False
        attention = extra_fail or _needs_attention(error)
        text = f"{detail}{_describe(error)}" if detail else _describe(error)
        result.steps.append(GrowthStep(
            label, StepStatus.FAILED if attention else StepStatus.IDLE, text))
        return True


def _needs_attention(error: Exception) -> bool:
    """是否属于「接口坏了 / 网络坏了」这类需要人处理的失败。

    4xx 是业务规则（今天名额用完了、任务没做完、活动没开始），是日常状态；
    把它算成失败会让定时任务天天报红，真正的故障反而被淹没。
    """
    if isinstance(error, GrowthRejected):
        return error.needs_attention
    if isinstance(error, UpstreamProtocolViolation):
        return True          # 结构不符大概率是上游改版，必须让人看到
    return True              # 网络/超时等未知异常按需要关注处理


def _fmt(value: Any) -> str:
    """积分数值展示：整数就不带小数点。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _in_report(step) -> bool:
    """该步是否进一行汇报：显式声明优先，否则 DONE/FAILED 默认进。"""
    if step.reportable is not None:
        return step.reportable
    return step.status in (StepStatus.DONE, StepStatus.FAILED)


def _report(result: GrowthResult) -> str:
    """一行中文汇报（存 events 表 / 直接展示给用户）。"""
    parts = [f"{step.name}：{step.detail}" if step.detail else step.name
             for step in result.steps if _in_report(step)]
    if not parts:
        parts = ["成长中心无可领取项"]
    tail = []
    if result.energy is not None:
        tail.append(f"能量 {result.energy}")
    if result.streak_days is not None:
        # 「连登」是官方术语（H5 规则：按「连续登录且使用」的天数计，含 1 天容忍
        # 窗口，故这个值 = 实际使用天数 + 1）。与签到接口的 streak_days 是两个数
        # （实测同一天 1 vs 5），用「连登」而非「连签」以示区分。
        tail.append(f"连登 {result.streak_days} 天")
    if result.credit:
        tail.append(f"本次 +共 {_fmt(result.credit)} 积分")
    return "；".join(parts) + (f"（{'，'.join(tail)}）" if tail else "")

# 前置条件在 tasks/accept 的 results 里以 "prerequisite not met: <task_code>" 出现。
# 已知的前置任务展示名（接口不给标题时回落成 task_code 本身）。
_PREREQUISITE_LABELS = {"first_buddy": "领取一只 Buddy（在客户端新建任务并发起对话）"}


def _is_no_buddy(error: GrowthRejected) -> bool:
    """派出 Buddy 是否因为「还没有 Buddy」被拒——账号状态，不是故障。

    上游对这种前置缺失回 400 + `no active buddy`。当故障记会让新账号的报告里
    混进一条并不需要处理的"失败"，而它与 17 个任务的 first_buddy 是同一根因。
    """
    return error.status == 400 and "buddy" in error.message.lower()


def _prerequisite_of(message: str) -> str | None:
    """从失败消息里取出前置任务 code；不是前置条件问题则返回 None。"""
    marker = "prerequisite not met:"
    lowered = message.lower()
    if marker not in lowered:
        return None
    return message[lowered.index(marker) + len(marker):].strip().split()[0].strip(".,;") \
        if message[lowered.index(marker) + len(marker):].strip() else ""


def _prerequisite_label(reason: str) -> str:
    """前置条件的可读标签：接口只给 task_code，这里补一句用户该做什么。"""
    return _PREREQUISITE_LABELS.get(reason, reason)

