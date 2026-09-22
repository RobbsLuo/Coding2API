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
  CardTitle,
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

export function Panel({
  title,
  action,
  children,
  className,
}: {
  title?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("rounded-xl", className)}>
      {(title || action) && (
        <CardHeader>
          {title && (
            <CardTitle className="text-sm font-semibold tracking-wide">{title}</CardTitle>
          )}
          <CardAction>{action}</CardAction>
        </CardHeader>
      )}
      <CardContent>{children}</CardContent>
    </Card>
  );
}

// ================================================================ Badge
// 语义色调(ok/warn/danger/muted/accent)在 shadcn Badge 基础上用颜色类扩展
type Tone = "ok" | "warn" | "danger" | "muted" | "accent";

const TONE_CLASS: Record<Tone, string> = {
  ok: "border-ok/30 bg-ok/10 text-ok",
  warn: "border-warn/40 bg-warn/15 text-warn",
  danger: "border-destructive/30 bg-destructive/10 text-destructive",
  muted: "border-transparent bg-secondary text-secondary-foreground",
  accent: "border-primary/30 bg-primary/10 text-primary",
};

export function Badge({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <ShadBadge variant="outline" className={cn(TONE_CLASS[tone])}>
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
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
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
// 轻量分段切换（非 radix Tab）：一排互斥按钮，选中项白底浮起。适用少量平级选项（如时间范围）。

export function Tabs({
  value,
  options,
  onChange,
  testId,
}: {
  value: string;
  options: { value: string; label: string; icon?: ReactNode }[];
  onChange: (value: string) => void;
  testId?: string;
}) {
  return (
    <div role="tablist" data-testid={testId} className="inline-flex items-center gap-0.5 rounded-lg bg-muted p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          role="tab"
          type="button"
          aria-selected={option.value === value}
          data-testid={testId ? `${testId}-${option.value}` : undefined}
          onClick={() => onChange(option.value)}
          className={cn(
            "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
            option.value === value
              ? "bg-background text-foreground shadow-sm"
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
    ok: "border-ok/30 bg-ok/10 text-ok",
    warn: "border-warn/40 bg-warn/15 text-warn",
    danger: "border-destructive/30 bg-destructive/10 text-destructive",
    muted: "border-border bg-muted text-muted-foreground",
    accent: "border-primary/35 bg-primary/10 text-primary",
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

// ================================================================ Empty

export function Empty({
  children,
  ...rest
}: { children: ReactNode } & React.HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p {...rest} className="flex items-center justify-center gap-2 py-8 text-center text-sm text-muted-foreground">
      <Inbox className="size-4 shrink-0" />
      {children}
    </p>
  );
}

// ================================================================ Metric (Card)

export function Metric({
  label,
  value,
  hint,
  tone,
  icon,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  tone?: "ok" | "danger" | "warn";
  icon?: ReactNode;
}) {
  return (
    <Card className="px-4 py-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs text-muted-foreground">{label}</div>
        {icon && <span className="text-muted-foreground/80">{icon}</span>}
      </div>
      <div
        className={cn(
          "mt-1 text-2xl font-bold tracking-tight tabular-nums",
          tone === "ok" && "text-ok",
          tone === "danger" && "text-destructive",
          tone === "warn" && "text-warn",
        )}
      >
        {value}
      </div>
      {hint && <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>}
    </Card>
  );
}