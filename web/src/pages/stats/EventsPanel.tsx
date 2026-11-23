/**
 * 请求明细卡片：实时流、钻取过滤、导出、分页明细（自 `pages/StatsPage.tsx` 原样搬出）。
 *
 * 游标压栈留在页面（`onPageChange`）：翻页语义与取数游标栈同处一地，
 * 本组件只负责把点击回传。
 */
import { Button, Checkbox, Empty, Footnote, Label, Notice, Panel, Table, TableBody,
  TableHead, TableHeader, TableRow } from "../../ui";
import { Pagination } from "../../components/Pagination";
import { SortableHead } from "../../components/SortableHead";
import { SortMenuHead } from "../../components/SortMenuHead";
import { Download, X } from "lucide-react";
import { formatNumber } from "../../api/display";
import type { UsageEventRow } from "../../api/types";
import { eventRowCells, rowToneClass } from "./cells";
import {
  LATENCY_COLUMNS,
  LIVE_MAX_ROWS,
  SOURCE_COLUMNS,
  SPEED_COLUMNS,
  TOKEN_COLUMNS,
  type StatsSortState,
} from "./constants";

interface EventsPanelProps {
  totalRequests: number | undefined;
  live: boolean;
  toggleLive: (value: boolean) => void;
  isOperator: boolean;
  exporting: boolean;
  exportCsv: () => void;
  exportNotice: string | null;
  drillLabels: string[];
  clearDrill: () => void;
  liveStatus: "idle" | "live" | "error";
  liveRows: UsageEventRow[];
  isError: boolean;
  isFetching: boolean;
  eventRows: UsageEventRow[];
  eventSort: StatsSortState;
  toggleEventSort: (columnKey: string) => void;
  pageIndex: number;
  pageSize: number;
  hasNextPage: boolean;
  changePageSize: (size: number) => void;
  onPageChange: (next: number) => void;
}

export function EventsPanel(props: EventsPanelProps) {
  const {
    totalRequests, live, toggleLive, isOperator, exporting, exportCsv, exportNotice,
    drillLabels, clearDrill, liveStatus, liveRows, isError, isFetching, eventRows,
    eventSort, toggleEventSort, pageIndex, pageSize, hasNextPage, changePageSize,
    onPageChange,
  } = props;
  return (
    <Panel data-testid="events-panel" title="请求明细" action={
      <div className="flex items-center gap-3 text-xs text-muted-foreground">
        <span>共 {formatNumber(totalRequests)} 条 · 保留 90 天</span>
        {/* 实时流：默认关闭（翻页看历史是常态），打开后追加「刚落库」的行 */}
        <Label className="w-fit cursor-pointer gap-1.5 text-xs font-normal text-muted-foreground">
          <Checkbox checked={live} data-testid="events-live-toggle"
                    onCheckedChange={(value) => toggleLive(value === true)} />
          实时
        </Label>
        {/* 导出限 admin/operator：整份明细落盘比翻页浏览的暴露面大 */}
        {isOperator && (
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
                  {isOperator && <TableHead>用户</TableHead>}
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
                  <TableRow key={`live-${row.rowid}`} data-testid="live-row"
                            className={rowToneClass(row)}>
                    {eventRowCells(row, isOperator)}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
      )}
      {isError ? (
        <Notice tone="danger" data-testid="events-error">
          请求明细加载失败，请刷新重试；若刚升级服务，请确认后端已重启加载新端点。
        </Notice>
      ) : eventRows.length === 0 ? (
        <Empty data-testid="no-events">该范围内没有请求明细</Empty>
      ) : (
        <>
          {/* 明细表列多（时间/用户/渠道凭证/模型/状态/Token/Credit/成本/延迟/速度），
              默认单元格内边距在 1280px 下会让表比容器宽十几像素、逼出横向滚动。
              这里收窄到 px-1.5：省下的正是最后两列被裁掉的宽度。 */}
          <Table data-testid="events-table" cards
                 className="[&_td]:px-1.5 [&_th]:px-1.5">
            <TableHeader>
              <TableRow>
                <SortableHead label="时间" columnKey="ts" active={eventSort.sort === "ts"}
                              direction={eventSort.order} onToggle={toggleEventSort}
                              testId="event-sort-ts" />
                {isOperator && (
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
                    请求不回落估算，直接 —）。列头下拉可选子列排序。
                    单位写进列头，行内只留数字（10 列的表每行重复 tok/s 要多占 45px）。
                    用「速度」+ 小字 tok/s 两行，避免列头被撑宽挤掉数据列。 */}
                <SortMenuHead label="速度" unit="tok/s" options={[...SPEED_COLUMNS]}
                              activeKey={eventSort.sort} direction={eventSort.order}
                              onToggle={toggleEventSort} testId="event-sort-speed" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {eventRows.map((row) => (
                <TableRow key={row.rowid} data-testid="event-row"
                          className={rowToneClass(row)}>
                  {eventRowCells(row, isOperator)}
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Pagination
            testId="events"
            page={pageIndex}
            pageSize={pageSize}
            hasNext={hasNextPage}
            busy={isFetching}
            onPageSizeChange={changePageSize}
            onPageChange={onPageChange}
          />
          <Footnote summary="口径说明（速度的分子与分母）" testId="events-footnote">
            速度 = 输出 token ÷ 生成时间（耗时 − 首字）：左侧正文（不含思考）、右侧总产出
            （含思考），与分组表同规则（有输出 token、生成窗口 ≥200ms、单条 ≤1000 tok/s）。
            逐行不按端到端耗时估算——单条样本摊不平首字等待，故无生成窗口的请求显示 —。
          </Footnote>
        </>
      )}
    </Panel>
  );
}
