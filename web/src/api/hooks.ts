import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type {
  CredentialsResponse,
  NavBadges,
  StatsEventFilters,
  StatsMetric,
  UsageEventRow,
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

export function useApiKeys(username?: string, sort?: string, order?: string,
                           limit?: number, offset?: number) {
  return useQuery({
    queryKey: adminKey(username, "api-keys", sort, order, limit, offset),
    queryFn: () => api.apiKeys(sort, order, limit, offset),
    placeholderData: keepPreviousData,
  });
}

export function useSettings(username?: string) {
  return useQuery({
    queryKey: adminKey(username, "settings"),
    queryFn: api.settings,
  });
}

/**
 * 会话鉴权的模型列表（`/api/playground/models`），与 Playground 页共用同一
 * 缓存键，故两处只发一次请求。CC Switch 默认模型下拉用它——用户无需自造 Key。
 */
export function usePlaygroundModels() {
  return useQuery({
    queryKey: ["playground-models"],
    queryFn: ({ signal }) => api.playgroundModels(signal),
  });
}

/** OpenRouter 模型目录（模型列表页，operator）：服务端分页 + 筛选，翻页保留上一页避免闪烁。 */
export function useModelCatalog(username?: string, sort?: string, order?: string,
                                q?: string, onlyAvailable?: boolean,
                                limit?: number, offset?: number) {
  return useQuery({
    queryKey: adminKey(username, "model-catalog", sort, order, q, onlyAvailable,
                       limit, offset),
    queryFn: () => api.modelCatalog(sort, order, q, onlyAvailable, limit, offset),
    placeholderData: keepPreviousData,
  });
}

/** 用户列表（admin only）。改动后由 useUserMutation 统一失效。 */
export function useUsers(username?: string, sort?: string, order?: string,
                         limit?: number, offset?: number) {
  return useQuery({
    queryKey: adminKey(username, "users", sort, order, limit, offset),
    queryFn: () => api.users(sort, order, limit, offset),
    placeholderData: keepPreviousData,
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
    placeholderData: keepPreviousData,
  });
}

/** 运维告警记录（admin only）。轮询：告警是「刚刚发生了什么」，需要自己刷新。 */
export function useAlerts(username?: string, refetchIntervalMs = 30_000,
                          sort?: string, order?: string,
                          limit?: number, offset?: number) {
  return useQuery({
    queryKey: adminKey(username, "alerts", sort, order, limit, offset),
    queryFn: () => api.alerts(sort, order, limit, offset),
    refetchInterval: refetchIntervalMs,
    placeholderData: keepPreviousData,
  });
}

/**
 * 上游状态页聚合快照（operator：admin / operator）。轮询：状态页是「现在上游怎么样」，
 * 用户停留时自动刷新；抓取由后端周期任务负责，前端不触发。
 */
export function useUpstreamStatus(username?: string, refetchIntervalMs = 60_000) {
  return useQuery({
    queryKey: adminKey(username, "upstream-status"),
    queryFn: api.upstreamStatus,
    refetchInterval: refetchIntervalMs,
  });
}

/**
 * 侧栏「数据提醒」角标（一个聚合端点，见 `api.navBadges`）。
 *
 * 60s 轮询：侧栏在每个页面都在，频率再高只是白打后端的几条 COUNT，而「待办」
 * 本身不是秒级观测量。窗口聚焦时也刷一次——离开一阵回来该看到最新待办。
 */
export function useNavBadges(username?: string) {
  return useQuery<NavBadges>({
    queryKey: adminKey(username, "nav-badges"),
    queryFn: api.navBadges,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
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

/**
 * 手动运行一个后台任务一轮（管理台「立即运行」）。
 *
 * 成功后失效任务运行态：后端已把这一轮记入 status，立刻回读才不会等到
 * 下一次 30 秒轮询才看到结果。返回的 `TaskRunResult` 供调用点直接展示
 * 「跑了 / 没排上 / 被开关跳过」，无需再读缓存。
 */
export function useRunTask(username?: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => api.runTask(key),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminKey(username, "tasks") });
    },
  });
}

