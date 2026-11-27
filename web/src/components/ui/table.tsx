import * as React from "react"
import { cn } from "cn"

/**
 * 卡片模式开关向下传递：窄屏把 table 的 display 改成 block 后，Chromium/WebKit
 * 会**丢掉隐式表格语义**（读屏不再报 row / columnheader）。所以卡片模式打开时
 * 给 table/thead/tbody/tr/th/td 挂上显式 ARIA role，语义与宽屏一致。
 *
 * 用 context 而不是给每个子组件加 prop：既有的 6 个页面只用 `<Table cards>`，
 * 子组件不该为了这个开关逐个改调用点。
 */
const TableCardsContext = React.createContext(false)

function Table({
  className,
  containerClassName,
  cards = false,
  ...props
}: React.ComponentProps<"table"> & {
  /** 滚动容器（table-container）附加样式：长列表用它设 max-height + 吸顶表头所需的高度约束。 */
  containerClassName?: string
  /**
   * 窄屏（<768px）转卡片布局。只在**呈现层**生效：DOM 仍是标准 table，
   * 表头变一排可换行的排序片、每个数据格用 `TableCell label` 在自己行首显示字段名。
   *
   * 之所以不做「按视口渲染两套 JSX」：表格被 6 个页面按 `xxx-table` / 行 testid
   * 断言，DOM 一变「同一行」的语义就没了，读屏也会拿到一份重复内容。
   * 具体样式见 `styles.css` 的「窄屏表 → 卡片」段。
   */
  cards?: boolean
}) {
  return (
    <div
      data-slot="table-container"
      className={cn("relative w-full overflow-x-auto", containerClassName)}
    >
      <TableCardsContext.Provider value={cards}>
        <table
          data-slot="table"
          data-table-cards={cards ? "" : undefined}
          role={cards ? "table" : undefined}
          className={cn("w-full caption-bottom text-sm tabular-nums", className)}
          {...props}
        />
      </TableCardsContext.Provider>
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  const cards = React.useContext(TableCardsContext)
  return (
    <thead
      data-slot="table-header"
      role={cards ? "rowgroup" : undefined}
      className={cn(
        "[&_tr]:border-b [&_tr]:border-border [&_tr]:bg-muted/40 [&_tr:hover]:bg-muted/40",
        className
      )}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  const cards = React.useContext(TableCardsContext)
  return (
    <tbody
      data-slot="table-body"
      role={cards ? "rowgroup" : undefined}
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "border-t bg-muted/50 font-medium [&>tr]:last:border-b-0",
        className
      )}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  const cards = React.useContext(TableCardsContext)
  return (
    <tr
      data-slot="table-row"
      role={cards ? "row" : undefined}
      className={cn(
        "border-b transition-colors hover:bg-muted/50 has-aria-expanded:bg-muted/50 data-[state=selected]:bg-muted",
        className
      )}
      {...props}
    />
  )
}

function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  const cards = React.useContext(TableCardsContext)
  return (
    <th
      data-slot="table-head"
      role={cards ? "columnheader" : undefined}
      className={cn(
        "h-10 px-2 text-left align-middle text-xs font-medium tracking-wide whitespace-nowrap text-muted-foreground [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

function TableCell({
  className,
  label,
  ...props
}: React.ComponentProps<"td"> & {
  /**
   * 窄屏卡片模式下的字段名（`Table cards` 打开时生效）。宽屏不渲染，
   * 用 CSS `::before` 从 `data-label` 取值——不额外产生 DOM 节点，
   * 宽屏与读屏拿到的结构与不开卡片模式时完全一致。
   */
  label?: string
}) {
  const cards = React.useContext(TableCardsContext)
  return (
    <td
      data-slot="table-cell"
      data-label={label}
      role={cards ? "cell" : undefined}
      className={cn(
        "px-2 py-2.5 align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-4 text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
}
