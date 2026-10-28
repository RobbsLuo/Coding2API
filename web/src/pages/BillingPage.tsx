import { useEffect, useState } from "react";
import { Download, Receipt } from "lucide-react";
import { api } from "../api/client";
import { formatCompact, formatMoney, formatNumber } from "../api/display";
import type { BillingReport, BillingRow } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import {
  Button,
  EmptyState,
  Notice,
  Panel,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";

/** 当前本地月份（`YYYY-MM`）：与服务端默认口径一致（本地时区整月）。 */
function currentMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function tokenCell(row: BillingRow): string {
  return `${formatCompact(row.input_tokens)} / ${formatCompact(row.output_tokens)}`;
}

/**
 * 月度账单：按人 / 按 Key 两套汇总 + CSV / JSONL 导出。
 *
 * 页面只读一次（切月份才重新拉）：账单是结账视图，不是实时看板，加轮询只会
 * 让数字在眼前跳动。原始明细在「用量统计」页，这里不重复做下钻。
 */
export function BillingPage() {
  const [month, setMonth] = useState(currentMonth);
  const [report, setReport] = useState<BillingReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<"csv" | "jsonl" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.billing(month)
      .then((data) => {
        if (!cancelled) setReport(data);
      })
      .catch((caught) => {
        if (!cancelled) setError(
          caught instanceof Error ? caught.message : "账单加载失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [month]);

  const exportFile = async (format: "csv" | "jsonl") => {
    setExporting(format);
    setNotice(null);
    try {
      const { blob } = await api.billingExport(month, format);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `billing-${month}.${format}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setNotice(`已导出 ${month} 的 ${format.toUpperCase()} 账单。`);
    } catch (caught) {
      setNotice(caught instanceof Error ? `导出失败：${caught.message}` : "导出失败。");
    } finally {
      setExporting(null);
    }
  };

  const totals = report?.totals;
  return (
    <div className="space-y-6" data-testid="billing-page">
      <PageHeader
        eyebrow="运维"
        title="月度账单"
        icon={<Receipt className="size-4" />}
        description="按用户与 API Key 汇总指定月份的请求、token 与成本；可导出 CSV（按人）或 JSONL（逐条明细）。"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="month"
              aria-label="账单月份"
              data-testid="billing-month"
              value={month}
              onChange={(event) => setMonth(event.target.value || currentMonth())}
              className="h-8 rounded-lg border border-input bg-background px-2.5 text-sm"
            />
            <Button
              size="sm"
              variant="default"
              data-testid="billing-export-csv"
              disabled={exporting !== null}
              onClick={() => void exportFile("csv")}
            >
              <Download className="size-3.5" /> 导出 CSV
            </Button>
            <Button
              size="sm"
              variant="default"
              data-testid="billing-export-jsonl"
              disabled={exporting !== null}
              onClick={() => void exportFile("jsonl")}
            >
              <Download className="size-3.5" /> 导出 JSONL
            </Button>
          </div>
        }
      />

      {notice && <Notice tone="muted">{notice}</Notice>}
      {error && <Notice tone="danger">{error}</Notice>}
      {loading && <PageSkeleton rows={3} />}

      {!loading && report && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" data-testid="billing-totals">
            <Panel className="p-4">
              <div className="text-xs text-muted-foreground">请求数</div>
              <div className="mt-1 text-xl font-semibold">{formatNumber(totals?.requests)}</div>
            </Panel>
            <Panel className="p-4">
              <div className="text-xs text-muted-foreground">输入 / 输出 token</div>
              <div className="mt-1 text-xl font-semibold">
                {formatCompact(totals?.input_tokens)} / {formatCompact(totals?.output_tokens)}
              </div>
            </Panel>
            <Panel className="p-4">
              <div className="text-xs text-muted-foreground">成本（人民币）</div>
              <div className="mt-1 text-xl font-semibold">{formatMoney(totals?.cost_cny)}</div>
            </Panel>
            <Panel className="p-4">
              <div className="text-xs text-muted-foreground">成功率</div>
              <div className="mt-1 text-xl font-semibold">{percent(totals?.success_rate)}</div>
            </Panel>
          </div>

          <Panel title="按用户" data-testid="billing-users">
            {report.users.length === 0 ? (
              <EmptyState title="该月没有请求记录" description="换一个月，或确认明细是否已过保留期。" />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>用户</TableHead>
                    <TableHead>请求</TableHead>
                    <TableHead>成功</TableHead>
                    <TableHead>输入 / 输出 token</TableHead>
                    <TableHead>成本</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {report.users.map((row) => (
                    <TableRow key={row.username ?? "-"}>
                      <TableCell>{row.username ?? "—"}</TableCell>
                      <TableCell>{formatNumber(row.requests)}</TableCell>
                      <TableCell>{formatNumber(row.ok_count)}</TableCell>
                      <TableCell>{tokenCell(row)}</TableCell>
                      <TableCell>{formatMoney(row.cost_cny)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Panel>

          <Panel title="按 API Key" data-testid="billing-keys">
            {report.keys.length === 0 ? (
              <EmptyState title="该月没有带 Key 的请求"
                description="明细只保留 90 天；更早的月份按 Key 聚合为空，按人汇总仍来自小时表。" />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Key</TableHead>
                    <TableHead>归属用户</TableHead>
                    <TableHead>请求</TableHead>
                    <TableHead>输入 / 输出 token</TableHead>
                    <TableHead>成本</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {report.keys.map((row) => (
                    <TableRow key={row.api_key_id ?? "-"}>
                      <TableCell>
                        {row.key_name || (row.api_key_id ?? "").slice(0, 12) || "—"}
                      </TableCell>
                      <TableCell>{row.username ?? "—"}</TableCell>
                      <TableCell>{formatNumber(row.requests)}</TableCell>
                      <TableCell>{tokenCell(row)}</TableCell>
                      <TableCell>{formatMoney(row.cost_cny)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}
