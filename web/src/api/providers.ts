import type { Provider } from "./types";

/**
 * 渠道展示元数据的唯一来源。
 *
 * 新增渠道时只改这里：各页面曾经各存一份 `PROVIDER_LABEL`，加第三个渠道
 * 时要改五六处、漏一处就是「某个页面显示原始 id」。缩写用于模型列表的
 * 倍率标记（`通道缩写 x倍率`），必须短到不挤爆下拉宽度。
 */
export const PROVIDER_LABEL: Record<Provider, string> = {
  codebuddy: "CodeBuddy",
  trae: "TRAE",
  zen: "OpenCode Zen",
};

/** 模型下拉里的渠道缩写（如 `CB x0.29`）。 */
export const PROVIDER_ABBR: Record<Provider, string> = {
  codebuddy: "CB",
  trae: "TR",
  zen: "OC",
};

/**
 * 趋势图曲线配色（CSS 变量名）。
 *
 * 顺序固定、每个渠道一个颜色：颜色随渠道稳定，用户不会因为某个渠道当天
 * 没有数据就把两种颜色读串。渠道数超过配色数时回落到 --chart-5 之前的循环。
 */
export const PROVIDER_CHART_COLOR: Record<Provider, string> = {
  codebuddy: "var(--chart-1)",
  trae: "var(--chart-3)",
  zen: "var(--chart-4)",
};

/** 未知渠道（老数据/未来渠道）的兜底色。 */
export const FALLBACK_CHART_COLOR = "var(--chart-5)";

export function providerLabel(provider: string): string {
  return PROVIDER_LABEL[provider as Provider] ?? provider;
}

export function providerAbbr(provider: string): string {
  return PROVIDER_ABBR[provider as Provider] ?? provider;
}

export function providerChartColor(provider: string): string {
  return PROVIDER_CHART_COLOR[provider as Provider] ?? FALLBACK_CHART_COLOR;
}
