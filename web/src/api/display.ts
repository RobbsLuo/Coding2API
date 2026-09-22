/**
 * 健康度三态与配额周期语义的展示规则（全站共用）。
 *
 * 后端把「未探测 / 已耗尽 / 已知剩余百分比」编码为 null / -1 / 0-100，
 * 展示层必须区分它们，否则探测失败会被误读成「没额度」。
 */

import type { Credential, Health, ModelCooldown, ProbeFailureReason } from "../api/types";

export type HealthKind = "known" | "unknown" | "exhausted";

export interface HealthView {
  kind: HealthKind;
  /** 0-100，仅 known 时有值 */
  percent: number | null;
  label: string;
  tone: "ok" | "warn" | "danger" | "muted";
}

export function healthView(health: Health): HealthView {
  if (health === null || health === undefined) {
    return { kind: "unknown", percent: null, label: "未探测到额度", tone: "muted" };
  }
  if (health < 0) {
    return { kind: "exhausted", percent: 0, label: "已耗尽", tone: "danger" };
  }
  const tone = health >= 50 ? "ok" : health > 0 ? "warn" : "danger";
  return { kind: "known", percent: health, label: `${health}%`, tone };
}

/**
 * 周期语义：CodeBuddy 的额度随周期重置，TRAE 是单调递减的账户余额。
 * 两者单位都是积分（credit），但重置行为不同，必须标注。
 *
 * 判定依据是**渠道类型**，不是 quota_cycle_end 是否存在：
 * CodeBuddy 未探测时 cycle_end 同样是 null，而按 cycle_end 推断会把它
 * 错标成 TRAE 的「账户剩余（单调递减）」。
 */
export function quotaSemantics(credential: Credential): string {
  if (credential.provider !== "codebuddy") {
    return "账户剩余（单调递减）";
  }
  if (!credential.quota_cycle_end) {
    return "本周期剩余（未探测到重置时间）";
  }
  const date = new Date(credential.quota_cycle_end * 1000).toLocaleDateString("zh-CN");
  return `本周期剩余，${date} 重置`;
}

/**
 * 到期指标：调度窗口内即将到期的积分（后端与选号排序同源计算）。
 *
 * 返回 null 表示「不值得展示」：渠道没有到期信息（TRAE → 后端回 null），
 * 或窗口关闭 / 确实没有积分临近过期（后端回 0）。只有关键的 0 需要藏起来。
 *
 * 主/次两个窗口共用一个函数：措辞区分开，否则两行同样的句式看不出
 * 谁是第一优先级（主窗口 36h 打平时才轮到次窗口 7 天）。
 */
export function expiringQuotaLabel(
  credits: number | null | undefined,
  windowSeconds: number | undefined,
  wording: "primary" | "secondary" = "primary",
): string | null {
  if (credits === null || credits === undefined) return null;
  if (credits <= 0 || !windowSeconds || windowSeconds <= 0) return null;
  const amount = formatNumber(credits);
  const window = formatDuration(windowSeconds);
  return wording === "secondary"
    ? `${window}内共 ${amount} 积分将过期`
    : `${amount} 积分将在 ${window}内过期`;
}

export function cooldownRemaining(coolingUntil: number | null, now = Date.now() / 1000): number {
  if (!coolingUntil) return 0;
  return Math.max(0, Math.ceil(coolingUntil - now));
}

export interface TokenExpiryView {
  /** 剩余秒数；到期时间未知（0）时为 null */
  remaining: number | null;
  /** 是否处于预警窗口（剩余 < 阈值；已过期为 0） */
  expiring: boolean;
  label: string;
}

/**
 * token 到期展示：剩余时间与预警都在前端按同一个时钟现算。
 *
 * 后端只给绝对 `token_expires_at`——服务端算好的「剩余秒数」不会随页面
 * tick 更新，而且阈值判定会与冷却时长各用一套口径。
 *
 * `token_expires_at` 为 0 表示**未知**（渠道没给到期信息）：此时 `remaining`
 * 为 null，展示层显示 `—` 而非「已过期」，否则拿不到到期时间会误报成预警。
 */
