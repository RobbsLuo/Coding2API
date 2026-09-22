import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ModelTimelinePoint } from "../api/types";
import { formatAxisValue, formatChartValue, METRIC_AXIS_UNIT } from "../api/display";

const COLORS = [
  "var(--chart-1)",
  "var(--chart-3)",
  "var(--chart-2)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--primary)",
];

/** Y 轴宽度：容下紧凑刻度（如「1500万」「850 ms」）。 */
const Y_AXIS_WIDTH = 56;

/** 按模型指标趋势：每小时各 Top N 模型一条曲线（recharts）。 */
export function ModelTrendChart({
  points,
  models,
  metric,
}: {
  points: ModelTimelinePoint[];
  models: string[];
  metric?: string;
}) {
  const data = points.map((point) => ({
    ...point,
    time: new Date(point.hour * 1000).toLocaleString("zh-CN", {
      hour12: false,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
    }),
  }));
  // 单位标签只对不自带单位的指标（计数 / token）显示；耗时/首字的刻度已含 ms/s
  const axisUnit = METRIC_AXIS_UNIT[metric ?? "requests"];

  return (
    <div className="h-64 w-full" data-testid="model-trend-chart">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 32, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="time"
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={{ stroke: "var(--border)" }}
            interval="preserveStartEnd"
            minTickGap={40}
          />
          <YAxis
            allowDecimals={false}
            width={Y_AXIS_WIDTH}
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={false}
            tickFormatter={(value) => formatAxisValue(Number(value), metric ?? "requests")}
            label={
              axisUnit
                ? {
                    value: axisUnit,
                    position: "top",
                    angle: 0,
                    offset: 13,
                    style: { fontSize: 11, fill: "var(--muted-foreground)", textAnchor: "middle" },
                  }
                : undefined
            }
          />
          <Tooltip
            contentStyle={{
              background: "var(--popover)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              fontSize: 12,
              color: "var(--popover-foreground)",
            }}
            labelStyle={{ fontWeight: 600 }}
            formatter={(value, name) => [formatChartValue(Number(value), metric ?? "requests"), name]}
          />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {models.map((model, index) => (
            <Line
              key={model}
              type="monotone"
              dataKey={model}
              name={model}
              stroke={COLORS[index % COLORS.length]}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
