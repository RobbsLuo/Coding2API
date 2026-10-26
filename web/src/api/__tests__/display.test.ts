import { describe, expect, it } from "vitest";
import { makeCredential } from "@/test-utils";
import {
  creditEventLabel,
  expiringQuotaLabel,
  formatAgo,
  formatAxisValue,
  formatCacheRate,
  formatChartValue,
  formatCredit,
  formatMoney,
  formatSpeed,
  METRIC_AXIS_UNIT,
  QUOTA_UNIT,
  taskReportLabel,
  tokenExpiryView,
  activeModelCooldowns,
  credentialState,
  formatDuration,
  formatNumber,
  formatTime,
  formatTimeParts,
  upstreamLevelLabel,
  upstreamLevelTone,
  hasQuotaProbe,
  healthView,
  modelCooldownLabel,
  probeFailureLabel,
  quotaSemantics,
  usageErrorLabel,
} from "../display";

describe("formatCredit", () => {
  it("null/undefined 显示占位符", () => {
    expect(formatCredit(null)).toBe("—");
    expect(formatCredit(undefined)).toBe("—");
  });

  it("真实值直接展示（保留 2 位）", () => {
    expect(formatCredit(12.5)).toBe("12.5");
    expect(formatCredit(0.349)).toBe("0.35");
    expect(formatCredit(12.5, false)).toBe("12.5");
    expect(formatCredit(12.5, 0)).toBe("12.5");
  });

  it("推算值前置 ≈（boolean 与 0/1 都识别）", () => {
    expect(formatCredit(5.25, true)).toBe("≈5.25");
    expect(formatCredit(5.25, 1)).toBe("≈5.25");
    expect(formatCredit(0, true)).toBe("≈0");
  });
});

describe("formatMoney", () => {
  it("null/undefined 显示占位符（未匹配定价或未上报 token）", () => {
    expect(formatMoney(null)).toBe("—");
    expect(formatMoney(undefined)).toBe("—");
  });

  it("人民币为主、美元可切；默认前置 ≈ 标明是估算", () => {
    expect(formatMoney(12.34, "CNY")).toBe("≈¥12.34");
    expect(formatMoney(1.84, "USD")).toBe("≈$1.84");
    expect(formatMoney(12.34, "CNY", false)).toBe("¥12.34");
  });

  it("小额自适应小数位，不把零点几厘抹成 0", () => {
    expect(formatMoney(0.0042, "CNY", false)).toBe("¥0.0042");
    expect(formatMoney(0.5, "CNY", false)).toBe("¥0.5");
    expect(formatMoney(3, "CNY", false)).toBe("¥3");
  });
});

describe("formatCacheRate", () => {
  it("命中 / 输入，保留 1 位百分比", () => {
    expect(formatCacheRate(400, 1000)).toBe("40.0%");
    expect(formatCacheRate(1, 3)).toBe("33.3%");
    expect(formatCacheRate(0, 1000)).toBe("0.0%");
    expect(formatCacheRate(1000, 1000)).toBe("100.0%");
  });

  it("缓存未上报或输入为 0 → —（不拿 0 冒充 0%）", () => {
    expect(formatCacheRate(null, 1000)).toBe("—");
    expect(formatCacheRate(undefined, 1000)).toBe("—");
    expect(formatCacheRate(10, null)).toBe("—");
    expect(formatCacheRate(10, 0)).toBe("—");
    expect(formatCacheRate(0, 0)).toBe("—");
  });

  it("命中超过输入时按 100% 截断（避免 >100% 怪值）", () => {
    expect(formatCacheRate(1500, 1000)).toBe("100.0%");
  });
});

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

  it("成本：人民币金额 + 单位（元）", () => {
    expect(formatChartValue(12.34, "cost")).toBe("¥12.34 (元)");
  });

  it("未知指标回退请求次数", () => {
    expect(formatChartValue(12, "bogus")).toBe("12 (次)");
  });
});

describe("formatSpeed", () => {
  it("输出速度带 tok/s；估算值前置 ≈；无样本显示 —", () => {
    expect(formatSpeed(94.1)).toBe("94.1 tok/s");
    expect(formatSpeed(94.1, false)).toBe("94.1 tok/s");
    expect(formatSpeed(76.4, true)).toBe("≈76.4 tok/s");
    expect(formatSpeed(0)).toBe("0.0 tok/s");
    expect(formatSpeed(null)).toBe("—");
    expect(formatSpeed(undefined)).toBe("—");
  });
});