export function tokenExpiryView(
  expiresAt: number | null | undefined,
  warningSeconds: number,
  now = Date.now() / 1000,
): TokenExpiryView {
  if (!expiresAt || expiresAt <= 0) {
    return { remaining: null, expiring: false, label: "—" };
  }
  const remaining = Math.max(0, Math.floor(expiresAt - now));
  const expiring = warningSeconds > 0 && remaining <= warningSeconds;
  return { remaining, expiring, label: formatDuration(remaining) };
}

export type CredentialState =
  | "ready"
  | "cooling"
  | "disabled"
  | "off"
  | "exhausted";

export function credentialState(credential: Credential, now = Date.now() / 1000): CredentialState {
  if (credential.disabled) return "disabled";
  if (!credential.enabled) return "off";
  if (cooldownRemaining(credential.cooling_until, now) > 0) return "cooling";
  if (credential.health !== null && credential.health < 0) return "exhausted";
  return "ready";
}

export const STATE_LABEL: Record<CredentialState, string> = {
  ready: "可用",
  cooling: "冷却中",
  disabled: "已禁用",
  // 「已暂停」= enabled=0（管理员软开关）：只摘出对话流量，签到 / token
  // 刷新 / 成长中心 / 额度探测照常跑（实测：tasks 只检查 disabled，不检查
  // enabled）。用「暂停」而非「停用/关闭」，避免被读成整条凭证停摆。
  off: "已暂停",
  exhausted: "额度耗尽",
};

export const STATE_TONE: Record<CredentialState, HealthView["tone"]> = {
  ready: "ok",
  cooling: "warn",
  disabled: "danger",
  off: "muted",
  exhausted: "danger",
};

/** 生效中的模型级冷却（过期条目不上屏）。按剩余时长升序，最紧要的排最前。 */
export function activeModelCooldowns(
  credential: Credential,
  now = Date.now() / 1000,
): ModelCooldown[] {
  return [...(credential.model_cooldowns ?? [])]
    .filter((item) => item.cooling_until > now)
    .sort((left, right) => left.cooling_until - right.cooling_until);
}

/** 模型级冷却的说明：显式写出「其它模型不受影响」，否则用户会以为账号被限。 */
export function modelCooldownLabel(item: ModelCooldown): string {
  return item.reason === "blocked"
    ? "该账号无此模型，已避让"
    : "该模型限流（其它模型不受影响）";
}

