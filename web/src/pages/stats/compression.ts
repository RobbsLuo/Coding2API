/**
 * 压缩收益读数（只取数，不渲染）。
 *
 * 为什么必须与总览同屏：压缩省下的 token 只有在「上游前缀缓存仍然命中」时才
 * 是净收益——上下文裁剪每裁一条都会把前缀边界前移，命中率掉下去的代价可能
 * 超过省下的 token。只看其中任何一个都会得出相反的结论（「省了很多」或「开了
 * 没用」），所以这两种指标合并进同一块概览里呈现。
 *
 * 数据源：
 * - 压缩计数：`GET /api/stats/compression?since=&until=`（进程内按小时分桶，
 *   重启从磁盘快照续接、保留 90 天）；把当前时间窗传下去，数字才与同屏缓存
 *   命中率同口径；
 * - 缓存命中率：与总览**同一个** `/api/stats/overview` 响应（同一时间窗），
 *   由父组件以 `stats` 传入，避免两处各查一次导致口径/窗口不一致。
 *
 * 这里刻意不引新的 API 客户端方法：只读一个轻量端点，用原生 fetch + 同一
 * CSRF 头即可；共享 `api/client.ts` 会增加一层与页面生命周期无关的耦合。
 */
import { useQuery } from "@tanstack/react-query";
import { formatAssumedCacheRate, formatCacheRate } from "../../api/display";
import type { StatsOverview } from "../../api/types";

/** `/api/stats/compression` 的响应形状（端点只回计数，不回任何用户维度）。 */
export interface CompressionStats {
  triggers: number;
  tool_triggers: number;
  context_trims: number;
  chars_saved: number;
  tokens_saved: number;
  unchanged: number;
}

export interface CompressionResponse {
  stats: CompressionStats;
  context_compress_enabled: boolean;
  tool_output_compress_enabled: boolean;
}

/** 进程内计数器端点：失败不重试（读数只是辅助，别拖慢页面）。 */
async function fetchCompression(
  since?: number,
  until?: number,
): Promise<CompressionResponse> {
  const query = new URLSearchParams();
  if (since !== undefined) query.set("since", String(since));
  if (until !== undefined) query.set("until", String(until));
  const suffix = query.toString() ? `?${query.toString()}` : "";
  const response = await fetch(`/api/stats/compression${suffix}`, {
    credentials: "same-origin",
    headers: { "X-Requested-With": "XMLHttpRequest" },
  });
  if (!response.ok) {
    throw new Error(`compression stats unavailable (HTTP ${response.status})`);
  }
  return (await response.json()) as CompressionResponse;
}

/** 开关状态文案：明确写「哪一项开着」，而不是笼统的「已启用」。 */
export function compressionSwitchLabel(data: CompressionResponse | undefined): string {
  if (!data) return "—";
  const context = data.context_compress_enabled ? "上下文裁剪 开" : "上下文裁剪 关";
  const tool = data.tool_output_compress_enabled ? "tool 输出 开" : "tool 输出 关";
  return `${context} · ${tool}`;
}

export interface CompressionFeed {
  data: CompressionResponse | undefined;
  counters: CompressionStats | undefined;
  loading: boolean;
  failed: boolean;
  /** 所选时间窗内一次压缩都没触发（数据到手后才可能为 true）。 */
  idle: boolean;
  /** 同期缓存命中率（含未上报时的 ≈ 推算值）。 */
  hitRate: string;
  switchLabel: string;
}

export interface CompressionQuery {
  /** 与总览同一时间窗的统计（缓存命中率从这里取，保证「同期」）。 */
  stats: StatsOverview | undefined;
  /** 计数是池级/进程级的，只有 operator+ 能看到（viewer 的窗口口径对不上）。 */
  isOperator: boolean;
  /** 当前时间窗起点（秒）；与总览同源，undefined = 「全部」。 */
  since?: number;
  /** 当前时间窗终点（秒，半开区间）；undefined = 至今。 */
  until?: number;
  /** 自定义范围非法（暂停取数）时不请求，避免把「全量」当成窗口结果。 */
  rangeReady?: boolean;
}

export function useCompressionFeed({
  stats,
  isOperator,
  since,
  until,
  rangeReady = true,
}: CompressionQuery): CompressionFeed {
  const compression = useQuery({
    queryKey: ["stats-compression", since ?? null, until ?? null],
    queryFn: () => fetchCompression(since, until),
    retry: false,
    enabled: isOperator && rangeReady,
  });
  const data = compression.data;
  const counters = data?.stats;
  // 空态判据：所选时间窗内一次压缩都没触发（不是「没有请求」——unchanged 也会
  // 记数，两者都是 0 才说明压缩路径根本没被调用过）。必须等数据到手再判：
  // undefined 时同样「都是 0」，否则加载中会先闪一下「未触发」的假空态。
  const idle = data !== undefined
    && (counters?.triggers ?? 0) === 0
    && (counters?.unchanged ?? 0) === 0;
  // 分母只含上报过缓存的请求；老接口没有该字段时才回落到总输入。
  // 未上报但后端给了校准命中率（TRAE）时显示 ≈ 推算值。
  const hitRate = stats?.cached_tokens === null
    && stats?.cache_rate_assumed !== null
    && stats?.cache_rate_assumed !== undefined
    ? formatAssumedCacheRate(stats.cache_rate_assumed)
    : formatCacheRate(
      stats?.cached_tokens,
      stats?.cached_input_tokens ?? stats?.input_tokens,
    );

  return {
    data,
    counters,
    loading: compression.isPending && isOperator && rangeReady,
    failed: compression.isError,
    idle: isOperator && rangeReady && idle,
    hitRate,
    switchLabel: compressionSwitchLabel(data),
  };
}