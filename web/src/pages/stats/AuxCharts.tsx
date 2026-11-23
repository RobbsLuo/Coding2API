/**
 * 辅助图区：延迟分位图（延迟类指标才有）+ 按模型趋势（自 `pages/StatsPage.tsx` 原样搬出）。
 */
import { cn } from "cn";
import { Empty, Panel } from "../../ui";
import { LatencyChart } from "../../components/LatencyChart";
import { ModelLegend, ModelTrendChart } from "../../components/ModelTrendChart";
import type {
  ModelTimelineResponse,
  PercentileTimelineResponse,
} from "../../api/types";
import { LATENCY_METRICS } from "./constants";

interface AuxChartsProps {
  metric: string;
  percentile: PercentileTimelineResponse | undefined;
  modelTimeline: ModelTimelineResponse | undefined;
  modelAxisTruncated: boolean;
}

export function AuxCharts(props: AuxChartsProps) {
  const { metric, percentile, modelTimeline, modelAxisTruncated } = props;
  return (
    <>
      {/* 主图之外的两张辅助图：延迟类指标多一张分位图。
          单张时必须占满整行——否则 `lg:grid-cols-2` 只放一个子元素，
          右半边整块留白（900–1200px 下尤其明显）。 */}
      {/* 辅助图区：延迟类指标并排「分位图 + 模型趋势」；其余指标只有模型趋势，
          独占整行（否则 `lg:grid-cols-2` 只放一个子元素，右半边整块留白）。 */}
      <section className={cn(
        "grid gap-4",
        LATENCY_METRICS.has(metric) && "lg:grid-cols-2",
      )}>
        {/* 延迟类指标：主图之外再给一张分位图（P50 中位数 + P95 上界）。
            均值会摊平长尾，分位才看得出「有多少请求在受苦」。 */}
        {LATENCY_METRICS.has(metric) && (
          <Panel title={metric === "ttfb" ? "首字延迟分位" : "耗时分位"} action={
            <span className="text-xs text-muted-foreground">
              P50 中位数 · P95 上界 · 成功请求
            </span>
          }>
            {((percentile?.points.length ?? 0) === 0) ? (
              <Empty data-testid="no-latency-percentile">该范围内没有可用的延迟样本</Empty>
            ) : (
              <LatencyChart
                points={percentile?.points ?? []}
                metric={percentile?.metric ?? "latency"}
                windowDays={percentile?.window_days ?? 90}
              />
            )}
          </Panel>
        )}

        <Panel title="按模型趋势" action={
          modelTimeline?.models.length ? (
            <span className="text-xs text-muted-foreground">
              请求量 Top {Math.min(3, modelTimeline.models.length)} 模型
            </span>
          ) : undefined
        }>
          {(modelTimeline?.points.length ?? 0) === 0 ? (
            <Empty data-testid="no-model-trend">该范围内没有小时汇总数据</Empty>
          ) : (
            <>
              <ModelTrendChart
                points={modelTimeline?.points ?? []}
                models={modelTimeline?.models ?? []}
                metric={metric}
              />
              <ModelLegend
                models={modelTimeline?.models ?? []}
                note={modelAxisTruncated ? "Y 轴自 90% 起（便于看清窄幅波动）" : undefined}
              />
            </>
          )}
        </Panel>
      </section>
    </>
  );
}