export function formatDuration(seconds: number): string {
  if (seconds <= 0) return "—";
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} 小时`;
  return `${(seconds / 86400).toFixed(1)} 天`;
}

export function formatTime(epoch: number | null | undefined): string {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleString("zh-CN", { hour12: false });
}

/**
 * 相对时间（「刚刚 / 12 分钟前 / 3 小时后」），后台任务运行态用。
 *
 * `now` 默认取浏览器时钟；调用方传服务端时钟可避免浏览器偏移把刚跑完的
 * 任务显示成几小时前。未来时间给「后」，未知给破折号。
 */
export function formatAgo(epoch: number | null | undefined, now = Date.now() / 1000): string {
  if (!epoch) return "—";
  const seconds = Math.floor(epoch - now);
  if (Math.abs(seconds) < 10) return "刚刚";
  const label = formatDuration(Math.abs(seconds));
  return seconds > 0 ? `${label}后` : `${label}前`;
}

/** 任务报告字段的展示名；未知字段回落到原始 key（后端加字段前端不炸）。 */
export const TASK_REPORT_LABEL: Record<string, string> = {
  attempted: "尝试",
  succeeded: "成功",
  failed: "失败",
  skipped: "跳过",
  rolled_up: "汇总小时",
  purged: "清理明细",
  expired_coolings: "回收冷却",
  purged_credit_events: "回收流水",
  result: "返回",
};

/** 把 report 字典渲染成一行「成功 3 · 失败 1」；空/未知都不隐藏。 */
export function taskReportLabel(report: Record<string, unknown> | null): string {
  if (!report) return "—";
  const parts = Object.entries(report).map(
    ([key, value]) => `${TASK_REPORT_LABEL[key] ?? key} ${String(value)}`,
  );
  return parts.length ? parts.join(" · ") : "无明细";
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("zh-CN");
}

/** 大数紧凑格式（万/亿），用于 token 量级；万以下保持原样。 */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("zh-CN", { notation: "compact", maximumFractionDigits: 1 });
}

/** 积分变动流水的说明列（B3.4）：只表达归因已知度，不谎称来源。 */
export const CREDIT_SOURCE_LABEL: Record<string, string> = {
  observed: "两次探测间净变化",
  sync: "首次建立基线",
};

/**
 * 一条积分流水的摘要文案。
 *
 * 为什么不说「签到 +5」：上游签到/成长接口不打日志，探测 diff 只能看到区间
 * 净变化，这段区间里可能同时发生签到、成长领取与对话消耗。把净变化写成
 * 某个动作的成果就是拿猜测当事实，所以这里只说「净变化」多少。
 */
export function creditEventLabel(event: {
  delta: number | null;
  before: number | null;
  after: number | null;
  source: string;
}): string {
  if (event.source === "sync") return `基线 ${formatNumber(event.after)}`;
  if (event.delta === null) {
    // 任一端未知：变化无法量化。绝不当成 0，否则「余额变未知」会被读成「没变」
    return `由 ${formatNumber(event.before)} 变为未知`;
  }
  const sign = event.delta > 0 ? "+" : "";
  return `${sign}${formatNumber(event.delta)}（${formatNumber(event.before)} → ${formatNumber(event.after)}）`;
}

/** 时长展示（耗时 / 首字延迟共用）：≥1s 以 s 计（保留 1 位小数、去尾零），否则 ms。 */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms >= 1000) return `${Number((ms / 1000).toFixed(1))} s`;
  return `${formatNumber(ms)} ms`;
}

/** 图表 hover 数值：按指标格式化，单位用 () 包裹（耗时/首字数已含 ms/s）。 */
export function formatChartValue(value: number, metric: string): string {
  switch (metric) {
    case "tokens":
      return `${formatCompact(value)} (tokens)`;
    case "ttfb":
    case "latency":
      return formatLatency(value);
    default:
      return `${formatCompact(value)} (次)`;
  }
}


/** 统计明细的失败类型（线值，来自后端 CONTROLLED_ERROR_TYPES）。
 *  注意与 ProbeFailureReason 不是同一套：那是探测失败，这是请求失败。 */
export type UsageErrorType =
  | "client_disconnect"
  | "credential_unavailable"
  | "invalid_request"
  | "no_healthy_credential"
  | "rate_limit"
  | "upstream_error"
  | "upstream_protocol";

/** 明细失败类型的中文说明（TECHNICAL §6.5：界面只展示稳定枚举的翻译）。 */
export const USAGE_ERROR_LABEL: Record<UsageErrorType, string> = {
  client_disconnect: "客户端中断",
  credential_unavailable: "凭证失效",
  invalid_request: "请求无效",
  no_healthy_credential: "无可用凭证",
  rate_limit: "额度耗尽",
  upstream_error: "渠道错误",
  upstream_protocol: "渠道响应异常",
};

/** 未知取值不得原样透传（TECHNICAL §6.5），兜底为「失败」。 */
export function usageErrorLabel(errorType: string | null | undefined): string {
  if (!errorType) return "失败";
  return USAGE_ERROR_LABEL[errorType as UsageErrorType] ?? `失败（${errorType}）`;
}


export const PROBE_FAILURE_LABEL: Record<ProbeFailureReason, string> = {
  credential_rejected: "凭证被渠道拒绝，需要重新登录该账号",
  rate_limited: "渠道限流，稍后重试",
  upstream_unavailable: "渠道服务异常，与本账号凭证无关",
  upstream_rejected: "渠道拒绝了这次请求",
  upstream_response_invalid: "渠道响应格式与预期不符，可能是官方接口变更",
  upstream_timeout: "渠道响应超时",
  unknown_error: "未知错误",
};

export function probeFailureLabel(reason: ProbeFailureReason | undefined): string {
  return reason ? (PROBE_FAILURE_LABEL[reason] ?? "未知错误") : "未知错误";
}
