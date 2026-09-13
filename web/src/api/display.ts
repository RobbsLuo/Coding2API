/**
 * 健康度三态与配额周期语义的展示规则（全站共用）。
 *
 * 后端把「未探测 / 已耗尽 / 已知剩余百分比」编码为 null / -1 / 0-100，
 * 展示层必须区分它们，否则探测失败会被误读成「没额度」。
 */

import type { Credential, Health, ProbeFailureReason } from "../api/types";

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
 * 判定依据是**上游类型**，不是 quota_cycle_end 是否存在：
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

export function cooldownRemaining(coolingUntil: number | null, now = Date.now() / 1000): number {
  if (!coolingUntil) return 0;
  return Math.max(0, Math.ceil(coolingUntil - now));
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
  off: "已关闭",
  exhausted: "额度耗尽",
};

export const STATE_TONE: Record<CredentialState, HealthView["tone"]> = {
  ready: "ok",
  cooling: "warn",
  disabled: "danger",
  off: "muted",
  exhausted: "danger",
};

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

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("zh-CN");
}

/** 大数紧凑格式（万/亿），用于 token 量级；万以下保持原样。 */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("zh-CN", { notation: "compact", maximumFractionDigits: 1 });
}

/** 时长展示（耗时 / 首字延迟共用）：≥1s 以 s 计（保留 1 位小数、去尾零），否则 ms。 */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms >= 1000) return `${Number((ms / 1000).toFixed(1))} s`;
  return `${formatNumber(ms)} ms`;
}


export const PROBE_FAILURE_LABEL: Record<ProbeFailureReason, string> = {
  credential_rejected: "凭证被上游拒绝，需要重新登录该账号",
  rate_limited: "上游限流，稍后重试",
  upstream_unavailable: "上游服务异常，与本账号凭证无关",
  upstream_rejected: "上游拒绝了这次请求",
  upstream_response_invalid: "上游响应格式与预期不符，可能是官方接口变更",
  upstream_timeout: "上游响应超时",
  unknown_error: "未知错误",
};

export function probeFailureLabel(reason: ProbeFailureReason | undefined): string {
  return reason ? (PROBE_FAILURE_LABEL[reason] ?? "未知错误") : "未知错误";
}
