import { useMemo, useState } from "react";
import { Activity, BarChart3, Coins, CreditCard, Gauge, Timer, Wallet } from "lucide-react";
import { useSessionContext } from "../Layout";
import { useSort } from "../hooks/useSort";
import {
  useStatsByProvider,
  useStatsByModel,
  useStatsByUser,
  useStatsByCredential,
  useStatsEvents,
  useStatsModelTimeline,
  useStatsOverview,
  useStatsTimeline,
} from "../api/hooks";
import { formatCacheRate, formatCompact, formatCredit, formatLatency, formatMoney, formatNumber, formatTime, usageErrorLabel } from "../api/display";
import { Notice } from "../ui";
import { ModelTrendChart } from "../components/ModelTrendChart";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { ProviderIcon } from "../components/ProviderIcon";
import { SortableHead } from "../components/SortableHead";
import { UsageChart, chartProviders } from "../components/UsageChart";
import { providerLabel } from "../api/providers";
import type { GroupStats, UsageEventRow } from "../api/types";
import {
  Button,
  Empty,
  Metric,
  Panel,
  Select,
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

/** 图表指标筛选：默认请求次数；均值类（耗时/首字）后端按成功请求归一。 */
const METRICS = [
  { value: "requests", label: "请求次数", icon: <BarChart3 className="size-3.5" /> },
  { value: "tokens", label: "Token", icon: <Coins className="size-3.5" /> },
  { value: "cost", label: "成本", icon: <Wallet className="size-3.5" /> },
  { value: "latency", label: "耗时", icon: <Timer className="size-3.5" /> },
  { value: "ttfb", label: "首字延迟", icon: <Gauge className="size-3.5" /> },
];

/** 明细分页的可选每页条数。 */
const PAGE_SIZES = [10, 20, 50, 100];

/**
 * 分组统计的维度。后端各维度的行结构一致（分组键 + 同一组公共指标），
 * 前端按 key 取出分组键列即可，表格本身不分叉。
 *
 * 用户/凭证维度标记 operatorOnly：viewer 只能看自己，恒是「我」一行，
 * 给这个 tab 只是噪音。
 */
const GROUP_DIMENSIONS = [
  { value: "provider", label: "按渠道", groupKey: "provider", label2: "渠道", operatorOnly: false },
  { value: "model", label: "按模型", groupKey: "model", label2: "模型", operatorOnly: false },
  { value: "user", label: "按用户", groupKey: "username", label2: "用户", operatorOnly: true },
  { value: "credential", label: "按凭证", groupKey: "credential_id", label2: "凭证", operatorOnly: true },
] as const;

/** 公共指标 + 至多一个分组键（键名随维度变化，用可选字段统一承载）。 */
type GroupRow = GroupStats & {
  provider?: string;
  model?: string;
  username?: string;
  credential_id?: string;
  credential_name?: string | null;
};

/** 凭证文案：昵称优先，回落 ID 前 12 位（ID 很长）。分组表与明细表共用，
 *  保证同一凭证在两处显示成同一个名字。 */
function credentialCellLabel(name: string | null | undefined, id: string | null | undefined) {
  return name ?? (id ? id.slice(0, 12) : "—");
}

/** 明细状态列：成功固定文案；失败展示受控错误类型的中文说明（脱敏，不含原始错误体）。 */
function statusCell(row: UsageEventRow) {
  if (row.ok) {
    return <span className="text-ok">成功</span>;
  }
  return <span className="text-destructive">{usageErrorLabel(row.error_type)}</span>;
}

export function StatsPage() {
  const session = useSessionContext();
  const [range, setRange] = useState("7d");
  const [metric, setMetric] = useState("requests");
  const [groupDimension, setGroupDimension] = useState("provider");
  // 分组统计默认按请求数降序（「用得最多」才是分组表的用途）
  const groupSort = useSort("requests", "desc", { group: "asc" });
  // 明细默认按时间（rowid）降序 → 走游标分页；换列则切 offset 分页
  const eventSort = useSort("time", "desc");
  // 明细分页：cursors[i] = 第 i 页的 before 游标（第 0 页为 undefined）；
  // rowid 单调递增，游标栈支持双向翻页且不漏不重
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [cursors, setCursors] = useState<(number | undefined)[]>([undefined]);
  // 游标分页只在「按 rowid 降序」时成立：换列即改用 offset 分页
  const cursorMode = eventSort.sort === "time" && eventSort.order === "desc";
  const eventBefore = cursorMode
    ? cursors[Math.min(pageIndex, cursors.length - 1)]
    : undefined;
  // cursor 模式不发 offset（省一个无意义参数，也让后端走游标分支更明确）
  const eventOffset = cursorMode ? undefined : pageIndex * pageSize;

  const seconds = RANGES.find((item) => item.value === range)?.seconds ?? 0;
  // since 必须锚定：若每次渲染都重算 Date.now()-seconds，值会随时间漂移，
  // 导致 queryKey 抖动重复请求，且「范围变化回第一页」的 effect 把翻页打回第一页
  const since = useMemo(
    () => (seconds > 0 ? Math.floor(Date.now() / 1000) - seconds : undefined),
    [seconds],
  );

  const events = useStatsEvents(
    session.username, since, eventBefore, pageSize,
    eventSort.sort, eventSort.order, eventOffset);
  const eventRows = events.data?.events ?? [];
  // 游标模式看 next_before；offset 模式看 total（还有下一页 = 已加载 < 总数）
  const hasNextPage = cursorMode
    ? events.data
      ? events.data.next_before !== null
      : false
    : eventRows.length + pageIndex * pageSize < (events.data?.total ?? 0);

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

  const toggleEventSort = (columnKey: string) => {
    eventSort.toggle(columnKey);
    setPageIndex(0);
    setCursors([undefined]);
  };

  const overview = useStatsOverview(session.username, undefined, since);
  const byProvider = useStatsByProvider(
    session.username, undefined, since, groupSort.sort, groupSort.order);
  const byModel = useStatsByModel(
    session.username, undefined, since, groupSort.sort, groupSort.order);
  const byUser = useStatsByUser(
    session.username, undefined, since, groupSort.sort, groupSort.order);
  const byCredential = useStatsByCredential(
    session.username, undefined, since, groupSort.sort, groupSort.order);
  const timeline = useStatsTimeline(session.username, undefined, since, metric);
  const modelTimeline = useStatsModelTimeline(session.username, undefined, since, metric);

  // 四个维度一次性取数（各维度行结构一致），切 tab 只换渲染、不再发请求；
  // viewer 看不到用户/凭证 tab，若状态残留则回落到渠道维度
  const groupDimensions = GROUP_DIMENSIONS.filter(
    (item) => session.is_operator || !item.operatorOnly);
  const group = groupDimensions.find((item) => item.value === groupDimension)
    ?? groupDimensions[0];
  const groupKey = group.groupKey;
  const groupLabel = group.label2;
  const groupRows: GroupRow[] = group.value === "provider"
    ? (byProvider.data?.providers ?? [])
    : group.value === "model"
      ? (byModel.data?.models ?? [])
      : group.value === "user"
        ? (byUser.data?.users ?? [])
        : (byCredential.data?.credentials ?? []);

  if (overview.isLoading) {
    return (
      <div data-testid="stats-page">
        <PageSkeleton variant="chart" rows={4} />
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
    + ` · 输出 ${formatCompact(stats?.output_tokens)} · 推理 ${formatCompact(stats?.reasoning_tokens)}`
    + ` · 缓存命中率 ${formatCacheRate(cached, stats?.input_tokens)}`;

  return (
    <div className="space-y-6" data-testid="stats-page">
      <PageHeader
        eyebrow="控制台"
        title="用量统计"
        description="按时间范围查看请求量、成功率与 token 消耗；数据按 API Key 归属用户统计，管理员与操作员可看全量，只读用户只能看自己。"
        icon={<BarChart3 className="size-5" />}
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

      <section
        aria-label="用量概览统计"
        className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5"
      >
        <Metric label="请求数" value={formatNumber(stats?.requests)} tone="ok"
                hint={`成功率 ${rateText}`} icon={<Activity className="size-4" />} />
        <Metric
          label="Token 消耗"
          value={formatCompact(totalTokens)}
          hint={tokenHint}
          icon={<Coins className="size-4" />}
        />
        <Metric
          label="平均耗时"
          value={formatLatency(stats?.avg_latency_ms)}
          hint={`首字延迟 ${formatLatency(stats?.avg_ttfb_ms)}`}
          icon={<Timer className="size-4" />}
        />
        <Metric
          label="Credit 消耗"
          value={formatCredit(stats?.credit, stats?.credit_estimated)}
          hint="渠道可选字段，可能不返回；≈ 为推算值（TRAE 按单价、CodeArts 福利模型按每日池）"
          icon={<CreditCard className="size-4" />}
        />
        <Metric
          label="成本（估算）"
          value={formatMoney(stats?.cost_cny, "CNY")}
          hint={`美元 ${formatMoney(stats?.cost_usd, "USD")} · 按 models.dev 刊例价折算，未匹配定价的请求不计`}
          icon={<Wallet className="size-4" />}
        />
      </section>

      <section className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-muted-foreground">图表指标</h2>
        <Tabs
          value={metric}
          options={METRICS.map(({ value, label }) => ({ value, label }))}
          onChange={setMetric}
          testId="metric-tabs"
        />
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Panel title="请求量趋势" action={
          <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
            {chartProviders(timeline.data?.points ?? []).map((provider) => (
              <span key={provider} className="inline-flex items-center gap-1.5">
                <ProviderIcon provider={provider} size={12} />
                {providerLabel(provider)}
              </span>
            ))}
          </div>
        }>
          {(timeline.data?.points.length ?? 0) === 0 ? (
            <Empty>该范围内没有小时汇总数据</Empty>
          ) : (
            <UsageChart points={timeline.data?.points ?? []} metric={metric} />
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
              metric={metric}
            />
          )}
        </Panel>
      </section>

      <Panel title="分组统计" action={
        <Tabs
          value={groupDimension}
          options={groupDimensions.map(({ value, label }) => ({ value, label }))}
          onChange={setGroupDimension}
          testId="group-tabs"
        />
      }>
        {groupRows.length === 0 ? (
          <Empty data-testid="no-group-stats">该范围内没有请求</Empty>
        ) : (
          <Table data-testid="group-table">
            <TableHeader>
              <TableRow>
                <SortableHead label={group.value === "provider" ? "渠道" : groupLabel}
                              columnKey="group" active={groupSort.sort === "group"}
                              direction={groupSort.order} onToggle={groupSort.toggle}
                              testId="sort-group" />
                <SortableHead label="请求数" columnKey="requests"
                              active={groupSort.sort === "requests"} direction={groupSort.order}
                              onToggle={groupSort.toggle} align="right" testId="sort-requests" />
                <SortableHead label="成功数" columnKey="ok_count"
                              active={groupSort.sort === "ok_count"} direction={groupSort.order}
                              onToggle={groupSort.toggle} align="right" testId="sort-ok_count" />
                <TableHead className="text-right">成功率</TableHead>
                <SortableHead label="输入 token" columnKey="input_tokens"
                              active={groupSort.sort === "input_tokens"}
                              direction={groupSort.order} onToggle={groupSort.toggle}
                              align="right" testId="sort-input_tokens" />
                <SortableHead label="输出 token" columnKey="output_tokens"
                              active={groupSort.sort === "output_tokens"}
                              direction={groupSort.order} onToggle={groupSort.toggle}
                              align="right" testId="sort-output_tokens" />
                <SortableHead label="命中缓存" columnKey="cached_tokens"
                              active={groupSort.sort === "cached_tokens"}
                              direction={groupSort.order} onToggle={groupSort.toggle}
                              align="right" testId="sort-cached_tokens" />
                <SortableHead label="Credit" columnKey="credit"
                              active={groupSort.sort === "credit"} direction={groupSort.order}
                              onToggle={groupSort.toggle} align="right" testId="sort-credit" />
                <SortableHead label="成本" columnKey="cost_cny"
                              active={groupSort.sort === "cost_cny"} direction={groupSort.order}
                              onToggle={groupSort.toggle} align="right" testId="sort-cost_cny" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {groupRows.map((row) => (
                <TableRow key={String(row[groupKey])}>
                  <TableCell className="max-w-48 truncate"
                             title={groupKey === "credential_id"
                               ? (row.credential_id ?? undefined)
                               : String(row[groupKey])}>
                    {groupKey === "provider" ? (
                      <span className="inline-flex items-center gap-1.5">
                        <ProviderIcon provider={String(row.provider)} size={13} />
                        {providerLabel(String(row.provider))}
                      </span>
                    ) : groupKey === "credential_id" ? (
                      // 渠道 icon + 凭证名：与请求明细的凭证列同口径
                      <span className="inline-flex items-center gap-1.5">
                        <ProviderIcon provider={String(row.provider)} size={13} />
                        {credentialCellLabel(row.credential_name, row.credential_id)}
                      </span>
                    ) : (
                      String(row[groupKey])
                    )}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{formatNumber(row.requests)}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatNumber(row.ok_count)}</TableCell>
                  {/* 成功率：请求数为 0 无意义（不渲染 0% 冒充数据） */}
                  <TableCell className="text-right tabular-nums" data-testid="group-success-rate">
                    {row.requests > 0
                      ? `${((row.ok_count / row.requests) * 100).toFixed(1)}%`
                      : "—"}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{formatCompact(row.input_tokens)}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatCompact(row.output_tokens)}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatCompact(row.cached_tokens)}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatCredit(row.credit, row.credit_estimated)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums"
                             title={`美元 ${formatMoney(row.cost_usd, "USD")}`}>
                    {formatMoney(row.cost_cny, "CNY")}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          {groupKey === "credential_id"
            ? "凭证分组取自逐请求明细（仅保留 90 天），无凭证的预热失败等请求不计入。"
            : "各维度数据源为小时汇总（永久保留），最近 5 分钟内的请求可能尚未计入。"}
        </p>
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
                  <SortableHead label="时间" columnKey="ts" active={eventSort.sort === "ts"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-ts" />
                  {session.is_operator && (
                    <SortableHead label="用户" columnKey="username"
                                  active={eventSort.sort === "username"}
                                  direction={eventSort.order} onToggle={toggleEventSort}
                                  testId="event-sort-username" />
                  )}
                  <SortableHead label="渠道" columnKey="provider"
                                active={eventSort.sort === "provider"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-provider" />
                  <SortableHead label="凭证" columnKey="credential"
                                active={eventSort.sort === "credential"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-credential" />
                  <SortableHead label="模型" columnKey="model" active={eventSort.sort === "model"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-model" />
                  <SortableHead label="状态" columnKey="ok" active={eventSort.sort === "ok"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-ok" />
                  <SortableHead label="输入" columnKey="input_tokens"
                                active={eventSort.sort === "input_tokens"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-input_tokens" />
                  <SortableHead label="输出" columnKey="output_tokens"
                                active={eventSort.sort === "output_tokens"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-output_tokens" />
                  <SortableHead label="命中" columnKey="cached_tokens"
                                active={eventSort.sort === "cached_tokens"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-cached_tokens" />
                  <SortableHead label="Credit" columnKey="credit"
                                active={eventSort.sort === "credit"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-credit" />
                  <SortableHead label="成本" columnKey="cost_cny"
                                active={eventSort.sort === "cost_cny"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-cost_cny" />
                  <SortableHead label="首字延迟" columnKey="ttfb_ms"
                                active={eventSort.sort === "ttfb_ms"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-ttfb_ms" />
                  <SortableHead label="耗时" columnKey="latency_ms"
                                active={eventSort.sort === "latency_ms"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-latency_ms" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {eventRows.map((row) => (
                  <TableRow key={row.rowid} data-testid="event-row">
                    <TableCell className="whitespace-nowrap tabular-nums">
                      {formatTime(row.ts)}
                    </TableCell>
                    {session.is_operator && <TableCell>{row.username}</TableCell>}
                    <TableCell>
                      <span className="inline-flex items-center gap-1.5">
                        <ProviderIcon provider={row.provider} size={13} />
                        {providerLabel(row.provider)}
                      </span>
                    </TableCell>
                    <TableCell className="max-w-40 truncate" title={row.credential_id ?? undefined}>
                      {credentialCellLabel(row.credential_name, row.credential_id)}
                    </TableCell>
                    <TableCell className="max-w-48 truncate" title={row.model}>{row.model}</TableCell>
                    <TableCell>{statusCell(row)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatCompact(row.input_tokens)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatCompact(row.output_tokens)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatCompact(row.cached_tokens)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatCredit(row.credit, row.credit_estimated)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums"
                               title={`美元 ${formatMoney(row.cost_usd, "USD")}`}>
                      {formatMoney(row.cost_cny, "CNY")}
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
                      if (cursorMode) {
                        const nextBefore = events.data?.next_before;
                        if (nextBefore === null || nextBefore === undefined) return;
                        setCursors((prev) => {
                          const next = prev.slice();
                          next[pageIndex + 1] = nextBefore;
                          return next;
                        });
                      }
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

      <Notice tone="muted" className="items-start gap-1 px-3 py-2.5 text-xs">
        <p>
          隐私：不保存提示词、回答、请求头、Token、工具参数与原始错误体；逐请求明细保留 90 天，小时汇总永久保留；按 API Key 归属用户统计。
        </p>
        <p>
          credit 为渠道可选字段，经常不返回；TRAE 上游不给单请求积分，带 ≈ 的数值是按官方单价折算；CodeArts 福利模型按每日 token 池 1:1 扣减后折成积分（1 积分 = 10000 token），带 ≈ 的值即该请求折算后的积分（CodeBuddy 为上游返回的真值）。健康度只依赖额度探测接口，主指标是 token 数。缓存命中率 = 命中 token ÷ 输入 token，仅在上报过缓存时展示。
        </p>
        <p>
          成本为估算：按 models.dev 的模型刊例价（USD / 百万 token）与本服务写入时生效的美元汇率折算，展示人民币、括号内为美元；不是上游真实扣费，未匹配到定价的模型不计入（显示 —）。汇率可在「任务与配置」页热更，仅影响之后写入的请求，历史成本不重算。
        </p>
      </Notice>
    </div>
  );
}