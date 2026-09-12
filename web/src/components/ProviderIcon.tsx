import CodeBuddyColor from "@lobehub/icons/es/CodeBuddy/components/Color";
import CodeBuddyMono from "@lobehub/icons/es/CodeBuddy/components/Mono";
import TraeColor from "@lobehub/icons/es/Trae/components/Color";
import TraeMono from "@lobehub/icons/es/Trae/components/Mono";
import type { ComponentType } from "react";

type BrandComp = ComponentType<{ size?: number | string; className?: string }>;

const BRAND: Record<string, { Main: BrandComp; Color: BrandComp }> = {
  codebuddy: { Main: CodeBuddyMono, Color: CodeBuddyColor },
  trae: { Main: TraeMono, Color: TraeColor },
};

/**
 * 渠道品牌 logo（@lobehub/icons）：默认品牌彩色版；未知渠道不渲染。
 * 深路径 import 绕开 Avatar 变体（其依赖 @lobehub/ui 的 emoji-mart JSON
 * 在 vitest/node 下无法加载）。
 */
export function ProviderIcon({
  provider,
  size = 14,
  colored = true,
  className,
}: {
  provider: string;
  size?: number;
  colored?: boolean;
  className?: string;
}) {
  const brand = BRAND[provider];
  if (!brand) return null;
  const Icon = colored ? brand.Color : brand.Main;
  return <Icon size={size} className={className} />;
}
