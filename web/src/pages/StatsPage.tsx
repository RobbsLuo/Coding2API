import { useMemo, useState } from "react";
import { BarChart3 } from "lucide-react";
import { useSessionContext } from "../Layout";
import { useSort } from "../hooks/useSort";
import {
  useStatsByProvider,
  useStatsByModel,
  useStatsByUser,
  useStatsByCredential,
  useStatsEventStream,
  useStatsEvents,
  useStatsModelTimeline,
  useStatsOverview,
  useStatsPercentileTimeline,
  useStatsTimeline,
} from "../api/hooks";
import { useStatsErrors } from "../api/statsErrors";
import { modelTrendAxis } from "../components/ModelTrendChart";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { chartProviders, hasData } from "../components/UsageChart";
import { providerLabel } from "../api/providers";
import { api } from "../api/client";
import { buildCustomRange, toLocalInput } from "../lib/customRange";
import type { StatsEventFilters, UsageEventRow } from "../api/types";
import {
  GROUP_DIMENSIONS,
  LATENCY_METRICS,
  LIVE_MAX_ROWS,
  RANGES,
  type GroupRow,
  type PageDrill,
  type Percentile,
} from "./stats/constants";
import { AuxCharts } from "./stats/AuxCharts";
import { CompressPanel } from "./stats/CompressPanel";
import { EventsPanel } from "./stats/EventsPanel";
import { FailurePanel } from "./stats/FailurePanel";
import { GroupStatsPanel } from "./stats/GroupStatsPanel";
import { PrivacyNotice } from "./stats/PrivacyNotice";
import { StatsFilters } from "./stats/StatsFilters";
import { StatsOverviewCards } from "./stats/StatsOverviewCards";
import { TrendChartPanel } from "./stats/TrendChartPanel";

