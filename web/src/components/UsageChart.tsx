import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  formatAxisValue,
  formatChartValue,
  formatNumber,
  METRIC_AXIS_UNIT,
  RATE_METRICS,
} from "../api/display";
import { providerChartColor, providerLabel } from "../api/providers";
import type { TimelinePoint } from "../api/types";
import { ProviderIcon } from "./ProviderIcon";

/** Y 轴宽度：容下紧凑刻度（如「1500万」「850 ms」）。 */
const Y_AXIS_WIDTH = 56;

/** 时间序列点的保留键（不是渠道名），其余键都当作渠道序列。 */
const NON_PROVIDER_KEYS = new Set(["hour", "time", "__total"]);

/**
 * 从数据点里收集出现过的渠道名（稳定排序）。
 *
 * 渠道集合动态取自后端返回的键：新增渠道时后端数据一变，图例与曲线自动
 * 跟上，不需要在前端再维护一份渠道清单。
 */
export function chartProviders(points: TimelinePoint[]): string[] {
  const seen = new Set<string>();
  for (const point of points) {
    for (const key of Object.keys(point)) {
      if (!NON_PROVIDER_KEYS.has(key)) seen.add(key);
    }
  }
  return [...seen].sort();
}

/** 渠道在该窗口内是否真有数据（全 null / 全 0 的渠道不进图例）。 */
export function hasData(points: TimelinePoint[], provider: string): boolean {
  return points.some((point) => {
    const value = point[provider];
    return value !== null && value !== undefined && value !== 0;
  });
}

/** 图表的视图口径：总量（全渠道合计）或单渠道。 */
export type ChartScope = "total" | string;

/**
 * 比率类指标的 Y 轴定义域。
 *
 * 固定 `[0, 1]` 诚实但没信息量：成功率常年在 98%–100% 之间，整条线会贴在
 * 顶部、波动完全看不见（实测 30 天 min=98.55%，固定 0–100% 时曲线是一条直线）。
 * 但也不能让 recharts 的 `auto` 自己决定——那会把 98.5% 画成「接近 0」，
 * 而且行内的毫秒时间戳会污染定义域（曾算出 400% 的上界）。
 *
 * 取舍：**数据全部高于 `RATE_FLOOR` 时，轴从 `RATE_FLOOR` 起**，让窄幅波动
 * 可见；否则从 0 起，保持「0 就是 0」的直觉。截断轴必须标注（见调用处的
 * `truncated`），否则读者会以为底部就是 0——那是另一种撒谎。
 */
const RATE_FLOOR = 0.9;

export function rateAxisDomain(values: (number | null | undefined)[]): {
  domain: [number, number];
  ticks: number[] | undefined;
  truncated: boolean;
} {
  const present = values.filter((value): value is number =>
    typeof value === "number" && Number.isFinite(value));
  const low = present.length > 0 ? Math.min(...present) : 0;
  if (low >= RATE_FLOOR) {
    return {
      domain: [RATE_FLOOR, 1],
      ticks: [RATE_FLOOR, 0.95, 1].filter((tick) => tick >= RATE_FLOOR && tick <= 1),
      truncated: true,
    };
  }
  return { domain: [0, 1], ticks: [0, 0.25, 0.5, 0.75, 1], truncated: false };
}

/**
 * 单请求成本的 Y 轴上界：取「数据最大值 ×1.1」向上取整到 1/2/5×10ⁿ 的整数档。
 *
 * 为什么要自己算：recharts 的 `auto` 会直接拿最小值当刻度（实测出现
 * `0.0085 / 0.017 / 0.0255`），读数时得做除法。规整成 `0.02 / 0.04` 这类
 * 整档后，刻度看一眼就知道量级。
 */
export function niceCostCeiling(max: number): number {
  if (!(max > 0)) return 1;
  const target = max * 1.1;
  const magnitude = 10 ** Math.floor(Math.log10(target));
  for (const step of [1, 2, 2.5, 5]) {
    const candidate = step * magnitude;
    if (candidate >= target) return candidate;
  }
  // 5×10ⁿ 不够时进下一档（如 7×10ⁿ → 10×10ⁿ）；10×10ⁿ 一定够，故不再循环
  return 10 * magnitude;
}