/**
 * 人工标记一条告警已恢复（规则已下线 / 误报时的显式出口）。
 *
 * 成功后失效告警列表与侧栏角标：角标数 `resolved_at IS NULL`，不立刻回读会
 * 让用户看到「点了但角标没变」。
 */
export function useResolveAlert(username?: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.resolveAlert(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminKey(username, "alerts") });
      void client.invalidateQueries({ queryKey: adminKey(username, "nav-badges") });
    },
  });
}

export function useStatsOverview(username?: string, target?: string, since?: number,
                                 until?: number, enabled = true) {
  return useQuery({
    queryKey: adminKey(username, "stats-overview", target, since, until),
    queryFn: () => api.statsOverview(target, since, until),
    // 换时间范围（含自定义窗口逐字段编辑）时保留上一批数据：页面在
    // `overview.isLoading` 时整体切骨架屏，若不带占位数据，每次改时间都会把
    // 筛选控件一起卸载重挂——用户正在编辑的输入框会消失。
    placeholderData: keepPreviousData,
    enabled,
  });
}

export function useStatsByProvider(username?: string, target?: string, since?: number,
                                   until?: number, sort?: string, order?: string,
                                   enabled = true) {
  return useQuery({
    queryKey: adminKey(username, "stats-providers", target, since, until, sort, order),
    queryFn: () => api.statsByProvider(target, since, until, sort, order),
    enabled,
  });
}

export function useStatsByModel(username?: string, target?: string, since?: number,
                                until?: number, sort?: string, order?: string,
                                enabled = true) {
  return useQuery({
    queryKey: adminKey(username, "stats-models", target, since, until, sort, order),
    queryFn: () => api.statsByModel(target, since, until, sort, order),
    enabled,
  });
}

export function useStatsByUser(username?: string, target?: string, since?: number,
                               until?: number, sort?: string, order?: string,
                               enabled = true) {
  return useQuery({
    queryKey: adminKey(username, "stats-users", target, since, until, sort, order),
    queryFn: () => api.statsByUser(target, since, until, sort, order),
    enabled,
  });
}

/** 按凭证聚合：数据源是明细表（仅 90 天），非 admin/operator 看不到他人凭证。 */
export function useStatsByCredential(username?: string, target?: string, since?: number,
                                     until?: number, sort?: string, order?: string,
                                     enabled = true) {
  return useQuery({
    queryKey: adminKey(username, "stats-credentials", target, since, until, sort, order),
    queryFn: () => api.statsByCredential(target, since, until, sort, order),
    enabled,
  });
}

export function useStatsTimeline(username?: string, target?: string, since?: number,
                                 until?: number, metric?: string, enabled = true) {
  return useQuery({
    queryKey: adminKey(username, "stats-timeline", target, since, until, metric),
    queryFn: () => api.statsTimeline(target, since, until, metric as StatsMetric),
    enabled,
  });
}

export function useStatsModelTimeline(username?: string, target?: string, since?: number,
                                      until?: number, metric?: string, enabled = true) {
  return useQuery({
    queryKey: adminKey(username, "stats-model-timeline", target, since, until, metric),
    queryFn: () => api.statsModelTimeline(target, since, until, metric as StatsMetric),
    enabled,
  });
}

/** 延迟分位时序（P50 中位数 + P95 上界）：读明细表，仅覆盖 90 天。
 *
 * `enabled=false` 时不发请求：该图只在延迟类指标下展示，其余指标用不上，
 * 没必要为每次切指标都扫一遍明细表。 */
export function useStatsPercentileTimeline(username?: string, target?: string,
                                           since?: number, until?: number,
                                           metric?: "latency" | "ttfb",
                                           enabled = true) {
  return useQuery({
    queryKey: adminKey(username, "stats-percentile", target, since, until, metric),
    queryFn: () => api.statsPercentileTimeline(target, since, until, metric),
    enabled,
  });
}

