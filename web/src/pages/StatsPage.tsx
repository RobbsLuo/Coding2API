import { useMemo, useState } from "react";
import { useSessionContext } from "../Layout";
import {
  useStatsByProvider,
  useStatsEvents,
  useStatsModelTimeline,
  useStatsOverview,
  useStatsTimeline,
} from "../api/hooks";
import { formatCompact, formatLatency, formatNumber, formatTime } from "../api/display";
import { Notice } from "../ui";
import { ModelTrendChart } from "../components/ModelTrendChart";
import { PageHeader } from "../components/PageHeader";
import { ProviderIcon } from "../components/ProviderIcon";
import { UsageChart } from "../components/UsageChart";
import type { Provider, UsageEventRow } from "../api/types";
import {
  Button,
  Empty,
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
  Tabs,
} from "../ui";

const RANGES = [
  { value: "24h", label: "最近 24 小时", seconds: 86400 },
  { value: "7d", label: "最近 7 天", seconds: 7 * 86400 },
  { value: "30d", label: "最近 30 天", seconds: 30 * 86400 },
  { value: "all", label: "全部", seconds: 0 },
];

const PROVIDER_LABEL: Record<Provider, string> = { codebuddy: "CodeBuddy", trae: "TRAE" };

/** 明细分页的可选每页条数。 */
const PAGE_SIZES = [10, 20, 50, 100];

/** 明细状态列：成功固定文案；失败展示受控错误类型（脱敏，不含原始错误体）。 */
function statusCell(row: UsageEventRow) {
  if (row.ok) {
    return <span className="text-ok">成功</span>;
  }
  return <span className="text-destructive">{row.error_type ?? "失败"}</span>;
}

