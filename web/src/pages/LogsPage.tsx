import { useEffect, useMemo, useRef, useState } from "react";
import { FileTerminal, Pause, Play, Trash2 } from "lucide-react";
import { ApiError } from "../api/client";
import {
  LOG_LEVELS,
  fetchLogs,
  subscribeLogs,
  type LogLevel,
  type LogLine,
} from "../api/logs";
import { errorText } from "../lib/errors";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { Badge, Button, Checkbox, EmptyState, Field, Input, Notice, Panel } from "../ui";
import type { Tone } from "../ui";

/** 首次加载 / 订阅回放的保留行数：与缓冲上限无关，页面只看最近这些。 */
const INITIAL_LINES = 500;

/** 行里只显示时分秒（毫秒保留三位）：跨天也够定位，又不会被日期挤掉正文。 */
export function formatLogTime(ts: number): string {
  if (!Number.isFinite(ts)) return "—";
  const date = new Date(ts * 1000);
  const pad = (value: number, width = 2) => String(value).padStart(width, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    + `.${pad(date.getMilliseconds(), 3)}`;
}

const LEVEL_TONE: Record<string, Tone> = {
  DEBUG: "muted",
  INFO: "muted",
  WARNING: "warn",
  ERROR: "danger",
  CRITICAL: "danger",
};

/** 按级别集合与关键字过滤（纯函数，页面与测试共用）。
 *
 * 关键字同时匹配 message / logger / request_id：排查一条请求跨渠道重试时，
 * 直接把响应头的 `X-Request-ID`（或页面上显示的 8 位前缀）贴进来，就能收起
 * 该请求的全部服务端日志——executor 每次换号、冷却、降级都带同一个上下文。
 */
export function filterLines(lines: LogLine[], levels: ReadonlySet<string>,
                             keyword: string): LogLine[] {
  const needle = keyword.trim().toLowerCase();
  const bare = needle.replace(/^#/, "");
  return lines.filter((line) => {
    if (!levels.has(line.level)) return false;
    if (!needle) return true;
    return line.message.toLowerCase().includes(needle)
      || line.logger.toLowerCase().includes(needle)
      || (line.request_id ?? "").toLowerCase().includes(bare);
  });
}

/**
 * 服务端日志页（admin only，只读）。
 *
 * 数据源是后端进程内环形缓冲（`log_buffer_lines`），先 `GET /api/logs` 拉
 * 最近若干行，再用 SSE 订阅增量。**只读**：本页没有任何写入 / 执行入口，
 * 筛选全部在客户端完成（不再打接口，也不改变后端缓冲）。
 *
 * 暂停 = 断开 SSE（不占后端订阅槽位、页面也不再增长）；恢复 = 重连并重新
 * 拉一次快照（暂停期间的行不会凭空出现，但环形缓冲里还在）。
 */
export function LogsPage() {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 后端是否开着日志页（null = 还没问过）。关闭时不建 SSE：否则浏览器会对着
  // 404 无限重连，页面还一直显示「实时」。
  const [available, setAvailable] = useState<boolean | null>(null);
  const [paused, setPaused] = useState(false);
  const [follow, setFollow] = useState(true);
  const [levels, setLevels] = useState<Set<LogLevel>>(new Set(LOG_LEVELS));
  const [keyword, setKeyword] = useState("");
  const [status, setStatus] = useState<"live" | "error" | "paused">("live");
  const [capacity, setCapacity] = useState<number | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // 合并新行：按 seq 去重（快照与实时推送会交叠），并保持 seq 升序。
  const merge = (incoming: LogLine[]) => {
    setLines((previous) => {
      const seen = new Set(previous.map((line) => line.seq));
      const fresh = incoming.filter((line) => !seen.has(line.seq));
      if (fresh.length === 0) return previous;
      return [...previous, ...fresh].sort((a, b) => a.seq - b.seq);
    });
  };

  useEffect(() => {
    if (paused) return;
    let cancelled = false;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchLogs(INITIAL_LINES, controller.signal)
      .then((response) => {
        if (cancelled) return;
        setAvailable(true);
        setCapacity(response.capacity);
        merge(response.lines);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        // 404 = 后端 `log_buffer_lines=0`：这是「页面关了」，不是故障
        if (caught instanceof ApiError && caught.status === 404) {
          setAvailable(false);
        } else {
          setError(errorText(caught, "读取日志失败，请稍后重试。"));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [paused]);

  useEffect(() => {
    if (paused || available !== true) {
      if (paused) setStatus("paused");
      return;
    }
    return subscribeLogs({
      limit: INITIAL_LINES,
      onLine: (line) => merge([line]),
      onStatus: setStatus,
    });
  }, [paused, available]);

  // 跟随最新：新行到达后滚到底（关闭时保持当前位置，方便回看）。
  useEffect(() => {
    if (!follow || paused) return;
    bottomRef.current?.scrollIntoView?.({ block: "end" });
  }, [lines, follow, paused]);

  const visible = useMemo(() => filterLines(lines, levels, keyword), [lines, levels, keyword]);

  const toggleLevel = (level: LogLevel) => {
    setLevels((previous) => {
      const next = new Set(previous);
      if (next.has(level)) next.delete(level);
      else next.add(level);
      return next;
    });
  };

  const pausedLabel = paused ? "已暂停" : status === "error" ? "实时连接已断开" : "实时";

  return (
    <div className="space-y-6" data-testid="logs-page">
      <PageHeader
        eyebrow="运维"
        title="服务端日志"
        description="本进程最近产生的日志（进程内环形缓冲，重启清空）。只读视图，不读文件、不 tail；每行发出前经脱敏白名单，含凭证形态的行整行丢弃。"
        icon={<FileTerminal className="size-5" />}
      />

      {available === false ? (
        <div data-testid="logs-disabled">
          <Notice tone="warn">
            日志页已关闭：热更项 <code>log_buffer_lines</code> 为 0。调到 1 以上即可开启
            （「任务与配置」→ 可观测性），无需重启。
          </Notice>
        </div>
      ) : (
        <Panel
          title={`日志${capacity !== null ? `（缓冲上限 ${capacity} 行）` : ""}`}
          action={
            <div className="flex flex-wrap items-center gap-2">
              <span data-testid="logs-status">
                <Badge tone={paused ? "muted" : status === "error" ? "danger" : "ok"}>
                  {pausedLabel}
                </Badge>
              </span>
              <Button
                size="sm"
                variant="default"
                data-testid="logs-pause"
                onClick={() => setPaused((value) => !value)}
              >
                {paused ? <Play className="mr-1 size-3.5" /> : <Pause className="mr-1 size-3.5" />}
                {paused ? "继续" : "暂停"}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                data-testid="logs-clear"
                onClick={() => setLines([])}
              >
                <Trash2 className="mr-1 size-3.5" />
                清空
              </Button>
            </div>
          }
        >
          <div className="mb-3 flex flex-wrap items-end gap-3">
            <div className="w-64">
              <Field label="关键字" htmlFor="logs-keyword">
                <Input
                  id="logs-keyword"
                  data-testid="logs-keyword"
                  value={keyword}
                  placeholder="按消息 / logger / request_id 过滤"
                  onChange={(event) => setKeyword(event.target.value)}
                />
              </Field>
            </div>
            <fieldset className="flex flex-wrap items-center gap-3">
              <legend className="sr-only">级别</legend>
              {LOG_LEVELS.map((level) => (
                <label
                  key={level}
                  className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground"
                >
                  <Checkbox
                    data-testid={`logs-level-${level}`}
                    checked={levels.has(level)}
                    onCheckedChange={() => toggleLevel(level)}
                  />
                  {level}
                </label>
              ))}
            </fieldset>
            <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
              <Checkbox
                data-testid="logs-follow"
                checked={follow}
                onCheckedChange={() => setFollow((value) => !value)}
              />
              跟随最新
            </label>
          </div>

          {error && (
            <div className="mb-3" data-testid="logs-error" role="alert">
              <Notice tone="danger">{error}</Notice>
            </div>
          )}

          {loading && lines.length === 0 ? (
            <PageSkeleton variant="table" rows={6} />
          ) : visible.length === 0 ? (
            <EmptyState
              icon={<FileTerminal className="size-5" />}
              data-testid="logs-empty"
              title={lines.length === 0 ? "还没有日志" : "没有符合筛选条件的行"}
              description={
                lines.length === 0
                  ? "服务产生新日志时会自动出现在这里（打开「跟随最新」保持滚动）。"
                  : "调整级别勾选或清空关键字查看全部。"
              }
            />
          ) : (
            <ol
              className="max-h-[60vh] overflow-y-auto rounded-lg border border-border bg-muted/30 p-2 font-mono text-xs"
              data-testid="logs-list"
              aria-label="日志行"
            >
              {visible.map((line) => (
                <li
                  key={line.seq}
                  data-testid="log-row"
                  className="flex items-start gap-2 rounded px-1.5 py-0.5 hover:bg-muted"
                >
                  <span className="shrink-0 text-muted-foreground tabular-nums">
                    {formatLogTime(line.ts)}
                  </span>
                  <span className="w-16 shrink-0">
                    <Badge tone={LEVEL_TONE[line.level] ?? "muted"}>{line.level}</Badge>
                  </span>
                  <span className="shrink-0 text-muted-foreground">{line.logger}</span>
                  <span className="min-w-0 flex-1 break-words whitespace-pre-wrap">
                    {line.message}
                  </span>
                  {line.request_id && (
                    <button
                      type="button"
                      className="shrink-0 rounded px-1 text-muted-foreground/80 underline-offset-2 hover:text-foreground hover:underline"
                      title={`只看这条请求的全部日志（request_id=${line.request_id}）`}
                      data-testid="log-request-id"
                      onClick={() => setKeyword(line.request_id ?? "")}
                    >
                      #{line.request_id.slice(0, 8)}
                    </button>
                  )}
                </li>
              ))}
            </ol>
          )}
          <div ref={bottomRef} />
        </Panel>
      )}

      <Notice tone="muted">
        只收本进程日志；缓冲随 <code>log_buffer_lines</code> 热更（调小立即裁掉最旧的行）。
        脱敏规则：含 token / secret / authorization / cookie / password / api_key /
        <code>sk-</code> 形态的行整行丢弃——宁可漏一条，也不把凭证送进浏览器。
      </Notice>
    </div>
  );
}
