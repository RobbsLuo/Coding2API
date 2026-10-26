import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ModelTimelinePoint } from "../api/types";
import {
  formatAxisValue,
  formatChartValue,
  METRIC_AXIS_UNIT,
  SPARSE_METRICS,
} from "../api/display";
import { formatTimeTick } from "./UsageChart";

/**
 * 模型线专用色（与渠道色 `--chart-N` 分开）。
 *
 * 渠道色在 `providers.ts` 里是「渠道 → 颜色」的唯一来源（MASTER §2.4），
 * 语义是渠道；这张图上的线代表**模型**，复用渠道色会让同一页两张图的
 * 颜色语义对不上（看图例的「青绿 = CB」与「青绿 = 某个模型」混在一起）。
 */
const COLORS = [
  "var(--model-1)",
  "var(--model-2)",
  "var(--model-3)",
];

/** Y 轴宽度：容下紧凑刻度（如「1500万」「850 ms」）。 */
const Y_AXIS_WIDTH = 56;

/**
 * 一张图最多画几条模型线。
 *
 * 超过 3 条就分不清（曲线互相交叉、图例要折行），而「哪个模型在涨」用 Top 3
 * 已经能回答；更多模型交给下方的「按模型」分组表。后端 `top` 默认 6，这里再
 * 收敛一次，保证图本身可读。
 */
const MAX_LINES = 3;

/**
 * 按模型指标趋势（recharts）。
 *
 * **时间轴是数值轴**：`hour` 是 UTC 秒，`scale="time"` 让点按真实时间定位，
 * 缺测小时表现为空位而不是被压缩成等宽（与 `UsageChart` 同口径）。
 *
 * **最多 3 条线**（`MAX_LINES`）：模型数超过 3 时只画请求量 Top 3，其余在
 * 「按模型」分组表里看。单系列时不画图例——标题与 Top 文案已经说明是哪条。
 */
export function ModelTrendChart({
  points,
  models,
  metric,
}: {
  points: ModelTimelinePoint[];
  models: string[];
  metric?: string;
}) {
  const currentMetric = metric ?? "requests";
  const sparse = SPARSE_METRICS.has(currentMetric);
  const data = points.map((point) => ({ ...point, time: point.hour * 1000 }));
  // 单位标签只对不自带单位的指标（计数 / token）显示；耗时/首字的刻度已含 ms/s
  const axisUnit = METRIC_AXIS_UNIT[currentMetric];
  const shown = models.slice(0, MAX_LINES);

  return (
    <div className="h-56 w-full sm:h-52" data-testid="model-trend-chart">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 24, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="time"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={{ stroke: "var(--border)" }}
            tickFormatter={(value) => formatTimeTick(Number(value), points)}
            minTickGap={40}
          />
          <YAxis
            allowDecimals={false}
            width={Y_AXIS_WIDTH}
            // 显式 dataKey：不指定时 recharts 会把 hour / time 也纳入定义域
            dataKey={shown[0]}
            domain={[0, "auto"]}
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={false}
            tickFormatter={(value) => formatAxisValue(Number(value), currentMetric)}
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
            labelFormatter={(value) => formatTimeTick(Number(value), points, true)}
            formatter={(value, name) => [formatChartValue(Number(value), currentMetric), name]}
          />
          {shown.map((model, index) => (
            <Line
              key={model}
              type="linear"
              dataKey={model}
              name={model}
              stroke={COLORS[index % COLORS.length]}
              strokeWidth={2}
              dot={false}
              // 空档断线：均值 / 比率类的 null 是「无样本」，不能连成假斜线
              connectNulls={!sparse}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** 图例：每个模型一个色点（单系列时由标题说明，不渲染）。 */
export function ModelLegend({ models }: { models: string[] }) {
  const shown = models.slice(0, MAX_LINES);
  if (shown.length <= 1) return null;
  return (
    <div
      className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-xs text-muted-foreground"
      data-testid="model-trend-legend"
    >
      {shown.map((model, index) => (
        <span key={model} className="inline-flex items-center gap-1.5">
          <span
            aria-hidden
            className="size-2 rounded-full"
            style={{ background: COLORS[index % COLORS.length] }}
          />
          <span className="max-w-40 truncate" title={model}>{model}</span>
        </span>
      ))}
    </div>
  );
}
