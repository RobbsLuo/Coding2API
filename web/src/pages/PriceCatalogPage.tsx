import { useMemo, useState } from "react";
import { CircleDollarSign, Coins, RefreshCw, Search } from "lucide-react";
import { useSessionContext } from "../Layout";
import { usePricing } from "../api/hooks";
import { formatAgo, formatNumber, formatTime } from "../api/display";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import {
  Empty,
  Input,
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

/** 币种切换：价表原始单位是 USD / 百万 token，成本展示以人民币为主，两者都提供。 */
type Currency = "USD" | "CNY";

const CURRENCIES = [
  { value: "USD", label: "美元" },
  { value: "CNY", label: "人民币" },
];

/** 单价格式化：刊例价量级跨度大（0.02 ~ 数十），保留最多 4 位小数。 */
function formatUnitPrice(value: number, currency: Currency, rate: number): string {
  const converted = currency === "CNY" ? value * rate : value;
  const symbol = currency === "CNY" ? "¥" : "$";
  return `${symbol}${converted.toLocaleString("zh-CN", { maximumFractionDigits: 4 })}`;
}

/**
 * 价表页（控制台）：只读展示当前生效的 models.dev 刊例价。
 *
 * 这份表是统计页「成本（估算）」的输入——在这里能看到每个模型被怎么计价、
 * 缺价的模型为何显示 —。价表缺失（首次部署尚未拉取）时显示空态而非报错。
 */
export function PriceCatalogPage() {
  const session = useSessionContext();
  const { data, isLoading, isError } = usePricing(session.username);
  const [query, setQuery] = useState("");
  const [currency, setCurrency] = useState<Currency>("USD");

  const rate = data?.usd_cny_rate ?? 1;
  const models = useMemo(() => data?.models ?? [], [data]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return models;
    return models.filter((row) => row.model.toLowerCase().includes(needle));
  }, [models, query]);

  if (isLoading) {
    return (
      <div data-testid="pricing-page">
        <PageSkeleton variant="table" rows={8} />
      </div>
    );
  }

  const priceUnit = `${currency === "CNY" ? "¥" : "$"} / 百万 token`;

  return (
    <div className="space-y-6" data-testid="pricing-page">
      <PageHeader
        eyebrow="控制台"
        title="价表"
        description="统计页「成本（估算）」所用的 models.dev 模型刊例价（输入 / 输出 / 缓存命中，原始单位 USD / 百万 token）。价表由后台任务周期拉取并落盘，这里只读展示。"
        icon={<CircleDollarSign className="size-5" />}
      />

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Metric
          label="模型数"
          value={formatNumber(data?.count ?? 0)}
          hint="已收录定价的模型条目"
          icon={<Coins className="size-4" />}
        />
        <Metric
          label="快照更新"
          value={data?.saved_at ? formatAgo(data.saved_at) : "—"}
          hint={data?.saved_at ? formatTime(data.saved_at) : "尚无价表快照，成本将显示 —"}
          icon={<RefreshCw className="size-4" />}
        />
        <Metric
          label="汇率"
          value={`1 USD = ${formatNumber(rate)} CNY`}
          hint="成本折算汇率（可在「任务与配置」热更，仅影响之后写入的请求）"
          icon={<CircleDollarSign className="size-4" />}
        />
      </section>

      {isError ? (
        <div data-testid="pricing-error">
          <Notice tone="danger">
            价表加载失败，请刷新重试；若刚升级服务，请确认后端已重启加载新端点。
          </Notice>
        </div>
      ) : models.length === 0 ? (
        <Panel title="模型价表">
          <Empty data-testid="no-pricing">
            尚无价表快照（后台拉取 models.dev 后自动出现），统计里的成本会显示 —
          </Empty>
        </Panel>
      ) : (
        <Panel
          title={`模型价表（${filtered.length} / ${models.length}）`}
          action={
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={query}
                  data-testid="price-search"
                  placeholder="搜索模型 id"
                  className="h-8 w-44 pl-7 text-xs"
                  onChange={(event) => setQuery(event.target.value)}
                />
              </div>
              <Tabs
                value={currency}
                options={CURRENCIES}
                onChange={(value) => setCurrency(value as Currency)}
                testId="price-currency-tabs"
              />
            </div>
          }
        >
          {filtered.length === 0 ? (
            <Empty data-testid="no-price-match">没有匹配「{query}」的模型</Empty>
          ) : (
            <Table data-testid="pricing-table">
              <TableHeader>
                <TableRow>
                  <TableHead>模型</TableHead>
                  <TableHead className="text-right">输入（{priceUnit}）</TableHead>
                  <TableHead className="text-right">输出（{priceUnit}）</TableHead>
                  <TableHead className="text-right">缓存命中（{priceUnit}）</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map((row) => (
                  <TableRow key={row.model} data-testid={`price-row-${row.model}`}>
                    <TableCell className="font-mono text-xs">{row.model}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatUnitPrice(row.input, currency, rate)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatUnitPrice(row.output, currency, rate)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatUnitPrice(row.cache_read, currency, rate)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Panel>
      )}

      <Notice tone="muted" className="items-start gap-1 px-3 py-2.5 text-xs">
        <p>
          价表是「按 token × 公开刊例价」的估算依据，不是上游真实扣费；匹配口径与成本说明见「用量统计」页。
          模型 id 为 models.dev 的 id（小写），与本服务对外模型名（归一键）并不总是逐个相同，未匹配到的模型成本显示 —。
        </p>
        <p>
          币种：价表原始单位为 USD / 百万 token；切到人民币按当前汇率（1 USD = {formatNumber(rate)} CNY）折算。
          实际成本在写入明细时以当时汇率定值，历史行不随本页汇率变化重算。
        </p>
      </Notice>
    </div>
  );
}
