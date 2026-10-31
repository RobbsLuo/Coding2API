import type { ReactNode } from "react";
import { ChevronDown, Inbox, TriangleAlert } from "lucide-react";
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
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";

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
        // flex-wrap + basis：窄屏下标题与操作区各占一行，而不是把标题挤成
        // 「指标趋 / 势」两行（CardHeader 的 grid 在极窄容器里会压标题列）。
        <CardHeader className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-border/70 pb-3">
          {title && (
            <h2 className="text-sm font-semibold tracking-tight text-foreground whitespace-nowrap">
              {title}
            </h2>
          )}
          {action && <CardAction className="ml-auto self-center">{action}</CardAction>}
        </CardHeader>
      )}
      <CardContent>{children}</CardContent>
    </Card>
  );
}

// ================================================================ Badge
// 语义色调(ok/warn/danger/muted/accent)：柔和底色 + 强调文字，不用重描边。
export type Tone = "ok" | "warn" | "danger" | "muted" | "accent";

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

/**
 * 导航角标：侧栏（及日后页面内）的「待办计数」标记。
 *
 * 与 `Badge` 的分工：`Badge` 是静态状态标签，`NavBadge` 承载**会变的计数**，
 * 因此数字用 `tabular-nums`（轮询刷新时宽度不抖）、超上限收敛成 `99+`、
 * 值为 0 / 空时不渲染（不占位）。
 *
 * 无障碍：可见数字 `aria-hidden`，另配 `sr-only` 全语义文案——否则读屏会把
 * 导航项名与裸数字读成两段无关联的话（「运维告警 3」）。
 */
export function NavBadge({
  value,
  tone = "muted",
  label,
}: {
  /** 计数或文字形态；数字 > 99 显示 "99+"；0 / 空串 / undefined 不渲染 */
  value?: number | string;
  tone?: Tone;
  /** 无障碍全语义，如「2 条告警尚未恢复」 */
  label: string;
}) {
  const text = typeof value === "number" ? (value > 99 ? "99+" : String(value)) : value;
  if (!text || text === "0") return null;
  return (
    <span
      className={cn(
        "ml-auto inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-full px-1.5 text-[11px] font-medium tabular-nums",
        TONE_CLASS[tone],
      )}
    >
      <span aria-hidden>{text}</span>
      <span className="sr-only">{label}</span>
    </span>
  );
}

// ================================================================ Field (Label)

export function Field({
  label,
  hint,
  children,
  htmlFor,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  /**
   * 关联控件的 id。传了才把 label 与控件绑上（读屏报字段名、点 label 聚焦控件）。
   *
   * 不默认用 useId 自动生成：控件如果是 page 传进来的（多数场景），
   * 生成的 id 到不了它身上，绑了反而给一个错的 aria 关系。
   */
  htmlFor?: string;
}) {
  return (
    <div className="space-y-1.5">
      <Label className="text-xs font-medium text-muted-foreground" htmlFor={htmlFor}>
        {label}
      </Label>
      {children}
      {hint && (
        // hint 与 label 的关联靠调用方给控件 aria-describedby（hint 没有 id 可指）
        <p className="text-xs leading-relaxed text-muted-foreground">{hint}</p>
      )}
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
      // max-w-full + flex-wrap：标签多于一屏时换行，而不是把整个页面撑宽
      // （实测「图表指标」8 个标签 601px，在 420px 视口下会把文档顶到 613px，
      // 全页横向滚动）。不用横向滚动：触屏上滚动条不可见，用户不知道右边还有
      // 选项。按钮加 shrink-0，否则 flex 会把文字挤成换行。
      className="inline-flex max-w-full flex-wrap items-center gap-0.5 rounded-lg border border-border/70 bg-muted/60 p-0.5"
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
            // whitespace-nowrap + shrink-0：tab 行放不下时横向滚动（外层 overflow-x-auto），
            // 而不是把标签挤成多行
            "shrink-0 rounded-md font-medium whitespace-nowrap transition-colors",
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
  // 左侧语义色条（MASTER §5「Notice」行）：整圈边框看不出语气，色条一眼可辨。
  // 用 border-l-4 而不是伪元素——不引入额外 DOM 层级。
  const cls: Record<Tone, string> = {
    ok: "border-ok/30 border-l-4 border-l-ok bg-ok/10 text-ok-ink",
    warn: "border-warn/40 border-l-4 border-l-warn bg-warn/12 text-warn-ink",
    danger:
      "border-destructive/30 border-l-4 border-l-destructive bg-destructive/10 text-danger-ink",
    muted: "border-border border-l-4 border-l-muted-foreground/40 bg-muted/50 text-muted-foreground",
    accent: "border-primary/35 border-l-4 border-l-primary bg-primary/10 text-primary-ink",
  };
  return (
    <ShadAlert
      variant={tone === "danger" ? "destructive" : "default"}
      // 只有 danger 是「错误」，用 role=alert 强播报；其余是信息/状态提示，
      // 用 role=status（MASTER §5「Notice」行；role=alert 会把中性信息当告警
      // 念给读屏用户）。role 由 tone 决定，不能在底层组件里写死。
      role={tone === "danger" ? "alert" : "status"}
      className={cn("items-center", cls[tone], className)}
    >
      {children}
    </ShadAlert>
  );
}

// ================================================================ Footnote
/**
 * 表格/图表下方的口径说明。默认收起成一行「口径说明」，点击展开全文。
 *
 * 统计页每张表下面都挂着一大段口径解释（均值与分位口径差异、速度分子分母、
 * 失败数按尝试计……），三四段加起来近 300px，把真正的数据推到首屏之外。
 * 口径要保留（不写会被误读），但不该常驻占据版面：默认收起，需要时再点开。
 */
export function Footnote({
  summary = "口径说明",
  children,
  className,
  testId,
}: {
  summary?: string;
  children: ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <Collapsible className={cn("mt-2", className)} data-testid={testId}>
      <CollapsibleTrigger
        className="inline-flex cursor-pointer select-none items-center gap-1 rounded text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <ChevronDown className="size-3.5" />
        {summary}
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-1.5 space-y-1 text-xs leading-relaxed text-muted-foreground">
        {children}
      </CollapsibleContent>
    </Collapsible>
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

/**
 * 取数失败态：与 `EmptyState` 视觉与文案都区分开。
 *
 * 页面把「请求失败」渲染成「还没有数据」会把故障讲成空数据（审计页尤其危险：
 * 缺证据和没有记录看起来一样），所以失败必须有自己的图标、文案与重试入口。
 */
export function QueryErrorState({
  title = "数据加载失败",
  description,
  onRetry,
  className,
  ...rest
}: {
  title?: string;
  description?: string;
  onRetry?: () => void;
  className?: string;
} & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <EmptyState
      {...rest}
      role="alert"
      className={cn("text-destructive", className)}
      icon={<TriangleAlert className="size-5" />}
      title={title}
      description={
        description ?? "请稍后重试；若持续失败，到「服务端日志与告警」查看后端原因。"
      }
      action={
        onRetry ? (
          <Button size="sm" onClick={onRetry}>
            重试
          </Button>
        ) : undefined
      }
    />
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