/**
 * 指标时间序列曲线（recharts）。
 *
 * **时间轴是数值轴**（`dataKey="hour"` + 秒级时间戳 + `type="number"`）：用格式化
 * 字符串当分类轴会把缺失的小时压成等宽——7 天里空 3 天的那一段会被画得和相邻
 * 一天一样宽，等于伪造时间。后端已按小时补齐稠密桶，这里再让 recharts 按真实
 * 时间定位，缺测小时自然表现为空位。
 *
 * **默认只画一条线**（`scope="total"`，全渠道合计）：多系列叠加在 7 天窗口下会
 * 互相遮挡，且「今天总量多少」才是这个面板要回答的问题。要按渠道拆看，由调用方
 * 传 `scope` 切到单个渠道（`scale` 一次只画一个渠道），不是把所有渠道叠在一起。
 *
 * 均值 / 比率类指标的缺失桶是 `null`：`connectNulls={false}` 让线在空档断开，
 * 不把「没有样本」连成一条假的斜线。
 */
export function UsageChart({
  points,
  metric,
  scope = "total",
}: {
  points: TimelinePoint[];
  metric?: string;
  scope?: ChartScope;
}) {
  const currentMetric = metric ?? "requests";
  const isRate = RATE_METRICS.has(currentMetric);
  const data = useMemo(
    () => points.map((point) => ({ ...point, time: point.hour * 1000 })),
    [points],
  );
  // 单位标签只对不自带单位的指标（计数 / token）显示；耗时/首字的刻度已含 ms/s
  const axisUnit = METRIC_AXIS_UNIT[currentMetric];
  // 单渠道视图：只画该渠道；总量视图：画后端算好的 `__total`
  const series = scope === "total" ? "__total" : scope;
  const color = scope === "total" ? "var(--chart-1)" : providerChartColor(scope);
  const label = scope === "total" ? "全部渠道" : providerLabel(scope);
  // 比率指标：轴下界按数据决定（见 rateAxisDomain），截断时必须在轴顶写明
  const rateAxis = isRate
    ? rateAxisDomain(points.map((point) => point[series]))
    : null;
  // 单请求成本：量级小且取值分散，用规整上界（见 niceCostCeiling）
  const costAxis = currentMetric === "cost_per_request"
    ? [0, niceCostCeiling(Math.max(0, ...points.map((point) => point[series] ?? 0)))]
    : null;
  const empty = points.length === 0 || points.every(
    (point) => point[series] === null || point[series] === undefined);

  return (
    <div data-testid="usage-chart">
      {/* 绘图区高度：桌面 208px（h-52），窄屏 224px（h-56）。
          原来固定 256px（h-64）——在 1280px 宽、近千像素的卡片里，一条折线
          上下留白过多，纵轴利用率只有一半；图表本身读的是「趋势与拐点」，
          不需要那么高。窄屏反而要略高：横向刻度少，太扁会看不出起伏。 */}
      <div className="h-56 w-full sm:h-52">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 24, right: 12, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="g-usage" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.22} />
                <stop offset="100%" stopColor={color} stopOpacity={0} />
              </linearGradient>
            </defs>
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
              // 必须显式给 dataKey：不指定时 recharts 会把行内所有数值字段
              // （含毫秒时间戳 `time`）纳入定义域，轴会算出 400% 这类不可能的上界。
              dataKey={series}
              domain={rateAxis ? rateAxis.domain : (costAxis ?? [0, "auto"])}
              ticks={rateAxis?.ticks}
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
              formatter={(value) => [formatChartValue(Number(value), currentMetric), label]}
            />
            <Area
              type="linear"
              dataKey={series}
              name={label}
              stroke={color}
              strokeWidth={2}
              fill="url(#g-usage)"
              // 空档断线：均值 / 比率类指标的 null 是「无样本」，不是 0
              connectNulls={false}
              isAnimationActive={false}
              dot={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <div
        className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-xs text-muted-foreground"
        data-testid="usage-chart-legend"
      >
        <span className="inline-flex items-center gap-1.5">
          {scope === "total" ? (
            <span aria-hidden className="size-2 rounded-full" style={{ background: color }} />
          ) : (
            <ProviderIcon provider={scope} size={12} />
          )}
          {label}
        </span>
        {rateAxis?.truncated && (
          /* 轴从 90% 起：不说明就是撒谎（读者会以为底部是 0） */
          <span data-testid="usage-chart-axis-note">
            Y 轴自 90% 起（便于看清窄幅波动）
          </span>
        )}
        {empty && <span data-testid="usage-chart-empty">该范围内没有可绘制的数据</span>}
      </div>
    </div>
  );
}

