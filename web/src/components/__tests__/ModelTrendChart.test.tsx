import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ModelLegend, ModelTrendChart, modelTrendAxis } from "../ModelTrendChart";

const POINTS = [
  { hour: 1_700_000_000, "glm-5.2": 4, "kimi-k3": 1 },
  { hour: 1_700_003_600, "glm-5.2": 0, "kimi-k3": 2 },
];

describe("ModelTrendChart", () => {
  it("渲染容器（jsdom 无布局，ResponsiveContainer 不产出 SVG）", () => {
    render(<ModelTrendChart points={POINTS} models={["glm-5.2", "kimi-k3"]} />);
    expect(screen.getByTestId("model-trend-chart")).toBeInTheDocument();
  });

  it("模型数超过 3 时只画前 3 条（更多交给分组表）", () => {
    const models = ["a", "b", "c", "d", "e"];
    render(<ModelTrendChart points={POINTS} models={models} />);
    // 图例只列前 3 个：第 4、5 个不上图
    render(<ModelLegend models={models} />);
    const legends = screen.getAllByTestId("model-trend-legend");
    expect(legends[0]).toHaveTextContent("a");
    expect(legends[0]).not.toHaveTextContent("d");
  });

  it("均值类指标（null 空档）断线，可加量指标连线", () => {
    // 仅验证不抛错：connectNulls 由 metric 决定，行为在 SVG 层，jsdom 无法断言
    const sparsePoints = [
      { hour: 1_700_000_000, "glm-5.2": 100 },
      { hour: 1_700_003_600, "glm-5.2": null },
    ];
    render(<ModelTrendChart points={sparsePoints} models={["glm-5.2"]} metric="latency" />);
    render(<ModelTrendChart points={sparsePoints} models={["glm-5.2"]} metric="requests" />);
    expect(screen.getAllByTestId("model-trend-chart")).toHaveLength(2);
  });
});

describe("modelTrendAxis", () => {
  it("上限取所有在画模型线的公共最大值（不是第一条线）", () => {
    // 第一条是「请求量 Top1」，但第二条的峰值更高（耗时 / 成本类常见）
    const points = [
      { hour: 1, "glm-5.2": 4, "kimi-k3": 90 },
      { hour: 2, "glm-5.2": 2, "kimi-k3": null },
    ];
    expect(modelTrendAxis(points, ["glm-5.2", "kimi-k3"], "latency").domain)
      .toEqual([0, 90]);
  });

  it("第 4 名及以后不参与定义域（不上图就不撑轴）", () => {
    const points = [{ hour: 1, a: 1, b: 2, c: 3, d: 9999 }];
    expect(modelTrendAxis(points, ["a", "b", "c", "d"], "requests").domain).toEqual([0, 3]);
  });

  it("比率全部 ≥ 90% 时截断下界并标记（与主图同口径）", () => {
    const points = [{ hour: 1, a: 0.985, b: 0.995 }];
    const axis = modelTrendAxis(points, ["a", "b"], "success_rate");
    expect(axis.domain).toEqual([0.9, 1]);
    expect(axis.ticks).toEqual([0.9, 0.95, 1]);
    expect(axis.truncated).toBe(true);
  });

  it("比率低于 90% 时不截断", () => {
    const axis = modelTrendAxis([{ hour: 1, a: 0.5 }], ["a"], "cache_rate");
    expect(axis.domain).toEqual([0, 1]);
    expect(axis.truncated).toBe(false);
  });

  it("单请求成本取规整上界（不出 0.0085 / 0.017 这类刻度）", () => {
    const axis = modelTrendAxis([{ hour: 1, a: 0.031 }], ["a"], "cost_per_request");
    expect(axis.domain[0]).toBe(0);
    expect(axis.domain[1]).toBeCloseTo(0.05, 10);
    expect(axis.truncated).toBe(false);
  });

  it("全空 / 全 0 时回落 [0, 1]，不退化成一个点", () => {
    expect(modelTrendAxis([], [], "tokens").domain).toEqual([0, 1]);
    expect(modelTrendAxis([{ hour: 1, a: null }, { hour: 2, a: 0 }], ["a"], "requests").domain)
      .toEqual([0, 1]);
  });
});

describe("ModelLegend", () => {
  it("单系列不渲染图例（标题已说明是哪条）", () => {
    const { container } = render(<ModelLegend models={["glm-5.2"]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("多系列渲染色点 + 模型名", () => {
    render(<ModelLegend models={["glm-5.2", "kimi-k3"]} />);
    const legend = screen.getByTestId("model-trend-legend");
    expect(legend).toHaveTextContent("glm-5.2");
    expect(legend).toHaveTextContent("kimi-k3");
  });

  it("空模型列表不渲染", () => {
    const { container } = render(<ModelLegend models={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("轴口径说明：单系列也显示（只出说明，不列模型名）", () => {
    render(<ModelLegend models={["glm-5.2"]} note="Y 轴自 90% 起（便于看清窄幅波动）" />);
    const legend = screen.getByTestId("model-trend-legend");
    expect(legend).toHaveTextContent("Y 轴自 90% 起");
    expect(legend).not.toHaveTextContent("glm-5.2");
  });

  it("多系列时说明与色点同行", () => {
    render(<ModelLegend models={["glm-5.2", "kimi-k3"]} note="Y 轴自 90% 起" />);
    expect(screen.getByTestId("model-trend-axis-note")).toHaveTextContent("Y 轴自 90% 起");
    expect(screen.getByTestId("model-trend-legend")).toHaveTextContent("kimi-k3");
  });
});
