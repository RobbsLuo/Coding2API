import * as React from "react"
import { cn } from "cn"
import { Tooltip as TooltipPrimitive } from "radix-ui"

function TooltipProvider({
  delayDuration = 0,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return (
    <TooltipPrimitive.Provider
      data-slot="tooltip-provider"
      delayDuration={delayDuration}
      {...props}
    />
  )
}

function Tooltip({
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Root>) {
  return <TooltipPrimitive.Root data-slot="tooltip" {...props} />
}

function TooltipTrigger({
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Trigger>) {
  return <TooltipPrimitive.Trigger data-slot="tooltip-trigger" {...props} />
}

/**
 * 浮层表面：两种，按内容体量选。
 *
 * - `ink`（默认）：反色气泡（`bg-foreground`），适合一句话的短提示——短、强对比、
 *   一眼能读完，反色正是它该有的存在感。
 * - `panel`：卡片表面（`panel-raised` + 描边 + 阴影），适合**多行结构化内容**
 *   （清单 / 明细）。反色底放大段文字会糊成一块，而 `panel-raised` 正是设计系统里
 *   「浮层」那一档，和下拉 / 弹窗同一层语义（见 design-system MASTER §2.2）。
 *   该档不出箭头：箭头会盖住描边，留一道缺口。
 */
export type TooltipSurface = "ink" | "panel"

const SURFACE: Record<TooltipSurface, { content: string; arrow: string }> = {
  ink: { content: "bg-foreground text-background", arrow: "bg-foreground fill-foreground" },
  panel: {
    content: "bg-panel-raised text-foreground ring-1 ring-border shadow-lg",
    arrow: "",
  },
}

function TooltipContent({
  className,
  surface = "ink",
  sideOffset = 0,
  children,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content> & {
  /** 见 `TooltipSurface`：短提示用 ink，多行清单用 panel */
  surface?: TooltipSurface
}) {
  const style = SURFACE[surface]
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        data-surface={surface}
        sideOffset={sideOffset}
        className={cn(
          "z-50 inline-flex w-fit max-w-xs origin-(--radix-tooltip-content-transform-origin) items-center gap-1.5 rounded-md px-3 py-1.5 text-xs has-data-[slot=kbd]:pr-1.5 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 **:data-[slot=kbd]:relative **:data-[slot=kbd]:isolate **:data-[slot=kbd]:z-50 **:data-[slot=kbd]:rounded-sm data-[state=delayed-open]:animate-in data-[state=delayed-open]:fade-in-0 data-[state=delayed-open]:zoom-in-95 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95",
          style.content,
          className
        )}
        {...props}
      >
        {children}
        {surface === "ink" && (
          <TooltipPrimitive.Arrow
            data-slot="tooltip-arrow"
            className={cn(
              "z-50 size-2.5 translate-y-[calc(-50%_-_2px)] rotate-45 rounded-[2px]",
              style.arrow,
            )}
          />
        )}
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  )
}

export { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger }
