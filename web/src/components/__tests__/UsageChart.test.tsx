import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import {
  ProviderShare, UsageChart, chartProviders, formatTimeTick, hasData, niceCostCeiling,
  rateAxisDomain,
} from "../UsageChart";

describe("chartProviders", () => {
  it("收集出现过的渠道名，按字母序稳定排序", () => {
    expect(chartProviders([
      { hour: 1, trae: 1, codebuddy: 2, zen: 3 },
      { hour: 2, trae: 4 },
    ])).toEqual(["codebuddy", "trae", "zen"]);
  });

  it("只有 hour 时返回空数组（不把保留键当渠道）", () => {
    expect(chartProviders([{ hour: 1 }])).toEqual([]);
  });

  it("不把 __total 当渠道（它是全渠道合计键）", () => {
    expect(chartProviders([{ hour: 1, __total: 9, trae: 4 }])).toEqual(["trae"]);
  });
});

describe("hasData", () => {
  it("全 null / 全 0 视为无数据（不进图例与切换项）", () => {
    const points = [{ hour: 1, trae: 0 }, { hour: 2, trae: null }];
    expect(hasData(points, "trae")).toBe(false);
  });

  it("有任一非零值即有数据", () => {
    expect(hasData([{ hour: 1, trae: 0 }, { hour: 2, trae: 3 }], "trae")).toBe(true);
  });
});

describe("formatTimeTick", () => {
  it("同一天窗口只写时刻", () => {
    // 本地时区的同一天：用当天 10:00 与 14:00
    const day = new Date(2026, 9, 25, 10, 0, 0);
    const points = [
      { hour: Math.floor(day.getTime() / 1000) },
      { hour: Math.floor(day.getTime() / 1000) + 4 * 3600 },
    ];
    expect(formatTimeTick(points[0].hour * 1000, points)).toBe("10时");
  });

  it("跨天窗口带日期", () => {
    const first = new Date(2026, 9, 24, 23, 0, 0);
    const last = new Date(2026, 9, 25, 3, 0, 0);
    const points = [
      { hour: Math.floor(first.getTime() / 1000) },
      { hour: Math.floor(last.getTime() / 1000) },
    ];
    expect(formatTimeTick(first.getTime(), points)).toBe("10-24 23时");
  });

  it("withDate 强制带日期（hover 提示用）", () => {
    const same = new Date(2026, 9, 25, 10, 0, 0);
    expect(formatTimeTick(same.getTime(), [{ hour: Math.floor(same.getTime() / 1000) }], true))
      .toBe("10-25 10时");
  });

  it("空点集按同一天处理", () => {
    expect(formatTimeTick(new Date(2026, 9, 25, 10, 0, 0).getTime(), [])).toBe("10时");
  });
});

describe("rateAxisDomain", () => {
  it("数据全部高于 90% 时截断轴下界，并给三档刻度", () => {
    const axis = rateAxisDomain([0.9855, 0.999, 1]);
    expect(axis.domain).toEqual([0.9, 1]);
    expect(axis.ticks).toEqual([0.9, 0.95, 1]);
    expect(axis.truncated).toBe(true);
  });

  it("有低于 90% 的值时从 0 起（不做截断）", () => {
    const axis = rateAxisDomain([0.4, 1]);
    expect(axis.domain).toEqual([0, 1]);
    expect(axis.ticks).toEqual([0, 0.25, 0.5, 0.75, 1]);
    expect(axis.truncated).toBe(false);
  });

  it("忽略 null / 非数值（空档不参与定义域）", () => {
    expect(rateAxisDomain([null, undefined, 0.99]).domain).toEqual([0.9, 1]);
  });

  it("全空时退回 0 起的完整区间", () => {
    const axis = rateAxisDomain([null, null]);
    expect(axis.domain).toEqual([0, 1]);
    expect(axis.truncated).toBe(false);
  });
});

describe("niceCostCeiling", () => {
  it("向上取整到 1/2/2.5/5 × 10ⁿ 的整档", () => {
    // 0.0312 × 1.1 = 0.0343 → 0.05（5 × 10⁻²）
    expect(niceCostCeiling(0.0312)).toBeCloseTo(0.05);
    // 0.019 × 1.1 = 0.0209 → 0.025（2.5 × 10⁻²）
    expect(niceCostCeiling(0.019)).toBeCloseTo(0.025);
    expect(niceCostCeiling(1.5)).toBe(2);
    expect(niceCostCeiling(0.4)).toBeCloseTo(0.5);
  });

  it("0 或负值退回 1（不让定义域退化）", () => {
    expect(niceCostCeiling(0)).toBe(1);
    expect(niceCostCeiling(-1)).toBe(1);
  });

  it("5×10ⁿ 档不够时进下一档", () => {
    // 0.7 × 1.1 = 0.77：1/2/2.5/5 × 10⁻¹ 都 < 0.77 → 10 × 10⁻¹ = 1
    expect(niceCostCeiling(0.7)).toBeCloseTo(1);
  });
});

