import { describe, expect, it } from "vitest";
import { expiringQuotaLabel, formatChartValue } from "../display";

describe("formatChartValue", () => {
  it("请求次数：紧凑数字 + 单位（次）", () => {
    expect(formatChartValue(120, "requests")).toBe("120 (次)");
    expect(formatChartValue(12345, "requests")).toBe("1.2万 (次)");
  });

  it("token：紧凑数字 + 单位（tokens）", () => {
    expect(formatChartValue(260, "tokens")).toBe("260 (tokens)");
    expect(formatChartValue(1234567, "tokens")).toBe("123.5万 (tokens)");
  });

  it("耗时/首字延迟：毫秒人性化格式（自带 ms/s 单位）", () => {
    expect(formatChartValue(850, "latency")).toBe("850 ms");
    expect(formatChartValue(8200, "ttfb")).toBe("8.2 s");
  });

  it("未知指标回退请求次数", () => {
    expect(formatChartValue(12, "bogus")).toBe("12 (次)");
  });
});

describe("expiringQuotaLabel", () => {
  it("窗口内有过期积分时给出量级与剩余时间", () => {
    expect(expiringQuotaLabel(100, 129600)).toBe("100 积分将在 1.5 天内过期");
    expect(expiringQuotaLabel(25.5, 3600)).toBe("25.5 积分将在 1.0 小时内过期");
  });

  it("次窗口措辞区分开，避免两行同句式分不出优先级", () => {
    expect(expiringQuotaLabel(140, 604800, "secondary")).toBe("7.0 天内共 140 积分将过期");
  });

  it("无到期信息、窗口关闭或零积分时不展示", () => {
    expect(expiringQuotaLabel(null, 129600)).toBeNull();
    expect(expiringQuotaLabel(100, undefined)).toBeNull();
    expect(expiringQuotaLabel(100, 0)).toBeNull();
    expect(expiringQuotaLabel(0, 129600)).toBeNull();
    expect(expiringQuotaLabel(null, 604800, "secondary")).toBeNull();
    expect(expiringQuotaLabel(100, 0, "secondary")).toBeNull();
    expect(expiringQuotaLabel(0, 604800, "secondary")).toBeNull();
  });
});