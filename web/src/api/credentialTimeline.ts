import { ApiError } from "./client";

/**
 * 凭证可用性时间线的一行（`GET /api/credentials/{id}/timeline`）。
 *
 * 后端只放**脱敏枚举**：`reason` 是上游错误类型（soft / credit / plan /
 * restricted / model / blocked / expired / forced / relogin_required），
 * `detail` 是一行可读短句，**不含凭证明文**。前端不要再拼上游响应正文。
 */
export interface CredentialTimelineEvent {
  id: string;
  credential_id: string;
  /** 迁移发生时刻（epoch 秒） */
  ts: number;
  /** cooldown_start | cooldown_end | hard_failure | revived | enabled | disabled */
  event: string;
  /** 脱敏枚举（不是上游正文） */
  reason: string;
  /** 模型级冷却/负缓存时非空；账号级为 null */
  model: string | null;
  /** 一行可读短句（无凭证明文） */
  detail: string;
  /** 触发这次迁移的请求 ID（后台任务为 null） */
  request_id: string | null;
}

export interface CredentialTimelinePage {
  events: CredentialTimelineEvent[];
  /** 本次实际生效的 limit（已夹到 1..200） */
  limit: number;
  /** 还有更早的行（下一屏用 offset + limit 取） */
  has_more: boolean;
}

/** 事件枚举 → 中文标签。未知取值原样显示（后端加新事件时不至于空白）。 */
export const TIMELINE_EVENT_LABEL: Record<string, string> = {
  cooldown_start: "进入冷却",
  cooldown_end: "冷却结束",
  hard_failure: "硬禁用",
  revived: "人工恢复",
  enabled: "启用",
  disabled: "停用",
};

/** 事件 → 徽标色调：进入冷却=warn、恢复=ok、硬禁用=danger。 */
export const TIMELINE_EVENT_TONE: Record<string, "ok" | "warn" | "danger" | "muted" | "accent"> = {
  cooldown_start: "warn",
  cooldown_end: "ok",
  hard_failure: "danger",
  revived: "accent",
  enabled: "ok",
  disabled: "muted",
};

/**
 * 拉一页时间线（只读，不触发任何上游请求）。
 *
 * `since` / `until` 是 epoch 秒**闭区间**（不传 = 不限）；`limit` 由后端收敛到
 * [1, 200]，`offset` 供翻页。
 */
export async function fetchCredentialTimeline(
  credentialId: string,
  options: { since?: number; until?: number; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<CredentialTimelinePage> {
  const search = new URLSearchParams();
  if (options.since !== undefined) search.set("since", String(options.since));
  if (options.until !== undefined) search.set("until", String(options.until));
  if (options.limit !== undefined) search.set("limit", String(options.limit));
  if (options.offset !== undefined) search.set("offset", String(options.offset));
  const text = search.toString();
  const response = await fetch(
    `/api/credentials/${encodeURIComponent(credentialId)}/timeline${text ? `?${text}` : ""}`,
    { credentials: "same-origin", signal },
  );
  const body = (await response.json()) as unknown;
  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string } } | null)?.error;
    throw new ApiError(response.status, error?.code ?? "unknown",
                       error?.message ?? response.statusText);
  }
  return body as CredentialTimelinePage;
}
