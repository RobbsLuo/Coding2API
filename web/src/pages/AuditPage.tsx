import { useState } from "react";
import { ScrollText } from "lucide-react";
import { formatTime } from "../api/display";
import { useAudit } from "../api/hooks";
import { useSessionContext } from "../Layout";
import { useSort } from "../hooks/useSort";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { Pagination } from "../components/Pagination";
import { SortableHead } from "../components/SortableHead";
import { DEFAULT_PAGE_SIZE } from "../lib/constants";
import { errorText } from "../lib/errors";
import {
  Badge,
  Button,
  EmptyState,
  Field,
  Input,
  Notice,
  Panel,
  QueryErrorState,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";

export function AuditPage() {
  const session = useSessionContext();
  const { sort, order, toggle } = useSort("ts", "desc", { ts: "desc" });
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  // actor 用输入框，提交时才进查询（避免每敲一个字都打一次接口）
  const [actorDraft, setActorDraft] = useState("");
  const { data, isLoading, isError, error, refetch, isFetching } = useAudit(session.username, {
    actor: actor || undefined,
    action: action || undefined,
    limit: pageSize,
    offset: page * pageSize,
    sort,
    order,
  });

  const events = data?.events ?? [];
  const labels = data?.labels ?? {};
  const actions = data?.actions ?? [];
  const total = data?.total ?? 0;

  /** 换筛选 / 排序 = 换数据集：回到第一页，避免停在越界页码上（空白）。 */
  const reset = (next: () => void) => {
    setPage(0);
    next();
  };

  const toggleSort = (columnKey: string) => {
    toggle(columnKey);
    setPage(0);
  };

  const changePageSize = (size: number) => {
    setPageSize(size);
    setPage(0);
  };

  return (
    <div className="space-y-6" data-testid="audit-page">
      <PageHeader
        eyebrow="运维"
        title="审计日志"
        description="登录、账号变动与凭证写操作留痕；失败登录同样记录——「谁在何时试了哪个账号」正是审计价值。不含密码与令牌。"
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
            <Field label="操作者" htmlFor="audit-actor-input">
              <Input
                id="audit-actor-input"
                value={actorDraft}
                data-testid="audit-actor"
                placeholder="例如 root"
                onChange={(event) => setActorDraft(event.target.value)}
              />
            </Field>
          </div>
          <div className="w-56">
            <Field label="动作" htmlFor="audit-action-select">
              <Select
                id="audit-action-select"
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
        ) : isError ? (
          <QueryErrorState
            title="审计记录加载失败"
            description={errorText(error, "拿不到审计记录；缺失的证据不等于没有操作，请重试。")}
            onRetry={() => void refetch()}
            data-testid="audit-error-state"
          />
        ) : events.length === 0 ? (
          <EmptyState
            icon={<ScrollText className="size-5" />}
            title="没有符合条件的记录"
            description="调整或清空筛选查看全部。"
            data-testid="no-audit"
          />
        ) : (
          <>
            <Table data-testid="audit-table" cards>
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
                    <TableCell label="时间" className="whitespace-nowrap text-xs tabular-nums">
                      {formatTime(event.ts)}
                    </TableCell>
                    <TableCell label="操作者" className="text-xs">{event.actor}</TableCell>
                    <TableCell label="动作">
                      {/* 失败动作对读屏不可见（颜色语义）：加「失败」文案 */}
                      <Badge tone={event.ok ? "muted" : "danger"}>
                        {labels[event.action] ?? event.action}
                        {!event.ok && "（失败）"}
                      </Badge>
                    </TableCell>
                    <TableCell label="对象" className="text-xs">{event.target ?? "—"}</TableCell>
                    <TableCell label="详情" className="text-xs text-muted-foreground">
                      {event.detail || "—"}
                    </TableCell>
                    <TableCell label="来源 IP" className="font-mono text-xs">
                      {event.ip ?? "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Pagination
              testId="audit"
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
        日志保留期与请求明细一致（默认 90 天），由后台清理任务裁剪。
      </Notice>
    </div>
  );
}
