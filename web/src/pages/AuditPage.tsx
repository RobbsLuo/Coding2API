import { useState } from "react";
import { ScrollText } from "lucide-react";
import { formatTime } from "../api/display";
import { useAudit } from "../api/hooks";
import { useSessionContext } from "../Layout";
import { useSort } from "../hooks/useSort";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SortableHead } from "../components/SortableHead";
import {
  Badge,
  Button,
  EmptyState,
  Field,
  Input,
  Notice,
  Panel,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";

const PAGE_SIZE = 50;

export function AuditPage() {
  const session = useSessionContext();
  const { sort, order, toggle } = useSort("ts", "desc", { ts: "desc" });
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [offset, setOffset] = useState(0);

  // actor 用输入框，提交时才进查询（避免每敲一个字都打一次接口）
  const [actorDraft, setActorDraft] = useState("");
  const { data, isLoading } = useAudit(session.username, {
    actor: actor || undefined,
    action: action || undefined,
    limit: PAGE_SIZE,
    offset,
    sort,
    order,
  });

  const events = data?.events ?? [];
  const labels = data?.labels ?? {};
  const actions = data?.actions ?? [];

  const reset = (next: () => void) => {
    setOffset(0);
    next();
  };

  // 换排序 = 换数据集：回到第一页，避免停在越界页码上（空白）
  const toggleSort = (columnKey: string) => {
    toggle(columnKey);
    setOffset(0);
  };

  return (
    <div className="space-y-6" data-testid="audit-page">
      <PageHeader
        eyebrow="运维"
        title="审计日志"
        description="登录、账号变动与凭证写操作留痕。失败登录同样记录——「谁在什么时候试了哪个账号」正是审计的价值。日志绝不包含密码或令牌。"
        icon={<ScrollText className="size-5" />}
      />

      <Panel title="筛选">
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            reset(() => setActor(actorDraft.trim()));
          }}
        >
          <div className="w-56">
            <Field label="操作者">
              <Input
                value={actorDraft}
                data-testid="audit-actor"
                placeholder="例如 root"
                onChange={(event) => setActorDraft(event.target.value)}
              />
            </Field>
          </div>
          <div className="w-56">
            <Field label="动作">
              <Select
                value={action}
                data-testid="audit-action"
                onChange={(event) => reset(() => setAction(event.target.value))}
              >
                <option value="">全部动作</option>
                {actions.map((item) => (
                  <option key={item} value={item}>
                    {labels[item] ?? item}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Button type="submit" variant="default">
            应用筛选
          </Button>
          <Button
            type="button"
            variant="ghost"
            data-testid="audit-clear"
            onClick={() => {
              setActorDraft("");
              reset(() => {
                setActor("");
                setAction("");
              });
            }}
          >
            清空
          </Button>
        </form>
      </Panel>

      <Panel title={`记录${data ? `（本页 ${events.length} 条）` : ""}`} aria-label="审计记录">
        {isLoading ? (
          <PageSkeleton variant="table" rows={6} />
        ) : events.length === 0 ? (
          <EmptyState
            icon={<ScrollText className="size-5" />}
            title="没有符合条件的记录"
            description="调整上方筛选条件后再试，或清空筛选查看全部。"
            data-testid="no-audit"
          />
        ) : (
          <>
            <Table data-testid="audit-table">
              <TableHeader>
                <TableRow>
                  <SortableHead label="时间" columnKey="ts" active={sort === "ts"}
                                direction={order} onToggle={toggleSort} testId="sort-ts" />
                  <SortableHead label="操作者" columnKey="actor" active={sort === "actor"}
                                direction={order} onToggle={toggleSort} testId="sort-actor" />
                  <SortableHead label="动作" columnKey="action" active={sort === "action"}
                                direction={order} onToggle={toggleSort} testId="sort-action" />
                  <SortableHead label="对象" columnKey="target" active={sort === "target"}
                                direction={order} onToggle={toggleSort} testId="sort-target" />
                  <TableHead>详情</TableHead>
                  <SortableHead label="来源 IP" columnKey="ip" active={sort === "ip"}
                                direction={order} onToggle={toggleSort} testId="sort-ip" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.map((event) => (
                  <TableRow key={event.id} data-testid={`audit-row-${event.id}`}>
                    <TableCell className="whitespace-nowrap text-xs">
                      {formatTime(event.ts)}
                    </TableCell>
                    <TableCell className="text-xs">{event.actor}</TableCell>
                    <TableCell>
                      {/* 失败动作对读屏不可见（颜色语义）：加「失败」文案 */}
                      <Badge tone={event.ok ? "muted" : "danger"}>
                        {labels[event.action] ?? event.action}
                        {!event.ok && "（失败）"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs">{event.target ?? "—"}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {event.detail || "—"}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{event.ip ?? "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <div className="mt-4 flex items-center justify-between text-xs text-muted-foreground">
              <span>
                第 {Math.floor(offset / PAGE_SIZE) + 1} 页
              </span>
              <span className="inline-flex gap-2">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={offset === 0}
                  data-testid="audit-prev"
                  onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
                >
                  上一页
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={events.length < PAGE_SIZE}
                  data-testid="audit-next"
                  onClick={() => setOffset((value) => value + PAGE_SIZE)}
                >
                  下一页
                </Button>
              </span>
            </div>
          </>
        )}
      </Panel>

      <Notice tone="muted">
        日志保留期与请求明细一致（默认 90 天），由后台清理任务统一裁剪。
      </Notice>
    </div>
  );
}
