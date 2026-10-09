/**
 * 健康度四态与配额周期语义的展示规则（全站共用）。
 *
 * 后端把「未探测 / 已耗尽 / 已知剩余百分比」编码为 null / -1 / 0-100，
 * 展示层必须区分它们，否则探测失败会被误读成「没额度」。null 还要再分两种：
 * 免费层（zen / kilo）上游根本没有额度接口，「探也没用」，与「该探但没探到」
 * 是两回事——前者不该提示用户去点「探测」。
 */

import type { Credential, Health, ModelCooldown, ProbeFailureReason } from "../api/types";

export type HealthKind = "known" | "unknown" | "noprobe" | "exhausted";

export interface HealthView {
  kind: HealthKind;
  /** 0-100，仅 known 时有值 */
  percent: number | null;
  label: string;
  tone: "ok" | "warn" | "danger" | "muted";
}

/** 该渠道是否有额度探测：免费层（zen / kilo）上游没有额度接口，探也没用。 */
export function hasQuotaProbe(provider: string): boolean {
  return provider !== "zen" && provider !== "kilo";
}

/**
 * 健康度视图。`provider` 决定 null 落到「未探测」还是「无探测」：
 * 省略时按「未探测」处理（仅用于不关心渠道的旧调用点）。
 */
export function healthView(health: Health, provider?: string): HealthView {
  if (health === null || health === undefined) {
    if (provider !== undefined && !hasQuotaProbe(provider)) {
      return { kind: "noprobe", percent: null, label: "无探测", tone: "muted" };
    }
    return { kind: "unknown", percent: null, label: "未探测", tone: "muted" };
  }
  if (health < 0) {
    return { kind: "exhausted", percent: 0, label: "已耗尽", tone: "danger" };
  }
  const tone = health >= 50 ? "ok" : health > 0 ? "warn" : "danger";
  return { kind: "known", percent: health, label: `${health}%`, tone };
}

/**
 * 周期语义：CodeBuddy 的额度随周期重置，TRAE 是单调递减的账户余额，
 * Zen / Kilo 走免费层、上游没有额度接口（探测恒为「未知」），
 * CodeArts 是**每日积分池**（上游 1000 万 token 折 1000 积分，当日 0 点清零、不累计）。
 *
 * 判定依据是**渠道类型**，不是 quota_cycle_end 是否存在：
 * CodeBuddy 未探测时 cycle_end 同样是 null，而按 cycle_end 推断会把它
 * 错标成 TRAE 的「账户剩余（单调递减）」。
 */
export function quotaSemantics(credential: Credential): string {
  if (credential.provider === "zen" || credential.provider === "kilo") {
    return "免费层（无额度接口）";
  }
  // TRAE 是单调递减的账户余额（上游无周期重置）
  if (credential.provider === "trae") {
    return "账户剩余（单调递减）";
  }
  // CodeArts 是每日 1000 积分池（上游 1000 万 token 折成，1 积分 = 10000 token），
  // 当日 0 点清零——上游不给重置时间戳，固定按时段描述，不能退化成「本周期剩余」。
  if (credential.provider === "codearts") {
    return "每日积分额度（当日 0 点清零）";
  }
  // CodeBuddy / Qoder 随周期重置；未探测到重置时间时退化为本周期口径。
  if (!credential.quota_cycle_end) {
    return "本周期剩余（未探测到重置时间）";
  }
  const date = new Date(credential.quota_cycle_end * 1000).toLocaleDateString("zh-CN");
  return `本周期剩余，${date} 重置`;
}

/**
 * 额度单位统一为「积分」：CodeArts 上游的 token 已在后端折成积分
 * （1 积分 = 10000 token，见 `src/provider/codearts/units.py`），
 * 与 CodeBuddy / TRAE / Qoder 同口径，展示层不再需要按渠道换单位。
 */
export const QUOTA_UNIT = "积分";

/**
 * 到期指标：调度窗口内即将到期的额度（后端与选号排序同源计算）。
 *
 * 返回 null 表示「不值得展示」：渠道没有到期信息（TRAE → 后端回 null），
 * 或窗口关闭 / 确实没有额度临近过期（后端回 0）。只有关键的 0 需要藏起来。
 *
 * 主/次两个窗口共用一个函数：措辞区分开，否则两行同样的句式看不出
 * 谁是第一优先级（主窗口 36h 打平时才轮到次窗口 7 天）。
 *
 * `unit` 默认「积分」，全渠道同口径（CodeArts 的 token 已在后端折成积分）。
 */
