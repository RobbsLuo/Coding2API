/**
 * Key 级配额与自助用量（B2/B3 的前端侧）。
 *
 * 为什么单独一个文件而不是塞进 `api/types.ts` / `api/client.ts`：这两个文件
 * 是「所有页面共用」的契约层，改动会牵动其它页面的类型断言；配额属于
 * 「API Key 页自己的扩展」，放在这里由该页显式导入，边界更清楚。
 *
 * 服务端把配额存成 JSON 文本（`api_keys.quota`，`''` = 不限制），列表接口
 * 原样下发；这里负责「文本 ↔ 表单对象」的转换，读路径对脏数据宽容（解析
 * 失败按不限制处理，与服务端 `load_key_quota` 同一方针）。
 */

import { formatCompact, formatMoney, formatNumber } from "./display";

/** 六项配额；0 或缺省 = 该项关闭。与服务端 `src/key_quota.py` 一一对应。 */
export interface KeyQuota {
  daily_requests: number;
  daily_tokens: number;
  daily_cost_usd: number;
  rpm: number;
  tpm: number;
  max_concurrency: number;
}

export const EMPTY_QUOTA: KeyQuota = {
  daily_requests: 0,
  daily_tokens: 0,
  daily_cost_usd: 0,
  rpm: 0,
  tpm: 0,
  max_concurrency: 0,
};

/** 表单里的一项：字段名 + 中文标签 + 单位说明。 */
export const QUOTA_FIELDS: { key: keyof KeyQuota; label: string; hint: string; step?: string }[] = [
  { key: "daily_requests", label: "每日请求数", hint: "0 = 不限制" },
  { key: "daily_tokens", label: "每日 token", hint: "输入+输出，0 = 不限制" },
  { key: "daily_cost_usd", label: "每日成本（USD）", hint: "估算值，0 = 不限制", step: "0.01" },
  { key: "rpm", label: "每分钟请求数", hint: "RPM，0 = 不限制" },
  { key: "tpm", label: "每分钟 token", hint: "TPM（按预估），0 = 不限制" },
  { key: "max_concurrency", label: "并发上限", hint: "在途请求数，0 = 不限制" },
];

/** 当日用量（列表「当日已用」一列）。 */
export interface KeyUsageToday {
  requests: number;
  tokens: number;
  cost_usd: number;
}

const ZERO_USAGE: KeyUsageToday = { requests: 0, tokens: 0, cost_usd: 0 };

function positive(value: unknown): number {
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return 0;
  return numeric;
}

/** DB 文本 / 任意 JSON 值 → 配额对象；解析不了按「不限制」。 */
export function parseQuota(raw: unknown): KeyQuota {
  if (!raw) return { ...EMPTY_QUOTA };
  let data: unknown = raw;
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw);
    } catch {
      return { ...EMPTY_QUOTA };
    }
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return { ...EMPTY_QUOTA };
  const record = data as Record<string, unknown>;
  const quota = { ...EMPTY_QUOTA };
  for (const field of QUOTA_FIELDS) {
    quota[field.key] = positive(record[field.key]);
  }
  return quota;
}

/** 配额对象 → 提交给服务端的形态；全 0 返回 null（= 不带该字段 / 清除配额）。 */
export function quotaInput(quota: KeyQuota): KeyQuota | null {
  return QUOTA_FIELDS.some((field) => quota[field.key] > 0) ? { ...quota } : null;
}

/** 一行摘要（列表「配额」列）；全关 → 「不限制」。 */
export function quotaSummary(quota: KeyQuota): string {
  const parts: string[] = [];
  if (quota.daily_requests) parts.push(`${quota.daily_requests} 次/日`);
  if (quota.daily_tokens) parts.push(`${quota.daily_tokens} token/日`);
  if (quota.daily_cost_usd) parts.push(`$${quota.daily_cost_usd}/日`);
  if (quota.rpm) parts.push(`${quota.rpm} RPM`);
  if (quota.tpm) parts.push(`${quota.tpm} TPM`);
  if (quota.max_concurrency) parts.push(`${quota.max_concurrency} 并发`);
  return parts.join(" · ") || "不限制";
}

/** 列表行里的 `quota` 文本（`types.ts` 的 ApiKey 未声明该列，故走 unknown）。 */
export function quotaOf(row: unknown): KeyQuota {
  if (!row || typeof row !== "object") return { ...EMPTY_QUOTA };
  return parseQuota((row as { quota?: unknown }).quota);
}

/** 列表行里的 `usage_today`（老后端没这字段时按 0 处理）。 */
export function usageTodayOf(row: unknown): KeyUsageToday {
  if (!row || typeof row !== "object") return { ...ZERO_USAGE };
  const raw = (row as { usage_today?: unknown }).usage_today;
  if (!raw || typeof raw !== "object") return { ...ZERO_USAGE };
  const record = raw as Record<string, unknown>;
  return {
    requests: positive(record.requests),
    tokens: positive(record.tokens),
    cost_usd: positive(record.cost_usd),
  };
}

/**
 * 「当日已用」文案：请求数 + token（有成本时附上）。
 *
 * 三个量级差得远，一律过格式化：请求数千分位（`2,974 次`）、token 走紧凑
 * 单位（`3.7亿 token`，原样 `365309724` 会把这一列撑到几百 px）、成本按
 * 绝对值自适应小数位（`$7.49` / 小额 `$0.0042`）。原样透传原始数字会让
 * 表格在窄屏下被这一列挤变形。
 */
export function usageTodayLabel(usage: KeyUsageToday): string {
  if (!usage.requests) return "今日未用";
  const parts = [`${formatNumber(usage.requests)} 次`, `${formatCompact(usage.tokens)} token`];
  if (usage.cost_usd) parts.push(formatMoney(usage.cost_usd, "USD", false));
  return parts.join(" · ");
}

/**
 * 写入一把 Key 的配额（整体替换；全 0 = 清除）。
 *
 * 不复用 `api/client.ts` 的 `request`（未导出）：这里只多发一个自定义头
 * `X-Requested-With`（服务端 CSRF 纵深要求），行为与它一致。
 */
export async function setApiKeyQuota(id: string, quota: KeyQuota): Promise<void> {
  const response = await fetch(`/api/api-keys/${id}/quota`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
    },
    credentials: "same-origin",
    body: JSON.stringify({ quota: quotaInput(quota) ?? {} }),
  });
  if (!response.ok) {
    const text = await response.text();
    let message = response.statusText;
    try {
      message = (JSON.parse(text) as { error?: { message?: string } }).error?.message ?? message;
    } catch {
      // 非 JSON 错误体：保留 statusText
    }
    throw new Error(message);
  }
}
