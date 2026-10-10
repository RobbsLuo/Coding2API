import { useEffect, useMemo, useState } from "react";
import { Boxes, CircleDollarSign, Coins, RefreshCw, Search, Trophy } from "lucide-react";
import { useSessionContext } from "../Layout";
import { useModelCatalog } from "../api/hooks";
import {
  formatAgo,
  formatCompact,
  formatNumber,
  formatTime,
} from "../api/display";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SortableHead } from "../components/SortableHead";
import { useSort } from "../hooks/useSort";
import {
  Button,
  Empty,
  Input,
  Metric,
  Notice,
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
import type { ModelCatalogEntry } from "../api/types";

/** 币种切换：价格原始单位是 USD / 百万 token，成本展示以人民币为主，两者都提供。 */
type Currency = "USD" | "CNY";

const CURRENCIES = [
  { value: "USD", label: "美元" },
  { value: "CNY", label: "人民币" },
];

/** 分页步长：与「用量统计」明细表一致，默认 50（3536 条全渲染会拖慢搜索）。 */
const PAGE_SIZES = [20, 50, 100];

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

/** models.dev 模态取值 → 中文（未知取值原样回显，上游加值前端不炸）。 */
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
 * 居中的 —：models.dev 收录了 3500+ 条目，其中多数没有第三方成绩，逐个渲染
 * 三个 — 会让这一列显得很吵。
 */
function benchmarkLine(row: ModelCatalogEntry) {
  const b = row.benchmarks;
  if (!b) return <div className="text-muted-foreground">—</div>;
  const score = (value: number | undefined): string =>
    value === undefined ? "—" : value.toLocaleString("zh-CN");
  return (
    <div title="Artificial Analysis 指数（智能 / 编程 / 智能体），经 OpenRouter 公开接口；非本服务实测">
      <div>智 {score(b.intelligence_index)}</div>
      <div className="text-muted-foreground">编 {score(b.coding_index)}</div>
      <div className="text-muted-foreground">体 {score(b.agentic_index)}</div>
    </div>
  );
}

/**
 * 模型列表页（控制台）：只读展示 models.dev 的模型目录。
 *
 * 这份目录既是统计页「成本（估算）」的价格来源（输入 / 输出 / 缓存读，USD /
 * 百万 token），也带出 models.dev 更详细的元数据（上下文、模态、能力、知识
 * 截止等）。目录缺失（首次部署尚未拉取）时显示空态而非报错。
 */
export function ModelCatalogPage() {
  const session = useSessionContext();
  const { sort, order, toggle } = useSort("id", "asc",
    { context: "desc", max_output: "desc", input: "desc", output: "desc",
      cache_read: "desc", cache_write: "desc" });
  const { data, isLoading, isError } = useModelCatalog(session.username, sort, order);
  const [query, setQuery] = useState("");
  const [currency, setCurrency] = useState<Currency>("USD");
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(50);

  const rate = data?.usd_cny_rate ?? 1;
  const models = useMemo(() => data?.models ?? [], [data]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return models;
    return models.filter((row) =>
      row.id.toLowerCase().includes(needle) ||
      (row.name ?? "").toLowerCase().includes(needle) ||
      row.provider.toLowerCase().includes(needle),
    );
  }, [models, query]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const safePage = Math.min(pageIndex, pageCount - 1);
  const pageRows = useMemo(
    () => filtered.slice(safePage * pageSize, safePage * pageSize + pageSize),
    [filtered, safePage, pageSize],
  );

  // 搜索 / 改每页条数 / 换排序后回到第一页：否则会停在一个已不存在的页码上（空白）
  useEffect(() => {
    setPageIndex(0);
  }, [query, pageSize, sort, order]);

  if (isLoading) {
    return (
      <div data-testid="model-catalog-page">
        <PageSkeleton variant="table" rows={8} />
      </div>
    );
  }

  const priceUnit = `${currency === "CNY" ? "¥" : "$"} / 百万 token`;

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative">
        <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          data-testid="model-search"
          placeholder="搜索 id / 名称 / 渠道"
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
    </div>
  );

  return (
    <div className="space-y-6" data-testid="model-catalog-page">
      <PageHeader
        eyebrow="控制台"
        title="模型列表"
        description="models.dev 的模型目录：既列出统计页「成本（估算）」所用刊例价（输入 / 输出 / 缓存读，原始单位 USD / 百万 token），也带出上下文、模态、能力、知识截止等元数据；「能力分」列是 Artificial Analysis 指数（经 OpenRouter 公开接口），非本服务实测。两份目录都由后台任务周期拉取并落盘，这里只读展示。"
        icon={<Boxes className="size-5" />}
      />

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Metric
          label="模型数"
          value={formatNumber(data?.count ?? 0)}
          hint="models.dev 已收录的模型条目"
          icon={<Coins className="size-4" />}
        />
        <Metric
          label="目录更新"
          value={data?.saved_at ? formatAgo(data.saved_at) : "—"}
          hint={data?.saved_at ? formatTime(data.saved_at) : "尚无目录快照，成本将显示 —"}
          icon={<RefreshCw className="size-4" />}
        />
        <Metric
          label="能力分更新"
          value={data?.benchmark_saved_at ? formatAgo(data.benchmark_saved_at) : "—"}
          hint={data?.benchmark_saved_at
            ? `${formatTime(data.benchmark_saved_at)} · Artificial Analysis 指数（经 OpenRouter 公开接口）`
            : "尚无能力分快照（后台拉取后自动出现），「能力分」列将显示 —"}
          icon={<Trophy className="size-4" />}
        />
        <Metric
          label="汇率"
          value={`1 USD = ${formatNumber(rate)} CNY`}
          hint="成本折算汇率（可在「任务与配置」热更，仅影响之后写入的请求）"
          icon={<CircleDollarSign className="size-4" />}
        />
      </section>

      {isError ? (
        <div data-testid="model-catalog-error">
          <Notice tone="danger">
            模型列表加载失败，请刷新重试；若刚升级服务，请确认后端已重启加载新端点。
          </Notice>
        </div>
      ) : models.length === 0 ? (
        <Panel title="模型列表">
          <Empty data-testid="no-model-catalog">
            尚无模型目录快照（后台拉取 models.dev 后自动出现），统计里的成本会显示 —
          </Empty>
        </Panel>
      ) : (
        <Panel title={`模型列表（${filtered.length} / ${models.length}）`}>
          {/* 搜索与币种切换独立成行：放进 Panel 标题行会在窄视口挤压标题成竖排 */}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            {toolbar}
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              每页
              <Select
                value={String(pageSize)}
                data-testid="model-page-size"
                className="h-8 w-20 text-xs"
                onChange={(event) => setPageSize(Number(event.target.value))}
              >
                {PAGE_SIZES.map((size) => (
                  <option key={size} value={size}>{size} 条</option>
                ))}
              </Select>
            </label>
          </div>

          {filtered.length === 0 ? (
            <Empty data-testid="no-model-match">没有匹配「{query}」的模型</Empty>
          ) : (
            <div className="mt-3">
              <Table
                data-testid="model-catalog-table"
                containerClassName="max-h-[70vh] overflow-auto"
              >
                <TableHeader className="sticky top-0 z-10">
                  <TableRow>
                    <SortableHead label="模型" columnKey="id" active={sort === "id"}
                                  direction={order} onToggle={toggle} testId="sort-id" />
                    <SortableHead label="提供方" columnKey="provider" active={sort === "provider"}
                                  direction={order} onToggle={toggle} testId="sort-provider" />
                    <SortableHead label="上下文 / 输出" columnKey="context"
                                  active={sort === "context"} direction={order}
                                  onToggle={toggle} testId="sort-context" />
                    <TableHead>输入 → 输出模态</TableHead>
                    <TableHead>能力</TableHead>
                    <TableHead>能力分</TableHead>
                    <SortableHead label="知识 / 发布" columnKey="release_date"
                                  active={sort === "release_date"} direction={order}
                                  onToggle={toggle} testId="sort-release_date" />
                    <SortableHead label={`价格（${priceUnit}）`} columnKey="input"
                                  active={sort === "input"} direction={order}
                                  onToggle={toggle} align="right" testId="sort-input" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pageRows.map((row) => {
                    const caps = capabilities(row);
                    return (
                      <TableRow key={row.id} data-testid={`model-row-${row.id}`}>
                        <TableCell className="max-w-[17rem]">
                          <div className="font-medium break-words">{row.name ?? row.id}</div>
                          <div className="font-mono text-xs break-all text-muted-foreground">
                            {row.id}
                          </div>
                        </TableCell>
                        <TableCell className="font-mono text-xs">{row.provider}</TableCell>
                        <TableCell className="text-xs tabular-nums">
                          <div title="上下文窗口上限">{limitLine(row.context)}</div>
                          <div className="text-muted-foreground" title="单次输出上限">
                            {limitLine(row.max_output)}
                          </div>
                        </TableCell>
                        <TableCell className="max-w-[9rem] text-xs whitespace-normal break-keep">
                          {modalityText(row.input_modalities)} →{" "}
                          {modalityText(row.output_modalities)}
                        </TableCell>
                        <TableCell className="max-w-[12rem] text-xs whitespace-normal break-keep">
                          {caps.length === 0 ? "—" : caps.join(" · ")}
                        </TableCell>
                        <TableCell className="text-xs tabular-nums">
                          {benchmarkLine(row)}
                        </TableCell>
                        <TableCell className="text-xs">
                          <div>{row.knowledge ?? "—"}</div>
                          <div className="text-muted-foreground">
                            {row.release_date ?? "—"}
                          </div>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
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
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
              {pageCount > 1 && (
                <div className="mt-3 flex items-center justify-between">
                  <span className="text-xs text-muted-foreground" data-testid="model-page-info">
                    第 {safePage + 1} / {pageCount} 页
                  </span>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      data-testid="model-prev"
                      disabled={safePage === 0}
                      onClick={() => setPageIndex((p) => Math.max(0, p - 1))}
                    >
                      上一页
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      data-testid="model-next"
                      disabled={safePage >= pageCount - 1}
                      onClick={() => setPageIndex((p) => Math.min(pageCount - 1, p + 1))}
                    >
                      下一页
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </Panel>
      )}

      <Notice tone="muted" className="items-start gap-1 px-3 py-2.5 text-xs">
        <p>
          模型 id 为 models.dev 的 id（小写），与本服务对外模型名（归一键）并不总是逐个相同；未匹配到定价的模型成本显示 —。价格为「按 token × 公开刊例价」的估算依据，不是上游真实扣费；匹配口径见「用量统计」页。
        </p>
        <p>
          价格列上行是「输入 / 输出」单价、下行是「缓存读 / 缓存写」单价（未声明缓存价时显示 —）。同一模型可能挂在多个渠道下、价差极大，这里按「原厂优先，否则取输入价最高」选一条展示。币种：原始单位为 USD / 百万 token；切到人民币按当前汇率（1 USD = {formatNumber(rate)} CNY）折算，历史成本按写入时汇率定值，不随本页变化重算。
        </p>
      </Notice>
    </div>
  );
}
