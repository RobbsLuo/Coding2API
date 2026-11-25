import type { ModelCatalogEntry, ModelCatalogResponse, ModelInfo } from "./types";
import { ApiError } from "./client";

/**
 * 模型面的**新增字段与端点**（批次 C）。
 *
 * 刻意单独成文件而不改 `types.ts` / `client.ts`：这两个文件是全站共享契约，
 * 新增模型面字段时先在这里落地，等接口稳定后再并入（届时只需搬移，不必重写）。
 */

/** `/v1/models` 条目的刊例价（USD / 百万 token）。只有三项：上游的 cache_write 我们不采集。 */
export interface ModelCost {
  input: number;
  output: number;
  cache_read: number;
}

/** 后端在 `ModelInfo` 上追加的模型面字段（旧后端不带，全部可选）。 */
export type ModelInfoExtras = ModelInfo & {
  /** 用户自定义别名（`MODEL_ALIASES`）条目：前端据此打「自定义别名」徽章 */
  custom_alias?: boolean;
  /** 别名指向的真实对外 id（排障用） */
  alias_target?: string;
  /** 刊例价；缺价时整个字段缺失（不是 0） */
  cost?: ModelCost;
  /** 生效中的推理档位；未配置时整个字段缺失 */
  reasoning_efforts?: string[];
};

/** 该条目是否用户自定义别名（老后端 / 真实条目恒为 false）。 */
export function isCustomAlias(item: ModelInfo): boolean {
  return (item as ModelInfoExtras).custom_alias === true;
}

/** 条目刊例价；缺失返回 undefined（调用方显示 —，不要当成免费）。 */
export function modelCost(item: ModelInfo): ModelCost | undefined {
  return (item as ModelInfoExtras).cost;
}

/** 条目生效中的推理档位；未配置返回空数组。 */
export function reasoningEfforts(item: ModelInfo): string[] {
  return (item as ModelInfoExtras).reasoning_efforts ?? [];
}

/**
 * 别名徽章的 tooltip：带上目标模型，排障时能直接看出「这个名字是谁的别名」。
 */
export function aliasTitle(item: ModelInfo): string {
  const target = (item as ModelInfoExtras).alias_target;
  return target
    ? `用户自定义别名，指向 ${target}（MODEL_ALIASES）`
    : "用户自定义别名（MODEL_ALIASES）";
}

// ------------------------------------------------- 模型可用性（疑似下线）

/** 一条 (渠道, 模型) 的失败聚合（`GET /api/models/availability`）。 */
export interface ModelAvailabilityRow {
  provider: string;
  model: string;
  /** 窗口内的失败次数 */
  failures: number;
  /** 最后一次失败时刻（epoch 秒） */
  last_failed_at: number;
  /** 是否已被当前 `MODEL_BLOCKLIST` 命中（现算，热更后立即反映） */
  blocked: boolean;
}

export interface ModelAvailabilityResponse {
  window_days: number;
  since: number;
  rows: ModelAvailabilityRow[];
  /** 命中行数超过 limit：前端提示「只看前 N 条」 */
  truncated: boolean;
  /** 计入口径的 error_type（后端下发，前端不硬编码） */
  error_types: string[];
}

/** 目录页每行附带的「疑似下线」信息（与 `ModelAvailabilityRow` 同形，去掉内部匹配键）。 */
export type ModelSuspected = ModelAvailabilityRow;

/** 目录行：OpenRouter 条目 + 疑似下线标记。 */
export type ModelCatalogRow = ModelCatalogEntry & { suspected?: ModelSuspected };

export interface ModelCatalogPageResponse
  extends Omit<ModelCatalogResponse, "models"> {
  models: ModelCatalogRow[];
  /** 疑似下线判定窗口（天），后端钳到 [1, 30] */
  availability_days: number;
}

/** 目录页的请求参数（比后端基础口径多两项筛选）。 */
export interface ModelCatalogParams {
  sort?: string;
  order?: string;
  q?: string;
  onlyAvailable?: boolean;
  onlySuspected?: boolean;
  availabilityDays?: number;
  limit?: number;
  offset?: number;
}

/**
 * 读响应体：非 JSON（反向代理 HTML 错误页、网关截断）时不抛 `SyntaxError`，
 * 否则真实的 HTTP 状态会被解析异常掩盖成「未知错误」。与 `api/client.ts`
 * 的 `readBody` 同口径：`NOT_JSON` 标记「体不是合法 JSON」，`null` 是合法字面量。
 */
const NOT_JSON = Symbol("not-json");

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return NOT_JSON;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "X-Requested-With": "XMLHttpRequest",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
    credentials: "same-origin",
  });
  const body = await readBody(response);
  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string } } | null)?.error;
    throw new ApiError(response.status, error?.code ?? "unknown",
      error?.message ?? response.statusText);
  }
  // 2xx 但不是 JSON：反代/网关的 HTML 错误页、被截断的响应。不能把 `null`
  // 当成功数据返回，不然「请求挂了」会伪装成「没有数据」。
  if (body === NOT_JSON) {
    throw new ApiError(
      response.status,
      "invalid_response",
      "服务端返回了非 JSON 响应（可能是反向代理错误页），请检查服务是否正常。",
    );
  }
  return body as T;
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/** 模型目录（带「疑似下线」筛选）：`GET /api/model-catalog` 的唯一入口。 */
export function fetchModelCatalog(params: ModelCatalogParams = {}): Promise<ModelCatalogPageResponse> {
  return request<ModelCatalogPageResponse>(`/api/model-catalog${query({
    sort: params.sort,
    order: params.order,
    q: params.q,
    only_available: params.onlyAvailable,
    only_suspected: params.onlySuspected,
    availability_days: params.availabilityDays,
    limit: params.limit,
    offset: params.offset,
  })}`);
}

