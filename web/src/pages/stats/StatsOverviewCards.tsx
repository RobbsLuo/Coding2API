/**
 * 概览指标：四张主卡 + 次要指标条（自 `pages/StatsPage.tsx` 原样搬出）。
 *
 * 派生文案（成功率 / token 提示 / 缓存命中与未命中）就在本组件算，
 * 与搬出前同址同式，不跨组件传中间值。
 */
import { Activity, Coins, Timer, Wallet } from "lucide-react";
import { Metric, Notice } from "../../ui";
import {
  formatCacheRate,
  formatCompact,
  formatCredit,
  formatLatency,
  formatMoney,
  formatNumber,
  formatSpeed,
} from "../../api/display";
import type { StatsOverview } from "../../api/types";

interface StatsOverviewCardsProps {
  overviewFailed: boolean;
  stats: StatsOverview | undefined;
}

export function StatsOverviewCards({ overviewFailed, stats }: StatsOverviewCardsProps) {
  const rate =
    stats?.success_rate === null || stats?.success_rate === undefined
      ? null
      : stats.success_rate;
  const rateText = rate === null ? "—" : `${(rate * 100).toFixed(1)}%`;
  const totalTokens = (stats?.input_tokens ?? 0) + (stats?.output_tokens ?? 0);
  // 输入缓存：命中 = 上报的 cached_tokens；未命中 = 输入 − 命中（缺上报则显示 —）
  const cached = stats?.cached_tokens ?? null;
  const hitText = cached === null ? "—" : formatCompact(cached);
  const missText =
    cached === null || stats?.input_tokens === null || stats?.input_tokens === undefined
      ? "—"
      : formatCompact(stats.input_tokens - cached);
  const tokenHint =
    `输入 ${formatCompact(stats?.input_tokens)}（命中 ${hitText} · 未命中 ${missText}）`
    + ` · 输出 ${formatCompact(stats?.output_tokens)} · 推理 ${formatCompact(stats?.reasoning_tokens)}`
    + ` · 缓存命中率 ${formatCacheRate(cached, stats?.input_tokens)}`;

  return (
    <>
      {overviewFailed && (
        <div data-testid="stats-overview-error">
          <Notice tone="danger">
            总览数据加载失败：服务可能暂时不可用或会话已过期。
            下方指标显示为「—」是取不到数，不是真的没有用量。
          </Notice>
        </div>
      )}

      <section
        aria-label="用量概览统计"
        className="grid grid-cols-2 gap-3 lg:grid-cols-4"
      >
        {/* 四张主卡：请求数 / Token / 成本 / 耗时——正好对应「用了多少、花了多少、
            快不快」三个问题。其余指标（输出速度、Credit、成功率）降为下方一条
            状态条：它们的 hint 太长，占满六张卡会把每张压到 180px、hint 排成六行，
            反而让主指标看不见。 */}
        <Metric label="请求数" value={formatNumber(stats?.requests)} tone="ok"
                hint={`成功 ${formatNumber(stats?.ok_count)} · 成功率 ${rateText}`}
                icon={<Activity className="size-4" />} />
        <Metric
          label="Token 消耗"
          value={formatCompact(totalTokens)}
          hint={tokenHint}
          icon={<Coins className="size-4" />}
        />
        <Metric
          label="成本（估算）"
          value={formatMoney(stats?.cost_cny, "CNY")}
          hint={`美元 ${formatMoney(stats?.cost_usd, "USD")} · 按 OpenRouter 刊例价折算`}
          icon={<Wallet className="size-4" />}
        />
        <Metric
          label="平均耗时"
          value={formatLatency(stats?.avg_latency_ms)}
          hint={`首字延迟 ${formatLatency(stats?.avg_ttfb_ms)}`}
          icon={<Timer className="size-4" />}
        />
      </section>

      {/* 次要指标条：输出速度 / Credit / 缓存命中率。用一条分隔条而不是三张卡，
          避免与上面四张主卡抢视觉权重。 */}
      <section
        aria-label="次要指标"
        className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-border/70 bg-border/70 text-sm sm:grid-cols-3"
      >
        <div className="bg-panel px-4 py-3" data-testid="stat-speed">
          <div className="text-xs text-muted-foreground">输出速度</div>
          <div className="mt-1 text-base font-semibold tabular-nums">
            {formatSpeed(stats?.tps_visible, stats?.tps_estimated)}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {stats?.tps === null
              ? "该范围内没有可用的生成时间样本"
              : `总产出（含思考）${formatSpeed(stats?.tps, stats?.tps_estimated)}`}
          </div>
        </div>
        <div className="bg-panel px-4 py-3" data-testid="stat-cache">
          <div className="text-xs text-muted-foreground">缓存命中率</div>
          <div className="mt-1 text-base font-semibold tabular-nums">
            {formatCacheRate(cached, stats?.input_tokens)}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            命中 {hitText} · 未命中 {missText}
          </div>
        </div>
        <div className="bg-panel px-4 py-3 col-span-2 sm:col-span-1" data-testid="stat-credit">
          <div className="text-xs text-muted-foreground">Credit 消耗</div>
          <div className="mt-1 text-base font-semibold tabular-nums">
            {formatCredit(stats?.credit, stats?.credit_estimated)}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            渠道可选字段；≈ 为推算值
          </div>
        </div>
      </section>
    </>
  );
}
