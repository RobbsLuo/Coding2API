import { Skeleton } from "../ui";
import { cn } from "@/lib/utils";

/**
 * 页面加载骨架：替代纯文字「载入中…」，让布局在数据到达前就稳定，
 * 避免内容跳变（CLS）。`variant` 决定占位形态。
 *
 * 保留一条 sr-only 的「载入中…」：读屏可感知，且既有测试以该文本判断加载态。
 */
export function PageSkeleton({
  variant = "cards",
  rows = 6,
  className,
}: {
  variant?: "cards" | "table" | "chart";
  rows?: number;
  className?: string;
}) {
  return (
    <div className={cn("space-y-6", className)} data-testid="page-skeleton">
      <span className="sr-only">载入中…</span>

      {/* 页头占位 */}
      <div className="space-y-2">
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-4 w-full max-w-xl" />
      </div>

      {variant === "cards" && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-24 rounded-xl" />
          ))}
        </div>
      )}

      {variant === "chart" && <Skeleton className="h-72 rounded-xl" />}

      {/* 主体占位：表格行 / 卡片列表 */}
      <div className="rounded-xl bg-card p-4 ring-1 ring-border">
        <Skeleton className="mb-4 h-5 w-32" />
        <div className="space-y-2.5">
          {Array.from({ length: rows }).map((_, index) => (
            <Skeleton key={index} className="h-9 w-full" />
          ))}
        </div>
      </div>
    </div>
  );
}
