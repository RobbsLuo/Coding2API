import type { ReactNode } from "react";
import { ChevronDown, Inbox } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button as ShadButton } from "@/components/ui/button";
import { Badge as ShadBadge } from "@/components/ui/badge";
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { Alert as ShadAlert } from "@/components/ui/alert";

import {
  Label,
  Skeleton,
  Separator,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/controls";

// 控件 re-export shadcn 标准件（Label 同时本地用于 Field）
export { Input } from "@/components/ui/input";
export { Textarea } from "@/components/ui/textarea";
export { Checkbox } from "@/components/ui/checkbox";
export { Card } from "@/components/ui/card";
export {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from "@/components/ui/collapsible";
export {
  Label,
  Skeleton,
  Separator,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
};

// ================================================================ Button
// 旧变体映射到 shadcn：primary(实底)→default；default(描边)→outline；danger→destructive
type ButtonVariant = "default" | "primary" | "danger" | "ghost" | "link";
type ButtonSize = "sm" | "md" | "icon";

const VARIANT_MAP = {
  default: "outline",
  primary: "default",
  danger: "destructive",
  ghost: "ghost",
  link: "link",
} as const;

const SIZE_MAP = { sm: "sm", md: "default", icon: "icon-sm" } as const;

export function Button({
  children,
  variant = "default",
  size = "md",
  className,
  ...rest
}: {
  children: ReactNode;
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
} & Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "className">) {
  return (
    <ShadButton
      variant={VARIANT_MAP[variant] as "outline"}
      size={SIZE_MAP[size] as "default"}
      className={className}
      {...rest}
    >
      {children}
    </ShadButton>
  );
}

// ================================================================ Panel (Card)
// 卡片是页面里最主要的表面：默认 raised 观感（ring + 阴影），header 与内容分离。

export function Panel({
  title,
  action,
  children,
  className,
  ...rest
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
} & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <Card className={cn("rounded-xl shadow-sm", className)} {...rest}>
      {(title || action) && (
        <CardHeader className="border-b border-border/70 pb-3">
          {title && (
            <h2 className="text-sm font-semibold tracking-tight text-foreground">
              {title}
            </h2>
          )}
          <CardAction>{action}</CardAction>
        </CardHeader>
      )}
      <CardContent>{children}</CardContent>
    </Card>
  );
}

// ================================================================ Badge
// 语义色调(ok/warn/danger/muted/accent)：柔和底色 + 强调文字，不用重描边。
type Tone = "ok" | "warn" | "danger" | "muted" | "accent";

const TONE_CLASS: Record<Tone, string> = {
  ok: "bg-ok/12 text-ok-ink ring-1 ring-inset ring-ok/25",
  warn: "bg-warn/15 text-warn-ink ring-1 ring-inset ring-warn/30",
  danger: "bg-destructive/12 text-danger-ink ring-1 ring-inset ring-destructive/25",
  muted: "bg-secondary text-secondary-foreground",
  accent: "bg-primary/12 text-primary-ink ring-1 ring-inset ring-primary/25",
};

/** 色调 → 状态点颜色（Badge `dot` 形态用）。 */
const TONE_DOT: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-destructive",
  muted: "bg-muted-foreground/60",
  accent: "bg-primary",
};

export function Badge({
  tone = "muted",
  dot = false,
  children,
}: {
  tone?: Tone;
  /** 前置状态圆点：与文字并用（颜色不单独表意）。 */
  dot?: boolean;
  children: ReactNode;
}) {
  return (
    <ShadBadge variant="outline" className={cn("border-transparent", TONE_CLASS[tone])}>
      {dot && <span className={cn("size-1.5 rounded-full", TONE_DOT[tone])} aria-hidden />}
      {children}
    </ShadBadge>
  );
}

// ================================================================ Field (Label)

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <Label className="text-xs font-medium text-muted-foreground">{label}</Label>
      {children}
      {hint && <p className="text-xs leading-relaxed text-muted-foreground">{hint}</p>}
    </div>
  );
}

// ================================================================ 表单控件
// Input/Textarea/Label 直接来自 shadcn；Select 因页面用 optgroup（radix 不支持）保留原生，
// 但样式对齐 shadcn input + lucide 箭头。

export function Select({ className, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className={cn("relative", className)}>
      <select
        data-slot="select"
        className="h-8 w-full appearance-none rounded-lg border border-input bg-background px-2.5 pr-8 text-sm transition-colors outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30"
        {...props}
      />
      <ChevronDown className="pointer-events-none absolute top-1/2 right-2 size-4 -translate-y-1/2 text-muted-foreground" />
    </div>
  );
}

