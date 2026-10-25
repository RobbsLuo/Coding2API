import { useEffect, useState } from "react";
import {
  Ban,
  Boxes,
  CircleDollarSign,
  RefreshCw,
  Search,
  Trophy,
} from "lucide-react";
import { useSessionContext } from "../Layout";
import {
  useBlockModel,
  useModelCatalogPage,
  useModelProbes,
  useProbeModel,
  useSetProbeManual,
} from "../api/modelExtrasHooks";
import { probeStateLabel, type ModelCatalogRow, type ModelProbeRow } from "../api/modelExtras";
import {
  formatAgo,
  formatCompact,
  formatNumber,
  formatTime,
} from "../api/display";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { Pagination } from "../components/Pagination";
import { SortableHead } from "../components/SortableHead";
import { ColumnHint, LongTextTip } from "../components/Tip";
import { ToastViewport, useToasts } from "../components/Toast";
import { useSort } from "../hooks/useSort";
import { DEFAULT_PAGE_SIZE } from "../lib/constants";
import {
  Badge,
  Button,
  Checkbox,
  Empty,
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
import type { ModelCatalogEntry } from "../api/types";

/** 币种切换：价格原始单位是 USD / 百万 token，成本展示以人民币为主，两者都提供。 */
type Currency = "USD" | "CNY";

const CURRENCIES = [
  { value: "USD", label: "美元" },
  { value: "CNY", label: "人民币" },
];

/** 单价格式化：刊例价量级跨度大（0.02 ~ 数十），保留最多 4 位小数。 */
function formatUnitPrice(
  value: number | null,
  currency: Currency,
  rate: number,
): string {
  if (value === null) return "—";
  const converted = currency === "CNY" ? value * rate : value;
  const symbol = currency === "CNY" ? "¥" : "$";
  return `${symbol}${converted.toLocaleString("zh-CN", { maximumFractionDigits: 4 })}`;
}

/** 上游模态取值 → 中文（未知取值原样回显，上游加值前端不炸）。 */
const MODALITY_LABEL: Record<string, string> = {
  text: "文本",
  image: "图片",
  pdf: "PDF",
  audio: "音频",
  video: "视频",
  file: "文件",
};

function modalityText(items: string[]): string {
  if (items.length === 0) return "—";
  return items.map((item) => MODALITY_LABEL[item] ?? item).join(" · ");
}

/** 能力标签：只列具备的能力；一个都没有时返回空数组（页面显示 —）。 */
function capabilities(row: ModelCatalogEntry): string[] {
  const items: string[] = [];
  if (row.reasoning) items.push("推理");
  if (row.tool_call) items.push("工具调用");
  if (row.attachment) items.push("附件");
  if (row.structured_output) items.push("结构化输出");
  if (row.open_weights) items.push("开放权重");
  return items;
}

/** 上下文 / 输出上限：两者都缺显示 —，各占一行（表头「上下文 / 输出」对应上行 / 下行）。 */
function limitLine(value: number | null): string {
  return value === null ? "—" : formatCompact(value);
}

/**
 * 能力分一行：智 / 编 / 智体三项各占一行，缺失的那项显示 —。
 *
 * 与表头「能力分」对应（三行结构同「上下文 / 输出」）。整块没有分时只显示一个
 * 居中的 —：OpenRouter 收录数百条目，其中多数没有第三方成绩，逐个渲染
 * 三个 — 会让这一列显得很吵。
 */
function benchmarkLine(row: ModelCatalogEntry) {
  const b = row.benchmarks;
  if (!b) return <div className="text-muted-foreground">—</div>;
  const score = (value: number | undefined): string =>
    value === undefined ? "—" : value.toLocaleString("zh-CN");
  return (
    <div title="Artificial Analysis 指数（智能 / 编程 / 智能体），非本服务实测">
      <div>智 {score(b.intelligence_index)}</div>
      <div className="text-muted-foreground">编 {score(b.coding_index)}</div>
      <div className="text-muted-foreground">体 {score(b.agentic_index)}</div>
    </div>
  );
}

/**
 * 可用性单元格：窗口内失败次数 + 最后失败时刻 + 「加入黑名单」按钮。
 *
 * 没有失败记录时显示 —（「没数据」不等于「确认健康」，与后端 `suspected`
 * 字段同口径）。已命中当前黑名单的行只显示「已屏蔽」，按钮置灰——重复点击
 * 不会产生新的审计记录，只会让用户误以为操作没生效。
 */
function AvailabilityCell({
  row,
  windowDays,
  busy,
  onBlock,
}: {
  row: ModelCatalogRow;
  windowDays: number;
  busy: boolean;
  onBlock: () => void;
}) {
  const suspected = row.suspected;
  if (!suspected) return <span className="text-muted-foreground">—</span>;
  return (
    <div className="space-y-1" data-testid={`model-availability-${row.id}`}>
      <div className="flex flex-wrap items-center gap-1">
        <Badge tone="warn" dot>
          近 {windowDays} 天失败 {suspected.failures} 次
        </Badge>
        {suspected.blocked && <Badge tone="muted">已屏蔽</Badge>}
      </div>
      <div className="text-muted-foreground" title="最后一次失败的请求时刻">
        {suspected.provider} · {formatTime(suspected.last_failed_at)}
      </div>
      <Button
        variant="default"
        size="sm"
        data-testid={`model-block-${row.id}`}
        disabled={busy || suspected.blocked}
        title={suspected.blocked
          ? "该模型已命中当前黑名单"
          : `把 ${suspected.model} 追加进 MODEL_BLOCKLIST（走设置写接口，记审计）`}
        onClick={onBlock}
      >
        <Ban className="size-3" />
        加入黑名单
      </Button>
    </div>
  );
}

/**
 * 探活面板：只有「仍被 blocked 负缓存锁住」或「探过」的 (渠道, 模型) 才出现。
 *
 * 独立成块而不并进目录行：目录是 OpenRouter 的模型 id（`z-ai/glm-5.3`），
 * 负缓存与探活是网关侧名（`glm-5.3`），两边要靠后端的 `model_match` 才能对上。
 * 前端自己复刻那套规则（同义词、尾缀、版本号点/连字符互换）必然会漂移，
 * 与其显示错行，不如各列各的——探活表的行本来就是可直接操作的对象。
 *
 * 三态措辞刻意区分「确认不可用」与「无法判定」：后者（额度 / 限流 / 超时）
 * 不构成下线证据，页面不能让用户误读成「这个模型坏了」。
 */
function ProbePanel({
  rows,
  loading,
  error,
  busy,
  onProbe,
  onManual,
}: {
  rows: ModelProbeRow[];
  loading: boolean;
  error: boolean;
  busy: boolean;
  onProbe: (row: ModelProbeRow) => void;
  onManual: (row: ModelProbeRow) => void;
}) {
  const locked = rows.filter((row) => row.credentials > 0).length;
  return (
    <Panel title="主动探活">
      <p className="text-xs text-muted-foreground">
        只探仍被「该后端无此模型」负缓存锁住的 (渠道, 模型)：成功即清负缓存、
        模型立刻回到池子；上游明确说没这个模型才算失败；额度 / 限流 / 网络一律
        「无法判定」，不禁用。
      </p>
      {error ? (
        <div data-testid="model-probe-error">
          <Notice tone="danger">探活记录加载失败，请刷新重试。</Notice>
        </div>
      ) : loading ? (
        <Empty data-testid="model-probe-loading">正在读取探活记录…</Empty>
      ) : rows.length === 0 ? (
        <Empty data-testid="model-probe-empty">
          当前没有被负缓存锁住的模型，也没有探活记录
        </Empty>
      ) : (
        <div className="space-y-2">
          <div className="text-xs text-muted-foreground" data-testid="model-probe-summary">
            共 {rows.length} 个 · 仍被负缓存 {locked} 个
          </div>
          <Table cards>
            <TableHeader>
              <TableRow>
                <TableHead>渠道 / 模型</TableHead>
                <TableHead>探活结论</TableHead>
                <TableHead>负缓存</TableHead>
                <TableHead>最近探活</TableHead>
                <TableHead>操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const key = `${row.provider}-${row.model}`;
                const manual = row.manual !== "";
                return (
                  <TableRow key={key} data-testid={`probe-row-${key}`}>
                    <TableCell label="渠道 / 模型" className="text-xs">
                      <div className="font-mono break-all">{row.model}</div>
                      <div className="text-muted-foreground">{row.provider}</div>
                    </TableCell>
                    <TableCell label="探活结论" className="text-xs">
                      <div className="flex flex-wrap items-center gap-1">
                        <Badge tone={row.state === "ok" ? "ok"
                          : row.state === "failed" ? "danger" : "muted"}
                          dot>
                          探活 · {probeStateLabel(row.state)}
                        </Badge>
                        {manual && <Badge tone="muted">人工接管</Badge>}
                      </div>
                      {row.state === "failed" && row.failures > 0 && (
                        <div className="text-muted-foreground">连续失败 {row.failures} 次</div>
                      )}
                    </TableCell>
                    <TableCell label="负缓存" className="text-xs">
                      {row.credentials > 0 ? (
                        <span title="仍被 blocked 负缓存避让的号数 · 最晚到期时刻">
                          {row.credentials} 号 · 至 {formatTime(row.until)}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">无</span>
                      )}
                    </TableCell>
                    <TableCell label="最近探活" className="text-xs">
                      {row.last_probe_at ? (
                        <>
                          <div title={formatTime(row.last_probe_at)}>
                            {formatAgo(row.last_probe_at)}
                          </div>
                          {row.last_ok_at > 0 && (
                            <div className="text-muted-foreground">
                              上次成功 {formatAgo(row.last_ok_at)}
                            </div>
                          )}
                        </>
                      ) : (
                        <span className="text-muted-foreground">未探过</span>
                      )}
                      {row.last_error && (
                        // 错误摘要可能很长（上游原文）：限宽单行截断，hover 看全文。
                        // 不折行是必要的——折行会把整列撑宽，挤掉右侧操作列。
                        <LongTextTip content={row.last_error}>
                          <div
                            className="max-w-[16rem] truncate text-muted-foreground"
                            data-testid={`probe-error-${key}`}
                          >
                            {row.last_error}
                          </div>
                        </LongTextTip>
                      )}
                    </TableCell>
                    <TableCell label="操作" className="text-xs">
                      <div className="flex flex-wrap gap-1">
                        <Button
                          variant="default"
                          size="sm"
                          data-testid={`probe-now-${key}`}
                          disabled={busy}
                          title={`立即探活 ${row.provider}/${row.model}：成功即清该渠道的 blocked 负缓存（记审计）`}
                          onClick={() => onProbe(row)}
                        >
                          <RefreshCw className="size-3" />
                          立即探活
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          data-testid={`probe-manual-${key}`}
                          disabled={busy}
                          title={manual
                            ? "交还自动探活：后台将重新按周期探这个模型"
                            : "人工接管：后台不再自动探这个模型（不改变当前负缓存）"}
                          onClick={() => onManual(row)}
                        >
                          {manual ? "交还自动探活" : "人工接管"}
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </Panel>
  );
}

/**
 * 模型列表页（管理）：只读展示 OpenRouter 的模型目录。
 *
 * 这份目录既是统计页「成本（估算）」的价格来源（输入 / 输出 / 缓存读，USD /
 * 百万 token），也带出上游更详细的元数据（上下文、模态、能力、知识截止等）。
 * 目录缺失（首次部署尚未拉取）时显示空态而非报错。
 *
 * 门槛与「上游状态」一致：operator（admin / operator）可见，与路由守卫同口径。
 */
export function ModelCatalogPage() {
  const session = useSessionContext();
  const { sort, order, toggle } = useSort("id", "asc",
    { context: "desc", max_output: "desc", input: "desc", output: "desc",
      cache_read: "desc", cache_write: "desc", intelligence: "desc" });
  const [query, setQuery] = useState("");
  // 搜索词去抖：服务端分页后每次击键都会打接口，逐字符发请求既费流量又让结果闪烁。
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [currency, setCurrency] = useState<Currency>("USD");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  // 只看「当前渠道获取到的模型」：默认关闭（OpenRouter 收录全量，用户按需收窄）。
  const [onlyAvailable, setOnlyAvailable] = useState(false);
  // 只看「疑似下线」：最近 N 天有模型级失败（口径见后端 /api/models/availability）。
  const [onlySuspected, setOnlySuspected] = useState(false);
  const { toasts, push, dismiss } = useToasts();
  const block = useBlockModel(session.username);
  const probes = useModelProbes(session.username);
  const probeNow = useProbeModel(session.username);
  const probeManual = useSetProbeManual(session.username);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 250);
    return () => clearTimeout(timer);
  }, [query]);

  const { data, isLoading, isError, isFetching } = useModelCatalogPage(
    session.username,
    {
      sort, order,
      q: debouncedQuery || undefined,
      onlyAvailable,
      onlySuspected,
      limit: pageSize,
      offset: page * pageSize,
    });

  const rate = data?.usd_cny_rate ?? 1;
  const models = data?.models ?? [];
  const catalogCount = data?.count ?? 0;
  const total = data?.total ?? 0;
  const windowDays = data?.availability_days ?? 7;

  // 搜索 / 改每页条数 / 换排序 / 切过滤后回到第一页：否则会停在一个已不存在的页码上（空白）
  useEffect(() => {
    setPage(0);
  }, [debouncedQuery, pageSize, sort, order, onlyAvailable, onlySuspected]);

  if (isLoading) {
    return (
      <div data-testid="model-catalog-page">
        <PageSkeleton variant="table" rows={8} />
      </div>
    );
  }

  const addToBlocklist = async (row: ModelCatalogRow) => {
    const pattern = row.id;
    try {
      await block.mutateAsync(pattern);
      push({
        tone: "ok",
        testId: "model-blocklist-toast",
        content: `已把 ${pattern} 加入黑名单，下一次 /v1/models 起不再下发。`,
      });
    } catch (caught) {
      push({
        tone: "danger",
        testId: "model-blocklist-toast",
        content: `加入黑名单失败：${caught instanceof Error ? caught.message : String(caught)}`,
      });
    }
  };

  const runProbe = async (row: ModelProbeRow) => {
    try {
      const result = await probeNow.mutateAsync({
        provider: row.provider, model: row.model,
      });
      // 三态文案不合并：inconclusive 若被写成「探活失败」，用户会以为模型坏了。
      push({
        tone: result.state === "ok" ? "ok"
          : result.state === "failed" ? "danger" : "warn",
        testId: "model-probe-toast",
        content: result.state === "ok"
          ? `${row.model} 探活成功，已清除该渠道的负缓存，模型立即回到池子。`
          : result.state === "failed"
            ? `${row.model} 确认不可用：${result.error || "上游无此模型"}`
            : `${row.model} 无法判定（不构成下线证据，未做任何禁用）：${result.error || "上游无内容"}`,
      });
    } catch (caught) {
      push({
        tone: "danger",
        testId: "model-probe-toast",
        content: `探活失败：${caught instanceof Error ? caught.message : String(caught)}`,
      });
    }
  };

  const toggleManual = async (row: ModelProbeRow) => {
    const manual = row.manual === "";
    try {
      await probeManual.mutateAsync({
        provider: row.provider, model: row.model, on: manual,
      });
      push({
        tone: "ok",
        testId: "model-probe-toast",
        content: manual
          ? `${row.model} 已人工接管：自动探活跳过它，直到你交还。`
          : `${row.model} 已交还自动探活：后台将按周期重新探它。`,
      });
    } catch (caught) {
      push({
        tone: "danger",
        testId: "model-probe-toast",
        content: `切换接管失败：${caught instanceof Error ? caught.message : String(caught)}`,
      });
    }
  };

  const priceUnit = `${currency === "CNY" ? "¥" : "$"} / 百万 token`;

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative">
        <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          data-testid="model-search"
          placeholder="搜索 id / 名称"
          className="h-8 w-full pl-7 text-xs sm:w-56"
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <Tabs
        value={currency}
        options={CURRENCIES}
        onChange={(value) => setCurrency(value as Currency)}
        testId="model-currency-tabs"
      />
      <Label className="w-fit cursor-pointer gap-1.5 text-xs font-normal text-muted-foreground">
        <Checkbox
          checked={onlyAvailable}
          data-testid="model-available-toggle"
          onCheckedChange={(value) => setOnlyAvailable(value === true)}
        />
        只看当前渠道
      </Label>
      <Label className="w-fit cursor-pointer gap-1.5 text-xs font-normal text-muted-foreground">
        <Checkbox
          checked={onlySuspected}
          data-testid="model-suspected-toggle"
          onCheckedChange={(value) => setOnlySuspected(value === true)}
        />
        只看疑似下线
      </Label>
    </div>
  );

  return (
    <div className="space-y-6" data-testid="model-catalog-page">
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        eyebrow="管理"
        title="模型列表"
        description="OpenRouter 模型目录：统计页「成本（估算）」的刊例价来源（输入 / 输出 / 缓存读，USD / 百万 token）与上下文、模态、知识截止等元数据；「能力分」来自 Artificial Analysis。"
        icon={<Boxes className="size-5" />}
      />

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Metric
          label="目录更新"
          value={data?.saved_at ? formatAgo(data.saved_at) : "—"}
          hint={data?.saved_at
            ? `${formatTime(data.saved_at)} · 价格 / 明细 / 能力分同一次抓取`
            : "尚无目录快照，成本与能力分将显示 —"}
          icon={<RefreshCw className="size-4" />}
        />
        <Metric
          label="能力分来源"
          value="Artificial Analysis"
          hint="经 OpenRouter 公开接口分发，非本服务实测"
          icon={<Trophy className="size-4" />}
        />
        <Metric
          label="汇率"
          value={`1 USD = ${formatNumber(rate)} CNY`}
          hint="可在「任务与配置」热更，仅影响之后写入的请求"
          icon={<CircleDollarSign className="size-4" />}
        />
      </section>

      <ProbePanel
        rows={probes.data?.rows ?? []}
        loading={probes.isLoading}
        error={probes.isError}
        busy={probeNow.isPending || probeManual.isPending}
        onProbe={(row) => void runProbe(row)}
        onManual={(row) => void toggleManual(row)}
      />

      {isError ? (
        <div data-testid="model-catalog-error">
          <Notice tone="danger">
            模型列表加载失败，请刷新重试；若刚升级服务，请确认后端已重启。
          </Notice>
        </div>
      ) : catalogCount === 0 ? (
        <Panel title="模型列表">
          <Empty data-testid="no-model-catalog">
            尚无模型目录快照（后台拉取 OpenRouter 后自动出现），统计里的成本与能力分会显示 —
          </Empty>
        </Panel>
      ) : (
        <Panel title={`模型列表（${total} / ${catalogCount}）`}>
          {/* 搜索与币种切换独立成行：放进 Panel 标题行会在窄视口挤压标题成竖排 */}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            {toolbar}
          </div>

          {total === 0 ? (
            <Empty data-testid="no-model-match">
              {query.trim()
                ? `没有匹配「${query}」的模型`
                : onlySuspected
                  ? `最近 ${windowDays} 天没有模型级失败（疑似下线为空）`
                  : "当前渠道没有可用模型（未接入或全部暂停）"}
            </Empty>
          ) : (
            <div className="mt-3">
              <Table
                data-testid="model-catalog-table"
                cards
                containerClassName="max-h-[70vh] overflow-auto"
              >
                {/* [&_th]:bg-muted：吸顶表头必须不透明，否则滚动时行内容会从
                    半透明的表头底色（基类 tr 的 bg-muted/40）里透出来。 */}
                <TableHeader className="sticky top-0 z-10 [&_th]:bg-muted">
                  <TableRow>
                    <SortableHead label="模型" columnKey="id" active={sort === "id"}
                                  direction={order} onToggle={toggle} testId="sort-id" />
                    <SortableHead label={`价格（${priceUnit}）`} columnKey="input"
                                  active={sort === "input"} direction={order}
                                  onToggle={toggle} align="right" testId="sort-input" />
                    <SortableHead label="能力分" columnKey="intelligence"
                                  active={sort === "intelligence"} direction={order}
                                  onToggle={toggle} testId="sort-intelligence"
                                  hint={<ColumnHint text="Artificial Analysis 三项指数（智 / 编 / 体），非本服务实测。点列头按「智」排序；无分数行排末尾。" />} />
                    <SortableHead label="上下文 / 输出" columnKey="context"
                                  active={sort === "context"} direction={order}
                                  onToggle={toggle} testId="sort-context" />
                    <TableHead>输入 → 输出模态</TableHead>
                    <TableHead>能力</TableHead>
                    <SortableHead label="知识截止" columnKey="knowledge"
                                  active={sort === "knowledge"} direction={order}
                                  onToggle={toggle} testId="sort-knowledge" />
                    <TableHead>可用性</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {models.map((row) => {
                    const caps = capabilities(row);
                    return (
                      <TableRow key={row.id} data-testid={`model-row-${row.id}`}>
                        <TableCell label="模型" className="max-w-[17rem]">
                          <div className="font-medium break-words">{row.name ?? row.id}</div>
                          <div className="font-mono text-xs break-all text-muted-foreground">
                            {row.id}
                          </div>
                        </TableCell>
                        <TableCell label="价格" className="text-right tabular-nums">
                          <div title="输入 单价 · 输出 单价">
                            <span className="text-muted-foreground">输入 </span>
                            {formatUnitPrice(row.input, currency, rate)}
                            <span className="text-muted-foreground"> · 输出 </span>
                            {formatUnitPrice(row.output, currency, rate)}
                          </div>
                          <div className="text-xs text-muted-foreground" title="缓存读 单价 · 缓存写 单价">
                            <span>缓存读 </span>
                            {formatUnitPrice(row.cache_read, currency, rate)}
                            <span> · 缓存写 </span>
                            {formatUnitPrice(row.cache_write, currency, rate)}
                          </div>
                        </TableCell>
                        <TableCell label="能力分" className="text-xs tabular-nums">
                          {benchmarkLine(row)}
                        </TableCell>
                        <TableCell label="上下文 / 输出" className="text-xs tabular-nums">
                          <div title="上下文窗口上限">{limitLine(row.context)}</div>
                          <div className="text-muted-foreground" title="单次输出上限">
                            {limitLine(row.max_output)}
                          </div>
                        </TableCell>
                        <TableCell label="模态" className="max-w-[9rem] text-xs whitespace-normal break-keep">
                          {modalityText(row.input_modalities)} →{" "}
                          {modalityText(row.output_modalities)}
                        </TableCell>
                        <TableCell label="能力" className="max-w-[12rem] text-xs whitespace-normal break-keep">
                          {caps.length === 0 ? "—" : caps.join(" · ")}
                        </TableCell>
                        <TableCell label="知识截止" className="text-xs">
                          {row.knowledge ?? "—"}
                        </TableCell>
                        <TableCell label="可用性" className="text-xs">
                          <AvailabilityCell
                            row={row}
                            windowDays={windowDays}
                            busy={block.isPending}
                            onBlock={() => void addToBlocklist(row)}
                          />
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
              <Pagination
                testId="model"
                page={page}
                pageSize={pageSize}
                total={total}
                busy={isFetching}
                onPageChange={setPage}
                onPageSizeChange={setPageSize}
              />
            </div>
          )}
        </Panel>
      )}

      <Notice tone="muted" className="items-start gap-1 px-3 py-2.5 text-xs">
        <p>
          模型 id 为 OpenRouter 的 id（含厂商前缀，如 z-ai/glm-5.3），与本服务对外模型名（归一键）不总是逐个相同；未匹配到定价的模型成本显示 —。价格为「按 token × 公开刊例价」的估算，不是上游真实扣费；匹配口径见「用量统计」页。
        </p>
        <p>
          本表是 OpenRouter 公开收录的范围（数百个模型）。价格列上行是「输入 / 输出」单价、下行是「缓存读 / 缓存写」单价（未声明缓存价时显示 —）。原始单位为 USD / 百万 token；切到人民币按当前汇率（1 USD = {formatNumber(rate)} CNY）折算，历史成本按写入时汇率定值、不重算。OpenRouter 的 `created` 是收录时间、不是发布日，故本表只列知识截止。
        </p>
      </Notice>
    </div>
  );
}
