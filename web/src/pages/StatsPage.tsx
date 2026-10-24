import { useMemo, useState } from "react";
import { Activity, BarChart3, Coins, CreditCard, Download, Gauge, Timer, Wallet, X, Zap } from "lucide-react";
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
  useStatsTimeline,
} from "../api/hooks";
import { formatCacheRate, formatCompact, formatCredit, formatLatency, formatMoney, formatNumber, formatSpeed, formatSpeedPair, formatTime, formatTimeParts, usageErrorLabel } from "../api/display";
import { useStatsErrors, type StatsErrorRow } from "../api/statsErrors";
import { ModelTrendChart } from "../components/ModelTrendChart";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { Pagination } from "../components/Pagination";
import { ProviderIcon } from "../components/ProviderIcon";
import { SortableHead } from "../components/SortableHead";
import { SortMenuHead } from "../components/SortMenuHead";
import { Tip } from "../components/Tip";
import { UsageChart } from "../components/UsageChart";
import { providerLabel } from "../api/providers";
import { api } from "../api/client";
import { buildCustomRange, toLocalInput } from "../lib/customRange";
import type { GroupStats, StatsDrill, StatsEventFilters, UsageEventRow } from "../api/types";
import {
  Button,
  Checkbox,
  Empty,
  Field,
  Input,
  Label,
  Metric,
  Notice,
  Panel,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Tabs,
} from "../ui";

/** 实时流前端缓冲上限：只保留最近 50 条（新→旧），页面长开也不会无限增长。 */
const LIVE_MAX_ROWS = 50;

const RANGES = [
  { value: "24h", label: "最近 24 小时", seconds: 86400 },
  { value: "7d", label: "最近 7 天", seconds: 7 * 86400 },
  { value: "30d", label: "最近 30 天", seconds: 30 * 86400 },
  { value: "all", label: "全部", seconds: 0 },
  // 自定义：两个 datetime-local 输入，起止任意。以「本地时区、精确到分钟」为准。
  { value: "custom", label: "自定义", seconds: -1 },
];

/** 图表指标筛选：默认请求次数；均值类（耗时/首字）后端按成功请求归一。 */
const METRICS = [
  { value: "requests", label: "请求次数", icon: <BarChart3 className="size-3.5" /> },
  { value: "tokens", label: "Token", icon: <Coins className="size-3.5" /> },
  { value: "cost", label: "成本", icon: <Wallet className="size-3.5" /> },
  { value: "latency", label: "耗时", icon: <Timer className="size-3.5" /> },
  { value: "ttfb", label: "首字延迟", icon: <Gauge className="size-3.5" /> },
];

/**
 * 请求明细的合并列：把同类列并成一列，默认值一眼可读，悬停展开全部子项。
 *
 * Token = 输入 / 输出 / 命中 / 命中率；延迟 = 耗时 / 首字。合并把列数从 14 压到
 * 约 10（窄屏卡片也少一半字段），排序能力靠列头下拉选子列保留（见 SortMenuHead）。
 */
const TOKEN_COLUMNS = [
  { key: "input_tokens", label: "输入" },
  { key: "output_tokens", label: "输出" },
  { key: "cached_tokens", label: "命中" },
  { key: "cache_rate", label: "命中率" },
] as const;

const LATENCY_COLUMNS = [
  { key: "latency_ms", label: "耗时" },
  { key: "ttfb_ms", label: "首字" },
] as const;

/**
 * 明细「渠道 / 凭证」合并列的子列：渠道 / 凭证。
 *
 * 单元格主行给凭证、副行给渠道（同渠道常挂多个账号，先认账号）；排序能力靠列头
 * 下拉保留（两个子键都还在），故列头同时写明两列。
 */
const SOURCE_COLUMNS = [
  { key: "provider", label: "渠道" },
  { key: "credential", label: "凭证" },
] as const;

/**
 * 分组表请求合并列的子列：请求数 / 成功数（成功率是派生只读值，不可排序）。
 * 与 Token 列同例：默认只显示请求数，悬停补全成功数与成功率。
 */
const GROUP_REQUEST_COLUMNS = [
  { key: "requests", label: "请求数" },
  { key: "ok_count", label: "成功数" },
] as const;

/**
 * 分组表 Token 合并列的子列：输入 / 输出 / 命中（+命中率，派生列不可排序）。
 * 只取明细表同名集合的前三项——`cache_rate` 后端没有分组排序键。
 */
const GROUP_TOKEN_COLUMNS = [
  { key: "input_tokens", label: "输入" },
  { key: "output_tokens", label: "输出" },
  { key: "cached_tokens", label: "命中" },
] as const;

/** 分组表两种延迟口径：均值读小时汇总，分位读明细（菜单即排序键白名单）。 */
const GROUP_AVG_LATENCY_COLUMNS = [
  { key: "avg_latency_ms", label: "平均耗时" },
  { key: "avg_ttfb_ms", label: "平均首字" },
] as const;

