import {
  CREDIT_SOURCE_LABEL,
  creditEventLabel,
  formatNumber,
  formatTime,
} from "../../api/display";
import type { CheckinEvent, CreditEvent } from "../../api/types";
import type { Ledger } from "../../lib/credentialList";
import { CredentialTimeline } from "../../components/CredentialTimeline";
import { SortableHead } from "../../components/SortableHead";
import {
  Button,
  Card,
  Empty,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../ui";

/**
 * 变动流水抽屉：同一行内展开，不弹层。
 *
 * 语义纪律：这里展示的是**两次额度探测之间的净变化**，不是动作归因。上游
 * 签到/成长接口不打日志，diff 看不到分数是谁加的，所以文案一律说「净变化」，
 * 只有归因已知度（observed/sync）。把净变化说成「签到获得」就是拿猜测当事实。
 *
 * `ledger` 标明读的是哪本账：`credits`＝积分账（内置组），其余＝每日池。标题
 * 与说明都按它写，否则内置组会把积分账的流水读成每日池的。
 */
export function CreditDrawer({
  credentialId,
  ledger,
  events,
  sort,
  order,
  onSort,
  onClose,
}: {
  credentialId: string;
  /** 本抽屉讲哪一本账：credits＝积分账（内置组），pool＝每日池（其余） */
  ledger: Ledger;
  events: CreditEvent[];
  sort: string;
  order: string;
  onSort: (columnKey: string) => void;
  onClose: () => void;
}) {
  const creditsLedger = ledger === "credits";
  return (
    <Card
      className="mt-4 gap-0 bg-muted/30 p-3 ring-border/60"
      data-testid={`credit-drawer-${credentialId}`}
    >
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-medium">
          {creditsLedger ? "积分流水" : "额度流水"}
          <span className="ml-2 text-xs text-muted-foreground">
            {creditsLedger
              ? "积分账（内置模型计费）两次额度探测之间的净变化（非动作归因）"
              : "每日额度池两次额度探测之间的净变化（非动作归因）"}
          </span>
        </div>
        <Button size="sm" variant="ghost" onClick={onClose} data-testid="credit-drawer-close">
          关闭
        </Button>
      </div>
      {events.length === 0 ? (
        <Empty data-testid="credit-drawer-empty">
          还没有记录。余额变化会在下一轮额度探测时写入（首次探测只建立基线）。
        </Empty>
      ) : (
        <Table data-testid="credit-drawer-table" cards>
          <TableHeader>
            <TableRow>
              <SortableHead label="观测时间" columnKey="ts" active={sort === "ts"}
                            direction={order as "asc" | "desc"} onToggle={onSort}
                            testId="sort-credit-ts" />
              <SortableHead label="区间起点" columnKey="window_start"
                            active={sort === "window_start"} direction={order as "asc" | "desc"}
                            onToggle={onSort} testId="sort-credit-window_start" />
              <SortableHead label="变化" columnKey="delta" active={sort === "delta"}
                            direction={order as "asc" | "desc"} onToggle={onSort}
                            testId="sort-credit-delta" />
              <SortableHead label="说明" columnKey="source" active={sort === "source"}
                            direction={order as "asc" | "desc"} onToggle={onSort}
                            testId="sort-credit-source" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {events.map((event) => (
              <TableRow key={event.id} data-testid={`credit-event-${event.id}`}>
                <TableCell label="观测时间" className="text-xs">
                  {formatTime(event.ts)}
                </TableCell>
                <TableCell label="区间起点" className="text-xs text-muted-foreground">
                  {event.window_start ? formatTime(event.window_start) : "—"}
                </TableCell>
                <TableCell
                  label="变化"
                  className={`text-xs ${
                    event.delta === null
                      ? "text-muted-foreground"
                      : event.delta > 0
                        ? "text-ok"
                        : event.delta < 0
                          ? "text-warn"
                          : "text-muted-foreground"
                  }`}
                >
                  {creditEventLabel(event)}
                </TableCell>
                <TableCell label="说明" className="text-xs text-muted-foreground">
                  {CREDIT_SOURCE_LABEL[event.source] ?? event.source}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {/* 凭证详情抽屉的可用性时间线：与额度流水同一抽屉，但数据源独立（懒加载）。 */}
      <CredentialTimeline credentialId={credentialId} />
    </Card>
  );
}

/**
 * 签到记录抽屉：同一行内展开，不弹层。
 *
 * 数据来自本地落库的 `checkin_events`（每次签到落一行），**不实时查上游**——
 * 因此对已失效 / 未探测的凭证也能回看历史。像「界面显示签到成功但没加积分」
 * 这类问题，答案就在这张表里：时间 + 结果 + 领取积分 + 上游业务码。
 *
 * 只读：不动上游。手动签到仍在行内的「签到」按钮（本抽屉只是把它记下的结果
 * 摊开看）。
 */
export function CheckinLogDrawer({
  credentialId,
  events,
  onClose,
}: {
  credentialId: string;
  events: CheckinEvent[];
  onClose: () => void;
}) {
  return (
    <Card
      className="mt-4 gap-0 bg-muted/30 p-3 ring-border/60"
      data-testid={`checkin-drawer-${credentialId}`}
    >
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-medium">
          签到记录
          <span className="ml-2 text-xs text-muted-foreground">
            本地落库的签到审计日志（定时任务与手动签到都记），保留 90 天
          </span>
        </div>
        <Button size="sm" variant="ghost" onClick={onClose}
                data-testid="checkin-drawer-close">
          关闭
        </Button>
      </div>
      {events.length === 0 ? (
        <Empty data-testid="checkin-drawer-empty">
          还没有记录。签到（定时或手动）后这里会留下时间、结果与领取积分。
        </Empty>
      ) : (
        <Table data-testid="checkin-drawer-table" cards>
          <TableHeader>
            <TableRow>
              <TableHead>时间</TableHead>
              <TableHead>结果</TableHead>
              <TableHead>领取</TableHead>
              <TableHead>上游码</TableHead>
              <TableHead>触发</TableHead>
              <TableHead>说明</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {events.map((event) => (
              <TableRow key={event.id} data-testid={`checkin-event-${event.id}`}>
                <TableCell label="时间" className="text-xs">
                  {formatTime(event.ts)}
                </TableCell>
                <TableCell label="结果"
                            className={`text-xs ${event.ok ? "text-ok" : "text-warn"}`}>
                  {event.ok ? (event.already ? "今日已签" : "签到成功") : "签到失败"}
                </TableCell>
                <TableCell label="领取" className="text-xs tabular-nums">
                  {event.credit === null ? "—" : `+${formatNumber(event.credit)}`}
                </TableCell>
                <TableCell label="上游码" className="text-xs tabular-nums text-muted-foreground">
                  {event.code === null ? "—" : event.code}
                </TableCell>
                <TableCell label="触发" className="text-xs text-muted-foreground">
                  {event.trigger === "manual" ? "手动" : "定时"}
                </TableCell>
                <TableCell label="说明" className="text-xs text-muted-foreground">
                  {event.message || "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Card>
  );
}
