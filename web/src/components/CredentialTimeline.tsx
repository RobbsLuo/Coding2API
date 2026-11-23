import { useState } from "react";
import {
  fetchCredentialTimeline,
  TIMELINE_EVENT_LABEL,
  TIMELINE_EVENT_TONE,
  type CredentialTimelineEvent,
} from "../api/credentialTimeline";
import { formatTime } from "../api/display";
import { Badge, Button, Empty, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../ui";

/** 一页条数：时间线是排障视图，20 条覆盖「一个小时内发生的全部迁移」。 */
const PAGE_SIZE = 20;

/**
 * 凭证可用性时间线（只读）。
 *
 * 回答「昨天 14:00 这个号为什么不可用」：冷却开始/结束、硬禁用、人工恢复、
 * 启停各落一行，比翻服务端日志更直接。数据**懒加载**——抽屉默认只显示入口
 * 按钮，点了才发请求（列表页一次展开几十条凭证时不该为每行预取一份历史）。
 *
 * 只展示后端给的脱敏枚举与短句：`reason` 是上游错误类型、`detail` 是人话，
 * 都不含凭证明文。不要在组件里自己拼上游响应正文。
 */
export function CredentialTimeline({ credentialId }: { credentialId: string }) {
  const [events, setEvents] = useState<CredentialTimelineEvent[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async (offset: number) => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchCredentialTimeline(credentialId, {
        limit: PAGE_SIZE,
        offset,
      });
      setEvents((previous) => (offset > 0 && previous !== null
        ? [...previous, ...response.events]
        : response.events));
      setHasMore(response.has_more);
    } catch (failure) {
      // 失败只影响本区块（不把整个抽屉打崩）：给一行可读原因，按钮可重试
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mt-3 border-t border-border/60 pt-3"
         data-testid={`credential-timeline-${credentialId}`}>
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-medium">
          可用性时间线
          <span className="ml-2 text-xs text-muted-foreground">
            冷却开始/结束、硬禁用与人工恢复的逐次审计（本地落库，保留 30 天）
          </span>
        </div>
        {events === null && (
          <Button size="sm" variant="ghost" disabled={loading}
                  onClick={() => void load(0)} data-testid="timeline-load">
            {loading ? "加载中…" : "查看时间线"}
          </Button>
        )}
      </div>
      {error !== null && (
        <div className="text-xs text-warn" data-testid="timeline-error">
          时间线加载失败：{error}
        </div>
      )}
      {events !== null && (events.length === 0 ? (
        <Empty data-testid="timeline-empty">
          还没有记录。冷却、恢复或启停发生后，这里会留下时间与原因。
        </Empty>
      ) : (
        <>
          <Table data-testid="timeline-table" cards>
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>事件</TableHead>
                <TableHead>模型</TableHead>
                <TableHead>原因</TableHead>
                <TableHead>说明</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((event) => (
                <TableRow key={event.id} data-testid={`timeline-event-${event.id}`}>
                  <TableCell label="时间" className="text-xs whitespace-nowrap">
                    {formatTime(event.ts)}
                  </TableCell>
                  <TableCell label="事件" className="text-xs">
                    <Badge tone={TIMELINE_EVENT_TONE[event.event] ?? "muted"}>
                      {TIMELINE_EVENT_LABEL[event.event] ?? event.event}
                    </Badge>
                  </TableCell>
                  <TableCell label="模型" className="text-xs text-muted-foreground">
                    {event.model ?? "—"}
                  </TableCell>
                  <TableCell label="原因" className="text-xs text-muted-foreground">
                    {event.reason || "—"}
                  </TableCell>
                  <TableCell label="说明" className="text-xs text-muted-foreground">
                    {event.detail || "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {hasMore && (
            <div className="mt-2 text-center">
              <Button size="sm" variant="ghost" disabled={loading}
                      onClick={() => void load(events.length)}
                      data-testid="timeline-more">
                {loading ? "加载中…" : "加载更早的记录"}
              </Button>
            </div>
          )}
        </>
      ))}
    </div>
  );
}
