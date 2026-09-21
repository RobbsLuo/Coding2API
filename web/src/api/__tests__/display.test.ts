import { describe, expect, it } from "vitest";
import { expiringQuotaLabel, formatChartValue, creditEventLabel, tokenExpiryView } from "../display";

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
describe("tokenExpiryView", () => {
  const now = 1_700_000_000;
  const DAY = 86400;

  it("到期时间未知（0）→ 全部为 null，不预警", () => {
    const view = tokenExpiryView(0, now, 3600, now);
    expect(view.remaining).toBeNull();
    expect(view.expiring).toBe(false);
    expect(view.percent).toBeNull();
    expect(tokenExpiryView(null, now, 3600, now).remaining).toBeNull();
    expect(tokenExpiryView(undefined, now, 3600, now).remaining).toBeNull();
  });

  it("进入阈值窗口 → 预警且 tone=warn；进度条按 token 自身寿命算", () => {
    // 寿命 30 天，只剩 600 秒 → 几乎见底
    const view = tokenExpiryView(now + 600, now - 30 * DAY + 600, 3600, now);
    expect(view.remaining).toBe(600);
    expect(view.expiring).toBe(true);
    expect(view.tone).toBe("warn");
    expect(view.percent).toBeCloseTo((600 / (30 * DAY)) * 100, 5);
  });

  it("阈值外不预警，tone=ok", () => {
    const view = tokenExpiryView(now + 30 * DAY, now, 3600, now);
    expect(view.expiring).toBe(false);
    expect(view.tone).toBe("ok");
    expect(view.label).toBe("30.0 天");
    // 刚续期 → 满格
    expect(view.percent).toBe(100);
  });

  it("已过期 → 剩余归零、tone=danger（仍算预警）", () => {
    const view = tokenExpiryView(now - 60, now - 30 * DAY, 3600, now);
    expect(view.remaining).toBe(0);
    expect(view.expiring).toBe(true);
    expect(view.tone).toBe("danger");
    expect(view.percent).toBe(0);
  });

  it("阈值 ≤0 关闭预警（仍给剩余时间）", () => {
    const view = tokenExpiryView(now + 600, now, 0, now);
    expect(view.remaining).toBe(600);
    expect(view.expiring).toBe(false);
  });

  it("拿不到签发时间 → 不画进度条（percent=null），只给剩余时间", () => {
    // 没有 iat 就不知道 token 寿命；拿固定量程会把 50 天的 token 永远画成满格
    expect(tokenExpiryView(now + 600, 0, 3600, now).percent).toBeNull();
    expect(tokenExpiryView(now + 600, null, 3600, now).percent).toBeNull();
    expect(tokenExpiryView(now + 600, undefined, 3600, now).percent).toBeNull();
    // 签发时间晚于到期时间（脏数据）→ 寿命 ≤0，同样不画条而不是画负宽度
    expect(tokenExpiryView(now + 600, now + 900, 3600, now).percent).toBeNull();
  });

  it("进度条上限 100%（剩余超过寿命时，如时钟偏差）", () => {
    expect(tokenExpiryView(now + 400 * DAY, now, 3600, now).percent).toBe(100);
  });
});

describe("creditEventLabel", () => {
  it("净增加：带符号 + 前后对照", () => {
    expect(creditEventLabel({ delta: 15, before: 100, after: 115, source: "observed" }))
      .toBe("+15（100 → 115）");
  });

  it("净减少：如实给负数（对话消耗等）", () => {
    expect(creditEventLabel({ delta: -5, before: 110, after: 105, source: "observed" }))
      .toBe("-5（110 → 105）");
  });

  it("首次建立基线：只说基线值，不算积分", () => {
    expect(creditEventLabel({ delta: null, before: null, after: 100, source: "sync" }))
      .toBe("基线 100");
  });

  it("变化无法量化：说「变为未知」，绝不渲染成 0 的变动量", () => {
    const label = creditEventLabel({ delta: null, before: 100, after: null, source: "observed" });
    expect(label).toBe("由 100 变为未知");
    // 不能出现「±0（… → …）」这种「把未知当成没变」的读法
    expect(label).not.toMatch(/[+-]0/);
  });
});
