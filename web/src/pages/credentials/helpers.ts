import { probeFailureLabel } from "../../api/display";
import { CHECKIN_PROVIDERS } from "../../api/providers";
import type { BatchAction, BatchResult, Credential, GrowthRunResult } from "../../api/types";

/**
 * 渠道是否支持每日签到。
 *
 * CodeArts 已接入 `/v1/ops/delivery`（此前「上游没有签到接口」的结论
 * 已推翻）。能力清单见 `CHECKIN_PROVIDERS`：CodeBuddy 国际站（`codebuddy_intl`）
 * **不支持**（上游 `/billing/meter/checkin-activity-status` 回 `active: false`
 * 且活动字段全空，后端把该渠道的 `checkin*` 置 None）；Zen / Kilo 无签到接口。
 * 这里同步隐藏按钮与组头汇总，避免给一个必然失败的出口 / 一个永远 0/N 的仪表。
 */
export function supportsCheckin(provider: string): boolean {
  // 能力清单与后端同源（Zen / Kilo 无签到接口、国际站上游无活动），
  // 集中定义在 `api/providers.ts`，组头汇总与行内按钮共用一份判定。
  return CHECKIN_PROVIDERS.has(provider);
}

/**
 * 渠道是否支持活跃上报（手动补发一条对话事件）。
 *
 * 后端按能力探测（`getattr(provider, "activity", None)`），国际站已启用——
 * 活跃上报打的 `/v2/report` 与成长中心的 `/v2/activity/*` 不是同一路径，
 * 实测国际站回 `200 {"code":0}`，故 `intl.py` 不再把 `activity` 置 None。
 * 前端据此不能用 `provider === "codebuddy"` 写死：那会让国际版菜单项消失，
 * 而后端其实可以跑（用户报的正是这个错配）。
 */
export function supportsActivity(provider: Credential["provider"]): boolean {
  // 小浣熊发设备心跳（网页端真实接口），见 provider/raccoon/activity.py
  return provider === "codebuddy" || provider === "codebuddy_intl"
    || provider === "raccoon";
}

/**
 * 渠道是否有「成长中心」这一列。
 *
 * 后端按能力探测（`getattr(provider, "growth", None)`），目前只有 CodeBuddy 有
 * （国际站 `intl.py` 显式 `growth = None`）。与行内菜单项的判定同源，避免
 * 「菜单里没有成长中心、列却常驻一列 —」的错配。
 */
export function supportsGrowth(provider: string): boolean {
  return provider === "codebuddy";
}

/** 套餐到期日：只要日期，时分秒对"哪个包先过期"没用。 */
export function packageExpiry(epoch: number | null): string {
  return epoch === null ? "—" : new Date(epoch * 1000).toLocaleDateString("zh-CN");
}

/**
 * 批量条按钮：后端动作 + 按钮自身文案。
 *
 * 按钮与后端动作**不是一一对应**：`toggle` 要拆成「暂停」（enabled=false）与
 * 「取消暂停」（enabled=true）两个按钮——只给一个按钮的话，操作员把号批量
 * 暂停后就再也没有入口批量放回来（`revive` 清的是 `disabled` 硬禁用，不碰
 * `enabled`，救不了暂停态）。因此按钮有独立 `key` 用于 testid 与文案查表，
 * `action` 才是发给后端的动作名。
 *
 * `verb` 用于按钮；`done` 用于结果句。放一张表而不是散在各处的 if/else：
 * 文案要在按钮与结果汇总两处保持一致，写歪一处就会出现「批量探测」按钮却
 * 弹出「批量签到完成」。
 */
export type BatchButton = {
  key: string;
  action: BatchAction;
  /** 仅 toggle 需要：显式目标状态，不做「各自取反」。 */
  enabled?: boolean;
  verb: string;
  done: string;
};

export const BATCH_BUTTONS: readonly BatchButton[] = [
  { key: "probe", action: "probe", verb: "探测额度", done: "探测完成" },
  { key: "checkin", action: "checkin", verb: "每日签到", done: "签到完成" },
  { key: "pause", action: "toggle", enabled: false, verb: "暂停", done: "已暂停" },
  { key: "resume", action: "toggle", enabled: true, verb: "取消暂停", done: "已取消暂停" },
  // 「恢复」= revive（解除 session 死亡硬禁用），与「取消暂停」是两件事；
  // 单条菜单里已按同样口径区分，批量条保持一致。
  { key: "revive", action: "revive", verb: "恢复", done: "已恢复" },
];

/**
 * 一条批量失败的中文原因。
 *
 * 三种来源的优先级：`error`（没执行到上游：凭证不存在 / 渠道不支持 / 意外异常）
 * → probe 的 `reason`（执行了但上游拒绝，是正常结果）→ checkin 的 `message`。
 * 顺序不能颠倒：probe 失败时 detail.reason 与 error 不会同时存在，但若未来
 * 两者都在，`error` 更接近「这条根本没跑」的事实。
 */
export function batchRowReason(action: BatchAction, row: BatchResult["results"][number]): string {
  if (row.error) {
    if (row.error === "credential not found") return "凭证不存在（可能已删除）";
    if (row.error === "action_failed") return "执行失败，详见服务端日志";
    if (row.error.startsWith("credential does not support")) return "该渠道不支持此操作";
    return row.error;
  }
  if (action === "probe" && row.detail && "reason" in row.detail) {
    return probeFailureLabel(row.detail.reason);
  }
  if (action === "checkin" && row.detail && "message" in row.detail && row.detail.message) {
    return row.detail.message;
  }
  return "失败";
}

/** 成长中心步骤状态的人话标签；idle 不是失败（无事可做），必须与 failed 分开显示。 */
export const GROWTH_STEP_LABEL: Record<GrowthRunResult["steps"][number]["status"], string> = {
  done: "已领取",
  idle: "未执行",
  skipped: "已关闭",
  failed: "失败",
};

export const GROWTH_STEP_TONE: Record<GrowthRunResult["steps"][number]["status"], string> = {
  done: "text-ok",
  idle: "text-muted-foreground",
  skipped: "text-muted-foreground",
  failed: "text-danger",
};

/** 一句话汇报：登录失效要单独提示「重新登录」，不能与普通失败混同。 */
export function growthNotice(result: GrowthRunResult): string {
  if (result.session_dead) return "登录态已失效，需重新登录该渠道";
  return result.report || (result.ok ? "成长中心执行完成" : "成长中心未完全成功");
}