export function StatsPage() {
  const session = useSessionContext();
  // 默认最近 24 小时：「这个池子现在怎么样」是打开用量统计的第一问题，
  // 7 天会把当天的波动摊平（与凭证池页的实时气质一致）。
  const [range, setRange] = useState("24h");
  // 自定义范围的原始输入（`datetime-local` 文本）。只在 range==="custom" 时参与取数。
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [metric, setMetric] = useState("requests");
  // 主图口径：默认「全部渠道」合计；切到某渠道则只看那一条曲线。
  // 不做多系列叠加（7 天窗口下会互相遮挡），一次只画一条。
  const [chartScope, setChartScope] = useState<string>("total");
  const [groupDimension, setGroupDimension] = useState("provider");
  // 分组统计默认按请求数降序（「用得最多」才是分组表的用途）
  const groupSort = useSort("requests", "desc", { group: "asc" });
  // 明细默认按时间（rowid）降序 → 走游标分页；换列则切 offset 分页
  const eventSort = useSort("time", "desc");
  // 明细分页：cursors[i] = 第 i 页的 before 游标（第 0 页为 undefined）；
  // rowid 单调递增，游标栈支持双向翻页且不漏不重。
  // 默认 10 条：明细行是两行文本（时间+日期、凭证+渠道），20 条就是 1140px，
  // 把页面拉到近 4 屏。看明细通常是「扫最近几条」，需要更多可切 50/100。
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(10);
  const [cursors, setCursors] = useState<(number | undefined)[]>([undefined]);
  // 分组表分位档：默认 P95（既有观感）；四档后端一次算好，切换只换展示列
  const [percentile, setPercentile] = useState<Percentile>("p95");
  // 钻取：点分组表的行 → 只看该渠道/模型/用户/凭证的明细。空对象 = 未钻取。
  const [drill, setDrill] = useState<PageDrill>({});
  // 导出反馈（行数/截断/失败），一次性展示，再次导出即覆盖
  const [exportNotice, setExportNotice] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  // 实时流：开关打开后用 SSE 订阅「刚落库」的明细（与翻页结果分开展示，
  // 避免把「实时行」和「历史分页」混成一张会跳动的表）。
  const [live, setLive] = useState(false);
  const [liveRows, setLiveRows] = useState<UsageEventRow[]>([]);
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
  // 自定义范围：解析失败 / 起点≥终点时为 null（端点会提示，且不参与取数）
  const custom = useMemo(
    () => (range === "custom" ? buildCustomRange(customFrom, customTo) : null),
    [range, customFrom, customTo],
  );
  // 自定义模式下以解析结果为准；否则用预设范围的 since（无 until）
  const effectiveSince = range === "custom" ? custom?.since : since;
  const effectiveUntil = range === "custom" ? custom?.until : undefined;
  // 自定义输入非法（两个框都填了却解析不出）时，提示用户而不是静默退回全量
  const customInvalid = range === "custom" && custom === null
    && (customFrom.trim() !== "" || customTo.trim() !== "");
  // 自定义范围非法时**暂停取数**：effectiveSince/Until 此时都是 undefined，
  // 直接发出去等于「不限定时间窗」的全量统计——既与页面上「已暂停取数」的
  // 文案矛盾，又会在用户逐字编辑输入框时反复触发无时间窗的全表扫描。
  const rangeReady = !customInvalid;

  const events = useStatsEvents(
    drill.username, effectiveSince, effectiveUntil, eventBefore, pageSize,
    eventSort.sort, eventSort.order, eventOffset,
    { provider: drill.provider, model: drill.model,
      credential_id: drill.credential_id, ok: drill.ok },
    rangeReady);
  const eventRows = events.data?.events ?? [];
  // 实时流：只保留最近 50 条（新→旧）。缓冲由前端自己管：页面长时间开着时
  // 不无限增长，且「刚发生的」永远在最上面。
  const liveStatus = useStatsEventStream({
    enabled: live,
    username: drill.username,
    filters: { provider: drill.provider, model: drill.model,
               credential_id: drill.credential_id, ok: drill.ok },
    onEvent: (row) => {
      setLiveRows((prev) => [row, ...prev].slice(0, LIVE_MAX_ROWS));
    },
  });
  const toggleLive = (value: boolean) => {
    setLive(value);
    // 关掉再打开 = 重新开始看「现在」：清空旧行，避免混进上一段的记录
    if (!value) setLiveRows([]);
  };
  // 游标模式看 next_before；offset 模式看 total（还有下一页 = 已加载 < 总数）
  const hasNextPage = cursorMode
    ? events.data
      ? events.data.next_before !== null
      : false
    : eventRows.length + pageIndex * pageSize < (events.data?.total ?? 0);

  // 切时间范围 = 换了数据集：事件驱动重置分页（不监听 since 派生值，
  // 否则值随时间漂移会把正常翻页误重置）
  const changeRange = (value: string) => {
    // 首次切到自定义且两个框都空：预填「最近 24 小时」，否则会先按全量查一次
    if (value === "custom" && !customFrom && !customTo) {
      const now = new Date();
      const start = new Date(now.getTime() - 86400 * 1000);
      setCustomFrom(toLocalInput(start));
      setCustomTo(toLocalInput(now));
    }
    setRange(value);
    setPageIndex(0);
    setCursors([undefined]);
  };

  /** 自定义时间窗逐字编辑同样算「换数据集」：回第一页并清游标栈。 */
  const changeCustomFrom = (value: string) => {
    setCustomFrom(value);
    setPageIndex(0);
    setCursors([undefined]);
  };

  const changeCustomTo = (value: string) => {
    setCustomTo(value);
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

  /**
   * 应用钻取：**合并**而非替换——分组表各维度独立（渠道/模型/用户/凭证），
   * 依次点行即可层层收窄（provider=trae AND model=glm-5.2），这正是钻取
   * 相对「跳转看全量」的价值。换过滤即换数据集，分页回到第一页。
   */
  const applyDrill = (next: PageDrill) => {
    setDrill((prev) => ({ ...prev, ...next }));
    setPageIndex(0);
    setCursors([undefined]);
  };

  const clearDrill = () => {
    setDrill({});
    setPageIndex(0);
    setCursors([undefined]);
  };

  const drillLabels: string[] = [];
  if (drill.username) drillLabels.push(`用户：${drill.username}`);
  if (drill.provider) drillLabels.push(`渠道：${providerLabel(drill.provider)}`);
  if (drill.model) drillLabels.push(`模型：${drill.model}`);
  if (drill.credential_id) drillLabels.push(`凭证：${drill.credential_id.slice(0, 12)}`);
  if (drill.ok !== undefined) drillLabels.push(drill.ok === 0 ? "只看失败" : "只看成功");

  const exportCsv = async () => {
    setExporting(true);
    setExportNotice(null);
    try {
      const { blob, rowCount, truncated } = await api.statsEventsCsv(
        drill.username, effectiveSince, effectiveUntil,
        // `ok` 由 client 展开进 query（类型层面额外属性）
        { provider: drill.provider, model: drill.model,
          credential_id: drill.credential_id, ok: drill.ok } as StatsEventFilters);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `coding2api-events-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setExportNotice(truncated
        ? `已导出 ${rowCount} 条，达到上限被截断；请缩小时间范围或加筛选后重试。`
        : `已导出 ${rowCount} 条明细。`);
    } catch (error) {
      setExportNotice(error instanceof Error ? `导出失败：${error.message}` : "导出失败。");
    } finally {
      setExporting(false);
    }
  };

  // viewer 看不到用户/凭证 tab：状态残留时回落到渠道维度（与下面 `group` 解析同口径），
  // 否则 enabled 会判给一个根本不渲染的维度，分组表永远是空表。
  const effectiveGroupDimension = session.is_operator
    || GROUP_DIMENSIONS.some(
      (item) => !item.operatorOnly && item.value === groupDimension)
    ? groupDimension
    : "provider";
  const overview = useStatsOverview(session.username, undefined, effectiveSince,
                                    effectiveUntil, rangeReady);
  // 四个维度各是一次分组聚合（多数还要扫明细表）：只取当前 tab 那一维，
  // 否则打开页面就并发 7–8 个重查询，而其中三个的结果根本不渲染。
  // 例外是渠道维度：主图底部的「渠道构成」在任何 tab 下都展示，请求量指标下仍需要它
  // （`showShare` 依赖 `byProvider`，禁用会让占比块在有数据时也不出现）。
  const byProvider = useStatsByProvider(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order,
    rangeReady && (effectiveGroupDimension === "provider" || metric === "requests"));
  const byModel = useStatsByModel(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order, rangeReady && effectiveGroupDimension === "model");
  const byUser = useStatsByUser(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order, rangeReady && effectiveGroupDimension === "user");
  const byCredential = useStatsByCredential(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order, rangeReady && effectiveGroupDimension === "credential");
  // 渠道构成卡片：只在请求量指标下出现（份额需要「请求数」这个分母）
  const showShare = metric === "requests"
    && (byProvider.data?.providers.length ?? 0) > 0;
  const timeline = useStatsTimeline(session.username, undefined, effectiveSince,
                                    effectiveUntil, metric, rangeReady);
  const modelTimeline = useStatsModelTimeline(session.username, undefined, effectiveSince,
                                              effectiveUntil, metric, rangeReady);
  // 延迟分位只在延迟类指标下取数（其余指标用不上，省一次明细表扫描）
  const latencyPercentile = useStatsPercentileTimeline(
    session.username, undefined, effectiveSince, effectiveUntil,
    metric === "ttfb" ? "ttfb" : "latency", LATENCY_METRICS.has(metric));
  // 失败归因：端点仅 operator+（viewer 打开会 403），故按角色禁用查询
  const errors = useStatsErrors(session.username, effectiveSince, effectiveUntil,
                                undefined, session.is_operator);
  const errorRows = errors.data?.errors ?? [];

  // 四个维度各取一维（各维度行结构一致），切 tab 才换查询、不再为不渲染的
  // 维度白扫一遍明细表；viewer 看不到用户/凭证 tab，若状态残留则回落到渠道维度
  const groupDimensions = GROUP_DIMENSIONS.filter(
    (item) => session.is_operator || !item.operatorOnly);
  const group = groupDimensions.find((item) => item.value === groupDimension)
    ?? groupDimensions[0];
  const groupRows: GroupRow[] = group.value === "provider"
    ? (byProvider.data?.providers ?? [])
    : group.value === "model"
      ? (byModel.data?.models ?? [])
      : group.value === "user"
        ? (byUser.data?.users ?? [])
        : (byCredential.data?.credentials ?? []);

  // 主图的渠道切换候选：只列窗口内真有数据的渠道（全 0 / 全空的渠道不进选项，
  // 否则切过去是一张空图）
  const timelineProviders = useMemo(() => {
    const points = timeline.data?.points ?? [];
    return chartProviders(points).filter((provider) => hasData(points, provider));
  }, [timeline.data]);
  // 切走的渠道可能在新窗口里没有数据：此时回落到「全部渠道」，不留空白图
  const effectiveScope = chartScope !== "total" && !timelineProviders.includes(chartScope)
    ? "total"
    : chartScope;
  // 模型趋势的 Y 轴口径：图与图例说明共用一份（比率截断时必须在图例标注）
  const modelAxis = useMemo(() => modelTrendAxis(
    modelTimeline.data?.points ?? [],
    modelTimeline.data?.models ?? [],
    metric,
  ), [modelTimeline.data, metric]);

  if (overview.isLoading) {
    return (
      <div data-testid="stats-page">
        <PageSkeleton variant="chart" rows={4} />
      </div>
    );
  }
  // 总览请求失败：显式错误态。不判 isError 的话，失败会被渲染成「0 请求 / —」
  // 的正常空数据——用户以为真的没有用量，而故障被完全吞掉。
  const overviewFailed = overview.isError;
  const stats = overview.data;

  /** 游标分页的翻页动作：向下翻页时先把本页 next_before 压栈，下一页才有游标可发。 */
  const changePage = (next: number) => {
    if (next > pageIndex && cursorMode) {
      const nextBefore = events.data?.next_before;
      if (nextBefore === null || nextBefore === undefined) return;
      setCursors((prev) => {
        const updated = prev.slice();
        updated[pageIndex + 1] = nextBefore;
        return updated;
      });
    }
    setPageIndex(next);
  };

  return (
    <div className="space-y-6" data-testid="stats-page">
      <PageHeader
        eyebrow="控制台"
        title="用量统计"
        description="按时间范围查看请求量、成功率与 token 消耗；数据按 API Key 归属用户统计，管理员与操作员可看全量，只读用户只能看自己。"
        icon={<BarChart3 className="size-5" />}
      />

      <StatsFilters
        range={range}
        metric={metric}
        customFrom={customFrom}
        customTo={customTo}
        customInvalid={customInvalid}
        onRangeChange={changeRange}
        onMetricChange={setMetric}
        onCustomFromChange={changeCustomFrom}
        onCustomToChange={changeCustomTo}
      />

      <StatsOverviewCards overviewFailed={overviewFailed} stats={stats} />

      {/* 压缩收益与上面总览的缓存命中率**同屏**：省下的 token 只有在缓存仍命中
          时才是净收益，分开看会得出相反结论。 */}
      <CompressPanel stats={stats} isOperator={session.is_operator}
                     since={effectiveSince} until={effectiveUntil}
                     rangeReady={rangeReady} />

      <TrendChartPanel
        metric={metric}
        points={timeline.data?.points ?? []}
        scope={effectiveScope}
        scopeProviders={timelineProviders}
        onScopeChange={setChartScope}
        showShare={showShare}
        providers={byProvider.data?.providers ?? []}
      />

      <AuxCharts
        metric={metric}
        percentile={latencyPercentile.data}
        modelTimeline={modelTimeline.data}
        modelAxisTruncated={modelAxis.truncated}
      />

      <GroupStatsPanel
        groupDimension={groupDimension}
        groupDimensions={groupDimensions}
        onGroupDimensionChange={setGroupDimension}
        onPercentileChange={(value) => setPercentile(value as Percentile)}
        percentile={percentile}
        groupSort={groupSort}
        group={group}
        groupRows={groupRows}
        onDrill={applyDrill}
      />

      <FailurePanel
        isOperator={session.is_operator}
        isError={errors.isError}
        errorRows={errorRows}
        onDrill={applyDrill}
      />

      <EventsPanel
        totalRequests={overview.data?.requests}
        live={live}
        toggleLive={toggleLive}
        isOperator={session.is_operator}
        exporting={exporting}
        exportCsv={exportCsv}
        exportNotice={exportNotice}
        drillLabels={drillLabels}
        clearDrill={clearDrill}
        liveStatus={liveStatus}
        liveRows={liveRows}
        isError={events.isError}
        isFetching={events.isFetching}
        eventRows={eventRows}
        eventSort={eventSort}
        toggleEventSort={toggleEventSort}
        pageIndex={pageIndex}
        pageSize={pageSize}
        hasNextPage={hasNextPage}
        changePageSize={changePageSize}
        onPageChange={changePage}
      />

      <PrivacyNotice />
    </div>
  );
}