/** 模型可用性只读视图（operator）：最近 N 天按 (渠道, 模型) 的模型级失败。 */
export function fetchModelAvailability(days?: number, limit?: number) {
  return request<ModelAvailabilityResponse>(
    `/api/models/availability${query({ days, limit })}`);
}

// ------------------------------------------------------- 黑名单（一键下线）

/**
 * `MODEL_BLOCKLIST` 文本 → 模式数组（与后端 `blocklist_patterns` 同口径：逗号分隔、
 * 去首尾空白、丢弃空项）。
 */
export function blocklistPatterns(raw: string): string[] {
  return raw.split(",").map((item) => item.trim()).filter((item) => item !== "");
}

/**
 * 把一条模型名并入黑名单文本；已存在（大小写不敏感）则原样返回。
 *
 * 只做「追加一条精确名」：用户要通配可以随后去设置页改。追加而不是覆盖，
 * 否则点一次按钮会静默清掉已有的黑名单（那是安全相关的配置）。
 */
export function withBlocklistPattern(raw: string, pattern: string): string {
  const target = pattern.trim();
  if (target === "") return raw;
  const patterns = blocklistPatterns(raw);
  if (patterns.some((item) => item.toLowerCase() === target.toLowerCase())) {
    return patterns.join(",");
  }
  return [...patterns, target].join(",");
}

// ------------------------------------------------- 主动探活 + 人工接管

/** 探活三态：`ok` 恢复、`failed` 上游明确说没这个模型、其余一律 `inconclusive`。 */
export type ProbeState = "ok" | "failed" | "inconclusive";

/** `GET /api/models/probes` 的一行：探活结论 + 当前负缓存（两者按 (渠道, 模型) 合并）。 */
export interface ModelProbeRow {
  provider: string;
  model: string;
  /** 仍被 blocked 负缓存锁住的号数（0 = 已无负缓存，但可能有人工接管标记） */
  credentials: number;
  /** 负缓存最晚到期时刻（epoch 秒），0 表示当前无负缓存 */
  until: number;
  /** 负缓存命中次数（多号取最大） */
  hits: number;
  /** 最近一次探活结论；从未探过为空串 */
  state: ProbeState | "";
  /** 连续 `failed` 次数（`ok` / `inconclusive` 会归零） */
  failures: number;
  last_probe_at: number;
  last_ok_at: number;
  last_error: string;
  /** `manual` = 人工接管中，自动探活跳过；空串 = 自动 */
  manual: string;
}

export interface ModelProbeResponse {
  rows: ModelProbeRow[];
  /**
   * 模型表当前来自**静态兜底**（非上游真值）的渠道。
   *
   * provider 内部把动态拉取失败转成静态表是「正常返回」，不出这段时页面只能
   * 看到「模型少了几个」，查不到原因（issue #56）。
   */
  degraded: ModelListDegraded[];
}

/** 一个降级渠道：原因 + 连续失败次数（与 `/metrics` 的 gauge 同源）。 */
export interface ModelListDegraded {
  provider: string;
  reason: string;
  failures: number;
}

export interface ProbeResult {
  provider: string;
  model: string;
  state: ProbeState;
  error: string;
}

/** 探活结论 + 当前负缓存（operator）。 */
export function fetchModelProbes(): Promise<ModelProbeResponse> {
  return request<ModelProbeResponse>("/api/models/probes");
}

/** 立即探活一个 (渠道, 模型)：成功即清该渠道的 blocked 负缓存（operator + CSRF）。 */
export function probeModel(provider: string, model: string): Promise<ProbeResult> {
  return request<ProbeResult>("/api/models/probes/probe", {
    method: "POST",
    body: JSON.stringify({ provider, model }),
  });
}

/**
 * 人工接管 / 交还自动探活（operator + CSRF）。
 *
 * 接管不改负缓存：要立刻恢复请用「立即探活」。接管只是让后台别再反复探它。
 */
export function setModelProbeManual(
  provider: string,
  model: string,
  on: boolean,
): Promise<{ provider: string; model: string; manual: boolean }> {
  return request("/api/models/probes/manual", {
    method: "POST",
    body: JSON.stringify({ provider, model, manual: on ? "on" : "off" }),
  });
}

/** 探活状态的中文标签（页面与 toast 共用一套措辞）。 */
export function probeStateLabel(state: ProbeState | ""): string {
  switch (state) {
    case "ok": return "已恢复";
    case "failed": return "确认不可用";
    case "inconclusive": return "无法判定";
    default: return "未探活";
  }
}
