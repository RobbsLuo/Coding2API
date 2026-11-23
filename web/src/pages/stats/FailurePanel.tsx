/**
 * 失败归因卡片（operator+）：自 `pages/StatsPage.tsx` 原样搬出。
 *
 * 计数条按本批最大值归一（相对长度才有意义）；全 0 时给 1 避免除零。
 */
import { Footnote, Notice, Panel, Table, TableBody, TableCell, TableHead,
  TableHeader, TableRow } from "../../ui";
import { ProviderIcon } from "../../components/ProviderIcon";
import { formatNumber, formatTime, usageErrorLabel } from "../../api/display";
import { providerLabel } from "../../api/providers";
import type { StatsDrill } from "../../api/types";
import type { StatsErrorRow } from "../../api/statsErrors";

interface FailurePanelProps {
  isOperator: boolean;
  isError: boolean;
  errorRows: StatsErrorRow[];
  onDrill: (next: StatsDrill & { ok?: number }) => void;
}

export function FailurePanel(props: FailurePanelProps) {
  const { isOperator, isError, errorRows, onDrill } = props;
  // 计数条按本批最大值归一（相对长度才有意义）；全 0 时给 1 避免除零
  const maxFailures = Math.max(1, ...errorRows.map((row) => row.failures));
  const barWidth = (failures: number) => Math.round((failures / maxFailures) * 100);
  /** 失败数减去换号救回数 = 未被救回的失败尝试数（rescued 恒 ≤ failures）。 */
  const unrescued = (row: StatsErrorRow) => row.failures - row.rescued;

  return (
    <>
      {/* 失败归因（operator+）：跨用户看「哪里在坏」。点行 → 钻取到该渠道+模型
          的失败明细（ok=0），与下方「请求明细」联动。
          无失败时整张卡不渲染：一张只有「该范围内没有失败请求」的空卡仍占 239px
          （标题栏 + 空态 + 口径说明），而「没有失败」本身就是好消息，不必占版面。
          错误态仍要显示，否则加载失败会被误读成「零失败」。 */}
      {isOperator && (isError || errorRows.length > 0) && (
        <Panel data-testid="error-panel" title="失败归因" action={
          <span className="text-xs text-muted-foreground">
            按渠道 / 模型 / 错误类型分组 · 取自逐请求明细（仅 90 天）
          </span>
        }>
          {isError ? (
            <div data-testid="errors-error">
              <Notice tone="danger">
                失败归因加载失败，请刷新重试；若刚升级服务，请确认后端已重启加载新端点。
              </Notice>
            </div>
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
                    onClick={() => onDrill({ provider: row.provider, model: row.model,
                                                ok: 0 })}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onDrill({ provider: row.provider, model: row.model, ok: 0 });
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
          <Footnote summary="口径说明（失败数按尝试计）" testId="error-footnote">
            窗口缺省为最近 24 小时（与上方时间范围一致时按所选范围统计）；
            未上报错误类型的历史行显示为「失败」。失败数是<strong>尝试</strong>口径：
            换号重试每次上游拒绝都记一行，「未救回」是未被救回的失败尝试数
            （同一请求多次失败会算多次；同请求另有成功行即算救回）。点任意一行可钻取到下方「请求明细」（只看失败）。
          </Footnote>
        </Panel>
      )}
    </>
  );
}
