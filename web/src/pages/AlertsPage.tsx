import { BellRing } from "lucide-react";
import { ALERT_RULE_LABEL, formatTime } from "../api/display";
import { useAlerts } from "../api/hooks";
import { useSessionContext } from "../Layout";
import { PageHeader } from "../components/PageHeader";
import {
  Badge,
  Empty,
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
  // 30 秒轮询：告警是「刚刚发生了什么」，用户停留时自动刷新。
  const { data, isLoading } = useAlerts(session.username);

  const alerts = data?.alerts ?? [];

  return (
    <div className="space-y-6" data-testid="alerts-page">
      <PageHeader
        title="运维告警"
        description="后台周期评估四类风险：凭证池耗尽、后台任务连续失败、token 临近到期、上游错误率骤升。命中即落库并可选推送 webhook；同一条告警在静默窗内只记一次。"
        icon={<BellRing className="size-5" />}
      />

      <Panel
        title={`最近告警${data ? `（${alerts.length} 条）` : ""}`}
        aria-label="告警记录"
      >
        {isLoading ? (
          <Empty>载入中…</Empty>
        ) : alerts.length === 0 ? (
          <Empty data-testid="no-alerts">暂无告警记录</Empty>
        ) : (
          <Table data-testid="alerts-table">
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>级别</TableHead>
                <TableHead>规则</TableHead>
                <TableHead>对象</TableHead>
                <TableHead>说明</TableHead>
                <TableHead>推送</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {alerts.map((alert) => (
                <TableRow key={alert.id} data-testid={`alert-row-${alert.id}`}>
                  <TableCell className="whitespace-nowrap text-xs">
                    {formatTime(alert.ts)}
                  </TableCell>
                  <TableCell>
                    <Badge tone={severityTone(alert.severity)}>
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
                      <Badge tone="ok">已推送</Badge>
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