describe("formatAxisValue", () => {
  it("计数 / token：紧凑格式且不带单位（单位交给轴顶 label）", () => {
    expect(formatAxisValue(600, "requests")).toBe("600");
    expect(formatAxisValue(15000000, "tokens")).toBe("1500万");
    expect(formatAxisValue(12, "bogus")).toBe("12");
  });

  it("耗时 / 首字：复用 formatLatency，刻度自带 ms/s", () => {
    expect(formatAxisValue(850, "latency")).toBe("850 ms");
    expect(formatAxisValue(22000, "ttfb")).toBe("22 s");
  });

  it("成本：保留两位小数（不用万/亿紧凑格式）", () => {
    expect(formatAxisValue(12.34, "cost")).toBe("12.34");
    expect(formatAxisValue(1500.5, "cost")).toBe("1,500.5");
  });

  it("轴顶单位映射：耗时/首字不重复标单位", () => {
    expect(METRIC_AXIS_UNIT.requests).toBe("次");
    expect(METRIC_AXIS_UNIT.tokens).toBe("tokens");
    expect(METRIC_AXIS_UNIT.cost).toBe("元");
    expect(METRIC_AXIS_UNIT.latency).toBeUndefined();
    expect(METRIC_AXIS_UNIT.ttfb).toBeUndefined();
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

  it("CodeArts 折成积分后与其余渠道同单位（默认措辞即为积分）", () => {
    expect(expiringQuotaLabel(999.5, 3600)).toBe("999.5 积分将在 1.0 小时内过期");
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

describe("QUOTA_UNIT", () => {
  it("全渠道统一为积分（CodeArts 的 token 已在后端折算）", () => {
    expect(QUOTA_UNIT).toBe("积分");
  });
});
describe("tokenExpiryView", () => {
  const now = 1_700_000_000;
  const DAY = 86400;

  it("到期时间未知（0）→ remaining 为 null，不预警", () => {
    const view = tokenExpiryView(0, 3600, now);
    expect(view.remaining).toBeNull();
    expect(view.expiring).toBe(false);
    expect(view.label).toBe("—");
    expect(tokenExpiryView(null, 3600, now).remaining).toBeNull();
    expect(tokenExpiryView(undefined, 3600, now).remaining).toBeNull();
  });

  it("进入阈值窗口 → 预警且 expiring=true", () => {
    const view = tokenExpiryView(now + 600, 3600, now);
    expect(view.remaining).toBe(600);
    expect(view.expiring).toBe(true);
    expect(view.label).toBe("10 分钟");
  });

  it("阈值外不预警", () => {
    const view = tokenExpiryView(now + 30 * DAY, 3600, now);
    expect(view.expiring).toBe(false);
    expect(view.label).toBe("30.0 天");
  });

  it("已过期 → 剩余归零、仍算预警（展示层写作「已过期」）", () => {
    const view = tokenExpiryView(now - 60, 3600, now);
    expect(view.remaining).toBe(0);
    expect(view.expiring).toBe(true);
    // 归零后 formatDuration 给「—」，展示层改用 remaining<=0 判断，不依赖 label
    expect(view.label).toBe("—");
  });

  it("阈值 ≤0 关闭预警（仍给剩余时间）", () => {
    const view = tokenExpiryView(now + 600, 0, now);
    expect(view.remaining).toBe(600);
    expect(view.expiring).toBe(false);
  });

  it("短寿命凭证：剩余落在预警窗口内也不标红（refresh 常态，非异常）", () => {
    // CodeArts STS：寿命 2h（exp - iat = 7200）、预警 1h。剩余 30min 时距刷新
    // 窗口很近，但「即将到期」是常态，不该恒标红。
    const issuedAt = now - (7200 - 1800); // 已过 1.5h，剩余 30min
    const view = tokenExpiryView(now + 1800, 3600, now, issuedAt);
    expect(view.remaining).toBe(1800);
    expect(view.expiring).toBe(false);
    expect(view.label).toBe("30 分钟");
  });

  it("短寿命凭证真过期（剩余 ≤0）仍标红，不静音刷新失败信号", () => {
    const issuedAt = now - 7200 - 60; // 寿命 2h，已过期 60s
    const view = tokenExpiryView(now - 60, 3600, now, issuedAt);
    expect(view.remaining).toBe(0);
    expect(view.expiring).toBe(true);
  });

  it("长寿命凭证不受短寿命静音影响：进入窗口照常标红", () => {
    // 30 天寿命 ≫ 2×预警窗口，签发给不给签发给都不抑制
    const issuedAt = now - (30 * DAY - 600);
    const view = tokenExpiryView(now + 600, 3600, now, issuedAt);
    expect(view.expiring).toBe(true);
  });

  it("不依赖签发时间：未传 issuedAt 时只按剩余时间判定", () => {
    // 签发时间未知时不静音，保持旧语义（否则会误藏预警）
    expect(tokenExpiryView(now + 600, 3600, now, 0).expiring).toBe(true);
    expect(tokenExpiryView(now + 600, 3600, now, null).expiring).toBe(true);
    expect(tokenExpiryView(now + 600, 3600, now).remaining).toBe(600);
    expect(tokenExpiryView(now + 400 * DAY, 3600, now).label).toBe("400.0 天");
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

describe("formatAgo", () => {
  const NOW = 1_700_000_000;

  it("十秒内算「刚刚」，往前用「前」、往后用「后」", () => {
    expect(formatAgo(NOW - 3, NOW)).toBe("刚刚");
    expect(formatAgo(NOW + 3, NOW)).toBe("刚刚");
    expect(formatAgo(NOW - 120, NOW)).toBe("2 分钟前");
    expect(formatAgo(NOW - 7200, NOW)).toBe("2.0 小时前");
    expect(formatAgo(NOW + 3600, NOW)).toBe("1.0 小时后");
  });

  it("缺失时间给破折号，而不是「刚刚」", () => {
    expect(formatAgo(null, NOW)).toBe("—");
    expect(formatAgo(undefined, NOW)).toBe("—");
    expect(formatAgo(0, NOW)).toBe("—");
  });
});

describe("taskReportLabel", () => {
  it("已知字段用中文名，未知字段保留原 key（后端加字段前端不炸）", () => {
    expect(taskReportLabel({ attempted: 2, succeeded: 1, skipped: 1 }))
      .toBe("尝试 2 · 成功 1 · 跳过 1");
    expect(taskReportLabel({ rolled_up: 3, purged: 1 })).toBe("汇总小时 3 · 清理明细 1");
    expect(taskReportLabel({ brand_new: 7 })).toBe("brand_new 7");
  });

  it("无报告给破折号；空对象说「无明细」而不是空白", () => {
    expect(taskReportLabel(null)).toBe("—");
    expect(taskReportLabel({})).toBe("无明细");
  });
});

describe("display helpers", () => {
  it("健康度四态互不混淆", () => {
    expect(healthView(null)).toMatchObject({ kind: "unknown", label: "未探测", tone: "muted" });
    expect(healthView(-1)).toMatchObject({ kind: "exhausted", label: "已耗尽", tone: "danger" });
    expect(healthView(0)).toMatchObject({ kind: "known", percent: 0, tone: "danger" });
    expect(healthView(30)).toMatchObject({ kind: "known", tone: "warn" });
    expect(healthView(90)).toMatchObject({ kind: "known", tone: "ok" });
  });

  it("undefined 也视为未探测", () => {
    expect(healthView(undefined as unknown as null).kind).toBe("unknown");
  });

  it("免费层（zen / kilo）的 null 归「无探测」，付费渠道归「未探测」", () => {
    expect(healthView(null, "zen")).toMatchObject({ kind: "noprobe", label: "无探测" });
    expect(healthView(null, "kilo")).toMatchObject({ kind: "noprobe", label: "无探测" });
    expect(healthView(null, "codebuddy")).toMatchObject({ kind: "unknown", label: "未探测" });
    // 已知额度不受渠道影响
    expect(healthView(62, "zen")).toMatchObject({ kind: "known", label: "62%" });
    expect(hasQuotaProbe("zen")).toBe(false);
    expect(hasQuotaProbe("kilo")).toBe(false);
    expect(hasQuotaProbe("trae")).toBe(true);
    // Qoder / CodeArts / officeAce 都有额度接口，属「需探测」
    expect(hasQuotaProbe("qoder")).toBe(true);
    expect(hasQuotaProbe("codearts")).toBe(true);
    expect(hasQuotaProbe("officeace")).toBe(true);
  });

  it("周期语义按渠道类型判定，而不是看 cycle_end 是否存在", () => {
    // CodeBuddy 有重置时间
    const cycle = quotaSemantics(makeCredential({
      provider: "codebuddy", quota_cycle_end: 1_800_000_000 }) as never);
    expect(cycle).toContain("本周期剩余，");
    expect(cycle).toContain("重置");

    // CodeBuddy 未探测：cycle_end 为 null，但绝不能显示成 TRAE 的语义
    const unprobed = quotaSemantics(makeCredential({
      provider: "codebuddy", quota_cycle_end: null }) as never);
    expect(unprobed).toContain("本周期剩余");
    expect(unprobed).not.toContain("单调递减");

    // TRAE 永远是单调递减
    const balance = quotaSemantics(makeCredential({
      provider: "trae", quota_cycle_end: null }) as never);
    expect(balance).toBe("账户剩余（单调递减）");

    // Kilo 与 Zen 同为免费层：无额度接口
    const kilo = quotaSemantics(makeCredential({
      provider: "kilo", quota_cycle_end: null }) as never);
    expect(kilo).toBe("免费层（无额度接口）");

    // Qoder 与 CodeBuddy 同属周期重置（未探测到重置时间时退化口径）
    const qoder = quotaSemantics(makeCredential({
      provider: "qoder", quota_cycle_end: null }) as never);
    expect(qoder).toContain("本周期剩余");
    expect(qoder).not.toContain("单调递减");

    // CodeArts 是积分余额（内置模型按积分计费），按积分包到期日作废；
    // 有 cycle_end 时点出最早到期日，不说「本周期」也不套「每日额度池」
    const codearts = quotaSemantics(makeCredential({
      provider: "codearts", quota_cycle_end: 1_800_000_000 }) as never);
    expect(codearts).toContain("积分余额（内置模型计费）");
    expect(codearts).toContain("最早");
    expect(codearts).not.toContain("每日额度池");

    // 未探测到到期时间：退化为不带日期的积分口径
    const codeartsNoEnd = quotaSemantics(makeCredential({
      provider: "codearts", quota_cycle_end: null }) as never);
    expect(codeartsNoEnd).toBe("积分余额（内置模型计费，按积分包到期日作废）");

    // officeAce 的积分是主账，逐笔到期（新用户福利 + 每日签到各自计时），
    // 不能说成「本周期剩余」（那是统一重置的语义）
    const officeace = quotaSemantics(makeCredential({
      provider: "officeace", quota_cycle_end: 1_800_000_000 }) as never);
    expect(officeace).toContain("积分余额");
    expect(officeace).toContain("逐笔到期");
    expect(officeace).not.toContain("本周期");

    // 国际版 Qoder 无旧签到接口，但额度语义不变（不在此断言）
  });

  it("凭证状态机覆盖全部状态", () => {
    expect(credentialState(makeCredential() as never, 0)).toBe("ready");
    expect(credentialState(makeCredential({ disabled: 1 }) as never, 0)).toBe("disabled");
    expect(credentialState(makeCredential({ enabled: 0 }) as never, 0)).toBe("off");
    expect(credentialState(makeCredential({ cooling_until: 500 }) as never, 100)).toBe("cooling");
    expect(credentialState(makeCredential({ health: -1 }) as never, 0)).toBe("exhausted");
    // 冷却已过期 → 不再是 cooling
    expect(credentialState(makeCredential({ cooling_until: 50 }) as never, 100)).toBe("ready");
  });

  it("时长格式化覆盖秒/分/时/天与零值", () => {
    expect(formatDuration(0)).toBe("—");
    expect(formatDuration(-5)).toBe("—");
    expect(formatDuration(30)).toBe("30 秒");
    expect(formatDuration(120)).toBe("2 分钟");
    expect(formatDuration(7200)).toBe("2.0 小时");
    expect(formatDuration(172800)).toBe("2.0 天");
  });

  it("探测失败原因有中文文案，且未知值有兜底", () => {
    expect(probeFailureLabel("credential_rejected")).toContain("重新登录");
    expect(probeFailureLabel("upstream_unavailable")).toContain("与凭证无关");
    expect(probeFailureLabel("network_unreachable")).toContain("连不上渠道服务器");
    expect(probeFailureLabel(undefined)).toBe("未知错误");
    expect(probeFailureLabel("something_new" as never)).toBe("未知错误");
  });

  it("明细失败类型翻成中文，未知值不进主提示但保留原文", () => {
    expect(usageErrorLabel("rate_limit")).toBe("额度耗尽");
    expect(usageErrorLabel("client_disconnect")).toBe("客户端中断");
    expect(usageErrorLabel("model_unavailable")).toBe("该模型暂时不可用");
    expect(usageErrorLabel("no_healthy_credential")).toBe("无可用凭证");
    // 「上游没这个模型」与「请求无效」是两回事（2026-10-26 独立成类）
    expect(usageErrorLabel("model_not_found")).toBe("上游无此模型");
    expect(usageErrorLabel("invalid_request")).toBe("请求无效");
    // A1 流式超时：两个新枚举都要有中文文案（不能落到兜底「失败（xxx）」）
    expect(usageErrorLabel("upstream_first_event_timeout")).toBe("上游首字超时");
    expect(usageErrorLabel("upstream_idle_timeout")).toBe("上游流中断");
    expect(usageErrorLabel(null)).toBe("失败");
    expect(usageErrorLabel(undefined)).toBe("失败");
    // 后端新增枚举时前端不能显示空白：兜底文案带原值便于对照排查
    expect(usageErrorLabel("brand_new_reason")).toBe("失败（brand_new_reason）");
  });

  it("时间与数字格式化处理空值", () => {
    expect(formatTime(null)).toBe("—");
    expect(formatTime(1_700_000_000)).not.toBe("—");
    expect(formatNumber(null)).toBe("—");
    expect(formatNumber(1234)).toBe("1,234");
  });

  it("时间拆成日期 / 时刻两段（明细表两行显示），空值只给一个破折号", () => {
    // 1700000000 = 2023-11-15 06:13:20 UTC，本地时区换算后两段都要补齐 0
    const [date, clock] = formatTimeParts(1_700_000_000);
    expect(date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(clock).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    // 与 formatTime 同一时刻（同一天，同一时区口径）
    expect(formatTime(1_700_000_000)).toContain(String(Number(date.slice(0, 4))));
    expect(formatTimeParts(null)).toEqual(["—", ""]);
    expect(formatTimeParts(0)).toEqual(["—", ""]);
  });

  it("模型级冷却只展示生效中的条目，按剩余时长升序", () => {
    const now = 1_000_000;
    const credential = makeCredential({
      model_cooldowns: [
        { model: "late", cooling_until: now + 600, hits: 1, reason: "model" },
        { model: "stale", cooling_until: now - 600, hits: 1, reason: "model" },
        { model: "soon", cooling_until: now + 60, hits: 2, reason: "blocked" },
      ],
    });
    expect(activeModelCooldowns(credential as never, now).map((item) => item.model)).toEqual([
      "soon",
      "late",
    ]);
    // 无冷却（老后端不返回该字段）时为空数组，不抛错
    expect(activeModelCooldowns(makeCredential() as never, now)).toEqual([]);
  });

  it("模型冷却文案区分「模型限流」与「该账号无此模型」", () => {
    expect(modelCooldownLabel(
      { model: "m", cooling_until: 1, hits: 1, reason: "model" })).toContain("其它模型不受影响");
    expect(modelCooldownLabel(
      { model: "m", cooling_until: 1, hits: 1, reason: "blocked" })).toContain("无此模型");
  });

  it("上游档位：六档各有中文名，未知取值不原样透传", () => {
    expect(upstreamLevelLabel("operational")).toBe("正常");
    expect(upstreamLevelLabel("degraded")).toBe("性能下降");
    expect(upstreamLevelLabel("partial")).toBe("部分中断");
    expect(upstreamLevelLabel("major")).toBe("重大故障");
    expect(upstreamLevelLabel("maintenance")).toBe("维护中");
    expect(upstreamLevelLabel("unknown")).toBe("未知");
    // 后端新增档位时前端不显示空白
    expect(upstreamLevelLabel("brand_new_level")).toBe("未知");
  });

  it("上游档位色调：partial / major 同为 danger，unknown 用 muted", () => {
    expect(upstreamLevelTone("operational")).toBe("ok");
    expect(upstreamLevelTone("degraded")).toBe("warn");
    expect(upstreamLevelTone("maintenance")).toBe("warn");
    expect(upstreamLevelTone("partial")).toBe("danger");
    expect(upstreamLevelTone("major")).toBe("danger");
    // 抓取失败不是「上游挂了」，不该抢真实故障的红色
    expect(upstreamLevelTone("unknown")).toBe("muted");
  });
});
