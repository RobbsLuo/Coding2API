import { useEffect, useState } from "react";
import { useSessionContext } from "../Layout";
import { useStatsByProvider, useStatsModelTimeline, useStatsOverview, useStatsTimeline } from "../api/hooks";
import { formatNumber } from "../api/display";
import { ModelTrendChart } from "../components/ModelTrendChart";
import { PageHeader } from "../components/PageHeader";
import { ProviderIcon } from "../components/ProviderIcon";
import { UsageChart } from "../components/UsageChart";
import type { Provider } from "../api/types";
import {
  Empty,
  Field,
  Input,
  Metric,
  Panel,
  Select,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";

const RANGES = [
  { value: "24h", label: "最近 24 小时", seconds: 86400 },
  { value: "7d", label: "最近 7 天", seconds: 7 * 86400 },
  { value: "30d", label: "最近 30 天", seconds: 30 * 86400 },
  { value: "all", label: "全部", seconds: 0 },
];

const PROVIDER_LABEL: Record<Provider, string> = { codebuddy: "CodeBuddy", trae: "TRAE" };

/** 用户名筛选的被控输入与服务端查询值分离，避免每敲一个字都打一次接口。 */
const FILTER_DEBOUNCE_MS = 300;

export function StatsPage() {
  const session = useSessionContext();
  const [range, setRange] = useState("7d");
  const [target, setTarget] = useState("");
  const [debouncedTarget, setDebouncedTarget] = useState("");

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedTarget(target), FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [target]);

  const seconds = RANGES.find((item) => item.value === range)?.seconds ?? 0;
  const since = seconds > 0 ? Math.floor(Date.now() / 1000) - seconds : undefined;
  const username = session.is_admin && debouncedTarget ? debouncedTarget : undefined;

  const overview = useStatsOverview(session.username, username, since);
  const byProvider = useStatsByProvider(session.username, username, since);
  const timeline = useStatsTimeline(session.username, username, since);
  const modelTimeline = useStatsModelTimeline(session.username, username, since);

  if (overview.isLoading) {
    return (
      <div className="space-y-6" data-testid="stats-page">
        <p className="sr-only">载入中…</p>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {Array.from({ length: 8 }).map((_, index) => (
            <div key={index} className="rounded-xl border border-border p-4">
              <Skeleton className="h-3 w-14" />
              <Skeleton className="mt-2 h-6 w-12" />
            </div>
          ))}
        </div>
      </div>
    );
  }
  const stats = overview.data;
  const rate =
    stats?.success_rate === null || stats?.success_rate === undefined
      ? null
      : stats.success_rate;
  const rateTone =
    rate === null ? undefined : rate >= 0.99 ? "ok" : rate >= 0.9 ? "warn" : "danger";

  return (
    <div className="space-y-6" data-testid="stats-page">
      <PageHeader
        title="用量统计"
        description="按用户与上游查看请求量、成功率与 token 消耗；管理员可筛选任意用户，普通用户只能看自己。"
      />

      <Panel title="筛选">
        <div className="grid items-end gap-3 sm:max-w-xl sm:grid-cols-[minmax(0,14rem)_minmax(0,18rem)]">
          <Field label="时间范围">
            <Select
              value={range}
              data-testid="range-select"
              onChange={(event) => setRange(event.target.value)}
            >
              {RANGES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </Select>
          </Field>
          {session.is_admin && (
            <Field label="用户名" hint="留空表示全部用户">
              <Input
                value={target}
                data-testid="username-filter"
                placeholder="全部用户"
                onChange={(event) => setTarget(event.target.value)}
              />
            </Field>
          )}
        </div>
      </Panel>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Metric label="请求数" value={formatNumber(stats?.requests)} tone="ok" />
        <Metric
          label="成功率"
          value={
            rate === null ? "—" : `${(rate * 100).toFixed(1)}%`
          }
          tone={rateTone}
        />
        <Metric label="输入 token" value={formatNumber(stats?.input_tokens)} />
        <Metric label="输出 token" value={formatNumber(stats?.output_tokens)} />
        <Metric label="推理 token" value={formatNumber(stats?.reasoning_tokens)} />
        <Metric
          label="平均延迟"
          value={
            stats?.avg_latency_ms === null || stats?.avg_latency_ms === undefined
              ? "—"
              : `${formatNumber(stats.avg_latency_ms)} ms`
          }
        />
        <Metric
          label="平均首字延迟"
          value={
            stats?.avg_ttfb_ms === null || stats?.avg_ttfb_ms === undefined
              ? "—"
              : `${formatNumber(stats.avg_ttfb_ms)} ms`
          }
        />
        <Metric
          label="Credit 消耗"
          value={
            stats?.credit === null || stats?.credit === undefined
              ? "—"
              : formatNumber(Number(stats.credit.toFixed(2)))
          }
          hint="上游可选字段，可能不返回"
        />
      </section>

      <Panel title="请求量趋势" action={
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="inline-flex items-center gap-1.5">
            <ProviderIcon provider="codebuddy" size={12} />
            CodeBuddy
          </span>
          <span className="inline-flex items-center gap-1.5">
            <ProviderIcon provider="trae" size={12} />
            TRAE
          </span>
        </div>
      }>
        {(timeline.data?.points.length ?? 0) === 0 ? (
          <Empty>该范围内没有小时汇总数据</Empty>
        ) : (
          <UsageChart points={timeline.data?.points ?? []} />
        )}
      </Panel>

      <Panel title="按模型趋势" action={
        modelTimeline.data?.models.length ? (
          <span className="text-xs text-muted-foreground">
            请求量 Top {modelTimeline.data.models.length} 模型
          </span>
        ) : undefined
      }>
        {(modelTimeline.data?.points.length ?? 0) === 0 ? (
          <Empty data-testid="no-model-trend">该范围内没有小时汇总数据</Empty>
        ) : (
          <ModelTrendChart
            points={modelTimeline.data?.points ?? []}
            models={modelTimeline.data?.models ?? []}
          />
        )}
      </Panel>

      <Panel title="按上游分组">
        {(byProvider.data?.providers.length ?? 0) === 0 ? (
          <Empty data-testid="no-provider-stats">该范围内没有请求</Empty>
        ) : (
          <Table data-testid="provider-table">
            <TableHeader>
              <TableRow>
                <TableHead>上游</TableHead>
                <TableHead className="text-right">请求数</TableHead>
                <TableHead className="text-right">成功数</TableHead>
                <TableHead className="text-right">输入 token</TableHead>
                <TableHead className="text-right">输出 token</TableHead>
                <TableHead className="text-right">Credit</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {byProvider.data?.providers.map((row) => (
                <TableRow key={row.provider}>
                  <TableCell>
                    <span className="inline-flex items-center gap-1.5">
                      <ProviderIcon provider={row.provider} size={13} />
                      {PROVIDER_LABEL[row.provider] ?? row.provider}
                    </span>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{formatNumber(row.requests)}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatNumber(row.ok_count)}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatNumber(row.input_tokens)}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatNumber(row.output_tokens)}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {row.credit === null ? "—" : formatNumber(Number(row.credit.toFixed(2)))}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      <div className="flex flex-col gap-1 rounded-lg border border-border bg-card px-3 py-2.5 text-xs text-muted-foreground">
        <p>
          隐私：不保存提示词、回答、请求头、Token、工具参数与原始错误体；逐请求明细保留 90 天，小时汇总永久保留；按 API Key 归属用户统计。
        </p>
        <p>
          credit 为上游可选字段，经常不返回；健康度只依赖额度探测接口，主指标是 token 数。
        </p>
      </div>
    </div>
  );
}