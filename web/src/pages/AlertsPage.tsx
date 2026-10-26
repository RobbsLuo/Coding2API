import { BellRing, CheckCheck } from "lucide-react";
import { useState } from "react";
import { ALERT_RULE_LABEL, formatTime } from "../api/display";
import { useAlerts, useResolveAlert } from "../api/hooks";
import { useSessionContext } from "../Layout";
import { useSort } from "../hooks/useSort";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { Pagination } from "../components/Pagination";
import { SortableHead } from "../components/SortableHead";
import { DEFAULT_PAGE_SIZE } from "../lib/constants";
import {
  Badge,
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

/** 严重度 → 徽标色调：critical 红、warning 黄，未知回落到中性。 */
function severityTone(severity: string): "danger" | "warn" | "muted" {
  if (severity === "critical") return "danger";
  if (severity === "warning") return "warn";
  return "muted";
}

export function AlertsPage() {
  const session = useSessionContext();
  const { sort, order, toggle } = useSort("ts", "desc", { ts: "desc" });
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  // 30 秒轮询：告警是「刚刚发生了什么」，用户停留时自动刷新。
  const { data, isLoading, isFetching, isError } = useAlerts(
    session.username, 30_000, sort, order, pageSize, page * pageSize);
  const resolve = useResolveAlert(session.username);

  const alerts = data?.alerts ?? [];
  const total = data?.total ?? 0;
  // 正在处置的那条：只禁用它自己的按钮，别把整张表锁住
  const resolvingId = resolve.isPending ? resolve.variables : undefined;

  // 换排序 = 换数据集：回到第一页，避免停在越界页码上（空白）
  const toggleSort = (columnKey: string) => {
    toggle(columnKey);
    setPage(0);
  };
  const changePageSize = (size: number) => {
    setPageSize(size);
    setPage(0);
  };

  return (
    <div className="space-y-6" data-testid="alerts-page">
      <PageHeader
        eyebrow="运维"
        title="运维告警"
        description="后台周期评估四类风险：凭证池耗尽、后台任务连续失败、token 临近到期、上游错误率骤升。命中即落库并可推送 webhook；同一告警静默窗内只记一次，恢复后标「已恢复」并推送。规则不再命中时后台自动标记恢复；规则已下线或误报时可手动标记。"
        icon={<BellRing className="size-5" />}
      />

      {resolve.isError && (
        <div data-testid="resolve-error" role="alert">
          <Notice tone="danger" className="text-sm">
            标记恢复失败：{resolve.error instanceof Error ? resolve.error.message : "未知错误"}
          </Notice>
        </div>
      )}

      <Panel
        title={`最近告警${data ? `（共 ${total} 条）` : ""}`}
        aria-label="告警记录"
      >
        {isLoading ? (
          <PageSkeleton variant="table" rows={5} />
        ) : isError ? (
          // 加载失败不是「无告警」：此前显示「暂无告警记录…一切正常时保持空白」，
          // 用户会把故障读成「系统健康」，故障被完全吞掉。
          <EmptyState
            icon={<BellRing className="size-5" />}
            title="告警记录加载失败"
            description="服务暂时不可用或会话已过期，请稍后重试。"
            data-testid="alerts-error-state"
          />
        ) : alerts.length === 0 ? (
          <EmptyState
            icon={<BellRing className="size-5" />}
            title="暂无告警记录"
            description="后台评估命中风险后在此落库；一切正常时保持空白。"
            data-testid="no-alerts"
          />
        ) : (
          <>
            <Table data-testid="alerts-table" cards>
              <TableHeader>
                <TableRow>
                  <SortableHead label="时间" columnKey="ts" active={sort === "ts"}
                                direction={order} onToggle={toggleSort} testId="sort-ts" />
                  <SortableHead label="级别" columnKey="severity" active={sort === "severity"}
                                direction={order} onToggle={toggleSort} testId="sort-severity" />
                  <SortableHead label="规则" columnKey="rule" active={sort === "rule"}
                                direction={order} onToggle={toggleSort} testId="sort-rule" />
                  <SortableHead label="对象" columnKey="scope" active={sort === "scope"}
                                direction={order} onToggle={toggleSort} testId="sort-scope" />
                  <TableHead>说明</TableHead>
                  <SortableHead label="推送" columnKey="delivered" active={sort === "delivered"}
                                direction={order} onToggle={toggleSort} testId="sort-delivered" />
                  <TableHead>操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {alerts.map((alert) => (
                  <TableRow key={alert.id} data-testid={`alert-row-${alert.id}`}>
                    <TableCell label="时间" className="whitespace-nowrap text-xs tabular-nums">
                      {formatTime(alert.ts)}
                    </TableCell>
                    <TableCell label="级别">
                      {alert.resolved_at !== null ? (
                        <Badge tone="ok" dot>
                          已恢复
                        </Badge>
                      ) : (
                        <Badge tone={severityTone(alert.severity)} dot>
                          {alert.severity === "critical" ? "严重" : "警告"}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell label="规则" className="text-xs">
                      {ALERT_RULE_LABEL[alert.rule] ?? alert.rule}
                    </TableCell>
                    <TableCell label="对象" className="font-mono text-xs">
                      {alert.scope || "—"}
                    </TableCell>
                    <TableCell label="说明" className="text-xs">{alert.message}</TableCell>
                    <TableCell label="推送">
                      {alert.delivered === 1 ? (
                        <Badge tone="ok" dot>
                          已推送
                        </Badge>
                      ) : (
                        <Badge tone="muted">
                          {alert.delivery_error ? "推送失败" : "站内"}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell label="操作">
                      {alert.resolved_at === null ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          data-testid={`alert-resolve-${alert.id}`}
                          disabled={resolvingId === alert.id}
                          onClick={() => resolve.mutate(alert.id)}
                        >
                          <CheckCheck className="mr-1 size-3" />
                          标记恢复
                        </Button>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Pagination
              testId="alerts"
              page={page}
              pageSize={pageSize}
              total={total}
              busy={isFetching}
              onPageChange={setPage}
              onPageSizeChange={changePageSize}
            />
          </>
        )}
      </Panel>

      <Notice tone="muted">
        告警记录与请求明细同一保留期（默认 90 天），由后台清理任务裁剪。
        webhook 地址与各规则阈值见「任务与配置」页的「运维告警」卡片。
        「标记恢复」用于规则已下线或误报等后台无法自动判定的情况，操作会记入审计。
      </Notice>
    </div>
  );
}
