import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * 统一页头：可选 eyebrow（所属分组）+ 标题 + 一句话说明 + 右侧操作槽。
 * 所有管理页共用，保持层级一致。
 */
export function PageHeader({
  title,
  description,
  icon,
  eyebrow,
  actions,
  className,
}: {
  title: string;
  description?: ReactNode;
  icon?: ReactNode;
  /** 标题上方的小标签：说明当前页所属分组（如「运维」）。 */
  eyebrow?: string;
  /** 右侧操作槽：主操作按钮等。窄屏自动换行到标题下方。 */
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap items-start justify-between gap-3", className)}>
      <div className="min-w-0 flex-1 space-y-1">
        {eyebrow && (
          <div className="text-[11px] font-semibold tracking-widest text-primary uppercase">
            {eyebrow}
          </div>
        )}
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          {icon && (
            <span className="grid size-8 place-items-center rounded-lg bg-primary/10 text-primary">
              {icon}
            </span>
          )}
          {title}
        </h1>
        {description && (
          <p className="max-w-4xl text-sm leading-relaxed text-pretty text-muted-foreground">{description}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}