/**
 * 分组表分位档：后端一次算好 P50/P90/P95/P99，前端只切展示哪一档。
 *
 * 四档全铺成列会把表头撑爆，故合并成一列 + 档位切换；默认 P95 保持既有
 * 观感与 testId（`group-p95-latency` / `group-sort-p95-latency`）。
 */
const PERCENTILES = ["p50", "p90", "p95", "p99"] as const;
type Percentile = (typeof PERCENTILES)[number];

const PERCENTILE_TABS = PERCENTILES.map((value) => ({
  value, label: value.toUpperCase(),
}));

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

/** 分位列名（后端按 PERCENTILES 下发；types.ts 的 GroupStats 只写死了 P95）。 */
type PercentileField = `${Percentile}_latency_ms` | `${Percentile}_ttfb_ms`;

/** 公共指标 + 分位列 + 至多一个分组键（键名随维度变化，用可选字段统一承载）。 */
type GroupRow = GroupStats & Partial<Record<PercentileField, number | null>> & {
  provider?: string;
  model?: string;
  username?: string;
  credential_id?: string;
  credential_name?: string | null;
};

/**
 * 页面级钻取状态 = 既有 `StatsDrill` + 成功/失败过滤。
 *
 * `ok` 不进 `types.ts` 的 `StatsDrill`（那个类型描述的是「分组表点行」的通用
 * 形态）：只有失败归因卡片需要它。这里本地扩展，`applyDrill` 合并时一并带上。
 */
type PageDrill = StatsDrill & { ok?: number };

/** 凭证文案：昵称优先，回落 ID 前 12 位（ID 很长）。分组表与明细表共用，
 *  保证同一凭证在两处显示成同一个名字。 */
function credentialCellLabel(name: string | null | undefined, id: string | null | undefined) {
  return name ?? (id ? id.slice(0, 12) : "—");
}

/**
 * 明细时间列：时刻与日期摞成两行（`20:17:09` / `2026-10-21`）。
 *
 * **具体时刻优先**（第一行、正常色），日期降为第二行次要色：看明细时先关心
 * 「几点几分」，日期只在跨天对比时才用得上。两行等宽数字各自纵向对齐，扫读更快，
 * 也避免一行 `toLocaleString` 在窄屏把整行撑宽。
 */
function timeCell(row: UsageEventRow) {
  const [date, clock] = formatTimeParts(row.ts);
  return (
    <TableCell label="时间" className="whitespace-nowrap tabular-nums leading-tight"
               title={row.request_id ? `请求 ID：${row.request_id}` : undefined}>
      <span className="block">{clock}</span>
      <span className="block text-xs text-muted-foreground">{date}</span>
    </TableCell>
  );
}

/**
 * 明细「渠道 / 凭证」合并列：凭证名一行（第一行、正常色），渠道 icon + 名称一行（次要色）。
 *
 * **凭证优先**：同渠道下往往挂着多个账号，出问题时先要认出是哪个账号；渠道由图标
 * 一眼可辨，降为第二行次要色即可。两列都是「这条请求打在哪」，拆成两列时凭证列常
 * 只有几个字白占宽度；合并后排序能力靠列头下拉保留（渠道 / 凭证两个子键），
 * 完整凭证 ID 放 title。
 */
function sourceCell(row: UsageEventRow) {
  return (
    <TableCell label="渠道 / 凭证" className="max-w-48 truncate leading-tight"
               title={row.credential_id ?? undefined}>
      <span className="block truncate">
        {credentialCellLabel(row.credential_name, row.credential_id)}
      </span>
      <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        <ProviderIcon provider={row.provider} size={13} />
        {providerLabel(row.provider)}
      </span>
    </TableCell>
  );
}

/** 明细状态列：成功固定文案；失败展示受控错误类型的中文说明（脱敏，不含原始错误体）。 */
function statusCell(row: UsageEventRow) {
  if (row.ok) {
    return <span className="text-ok">成功</span>;
  }
  return <span className="text-destructive">{usageErrorLabel(row.error_type)}</span>;
}

/**
 * Token 合并列：默认「输入 / 输出 · 命中率」，悬停展开命中 token。
 *
 * 命中率复用 `formatCacheRate`（未上报 / 输入为 0 显示 —）；悬停详情用 `Tip`，
 * 窄屏卡片模式下同样可用（点击单元格即可唤出）。
 */
function tokenCell(row: UsageEventRow) {
  const rate = formatCacheRate(row.cached_tokens, row.input_tokens);
  return (
    <Tip content={
      `输入 ${formatCompact(row.input_tokens)} · 输出 ${formatCompact(row.output_tokens)}`
      + ` · 命中 ${formatCompact(row.cached_tokens)} · 命中率 ${rate}`
    }>
      <span className="tabular-nums">
        {formatCompact(row.input_tokens)} / {formatCompact(row.output_tokens)}
        <span className="text-muted-foreground"> · {rate}</span>
      </span>
    </Tip>
  );
}