describe("ProviderShare", () => {
  it("堆叠条按份额排宽度，图例降序展示百分比与请求数", () => {
    const { getByTestId } = render(
      <ProviderShare
        rows={[
          { provider: "trae", value: 50 },
          { provider: "codebuddy", value: 70 },
          { provider: "qoder", value: 5 },
        ]}
        total={125}
      />,
    );
    const root = getByTestId("provider-share");
    // 堆叠条：份额合计 100%，最宽的段是 codebuddy（70/125 = 56%）
    const bar = root.querySelector("[role='img']")!;
    const segments = [...bar.querySelectorAll("span")];
    expect(segments).toHaveLength(3);
    // React 归一化内联样式，`56.00%` 落成 `56%`——按数值断言，不锁字符串
    const widthOf = (el: Element) =>
      Number(/width:\s*([\d.]+)%/.exec(el.getAttribute("style") ?? "")?.[1]);
    expect(widthOf(segments[0])).toBeCloseTo(56, 1);
    expect(widthOf(segments[1])).toBeCloseTo(40, 1);
    expect(widthOf(segments[2])).toBeCloseTo(4, 1);
    expect(widthOf(segments[0]) + widthOf(segments[1]) + widthOf(segments[2]))
      .toBeCloseTo(100, 1);
    // 图例降序：CodeBuddy 在 TRAE 之前
    const items = [...root.querySelectorAll("li")];
    expect(items[0]).toHaveTextContent("CodeBuddy");
    expect(items[0]).toHaveTextContent("56.0%");
    expect(items[0]).toHaveTextContent("70");
    expect(items[1]).toHaveTextContent("TRAE");
  });

  it("total 为 0 时不除以 0（份额 0%，条形宽 0）", () => {
    const { getByTestId } = render(
      <ProviderShare rows={[{ provider: "trae", value: 0 }]} total={0} />,
    );
    const root = getByTestId("provider-share");
    const segment = root.querySelector("[role='img'] span")!;
    expect(segment.getAttribute("style")).toContain("0%");
    expect(root.querySelector("li")).toHaveTextContent("0.0%");
  });
});

describe("UsageChart", () => {
  it("渲染容器（jsdom 无布局，ResponsiveContainer 不产出 SVG）", () => {
    const { getByTestId } = render(
      <UsageChart points={[
        { hour: 1_700_000_000, codebuddy: 1, future: 2, __total: 3 },
        { hour: 1_700_003_600, codebuddy: 3, future: 4, __total: 7 },
      ]} />,
    );
    expect(getByTestId("usage-chart")).toBeInTheDocument();
  });

  it("默认画全部渠道合计，图例在图表下方", () => {
    const { getByTestId } = render(
      <UsageChart points={[
        { hour: 1_700_000_000, codebuddy: 1, trae: 2, __total: 3 },
        { hour: 1_700_003_600, codebuddy: 3, trae: 4, __total: 7 },
      ]} />,
    );
    const chart = getByTestId("usage-chart");
    const legend = getByTestId("usage-chart-legend");
    // 图例是图表容器（固定高度绘图区）之后的兄弟节点——位于下方，不占标题行
    const plot = chart.firstElementChild;
    // 绘图区高度：窄屏 h-56 / 桌面 sm:h-52（原来固定 h-64，桌面留白过多）
    expect(plot?.className).toContain("h-56");
    expect(plot?.className).toContain("sm:h-52");
    expect(legend.previousElementSibling).toBe(plot);
    // 默认口径是「全部渠道」：单系列不列各渠道
    expect(legend).toHaveTextContent("全部渠道");
    expect(legend).not.toHaveTextContent("CodeBuddy");
  });

  it("切到单渠道时图例显示该渠道", () => {
    const { getByTestId } = render(
      <UsageChart
        scope="trae"
        points={[
          { hour: 1_700_000_000, codebuddy: 1, trae: 2, __total: 3 },
          { hour: 1_700_003_600, codebuddy: 3, trae: 4, __total: 7 },
        ]}
      />,
    );
    expect(getByTestId("usage-chart-legend")).toHaveTextContent("TRAE");
  });

  it("整窗无数据时给出提示而不是留白", () => {
    const { getByTestId } = render(
      <UsageChart points={[{ hour: 1_700_000_000, __total: null }]} />,
    );
    expect(getByTestId("usage-chart-empty")).toBeInTheDocument();
  });

  it("比率轴截断时在图例标注，避免读者误以为底部是 0", () => {
    const { getByTestId } = render(
      <UsageChart
        metric="success_rate"
        points={[
          { hour: 1_700_000_000, __total: 0.99 },
          { hour: 1_700_003_600, __total: 1 },
        ]}
      />,
    );
    expect(getByTestId("usage-chart-axis-note")).toHaveTextContent("Y 轴自 90% 起");
  });

  it("非比率指标不出现截断标注", () => {
    const { queryByTestId } = render(
      <UsageChart points={[{ hour: 1_700_000_000, __total: 5 }]} />,
    );
    expect(queryByTestId("usage-chart-axis-note")).toBeNull();
  });

  it("有数据时不显示空态提示", () => {
    const { queryByTestId } = render(
      <UsageChart points={[{ hour: 1_700_000_000, __total: 5 }]} />,
    );
    expect(queryByTestId("usage-chart-empty")).toBeNull();
  });

  it("无点时也算空态", () => {
    const { getByTestId } = render(<UsageChart points={[]} />);
    expect(getByTestId("usage-chart-empty")).toBeInTheDocument();
  });
});
