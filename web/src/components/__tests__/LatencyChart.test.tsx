import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { LatencyChart, buildLatencyRows, formatLatencyTooltip } from "../LatencyChart";

const POINTS = [
  { hour: 1_700_000_000, p50: 1200, p95: 5200, samples: 40 },
  { hour: 1_700_003_600, p50: null, p95: null, samples: 0 },
  { hour: 1_700_007_200, p50: 2400, p95: 9800, samples: 3 },
];

describe("buildLatencyRows", () => {
  it("样本 ≥ 5 才给 P50–P95 区间", () => {
    const rows = buildLatencyRows(POINTS);
    expect(rows[0].band).toEqual([1200, 5200]);
    // 空档与样本不足的小时都不画带
    expect(rows[1].band).toBeNull();
    expect(rows[2].band).toBeNull();
  });

  it("补齐毫秒时间戳（recharts 数值轴用）", () => {
    expect(buildLatencyRows(POINTS)[0].time).toBe(1_700_000_000_000);
  });

  it("P50/P95 任一为 null 时不给带（半条数据不成区间）", () => {
    const rows = buildLatencyRows([
      { hour: 1, p50: 100, p95: null, samples: 40 },
      { hour: 2, p50: null, p95: 900, samples: 40 },
    ]);
    expect(rows[0].band).toBeNull();
    expect(rows[1].band).toBeNull();
  });
});

describe("formatLatencyTooltip", () => {
  it("区间给「下界 – 上界」", () => {
    expect(formatLatencyTooltip([1200, 5200])).toBe("1.2 s – 5.2 s");
  });

  it("单值按延迟格式（≥1s 用 s）", () => {
    expect(formatLatencyTooltip(850)).toBe("850 ms");
  });
});

describe("LatencyChart", () => {
  it("渲染容器与图例（jsdom 无布局，ResponsiveContainer 不产出 SVG）", () => {
    render(<LatencyChart points={POINTS} />);
    expect(screen.getByTestId("latency-chart")).toBeInTheDocument();
    const legend = screen.getByTestId("latency-chart-legend");
    expect(legend).toHaveTextContent("P50 中位数");
    expect(legend).toHaveTextContent("P95 上界");
  });

  it("图例写明口径与窗口天数（分位只覆盖明细保留期）", () => {
    render(<LatencyChart points={POINTS} windowDays={90} />);
    expect(screen.getByTestId("latency-chart-legend")).toHaveTextContent("仅 90 天");
    expect(screen.getByTestId("latency-chart-legend")).toHaveTextContent("只统计成功请求");
  });

  it("首字口径在图例里写明", () => {
    render(<LatencyChart points={POINTS} metric="ttfb" />);
    expect(screen.getByTestId("latency-chart-legend")).toHaveTextContent("首字口径");
  });

  it("耗时口径在图例里写明", () => {
    render(<LatencyChart points={POINTS} metric="latency" />);
    expect(screen.getByTestId("latency-chart-legend")).toHaveTextContent("耗时口径");
  });

  it("样本不足 5 条的小时不画带（避免一两条样本画出尖峰）", () => {
    // 行为在 SVG 层，jsdom 只能确认不抛错；分档逻辑由 band 字段决定
    render(<LatencyChart points={POINTS} />);
    expect(screen.getByTestId("latency-chart")).toBeInTheDocument();
  });

  it("空点集也能渲染（不抛错）", () => {
    render(<LatencyChart points={[]} />);
    expect(screen.getByTestId("latency-chart")).toBeInTheDocument();
  });
});
