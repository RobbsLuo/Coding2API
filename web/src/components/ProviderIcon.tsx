import CodeBuddyColor from "@lobehub/icons/es/CodeBuddy/components/Color";
import CodeBuddyMono from "@lobehub/icons/es/CodeBuddy/components/Mono";
import HuaweiCloudColor from "@lobehub/icons/es/HuaweiCloud/components/Color";
import HuaweiCloudMono from "@lobehub/icons/es/HuaweiCloud/components/Mono";
import KiloCodeMono from "@lobehub/icons/es/KiloCode/components/Mono";
import OpenClawMono from "@lobehub/icons/es/OpenClaw/components/Mono";
import OpenCodeMono from "@lobehub/icons/es/OpenCode/components/Mono";
import QoderColor from "@lobehub/icons/es/Qoder/components/Color";
import QoderMono from "@lobehub/icons/es/Qoder/components/Mono";
import TraeColor from "@lobehub/icons/es/Trae/components/Color";
import TraeMono from "@lobehub/icons/es/Trae/components/Mono";
import type { ComponentType } from "react";

type BrandComp = ComponentType<{ size?: number | string; className?: string }>;

const BRAND: Record<string, { Main: BrandComp; Color: BrandComp }> = {
  codebuddy: { Main: CodeBuddyMono, Color: CodeBuddyColor },
  // 国际版与国内站同一品牌、同一 logo，用渠道名区分而非图标
  codebuddy_intl: { Main: CodeBuddyMono, Color: CodeBuddyColor },
  trae: { Main: TraeMono, Color: TraeColor },
  // Zen 无独立彩色版：OpenCode 品牌 Mono 同时充当两态
  zen: { Main: OpenCodeMono, Color: OpenCodeMono },
  // Kilo 同理，@lobehub/icons 只提供 Mono（无 Color 变体）
  kilo: { Main: KiloCodeMono, Color: KiloCodeMono },
  qoder: { Main: QoderMono, Color: QoderColor },
  // CodeArts 无独立图标，用母公司 HuaweiCloud 品牌图标
  codearts: { Main: HuaweiCloudMono, Color: HuaweiCloudColor },
  // officeAce 同为华为云产品（AgentArts 办公智能体），沿用母公司图标
  officeace: { Main: HuaweiCloudMono, Color: HuaweiCloudColor },
  // LobsterAI（有道龙虾）无独立品牌图标，@lobehub/icons 也没有收录。
  // 沿用 OpenClaw（同为「龙虾」系开源桌面 Agent）的图标，靠渠道名区分。
  lobsterai: { Main: OpenClawMono, Color: OpenClawMono },
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
