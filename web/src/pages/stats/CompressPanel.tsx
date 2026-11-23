/**
 * 压缩可观测面板：触发次数 / 省下的字符与 token / 原地不变次数 / 同期缓存命中率。
 *
 * 为什么这几个数字必须**同屏**：压缩省下的 token 只有在「上游前缀缓存仍然命中」
 * 时才是净收益——上下文裁剪每裁一条都会把前缀边界前移，命中率掉下去的代价可能
 * 超过省下的 token。只看其中任何一个都会得出相反的结论（「省了很多」或「开了
 * 没用」），所以它们放在同一块面板里。
 *
 * 数据源：
 * - 压缩计数：`GET /api/stats/compression`（进程内计数器，重启归零）；
 * - 缓存命中率：与上方总览**同一个** `/api/stats/overview` 响应（同一时间窗），
 *   由父组件以 `stats` 传入，避免两处各查一次导致口径/窗口不一致。
 *
 * 这里刻意不引新的 API 客户端方法：面板只读一个轻量端点，用原生 fetch + 同一
 * CSRF 头即可；共享 `api/client.ts` 会增加一层与页面生命周期无关的耦合。
 */
import { useQuery } from "@tanstack/react-query";
import { Gauge, ArrowDownRight, ToggleLeft, ToggleRight } from "lucide-react";
import { Metric, Panel, Notice } from "../../ui";
import { formatCacheRate, formatCompact, formatNumber } from "../../api/display";
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

/** 进程内计数器端点：失败不重试（面板是辅助读数，别拖慢页面）。 */
async function fetchCompression(): Promise<CompressionResponse> {
  const response = await fetch("/api/stats/compression", {
    credentials: "same-origin",
    headers: { "X-Requested-With": "XMLHttpRequest" },
  });
  if (!response.ok) {
    throw new Error(`compression stats unavailable (HTTP ${response.status})`);
  }
  return (await response.json()) as CompressionResponse;
}

/** 开关状态文案：明确写「哪一项开着」，而不是笼统的「已启用」。 */
function switchLabel(data: CompressionResponse | undefined): string {
  if (!data) return "—";
  const context = data.context_compress_enabled ? "上下文裁剪 开" : "上下文裁剪 关";
  const tool = data.tool_output_compress_enabled ? "tool 输出 开" : "tool 输出 关";
  return `${context} · ${tool}`;
}

interface CompressPanelProps {
  /** 与总览同一时间窗的统计（缓存命中率从这里取，保证「同期」）。 */
  stats: StatsOverview | undefined;
  /** 计数是池级/进程级的，只有 operator+ 能看到（viewer 的窗口口径对不上）。 */
  isOperator: boolean;
}

export function CompressPanel({ stats, isOperator }: CompressPanelProps) {
  const compression = useQuery({
    queryKey: ["stats-compression"],
    queryFn: fetchCompression,
    retry: false,
    enabled: isOperator,
  });
  const data = compression.data;
  const counters = data?.stats;
  // 空态判据：本次启动以来一次压缩都没触发（不是「没有请求」——unchanged 也会
  // 记数，两者都是 0 才说明压缩路径根本没被调用过）。必须等数据到手再判：
  // undefined 时同样「都是 0」，否则加载中会先闪一下「未触发」的假空态。
  const idle = data !== undefined
    && (counters?.triggers ?? 0) === 0
    && (counters?.unchanged ?? 0) === 0;
  const hitRate = formatCacheRate(stats?.cached_tokens, stats?.input_tokens);

  if (!isOperator) return null;

  return (
    <Panel
      title="压缩收益"
      data-testid="compress-panel"
      action={
        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {data?.tool_output_compress_enabled
            ? <ToggleRight className="size-4 text-ok" aria-hidden />
            : <ToggleLeft className="size-4" aria-hidden />}
          {switchLabel(data)}
        </span>
      }
    >
      {compression.isPending ? (
        <div data-testid="compress-panel-loading">
          <Notice tone="muted">正在读取压缩计数…</Notice>
        </div>
      ) : compression.isError ? (
        <div data-testid="compress-panel-error">
          <Notice tone="muted">
            压缩读数不可用（端点无响应或会话已过期）；这不影响压缩本身，按开关继续生效。
          </Notice>
        </div>
      ) : idle ? (
        <div data-testid="compress-panel-empty">
          <Notice tone="muted">
            本次启动以来压缩未触发，也没有可压的 tool 输出或裁掉的历史；上方「缓存命中率」即为当前代价。
          </Notice>
        </div>
      ) : (
        <div className="space-y-3">
          <section aria-label="压缩收益指标" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Metric
              label="触发次数"
              value={formatNumber(counters?.triggers)}
              tone="ok"
              hint={`tool 输出 ${formatNumber(counters?.tool_triggers)} · 上下文裁剪 ${formatNumber(counters?.context_trims)}`}
              icon={<Gauge className="size-4" />}
            />
            <Metric
              label="省下 token（估算）"
              value={formatCompact(counters?.tokens_saved)}
              hint={`字符 ${formatCompact(counters?.chars_saved)} · 按 0.25 token/字符下界估算`}
              icon={<ArrowDownRight className="size-4" />}
            />
            <Metric
              label="同期缓存命中率"
              value={hitRate}
              hint={`命中 ${formatCompact(stats?.cached_tokens ?? null)} · 输入 ${formatCompact(stats?.input_tokens)}`}
            />
            <Metric
              label="原地不变"
              value={formatNumber(counters?.unchanged)}
              hint="开关开着但载荷逐字节没变：这是「白开」的证据"
            />
          </section>
          <p className="text-xs text-muted-foreground">
            判断口径：省下的 token 要大于「缓存命中率下降造成的未命中 token」才值得开。
            上下文裁剪会让前缀边界前移，命中率异常时优先看「上下文裁剪」次数；计数为进程内累计，重启归零。
          </p>
        </div>
      )}
    </Panel>
  );
}