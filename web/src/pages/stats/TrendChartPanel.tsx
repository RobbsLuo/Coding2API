/**
 * 指标趋势：主图 + 渠道切换 + 渠道构成（自 `pages/StatsPage.tsx` 原样搬出）。
 *
 * 渠道候选、回落后的口径（scope）仍由页面算好传入：取值依赖时间窗数据，
 * 放页面能和其他图共用同一份判断。
 */
import { Empty, Panel, Tabs } from "../../ui";
import { ProviderShare, UsageChart } from "../../components/UsageChart";
import { providerLabel } from "../../api/providers";
import type { ProviderStats, TimelinePoint } from "../../api/types";

interface TrendChartPanelProps {
  metric: string;
  points: TimelinePoint[];
  scope: string;
  scopeProviders: string[];
  onScopeChange: (value: string) => void;
  showShare: boolean;
  providers: ProviderStats[];
}

export function TrendChartPanel(props: TrendChartPanelProps) {
  const { metric, points, scope, scopeProviders, onScopeChange, showShare, providers } = props;
  return (
    <Panel
      title="指标趋势"
      action={
        /* 渠道切换：一次只画一条线。默认「全部渠道」读后端的 `__total`
           （均值/比率按同一小时的 sum/count 重新相除，不能前端相加）。 */
        scopeProviders.length > 1 ? (
          <Tabs
            value={scope}
            compact
            options={[
              { value: "total", label: "全部渠道" },
              ...scopeProviders.map((provider) => ({
                value: provider, label: providerLabel(provider),
              })),
            ]}
            onChange={onScopeChange}
            testId="chart-scope-tabs"
          />
        ) : undefined
      }
    >
      {points.length === 0 ? (
        <Empty>该范围内没有小时汇总数据</Empty>
      ) : (
        <>
          <UsageChart points={points} metric={metric}
                      scope={scope} />
          {/* 渠道构成只在请求量指标下出现（份额需要「请求数」这个分母）。
              放在主图卡片底部而不是单独一张卡：它只有一行高度，独立成卡会多出
              标题栏 + 上下内边距 + 外框，把一行内容撑成 123px。 */}
          {showShare && (
            <div className="mt-3 border-t border-border/70 pt-2.5" data-testid="provider-share-block">
              <div className="mb-1.5 flex items-baseline justify-between">
                <span className="text-xs font-medium text-muted-foreground">渠道构成</span>
                <span className="text-xs text-muted-foreground">按请求数占比</span>
              </div>
              <ProviderShare
                rows={providers.map((row) => ({
                  provider: row.provider, value: row.requests,
                }))}
                total={providers.reduce(
                  (sum, row) => sum + row.requests, 0)}
              />
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
