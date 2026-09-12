import { Cloud, Zap } from "lucide-react";
import { cn } from "@/lib/utils";

const ICON: Record<string, typeof Cloud> = { codebuddy: Cloud, trae: Zap };
// 颜色与用量统计页图例一致（CB 蓝 / TRAE 橙）
const COLOR: Record<string, string> = {
  codebuddy: "text-[var(--chart-1)]",
  trae: "text-[var(--chart-3)]",
};

/** 渠道前置 icon：CodeBuddy = Cloud（蓝），TRAE = Zap（橙）。未知渠道不渲染。 */
export function ProviderIcon({
  provider,
  className,
}: {
  provider: string;
  className?: string;
}) {
  const Icon = ICON[provider];
  if (!Icon) return null;
  return <Icon className={cn("size-3.5 shrink-0", COLOR[provider], className)} />;
}
