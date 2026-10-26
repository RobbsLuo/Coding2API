import { Check, ChevronDown, ChevronUp, ChevronsUpDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { TableHead } from "@/components/ui/table";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * 合并列的可排序表头：一个列头承载多个子列（如 Token = 输入/输出/命中/命中率）。
 *
 * 与 `SortableHead` 的区别：SortableHead 是「一列一个排序键」，这里是「一列多个
 * 排序键」——点列头弹菜单选子列，选中即按该子列排序，合并列不丢排序能力。
 * 未选中任何子列时显示中性双箭头；选中时列名后追加子列名与方向箭头。
 *
 * 排序仍由后端完成（`sort` / `order`），这里只负责「选哪个键 + 显示当前态」。
 */
export function SortMenuHead({
  label,
  unit,
  options,
  activeKey,
  direction,
  onToggle,
  align = "right",
  testId,
}: {
  label: string;
  /**
   * 单位（如 `tok/s`）。以更小字号缀在列名后，而不是拼进 `label`：
   * 「速度 tok/s」会把列头撑到 100px 以上，10 列的表里正好挤掉最后一列。
   */
  unit?: string;
  /** 子列：[{key: 后端排序键, label: 展示名}]，数组顺序即菜单顺序。 */
  options: { key: string; label: string }[];
  /** 当前排序键；不在 options 里时本列不显示激活态（别的列正在排序）。 */
  activeKey: string;
  direction: "asc" | "desc";
  onToggle: (columnKey: string) => void;
  align?: "left" | "right";
  testId?: string;
}) {
  const activeOption = options.find((option) => option.key === activeKey);
  const active = activeOption !== undefined;
  const Icon = !active ? ChevronsUpDown : direction === "asc" ? ChevronUp : ChevronDown;
  return (
    <TableHead
      aria-sort={active ? (direction === "asc" ? "ascending" : "descending") : "none"}
      className={cn(align === "right" && "text-right")}
    >
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            data-testid={testId}
            className={cn(
              "inline-flex items-center gap-1 rounded-sm transition-colors hover:text-foreground",
              "focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
              align === "right" && "flex-row-reverse",
              active && "text-foreground",
            )}
          >
            {label}
            {unit && (
              <span className="text-[10px] font-normal text-muted-foreground/80">{unit}</span>
            )}
            {activeOption && (
              <span className="font-normal text-muted-foreground">· {activeOption.label}</span>
            )}
            <Icon
              className={cn("size-3.5 shrink-0", active ? "opacity-100" : "opacity-40")}
              aria-hidden
            />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align={align === "right" ? "end" : "start"}>
          {options.map((option) => (
            <DropdownMenuItem
              key={option.key}
              onSelect={() => onToggle(option.key)}
              data-testid={testId ? `${testId}-${option.key}` : undefined}
            >
              {option.label}
              {option.key === activeKey && <Check className="ml-auto size-3.5" />}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </TableHead>
  );
}
