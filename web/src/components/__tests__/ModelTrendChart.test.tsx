import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ModelLegend, ModelTrendChart } from "../ModelTrendChart";

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
});
