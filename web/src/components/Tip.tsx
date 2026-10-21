import type { ReactNode } from "react";
import { CircleHelp } from "lucide-react";
import { cn } from "cn";
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
 * 与 Tip 的区别不止宽度：这里的内容常是**多行结构化清单**（额度包 / 模型与倍率 /
 * 成长中心汇报），故走 `panel` 表面（`panel-raised` + 描边 + 阴影）而不是反色气泡
 * ——反色底放大段文字会糊成一块，且反色气泡带箭头，箭头会盖住描边留一道缺口。
 * 同时默认 `items-start` + 左对齐：多行内容居中会让每行起点都不同，读不成列。
 */
export function LongTextTip({ content, children, className }: {
  content: ReactNode;
  children: ReactNode;
  /** 追加到弹层容器：调用方可放宽宽度 / 禁止折行；沿用 cn 合并，后者覆盖默认值 */
  className?: string;
}) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent
          surface="panel"
          sideOffset={6}
          className={cn(
            "block max-w-sm items-start px-3 py-2 text-xs whitespace-normal break-words text-left",
            className,
          )}
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
