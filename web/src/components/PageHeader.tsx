import type { ReactNode } from "react";

/** 统一页头：标题 + 一句话说明，所有管理页共用，保持层级一致。 */
export function PageHeader({
  title,
  description,
  icon,
}: {
  title: string;
  description?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="space-y-1">
      <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
        {icon && <span className="text-primary">{icon}</span>}
        {title}
      </h1>
      {description && <p className="text-sm leading-relaxed text-muted-foreground">{description}</p>}
    </div>
  );
}