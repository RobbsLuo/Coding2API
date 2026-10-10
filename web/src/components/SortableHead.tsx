import { ChevronDown, ChevronUp, ChevronsUpDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { TableHead } from "@/components/ui/table";

/**
 * 可排序表头单元格：点击列头在升序 / 降序之间切换。
 *
 * 排序由后端完成（`sort` / `order` 查询参数），这里只负责「显示当前态 + 发出
 * 意图」。未激活的列显示中性双箭头；激活列按方向显示上 / 下箭头。`aria-sort`
 * 让读屏用户知道当前是哪个列、什么方向。
 *
 * 方向语义交给调用方的 `useSort`（每列可有不同的「首次点击方向」）——本组件只
 * 被动接收 `active` / `direction`。
 */
export function SortableHead({
  label,
  columnKey,
  active,
  direction,
  onToggle,
  align = "left",
  className,
  hint,
  testId,
}: {
  label: React.ReactNode;
  /** 该列对应的后端排序键（点击时回传）。 */
  columnKey: string;
  /** 当前排序列是否就是本列。 */
  active: boolean;
  /** 激活列的方向（未激活时忽略）。 */
  direction: "asc" | "desc";
  onToggle: (columnKey: string) => void;
  align?: "left" | "right";
  className?: string;
  /** 列名后的语义提示（如 ColumnHint）：置于按钮外，避免点击提示误触排序。 */
  hint?: React.ReactNode;
  testId?: string;
}) {
  const Icon = !active ? ChevronsUpDown : direction === "asc" ? ChevronUp : ChevronDown;
  return (
    <TableHead
      aria-sort={active ? (direction === "asc" ? "ascending" : "descending") : "none"}
      className={cn(align === "right" && "text-right", className)}
    >
      <span className="inline-flex items-center gap-1">
        <button
          type="button"
          data-testid={testId}
          onClick={() => onToggle(columnKey)}
          className={cn(
            "inline-flex items-center gap-1 rounded-sm transition-colors hover:text-foreground",
            "focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
            align === "right" && "flex-row-reverse",
            active && "text-foreground",
          )}
        >
          {label}
          <Icon
            className={cn("size-3.5 shrink-0", active ? "opacity-100" : "opacity-40")}
            aria-hidden
          />
        </button>
        {hint}
      </span>
    </TableHead>
  );
}
