import { describe, expect, it } from "vitest";
import {
  FALLBACK_CHART_COLOR,
  PROVIDER_ABBR,
  PROVIDER_CHART_COLOR,
  PROVIDER_LABEL,
  PROVIDER_ORDER,
  providerAbbr,
  providerChartColor,
  providerLabel,
  providerRank,
} from "../providers";

describe("渠道展示元数据", () => {
  it("已知渠道返回中文名与缩写", () => {
    expect(providerLabel("codebuddy")).toBe("CodeBuddy");
    expect(providerLabel("trae")).toBe("TRAE");
    expect(providerLabel("zen")).toBe("OpenCode Zen");
    expect(providerAbbr("zen")).toBe("OC");
    expect(providerLabel("kilo")).toBe("Kilo Gateway");
    expect(providerAbbr("kilo")).toBe("KL");
  });

  it("未知渠道回落为原始 id，颜色用兜底色", () => {
    expect(providerLabel("future")).toBe("future");
    expect(providerAbbr("future")).toBe("future");
    expect(providerChartColor("future")).toBe(FALLBACK_CHART_COLOR);
  });

  it("每个已知渠道都有标签、缩写与专属配色（三者对齐）", () => {
    const providers = Object.keys(PROVIDER_LABEL);
    expect(providers.sort()).toEqual([
      "codearts", "codebuddy", "kilo", "qoder", "trae", "zen",
    ]);
    for (const provider of providers) {
      expect(PROVIDER_ABBR[provider as keyof typeof PROVIDER_ABBR]).toBeTruthy();
      expect(
        PROVIDER_CHART_COLOR[provider as keyof typeof PROVIDER_CHART_COLOR],
      ).toMatch(/^var\(--chart-\d\)$/);
    }
  });

  it("展示顺序 CB → TR → 其余，未知渠道排在已知渠道之后", () => {
    expect(PROVIDER_ORDER.slice(0, 2)).toEqual(["codebuddy", "trae"]);
    // PROVIDER_ORDER 覆盖全部已知渠道，与 PROVIDER_LABEL 对齐
    expect([...PROVIDER_ORDER].sort()).toEqual(Object.keys(PROVIDER_LABEL).sort());
    expect(providerRank("codebuddy")).toBe(0);
    expect(providerRank("trae")).toBe(1);
    expect(providerRank("zen")).toBeGreaterThan(providerRank("trae"));
    expect(providerRank("kilo")).toBeGreaterThan(providerRank("trae"));
    expect(providerRank("future")).toBe(PROVIDER_ORDER.length);
  });
});
