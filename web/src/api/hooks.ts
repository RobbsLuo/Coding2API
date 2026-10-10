import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  Credential,
  CredentialsResponse,
  SessionInfo,
  StatsMetric,
} from "../api/types";

/** Query key 约定：所有管理数据以 ["admin", username, ...] 开头。 */
export function adminKey(username: string | undefined, ...rest: unknown[]) {
  return ["admin", username ?? "anonymous", ...rest] as const;
}

export function useSession() {
  return useQuery({
    queryKey: ["session"],
    queryFn: api.session,
    retry: false,
    // 会话状态变化必须立刻反映：删用户/换密钥后不能继续放行
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
}

export function useCredentials(username?: string, refetchIntervalMs?: number,
                                sort?: string, order?: string) {
  return useQuery<CredentialsResponse>({
    queryKey: adminKey(username, "credentials", sort, order),
    queryFn: () => api.credentials(sort, order),
    // 仪表盘/凭证页的冷却倒计时是「随时间变化」的观测量；页面停留时自动
    // 刷新，其余调用点不传即为默认（不轮询）。
    refetchInterval: refetchIntervalMs,
  });
}

export function useApiKeys(username?: string, sort?: string, order?: string) {
  return useQuery({
    queryKey: adminKey(username, "api-keys", sort, order),
    queryFn: () => api.apiKeys(sort, order),
  });
}

export function useSettings(username?: string) {
  return useQuery({
    queryKey: adminKey(username, "settings"),
    queryFn: api.settings,
  });
}

/** OpenRouter 模型目录（模型列表页）不缓存太久的引用数据；与凭证/设置同区分。 */
export function useModelCatalog(username?: string, sort?: string, order?: string) {
  return useQuery({
    queryKey: adminKey(username, "model-catalog", sort, order),
    queryFn: () => api.modelCatalog(sort, order),
  });
}

/** 用户列表（admin only）。改动后由 useUserMutation 统一失效。 */
export function useUsers(username?: string, sort?: string, order?: string) {
  return useQuery({
    queryKey: adminKey(username, "users", sort, order),
    queryFn: () => api.users(sort, order),
  });
}

/** 审计流水（admin only）。筛选条件进 query key，切筛选即重新取数。 */
export function useAudit(
  username?: string,
  filters: { actor?: string; action?: string; limit?: number; offset?: number;
             sort?: string; order?: string } = {},
) {
  return useQuery({
    queryKey: adminKey(username, "audit", filters.actor ?? "", filters.action ?? "",
                       filters.limit ?? 100, filters.offset ?? 0,
                       filters.sort ?? "", filters.order ?? ""),
    queryFn: () => api.audit(filters),
  });
}

/** 运维告警记录（admin only）。轮询：告警是「刚刚发生了什么」，需要自己刷新。 */
export function useAlerts(username?: string, refetchIntervalMs = 30_000,
                          sort?: string, order?: string) {
  return useQuery({
    queryKey: adminKey(username, "alerts", sort, order),
    queryFn: () => api.alerts(sort, order),
    refetchInterval: refetchIntervalMs,
  });
}

/** 用户写操作：成功后失效用户列表（角色/启用状态改变了）。 */
export function useUserMutation<TArgs, TResult>(
  mutationFn: (args: TArgs) => Promise<TResult>,
  username?: string,
) {
  const client = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminKey(username, "users") });
      // 动到自己（改密/改角色）时会话信息也变了，一并刷新
      void client.invalidateQueries({ queryKey: ["session"] });
    },
  });
}

/**
 * 后台任务运行态：30 秒自动刷新。
 *
 * 任务状态是「随时间变化」的观测量（上次执行距现在多久、这一轮跑没跑），
 * 手动刷新会让人以为任务停了。settings 不刷：配置改动由用户自己触发。
 */
export function useTasks(username?: string) {
  return useQuery({
    queryKey: adminKey(username, "tasks"),
    queryFn: api.tasks,
    refetchInterval: 30_000,
  });
}

export function useStatsOverview(username?: string, target?: string, since?: number) {
  return useQuery({
    queryKey: adminKey(username, "stats-overview", target, since),
    queryFn: () => api.statsOverview(target, since),
  });
}

export function useStatsByProvider(username?: string, target?: string, since?: number,
                                   sort?: string, order?: string) {
  return useQuery({
    queryKey: adminKey(username, "stats-providers", target, since, sort, order),
    queryFn: () => api.statsByProvider(target, since, sort, order),
  });
}

export function useStatsByModel(username?: string, target?: string, since?: number,
                               sort?: string, order?: string) {
  return useQuery({
    queryKey: adminKey(username, "stats-models", target, since, sort, order),
    queryFn: () => api.statsByModel(target, since, sort, order),
  });
}

export function useStatsByUser(username?: string, target?: string, since?: number,
                              sort?: string, order?: string) {
  return useQuery({
    queryKey: adminKey(username, "stats-users", target, since, sort, order),
    queryFn: () => api.statsByUser(target, since, sort, order),
  });
}

/** 按凭证聚合：数据源是明细表（仅 90 天），非 admin/operator 看不到他人凭证。 */
export function useStatsByCredential(username?: string, target?: string, since?: number,
                                     sort?: string, order?: string) {
  return useQuery({
    queryKey: adminKey(username, "stats-credentials", target, since, sort, order),
    queryFn: () => api.statsByCredential(target, since, sort, order),
  });
}

export function useStatsTimeline(username?: string, target?: string, since?: number,
                                  metric?: string) {
  return useQuery({
    queryKey: adminKey(username, "stats-timeline", target, since, metric),
    queryFn: () => api.statsTimeline(target, since, metric as StatsMetric),
  });
}

export function useStatsModelTimeline(username?: string, target?: string, since?: number,
                                      metric?: string) {
  return useQuery({
    queryKey: adminKey(username, "stats-model-timeline", target, since, metric),
    queryFn: () => api.statsModelTimeline(target, since, metric as StatsMetric),
  });
}

/** 逐请求明细：排序切换 rowid 游标 / offset 两套分页（见后端 events 文档）。 */
export function useStatsEvents(username?: string, since?: number, before?: number,
                               limit?: number, sort?: string, order?: string,
                               offset?: number) {
  return useQuery({
    queryKey: adminKey(username, "stats-events", since, before, limit, sort, order, offset),
    queryFn: () => api.statsEvents(undefined, since, before, limit, sort, order, offset),
    placeholderData: keepPreviousData,
  });
}

/** 凭证相关写操作：成功后统一失效凭证与统计查询。 */
export function useCredentialMutation<TArgs, TResult>(
  mutationFn: (args: TArgs) => Promise<TResult>,
  username?: string,
) {
  const client = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["admin", username ?? "anonymous"] });
    },
  });
}

export function useLogout() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      client.clear();
    },
  });
}

export { useQueryClient };

export function isDisabled(credential: Credential): boolean {
  return credential.disabled === 1;
}

export function sessionOf(data: SessionInfo | undefined): SessionInfo | undefined {
  return data;
}