export function expiringQuotaLabel(
  credits: number | null | undefined,
  windowSeconds: number | undefined,
  wording: "primary" | "secondary" = "primary",
  unit = "积分",
): string | null {
  if (credits === null || credits === undefined) return null;
  if (credits <= 0 || !windowSeconds || windowSeconds <= 0) return null;
  const amount = formatNumber(credits);
  const window = formatDuration(windowSeconds);
  return wording === "secondary"
    ? `${window}内共 ${amount} ${unit}将过期`
    : `${amount} ${unit}将在 ${window}内过期`;
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
 *
 * **短寿命凭证不标红**：给出 `issuedAt`（签发时间）时，若整段寿命 ≤ 预警
 * 窗口的 2 倍，说明该 token 天生就在窗口附近（CodeArts 的 STS 凭证寿命仅
 * 2h、预警窗口 1h），「即将到期」是它的常态而非异常，恒标红只会淹掉真告警
 * ——与后端 `expiring_tokens` 对短寿命凭证静音同口径。**真过期（剩余 ≤0）
 * 仍标红**：那是刷新失败、需要人工介入的信号。`issuedAt` 缺省（0 / 未传）
 * 时保持原语义（只看剩余时间），避免未知签发时间时误静音。
 */
export function tokenExpiryView(
  expiresAt: number | null | undefined,
  warningSeconds: number,
  now = Date.now() / 1000,
  issuedAt?: number | null,
): TokenExpiryView {
  if (!expiresAt || expiresAt <= 0) {
    return { remaining: null, expiring: false, label: "—" };
  }
  const remaining = Math.max(0, Math.floor(expiresAt - now));
  const lifetime = issuedAt && issuedAt > 0 ? expiresAt - issuedAt : 0;
  // 预警窗口覆盖了整段寿命一半以上 → 命中窗口是常态，不标「即将到期」
  const structurallyShortLived =
    warningSeconds > 0 && lifetime > 0 && lifetime <= warningSeconds * 2;
  const expiring =
    warningSeconds > 0 &&
    remaining <= warningSeconds &&
    (!structurallyShortLived || remaining <= 0);
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
  purged_alerts: "回收告警",
  evaluated: "评估",
  fired: "命中",
  suppressed: "静默",
  delivered: "已推送",
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

/** 运维告警规则的展示名；未知规则回落到原始 key（后端加规则前端不炸）。 */
export const ALERT_RULE_LABEL: Record<string, string> = {
  pool_empty: "凭证池耗尽",
  task_failed: "后台任务连续失败",
  token_expiring: "token 临近到期",
  error_rate: "上游错误率骤升",
};

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("zh-CN");
}

/** 大数紧凑格式（万/亿），用于 token 量级；万以下保持原样。 */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("zh-CN", { notation: "compact", maximumFractionDigits: 1 });
}

/**
 * 缓存命中率：命中 token ÷ 输入 token。
 *
 * 输入为 0 或缓存未上报时返回 `—`——不能算比率，也不该拿 0 冒充「0%」。
 * 命中理论上不会超过输入，超出时按 100% 截断，避免出现 >100% 的怪值。
 */
export function formatCacheRate(
  cached: number | null | undefined,
  input: number | null | undefined,
): string {
  if (cached === null || cached === undefined) return "—";
  if (input === null || input === undefined || input <= 0) return "—";
  const percent = Math.min(100, Math.max(0, (cached / input) * 100));
  return `${percent.toFixed(1)}%`;
}

/**
 * Credit 展示：null 显示 —；推算值（TRAE 按官方单价折算、CodeArts 福利模型按
 * 每日池 1:1 折算）前置
 * ≈ 表示约等于，提醒是估算而非上游回传的真实扣费。
 */
export function formatCredit(
  value: number | null | undefined,
  estimated?: boolean | number | null,
): string {
  if (value === null || value === undefined) return "—";
  const text = formatNumber(Number(value.toFixed(2)));
  return estimated ? `≈${text}` : text;
}

/**
 * 成本金额格式化：null/undefined 显示 —，否则加币种符号（¥ / $）。
 *
 * 成本是估算值且量级很小（单请求常是几厘钱），按绝对值自适应小数位：
 * ≥1 元两位、≥0.01 元四位、更小六位——否则 `toFixed(2)` 会把所有小额抹成 0。
 * `estimated` 为真时前置 ≈，提醒这是刊例价折算而非上游真实扣费。
 */
export function formatMoney(
  value: number | null | undefined,
  currency: "CNY" | "USD" = "CNY",
  estimated = true,
): string {
  if (value === null || value === undefined) return "—";
  const symbol = currency === "CNY" ? "¥" : "$";
  const magnitude = Math.abs(value);
  const digits = magnitude >= 1 ? 2 : magnitude >= 0.01 ? 4 : 6;
  const text = `${symbol}${Number(value.toFixed(digits))}`;
  return estimated ? `≈${text}` : text;
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
    case "cost":
      return `${formatMoney(value, "CNY", false)} (元)`;
    case "ttfb":
    case "latency":
      return formatLatency(value);
    default:
      return `${formatCompact(value)} (次)`;
  }
}

/**
 * Y 轴刻度文案。
 *
 * 为什么不复用 `formatChartValue`：轴宽有限，hover 才需要带单位
 * （`1500万 (tokens)`）。轴刻度直接渲染原始数字时会渲染 `15000000`
 * （8 个字符 ≈ 55px），超出轴宽后被裁掉左侧，看起来像缺了一位。
 *
 * - 计数 / token：紧凑格式（`1500万`），单位交给轴顶的 `METRIC_AXIS_UNIT` 写一次
 * - 成本：保留两位小数（单请求常是零点几元，`formatCompact` 的「万/亿」不适用）
 * - 耗时 / 首字：复用 `formatLatency`，与 hover 完全同口径（`850 ms` / `8.2 s`），
 *   刻度自带单位所以不再另标
 */
export function formatAxisValue(value: number, metric: string): string {
  if (metric === "ttfb" || metric === "latency") {
    return formatLatency(value);
  }
  if (metric === "cost") {
    return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  }
  return formatCompact(value);
}

/**
 * 图表 Y 轴单位文案，写在轴顶（刻度本身不带单位，避免每格重复）。
 *
 * 耗时 / 首字不出现在这里：它们的刻度自带 `ms` / `s`，再标一次是啰嗦。
 */
export const METRIC_AXIS_UNIT: Record<string, string> = {
  requests: "次",
  tokens: "tokens",
  cost: "元",
};

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
  network_unreachable: "连不上渠道服务器，检查本机网络或代理",
  unknown_error: "未知错误",
};

export function probeFailureLabel(reason: ProbeFailureReason | undefined): string {
  return reason ? (PROBE_FAILURE_LABEL[reason] ?? "未知错误") : "未知错误";
}
