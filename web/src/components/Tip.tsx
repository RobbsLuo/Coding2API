import type { ReactNode } from "react";
import { CircleHelp } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

/** 就地悬浮说明：把简短语义提示放到它对应的元素上，hover 显示。 */
export function Tip({ content, children }: { content: ReactNode; children: ReactNode }) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent sideOffset={4} className="max-w-xs text-xs">
          {content}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

/**
 * 长文本就地悬浮：触发元素本身按容器宽度截断，hover 显示完整内容。
 *
 * 与 Tip 的区别：Tip 的 content 是短语义提示（固定 max-w-xs），而这里要展示
 * 可能是几行的完整文本（如成长中心的一行汇报），需要更宽的弹层与自动换行。
 */
export function LongTextTip({ content, children }: { content: ReactNode; children: ReactNode }) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent
          sideOffset={4}
          className="max-w-sm items-start whitespace-normal break-words text-left"
        >
          {content}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

/** 表头问号：跟在列名右侧，hover 显示该列语义。 */
export function ColumnHint({ text }: { text: ReactNode }) {
  return (
    <Tip content={text}>
      <span className="inline-flex cursor-help items-center">
        <CircleHelp className="size-3.5 text-muted-foreground" />
      </span>
    </Tip>
  );
}