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
  RATE_METRICS,
  SPARSE_METRICS,
} from "../api/display";
import { formatTimeTick, niceCostCeiling, rateAxisDomain } from "./UsageChart";

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

/** 在画的模型线上的全部有限值（`null` 是「无样本」，不参与定义域）。 */
function visibleValues(points: ModelTimelinePoint[], models: string[]): number[] {
  const values: number[] = [];
  for (const point of points) {
    for (const model of models) {
      const value = point[model];
      if (typeof value === "number" && Number.isFinite(value)) values.push(value);
    }
  }
  return values;
}

/**
 * 模型趋势图的 Y 轴定义域。
 *
 * 不能只给 `dataKey` 再让 recharts `"auto"`：那只会按**一条**线算上限
 * （早先是 `models[0]`），Top2/Top3 在耗时 / 成本类指标上峰值更高时会顶出
 * 绘图区被裁掉。上限取所有在画的模型的**公共最大值**。
 *
 * 比率 / 单请求成本复用主图的口径（`rateAxisDomain` / `niceCostCeiling`）：
 * 否则 98%–100% 会贴顶成直线、成本刻度会碎成 `0.0085 / 0.017`。
 */
export function modelTrendAxis(
  points: ModelTimelinePoint[],
  models: string[],
  metric: string,
): { domain: [number, number]; ticks: number[] | undefined; truncated: boolean } {
  const values = visibleValues(points, models.slice(0, MAX_LINES));
  if (RATE_METRICS.has(metric)) return rateAxisDomain(values);
  if (metric === "cost_per_request") {
    return {
      domain: [0, niceCostCeiling(Math.max(0, ...values))],
      ticks: undefined,
      truncated: false,
    };
  }
  const max = Math.max(0, ...values);
  // 全空 / 全 0 时给 [0, 1]，避免退化成一个点的轴
  return { domain: [0, max > 0 ? max : 1], ticks: undefined, truncated: false };
}

/**
 * 按模型指标趋势（recharts）。
 *
 * **时间轴是数值轴**：`hour` 是 UTC 秒，`scale="time"` 让点按真实时间定位，
 * 缺测小时表现为空位而不是被压缩成等宽（与 `UsageChart` 同口径）。
 *
 * **最多 3 条线**（`MAX_LINES`）：模型数超过 3 时只画请求量 Top 3，其余在
 * 「按模型」分组表里看。单系列时不列模型名——标题与 Top 文案已经说明是哪条
 * （轴口径说明仍要渲染，见 `ModelLegend`）。
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
  const isRate = RATE_METRICS.has(currentMetric);
  const data = points.map((point) => ({ ...point, time: point.hour * 1000 }));
  // 单位标签只对不自带单位的指标（计数 / token）显示；耗时/首字的刻度已含 ms/s
  const axisUnit = METRIC_AXIS_UNIT[currentMetric];
  const shown = models.slice(0, MAX_LINES);
  const axis = modelTrendAxis(points, models, currentMetric);

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
            allowDecimals={!isRate}
            width={Y_AXIS_WIDTH}
            // 显式数值定义域：交给 recharts 的 `dataKey` + `"auto"` 只会按一条线
            // 算上限，其余模型线会被裁掉（见 modelTrendAxis）
            domain={axis.domain}
            ticks={axis.ticks}
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

/** 图例：每个模型一个色点（单系列时由标题说明，不渲染名字）+ 轴口径说明。 */
export function ModelLegend({ models, note }: { models: string[]; note?: string }) {
  const shown = models.slice(0, MAX_LINES);
  const multi = shown.length > 1;
  // 单系列且无说明：标题已说明是哪条，整行不渲染
  if (!multi && !note) return null;
  return (
    <div
      className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-xs text-muted-foreground"
      data-testid="model-trend-legend"
    >
      {multi && shown.map((model, index) => (
        <span key={model} className="inline-flex items-center gap-1.5">
          <span
            aria-hidden
            className="size-2 rounded-full"
            style={{ background: COLORS[index % COLORS.length] }}
          />
          <span className="max-w-40 truncate" title={model}>{model}</span>
        </span>
      ))}
      {note && <span data-testid="model-trend-axis-note">{note}</span>}
    </div>
  );
}