/** 延迟合并列：默认「耗时 / 首字」，悬停补字段名（两个都是 ms/s 的数字，靠提示消歧）。 */
function latencyCell(row: UsageEventRow) {
  return (
    <Tip content={`耗时 ${formatLatency(row.latency_ms)} · 首字延迟 ${formatLatency(row.ttfb_ms)}`}>
      <span className="tabular-nums">
        {formatLatency(row.latency_ms)}
        <span className="text-muted-foreground"> / {formatLatency(row.ttfb_ms)}</span>
      </span>
    </Tip>
  );
}

/**
 * 速度合并列的子列（分组表与明细表共用同一组排序键）：正文 / 总产出。
 *
 * 两列同分母（生成时间总和），分子不同——正文贴合屏幕刷新速度，总产出含思考
 * token（思考成批快速吐出，数字明显更高），故并成一列 + 下拉选排序键。
 */
const SPEED_COLUMNS = [
  { key: "tps_visible", label: "正文速度" },
  { key: "tps", label: "总产出速度" },
] as const;

/** 明细速度单元格（分页明细表与实时表共用，保证两处同口径同文案）。 */
function eventSpeedCell(row: UsageEventRow) {
  return (
    <Tip content={row.tps === null
      ? "该请求的生成窗口不可测（无首字数据 / 没有输出 token / 生成窗口短于 200ms 或快得不合物理），不计算速度"
      : `正文 ${formatSpeed(row.tps_visible)} · 总产出（含思考）${formatSpeed(row.tps)}`}>
      <span className="tabular-nums">{formatSpeedPair(row.tps_visible, row.tps)}</span>
    </Tip>
  );
}

