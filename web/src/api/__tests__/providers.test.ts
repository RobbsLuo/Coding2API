import { describe, expect, it } from "vitest";
import {
  FALLBACK_CHART_COLOR,
  PROVIDER_ABBR,
  PROVIDER_CHART_COLOR,
  PROVIDER_LABEL,
  providerAbbr,
  providerChartColor,
  providerLabel,
} from "../providers";

describe("渠道展示元数据", () => {
  it("已知渠道返回中文名与缩写", () => {
    expect(providerLabel("codebuddy")).toBe("CodeBuddy");
    expect(providerLabel("trae")).toBe("TRAE");
    expect(providerLabel("zen")).toBe("OpenCode Zen");
    expect(providerAbbr("zen")).toBe("OC");
  });

  it("未知渠道回落为原始 id，颜色用兜底色", () => {
    expect(providerLabel("future")).toBe("future");
    expect(providerAbbr("future")).toBe("future");
    expect(providerChartColor("future")).toBe(FALLBACK_CHART_COLOR);
  });

  it("每个已知渠道都有标签、缩写与专属配色（三者对齐）", () => {
    const providers = Object.keys(PROVIDER_LABEL);
    expect(providers.sort()).toEqual(["codebuddy", "trae", "zen"]);
    for (const provider of providers) {
      expect(PROVIDER_ABBR[provider as keyof typeof PROVIDER_ABBR]).toBeTruthy();
      expect(
        PROVIDER_CHART_COLOR[provider as keyof typeof PROVIDER_CHART_COLOR],
      ).toMatch(/^var\(--chart-\d\)$/);
    }
  });
});
