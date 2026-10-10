import { BellRing } from "lucide-react";
import { ALERT_RULE_LABEL, formatTime } from "../api/display";
import { useAlerts } from "../api/hooks";
import { useSessionContext } from "../Layout";
import { useSort } from "../hooks/useSort";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SortableHead } from "../components/SortableHead";
import {
  Badge,
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
  // 30 秒轮询：告警是「刚刚发生了什么」，用户停留时自动刷新。
  const { data, isLoading } = useAlerts(session.username, 30_000, sort, order);

  const alerts = data?.alerts ?? [];

  return (
    <div className="space-y-6" data-testid="alerts-page">
      <PageHeader
        eyebrow="运维"
        title="运维告警"
        description="后台周期评估四类风险：凭证池耗尽、后台任务连续失败、token 临近到期、上游错误率骤升。命中即落库并可选推送 webhook；同一条告警在静默窗内只记一次。"
        icon={<BellRing className="size-5" />}
      />

      <Panel
        title={`最近告警${data ? `（${alerts.length} 条）` : ""}`}
        aria-label="告警记录"
      >
        {isLoading ? (
          <PageSkeleton variant="table" rows={5} />
        ) : alerts.length === 0 ? (
          <EmptyState
            icon={<BellRing className="size-5" />}
            title="暂无告警记录"
            description="后台任务评估命中风险后会在此落库；一切正常时这里保持空白。"
            data-testid="no-alerts"
          />
        ) : (
          <Table data-testid="alerts-table">
            <TableHeader>
              <TableRow>
                <SortableHead label="时间" columnKey="ts" active={sort === "ts"}
                              direction={order} onToggle={toggle} testId="sort-ts" />
                <SortableHead label="级别" columnKey="severity" active={sort === "severity"}
                              direction={order} onToggle={toggle} testId="sort-severity" />
                <SortableHead label="规则" columnKey="rule" active={sort === "rule"}
                              direction={order} onToggle={toggle} testId="sort-rule" />
                <SortableHead label="对象" columnKey="scope" active={sort === "scope"}
                              direction={order} onToggle={toggle} testId="sort-scope" />
                <TableHead>说明</TableHead>
                <SortableHead label="推送" columnKey="delivered" active={sort === "delivered"}
                              direction={order} onToggle={toggle} testId="sort-delivered" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {alerts.map((alert) => (
                <TableRow key={alert.id} data-testid={`alert-row-${alert.id}`}>
                  <TableCell className="whitespace-nowrap text-xs tabular-nums">
                    {formatTime(alert.ts)}
                  </TableCell>
                  <TableCell>
                    <Badge tone={severityTone(alert.severity)} dot>
                      {alert.severity === "critical" ? "严重" : "警告"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs">
                    {ALERT_RULE_LABEL[alert.rule] ?? alert.rule}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{alert.scope || "—"}</TableCell>
                  <TableCell className="text-xs">{alert.message}</TableCell>
                  <TableCell>
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
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      <Notice tone="muted">
        告警记录与请求明细同一保留期（默认 90 天），由后台清理任务统一裁剪。
        配置 webhook 地址与各规则阈值见「任务与配置」页的「运维告警」卡片。
      </Notice>
    </div>
  );
}
