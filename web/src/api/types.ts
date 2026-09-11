/**
 * 后端 API 契约（并行开发的唯一事实来源）。
 *
 * 后端已完成并测试通过（484 测试 / 100% 覆盖），本文件与 src/main.py 的路由一一对应。
 * 各页面只依赖本文件，不要各自猜测字段名。
 */

export type Provider = "codebuddy" | "trae";

/** 健康度三态：null = 未探测；-1 = 已耗尽；0-100 = 已知剩余百分比。 */
export type Health = number | null;

export interface Credential {
  id: string;
  provider: Provider;
  nickname: string;
  /** 用户软开关 */
  enabled: 0 | 1;
  /** session 死亡硬禁用 */
  disabled: 0 | 1;
  disabled_reason: string | null;
  /** 手动指定（全局唯一一条为 1） */
  pinned: 0 | 1;
  health: Health;
  cooling_until: number | null;
  err_count: number;
  quota_remaining: number | null;
  quota_total: number | null;
  /** 仅 CodeBuddy 有周期；TRAE 为 null */
  quota_cycle_end: number | null;
  quota_probed_at: number | null;
  created_at: number;
  added_by: string | null;
}

export interface ApiKey {
  id: string;
  username: string;
  name: string;
  /** 脱敏预览，形如 sk-…Ab3d */
  preview: string;
  created_at: number;
  last_used_at: number | null;
}

/** 创建成功时才有明文（只返回一次） */
export interface ApiKeyCreated extends ApiKey {
  api_key: string;
}

export interface StatsOverview {
  requests: number;
  ok_count: number;
  success_rate: number | null;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  /** 上游可选字段，两边都经常为 null */
  credit: number | null;
  avg_latency_ms: number | null;
  avg_ttfb_ms: number | null;
}

export interface ProviderStats {
  provider: Provider;
  requests: number;
  ok_count: number;
  input_tokens: number;
  output_tokens: number;
  credit: number | null;
}

export interface SessionInfo {
  username: string;
  is_admin: boolean;
}

export interface CredentialsResponse {
  credentials: Credential[];
  viewer: string;
  is_admin: boolean;
}

export interface UpstreamAuthStart {
  flow: "poll" | "callback";
  state: string;
  auth_url: string | null;
  interval: number | null;
}

export interface UpstreamAuthPoll {
  status: "pending" | "success";
  credential_id?: string;
}

/** 后端保证是稳定的机器可读枚举，不是 Python 异常类名。 */
export type ProbeFailureReason =
  | "credential_rejected"
  | "rate_limited"
  | "upstream_unavailable"
  | "upstream_rejected"
  | "upstream_response_invalid"
  | "upstream_timeout"
  | "unknown_error";

export interface ProbeResult {
  probed: boolean;
  remaining?: number;
  total?: number;
  reason?: ProbeFailureReason;
  /** 原始错误摘要，仅用于排查；界面不应直接展示给普通用户 */
  detail?: string;
}

export interface ModelInfo {
  id: string;
  object: string;
  owned_by: string;
  providers: Provider[];
}