/** 逐请求明细：排序切换 rowid 游标 / offset 两套分页（见后端 events 文档）。
 *
 * `username` 是**作用域收窄**（钻取到某个用户时才有值）：为空则按会话作用域
 * （admin/operator 看全量、viewer 看自己），传值则只看该用户（viewer 传了也
 * 会被后端强制改回自己）。
 *
 * `filters.ok`：只看成功（1）/ 只看失败（0）。失败归因卡片点行下钻时带 0——
 * 不进 query key 的话，切换失败过滤不会重新取数。 */
export function useStatsEvents(username?: string, since?: number, until?: number,
                               before?: number, limit?: number, sort?: string,
                               order?: string, offset?: number,
                               filters?: StatsEventFilters & { ok?: number },
                               enabled = true) {
  const provider = filters?.provider;
  const model = filters?.model;
  const credentialId = filters?.credential_id;
  const ok = filters?.ok;
  return useQuery({
    queryKey: adminKey(username, "stats-events", since, until, before, limit, sort, order,
                       offset, provider, model, credentialId, ok),
    queryFn: () => api.statsEvents(username, since, until, before, limit, sort, order,
                                   offset,
                                   // `ok` 不在 StatsEventFilters 里（那个类型描述分组表
                                   // 钻取）；client 会把对象整体展开进 query，故额外属性生效
                                   { provider, model, credential_id: credentialId,
                                     ok } as StatsEventFilters),
    placeholderData: keepPreviousData,
    enabled,
  });
}

/** 明细实时流（SSE）：`enabled` 为真时建连，关闭即断开。
 *
 * 为什么不用 react-query 轮询：后端在写入事务提交后**主动推**，轮询要么太密
 * （每秒一次查询）要么不够「实时」。这里只管连接生命周期，收到的行由调用方
 * 决定怎么展示（页面把它与翻页结果合并）。
 *
 * 依赖变化（钻取过滤/作用域）会重建连接：`EventSource` 无法改查询串，只能重开。
 * 断线由浏览器自动重连，这里只在 `error` 时把状态标成「重连中」，不主动 close。
 */
export function useStatsEventStream(options: {
  enabled: boolean;
  username?: string;
  filters?: StatsEventFilters & { ok?: number };
  onEvent: (row: UsageEventRow) => void;
}) {
  const { enabled, username, filters, onEvent } = options;
  const provider = filters?.provider;
  const model = filters?.model;
  const credentialId = filters?.credential_id;
  const ok = filters?.ok;
  // 回调放 ref：调用方每次渲染都可能传新函数，不应触发重连
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const [status, setStatus] = useState<"idle" | "live" | "error">("idle");

  useEffect(() => {
    if (!enabled) {
      setStatus("idle");
      return;
    }
    if (typeof EventSource === "undefined") {
      // 老浏览器 / 测试环境未提供：不假装已连上，页面按「不支持」提示
      setStatus("error");
      return;
    }
    const url = api.statsEventsStreamUrl(username,
      // 同 `useStatsEvents`：`ok` 由 client 展开进 query，类型层面额外属性
      { provider, model, credential_id: credentialId, ok } as StatsEventFilters);
    const source = new EventSource(url);
    setStatus("live");
    source.onmessage = (event: MessageEvent<string>) => {
      try {
        handler.current(JSON.parse(event.data) as UsageEventRow);
      } catch {
        // 半截帧 / 非 JSON：丢弃这一条，连接继续（不能因一条坏数据断开实时流）
      }
    };
    source.onerror = () => {
      // readyState=CLOSED 说明浏览器放弃重连（如 401/429）：标成错误
      setStatus(source.readyState === EventSource.CLOSED ? "error" : "live");
    };
    return () => {
      source.close();
    };
  }, [enabled, username, provider, model, credentialId, ok]);

  return status;
}

/** 凭证相关写操作：成功后统一失效凭证与统计查询。 */export function useCredentialMutation<TArgs, TResult>(
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
