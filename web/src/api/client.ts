import type {
  ApiKey,
  ApiKeyCreated,
  CheckinResult,
  CheckinStatus,
  CredentialsResponse,
  CreditEvent,
  GrowthEvent,
  GrowthRunResult,
  ActivityReportResult,
  ModelInfo,
  ProbeResult,
  ProviderStats,
  SessionInfo,
  StatsMetric,
  StatsOverview,
  TimelinePoint,
  ModelTimelineResponse,
  StatsEventsResponse,
  SettingsResponse,
  UpstreamAuthPoll,
  UpstreamAuthStart,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      // CSRF 纵深（服务端要求写请求带自定义头，PROPOSAL §8）：
      // 浏览器跨站表单/简单请求无法携带自定义头
      "X-Requested-With": "XMLHttpRequest",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
    credentials: "same-origin",
  });

  const text = await response.text();
  const body = text ? (JSON.parse(text) as unknown) : null;

  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string } } | null)?.error;
    throw new ApiError(response.status, error?.code ?? "unknown", error?.message ?? response.statusText);
  }
  return body as T;
}

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) });

export const api = {
  // ---------------------------------------------------------------- 会话
  login: (username: string, password: string) =>
    request<SessionInfo>("/api/auth/login", { method: "POST", ...json({ username, password }) }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  session: () => request<SessionInfo>("/api/auth/session"),

  // ---------------------------------------------------------------- 凭证
  credentials: () => request<CredentialsResponse>("/api/credentials"),
  importCredential: (provider: string, credential: unknown, nickname = "") =>
    request<{ id: string }>("/api/credentials", {
      method: "POST",
      ...json({ provider, credential, nickname }),
    }),
  reviveCredential: (id: string) =>
    request<{ ok: boolean }>(`/api/credentials/${id}/revive`, { method: "POST" }),
  toggleCredential: (id: string, enabled: boolean) =>
    request<{ ok: boolean }>(`/api/credentials/${id}/toggle`, { method: "POST", ...json({ enabled }) }),
  pinCredential: (id: string | null) =>
    request<{ ok: boolean }>("/api/credentials/pin", { method: "POST", ...json({ credential_id: id }) }),
  deleteCredential: (id: string) =>
    request<{ ok: boolean }>(`/api/credentials/${id}`, { method: "DELETE" }),
  probeCredential: (id: string) =>
    request<ProbeResult>(`/api/credentials/${id}/probe`, { method: "POST" }),
  checkinCredential: (id: string) =>
    request<CheckinResult>(`/api/credentials/${id}/checkin`, { method: "POST" }),
  checkinStatus: (id: string) =>
    request<{ status: CheckinStatus }>(`/api/credentials/${id}/checkin`),
  runGrowth: (id: string) =>
    request<GrowthRunResult>(`/api/credentials/${id}/growth`, { method: "POST" }),
  growthHistory: (id: string) =>
    request<{ events: GrowthEvent[] }>(`/api/credentials/${id}/growth`),
  creditEvents: (id: string) =>
    request<{ events: CreditEvent[] }>(`/api/credentials/${id}/credit-events`),
  reportActivity: (id: string) =>
    request<ActivityReportResult>(`/api/credentials/${id}/activity`, { method: "POST" }),

  // ------------------------------------------------------------ 渠道登录
  upstreamStart: (provider: string) =>
    request<UpstreamAuthStart>("/api/auth/upstream/start", { method: "POST", ...json({ provider }) }),
  upstreamPoll: (provider: string, state: string) =>
    request<UpstreamAuthPoll>("/api/auth/upstream/poll", {
      method: "POST",
      ...json({ provider, state }),
    }),
  upstreamCancel: (provider: string, state: string) =>
    request<{ cancelled: boolean }>("/api/auth/upstream/cancel", {
      method: "POST",
      ...json({ provider, state }),
    }),

  // ------------------------------------------------------------- API Key
  apiKeys: () => request<{ api_keys: ApiKey[] }>("/api/api-keys"),
  createApiKey: (name: string) =>
    request<ApiKeyCreated>("/api/api-keys", { method: "POST", ...json({ name }) }),
  deleteApiKey: (id: string) => request<{ ok: boolean }>(`/api/api-keys/${id}`, { method: "DELETE" }),

  // ------------------------------------------------------------ 运行时配置
  settings: () => request<SettingsResponse>("/api/settings"),
  updateSettings: (values: Record<string, unknown>) =>
    request<SettingsResponse>("/api/settings", { method: "PUT", ...json({ values }) }),

  // ---------------------------------------------------------------- 统计
  statsOverview: (username?: string, since?: number) =>
    request<StatsOverview>(`/api/stats/overview${query({ username, since })}`),
  statsByProvider: (username?: string, since?: number) =>
    request<{ providers: ProviderStats[] }>(`/api/stats/by-provider${query({ username, since })}`),
  statsTimeline: (username?: string, since?: number, metric?: StatsMetric) =>
    request<{ points: TimelinePoint[] }>(`/api/stats/timeline${query({ username, since, metric })}`),
  statsModelTimeline: (username?: string, since?: number, metric?: StatsMetric) =>
    request<ModelTimelineResponse>(`/api/stats/model-timeline${query({ username, since, metric })}`),
  statsEvents: (username?: string, since?: number, before?: number, limit?: number) =>
    request<StatsEventsResponse>(`/api/stats/events${query({ username, since, before, limit })}`),

  // ---------------------------------------------------------------- 模型
  models: (apiKey: string) =>
    request<{ object: string; data: ModelInfo[] }>("/v1/models", {
      headers: { Authorization: `Bearer ${apiKey}` },
    }),

  // ------------------------------------------------------------ Playground
  chatCompletion: (apiKey: string, body: unknown) =>
    fetch("/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(body),
    }),
};

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}
