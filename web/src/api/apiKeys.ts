/**
 * API Key 页专用的请求函数（列表窗口参数 + 按字段编辑）。
 *
 * 为什么不复用 `api/client.ts` 的 `request`（未导出）：这里要带 `window`
 * 查询参数、编辑要发 PUT 且需要会话 Cookie + 同源 Origin（服务端 CSRF 纵深
 * 校验要求），与 `keyQuota.setApiKeyQuota` 同一写法。放独立文件而不是改
 * `client.ts`，避免与并行改动冲突。
 */

import { ApiError } from "./client";
import type { ApiKey } from "./types";

/** 列表用量窗口：今日 / 近 7 天 / 近 30 天（含今天在内的自然日）。 */
export type ApiKeyWindow = "today" | "7d" | "30d";

/** 三档切换控件与表头的文案（单一来源，避免「近 7 天」两处写法不一致）。 */
export const USAGE_WINDOWS: { value: ApiKeyWindow; label: string }[] = [
  { value: "today", label: "今日" },
  { value: "7d", label: "近 7 天" },
  { value: "30d", label: "近 30 天" },
];

export const USAGE_WINDOW_LABELS: Record<ApiKeyWindow, string> = {
  today: "今日",
  "7d": "近 7 天",
  "30d": "近 30 天",
};

export interface ApiKeyListResponse {
  api_keys: ApiKey[];
  total: number;
  /** 服务端回显的窗口（与请求一致；缺省时按 today 处理）。 */
  window: ApiKeyWindow;
}

/**
 * 可编辑字段：**不含**明文 Key 与 id。
 *
 * 未提供的字段保持原值；传空串（`expires_at` 传 `null`）= 显式清空该项。
 */
export interface ApiKeyPatch {
  name?: string;
  provider_binding?: string;
  allowed_ips?: string;
  allowed_models?: string;
  expires_at?: number | null;
}

/** 非 2xx → `ApiError`（`errorText` 认它，页面才能透出后端受控文案）。 */
async function failWith(response: Response): Promise<never> {
  const text = await response.text();
  let message = response.statusText;
  let code = "http_error";
  try {
    const body = JSON.parse(text) as { error?: { code?: string; message?: string } };
    message = body.error?.message ?? message;
    code = body.error?.code ?? code;
  } catch {
    // 非 JSON 错误体：保留 statusText
  }
  throw new ApiError(response.status, code, message);
}

/** 带会话 Cookie 的同源请求；非 2xx 抛带受控文案的 ApiError。 */
export async function requestApiKeyJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      // 写操作的自定义头：跨站表单无法携带（服务端 CSRF 的第二道防线）
      "X-Requested-With": "XMLHttpRequest",
      ...(init.headers ?? {}),
    },
    credentials: "same-origin",
  });
  if (!response.ok) return failWith(response);
  return (await response.json()) as T;
}

/** 列表（含窗口用量）：`window` 缺省 = 今日，与老调用方兼容。 */
export function listApiKeys(params: {
  sort?: string;
  order?: string;
  limit?: number;
  offset?: number;
  window?: ApiKeyWindow;
} = {}): Promise<ApiKeyListResponse> {
  const query = new URLSearchParams();
  if (params.sort) query.set("sort", params.sort);
  if (params.order) query.set("order", params.order);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  if (params.window) query.set("window", params.window);
  const suffix = query.toString();
  return requestApiKeyJson(`/api/api-keys${suffix ? `?${suffix}` : ""}`);
}

/** 按字段编辑一把 Key 的策略（明文与 id 不可改）。 */
export async function updateApiKey(id: string, patch: ApiKeyPatch): Promise<void> {
  await requestApiKeyJson(`/api/api-keys/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}