/** X 轴刻度共用：只依赖 `hour`，故接受任何带 hour 的点（TimelinePoint / PercentilePoint）。 */
type HourPoint = { hour: number };

/**
 * X 轴刻度文案：短窗口只显示时刻（`14时`），跨天窗口显示「月-日 时」。
 *
 * 窗口是否跨天由数据首末时间决定，不按点数猜：24 小时窗口即使只有 3 个点也
 * 应该只写时刻，而 7 天窗口的第一个点必须带日期。
 */
export function formatTimeTick(timestamp: number, points: HourPoint[],
                               withDate = false): string {
  const date = new Date(timestamp);
  const clock = `${String(date.getHours()).padStart(2, "0")}时`;
  if (!withDate && spansSingleDay(points)) return clock;
  const day = `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  return `${day} ${clock}`;
}

/** 首末点是否落在同一天（本地时区）。 */
function spansSingleDay(points: HourPoint[]): boolean {
  if (points.length === 0) return true;
  const first = new Date(points[0].hour * 1000);
  const last = new Date(points[points.length - 1].hour * 1000);
  return first.getFullYear() === last.getFullYear()
    && first.getMonth() === last.getMonth()
    && first.getDate() === last.getDate();
}

/**
 * 渠道构成：一条 100% 宽度的堆叠条 + 图例。
 *
 * 不用「每渠道一条横条」：渠道通常只有 2–4 个，横条在 1100px 宽的卡片里会被
 * 拉到极长，读起来散且浪费高度。堆叠条本身就表达「构成」（份额合计 100%），
 * 宽度被自然填满，高度只占一行。
 *
 * 也不做饼 / 环：渠道数常到 8，环形图在 5 个以上就分不清扇区，且无法按大小排序。
 */
export function ProviderShare({
  rows,
  total,
}: {
  rows: { provider: string; value: number }[];
  total: number;
}) {
  const sorted = [...rows].sort((a, b) => b.value - a.value);
  const sum = total > 0 ? total : 1;
  const items = sorted.map((row) => ({
    ...row,
    percent: (row.value / sum) * 100,
  }));
  return (
    <div data-testid="provider-share">
      <div
        className="flex h-2.5 w-full overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label={`渠道构成：${items
          .map((row) => `${providerLabel(row.provider)} ${row.percent.toFixed(1)}%`)
          .join("，")}`}
      >
        {items.map((row) => (
          <span
            key={row.provider}
            className="block h-full first:rounded-l-full last:rounded-r-full"
            style={{
              width: `${row.percent.toFixed(2)}%`,
              background: providerChartColor(row.provider),
            }}
          />
        ))}
      </div>
      <ul className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {items.map((row) => (
          <li key={row.provider} className="flex items-center gap-1.5">
            <span
              aria-hidden
              className="size-2 shrink-0 rounded-full"
              style={{ background: providerChartColor(row.provider) }}
            />
            <span className="truncate">{providerLabel(row.provider)}</span>
            <span className="tabular-nums text-muted-foreground">
              {row.percent.toFixed(1)}%
            </span>
            <span className="tabular-nums text-muted-foreground/70">
              {formatNumber(row.value)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
