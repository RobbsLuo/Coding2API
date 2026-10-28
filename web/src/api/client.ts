import type {
  ApiKey,
  ApiKeyCreated,
  ActivationDescribe,
  ActivationIssued,
  AlertsResponse,
  AuditResponse,
  BatchAction,
  BatchImportEntry,
  BatchImportResult,
  BatchResult,
  CheckinResult,
  CheckinStatus,
  CredentialLimits,
  CredentialLimitsInput,
  CredentialsResponse,
  CreditEvent,
  GrowthEvent,
  GrowthRunResult,
  ActivityReportResult,
  ModelCatalogResponse,
  ModelInfo,
  ModelStats,
  CredentialStats,
  ProbeResult,
  ProviderStats,
  Role,
  SessionInfo,
  StatsMetric,
  StatsOverview,
  TimelinePoint,
  TosRisk,
  BillingReport,
  ModelTimelineResponse,
  PercentileTimelineResponse,
  NavBadges,
  StatsEventsResponse,
  StatsEventFilters,
  CsvExport,
  SettingsResponse,
  TasksResponse,
  TaskRunResult,
  UpstreamAuthPoll,
  UpstreamAuthStart,
  UpstreamStatusResponse,
  UserStats,
  UserRow,
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
      // CSRF 纵深（服务端要求写请求带自定义头，PROPOSAL §6）：
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
  /** 自助改密：成功后后端换发 Cookie（epoch 已变），无需再登录。 */
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ ok: boolean }>("/api/auth/password", {
      method: "POST",
      ...json({ current_password: currentPassword, new_password: newPassword }),
    }),

  // ------------------------------------------------------------ 账号激活
  /** 校验一次性令牌，返回待激活用户名（无需登录）。 */
  activation: (token: string) =>
    request<ActivationDescribe>(`/api/auth/activate${query({ token })}`),
  /** 用令牌自设密码完成激活。 */
  activate: (token: string, password: string) =>
    request<{ ok: boolean; username: string }>("/api/auth/activate", {
      method: "POST",
      ...json({ token, password }),
    }),

  // ------------------------------------------------------------ 用户管理
  users: (sort?: string, order?: string, limit?: number, offset?: number) =>
    request<{ users: UserRow[]; total: number }>(
      `/api/users${query({ sort, order, limit, offset })}`),
  createUser: (username: string, role: Role) =>
    request<ActivationIssued>("/api/users", { method: "POST", ...json({ username, role }) }),
  updateUserRole: (username: string, role: Role) =>
    request<{ ok: boolean }>(`/api/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      ...json({ role }),
    }),
  disableUser: (username: string) =>
    request<{ ok: boolean }>(`/api/users/${encodeURIComponent(username)}/disable`, {
      method: "POST",
    }),
  enableUser: (username: string) =>
    request<{ ok: boolean }>(`/api/users/${encodeURIComponent(username)}/enable`, {
      method: "POST",
    }),
  resetUserPassword: (username: string) =>
    request<ActivationIssued>(
      `/api/users/${encodeURIComponent(username)}/reset-password`,
      { method: "POST" },
    ),
  /** 用户级策略（Playground）：模型白名单 + 配额。传空串 = 不限制。 */
  setUserPolicy: (username: string, allowedModels: string, quota: string) =>
    request<{ ok: boolean }>(`/api/users/${encodeURIComponent(username)}/policy`, {
      method: "PUT",
      ...json({ allowed_models: allowedModels, quota }),
    }),

  // ---------------------------------------------------------------- 审计
  audit: (filters: { actor?: string; action?: string; limit?: number; offset?: number;
                     sort?: string; order?: string } = {}) =>
    request<AuditResponse>(`/api/audit${query(filters)}`),
  // ---------------------------------------------------------------- 凭证
  credentials: (sort?: string, order?: string) =>
    request<CredentialsResponse>(`/api/credentials${query({ sort, order })}`),
  billing: (month?: string) =>
    request<BillingReport>(`/api/billing${query({ month })}`),
  /** 账单导出：返回原始响应，页面自己落盘（CSV 有 BOM，JSONL 逐行）。 */
  billingExport: async (month: string, format: "csv" | "jsonl") => {
    const response = await fetch(
      `/api/billing${query({ month, format })}`, { credentials: "same-origin" });
    if (!response.ok) {
      let message = `导出失败（HTTP ${response.status}）`;
      try {
        const body = await response.json();
        if (body?.error?.message) message = body.error.message;
      } catch {
        // 非 JSON 错误体：保留 HTTP 状态提示
      }
      throw new ApiError(response.status, "export_failed", message);
    }
    return { blob: await response.blob(), rowCount: response.headers.get("X-Export-Row-Count") };
  },
  importCredential: (provider: string, credential: unknown, nickname = "") =>
    request<{ id: string; tos: TosRisk }>("/api/credentials", {
      method: "POST",
      ...json({ provider, credential, nickname }),
    }),
  /** 批量导入：逐条校验，坏条目只进 failed、不连累其他条目。 */
  batchImportCredentials: (items: BatchImportEntry[]) =>
    request<BatchImportResult>("/api/credentials/batch-import", {
      method: "POST",
      ...json({ items }),
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
  /** 设置 / 清除凭证限额护栏（整体替换；全关时后端删行）。 */
  setCredentialLimits: (id: string, limits: CredentialLimitsInput) =>
    request<{ limits: CredentialLimits | null }>(`/api/credentials/${id}/limits`, {
      method: "PUT",
      ...json(limits),
    }),
  /** 整体替换一条凭证的标签（空数组 = 清空）。 */
  setCredentialTags: (id: string, tags: string[]) =>
    request<{ tags: string[] }>(`/api/credentials/${id}/tags`, {
      method: "PUT",
      ...json({ tags }),
    }),
  /** 批量运维：一次对多条凭证执行同一动作，后端逐条隔离返回结果。 */
  batchCredentials: (action: BatchAction, ids: string[], enabled?: boolean) =>
    request<BatchResult>("/api/credentials/batch", {
      method: "POST",
      ...json(enabled === undefined ? { action, ids } : { action, ids, enabled }),
    }),
  checkinCredential: (id: string) =>
    request<CheckinResult>(`/api/credentials/${id}/checkin`, { method: "POST" }),
  checkinStatus: (id: string) =>
    request<{ status: CheckinStatus }>(`/api/credentials/${id}/checkin`),
  runGrowth: (id: string) =>
    request<GrowthRunResult>(`/api/credentials/${id}/growth`, { method: "POST" }),
  growthHistory: (id: string) =>
    request<{ events: GrowthEvent[] }>(`/api/credentials/${id}/growth`),
  creditEvents: (id: string, ledger?: string, sort?: string, order?: string) =>
    request<{ events: CreditEvent[] }>(
      `/api/credentials/${id}/credit-events${query({ ledger, sort, order })}`),
  reportActivity: (id: string) =>
    request<ActivityReportResult>(`/api/credentials/${id}/activity`, { method: "POST" }),

  // ------------------------------------------------------------ 渠道登录
  /** 发起渠道登录。`forceLogin`：先登出上游，用于新增另一个账号（避免云端
   *  直接复用浏览器里现有的会话，把「新增」变成「同一个账号再登一遍」）。 */
  upstreamStart: (provider: string, forceLogin = false) =>
    request<UpstreamAuthStart>("/api/auth/upstream/start", {
      method: "POST",
      ...json({ provider, force_login: forceLogin }),
    }),
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
  upstreamComplete: (provider: string, state: string, url: string) =>
    request<{ status: "success"; credential_id: string }>("/api/auth/upstream/complete", {
      method: "POST",
      ...json({ provider, state, url }),
    }),

  // ------------------------------------------------------------- API Key
  apiKeys: (sort?: string, order?: string, limit?: number, offset?: number) =>
    request<{ api_keys: ApiKey[]; total: number }>(
      `/api/api-keys${query({ sort, order, limit, offset })}`),
  createApiKey: (payload: {
    name: string;
    provider_binding?: string;
    allowed_ips?: string;
    allowed_models?: string;
    expires_at?: number | null;
  }) =>
    request<ApiKeyCreated>("/api/api-keys", { method: "POST", ...json(payload) }),
  deleteApiKey: (id: string) => request<{ ok: boolean }>(`/api/api-keys/${id}`, { method: "DELETE" }),
  /**
   * 会话鉴权的模型列表（与 Playground 同源 `/api/playground/models`）：
   * 供「API Key 管理」页的 CC Switch 默认模型下拉使用，无需用户自己造 Key。
   */
  playgroundModels: (signal?: AbortSignal) =>
    request<{ data: ModelInfo[] }>("/api/playground/models", { signal }),

  // ------------------------------------------------------------ 运行时配置
  settings: () => request<SettingsResponse>("/api/settings"),
  updateSettings: (values: Record<string, unknown>) =>
    request<SettingsResponse>("/api/settings", { method: "PUT", ...json({ values }) }),
  /** 后台任务运行态（进程内）：与 /api/settings 同页展示。 */
  tasks: () => request<TasksResponse>("/api/tasks"),
  /** 手动运行一个后台任务一轮（管理台「立即运行」）；同步等结果返回。 */
  runTask: (key: string) =>
    request<TaskRunResult>(`/api/tasks/${encodeURIComponent(key)}/run`, { method: "POST" }),
  /** 运维告警记录：后台评估命中后落库，倒序回看（分页）。 */
  alerts: (sort?: string, order?: string, limit?: number, offset?: number) =>
    request<AlertsResponse>(`/api/alerts${query({ sort, order, limit, offset })}`),
  /** 人工标记一条告警已恢复（规则已下线 / 误报时的显式出口；admin + CSRF + 审计）。 */
  resolveAlert: (id: string) =>
    request<{ ok: boolean }>(`/api/alerts/${encodeURIComponent(id)}/resolve`,
                             { method: "POST" }),
  /** 上游官方状态页聚合快照（进程内、重启归零）：只读展示，不参与选号 / 熔断。 */
  upstreamStatus: () => request<UpstreamStatusResponse>("/api/upstream-status"),
  /**
   * 侧栏「数据提醒」角标：一个聚合端点覆盖整个侧栏，避免 Layout 挂 7 个页面级
   * hook。**按角色裁剪**——缺失的键 = 无权看（见 `NavBadges`）。
   */
  navBadges: () => request<NavBadges>("/api/nav-badges"),
  /** OpenRouter 模型目录（刊例价 + 明细元数据），模型列表页只读展示（服务端分页 + 筛选）。 */
  modelCatalog: (sort?: string, order?: string, q?: string, onlyAvailable?: boolean,
                 limit?: number, offset?: number) =>
    request<ModelCatalogResponse>(
      `/api/model-catalog${query({ sort, order, q, only_available: onlyAvailable,
                                   limit, offset })}`),

  // ---------------------------------------------------------------- 统计
  // `since` / `until`：自定义时间窗两端（半开 `[since, until)`，epoch 秒）。
  // 两者都省 = 全部；只给一端 = 单边不限。`until` 紧跟 `since`，避免调用点
  // 在长参数列表里错位。
  statsOverview: (username?: string, since?: number, until?: number) =>
    request<StatsOverview>(`/api/stats/overview${query({ username, since, until })}`),
  statsByProvider: (username?: string, since?: number, until?: number,
                    sort?: string, order?: string) =>
    request<{ providers: ProviderStats[] }>(
      `/api/stats/by-provider${query({ username, since, until, sort, order })}`),
  statsByModel: (username?: string, since?: number, until?: number,
                 sort?: string, order?: string) =>
    request<{ models: ModelStats[] }>(
      `/api/stats/by-model${query({ username, since, until, sort, order })}`),
  statsByUser: (username?: string, since?: number, until?: number,
                sort?: string, order?: string) =>
    request<{ users: UserStats[] }>(
      `/api/stats/by-user${query({ username, since, until, sort, order })}`),
  statsByCredential: (username?: string, since?: number, until?: number,
                      sort?: string, order?: string) =>
    request<{ credentials: CredentialStats[] }>(
      `/api/stats/by-credential${query({ username, since, until, sort, order })}`),
  statsTimeline: (username?: string, since?: number, until?: number, metric?: StatsMetric) =>
    request<{ points: TimelinePoint[] }>(
      `/api/stats/timeline${query({ username, since, until, metric })}`),
  statsModelTimeline: (username?: string, since?: number, until?: number,
                       metric?: StatsMetric) =>
    request<ModelTimelineResponse>(
      `/api/stats/model-timeline${query({ username, since, until, metric })}`),
  /** 延迟分位时序（P50 中位数 + P95 上界）：读明细表，仅覆盖 90 天。 */
  statsPercentileTimeline: (username?: string, since?: number, until?: number,
                            metric?: "latency" | "ttfb") =>
    request<PercentileTimelineResponse>(
      `/api/stats/percentile-timeline${query({ username, since, until, metric })}`),
  statsEvents: (username?: string, since?: number, until?: number, before?: number,
                limit?: number, sort?: string, order?: string, offset?: number,
                filters?: StatsEventFilters) =>
    request<StatsEventsResponse>(
      `/api/stats/events${query({ username, since, until, before, limit, sort, order,
                                  offset, ...filters })}`),
  /** 明细实时流（SSE）地址：交给 `EventSource` 订阅，页面不自己拼查询串。
   *
   * `EventSource` 自动带同源 Cookie、断线重连，但不支持自定义头（CSRF 只对
   * 写请求要求，GET 不受影响）。`username` 是作用域收窄，其余是明细过滤。 */
  statsEventsStreamUrl: (username?: string, filters?: StatsEventFilters) =>
    `/api/stats/events/stream${query({ username, ...filters })}`,
  /** 明细 CSV 导出：返回文件内容与截断标记（响应头），由页面触发下载。 */
  statsEventsCsv: async (username?: string, since?: number, until?: number,
                         filters?: StatsEventFilters): Promise<CsvExport> => {
    const path = `/api/stats/events${query({ username, since, until, ...filters, format: "csv" })}`;
    const response = await fetch(path, { credentials: "same-origin" });
    if (!response.ok) {
      // 与 request() 同口径地还原错误体，页面才能弹一致的中文提示
      let code = "error";
      let message = `导出失败（HTTP ${response.status}）`;
      try {
        const body = await response.json();
        if (body?.error) {
          code = body.error.code ?? code;
          message = body.error.message ?? message;
        }
      } catch {
        // 非 JSON 错误体（如反向代理的 HTML）：保留兜底文案
      }
      throw new ApiError(response.status, code, message);
    }
    return {
      blob: await response.blob(),
      rowCount: Number(response.headers.get("X-Export-Row-Count") ?? 0),
      truncated: response.headers.get("X-Export-Truncated") === "1",
    };
  },
};

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}