// ================================================================ Tabs (Segmented)
// 轻量分段切换（非 radix Tab）：一排互斥按钮，选中项浮起 + 主色指示。

export function Tabs({
  value,
  options,
  onChange,
  testId,
  compact = false,
}: {
  value: string;
  options: { value: string; label: string; icon?: ReactNode }[];
  onChange: (value: string) => void;
  testId?: string;
  /** 紧凑模式：更小的字号与内边距，给 tab 数量多的页面（如「任务与配置」）。 */
  compact?: boolean;
}) {
  return (
    <div
      role="tablist"
      data-testid={testId}
      className="inline-flex items-center gap-0.5 rounded-lg border border-border/70 bg-muted/60 p-0.5"
    >
      {options.map((option) => (
        <button
          key={option.value}
          role="tab"
          type="button"
          aria-selected={option.value === value}
          data-testid={testId ? `${testId}-${option.value}` : undefined}
          onClick={() => onChange(option.value)}
          className={cn(
            // whitespace-nowrap：tab 行放不下时横向滚动（外层 overflow-x-auto），
            // 而不是把标签挤成多行
            "rounded-md font-medium whitespace-nowrap transition-colors",
            compact ? "px-2 py-1 text-xs" : "px-3 py-1.5 text-sm",
            option.value === value
              ? "bg-background text-foreground shadow-sm ring-1 ring-border/60"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {option.icon && <span className="mr-1.5 inline-flex align-[-0.1em]">{option.icon}</span>}
          {option.label}
        </button>
      ))}
    </div>
  );
}

// ================================================================ Notice (Alert)

export function Notice({
  tone = "muted",
  className,
  children,
}: {
  tone?: Tone;
  className?: string;
  children: ReactNode;
}) {
  const cls: Record<Tone, string> = {
    ok: "border-ok/30 bg-ok/10 text-ok-ink",
    warn: "border-warn/40 bg-warn/12 text-warn-ink",
    danger: "border-destructive/30 bg-destructive/10 text-danger-ink",
    muted: "border-border bg-muted/50 text-muted-foreground",
    accent: "border-primary/35 bg-primary/10 text-primary-ink",
  };
  return (
    <ShadAlert
      variant={tone === "danger" ? "destructive" : "default"}
      className={cn("items-center", cls[tone], className)}
    >
      {children}
    </ShadAlert>
  );
}

// ================================================================ Empty / EmptyState

/** 兼容旧调用的单行空态（表格内、抽屉内等紧凑场景）。 */
export function Empty({
  children,
  ...rest
}: { children: ReactNode } & React.HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p
      {...rest}
      className="flex items-center justify-center gap-2 py-8 text-center text-sm text-muted-foreground"
    >
      <Inbox className="size-4 shrink-0" />
      {children}
    </p>
  );
}

/** 整块空态：图标 + 标题 + 描述 + 可选操作，用于页面/面板级「还没有内容」。 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
  ...rest
}: {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
} & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      {...rest}
      className={cn(
        "flex flex-col items-center justify-center gap-2 px-6 py-12 text-center",
        className,
      )}
    >
      <span className="grid size-11 place-items-center rounded-full bg-muted text-muted-foreground">
        {icon ?? <Inbox className="size-5" />}
      </span>
      <div className="text-sm font-medium text-foreground">{title}</div>
      {description && (
        <p className="max-w-md text-xs leading-relaxed text-muted-foreground">{description}</p>
      )}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}

// ================================================================ Metric (Card)

export function Metric({
  label,
  value,
  hint,
  tone,
  icon,
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  tone?: "ok" | "danger" | "warn";
  icon?: ReactNode;
  /** 覆写卡片外观：池概览把多块 Metric 并进同一容器时去掉各自的边框底色。 */
  className?: string;
}) {
  return (
    <Card className={cn("gap-0 px-4 py-3.5 shadow-sm", className)}>
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-medium text-muted-foreground">{label}</div>
        {icon && (
          <span className="grid size-7 place-items-center rounded-lg bg-muted text-muted-foreground">
            {icon}
          </span>
        )}
      </div>
      <div
        className={cn(
          "mt-2 text-2xl leading-none font-bold tracking-tight tabular-nums",
          tone === "ok" && "text-ok",
          tone === "danger" && "text-destructive",
          tone === "warn" && "text-warn",
        )}
      >
        {value}
      </div>
      {hint && (
        <div className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{hint}</div>
      )}
    </Card>
  );
}