export function StatsPage() {
  const session = useSessionContext();
  const [range, setRange] = useState("7d");
  // 明细分页：cursors[i] = 第 i 页的 before 游标（第 0 页为 undefined）；
  // rowid 单调递增，游标栈支持双向翻页且不漏不重
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [cursors, setCursors] = useState<(number | undefined)[]>([undefined]);

  const seconds = RANGES.find((item) => item.value === range)?.seconds ?? 0;
  // since 必须锚定：若每次渲染都重算 Date.now()-seconds，值会随时间漂移，
  // 导致 queryKey 抖动重复请求，且「范围变化回第一页」的 effect 把翻页打回第一页
  const since = useMemo(
    () => (seconds > 0 ? Math.floor(Date.now() / 1000) - seconds : undefined),
    [seconds],
  );

  const events = useStatsEvents(
    session.username, since, cursors[Math.min(pageIndex, cursors.length - 1)], pageSize);
  const eventRows = events.data?.events ?? [];
  const hasNextPage = events.data ? events.data.next_before !== null : false;

  // 切时间范围 = 换了数据集：事件驱动重置分页（不监听 since 派生值，
  // 否则值随时间漂移会把正常翻页误重置）
  const changeRange = (value: string) => {
    setRange(value);
    setPageIndex(0);
    setCursors([undefined]);
  };

  const changePageSize = (size: number) => {
    setPageSize(size);
    setPageIndex(0);
    setCursors([undefined]);
  };

  const overview = useStatsOverview(session.username, undefined, since);
  const byProvider = useStatsByProvider(session.username, undefined, since);
  const timeline = useStatsTimeline(session.username, undefined, since);
  const modelTimeline = useStatsModelTimeline(session.username, undefined, since);

  if (overview.isLoading) {
    return (
      <div className="space-y-6" data-testid="stats-page">
        <p className="sr-only">载入中…</p>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
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
  const rateText = rate === null ? "—" : `${(rate * 100).toFixed(1)}%`;
  const totalTokens = (stats?.input_tokens ?? 0) + (stats?.output_tokens ?? 0);
  // 输入缓存：命中 = 上报的 cached_tokens；未命中 = 输入 − 命中（缺上报则显示 —）
  const cached = stats?.cached_tokens ?? null;
  const hitText = cached === null ? "—" : formatCompact(cached);
  const missText =
    cached === null || stats?.input_tokens === null || stats?.input_tokens === undefined
      ? "—"
      : formatCompact(stats.input_tokens - cached);
  const tokenHint =
    `输入 ${formatCompact(stats?.input_tokens)}（命中 ${hitText} · 未命中 ${missText}）`
    + ` · 输出 ${formatCompact(stats?.output_tokens)} · 推理 ${formatCompact(stats?.reasoning_tokens)}`;

  return (
    <div className="space-y-6" data-testid="stats-page">
      <PageHeader
        title="用量统计"
        description="按时间范围查看请求量、成功率与 token 消耗；数据按 API Key 归属用户统计，普通用户只能看自己。"
      />

      <Panel title="筛选">
        {/* 「时间范围」label 后紧接 tabs（左右相邻，不拉开两端） */}
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm font-medium text-muted-foreground">时间范围</span>
          <Tabs
            value={range}
            options={RANGES.map(({ value, label }) => ({ value, label }))}
            onChange={changeRange}
            testId="range-tabs"
          />
        </div>
      </Panel>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Metric label="请求数" value={formatNumber(stats?.requests)} tone="ok"
                hint={`成功率 ${rateText}`} />
        <Metric
          label="Token 消耗"
          value={formatCompact(totalTokens)}
          hint={tokenHint}
        />
        <Metric
          label="平均耗时"
          value={formatLatency(stats?.avg_latency_ms)}
          hint={`首字延迟 ${formatLatency(stats?.avg_ttfb_ms)}`}
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
                  <TableCell className="text-right tabular-nums">{formatCompact(row.input_tokens)}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatCompact(row.output_tokens)}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {row.credit === null ? "—" : formatNumber(Number(row.credit.toFixed(2)))}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      <Panel title="请求明细" action={
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span>共 {formatNumber(overview.data?.requests)} 条 · 保留 90 天</span>
          <label className="flex items-center gap-1.5">
            每页
            <Select
              value={String(pageSize)}
              data-testid="events-page-size"
              className="h-7 w-20 text-xs"
              onChange={(event) => changePageSize(Number(event.target.value))}
            >
              {PAGE_SIZES.map((size) => (
                <option key={size} value={size}>{size} 条</option>
              ))}
            </Select>
          </label>
        </div>
      }>
        {events.isError ? (
          <Notice tone="danger" data-testid="events-error">
            请求明细加载失败，请刷新重试；若刚升级服务，请确认后端已重启加载新端点。
          </Notice>
        ) : eventRows.length === 0 ? (
          <Empty data-testid="no-events">该范围内没有请求明细</Empty>
        ) : (
          <>
            <Table data-testid="events-table">
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  {session.is_admin && <TableHead>用户</TableHead>}
                  <TableHead>上游</TableHead>
                  <TableHead>模型</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead className="text-right">输入</TableHead>
                  <TableHead className="text-right">输出</TableHead>
                  <TableHead className="text-right">命中</TableHead>
                  <TableHead className="text-right">Credit</TableHead>
                  <TableHead className="text-right">首字延迟</TableHead>
                  <TableHead className="text-right">耗时</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {eventRows.map((row) => (
                  <TableRow key={row.rowid} data-testid="event-row">
                    <TableCell className="whitespace-nowrap tabular-nums">
                      {formatTime(row.ts)}
                    </TableCell>
                    {session.is_admin && <TableCell>{row.username}</TableCell>}
                    <TableCell>
                      <span className="inline-flex items-center gap-1.5">
                        <ProviderIcon provider={row.provider as Provider} size={13} />
                        {PROVIDER_LABEL[row.provider as Provider] ?? row.provider}
                      </span>
                    </TableCell>
                    <TableCell className="max-w-48 truncate" title={row.model}>{row.model}</TableCell>
                    <TableCell>{statusCell(row)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatCompact(row.input_tokens)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatCompact(row.output_tokens)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatCompact(row.cached_tokens)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {row.credit === null ? "—" : formatNumber(Number(row.credit.toFixed(2)))}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{formatLatency(row.ttfb_ms)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatLatency(row.latency_ms)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {eventRows.length > 0 && (
              <div className="mt-3 flex items-center justify-between">
                <span className="text-xs text-muted-foreground" data-testid="events-page-info">
                  第 {pageIndex + 1} 页
                </span>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="ghost"
                    data-testid="events-prev"
                    disabled={pageIndex === 0 || events.isFetching}
                    onClick={() => setPageIndex((p) => Math.max(0, p - 1))}
                  >
                    上一页
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    data-testid="events-next"
                    disabled={!hasNextPage || events.isFetching}
                    onClick={() => {
                      const nextBefore = events.data?.next_before;
                      if (nextBefore === null || nextBefore === undefined) return;
                      setCursors((prev) => {
                        const next = prev.slice();
                        next[pageIndex + 1] = nextBefore;
                        return next;
                      });
                      setPageIndex((p) => p + 1);
                    }}
                  >
                    下一页
                  </Button>
                </div>
              </div>
            )}          </>
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