export function StatsPage() {
  const session = useSessionContext();
  // 默认最近 24 小时：「这个池子现在怎么样」是打开用量统计的第一问题，
  // 7 天会把当天的波动摊平（与凭证池页的实时气质一致）。
  const [range, setRange] = useState("24h");
  // 自定义范围的原始输入（`datetime-local` 文本）。只在 range==="custom" 时参与取数。
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
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

  const events = useStatsEvents(
    drill.username, effectiveSince, effectiveUntil, eventBefore, pageSize,
    eventSort.sort, eventSort.order, eventOffset,
    { provider: drill.provider, model: drill.model,
      credential_id: drill.credential_id, ok: drill.ok });
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

  const overview = useStatsOverview(session.username, undefined, effectiveSince,
                                    effectiveUntil);
  const byProvider = useStatsByProvider(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order);
  const byModel = useStatsByModel(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order);
  const byUser = useStatsByUser(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order);
  const byCredential = useStatsByCredential(
    session.username, undefined, effectiveSince, effectiveUntil,
    groupSort.sort, groupSort.order);
  const timeline = useStatsTimeline(session.username, undefined, effectiveSince,
                                    effectiveUntil, metric);
  const modelTimeline = useStatsModelTimeline(session.username, undefined, effectiveSince,
                                              effectiveUntil, metric);
  // 失败归因：端点仅 operator+（viewer 打开会 403），故按角色禁用查询
  const errors = useStatsErrors(session.username, effectiveSince, effectiveUntil,
                                undefined, session.is_operator);
  const errorRows = errors.data?.errors ?? [];
  // 计数条按本批最大值归一（相对长度才有意义）；全 0 时给 1 避免除零
  const maxFailures = Math.max(1, ...errorRows.map((row) => row.failures));
  const barWidth = (failures: number) => Math.round((failures / maxFailures) * 100);
  /** 失败数减去换号救回数 = 未被救回的失败尝试数（rescued 恒 ≤ failures）。 */
  const unrescued = (row: StatsErrorRow) => row.failures - row.rescued;

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
        {/* 自定义范围：两个 datetime-local，精确到分钟、按浏览器本地时区解析。
            起点晚于终点时提示并暂停取数（见 buildCustomRange）。 */}
        {range === "custom" && (
          <div className="mt-3 flex flex-wrap items-end gap-3" data-testid="custom-range">
            <Field label="开始时间" htmlFor="custom-from-input">
              <Input
                id="custom-from-input"
                type="datetime-local"
                value={customFrom}
                data-testid="custom-from"
                className="w-52"
                onChange={(event) => {
                  setCustomFrom(event.target.value);
                  setPageIndex(0);
                  setCursors([undefined]);
                }}
              />
            </Field>
            <Field label="结束时间" htmlFor="custom-to-input">
              <Input
                id="custom-to-input"
                type="datetime-local"
                value={customTo}
                data-testid="custom-to"
                className="w-52"
                onChange={(event) => {
                  setCustomTo(event.target.value);
                  setPageIndex(0);
                  setCursors([undefined]);
                }}
              />
            </Field>
            {customInvalid && (
              <p className="pb-2 text-xs text-destructive" data-testid="custom-range-invalid">
                开始时间须早于结束时间，且格式有效；已暂停取数。
              </p>
            )}
          </div>
        )}
      </Panel>

      <section
        aria-label="用量概览统计"
        className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6"
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
          label="输出速度"
          value={formatSpeed(stats?.tps_visible, stats?.tps_estimated)}
          hint={stats?.tps === null
            ? "该范围内没有可用的生成时间样本（需有输出 token 且生成窗口 ≥200ms），故不计算速度；换更长的时间范围再试"
            : `正文 token ÷ 生成时间总和（非逐条平均）；总产出（含思考）${formatSpeed(stats?.tps, stats?.tps_estimated)}。≈ 为无首字数据时按端到端耗时估算（不设输出门槛），取自逐请求明细（仅 90 天）`}
          icon={<Zap className="size-4" />}
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
          hint="渠道可选字段，可能不返回；≈ 为推算值（TRAE 按单价折算）"
          icon={<CreditCard className="size-4" />}
        />
        <Metric
          label="成本（估算）"
          value={formatMoney(stats?.cost_cny, "CNY")}
          hint={`美元 ${formatMoney(stats?.cost_usd, "USD")} · 按 OpenRouter 刊例价折算，未匹配定价的请求不计`}
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
        <Panel title="请求量趋势">
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
        {/* 分位档切换：四档后端一次算好，这里只决定展示哪一档（避免四列撑爆表头） */}
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">延迟分位</span>
          <Tabs value={percentile} options={PERCENTILE_TABS}
                onChange={(value) => setPercentile(value as Percentile)}
                testId="percentile-tabs" compact />
          <span className="text-xs text-muted-foreground">
            取自逐请求明细（仅 90 天），只统计成功请求
          </span>
        </div>
        {groupRows.length === 0 ? (
          <Empty data-testid="no-group-stats">该范围内没有请求</Empty>
        ) : (
          <Table data-testid="group-table" cards>
            <TableHeader>
              <TableRow>
                <SortableHead label={group.value === "provider" ? "渠道" : groupLabel}
                              columnKey="group" active={groupSort.sort === "group"}
                              direction={groupSort.order} onToggle={groupSort.toggle}
                              testId="sort-group" />
                <SortMenuHead label="请求" options={[...GROUP_REQUEST_COLUMNS]}
                              activeKey={groupSort.sort} direction={groupSort.order}
                              onToggle={groupSort.toggle} testId="group-sort-requests" />
                <SortableHead label="Credit" columnKey="credit"
                              active={groupSort.sort === "credit"} direction={groupSort.order}
                              onToggle={groupSort.toggle} align="right" testId="sort-credit" />
                <SortableHead label="成本" columnKey="cost_cny"
                              active={groupSort.sort === "cost_cny"} direction={groupSort.order}
                              onToggle={groupSort.toggle} align="right" testId="sort-cost_cny" />
                {/* Token 合并列：输入 / 输出 · 命中率，悬停展开命中 token。
                    列头下拉仍可分别按输入 / 输出 / 命中排序。 */}
                <SortMenuHead label="Token" options={[...GROUP_TOKEN_COLUMNS]}
                              activeKey={groupSort.sort} direction={groupSort.order}
                              onToggle={groupSort.toggle} testId="group-sort-token" />
                {/* 速度合并列：正文 / 总产出（含思考）——同分母（生成时间总和，
                    读明细、仅 90 天），分子不同：正文贴近屏幕上看到的刷新速度，
                    总产出含思考 token（思考成批快速吐出，数字会明显更高）。无首字
                    数据时回落估算并标 ≈。列头下拉可分别按两个分子排序。 */}
                <SortMenuHead label="速度" options={[...SPEED_COLUMNS]}
                              activeKey={groupSort.sort} direction={groupSort.order}
                              onToggle={groupSort.toggle} testId="group-sort-speed" />
                {/* 延迟两列：均值读小时汇总（与总览同口径），分位读明细表（仅 90 天）。
                    两套口径各自合并成一列，靠表尾说明与悬停文案区分。 */}
                <SortMenuHead label="平均延迟" options={[...GROUP_AVG_LATENCY_COLUMNS]}
                              activeKey={groupSort.sort} direction={groupSort.order}
                              onToggle={groupSort.toggle} testId="group-sort-avg-latency" />
                <SortMenuHead label={`${percentile.toUpperCase()} 延迟`}
                              options={[
                                { key: `${percentile}_latency_ms`,
                                  label: `${percentile.toUpperCase()} 耗时` },
                                { key: `${percentile}_ttfb_ms`,
                                  label: `${percentile.toUpperCase()} 首字` },
                              ]}
                              activeKey={groupSort.sort} direction={groupSort.order}
                              onToggle={groupSort.toggle}
                              testId={`group-sort-${percentile}-latency`} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {groupRows.map((row) => {
                // 点行 = 钻取到该维度的明细；分组键 → 明细过滤字段的映射：
                // provider/model/credential_id 直接同名，用户维度走作用域 username
                const keyValue = String(row[groupKey]);
                const next: StatsDrill = group.value === "user"
                  ? { username: keyValue }
                  : group.value === "provider" ? { provider: keyValue }
                    : group.value === "model" ? { model: keyValue }
                      : { credential_id: keyValue };
                return (
                <TableRow key={keyValue}
                          data-testid={`group-row-${keyValue}`}
                          className="cursor-pointer"
                          tabIndex={0}
                          role="button"
                          aria-label={`查看 ${keyValue} 的请求明细`}
                          onClick={() => applyDrill(next)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              applyDrill(next);
                            }
                          }}>
                  <TableCell label={group.value === "provider" ? "渠道" : groupLabel}
                             className="max-w-48 truncate"
                             title={groupKey === "credential_id"
                               ? (row.credential_id ?? undefined)
                               : keyValue}>
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
                      keyValue
                    )}
                  </TableCell>
                  {/* 请求合并列：默认「请求数 · 成功率」，悬停补成功数。
                      成功率 = 成功 ÷ 请求，请求数为 0 时给 —（不渲染 0% 冒充数据）。 */}
                  <TableCell label="请求" className="text-right">
                    <Tip content={`请求 ${formatNumber(row.requests)}`
                      + ` · 成功 ${formatNumber(row.ok_count)}`
                      + ` · 成功率 ${row.requests > 0
                        ? `${((row.ok_count / row.requests) * 100).toFixed(1)}%`
                        : "—"}`}>
                      <span className="tabular-nums">
                        {formatNumber(row.requests)}
                        <span className="text-muted-foreground" data-testid="group-success-rate">
                          {" · "}{row.requests > 0
                            ? `${((row.ok_count / row.requests) * 100).toFixed(1)}%`
                            : "—"}
                        </span>
                      </span>
                    </Tip>
                  </TableCell>
                  <TableCell label="Credit" className="text-right tabular-nums">
                    {formatCredit(row.credit, row.credit_estimated)}
                  </TableCell>
                  <TableCell label="成本" className="text-right tabular-nums"
                             title={`美元 ${formatMoney(row.cost_usd, "USD")}`}>
                    {formatMoney(row.cost_cny, "CNY")}
                  </TableCell>
                  <TableCell label="Token" className="text-right"
                             data-testid="group-token">
                    <Tip content={
                      `输入 ${formatCompact(row.input_tokens)}`
                      + ` · 输出 ${formatCompact(row.output_tokens)}`
                      + ` · 命中 ${formatCompact(row.cached_tokens)}`
                    }>
                      <span className="tabular-nums">
                        {formatCompact(row.input_tokens)} / {formatCompact(row.output_tokens)}
                        <span className="text-muted-foreground" data-testid="group-cache-rate">
                          {" · "}{formatCacheRate(row.cached_tokens, row.input_tokens)}
                        </span>
                      </span>
                    </Tip>
                  </TableCell>
                  {/* 速度合并列：正文 / 总产出；无合格样本整格 —（估算口径前置 ≈）。
                      正文为 0（整段输出都是思考）时正文侧显示 —，不拿 0 冒充速度。 */}
                  <TableCell label="速度" className="text-right tabular-nums"
                             data-testid="group-tps">
                    <Tip content={row.tps === null && row.tps_visible === null
                      ? "该组没有产出了 token 的成功请求，或生成窗口不可测，不计算速度"
                      : `正文 ${formatSpeed(row.tps_visible, row.tps_estimated)}`
                        + ` · 总产出（含思考）${formatSpeed(row.tps, row.tps_estimated)}`}>
                      <span>{formatSpeedPair(row.tps_visible, row.tps, row.tps_estimated)}</span>
                    </Tip>
                  </TableCell>
                  {/* 延迟两列：均值与分位都只算成功请求；无成功请求显示 — */}
                  <TableCell label="平均延迟" className="text-right"
                             data-testid="group-avg-latency">
                    <Tip content={`平均耗时 ${formatLatency(row.avg_latency_ms)}`
                      + ` · 平均首字 ${formatLatency(row.avg_ttfb_ms)}`}>
                      <span className="tabular-nums">
                        {formatLatency(row.avg_latency_ms)}
                        <span className="text-muted-foreground" data-testid="group-avg-ttfb">
                          {" / "}{formatLatency(row.avg_ttfb_ms)}
                        </span>
                      </span>
                    </Tip>
                  </TableCell>
                  <TableCell label={`${percentile.toUpperCase()} 延迟`} className="text-right"
                             data-testid={`group-${percentile}-latency`}>
                    <Tip content={`${percentile.toUpperCase()} 耗时 `
                      + `${formatLatency(row[`${percentile}_latency_ms`])}`
                      + ` · ${percentile.toUpperCase()} 首字 `
                      + `${formatLatency(row[`${percentile}_ttfb_ms`])}`}>
                      <span className="tabular-nums">
                        {formatLatency(row[`${percentile}_latency_ms`])}
                        <span className="text-muted-foreground"
                              data-testid={`group-${percentile}-ttfb`}>
                          {" / "}{formatLatency(row[`${percentile}_ttfb_ms`])}
                        </span>
                      </span>
                    </Tip>
                  </TableCell>
                </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          {groupKey === "credential_id"
            ? "凭证分组取自逐请求明细（仅保留 90 天），无凭证的预热失败等请求不计入。"
            : "各维度数据源为小时汇总（永久保留），最近 5 分钟内的请求可能尚未计入。"}
          {" "}点任意一行可钻取到下方「请求明细」。
        </p>
        {/* 延迟列的两套口径必须写明：均值永久、分位只覆盖 90 天，
            混在同一行里不说明会被读成「同一个范围」。 */}
        <p className="mt-1 text-xs text-muted-foreground">
          延迟列只统计成功请求。平均耗时 / 平均首字取自小时汇总（与总览卡片同口径）；
          分位为第 P50/P90/P95/P99 百分位（向上取整名次，默认 P95），取自逐请求明细，
          只覆盖最近 90 天。速度列两个分子分母相同（生成时间总和，不是逐条平均），正文
          token（output − reasoning）贴近屏幕上看到的刷新速度，总产出 token（含思考）
          含思考成批吐出的部分；成功请求只要有产出就计入（不设输出量下限），并剔除生成
          窗口 &lt;200ms 或单条 &gt;1000 tok/s 的样本——那是上游把内容攒成一批吐出、生成
          时长根本不可测，不是「很快」。无首字数据时按端到端耗时估算（同样不设输出门槛，
          分母含首字等待，前置 ≈），同样取自逐请求明细（仅 90 天）。
        </p>
      </Panel>

      {/* 失败归因（operator+）：跨用户看「哪里在坏」。点行 → 钻取到该渠道+模型
          的失败明细（ok=0），与下方「请求明细」联动。 */}
      {session.is_operator && (
        <Panel data-testid="error-panel" title="失败归因" action={
          <span className="text-xs text-muted-foreground">
            按渠道 / 模型 / 错误类型分组 · 取自逐请求明细（仅 90 天）
          </span>
        }>
          {errors.isError ? (
            <div data-testid="errors-error">
              <Notice tone="danger">
                失败归因加载失败，请刷新重试；若刚升级服务，请确认后端已重启加载新端点。
              </Notice>
            </div>
          ) : (errors.data?.errors.length ?? 0) === 0 ? (
            <Empty data-testid="no-errors">该范围内没有失败请求</Empty>
          ) : (
            <Table data-testid="error-table" cards>
              <TableHeader>
                <TableRow>
                  <TableHead>渠道</TableHead>
                  <TableHead>模型</TableHead>
                  <TableHead>错误类型</TableHead>
                  <TableHead className="text-right">失败数</TableHead>
                  <TableHead className="text-right">未救回</TableHead>
                  <TableHead className="text-right">最近失败</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {errorRows.map((row) => (
                  <TableRow
                    key={`${row.provider}/${row.model}/${row.error_type ?? "null"}`}
                    data-testid={`error-row-${row.provider}-${row.model}-${row.error_type ?? "unknown"}`}
                    className="cursor-pointer"
                    tabIndex={0}
                    role="button"
                    aria-label={`查看 ${row.provider} / ${row.model} 的失败明细`}
                    onClick={() => applyDrill({ provider: row.provider, model: row.model,
                                                ok: 0 })}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        applyDrill({ provider: row.provider, model: row.model, ok: 0 });
                      }
                    }}>
                    <TableCell label="渠道">
                      <span className="inline-flex items-center gap-1.5">
                        <ProviderIcon provider={row.provider} size={13} />
                        {providerLabel(row.provider)}
                      </span>
                    </TableCell>
                    <TableCell label="模型" className="max-w-48 truncate" title={row.model}>
                      {row.model}
                    </TableCell>
                    <TableCell label="错误类型" className="text-destructive">
                      {usageErrorLabel(row.error_type)}
                    </TableCell>
                    <TableCell label="失败数" className="text-right">
                      <span className="inline-flex items-center gap-2 tabular-nums">
                        <span className="hidden h-1.5 w-16 overflow-hidden rounded-full bg-muted sm:block">
                          <span className="block h-full rounded-full bg-destructive"
                                style={{ width: `${barWidth(row.failures)}%` }} />
                        </span>
                        {formatNumber(row.failures)}
                      </span>
                    </TableCell>
                    {/* 失败数是「尝试」口径：换号成功后失败行照样留着。
                        未救回 = failures − rescued，是未被救回的失败尝试数。 */}
                    <TableCell label="未救回" className="text-right tabular-nums">
                      <span data-testid={`error-unrescued-${row.provider}-${row.model}-${row.error_type ?? "unknown"}`}
                            className={unrescued(row) > 0 ? "text-destructive" : "text-muted-foreground"}>
                        {formatNumber(unrescued(row))}
                      </span>
                    </TableCell>
                    <TableCell label="最近失败"
                               className="whitespace-nowrap text-right tabular-nums">
                      {formatTime(row.last_ts)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          <p className="mt-2 text-xs text-muted-foreground">
            窗口缺省为最近 24 小时（与上方时间范围一致时按所选范围统计）；
            未上报错误类型的历史行显示为「失败」。失败数是<strong>尝试</strong>口径：
            换号重试每次上游拒绝都记一行，「未救回」是未被救回的失败尝试数
            （同一请求多次失败会算多次；同请求另有成功行即算救回）。点任意一行可钻取到下方「请求明细」（只看失败）。
          </p>
        </Panel>
      )}

      <Panel data-testid="events-panel" title="请求明细" action={
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span>共 {formatNumber(overview.data?.requests)} 条 · 保留 90 天</span>
          {/* 实时流：默认关闭（翻页看历史是常态），打开后追加「刚落库」的行 */}
          <Label className="w-fit cursor-pointer gap-1.5 text-xs font-normal text-muted-foreground">
            <Checkbox checked={live} data-testid="events-live-toggle"
                      onCheckedChange={(value) => toggleLive(value === true)} />
            实时
          </Label>
          {/* 导出限 admin/operator：整份明细落盘比翻页浏览的暴露面大 */}
          {session.is_operator && (
            <Button size="sm" variant="ghost" data-testid="events-export"
                    disabled={exporting} onClick={exportCsv}>
              <Download className="size-3.5" />
              {exporting ? "导出中…" : "导出 CSV"}
            </Button>
          )}
        </div>
      }>
        {drillLabels.length > 0 && (
          <div className="mb-3 flex flex-wrap items-center gap-2" data-testid="drill-filters">
            <span className="text-xs text-muted-foreground">钻取过滤</span>
            {drillLabels.map((label) => (
              <span key={label}
                    className="inline-flex items-center rounded-md bg-secondary px-2 py-0.5 text-xs text-secondary-foreground">
                {label}
              </span>
            ))}
            <Button size="sm" variant="ghost" data-testid="drill-clear"
                    onClick={clearDrill}>
              <X className="size-3.5" />
              清除
            </Button>
          </div>
        )}
        {exportNotice && (
          <div data-testid="export-notice">
            <Notice tone="muted" className="mb-3 text-xs">{exportNotice}</Notice>
          </div>
        )}
        {live && (
          <div className="mb-4 rounded-md border border-border/70" data-testid="events-live">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/70 px-3 py-2 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
                <span aria-hidden
                      className={liveStatus === "live"
                        ? "size-1.5 rounded-full bg-ok" : "size-1.5 rounded-full bg-muted-foreground"} />
                实时明细
              </span>
              <span role="status">
                {liveStatus === "error"
                  ? "实时连接已断开（可能订阅数已满或会话过期），请刷新重试。"
                  : liveRows.length === 0
                    ? "已连接，等待新的请求…"
                    : `已收到 ${liveRows.length} 条（最多保留 ${LIVE_MAX_ROWS} 条）`}
              </span>
              <span className="ml-auto">仅推送本页当前筛选范围内的新请求</span>
            </div>
            {liveRows.length > 0 && (
              <Table data-testid="events-live-table" cards>
                <TableHeader>
                  <TableRow>
                    <TableHead>时间</TableHead>
                    {session.is_operator && <TableHead>用户</TableHead>}
                    <TableHead>渠道 / 凭证</TableHead>
                    <TableHead>模型</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead className="text-right">Token</TableHead>
                    <TableHead className="text-right">Credit</TableHead>
                    <TableHead className="text-right">成本</TableHead>
                    <TableHead className="text-right">延迟</TableHead>
                    <TableHead className="text-right">速度</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {liveRows.map((row) => (
                    <TableRow key={`live-${row.rowid}`} data-testid="live-row">
                      {timeCell(row)}
                      {session.is_operator && (
                        <TableCell label="用户">{row.username}</TableCell>
                      )}
                      {sourceCell(row)}
                      <TableCell label="模型" className="max-w-48 truncate">
                        {row.model}
                      </TableCell>
                      <TableCell label="状态">{statusCell(row)}</TableCell>
                      <TableCell label="Token" className="text-right">{tokenCell(row)}</TableCell>
                      <TableCell label="Credit" className="text-right tabular-nums">
                        {formatCredit(row.credit, row.credit_estimated)}
                      </TableCell>
                      <TableCell label="成本" className="text-right tabular-nums">
                        {formatMoney(row.cost_cny, "CNY")}
                      </TableCell>
                      <TableCell label="延迟" className="text-right">
                        {latencyCell(row)}
                      </TableCell>
                      <TableCell label="速度" className="text-right">
                        {eventSpeedCell(row)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>
        )}
        {events.isError ? (
          <Notice tone="danger" data-testid="events-error">
            请求明细加载失败，请刷新重试；若刚升级服务，请确认后端已重启加载新端点。
          </Notice>
        ) : eventRows.length === 0 ? (
          <Empty data-testid="no-events">该范围内没有请求明细</Empty>
        ) : (
          <>
            <Table data-testid="events-table" cards>
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
                  {/* 渠道 / 凭证合并列：凭证在上、渠道在下（同渠道常挂多个账号，先认账号）。
                      排序能力靠列头下拉保留（provider / credential 两个子键）。 */}
                  <SortMenuHead label="渠道 / 凭证" options={[...SOURCE_COLUMNS]}
                                activeKey={eventSort.sort} direction={eventSort.order}
                                onToggle={toggleEventSort} align="left"
                                testId="event-sort-source" />
                  <SortableHead label="模型" columnKey="model" active={eventSort.sort === "model"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-model" />
                  <SortableHead label="状态" columnKey="ok" active={eventSort.sort === "ok"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                testId="event-sort-ok" />
                  {/* Token 合并列：输入/输出/命中/命中率，列头下拉选子列排序 */}
                  <SortMenuHead label="Token" options={[...TOKEN_COLUMNS]}
                                activeKey={eventSort.sort} direction={eventSort.order}
                                onToggle={toggleEventSort} testId="event-sort-token" />
                  <SortableHead label="Credit" columnKey="credit"
                                active={eventSort.sort === "credit"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-credit" />
                  <SortableHead label="成本" columnKey="cost_cny"
                                active={eventSort.sort === "cost_cny"}
                                direction={eventSort.order} onToggle={toggleEventSort}
                                align="right" testId="event-sort-cost_cny" />
                  {/* 延迟合并列：耗时/首字，列头下拉选子列排序 */}
                  <SortMenuHead label="延迟" options={[...LATENCY_COLUMNS]}
                                activeKey={eventSort.sort} direction={eventSort.order}
                                onToggle={toggleEventSort} testId="event-sort-latency" />
                  {/* 速度合并列：正文 / 总产出（逐行按生成窗口算，无首字数据的
                      请求不回落估算，直接 —）。列头下拉可选子列排序。 */}
                  <SortMenuHead label="速度" options={[...SPEED_COLUMNS]}
                                activeKey={eventSort.sort} direction={eventSort.order}
                                onToggle={toggleEventSort} testId="event-sort-speed" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {eventRows.map((row) => (
                  <TableRow key={row.rowid} data-testid="event-row">
                    {timeCell(row)}
                    {session.is_operator && (
                      <TableCell label="用户">{row.username}</TableCell>
                    )}
                    {sourceCell(row)}
                    <TableCell label="模型" className="max-w-48 truncate" title={row.model}>
                      {row.model}
                    </TableCell>
                    <TableCell label="状态">{statusCell(row)}</TableCell>
                    <TableCell label="Token" className="text-right">
                      {tokenCell(row)}
                    </TableCell>
                    <TableCell label="Credit" className="text-right tabular-nums">
                      {formatCredit(row.credit, row.credit_estimated)}
                    </TableCell>
                    <TableCell label="成本" className="text-right tabular-nums"
                               title={`美元 ${formatMoney(row.cost_usd, "USD")}`}>
                      {formatMoney(row.cost_cny, "CNY")}
                    </TableCell>
                    <TableCell label="延迟" className="text-right">
                      {latencyCell(row)}
                    </TableCell>
                    <TableCell label="速度" className="text-right">
                      {eventSpeedCell(row)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Pagination
              testId="events"
              page={pageIndex}
              pageSize={pageSize}
              hasNext={hasNextPage}
              busy={events.isFetching}
              onPageSizeChange={changePageSize}
              onPageChange={(next) => {
                // 游标模式向下翻页：先把本页的 next_before 压栈，下一页才有游标可发
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
              }}
            />
            <p className="mt-2 text-xs text-muted-foreground">
              速度 = 输出 token ÷ 生成时间（耗时 − 首字）：左侧正文（不含思考）、右侧总产出
              （含思考），与分组表同规则（有输出 token、生成窗口 ≥200ms、单条 ≤1000 tok/s）。
              逐行不按端到端耗时估算——单条样本摊不平首字等待，故无生成窗口的请求显示 —。
            </p>
          </>
        )}
      </Panel>

      <Notice tone="muted" className="items-start gap-1 px-3 py-2.5 text-xs">
        <p>
          隐私：不保存提示词、回答、请求头、Token、工具参数与原始错误体；逐请求明细保留 90 天，小时汇总永久保留；按 API Key 归属用户统计。
        </p>
        <p>
          credit 为渠道可选字段，经常不返回；TRAE 上游不给单请求积分，带 ≈ 的数值是按官方单价折算（CodeBuddy 为上游返回的真值）。健康度只依赖额度探测接口，主指标是 token 数。缓存命中率 = 命中 token ÷ 输入 token，仅在上报过缓存时展示。
        </p>
        <p>
          成本为估算：按 OpenRouter 的模型刊例价（USD / 百万 token）与本服务写入时生效的美元汇率折算，展示人民币、括号内为美元；不是上游真实扣费，未匹配到定价的模型不计入（显示 —）。汇率可在「任务与配置」页热更，仅影响之后写入的请求，历史成本不重算。
        </p>
      </Notice>
    </div>
  );
}