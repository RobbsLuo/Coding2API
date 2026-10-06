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
  kilo: "Kilo Gateway",
  qoder: "Qoder",
  codearts: "CodeArts",
};

/** 模型下拉里的渠道缩写（如 `CB x0.29`）。 */
export const PROVIDER_ABBR: Record<Provider, string> = {
  codebuddy: "CB",
  trae: "TR",
  zen: "OC",
  kilo: "KL",
  qoder: "QD",
  codearts: "CA",
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
  kilo: "var(--chart-2)",
  qoder: "var(--chart-5)",
  codearts: "var(--chart-6)",
};

/** 未知渠道（老数据/未来渠道）的兜底色。 */
export const FALLBACK_CHART_COLOR = "var(--chart-5)";

/**
 * 模型列表 / 下拉的渠道展示顺序：CodeBuddy、TRAE 优先，其余渠道在后。
 *
 * 注意：这**不是**后端 `/v1/models` 的排序权重（`src/api/models.py` 的
 * `_PROVIDER_RANK` 把 zen/kilo 与 qoder/codearts 同归「其他 4」）。前端把
 * zen/kilo 提到 qoder/codearts 之前是有意的 UI 取舍（免费渠道更常被翻），
 * 因此 Playground 的分组顺序与接口返回的 data 顺序并不保证一致。
 */
export const PROVIDER_ORDER: Provider[] = ["codebuddy", "trae", "zen", "kilo", "qoder", "codearts"];

/** 渠道排序权重；未知渠道排在已知渠道之后。 */
export function providerRank(provider: string): number {
  const index = PROVIDER_ORDER.indexOf(provider as Provider);
  return index === -1 ? PROVIDER_ORDER.length : index;
}

export function providerLabel(provider: string): string {
  return PROVIDER_LABEL[provider as Provider] ?? provider;
}

export function providerAbbr(provider: string): string {
  return PROVIDER_ABBR[provider as Provider] ?? provider;
}

export function providerChartColor(provider: string): string {
  return PROVIDER_CHART_COLOR[provider as Provider] ?? FALLBACK_CHART_COLOR;
}
