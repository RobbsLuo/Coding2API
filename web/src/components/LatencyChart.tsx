import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { PercentilePoint } from "../api/types";
import { formatAxisValue, formatLatency } from "../api/display";
import { formatTimeTick } from "./UsageChart";

/** Y 轴宽度：容下「120 s」这类刻度。 */
const Y_AXIS_WIDTH = 56;

/**
 * 分位图的数据整形：给每个点补 `time`（毫秒）与 `band`（P50–P95 区间）。
 *
 * 抽成导出函数是为了可测：jsdom 不产出 SVG，图表内部的 `tickFormatter` /
 * `formatter` 回调根本不会执行，把逻辑留在 JSX 里就永远是覆盖盲区。
 *
 * `band` 只在该小时样本 ≥ 5 时才给值：一两条样本算出的 P95 没有统计意义，
 * 画成带会让人误以为「长尾很重」。样本不足时给 null，带断开，折线照旧。
 */
export function buildLatencyRows(points: PercentilePoint[]) {
  return points.map((point) => ({
    ...point,
    time: point.hour * 1000,
    band: point.samples >= 5 && point.p50 !== null && point.p95 !== null
      ? [point.p50, point.p95]
      : null,
  }));
}

/** Tooltip 值格式化：区间给「a – b」，单值给原值。 */
export function formatLatencyTooltip(value: number | [number, number]): string {
  if (Array.isArray(value)) {
    return `${formatLatency(value[0])} – ${formatLatency(value[1])}`;
  }
  return formatLatency(value);
}

/**
 * 延迟分位图：P50 中位数线 + P95 上界带。
 *
 * 为什么不用「均值折线」：池级均值会把长尾摊平——实测某天均值 10 s 时 P95 是
 * 21 s，而「少数请求卡住」正是最该被看见的故障形态。中位数说明「典型体验」，
 * P95 与它的间距说明「有多少请求在受苦」，两条线一起才回答得了「慢在哪」。
 *
 * 带不是「置信区间」：它只是 P50 到 P95 之间的区域，用来把长尾的量级视觉化，
 * 不要写成统计置信度。
 */
export function LatencyChart({
  points,
  metric = "latency",
  windowDays = 90,
}: {
  points: PercentilePoint[];
  metric?: "latency" | "ttfb";
  windowDays?: number;
}) {
  const data = buildLatencyRows(points);
  const unit = metric === "ttfb" ? "首字" : "耗时";

  return (
    <div data-testid="latency-chart">
      <div className="h-56 w-full sm:h-52">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} margin={{ top: 24, right: 12, left: 0, bottom: 0 }}>
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
              dataKey="p95"
              domain={[0, "auto"]}
              tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              tickLine={false}
              axisLine={false}
              tickFormatter={(value) => formatAxisValue(Number(value), metric)}
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
              formatter={(value, name) => [
                formatLatencyTooltip(value as number | [number, number]), name,
              ]}
            />
            {/* P95 上界带：P50 到 P95 之间的区域，把长尾量级视觉化 */}
            <Area
              dataKey="band"
              name="P50–P95 区间"
              stroke="none"
              fill="var(--chart-1)"
              fillOpacity={0.12}
              connectNulls={false}
              isAnimationActive={false}
            />
            <Line
              type="linear"
              dataKey="p50"
              name="P50 中位数"
              stroke="var(--chart-1)"
              strokeWidth={2}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
            <Line
              type="linear"
              dataKey="p95"
              name="P95 上界"
              stroke="var(--chart-3)"
              strokeWidth={1.5}
              strokeDasharray="4 3"
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div
        className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-xs text-muted-foreground"
        data-testid="latency-chart-legend"
      >
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden className="h-0.5 w-3 rounded-full" style={{ background: "var(--chart-1)" }} />
          P50 中位数
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden
            className="h-0.5 w-3 rounded-full"
            style={{
              backgroundImage:
                "repeating-linear-gradient(to right, var(--chart-3) 0 4px, transparent 4px 7px)",
            }}
          />
          P95 上界
        </span>
        <span className="text-muted-foreground/80">
          只统计成功请求 · 取自逐请求明细（仅 {windowDays} 天）· {unit}口径
        </span>
      </div>
    </div>
  );
}
