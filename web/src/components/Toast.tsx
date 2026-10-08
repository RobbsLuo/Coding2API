import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * 浮层轻提示（toast）：操作结果不再以插入式横幅撑开页面，改为顶部居中浮动、
 * 数秒后自动消失、可手动关闭。适用于探测/签到这类高频瞬时反馈。
 *
 * 与模态对话框的区别：不阻断操作、不夺焦点、无需确认，因此适合「连续点几次
 * 探测」的场景。成长中心这种带步骤清单的结构化结果也走这里，只是给更长的
 * 停留时间，内容由调用方以 ReactNode 提供。
 */

export type ToastTone = "ok" | "danger" | "warn" | "muted";

export interface ToastItem {
  id: number;
  tone: ToastTone;
  /** 承载该提示的 DOM 节点的 testid（用于测试与定位）。 */
  testId?: string;
  content: ReactNode;
  /** 自动关闭时长（毫秒）；<= 0 表示不自动关闭（只能手动关）。 */
  duration?: number;
}

/** 默认停留 8s：够看清一行数字，又不会在连续操作时堆一屏。 */
const DEFAULT_DURATION = 8000;

export function useToasts() {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(0);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((item) => item.id !== id));
  }, []);

  const push = useCallback((toast: Omit<ToastItem, "id">) => {
    const id = nextId.current++;
    setToasts((current) => {
      // 同类提示（同 testId）只保留最新一条：连续探测不该把旧结果堆在屏幕上。
      const kept = toast.testId
        ? current.filter((item) => item.testId !== toast.testId)
        : current;
      return [...kept, { id, ...toast }];
    });
    return id;
  }, []);

  return { toasts, push, dismiss };
}

/** 色调 → 语气底 + 边框 + 图标色，沿用 Badge/Notice 的 tint 体系。浮层悬在
 * 页面内容之上，底色必须不透明，故用 color-mix 把语气色混进 `--card`（而不是
 * `bg-ok/10` 那种半透明底，否则后面的文字会透出来）。`muted` 保持中性卡片底。 */
const TONE: Record<ToastTone, { card: string; icon: string }> = {
  ok: {
    card: "border-[color-mix(in_oklab,var(--ok)_35%,var(--card))] bg-[color-mix(in_oklab,var(--ok)_10%,var(--card))]",
    icon: "text-ok-ink",
  },
  danger: {
    card: "border-[color-mix(in_oklab,var(--destructive)_35%,var(--card))] bg-[color-mix(in_oklab,var(--destructive)_10%,var(--card))]",
    icon: "text-danger-ink",
  },
  warn: {
    card: "border-[color-mix(in_oklab,var(--warn)_40%,var(--card))] bg-[color-mix(in_oklab,var(--warn)_12%,var(--card))]",
    icon: "text-warn-ink",
  },
  muted: { card: "border-border bg-card", icon: "text-muted-foreground" },
};

const ICON: Record<ToastTone, typeof Info> = {
  ok: CircleCheck,
  danger: CircleAlert,
  warn: TriangleAlert,
  muted: Info,
};

/** 顶部居中浮层容器：移动端与桌面端一致，贴顶居中、逐条向下堆叠。
 * 顶栏 sticky 高 h-14（56px），这里留 pt-16 让首条落在顶栏下方，
 * 不遮挡品牌区与右侧账号/主题按钮。 */
export function ToastViewport({
  toasts,
  onDismiss,
}: {
  toasts: ToastItem[];
  onDismiss: (id: number) => void;
}) {
  return (
    <div
      data-testid="toast-viewport"
      className="pointer-events-none fixed inset-x-0 top-0 z-[60] flex flex-col items-center gap-2 px-4 pt-16"
    >
      {toasts.map((toast) => (
        <ToastCard key={toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function ToastCard({
  toast,
  onDismiss,
}: {
  toast: ToastItem;
  onDismiss: (id: number) => void;
}) {
  const duration = toast.duration ?? DEFAULT_DURATION;
  const Icon = ICON[toast.tone];

  useEffect(() => {
    if (duration <= 0) return;
    const timer = window.setTimeout(() => onDismiss(toast.id), duration);
    return () => window.clearTimeout(timer);
  }, [duration, onDismiss, toast.id]);

  return (
    <div
      data-testid={toast.testId}
      // 失败提示是「需要立刻被感知」的信息，用 alert 强播报；其余用 status 礼貌播报。
      role={toast.tone === "danger" ? "alert" : "status"}
      className={cn(
        "pointer-events-auto flex w-full max-w-md items-start gap-2.5 rounded-xl border p-3 text-sm shadow-xl ring-1 ring-foreground/5",
        "animate-in fade-in-0 slide-in-from-top-4",
        TONE[toast.tone].card,
      )}
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", TONE[toast.tone].icon)} aria-hidden />
      <div className="min-w-0 flex-1 break-words">{toast.content}</div>
      <button
        type="button"
        aria-label="关闭提示"
        onClick={() => onDismiss(toast.id)}
        className="-mt-0.5 -mr-0.5 shrink-0 rounded-md p-1 text-muted-foreground transition-colors outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50"
      >
        <X className="size-3.5" />
      </button>
    </div>
  );
}