import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Credential, CredentialsResponse, SessionInfo } from "../api/types";

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

export function useCredentials(username?: string) {
  return useQuery<CredentialsResponse>({
    queryKey: adminKey(username, "credentials"),
    queryFn: api.credentials,
  });
}

export function useApiKeys(username?: string) {
  return useQuery({
    queryKey: adminKey(username, "api-keys"),
    queryFn: api.apiKeys,
  });
}

export function useStatsOverview(username?: string, target?: string, since?: number) {
  return useQuery({
    queryKey: adminKey(username, "stats-overview", target, since),
    queryFn: () => api.statsOverview(target, since),
  });
}

export function useStatsByProvider(username?: string, target?: string, since?: number) {
  return useQuery({
    queryKey: adminKey(username, "stats-providers", target, since),
    queryFn: () => api.statsByProvider(target, since),
  });
}

export function useStatsTimeline(username?: string, target?: string, since?: number) {
  return useQuery({
    queryKey: adminKey(username, "stats-timeline", target, since),
    queryFn: () => api.statsTimeline(target, since),
  });
}

export function useStatsModelTimeline(username?: string, target?: string, since?: number) {
  return useQuery({
    queryKey: adminKey(username, "stats-model-timeline", target, since),
    queryFn: () => api.statsModelTimeline(target, since),
  });
}

/** 逐请求明细：游标分页（before + limit），翻页时保留旧数据避免闪空。 */
export function useStatsEvents(username?: string, since?: number, before?: number, limit?: number) {
  return useQuery({
    queryKey: adminKey(username, "stats-events", since, before, limit),
    queryFn: () => api.statsEvents(undefined, since, before, limit),
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
